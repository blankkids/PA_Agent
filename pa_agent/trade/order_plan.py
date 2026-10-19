"""AI 决策 → Binance 订单计划（双市场）."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from pa_agent.trade.errors import TradeBlockedError

SPOT = "spot"
FUTURES = "futures"

_TRADE_ORDER_TYPES = frozenset({"限价单", "突破单", "市价单"})


@dataclass(frozen=True)
class SymbolFilters:
    """交易对过滤器（来自 exchange_info）."""

    symbol: str = ""
    tick_size: float = 0.0   # 价格步长
    step_size: float = 0.0   # 数量步长
    min_qty: float = 0.0     # 最小数量
    min_notional: float = 0.0  # 最小名义价值

    @classmethod
    def from_filters(cls, symbol: str, filters: list[dict[str, Any]]) -> "SymbolFilters":
        tick = step = min_qty = min_notional = 0.0
        for f in filters or []:
            ftype = f.get("filterType") or f.get("filter_type") or ""
            try:
                if ftype == "PRICE_FILTER":
                    tick = float(f.get("tickSize") or f.get("tick_size") or 0)
                elif ftype == "LOT_SIZE":
                    step = float(f.get("stepSize") or f.get("step_size") or 0)
                    min_qty = float(f.get("minQty") or f.get("min_qty") or 0)
                elif ftype in ("NOTIONAL", "MIN_NOTIONAL"):
                    min_notional = float(
                        f.get("minNotional")
                        or f.get("min_notional")
                        or f.get("notional")
                        or 0
                    )
            except (TypeError, ValueError):
                continue
        return cls(symbol=symbol, tick_size=tick, step_size=step,
                   min_qty=min_qty, min_notional=min_notional)


def round_down_to_step(value: float, step: float) -> float:
    """按步长向下截断（step<=0 时按原值保留 8 位小数）."""
    if step and step > 0:
        return math.floor(value / step + 1e-9) * step
    return math.floor(value * 1e8) / 1e8


def format_by_step(value: float, step: float) -> float:
    """截断后消除浮点尾差（用于展示与提交）."""
    v = round_down_to_step(value, step)
    if step and step > 0:
        decimals = max(0, int(round(-math.log10(step))))
        # tickSize 可能是 0.00010000 之类，log10 取整后安全截断
        return round(v, min(decimals, 8))
    return round(v, 8)


@dataclass(frozen=True)
class OrderPlan:
    """一次入场的完整订单计划（方向/入场 + 保护参数）."""

    market: str                     # spot | futures
    symbol: str
    direction: str                  # 做多 | 做空
    order_kind: str                 # market | limit
    entry_price: float | None       # 限价入场价；市价为 None
    stake_usdt: float               # 本笔投入金额（合约名义=金额×杠杆）
    leverage: int                   # 合约杠杆；现货为 1
    quantity: float                 # 入场数量（已截断）
    stop_loss: float                # 止损价
    take_profit_1: float            # TP1
    take_profit_2: float | None     # TP2（未设置时 None）
    tp_split: bool                  # True=TP1/TP2 各半；False=TP1 全量
    side: str = field(default="")   # BUY | SELL（现货恒 BUY；合约按方向）

    @property
    def is_short(self) -> bool:
        return self.direction == "做空"


def build_order_plan(
    decision: dict[str, Any],
    symbol: str,
    *,
    market: str,
    stake_usdt: float,
    leverage: int,
    filters: SymbolFilters,
    last_price: float | None = None,
) -> OrderPlan:
    """阶段二决策 → OrderPlan；现货做空/非法决策抛 TradeBlockedError.

    市价单数量估算优先用 *last_price*（实时价）；缺省退化为 TP1 近似。
    现货市价实际按 quoteOrderQty 金额成交，数量仅供展示与护栏校验。
    """
    order_type = str(decision.get("order_type") or "")
    direction = str(decision.get("order_direction") or "")
    if order_type not in _TRADE_ORDER_TYPES:
        raise TradeBlockedError(f"决策为「{order_type or '不下单'}」，无可执行订单")
    if direction not in ("做多", "做空"):
        raise TradeBlockedError(f"决策方向无效：{direction!r}")
    if direction == "做空" and market == SPOT:
        raise TradeBlockedError("现货市场不支持做空，请在交易设置中切换到合约市场")

    def _price(key: str) -> float:
        v = decision.get(key)
        try:
            return float(v)
        except (TypeError, ValueError) as exc:
            raise TradeBlockedError(f"决策缺少有效 {key}：{v!r}") from exc

    stop_loss = _price("stop_loss_price")
    tp1 = _price("take_profit_price")
    tp2_raw = decision.get("take_profit_price_2")
    tp2: float | None = None
    if tp2_raw is not None:
        try:
            tp2_val = float(tp2_raw)
            if tp2_val > 0:
                tp2 = tp2_val
        except (TypeError, ValueError):
            tp2 = None

    order_kind = "market" if order_type == "市价单" else "limit"
    entry_price: float | None = None
    if order_kind == "limit":
        entry_price = _price("entry_price")

    # 数量估算：现货按金额/参考价；合约名义=金额×杠杆
    ref_price = entry_price if entry_price else (last_price or tp1)
    if not ref_price or ref_price <= 0:
        raise TradeBlockedError("缺少可用于估算数量的参考价格（实时价/入场价）")
    notional = stake_usdt * (leverage if market == FUTURES else 1)
    quantity = format_by_step(notional / ref_price, filters.step_size)

    if quantity <= 0 or (filters.min_qty and quantity < filters.min_qty):
        raise TradeBlockedError(
            f"计算数量 {quantity} 低于最小数量 {filters.min_qty}，请增大金额"
        )
    if filters.min_notional and quantity * ref_price < filters.min_notional:
        raise TradeBlockedError(
            f"名义价值 {quantity * ref_price:.2f} USDT 低于最小要求 {filters.min_notional:.2f}"
        )

    return OrderPlan(
        market=market,
        symbol=symbol,
        direction=direction,
        order_kind=order_kind,
        entry_price=format_by_step(entry_price, filters.tick_size) if entry_price else None,
        stake_usdt=stake_usdt,
        leverage=leverage if market == FUTURES else 1,
        quantity=quantity,
        stop_loss=format_by_step(stop_loss, filters.tick_size),
        take_profit_1=format_by_step(tp1, filters.tick_size),
        take_profit_2=format_by_step(tp2, filters.tick_size) if tp2 else None,
        # 默认 TP1 全仓止盈；分批（TP1/TP2 各半）仅在人工确认对话框勾选启用，
        # 全自动路径恒为全仓
        tp_split=False,
        side=_side_for(market, direction),
    )


def _side_for(market: str, direction: str) -> str:
    if market == FUTURES:
        return "SELL" if direction == "做空" else "BUY"
    return "BUY"
