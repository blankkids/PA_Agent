"""PA Agent Binance 交易执行层（现货 + USDT-M 合约）."""
from pa_agent.trade.binance_trader import BinanceTrader, OrderResult
from pa_agent.trade.errors import TradeBlockedError, TradeError
from pa_agent.trade.order_plan import (
    FUTURES,
    SPOT,
    OrderPlan,
    SymbolFilters,
    build_order_plan,
)

__all__ = [
    "BinanceTrader",
    "OrderResult",
    "TradeError",
    "TradeBlockedError",
    "OrderPlan",
    "SymbolFilters",
    "build_order_plan",
    "SPOT",
    "FUTURES",
]
