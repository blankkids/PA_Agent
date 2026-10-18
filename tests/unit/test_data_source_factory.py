"""Tests for data source factory and settings."""
from __future__ import annotations

from pa_agent.config.settings import GeneralSettings
from pa_agent.data.factory import (
    DATA_SOURCE_CHOICES,
    create_data_source,
    default_symbol_for_kind,
    default_tradingview_exchange,
    normalize_data_source_kind,
)
from pa_agent.data.binance_source import BinanceSource
from pa_agent.data.eastmoney_source import EastMoneySource
from pa_agent.data.mt5 import MT5Source
from pa_agent.data.tushare_source import TushareSource
from pa_agent.data.tradingview import TradingViewSource


def test_normalize_data_source_kind_defaults_unknown():
    assert normalize_data_source_kind("invalid") == "mt5"
    assert normalize_data_source_kind(None) == "mt5"


def test_normalize_data_source_kind_hidden_sources():
    assert normalize_data_source_kind("akshare") == "akshare"
    assert normalize_data_source_kind("eastmoney") == "eastmoney"
    assert normalize_data_source_kind("tushare") == "tushare"
    assert normalize_data_source_kind("yfinance") == "yfinance"
    assert normalize_data_source_kind("binance") == "binance"


def test_mt5_in_ui_choices():
    """MT5 为默认数据源, 必须在 UI 可选列表中且排首位。"""
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "mt5" in ui_kinds
    assert DATA_SOURCE_CHOICES[0][0] == "mt5"
    # eastmoney / AkShare 仍是隐藏源
    assert "eastmoney" not in ui_kinds
    assert "akshare" not in ui_kinds


def test_tushare_not_in_ui_choices():
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "tushare" not in ui_kinds


def test_binance_in_ui_choices():
    """Binance 在 UI 可选列表中（WebSocket 实时行情）."""
    ui_kinds = {k for k, _ in DATA_SOURCE_CHOICES}
    assert "binance" in ui_kinds


def test_create_data_source_returns_expected_types():
    assert isinstance(create_data_source("mt5"), MT5Source)
    assert isinstance(create_data_source("tradingview"), TradingViewSource)
    assert isinstance(create_data_source("eastmoney"), EastMoneySource)
    assert isinstance(create_data_source("tushare"), TushareSource)
    assert isinstance(create_data_source("binance"), BinanceSource)


def test_default_symbols_per_kind():
    assert default_symbol_for_kind("mt5") == "XAUUSDm"
    assert default_symbol_for_kind("tradingview") == "XAUUSD"
    assert default_symbol_for_kind("eastmoney") == "000001"
    assert default_symbol_for_kind("tushare") == "000001"
    assert default_symbol_for_kind("binance") == "BTCUSDT"


def test_default_tradingview_exchange_is_auto():
    assert default_tradingview_exchange() == ""


def test_general_settings_last_data_source_default():
    g = GeneralSettings()
    assert g.last_data_source == "mt5"
