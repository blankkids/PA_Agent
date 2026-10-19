"""下单执行 QThread：入场 → （市价）自动保护 / （限价）登记成交监听."""
from __future__ import annotations

import logging

from PyQt6.QtCore import QThread, pyqtSignal

from pa_agent.trade.binance_trader import BinanceTrader
from pa_agent.trade.errors import TradeError
from pa_agent.trade.executions import append_execution_event
from pa_agent.trade.fill_watcher import FillWatcher
from pa_agent.trade.order_plan import OrderPlan

logger = logging.getLogger(__name__)


class TradeEntryWorker(QThread):
    """执行一笔入场（含市价后的保护单）。绝不在主线程跑网络请求."""

    #: 成功：摘要文本（含订单号/成交）
    succeeded = pyqtSignal(str)
    #: 失败：友好错误
    failed = pyqtSignal(str)
    #: 限价单已挂出并进入成交监听
    limit_placed = pyqtSignal(int, str)

    def __init__(
        self,
        trader: BinanceTrader,
        watcher: FillWatcher | None,
        plan: OrderPlan,
        parent=None,
        *,
        auto: bool = False,
    ) -> None:
        super().__init__(parent)
        self._trader = trader
        self._watcher = watcher
        self._plan = plan
        self._auto = auto

    def run(self) -> None:
        p = self._plan
        env = "testnet" if self._trader.testnet else "live"
        # 开新仓前撤销该交易对全部旧挂单：上一笔交易若由止损/止盈触发平仓，
        # 币安的 CONDITIONAL 条件单相互独立、不会联动取消，残留保护单
        # 会在旧触发价守着，可能误平新仓。撤单失败不阻断入场（无残留时
        # 撤单接口亦正常返回）。
        try:
            self._trader.cancel_all(p.symbol)
            append_execution_event({
                "action": "cleanup_before_entry", "market": p.market, "env": env,
                "symbol": p.symbol,
            })
        except Exception as exc:  # noqa: BLE001
            logger.warning("入场前清理残留挂单失败（继续入场）: %s", exc)
        try:
            self._trader.verify_clock()
            res = self._trader.place_entry(p)
        except TradeError as exc:
            append_execution_event({
                "action": "entry", "market": p.market, "env": env,
                "symbol": p.symbol, "direction": p.direction,
                "order_kind": p.order_kind, "error": exc.friendly_message,
                "binance_code": exc.binance_code, "auto": self._auto,
            })
            self.failed.emit(exc.friendly_message)
            return

        append_execution_event({
            "action": "entry", "market": p.market, "env": env,
            "symbol": p.symbol, "direction": p.direction,
            "order_kind": p.order_kind, "order_id": res.order_id,
            "qty": res.executed_qty, "avg_price": res.avg_price,
            "status": res.status, "entry": p.entry_price,
            "sl": p.stop_loss, "tp1": p.take_profit_1, "tp2": p.take_profit_2,
            "auto": self._auto,
        })

        if p.order_kind == "market":
            self._place_protection(p, res.executed_qty, env)
            self.succeeded.emit(
                f"市价入场成交：{res.executed_qty:.8g} @ {res.avg_price:.6g}"
                f"（订单 {res.order_id}），保护单已提交"
            )
            return

        # 限价：登记成交监听，成交后由回调挂保护单
        if self._watcher is not None:
            self._watcher.watch(p.symbol, res.order_id, p)
            self.limit_placed.emit(
                res.order_id,
                f"限价单已挂出（订单 {res.order_id}），成交后将自动挂止盈/止损",
            )
        else:
            self.succeeded.emit(
                f"限价单已挂出（订单 {res.order_id}）；成交后请在交易面板手动补挂保护单"
            )

    def _place_protection(self, p: OrderPlan, filled_qty: float, env: str) -> None:
        try:
            results = self._trader.place_protection(p, filled_qty)
        except TradeError as exc:
            append_execution_event({
                "action": "protection", "market": p.market, "env": env,
                "symbol": p.symbol, "error": exc.friendly_message,
                "binance_code": exc.binance_code,
            })
            # 入场已成交，保护失败必须醒目提示（裸露持仓）
            self.failed.emit(
                f"⚠ 入场已成交，但保护单提交失败：{exc.friendly_message}\n"
                f"持仓处于无保护状态，请立即在交易面板手动补挂或平仓！"
            )
            return
        ids = ", ".join(str(r.order_id) for r in results if r.order_id)
        append_execution_event({
            "action": "protection", "market": p.market, "env": env,
            "symbol": p.symbol, "order_ids": ids,
            "count": len(results), "qty": filled_qty,
        })


def place_protection_for_fill(
    trader: BinanceTrader, plan: OrderPlan, filled_qty: float
) -> tuple[bool, str]:
    """FillWatcher 回调线程内执行：挂保护单并留痕；返回 (成功, 摘要)."""
    env = "testnet" if trader.testnet else "live"
    try:
        results = trader.place_protection(plan, filled_qty)
        ids = ", ".join(str(r.order_id) for r in results if r.order_id)
        append_execution_event({
            "action": "protection_after_fill", "market": plan.market, "env": env,
            "symbol": plan.symbol, "order_ids": ids, "count": len(results),
            "qty": filled_qty,
        })
        return True, f"{plan.symbol} 限价成交（{filled_qty:.8g}），保护单已自动挂出（{ids}）"
    except TradeError as exc:
        append_execution_event({
            "action": "protection_after_fill", "market": plan.market, "env": env,
            "symbol": plan.symbol, "error": exc.friendly_message,
            "binance_code": exc.binance_code,
        })
        return False, (
            f"⚠ {plan.symbol} 限价成交但保护单挂单失败：{exc.friendly_message}\n"
            f"持仓无保护，请立即手动处理！"
        )
