"""交易后端单测：错误映射 / 订单计划 / 保护单构造."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.trade.errors import TradeBlockedError, TradeError, friendly_binance_error
from pa_agent.trade.order_plan import (
    FUTURES,
    SPOT,
    SymbolFilters,
    build_order_plan,
    format_by_step,
    round_down_to_step,
)
from pa_agent.trade.protection import build_protection_plan


# ── errors ────────────────────────────────────────────────────────────────────

def test_friendly_error_by_code():
    exc = SimpleNamespace(status_code=-2010, error_message="insufficient balance")
    msg = friendly_binance_error(exc)
    assert "余额" in msg and "-2010" in msg


def test_friendly_error_network():
    class NetworkError(Exception):
        pass

    msg = friendly_binance_error(NetworkError("conn refused"))
    assert "代理" in msg or "网络" in msg


def test_trade_error_from_exception():
    exc = SimpleNamespace(status_code=-1013, error_message="filter failure")
    te = TradeError.from_exception(exc)
    assert te.binance_code == -1013
    assert "精度" in te.friendly_message or "名义" in te.friendly_message


# ── step rounding ─────────────────────────────────────────────────────────────

def test_round_down_to_step():
    assert round_down_to_step(0.57679, 0.001) == pytest.approx(0.576)
    assert round_down_to_step(10.0, 2.0) == pytest.approx(10.0)
    assert format_by_step(1234.56789, 0.01) == pytest.approx(1234.56)


# ── order plan ────────────────────────────────────────────────────────────────

_FILTERS = SymbolFilters(symbol="BTCUSDT", tick_size=0.01, step_size=0.001, min_qty=0.001, min_notional=5.0)

_LONG_MARKET = {
    "order_type": "市价单", "order_direction": "做多",
    "entry_price": None, "stop_loss_price": 95000.0,
    "take_profit_price": 105000.0, "take_profit_price_2": None,
}
_SHORT_LIMIT = {
    "order_type": "限价单", "order_direction": "做空",
    "entry_price": 100000.0, "stop_loss_price": 102000.0,
    "take_profit_price": 96000.0, "take_profit_price_2": 94000.0,
}


def test_spot_market_long_plan():
    plan = build_order_plan(_LONG_MARKET, "BTCUSDT", market=SPOT,
                            stake_usdt=100.0, leverage=1, filters=_FILTERS,
                            last_price=100000.0)
    assert plan.order_kind == "market"
    assert plan.side == "BUY"
    assert plan.leverage == 1
    assert plan.quantity == pytest.approx(0.001)  # 100/100000=0.001
    assert plan.tp_split is False


def test_futures_short_with_leverage_and_tp_split():
    plan = build_order_plan(_SHORT_LIMIT, "BTCUSDT", market=FUTURES,
                            stake_usdt=100.0, leverage=5, filters=_FILTERS)
    assert plan.side == "SELL"
    assert plan.is_short
    assert plan.leverage == 5
    # 名义 500 / 限价 100000 = 0.005
    assert plan.quantity == pytest.approx(0.005)
    assert plan.tp_split is True
    assert plan.take_profit_2 == pytest.approx(94000.0)
    assert plan.entry_price == pytest.approx(100000.0)


def test_spot_short_blocked():
    with pytest.raises(TradeBlockedError, match="合约"):
        build_order_plan(_SHORT_LIMIT, "BTCUSDT", market=SPOT,
                         stake_usdt=100.0, leverage=1, filters=_FILTERS)


def test_no_order_blocked():
    with pytest.raises(TradeBlockedError):
        build_order_plan({"order_type": "不下单", "order_direction": None}, "BTCUSDT",
                         market=SPOT, stake_usdt=100.0, leverage=1, filters=_FILTERS)


def test_min_notional_guard():
    filters = SymbolFilters(symbol="BTCUSDT", tick_size=0.01, step_size=0.00001,
                            min_qty=0.00001, min_notional=5.0)
    with pytest.raises(TradeBlockedError, match="名义"):
        build_order_plan(_LONG_MARKET, "BTCUSDT", market=SPOT,
                         stake_usdt=1.0, leverage=1, filters=filters,
                         last_price=100000.0)


# ── protection ────────────────────────────────────────────────────────────────

def _mk_plan(market: str, direction: str, tp2: float | None):
    return build_order_plan(
        {
            "order_type": "市价单", "order_direction": direction,
            "entry_price": None, "stop_loss_price": 95.0,
            "take_profit_price": 105.0, "take_profit_price_2": tp2,
        },
        "BTCUSDT", market=market, stake_usdt=100.0, leverage=5,
        filters=SymbolFilters(symbol="BTCUSDT", tick_size=0.01, step_size=0.001,
                              min_qty=0.001, min_notional=5.0),
        last_price=100.0,
    )


def test_spot_protection_single_oco_without_tp2():
    p = _mk_plan(SPOT, "做多", None)
    pp = build_protection_plan(p, filled_qty=2.0)
    assert len(pp.orders) == 1
    o = pp.orders[0]
    assert o.kind == "oco" and o.side == "SELL"
    assert o.quantity == pytest.approx(2.0)
    assert o.take_profit_price == pytest.approx(105.0)
    assert o.stop_loss_price == pytest.approx(95.0)


def test_spot_protection_dual_oco_with_tp2():
    p = _mk_plan(SPOT, "做多", 110.0)
    pp = build_protection_plan(p, filled_qty=2.0)
    assert len(pp.orders) == 2
    for i, o in enumerate(pp.orders, start=1):
        assert o.kind == "oco"
        assert o.quantity == pytest.approx(1.0)  # 各半
        assert o.group == i
        assert o.stop_loss_price == pytest.approx(95.0)  # 同 SL 价
    assert pp.orders[0].take_profit_price == pytest.approx(105.0)
    assert pp.orders[1].take_profit_price == pytest.approx(110.0)


def test_futures_long_protection():
    p = _mk_plan(FUTURES, "做多", None)
    pp = build_protection_plan(p, filled_qty=2.0)
    assert len(pp.orders) == 2  # TP 全量 + SL close
    tp, sl = pp.orders
    assert tp.kind == "take_profit" and tp.reduce_only
    assert tp.side == "SELL"  # 平多
    assert tp.quantity == pytest.approx(2.0)
    assert sl.kind == "stop_loss_close" and sl.close_position
    assert sl.stop_loss_price == pytest.approx(95.0)


def test_futures_short_protection_direction_inverted():
    p = _mk_plan(FUTURES, "做空", 90.0)
    pp = build_protection_plan(p, filled_qty=2.0)
    assert len(pp.orders) == 3  # TP1 半 + TP2 半 + SL
    tp1, tp2, sl = pp.orders
    for o in (tp1, tp2, sl):
        assert o.side == "BUY"  # 平空 = BUY
    assert tp1.quantity == pytest.approx(1.0)
    assert tp2.quantity == pytest.approx(1.0)
    assert tp2.take_profit_price == pytest.approx(90.0)
    assert sl.stop_loss_price == pytest.approx(95.0)  # 空头 SL 在上方


def test_protection_invalid_qty():
    p = _mk_plan(SPOT, "做多", None)
    with pytest.raises(ValueError):
        build_protection_plan(p, filled_qty=0.0)
