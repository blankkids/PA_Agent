"""TRAE Work CN API client for PA Agent.

Implements the same interface as :class:`pa_agent.ai.deepseek_client.DeepSeekClient`
(``stream_chat`` and ``update_provider``) but talks to TRAE Work CN's
non-OpenAI endpoint ``/api/ide/v1/llm_raw_chat`` (Server-Sent Events).

The TRAE API uses a custom request payload (``messages`` / ``model_name``)
and a custom SSE event schema:

  * ``metadata`` — session metadata (session_id, model, etc.)
  * ``output`` — content / reasoning_content deltas. Payload fields:
      - ``response``: content delta (string, may be empty)
      - ``reasoning_content``: thinking delta (string, may be null)
      - ``tool_calls`` / ``multimodal_contents`` / ``phase``: unused here
  * ``token_usage`` — usage statistics (prompt_tokens, completion_tokens,
    total_tokens, cache_*)
  * ``error`` / ``fatal_error`` — stream-level error with ``code``/``message``
  * ``done`` — stream termination
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, TYPE_CHECKING

from pa_agent.ai.deepseek_client import AIReply, AIUsage, CancelledError
from pa_agent.ai.trae_connector import (
    _TRAE_API_CHAT_PATH,
    _TRAE_APP_ID,
    _get_trae_cn_info,
    _is_jwt_expired,
    is_openclaw_twc_model,
    resolve_trae_cn_api_model,
)
from pa_agent.config.settings import AIProviderSettings

if TYPE_CHECKING:
    from pa_agent.util.threading import CancelToken

logger = logging.getLogger(__name__)


# Minimum interval between cancellation checks while streaming.
_CANCEL_POLL_INTERVAL_S = 0.2

# Rate-limit retry configuration.
_RATE_LIMIT_MAX_RETRIES = 5
_RATE_LIMIT_BACKOFF_BASE_S = 10.0  # First retry waits 10s, then 20s, 40s, 80s, 160s.


class _TraeRateLimitError(RuntimeError):
    """Raised when TRAE Work CN returns a rate-limit error (429 or SSE error)."""


def _messages_to_trae_payload(
    messages: list[dict[str, Any]],
    *,
    model_name: str,
) -> dict[str, Any]:
    """Convert OpenAI-style messages into TRAE llm_raw_chat request body.

    The llm_raw_chat endpoint accepts an OpenAI-style ``messages`` array
    plus a ``model_name`` field. Each message's ``content`` may be a string
    or a list of {type:"text", text:"..."} blocks; we normalize both forms
    to the block-list form that the TRAE API expects.
    """
    norm_messages: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role", "user"))
        content = msg.get("content", "")
        if isinstance(content, str):
            # Wrap plain strings into the block-list form.
            blocks: list[dict[str, Any]] = [{"type": "text", "text": content}]
        elif isinstance(content, list):
            # Already block-list; keep as-is (filter to text blocks).
            blocks = [
                {"type": "text", "text": str(b.get("text", ""))}
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            if not blocks:
                blocks = [{"type": "text", "text": ""}]
        else:
            blocks = [{"type": "text", "text": str(content)}]
        norm_messages.append({"role": role, "content": blocks})

    return {"messages": norm_messages, "model_name": model_name}


def _build_trae_headers(
    *,
    token: str,
    device_id: str,
    machine_id: str,
) -> dict[str, str]:
    """Build the headers required by TRAE llm_raw_chat (mirrors Trae CN desktop).

    The endpoint accepts two equivalent auth schemes:
      * ``Authorization: Cloud-IDE-JWT <jwt>``  (legacy Cloud-IDE form)
      * ``x-ide-token: <jwt>``                   (newer TRAE form)
    We send both so the call works regardless of which gateway version serves it.
    """
    trace_id = uuid.uuid4().hex
    request_id = str(uuid.uuid4())
    return {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "x-app-id": _TRAE_APP_ID,
        "x-app-version": "default",
        "x-app-version-code": "20260630",
        "x-ide-version": "3.3.76",
        "x-ide-version-code": "20260630",
        "x-ide-version-type": "stable",
        "x-custom-trace-id": trace_id,
        "x-flow-traceparent": f"04-{trace_id}-{uuid.uuid4().hex[:16]}-01",
        "x-device-id": device_id,
        "x-machine-id": machine_id,
        "x-device-brand": "PA-Agent",
        "x-device-cpu": "Intel",
        "x-device-type": "windows",
        "x-os-version": "Windows",
        "request-traffic-type": "prod",
        "x-request-id": request_id,
        "x-trae-request-id": request_id,
        # llm_raw_chat accepts either auth header; send both for compatibility.
        "Authorization": f"Cloud-IDE-JWT {token}",
        "x-ide-token": token,
    }


def _parse_sse_event(raw_block: str) -> tuple[str, str]:
    """Parse one SSE block (separated by blank lines) into (event, data).

    A block looks like::

        event: plan_item
        data: {"content": "..."}

    Or sometimes with multiple ``data:`` lines that should be concatenated.
    Returns ("", "") for empty/invalid blocks.
    """
    event = ""
    data_parts: list[str] = []
    for line in raw_block.splitlines():
        if not line or line.startswith(":"):  # comment / heartbeat
            continue
        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_parts.append(line[len("data:"):].lstrip())
    return event, "\n".join(data_parts)


def _extract_content_fields(data: Any) -> tuple[str, str]:
    """Extract (content_delta, reasoning_delta) from an ``output`` SSE payload.

    llm_raw_chat emits ``output`` events with this shape::

        {"response": "<content delta>",
         "reasoning_content": "<thinking delta>|null",
         "tool_calls": null,
         "multimodal_contents": null,
         "phase": null}

    For backwards compatibility we also accept the older ``plan_item`` shape
    that wraps fields in a ``delta``/``message``/``plan_item``/``data`` sub-dict
    and uses ``content``/``text`` instead of ``response``.
    """
    if data is None:
        return "", ""
    if isinstance(data, str):
        # Bare string → treat as content delta.
        return data, ""

    if not isinstance(data, dict):
        return "", ""

    # Unwrap common nesting shapes (older plan_item format).
    for wrapper in ("delta", "message", "plan_item", "data"):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            data = inner
            break

    # llm_raw_chat uses ``response``; older endpoints used ``content``/``text``.
    content = data.get("response") or data.get("content") or data.get("text") or ""
    reasoning = (
        data.get("reasoning_content")
        or data.get("reasoning")
        or data.get("thinking")
        or ""
    )
    # Make sure both are strings (sometimes JSON-encoded or null).
    if not isinstance(content, str):
        content = str(content) if content else ""
    if not isinstance(reasoning, str):
        reasoning = str(reasoning) if reasoning else ""
    return content, reasoning


def _extract_usage_fields(data: Any) -> dict[str, int]:
    """Extract token usage from a ``token_usage`` SSE event payload.

    Returns a dict with optional keys: prompt_tokens, completion_tokens,
    total_tokens, cached_prompt_tokens.  Missing keys default to 0.
    """
    out: dict[str, int] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_prompt_tokens": 0,
    }
    if not isinstance(data, dict):
        return out

    # Accept both flat and nested "usage" shapes.
    src = data.get("usage") if isinstance(data.get("usage"), dict) else data

    def _read_int(key: str, *aliases: str) -> int:
        for k in (key, *aliases):
            v = src.get(k)
            if v is None:
                continue
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
        return 0

    out["prompt_tokens"] = _read_int("prompt_tokens", "input_tokens", "input")
    out["completion_tokens"] = _read_int(
        "completion_tokens", "output_tokens", "output", "completion"
    )
    out["total_tokens"] = _read_int("total_tokens", "tokens")
    out["cached_prompt_tokens"] = _read_int(
        "cached_prompt_tokens",
        "prompt_cache_hit_tokens",
        "cached_tokens",
        "cache_hit_tokens",
    )
    return out


class TraeClient:
    """Custom streaming client for TRAE Work CN's llm_utils_chat endpoint."""

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
        """Stream a TRAE chat run and surface reasoning/content to the GUI.

        Raises CancelledError if cancel_token is set before or during the call.
        """
        del reasoning_effort  # TRAE API has no reasoning_effort parameter.

        if cancel_token is not None and cancel_token.is_set():
            raise CancelledError("Request cancelled before API call")

        api_model = resolve_trae_cn_api_model(self._settings.model)
        base_url = self._settings.base_url or _TRAE_API_CHAT_PATH
        token = self._settings.api_key or ""

        if not token:
            raise RuntimeError(
                "TRAE Work CN API Token 为空。请在「AI 模型」设置中填 openclaw_twc "
                "并保存，或启动 TRAE Work CN 登录后重试。"
            )
        if _is_jwt_expired(token):
            raise RuntimeError(
                "TRAE Work CN 的 JWT Token 已过期。请启动 TRAE Work CN 并登录，"
                "然后在「AI 模型」设置中重新保存以刷新 Token。"
            )

        device_id, machine_id = self._resolve_device_ids()
        headers = _build_trae_headers(
            token=token, device_id=device_id, machine_id=machine_id
        )
        payload = _messages_to_trae_payload(messages, model_name=api_model)

        self._log.info(
            "TraeClient.stream_chat: model=%s api_model=%s msgs=%d "
            "device=%s",
            self._settings.model,
            api_model,
            len(messages),
            device_id or "?",
        )

        t0 = time.monotonic()
        reasoning_parts: list[str] = []
        content_parts: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        cached_tokens = 0
        request_id = headers.get("x-request-id", "")

        try:
            import httpx  # type: ignore[import]
        except ImportError as exc:  # noqa: BLE001
            raise RuntimeError(
                "httpx 未安装；TRAE Work CN 路由需要 httpx 用于流式响应。"
                "请运行 pip install httpx"
            ) from exc

        try:
            for _attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
                try:
                    with httpx.stream(
                        "POST",
                        base_url,
                        headers=headers,
                        json=payload,
                        timeout=timeout_s,
                    ) as resp:
                        if resp.status_code != 200:
                            body = resp.read().decode("utf-8", errors="replace")[:500]
                            self._log.error(
                                "TraeClient HTTP %s: %s",
                                resp.status_code,
                                body,
                            )
                            if resp.status_code in (401, 403):
                                raise RuntimeError(
                                    f"TRAE Work CN 认证失败 (HTTP {resp.status_code})。"
                                    "请启动 TRAE Work CN 重新登录后，在「AI 模型」设置中"
                                    "重新保存以刷新 Token。"
                                )
                            if resp.status_code == 429 or "rate limit" in body.lower():
                                raise _TraeRateLimitError(
                                    f"TRAE Work CN 速率限制 (HTTP {resp.status_code})"
                                )
                            raise RuntimeError(
                                f"TRAE Work CN API 返回 HTTP {resp.status_code}: {body}"
                            )

                        # Iterate SSE lines as they arrive.
                        sse_buffer = ""
                        for raw_line in resp.iter_lines():
                            if cancel_token is not None and cancel_token.is_set():
                                raise CancelledError("Request cancelled during TRAE stream")

                            if raw_line is None:
                                continue
                            if isinstance(raw_line, bytes):
                                raw_line = raw_line.decode("utf-8", errors="replace")

                            # SSE events are separated by blank lines. httpx iter_lines
                            # strips the trailing newline, so an empty string marks the
                            # separator between events.
                            if raw_line:
                                sse_buffer += raw_line + "\n"
                                continue

                            if not sse_buffer.strip():
                                sse_buffer = ""
                                continue

                            event, data_str = _parse_sse_event(sse_buffer)
                            sse_buffer = ""

                            if not event:
                                continue

                            # Parse data as JSON when possible; fall back to raw string.
                            data: Any = data_str
                            if data_str:
                                try:
                                    data = json.loads(data_str)
                                except json.JSONDecodeError:
                                    # Keep as raw string; _extract_* helpers handle it.
                                    data = data_str

                            if event in ("output", "plan_item"):
                                # llm_raw_chat emits ``output``; older endpoints used
                                # ``plan_item``. Both carry content/reasoning deltas.
                                c_delta, r_delta = _extract_content_fields(data)
                                if r_delta:
                                    reasoning_parts.append(r_delta)
                                    if on_reasoning_token is not None:
                                        on_reasoning_token(r_delta)
                                if c_delta:
                                    content_parts.append(c_delta)
                                    if on_content_token is not None:
                                        on_content_token(c_delta)
                            elif event == "token_usage":
                                u = _extract_usage_fields(data)
                                if u["prompt_tokens"]:
                                    prompt_tokens = u["prompt_tokens"]
                                if u["completion_tokens"]:
                                    completion_tokens = u["completion_tokens"]
                                if u["total_tokens"]:
                                    total_tokens = u["total_tokens"]
                                if u["cached_prompt_tokens"]:
                                    cached_tokens = u["cached_prompt_tokens"]
                            elif event == "done":
                                break
                            elif event in ("error", "fatal_error"):
                                msg = (
                                    data.get("message")
                                    or data.get("error")
                                    or data_str
                                    if isinstance(data, dict)
                                    else data_str
                                )
                                if "rate limit" in str(msg).lower():
                                    raise _TraeRateLimitError(
                                        f"TRAE Work CN 流式响应错误: {msg}"
                                    )
                                raise RuntimeError(
                                    f"TRAE Work CN 流式响应错误: {msg}"
                                )
                            # Other events (notification / queuing / progress_notice /
                            # metadata / context_usage) are informational — ignore.

                        # If total_tokens wasn't reported, compute from parts.
                        if total_tokens == 0 and (prompt_tokens or completion_tokens):
                            total_tokens = prompt_tokens + completion_tokens

                    # Success — break out of retry loop.
                    break

                except _TraeRateLimitError:
                    if _attempt >= _RATE_LIMIT_MAX_RETRIES:
                        raise
                    wait_s = _RATE_LIMIT_BACKOFF_BASE_S * (2 ** _attempt)
                    self._log.warning(
                        "TRAE Work CN 速率限制，%.0f 秒后重试 (第 %d/%d 次)...",
                        wait_s,
                        _attempt + 1,
                        _RATE_LIMIT_MAX_RETRIES,
                    )
                    # Reset accumulated content for retry.
                    reasoning_parts.clear()
                    content_parts.clear()
                    prompt_tokens = 0
                    completion_tokens = 0
                    total_tokens = 0
                    cached_tokens = 0
                    time.sleep(wait_s)
                    continue

        except CancelledError:
            raise
        except httpx.HTTPError as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self._log.error(
                "TraeClient stream HTTP error after %.0f ms: %s", latency_ms, exc
            )
            raise RuntimeError(f"TRAE Work CN 网络错误: {exc}") from exc
        except Exception as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            self._log.error(
                "TraeClient stream error after %.0f ms: %s", latency_ms, exc
            )
            raise

        latency_ms = (time.monotonic() - t0) * 1000
        content = "".join(content_parts)
        reasoning_content = "".join(reasoning_parts)

        usage = AIUsage(
            prompt_tokens=prompt_tokens,
            cached_prompt_tokens=cached_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        )

        raw: dict[str, Any] = {
            "id": request_id,
            "model": api_model,
            "content": content,
            "reasoning_content": reasoning_content,
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "cached_prompt_tokens": usage.cached_prompt_tokens,
                "cache_miss_tokens": usage.cache_miss_tokens,
                "cache_hit_rate_pct": round(usage.cache_hit_rate * 100, 1),
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
            "latency_ms": latency_ms,
        }

        self._log.info(
            "TraeClient.stream_chat done: latency=%.0f ms "
            "reasoning_chars=%d content_chars=%d tokens=%d/%d",
            latency_ms,
            len(reasoning_content),
            len(content),
            usage.prompt_tokens,
            usage.completion_tokens,
        )

        if not content.strip():
            self._log.warning(
                "TRAE Work CN 返回空 content (model=%s). 请检查 Token 是否有效 "
                "或 TRAE Work CN 是否已登录。",
                api_model,
            )

        return AIReply(
            content=content,
            reasoning_content=reasoning_content,
            raw=raw,
            usage=usage,
            request_id=request_id,
            latency_ms=latency_ms,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _resolve_device_ids(self) -> tuple[str, str]:
        """Resolve device_id / machine_id for TRAE headers.

        Uses cached provider values when present (set by trae_connector), else
        re-reads from the TRAE CN local config files.
        """
        device_id = str(getattr(self._settings, "trae_device_id", "") or "")
        machine_id = str(getattr(self._settings, "trae_machine_id", "") or "")
        if device_id and machine_id:
            return device_id, machine_id

        info = _get_trae_cn_info()
        if info is None:
            return device_id or "unknown", machine_id or "unknown"
        _host, _token, device_info = info
        return (
            device_id or device_info.get("device_id") or "unknown",
            machine_id or device_info.get("machine_id") or "unknown",
        )
