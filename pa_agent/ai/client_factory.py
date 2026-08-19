"""Construct the correct AI client for the configured provider route."""

from __future__ import annotations

import logging
from typing import Any

from pa_agent.ai.cursor_connector import is_openclaw_cs_model
from pa_agent.ai.qoder_connector import is_openclaw_qc_model
from pa_agent.ai.trae_connector import is_openclaw_twc_model
from pa_agent.config.settings import AIProviderSettings


def create_ai_client(
    settings: AIProviderSettings,
    logger_: logging.Logger | None = None,
) -> Any:
    """Return CursorSdkClient for ``openclaw_cs*``, TraeClient for
    ``openclaw_twc*``, QoderClient for ``openclaw_qc*``, else DeepSeekClient."""
    log = logger_ or logging.getLogger(__name__)
    if is_openclaw_cs_model(settings.model):
        from pa_agent.ai.cursor_sdk_client import CursorSdkClient

        log.info("AI client route: Cursor SDK (model=%s)", settings.model)
        return CursorSdkClient(settings=settings, logger_=log)

    if is_openclaw_twc_model(settings.model):
        from pa_agent.ai.trae_client import TraeClient

        log.info("AI client route: TRAE Work CN (model=%s)", settings.model)
        return TraeClient(settings=settings, logger_=log)

    if is_openclaw_qc_model(settings.model):
        from pa_agent.ai.qoder_client import QoderClient

        log.info("AI client route: Qoder CN (model=%s)", settings.model)
        return QoderClient(settings=settings, logger_=log)

    from pa_agent.ai.deepseek_client import DeepSeekClient

    log.info(
        "AI client route: OpenAI-compatible (model=%s base_url=%s)",
        settings.model,
        settings.base_url or "(empty)",
    )
    return DeepSeekClient(settings=settings, logger_=log)
