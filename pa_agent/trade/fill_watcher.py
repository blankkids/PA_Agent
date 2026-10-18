"""限价入场成交检测：后台轮询 → 成交后自动挂保护单."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from pa_agent.trade.binance_trader import BinanceTrader
from pa_agent.trade.order_plan import OrderPlan
from pa_agent.trade.errors import TradeError

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 5.0
_MAX_BACKOFF_S = 60.0


class FillWatcher:
    """轮询限价单状态；FILLED → 回调（挂保护单）。守护线程，失败指数退避."""

    def __init__(self, trader: BinanceTrader) -> None:
        self._trader = trader
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._watching: dict[int, tuple[str, OrderPlan]] = {}
        self._lock = threading.Lock()
        self._on_filled: Callable[[int, OrderPlan, float], None] | None = None
        self._on_error: Callable[[int, str], None] | None = None

    # ── 回调注册（主线程设置） ────────────────────────────────────────────────

    def set_callbacks(
        self,
        on_filled: Callable[[int, OrderPlan, float], None],
        on_error: Callable[[int, str], None] | None = None,
    ) -> None:
        self._on_filled = on_filled
        self._on_error = on_error

    # ── 监听管理 ─────────────────────────────────────────────────────────────

    def watch(self, symbol: str, order_id: int, plan: OrderPlan) -> None:
        """登记一个待成交限价单并确保线程运行."""
        with self._lock:
            self._watching[order_id] = (symbol, plan)
        self._ensure_thread()

    def unwatch(self, order_id: int) -> None:
        with self._lock:
            self._watching.pop(order_id, None)

    def watching_ids(self) -> list[int]:
        with self._lock:
            return list(self._watching)

    def stop(self) -> None:
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    # ── 内部 ─────────────────────────────────────────────────────────────────

    def _ensure_thread(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="binance-fill-watcher", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        backoff = _POLL_INTERVAL_S
        while not self._stop_evt.is_set():
            with self._lock:
                items = list(self._watching.items())
            if not items:
                # 无监听任务：低频待命
                self._stop_evt.wait(2.0)
                continue
            try:
                self._poll_once(items)
                backoff = _POLL_INTERVAL_S
            except Exception as exc:  # noqa: BLE001
                logger.warning("FillWatcher 轮询失败（%.0fs 后重试）: %s", backoff, exc)
                self._stop_evt.wait(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF_S)
                continue
            self._stop_evt.wait(_POLL_INTERVAL_S)

    def _poll_once(self, items: list[tuple[int, tuple[str, OrderPlan]]]) -> None:
        for order_id, (symbol, plan) in items:
            try:
                st = self._trader.query_order(symbol, order_id)
            except TradeError as exc:
                if self._on_error:
                    self._on_error(order_id, exc.friendly_message)
                continue
            status = str(st.get("status") or "").upper()
            if status == "FILLED":
                with self._lock:
                    self._watching.pop(order_id, None)
                executed = float(st.get("executed_qty") or 0)
                if executed > 0 and self._on_filled:
                    self._on_filled(order_id, plan, executed)
            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                with self._lock:
                    self._watching.pop(order_id, None)


def scan_unprotected(
    trader: BinanceTrader,
    symbol: str,
) -> list[dict[str, Any]]:
    """启动兜底：找「有持仓但无保护挂单」的交易对状态.

    返回 [{symbol, market, has_position, has_protection}]——调用方提示用户补挂。
    """
    result: list[dict[str, Any]] = []
    try:
        market = trader.market
        has_position = False
        if market == "futures":
            has_position = bool(trader.positions(symbol))
        else:
            balances = trader.balances(market)
            base = symbol.replace("USDT", "").replace("USDC", "")
            has_position = any(
                b["asset"] == base and b["free"] > 0 for b in balances
            )
        orders = trader.open_orders(symbol)
        has_protection = any(
            o.get("algo") or o.get("type") in ("OCO", "STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT")
            for o in orders
        )
        result.append({
            "symbol": symbol, "market": market,
            "has_position": has_position, "has_protection": has_protection,
        })
    except TradeError as exc:
        logger.debug("scan_unprotected 失败: %s", exc.friendly_message)
    return result
