"""TradeEntryWorker 单测：入场前残留挂单清理（无网络）."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pa_agent.gui.trade_worker import TradeEntryWorker


def _plan(**kw):
    base = dict(
        market="futures", symbol="BTCUSDT", direction="做多", side="BUY",
        order_kind="market", stake_usdt=100.0, leverage=5, quantity=0.005,
        entry_price=100000.0, stop_loss=95000.0,
        take_profit_1=105000.0, take_profit_2=None, tp_split=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class _FakeTrader:
    market = "futures"
    testnet = True

    def __init__(self, *, cancel_fails: bool = False) -> None:
        self.calls: list[str] = []
        self._cancel_fails = cancel_fails

    def cancel_all(self, symbol: str) -> int:
        self.calls.append("cancel_all")
        if self._cancel_fails:
            raise RuntimeError("network down")
        return 2

    def verify_clock(self) -> float:
        self.calls.append("verify_clock")
        return 0.0

    def place_entry(self, plan) -> object:
        self.calls.append("place_entry")
        return SimpleNamespace(
            order_id=1, executed_qty=0.005, avg_price=100000.0,
            status="FILLED",
        )

    def place_protection(self, plan, filled_qty: float) -> list:
        self.calls.append("place_protection")
        return [SimpleNamespace(order_id=2)]


@patch("pa_agent.gui.trade_worker.append_execution_event", MagicMock())
def test_entry_worker_cancels_stale_orders_before_entry():
    """入场前必须先撤销该交易对旧挂单（止损触发后的残留保护单）."""
    trader = _FakeTrader()
    w = TradeEntryWorker(trader, None, _plan())
    w.run()
    assert trader.calls.index("cancel_all") < trader.calls.index("place_entry")


@patch("pa_agent.gui.trade_worker.append_execution_event", MagicMock())
def test_entry_worker_cancel_failure_does_not_block_entry():
    """撤残留失败不阻断入场（place_entry 仍执行）。"""
    trader = _FakeTrader(cancel_fails=True)
    w = TradeEntryWorker(trader, None, _plan())
    w.run()
    assert "place_entry" in trader.calls
