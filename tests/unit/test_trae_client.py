"""Unit tests for pa_agent.ai.trae_client helper functions."""
from __future__ import annotations

from pa_agent.ai.trae_client import (
    _extract_content_fields,
    _extract_usage_fields,
    _messages_to_trae_payload,
    _parse_sse_event,
)


# ── _messages_to_trae_payload ────────────────────────────────────────────────


def test_messages_to_trae_payload_basic() -> None:
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there"},
        {"role": "user", "content": "What is 2+2?"},
    ]
    payload = _messages_to_trae_payload(messages, model_name="glm-5.2")
    assert payload["model_name"] == "glm-5.2"
    assert len(payload["messages"]) == 4
    assert payload["messages"][0] == {
        "role": "system",
        "content": [{"type": "text", "text": "You are helpful."}],
    }
    assert payload["messages"][-1] == {
        "role": "user",
        "content": [{"type": "text", "text": "What is 2+2?"}],
    }


def test_messages_to_trae_payload_single_message() -> None:
    messages = [{"role": "user", "content": "Hi"}]
    payload = _messages_to_trae_payload(messages, model_name="glm-5.1")
    assert payload["model_name"] == "glm-5.1"
    assert payload["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "Hi"}]}
    ]


def test_messages_to_trae_payload_empty_messages() -> None:
    payload = _messages_to_trae_payload([], model_name="glm-5.2")
    assert payload["messages"] == []
    assert payload["model_name"] == "glm-5.2"


def test_messages_to_trae_payload_vision_style_content() -> None:
    messages = [
        {"role": "user", "content": "First question"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Look at this:"},
                {"type": "text", "text": "What do you see?"},
            ],
        },
    ]
    payload = _messages_to_trae_payload(messages, model_name="glm-5.2")
    assert payload["messages"][0] == {
        "role": "user",
        "content": [{"type": "text", "text": "First question"}],
    }
    assert payload["messages"][1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "Look at this:"},
            {"type": "text", "text": "What do you see?"},
        ],
    }


# ── _parse_sse_event ─────────────────────────────────────────────────────────


def test_parse_sse_event_plan_item() -> None:
    block = 'event: plan_item\ndata: {"content": "hello"}'
    event, data = _parse_sse_event(block)
    assert event == "plan_item"
    assert data == '{"content": "hello"}'


def test_parse_sse_event_done() -> None:
    block = "event: done\ndata: {}"
    event, data = _parse_sse_event(block)
    assert event == "done"
    assert data == "{}"


def test_parse_sse_event_multiline_data() -> None:
    block = 'event: plan_item\ndata: {"content": "line1\\ndata: line2"}'
    event, data = _parse_sse_event(block)
    assert event == "plan_item"
    # The second "data:" prefix is stripped, content concatenated with \n.
    assert "line1" in data
    assert "line2" in data


def test_parse_sse_event_empty_block() -> None:
    event, data = _parse_sse_event("")
    assert event == ""
    assert data == ""


def test_parse_sse_event_comment_line() -> None:
    block = ": heartbeat\nevent: progress_notice\ndata: \"Processing_123\""
    event, data = _parse_sse_event(block)
    assert event == "progress_notice"
    assert data == '"Processing_123"'


# ── _extract_content_fields ──────────────────────────────────────────────────


def test_extract_content_fields_direct() -> None:
    data = {"content": "hello", "reasoning_content": "thinking..."}
    c, r = _extract_content_fields(data)
    assert c == "hello"
    assert r == "thinking..."


def test_extract_content_fields_delta_wrapper() -> None:
    data = {"delta": {"content": "world", "reasoning_content": "why?"}}
    c, r = _extract_content_fields(data)
    assert c == "world"
    assert r == "why?"


def test_extract_content_fields_string_data() -> None:
    c, r = _extract_content_fields("raw text delta")
    assert c == "raw text delta"
    assert r == ""


def test_extract_content_fields_none() -> None:
    c, r = _extract_content_fields(None)
    assert c == ""
    assert r == ""


def test_extract_content_fields_text_alias() -> None:
    data = {"text": "alt content", "thinking": "alt reasoning"}
    c, r = _extract_content_fields(data)
    assert c == "alt content"
    assert r == "alt reasoning"


# ── _extract_usage_fields ────────────────────────────────────────────────────


def test_extract_usage_fields_flat() -> None:
    data = {
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "cached_prompt_tokens": 30,
    }
    u = _extract_usage_fields(data)
    assert u["prompt_tokens"] == 100
    assert u["completion_tokens"] == 50
    assert u["total_tokens"] == 150
    assert u["cached_prompt_tokens"] == 30


def test_extract_usage_fields_nested() -> None:
    data = {
        "usage": {
            "prompt_tokens": 200,
            "completion_tokens": 80,
            "total_tokens": 280,
        }
    }
    u = _extract_usage_fields(data)
    assert u["prompt_tokens"] == 200
    assert u["completion_tokens"] == 80
    assert u["total_tokens"] == 280
    assert u["cached_prompt_tokens"] == 0


def test_extract_usage_fields_aliases() -> None:
    data = {
        "input_tokens": 100,
        "output_tokens": 50,
        "prompt_cache_hit_tokens": 25,
    }
    u = _extract_usage_fields(data)
    assert u["prompt_tokens"] == 100
    assert u["completion_tokens"] == 50
    assert u["cached_prompt_tokens"] == 25


def test_extract_usage_fields_non_dict() -> None:
    u = _extract_usage_fields("not a dict")
    assert u["prompt_tokens"] == 0
    assert u["completion_tokens"] == 0
    assert u["total_tokens"] == 0
    assert u["cached_prompt_tokens"] == 0


def test_extract_usage_fields_none() -> None:
    u = _extract_usage_fields(None)
    assert u["prompt_tokens"] == 0
    assert u["completion_tokens"] == 0
