"""Unit tests for BinanceSource (no network; REST/WS layers faked)."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from pa_agent.data.base import DataSourceTransientError
from pa_agent.data.binance_source import (
    BinanceSource,
    normalize_binance_symbol,
    parse_proxy_url,
)
from pa_agent.data.market_defaults import normalize_gold_symbol_for_kind


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_normalize_binance_symbol():
    assert normalize_binance_symbol("BTCUSDT") == "BTCUSDT"
    assert normalize_binance_symbol(" btcusdt ") == "BTCUSDT"
    assert normalize_binance_symbol("BTC/USDT") == "BTCUSDT"
    assert normalize_binance_symbol("BTC USDT") == "BTCUSDT"
    assert normalize_binance_symbol("btc-usdt") == "BTCUSDT"
    assert normalize_binance_symbol("1000SHIBUSDT") == "1000SHIBUSDT"
    assert normalize_binance_symbol("") == ""
    assert normalize_binance_symbol("BT") == ""
    assert normalize_binance_symbol("XX!!!") == ""


def test_parse_proxy_url():
    assert parse_proxy_url("http://127.0.0.1:7890") == {
        "host": "127.0.0.1", "port": 7890, "protocol": "http",
    }
    assert parse_proxy_url("127.0.0.1:7890")["protocol"] == "http"
    assert parse_proxy_url("https://proxy.local:8080")["protocol"] == "https"
    assert parse_proxy_url("") is None
    assert parse_proxy_url("socks5://127.0.0.1:1080") is None
    assert parse_proxy_url("not a proxy") is None


def test_normalize_gold_symbol_for_kind_binance():
    assert normalize_gold_symbol_for_kind("binance", "ETHUSDT") == "ETHUSDT"
    assert normalize_gold_symbol_for_kind("binance", "BTC/USDT") == "BTCUSDT"
    # 非加密货币名（如 MT5 黄金）切到默认币对
    assert normalize_gold_symbol_for_kind("binance", "XAUUSDm") == "BTCUSDT"
    assert normalize_gold_symbol_for_kind("binance", "") == "BTCUSDT"


# ── Fake source ───────────────────────────────────────────────────────────────

def _row(t_ms: int, o: float, h: float, low: float, c: float, v: float, q: float):
    return [t_ms, str(o), str(h), str(low), str(c), str(v), t_ms + 3_599_999, str(q), 42, "0", "0", "0"]


_ROWS = [
    _row(1_700_000_000_000, 100.0, 110.0, 95.0, 105.0, 10.0, 1000.0),
    _row(1_700_003_600_000, 105.0, 120.0, 104.0, 118.0, 11.0, 1200.0),
    _row(1_700_007_200_000, 118.0, 125.0, 117.0, 121.0, 12.0, 1400.0),  # forming
]


class _FakeBinance(BinanceSource):
    def __init__(self, rows=None):
        super().__init__(
            settings=SimpleNamespace(binance=SimpleNamespace(proxy="http://127.0.0.1:7890"))
        )
        self.rows = rows if rows is not None else list(_ROWS)
        self.ws_starts = 0

    def _rest_fetch_rows(self, symbol, interval, limit):
        return self.rows[-min(limit, len(self.rows)):]

    def _start_ws_stream(self, *, wait_ready: bool = True):
        self.ws_starts += 1
        self._ws_ready.set()


def _connected_source() -> _FakeBinance:
    src = _FakeBinance()
    src.connect()
    src.subscribe("BTC/USDT", "1h")
    return src


# ── Subscription validation ───────────────────────────────────────────────────

def test_subscribe_rejects_bad_timeframe():
    src = _FakeBinance()
    src.connect()
    with pytest.raises(ValueError):
        src.subscribe("BTCUSDT", "45m")


def test_subscribe_rejects_bad_symbol():
    src = _FakeBinance()
    src.connect()
    with pytest.raises(ValueError):
        src.subscribe("???", "1h")


def test_connect_requires_sdk(monkeypatch):
    import builtins

    src = _FakeBinance()
    real_import = builtins.__import__

    def _no_binance(name, *args, **kwargs):
        if name.startswith("binance_sdk_spot") or name.startswith("binance_common"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_binance)
    with pytest.raises(DataSourceTransientError):
        src.connect()


# ── Snapshot semantics ────────────────────────────────────────────────────────

def test_snapshot_newest_first_and_forming_flag():
    src = _connected_source()
    bars = src.latest_snapshot(3)
    assert len(bars) == 3
    # newest first
    assert bars[0].ts_open == 1_700_007_200_000
    assert bars[1].ts_open == 1_700_003_600_000
    assert bars[2].ts_open == 1_700_000_000_000
    # REST 最后一行为 forming
    assert bars[0].closed is False
    assert bars[1].closed is True
    assert bars[2].closed is True
    # seq: 1 = newest
    assert [b.seq for b in bars] == [1, 2, 3]
    # amount ← quote asset volume (row[7])
    assert bars[0].amount == pytest.approx(1400.0)
    assert bars[0].close == pytest.approx(121.0)


def test_ws_dict_message_updates_newest_bar():
    src = _connected_source()
    forming_ts = 1_700_010_800_000
    src._on_ws_message(
        {
            "e": "kline",
            "E": forming_ts + 1000,
            "s": "BTCUSDT",
            "k": {
                "t": forming_ts, "T": forming_ts + 3_599_999,
                "s": "BTCUSDT", "i": "1h",
                "o": "121.0", "c": "130.5", "h": "131.0", "l": "120.5",
                "v": "13.5", "q": "1700.0", "x": False,
            },
        }
    )
    bars = src.latest_snapshot(2)
    assert bars[0].ts_open == forming_ts
    assert bars[0].close == pytest.approx(130.5)
    assert bars[0].closed is False
    assert bars[1].ts_open == 1_700_007_200_000


def test_ws_close_flag_marks_bar_closed():
    src = _connected_source()
    ts = 1_700_007_200_000
    src._on_ws_message(
        {"k": {"t": ts, "o": "118.0", "c": "122.0", "h": "123.0", "l": "117.0",
               "v": "12.5", "q": "1500.0", "x": True}}
    )
    bars = src.latest_snapshot(1)
    assert bars[0].ts_open == ts
    assert bars[0].closed is True


def test_ws_object_payload_supported():
    src = _connected_source()
    ts = 1_700_011_600_000
    k = SimpleNamespace(
        t=ts, T=ts + 3_599_999, s="BTCUSDT", i="1h",
        o="122.0", c="126.0", h="127.0", l="121.0",
        v="9.0", q="1130.0", x=False,
    )
    src._on_ws_message(SimpleNamespace(e="kline", E=ts + 500, s="BTCUSDT", k=k))
    bars = src.latest_snapshot(1)
    assert bars[0].ts_open == ts
    assert bars[0].high == pytest.approx(127.0)


def test_rest_rows_do_not_overwrite_ws_bar():
    src = _connected_source()
    ts = 1_700_007_200_000  # 与 REST 最后一根相同的 ts_open
    src._on_ws_message(
        {"k": {"t": ts, "o": "118.0", "c": "999.0", "h": "999.0", "l": "117.0",
               "v": "12.5", "q": "1500.0", "x": False}}
    )
    src._last_ws_msg_mono = time.monotonic()
    src._store_rows(list(_ROWS))  # REST 刷新
    bars = src.latest_snapshot(1)
    assert bars[0].close == pytest.approx(999.0)  # WS 值保留


# ── Proxy resolution ──────────────────────────────────────────────────────────

def test_connect_uses_settings_proxy():
    src = _FakeBinance()
    src.connect()
    assert src._proxy == {"host": "127.0.0.1", "port": 7890, "protocol": "http"}
    assert src._proxy_source == "settings"


def test_env_proxy_overrides_settings(monkeypatch):
    monkeypatch.setenv("BINANCE_PROXY", "http://127.0.0.1:7897")
    src = _FakeBinance()
    src.connect()
    assert src._proxy["port"] == 7897
    assert src._proxy_source == "env"


def test_socks_proxy_rejected():
    src = _FakeBinance()
    src._settings = SimpleNamespace(binance=SimpleNamespace(proxy="socks5://127.0.0.1:1080"))
    with pytest.raises(DataSourceTransientError):
        src.connect()


def test_disconnect_cleans_state():
    src = _connected_source()
    src.disconnect()
    assert src._connected is False
    assert src._symbol == ""
    assert src._bars == {}


# ── Degradation & recovery ────────────────────────────────────────────────────

def test_snapshot_raises_when_rest_returns_nothing():
    src = _FakeBinance(rows=[])
    src.connect()
    src._symbol = "BTCUSDT"
    src._timeframe = "1h"
    with pytest.raises(DataSourceTransientError):
        src.latest_snapshot(10)


def test_ui_preferred_timeframes_all_supported():
    """GUI 周期下拉 preferred 列表必须全部可用（否则下拉为空）."""
    preferred = {"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}
    src = _FakeBinance()
    assert preferred.issubset(set(src.supported_timeframes()))


def test_ws_stale_triggers_rest_refresh_and_lowfreq_restart():
    src = _connected_source()
    # WS 从未推送（_last_ws_msg_mono=0）→ 每次快照都会 REST 兜底并尝试重启 WS
    calls = []
    real_rest = src._rest_fetch_rows

    def counting_rest(symbol, interval, limit):
        calls.append(1)
        return real_rest(symbol, interval, limit)

    src._rest_fetch_rows = counting_rest
    src._ws_task = None
    src._last_ws_attempt_mono = time.monotonic() - 61  # 超过重启间隔
    src.latest_snapshot(3)
    assert calls, "WS 停滞时应触发 REST 兜底"
    assert src.ws_starts == 2  # subscribe 1 次 + 自动重启 1 次

    # 60s 内不重复重启
    src2 = _connected_source()
    src2._ws_task = None
    src2._last_ws_attempt_mono = time.monotonic()  # 刚试过
    before = src2.ws_starts
    src2.latest_snapshot(3)
    assert src2.ws_starts == before


def test_trim_cache_keeps_newest_1500():
    src = _FakeBinance(rows=[])
    src.connect()
    src._symbol = "BTCUSDT"
    src._timeframe = "1h"
    base = 1_700_000_000_000
    with src._bars_lock:
        for i in range(1600):
            src._bars[base + i * 3_600_000] = {
                "ts_open": base + i * 3_600_000,
                "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
                "volume": 1.0, "amount": 1.0, "closed": True,
            }
        src._trim_cache_locked()
    assert len(src._bars) == 1500
    assert max(src._bars) == base + 1599 * 3_600_000
    assert min(src._bars) == base + 100 * 3_600_000
