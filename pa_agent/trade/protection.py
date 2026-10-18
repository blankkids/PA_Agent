"""入场成交后的保护单构造（双市场策略）.

- 现货 · 未设 TP2：一组 OCO（TP1 全量 + SL 全量）
- 现货 · 设 TP2：两组 OCO（TP1 半量+SL 半量 / TP2 半量+SL 半量，同 SL 价；
  任一 TP 成交其同组 SL 腿联动取消，价格打 SL 时剩余量全部了结）
- 合约：TP1/TP2 各半量 reduceOnly 止盈单 + closePosition 型 SL（剩余仓位全平）；
  做空时 SL/TP 方向自动反转（触发价关系互换）
"""
from __future__ import annotations

from dataclasses import dataclass

from pa_agent.trade.order_plan import FUTURES, SPOT, OrderPlan, format_by_step


@dataclass(frozen=True)
class ProtectionOrder:
    """单个保护单的提交参数（trader 按市场翻译为 SDK 调用）."""

    kind: str        # oco | take_profit | stop_loss_close
    side: str        # 平仓方向：现货卖出=SELL；合约平多=SELL，平空=BUY
    quantity: float
    stop_loss_price: float          # SL 触发价（oco/stop_loss_close）
    take_profit_price: float        # TP 限价（oco/take_profit）
    group: int = 0                  # 现货双 OCO 的组号（1=TP1 组，2=TP2 组）
    reduce_only: bool = False       # 合约 reduceOnly
    close_position: bool = False    # 合约 closePosition（全平型，忽略 quantity）


@dataclass(frozen=True)
class ProtectionPlan:
    orders: tuple[ProtectionOrder, ...]
    market: str
    note: str = ""


def build_protection_plan(plan: OrderPlan, filled_qty: float) -> ProtectionPlan:
    """按市场构造入场成交（数量 *filled_qty*）后的保护单列表."""
    if filled_qty <= 0:
        raise ValueError(f"filled_qty 必须为正: {filled_qty}")
    if plan.market == SPOT:
        return _spot_protection(plan, filled_qty)
    return _futures_protection(plan, filled_qty)


def _spot_protection(plan: OrderPlan, qty: float) -> ProtectionPlan:
    # 现货只做多：平仓方向恒为 SELL
    if plan.tp_split:
        half = format_by_step(qty / 2, _step_hint(plan))
        orders = (
            ProtectionOrder(
                kind="oco", side="SELL", quantity=half,
                stop_loss_price=plan.stop_loss,
                take_profit_price=plan.take_profit_1,
                group=1,
            ),
            ProtectionOrder(
                kind="oco", side="SELL", quantity=half,
                stop_loss_price=plan.stop_loss,
                take_profit_price=plan.take_profit_2 or plan.take_profit_1,
                group=2,
            ),
        )
        return ProtectionPlan(
            orders=orders, market=SPOT,
            note="TP1/TP2 各半量双 OCO：任一止盈成交其同组止损联动取消",
        )
    orders = (
        ProtectionOrder(
            kind="oco", side="SELL", quantity=qty,
            stop_loss_price=plan.stop_loss,
            take_profit_price=plan.take_profit_1,
            group=1,
        ),
    )
    return ProtectionPlan(orders=orders, market=SPOT, note="TP1 全量 OCO")


def _futures_protection(plan: OrderPlan, qty: float) -> ProtectionPlan:
    # 合约：平仓方向与持仓相反（平多 SELL / 平空 BUY）
    close_side = "SELL" if not plan.is_short else "BUY"
    if plan.tp_split:
        half = format_by_step(qty / 2, _step_hint(plan))
        tps = (
            ProtectionOrder(
                kind="take_profit", side=close_side, quantity=half,
                stop_loss_price=0.0, take_profit_price=plan.take_profit_1,
                group=1, reduce_only=True,
            ),
            ProtectionOrder(
                kind="take_profit", side=close_side, quantity=half,
                stop_loss_price=0.0,
                take_profit_price=plan.take_profit_2 or plan.take_profit_1,
                group=2, reduce_only=True,
            ),
        )
    else:
        tps = (
            ProtectionOrder(
                kind="take_profit", side=close_side, quantity=qty,
                stop_loss_price=0.0, take_profit_price=plan.take_profit_1,
                group=1, reduce_only=True,
            ),
        )
    sl = ProtectionOrder(
        kind="stop_loss_close", side=close_side, quantity=0.0,
        stop_loss_price=plan.stop_loss,
        take_profit_price=0.0,
        close_position=True,
    )
    return ProtectionPlan(
        orders=tps + (sl,), market=FUTURES,
        note="TP 各半 reduceOnly + closePosition 全平型止损（触发即全平剩余仓位）",
    )


def _step_hint(plan: OrderPlan) -> float:
    """半量截断用的步长提示：从 plan.quantity 反推不可靠，调用方 trader 会
    用 filters 再截断；这里用 0 保守处理（trader 端最终截断）。"""
    return 0.0
