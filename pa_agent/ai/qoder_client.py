"""Qoder CN API client for PA Agent.

Implements the same interface as :class:`pa_agent.ai.deepseek_client.DeepSeekClient`
(``stream_chat`` and ``update_provider``) but talks to Qoder CN's local
sidecar WebSocket API at ``ws://127.0.0.1:36510/ws``.

Protocol: JSON-RPC 2.0 over LSP Content-Length framing.

  1. Connect with ``Cosy-MachineToken`` header
  2. Send ``initialize`` request
  3. Send ``chat/ask`` request with ``questionText`` + model config
  4. Receive streaming ``chat/answer`` notifications (text deltas)
  5. Receive ``chat/finish`` notification (completion)
  6. Receive ``context/usage/sync`` notification (token usage)

Thinking blocks are delimited by `` ````think::{THINK_TIME}`` `` markers
and routed to ``on_reasoning_token``; the rest is content.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, TYPE_CHECKING

from pa_agent.ai.deepseek_client import AIReply, AIUsage, CancelledError
from pa_agent.ai.json_validator import _extract_outer_json_object
from pa_agent.ai.qoder_connector import (
    _QODER_WS_URL,
    is_openclaw_qc_model,
    is_qoder_cn_sidecar_running,
    resolve_qoder_cn_api_model,
)
from pa_agent.config.settings import AIProviderSettings

if TYPE_CHECKING:
    from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)

_CANCEL_POLL_INTERVAL_S = 0.3
_RECV_TIMEOUT_S = 0.5

# Thinking block markers (from Qoder CN chat/answer stream).
# Full markers include surrounding newlines and backtick fences.
_THINK_START_MARKER = "\n````think::{THINK_TIME}\n"
_THINK_END_MARKER = "\n````\n"


def _extract_json_content(raw: str) -> tuple[str, str]:
    """Split model output into (json_content, preceding_reasoning).

    Qoder CN's agent system prompt may cause the model to emit analysis
    text before the actual JSON answer.  This function finds the first
    top-level ``{...}`` object and returns it as *content*; any text
    before it is returned as *reasoning* so PA Agent's validation logic
    sees clean JSON.
    """
    text = raw.strip()
    if not text:
        return "", ""

    brace_idx = text.find("{")
    if brace_idx < 0:
        # No JSON object at all — return as-is, no reasoning.
        return text, ""

    preceding = text[:brace_idx].strip()
    json_part = _extract_outer_json_object(text[brace_idx:])
    return json_part, preceding


def _messages_to_question_text(
    messages: list[dict[str, Any]],
) -> str:
    """Flatten OpenAI-style messages into a single questionText for Qoder CN.

    Qoder CN's ``chat/ask`` only accepts a single ``questionText`` string,
    so we merge system + user + assistant messages into one prompt.

    If the last message is a user message and there's a system message,
    we prepend the system prompt to the user's question.
    """
    if not messages:
        return ""

    # If only one message, use it directly.
    if len(messages) == 1:
        content = messages[0].get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(b.get("text", "")) for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return str(content).strip()

    # Multiple messages: flatten with role labels.
    parts: list[str] = []
    for msg in messages:
        role = str(msg.get("role", "user")).strip()
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(b.get("text", "")) for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        content = str(content).strip()
        if not content:
            continue
        if role == "system":
            parts.append(f"[System]\n{content}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{content}")
        else:
            parts.append(f"[User]\n{content}")
    return "\n\n".join(parts)


def _build_chat_ask_params(
    *,
    question_text: str,
    model_key: str,
    request_id: str,
) -> dict[str, Any]:
    """Build the ``chat/ask`` request params (mirrors Qoder CN desktop).

    Key: disable agent tools, project rules, and auto memory to avoid
    prompt bloat (69K+ tokens of tool definitions get injected in agent
    mode, which confuses the model and causes it to waste output tokens
    on thinking/repetition).
    """
    return {
        "sessionId": "",
        "requestId": request_id,
        "questionText": question_text,
        "mode": "agent",
        "sessionType": "assistant",
        "chatTask": "free_input",
        "stream": True,
        "source": 1,
        "isReply": False,
        "taskDefinitionType": "system",
        "shellType": "",
        "codeLanguage": "",
        "preferredLanguage": "zh-cn",
        "closeTypewriter": True,
        # Disable all agent-side prompt injections to keep prompt lean.
        "pluginPayloadConfig": {
            "isEnableProjectRule": False,
            "isEnableAskAgent": False,
            "isEnableAutoMemory": False,
        },
        "chatContext": {
            "text": question_text,
            "localeLang": "zh-cn",
            "preferredLanguage": "zh-cn",
        },
        "extra": {
            "modelConfig": {
                "key": model_key,
            },
        },
    }


def _send_lsp(ws, method: str, params: Any = None, id_: str | None = None) -> None:
    """Send a JSON-RPC message with LSP Content-Length framing."""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    raw = json.dumps(msg)
    framed = f"Content-Length: {len(raw)}\r\n\r\n{raw}"
    ws.send(framed)


def _parse_lsp_messages(data: str) -> list[dict[str, Any]]:
    """Parse one or more LSP-framed JSON-RPC messages from a buffer."""
    out: list[dict[str, Any]] = []
    rest = data
    while True:
        idx = rest.find("Content-Length:")
        if idx < 0:
            break
        rest = rest[idx:]
        end = rest.find("\r\n\r\n")
        if end < 0:
            end = rest.find("\n\n")
            if end < 0:
                break
            body_start = end + 2
        else:
            body_start = end + 4
        try:
            length = int(
                rest.split("Content-Length:")[1]
                .strip()
                .split("\n")[0]
                .strip()
            )
        except (ValueError, IndexError):
            break
        body = rest[body_start : body_start + length]
        if len(body) < length:
            break
        try:
            out.append(json.loads(body))
        except json.JSONDecodeError:
            pass
        rest = rest[body_start + length :]
    return out


class _ThinkBlockParser:
    """Stateful parser that separates thinking blocks from content.

    Thinking blocks are delimited by ``think::{THINK_TIME}`` (start) and
    ```````` (end). Text inside thinking blocks is reasoning; text outside
    is content.
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._in_thinking = False

    def feed(
        self,
        text: str,
        on_content: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> None:
        """Feed a text chunk and emit segments via callbacks."""
        self._buffer += text
        while self._buffer:
            if self._in_thinking:
                idx = self._buffer.find(_THINK_END_MARKER)
                if idx >= 0:
                    seg = self._buffer[:idx]
                    # Strip surrounding whitespace/newlines from the end marker
                    # context.
                    seg = seg.rstrip("\r\n")
                    if seg and on_reasoning is not None:
                        on_reasoning(seg)
                    self._buffer = self._buffer[idx + len(_THINK_END_MARKER) :]
                    self._in_thinking = False
                else:
                    # Might have a partial end marker at the buffer tail.
                    partial = self._partial_marker_suffix(
                        self._buffer, _THINK_END_MARKER
                    )
                    if partial:
                        seg = self._buffer[: -len(partial)]
                        if seg and on_reasoning is not None:
                            on_reasoning(seg)
                        self._buffer = partial
                    else:
                        if self._buffer and on_reasoning is not None:
                            on_reasoning(self._buffer)
                        self._buffer = ""
                    break
            else:
                idx = self._buffer.find(_THINK_START_MARKER)
                if idx >= 0:
                    seg = self._buffer[:idx]
                    seg = seg.rstrip("\r\n")
                    if seg and on_content is not None:
                        on_content(seg)
                    self._buffer = self._buffer[idx + len(_THINK_START_MARKER) :]
                    self._in_thinking = True
                else:
                    partial = self._partial_marker_suffix(
                        self._buffer, _THINK_START_MARKER
                    )
                    if partial:
                        seg = self._buffer[: -len(partial)]
                        if seg and on_content is not None:
                            on_content(seg)
                        self._buffer = partial
                    else:
                        if self._buffer and on_content is not None:
                            on_content(self._buffer)
                        self._buffer = ""
                    break

    def flush(
        self,
        on_content: Callable[[str], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> None:
        """Emit any remaining buffered text."""
        if not self._buffer:
            return
        if self._in_thinking:
            if on_reasoning is not None:
                on_reasoning(self._buffer.strip("\r\n"))
        else:
            if on_content is not None:
                on_content(self._buffer.strip("\r\n"))
        self._buffer = ""

    @staticmethod
    def _partial_marker_suffix(text: str, marker: str) -> str:
        """Return the suffix of *text* that is a prefix of *marker*, or ''."""
        max_len = min(len(text), len(marker) - 1)
        for length in range(max_len, 0, -1):
            if text.endswith(marker[:length]):
                return marker[:length]
        return ""


class QoderClient:
    """WebSocket streaming client for Qoder CN's local sidecar API."""

    def __init__(
        self,
        settings: AIProviderSettings,
        logger_: logging.Logger | None = None,
    ) -> None:
        self._settings = settings
        self._log = logger_ or logger

    def update_provider(self, settings: AIProviderSettings) -> None:
        """Replace in-memory provider settings (token refresh / fallback)."""
        self._settings = settings

    # ── Public API ────────────────────────────────────────────────────────────

    def stream_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        on_reasoning_token: Callable[[str], None] | None = None,
        on_content_token: Callable[[str], None] | None = None,
        thinking: bool | None = None,
        reasoning_effort: str | None = None,
        cancel_token: "CancelToken | None" = None,
        timeout_s: float = 600.0,
    ) -> AIReply:
        """Stream a Qoder CN chat run and surface reasoning/content to the GUI."""
        del reasoning_effort  # Qoder CN API has no reasoning_effort parameter.

        if cancel_token is not None and cancel_token.is_set():
            raise CancelledError("Request cancelled before API call")

        api_model = resolve_qoder_cn_api_model(self._settings.model)
        ws_url = self._settings.base_url or _QODER_WS_URL
        token = self._settings.api_key or ""

        if not token:
            raise RuntimeError(
                "Qoder CN machine_token 为空。请在「AI 模型」设置中填 openclaw_qc "
                "并保存，或启动 Qoder CN 登录后重试。"
            )
        if not is_qoder_cn_sidecar_running():
            raise RuntimeError(
                "Qoder CN sidecar 未运行（端口 36510 不通）。"
                "请启动 Qoder CN 应用程序后重试。"
            )

        question_text = _messages_to_question_text(messages)
        if not question_text.strip():
            raise RuntimeError("Qoder CN 路由：消息内容为空，无法发送。")

        request_id = str(uuid.uuid4())
        chat_params = _build_chat_ask_params(
            question_text=question_text,
            model_key=api_model,
            request_id=request_id,
        )

        self._log.info(
            "QoderClient.stream_chat: model=%s api_model=%s msgs=%d qtext_len=%d",
            self._settings.model,
            api_model,
            len(messages),
            len(question_text),
        )

        t0 = time.monotonic()
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        finish_reason = ""
        finish_status = 0

        try:
            import websocket  # type: ignore[import]
        except ImportError as exc:
            raise RuntimeError(
                "websocket-client 未安装；Qoder CN 路由需要它。"
                "请运行 pip install websocket-client"
            ) from exc

        try:
            ws = websocket.create_connection(
                ws_url,
                header={"Cosy-MachineToken": token},
                suppress_origin=True,
                timeout=15,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Qoder CN WebSocket 连接失败 ({ws_url}): {exc}"
            ) from exc

        try:
            # Step 1: initialize
            _send_lsp(ws, "initialize", {}, id_="init-qc")
            # Drain init response
            ws.settimeout(5)
            try:
                _ = ws.recv()
            except Exception:
                pass

            # Step 2: send chat/ask
            _send_lsp(ws, "chat/ask", chat_params, id_="chat-qc")

            # Step 3: listen for streaming notifications
            parser = _ThinkBlockParser()
            ws.settimeout(_RECV_TIMEOUT_S)
            deadline = time.monotonic() + timeout_s
            got_finish = False

            while time.monotonic() < deadline:
                if cancel_token is not None and cancel_token.is_set():
                    raise CancelledError("Request cancelled during Qoder CN stream")

                try:
                    raw = ws.recv()
                except Exception as exc:
                    # websocket timeout is expected; other errors are real.
                    import websocket as _ws_mod

                    if isinstance(exc, _ws_mod.WebSocketTimeoutException):
                        continue
                    raise

                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")

                msgs = _parse_lsp_messages(raw)
                for m in msgs:
                    method = m.get("method", "")
                    params = m.get("params", {})

                    if method == "chat/answer":
                        text = str(params.get("text", ""))
                        if not text:
                            continue
                        parser.feed(
                            text,
                            on_content=lambda s: (
                                content_parts.append(s),
                                on_content_token(s) if on_content_token else None,
                            ),
                            on_reasoning=lambda s: (
                                reasoning_parts.append(s),
                                on_reasoning_token(s) if on_reasoning_token else None,
                            ),
                        )

                    elif method == "context/usage/sync":
                        used = params.get("usedTokens", 0)
                        limit = params.get("limitTokens", 0)
                        if used:
                            total_tokens = int(used)
                        if limit:
                            prompt_tokens = max(0, int(used) - completion_tokens)

                    elif method == "chat/finish":
                        finish_reason = str(params.get("reason", ""))
                        finish_status = int(params.get("statusCode", 0))
                        got_finish = True
                        break

                    elif method in ("error", "fatal_error"):
                        err_msg = (
                            params.get("message")
                            or params.get("error")
                            or json.dumps(params, ensure_ascii=False)
                        )
                        raise RuntimeError(f"Qoder CN 流式响应错误: {err_msg}")

                if got_finish:
                    break

            # Flush any remaining buffered text
            parser.flush(
                on_content=lambda s: (
                    content_parts.append(s),
                    on_content_token(s) if on_content_token else None,
                ),
                on_reasoning=lambda s: (
                    reasoning_parts.append(s),
                    on_reasoning_token(s) if on_reasoning_token else None,
                ),
            )

        except CancelledError:
            raise
        except Exception as exc:
            self._log.error("QoderClient stream error: %s", exc)
            raise
        finally:
            try:
                ws.close()
            except Exception:
                pass

        latency_ms = (time.monotonic() - t0) * 1000
        raw_content = "".join(content_parts).strip()
        reasoning_content = "".join(reasoning_parts).strip()

        # Qoder CN's agent system prompt causes the model to output analysis
        # text before the actual JSON answer. Extract the JSON portion and
        # move any preceding analysis to reasoning_content so PA Agent's
        # validation logic sees clean JSON.
        content, extracted_reasoning = _extract_json_content(raw_content)
        if extracted_reasoning:
            reasoning_content = (
                reasoning_content + "\n" + extracted_reasoning
            ).strip() if reasoning_content else extracted_reasoning

        # Estimate completion tokens from content length if not reported.
        if not completion_tokens and content:
            completion_tokens = len(content) // 4
        if not total_tokens:
            total_tokens = prompt_tokens + completion_tokens

        usage = AIUsage(
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=0,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        raw_dict: dict[str, Any] = {
            "id": request_id,
            "model": api_model,
            "content": content,
            "reasoning_content": reasoning_content,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
            "latency_ms": latency_ms,
            "finish_reason": finish_reason,
            "status_code": finish_status,
        }

        self._log.info(
            "QoderClient.stream_chat done: latency=%.0f ms "
            "reasoning_chars=%d content_chars=%d tokens=%d finish=%s",
            latency_ms,
            len(reasoning_content),
            len(content),
            usage.total_tokens,
            finish_reason,
        )

        if not content:
            self._log.warning(
                "Qoder CN 返回空 content (model=%s). 请检查 Qoder CN 是否已登录 "
                "或 sidecar 是否正常运行。",
                api_model,
            )

        return AIReply(
            content=content,
            reasoning_content=reasoning_content,
            raw=raw_dict,
            usage=usage,
            request_id=request_id,
            latency_ms=latency_ms,
        )
