"""Tests for AI client factory routing."""
from __future__ import annotations

from pa_agent.ai.client_factory import create_ai_client
from pa_agent.ai.cursor_sdk_client import CursorSdkClient
from pa_agent.ai.deepseek_client import DeepSeekClient
from pa_agent.ai.qoder_client import QoderClient
from pa_agent.ai.trae_client import TraeClient
from pa_agent.config.settings import AIProviderSettings


def test_create_ai_client_openclaw_cs_uses_cursor_sdk() -> None:
    settings = AIProviderSettings(
        model="openclaw_cs",
        base_url="",
        api_key="crsr_test",
    )
    client = create_ai_client(settings)
    assert isinstance(client, CursorSdkClient)


def test_create_ai_client_openclaw_uses_deepseek_client() -> None:
    settings = AIProviderSettings(
        model="openclaw",
        base_url="http://127.0.0.1:19000/v1",
        api_key="test",
    )
    client = create_ai_client(settings)
    assert isinstance(client, DeepSeekClient)


def test_create_ai_client_openclaw_twc_uses_trae_client() -> None:
    settings = AIProviderSettings(
        model="openclaw_twc",
        base_url="https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat",
        api_key="eyJ.test.token",
    )
    client = create_ai_client(settings)
    assert isinstance(client, TraeClient)


def test_create_ai_client_openclaw_twc_with_model_suffix_uses_trae_client() -> None:
    settings = AIProviderSettings(
        model="openclaw_twc/glm-5.1",
        base_url="https://trae-api-cn.mchost.guru/api/agent/v3/llm_utils_chat",
        api_key="eyJ.test.token",
    )
    client = create_ai_client(settings)
    assert isinstance(client, TraeClient)


def test_create_ai_client_openclaw_qc_uses_qoder_client() -> None:
    settings = AIProviderSettings(
        model="openclaw_qc",
        base_url="ws://127.0.0.1:36510/ws",
        api_key="test_machine_token",
    )
    client = create_ai_client(settings)
    assert isinstance(client, QoderClient)


def test_create_ai_client_openclaw_qc_with_model_suffix_uses_qoder_client() -> None:
    settings = AIProviderSettings(
        model="openclaw_qc/qmodel_38max",
        base_url="ws://127.0.0.1:36510/ws",
        api_key="test_machine_token",
    )
    client = create_ai_client(settings)
    assert isinstance(client, QoderClient)
