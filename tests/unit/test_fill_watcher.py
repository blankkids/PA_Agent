"""FillWatcher 单测（fake trader，无网络）."""
from __future__ import annotations

import time

from pa_agent.trade import order_plan as op
from pa_agent.trade.fill_watcher import FillWatcher, scan_unprotected


class _FakeTrader:
    market = "futures"
    testnet = True

    def __init__(self):
        self.queries: list[int] = []
        self._status = "NEW"

    def query_order(self, symbol, order_id):
        self.queries.append(order_id)
        return {"order_id": order_id, "status": self._status,
                "executed_qty": 0.5, "avg_price": 101.0}

    def positions(self, symbol):
        return [{"symbol": symbol}]

    def balances(self, market=None):
        return [{"asset": "BTC", "free": 1.0, "locked": 0.0}]

    def open_orders(self, symbol):
        return []


def _plan():
    return op.build_order_plan(
        {"order_type": "限价单", "order_direction": "做多",
         "entry_price": 100.0, "stop_loss_price": 95.0,
         "take_profit_price": 105.0, "take_profit_price_2": None},
        "BTCUSDT", market="futures", stake_usdt=100.0, leverage=5,
        filters=op.SymbolFilters("BTCUSDT", 0.01, 0.001, 0.001, 5.0),
    )


def test_watcher_triggers_filled_callback():
    trader = _FakeTrader()
    watcher = FillWatcher(trader)
    filled: list[tuple[int, float]] = []
    watcher.set_callbacks(
        lambda oid, plan, qty: filled.append((oid, qty)),
        lambda oid, err: None,
    )
    trader._status = "FILLED"
    watcher.watch("BTCUSDT", 42, _plan())
    deadline = time.monotonic() + 10
    while not filled and time.monotonic() < deadline:
        time.sleep(0.1)
    watcher.stop()
    assert filled and filled[0][0] == 42
    assert filled[0][1] == 0.5
    assert 42 not in watcher.watching_ids()  # 成交后自动移除


def test_watcher_keeps_pending_order():
    trader = _FakeTrader()
    watcher = FillWatcher(trader)
    trader._status = "NEW"
    watcher.watch("BTCUSDT", 7, _plan())
    time.sleep(2.0)  # 至少一轮轮询
    watcher.stop()
    assert 7 in watcher.watching_ids()
    assert trader.queries  # 确实轮询过


def test_scan_unprotected_flags_missing_protection():
    trader = _FakeTrader()  # 有持仓、无挂单
    out = scan_unprotected(trader, "BTCUSDT")
    assert out and out[0]["has_position"] is True
    assert out[0]["has_protection"] is False
