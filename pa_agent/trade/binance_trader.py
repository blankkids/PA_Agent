"""Binance 统一双市场 Trader（现货 binance-sdk-spot / USDT-M 合约）.

- 路由：settings.binance.market 决定使用哪个 SDK 客户端
- 环境：testnet=True → 官方测试网（现货 testnet.binance.vision / 合约 testnet.binancefuture.com）
- 代理：复用 binance_source 的显式配置 + 本地端口探测
- 错误：SDK 异常统一转 TradeError（中文友好信息 + 币安错误码）
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from pa_agent.trade.errors import TradeError
from pa_agent.trade.order_plan import (
    FUTURES,
    SPOT,
    OrderPlan,
    SymbolFilters,
    format_by_step,
)
from pa_agent.trade.protection import ProtectionOrder

logger = logging.getLogger(__name__)

_RECV_WINDOW = 10000
_TIMEOUT_MS = 8000

_SPOT_PROD = "https://api.binance.com"
_SPOT_TESTNET = "https://testnet.binance.vision"
_FUT_PROD = "https://fapi.binance.com"
_FUT_TESTNET = "https://testnet.binancefuture.com"


@dataclass
class OrderResult:
    order_id: int = 0
    symbol: str = ""
    side: str = ""
    type: str = ""
    status: str = ""
    executed_qty: float = 0.0
    avg_price: float = 0.0
    client_order_id: str = ""
    order_list_id: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


def _get(d: Any, *names: str, default: Any = None) -> Any:
    """模型属性或 dict 键取值."""
    for n in names:
        if isinstance(d, dict):
            if n in d:
                return d[n]
        else:
            v = getattr(d, n, None)
            if v is not None:
                return v
    return default


class BinanceTrader:
    """现货 / USDT-M 合约统一交易入口（线程安全：每次调用独立请求）."""

    def __init__(self, settings: Any) -> None:
        # 兼容两种传入：完整 Settings（取 .binance）或直接的 Binance 配置对象
        cfg = settings if hasattr(settings, "api_key") else getattr(settings, "binance", None)
        self._cfg = cfg
        self._spot_client: Any = None
        self._fut_client: Any = None
        self._proxy: dict[str, Any] | None = None
        self._proxy_resolved = False
        self._filters_cache: dict[tuple[str, str], SymbolFilters] = {}
        self._lock = threading.Lock()
        self._clock_warned = False

    # ── 配置与客户端 ─────────────────────────────────────────────────────────

    @property
    def market(self) -> str:
        return getattr(self._cfg, "market", SPOT) or SPOT

    @property
    def testnet(self) -> bool:
        return bool(getattr(self._cfg, "testnet", True))

    def _resolve_proxy(self) -> dict[str, Any] | None:
        if self._proxy_resolved:
            return self._proxy
        from pa_agent.data.binance_source import parse_proxy_url, probe_local_proxy

        explicit = str(getattr(self._cfg, "proxy", "") or "").strip()
        proxy = parse_proxy_url(explicit) if explicit else probe_local_proxy()
        self._proxy, self._proxy_resolved = proxy, True
        return proxy

    def _make_client(self, market: str) -> Any:
        try:
            if market == FUTURES:
                from binance_common.configuration import ConfigurationRestAPI
                from binance_sdk_derivatives_trading_usds_futures.derivatives_trading_usds_futures import (
                    DerivativesTradingUsdsFutures,
                )

                cfg = ConfigurationRestAPI(
                    api_key=self._api_key(),
                    api_secret=self._api_secret(),
                    base_path=_FUT_TESTNET if self.testnet else _FUT_PROD,
                    timeout=_TIMEOUT_MS,
                    retries=0,
                    proxy=self._resolve_proxy(),
                )
                return DerivativesTradingUsdsFutures(config_rest_api=cfg).rest_api
            from binance_common.configuration import ConfigurationRestAPI
            from binance_sdk_spot.spot import Spot

            cfg = ConfigurationRestAPI(
                api_key=self._api_key(),
                api_secret=self._api_secret(),
                base_path=_SPOT_TESTNET if self.testnet else _SPOT_PROD,
                timeout=_TIMEOUT_MS,
                retries=0,
                proxy=self._resolve_proxy(),
            )
            return Spot(config_rest_api=cfg).rest_api
        except ImportError as exc:
            raise TradeError(f"交易 SDK 未安装：{exc}") from exc

    def _client(self, market: str | None = None) -> Any:
        market = market or self.market
        with self._lock:
            if market == FUTURES:
                if self._fut_client is None:
                    self._fut_client = self._make_client(FUTURES)
                return self._fut_client
            if self._spot_client is None:
                self._spot_client = self._make_client(SPOT)
            return self._spot_client

    def _api_key(self) -> str:
        return str(getattr(self._cfg, "api_key", "") or "").strip()

    def _api_secret(self) -> str:
        return str(getattr(self._cfg, "api_secret", "") or "").strip()

    def ensure_credentials(self) -> None:
        if not self._api_key() or not self._api_secret():
            raise TradeError("未配置 Binance API Key/Secret，请先在交易设置中填写")

    def verify_clock(self) -> float:
        """返回本机与服务器时间偏差（秒）；>2s 记警告."""
        try:
            cli = self._client(self.market)
            if self.market == FUTURES:
                resp = cli.check_server_time()  # futures SDK 方法名
                server_ms = int(_get(resp.data(), "serverTime", default=0) or 0)
            else:
                resp = cli.time()  # spot SDK
                data = resp.data()
                if isinstance(data, (int, float)):
                    server_ms = int(data)
                else:
                    server_ms = int(_get(data, "serverTime", default=0) or 0)
            offset_s = (server_ms - time.time() * 1000) / 1000.0 if server_ms else 0.0
            if abs(offset_s) > 2.0 and not self._clock_warned:
                logger.warning("本机时钟与币安服务器偏差 %.1fs，可能拒单，请校时", offset_s)
                self._clock_warned = True
            return offset_s
        except Exception as exc:  # noqa: BLE001
            logger.debug("server time 校验失败: %s", exc)
            return 0.0

    # ── 通用查询 ─────────────────────────────────────────────────────────────

    def symbol_filters(self, symbol: str, market: str | None = None) -> SymbolFilters:
        market = market or self.market
        key = (market, symbol)
        with self._lock:
            if key in self._filters_cache:
                return self._filters_cache[key]
        try:
            cli = self._client(market)
            if market == FUTURES:
                # futures SDK 的 exchange_information() 无参，返回全量 → 客户端过滤
                data = cli.exchange_information().data()
            else:
                data = cli.exchange_info(symbol=symbol).data()
            symbols = _get(data, "symbols", default=[]) or []
            item = next((s for s in symbols
                         if _get(s, "symbol", default="") == symbol), None)
            if item is None:
                raise TradeError(f"交易对 {symbol} 在该市场不存在")
            filters = _get(item, "filters", default=[]) or []
            result = SymbolFilters.from_filters(symbol, [dict(f) for f in filters])
        except TradeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TradeError.from_exception(exc) from exc
        with self._lock:
            self._filters_cache[key] = result
        return result

    def balances(self, market: str | None = None) -> list[dict[str, Any]]:
        """非零余额：[{asset, free, locked}]（合约返回可用保证金资产）."""
        market = market or self.market
        try:
            if market == FUTURES:
                resp = self._client(market).account_information_v3()
                data = resp.data()
                assets = _get(data, "assets", default=[]) or []
                out = []
                for a in assets:
                    free = float(_get(a, "availableBalance", "crossWalletBalance", default=0) or 0)
                    locked = float(_get(a, "walletBalance", default=0) or 0) - free
                    if abs(float(_get(a, "walletBalance", default=0) or 0)) > 1e-9:
                        out.append({
                            "asset": _get(a, "asset", default=""),
                            "free": round(free, 8),
                            "locked": round(max(locked, 0.0), 8),
                        })
                return out
            resp = self._client(market).get_account(omit_zero_balances=True)
            data = resp.data()
            out = []
            for b in _get(data, "balances", default=[]) or []:
                free = float(_get(b, "free", default=0) or 0)
                locked = float(_get(b, "locked", default=0) or 0)
                if abs(free) > 1e-9 or abs(locked) > 1e-9:
                    out.append({
                        "asset": _get(b, "asset", default=""),
                        "free": free,
                        "locked": locked,
                    })
            return out
        except Exception as exc:  # noqa: BLE001
            raise TradeError.from_exception(exc) from exc

    def positions(self, symbol: str) -> list[dict[str, Any]]:
        """合约持仓（单向模式）."""
        try:
            resp = self._client(FUTURES).position_information_v3(symbol=symbol)
            data = resp.data()
            data = data if isinstance(data, list) else [data]
            out = []
            for p in data:
                amt = float(_get(p, "positionAmt", default=0) or 0)
                if abs(amt) < 1e-12:
                    continue
                entry = float(_get(p, "entryPrice", default=0) or 0)
                out.append({
                    "symbol": _get(p, "symbol", default=symbol),
                    "amount": amt,
                    "direction": "做多" if amt > 0 else "做空",
                    "entry_price": entry,
                    "unrealized_pnl": float(_get(p, "unRealizedProfit", "unrealizedProfit", default=0) or 0),
                    "leverage": _get(p, "leverage", default=""),
                    "mark_price": float(_get(p, "markPrice", default=0) or 0),
                })
            return out
        except Exception as exc:  # noqa: BLE001
            raise TradeError.from_exception(exc) from exc

    def open_orders(self, symbol: str) -> list[dict[str, Any]]:
        """当前交易对挂单（合约含 algo 条件单，标记 algo=True）."""
        try:
            cli = self._client(self.market)
            out: list[dict[str, Any]] = []
            resp = cli.current_all_open_orders(symbol=symbol) if self.market == FUTURES \
                else cli.get_open_orders(symbol=symbol)
            data = resp.data()
            for o in data or []:
                out.append({
                    "order_id": int(_get(o, "orderId", default=0) or 0),
                    "symbol": _get(o, "symbol", default=symbol),
                    "side": _get(o, "side", default=""),
                    "type": _get(o, "type", default=""),
                    "price": float(_get(o, "price", default=0) or 0),
                    "orig_qty": float(_get(o, "origQty", default=0) or 0),
                    "stop_price": float(_get(o, "stopPrice", default=0) or 0),
                    "status": _get(o, "status", default=""),
                    "reduce_only": bool(_get(o, "reduceOnly", default=False)),
                    "algo": False,
                })
            if self.market == FUTURES:
                try:
                    resp2 = cli.current_all_algo_open_orders(symbol=symbol)
                    for o in _get(resp2.data(), "orders", default=[]) or []:
                        out.append({
                            "order_id": int(_get(o, "algoId", "orderId", default=0) or 0),
                            "symbol": _get(o, "symbol", default=symbol),
                            "side": _get(o, "side", default=""),
                            "type": _get(o, "algoType", "type", default=""),
                            "price": float(_get(o, "price", default=0) or 0),
                            "orig_qty": float(_get(o, "origQty", "quantity", default=0) or 0),
                            "stop_price": float(_get(o, "triggerPrice", "stopPrice", default=0) or 0),
                            "status": _get(o, "algoStatus", "status", default=""),
                            "reduce_only": bool(_get(o, "reduceOnly", default=False)),
                            "algo": True,
                        })
                except Exception as exc:  # noqa: BLE001
                    logger.debug("algo open orders 查询失败: %s", exc)
            return out
        except Exception as exc:  # noqa: BLE001
            raise TradeError.from_exception(exc) from exc

    def query_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        """订单状态（FillWatcher 轮询用）。现货 get_order / 合约 query_order."""
        try:
            cli = self._client(self.market)
            if self.market == FUTURES:
                resp = cli.query_order(symbol=symbol, order_id=order_id)
            else:
                resp = cli.get_order(symbol=symbol, order_id=order_id)
            o = resp.data()
            return {
                "order_id": int(_get(o, "orderId", default=0) or 0),
                "status": _get(o, "status", default=""),
                "executed_qty": float(_get(o, "executedQty", default=0) or 0),
                "avg_price": float(_get(o, "avgPrice", default=0) or 0),
            }
        except Exception as exc:  # noqa: BLE001
            raise TradeError.from_exception(exc) from exc

    # ── 撤单 ─────────────────────────────────────────────────────────────────

    def cancel_order(self, symbol: str, order_id: int, *, algo: bool = False) -> None:
        try:
            cli = self._client(self.market)
            if algo and self.market == FUTURES:
                # futures cancel_algo_order 签名无 symbol 参数（内省核实）
                cli.cancel_algo_order(algo_id=order_id)
            else:
                cli.delete_order(symbol=symbol, order_id=order_id) if self.market == SPOT \
                    else cli.cancel_order(symbol=symbol, order_id=order_id)
        except Exception as exc:  # noqa: BLE001
            raise TradeError.from_exception(exc) from exc

    def cancel_all(self, symbol: str) -> int:
        """撤销该交易对全部挂单（普通+algo）；返回撤销的接口调用数；失败上抛."""
        cli = self._client(self.market)
        count = 0
        if self.market == SPOT:
            cli.delete_open_orders(symbol=symbol)
            count += 1
        else:
            cli.cancel_all_open_orders(symbol=symbol)
            count += 1
            cli.cancel_all_algo_open_orders(symbol=symbol)
            count += 1
        return count

    # ── 入场 ─────────────────────────────────────────────────────────────────

    def ensure_futures_settings(self, symbol: str, leverage: int) -> None:
        """合约下单前：单向持仓 + 目标杠杆."""
        if self.market != FUTURES:
            return
        cli = self._client(FUTURES)
        try:
            # SDK 签名为 Optional[str]（内省核实）；已是单向时币安返回 -4059 可忽略
            cli.change_position_mode(dual_side_position="false")
        except Exception as exc:  # noqa: BLE001
            if "-4059" not in str(exc):
                logger.debug("position mode 设置: %s", exc)
        try:
            cli.change_initial_leverage(symbol=symbol, leverage=leverage)
        except Exception as exc:  # noqa: BLE001
            raise TradeError.from_exception(exc) from exc

    def place_entry(self, plan: OrderPlan) -> OrderResult:
        """提交入场单（市价/限价；现货市价按金额 quoteOrderQty）."""
        self.ensure_credentials()
        try:
            cli = self._client(plan.market)
            if plan.market == FUTURES:
                self.ensure_futures_settings(plan.symbol, plan.leverage)
                resp = cli.new_order(
                    symbol=plan.symbol,
                    side=plan.side,
                    type="MARKET" if plan.order_kind == "market" else "LIMIT",
                    time_in_force=None if plan.order_kind == "market" else "GTC",
                    quantity=plan.quantity,
                    price=plan.entry_price if plan.order_kind == "limit" else None,
                    new_order_resp_type="RESULT",
                    recv_window=_RECV_WINDOW,
                )
            elif plan.order_kind == "market":
                resp = cli.new_order(
                    symbol=plan.symbol,
                    side="BUY",
                    type="MARKET",
                    quote_order_qty=plan.stake_usdt,
                    new_order_resp_type="RESULT",
                    recv_window=_RECV_WINDOW,
                )
            else:
                resp = cli.new_order(
                    symbol=plan.symbol,
                    side="BUY",
                    type="LIMIT",
                    time_in_force="GTC",
                    quantity=plan.quantity,
                    price=plan.entry_price,
                    new_order_resp_type="RESULT",
                    recv_window=_RECV_WINDOW,
                )
            return self._parse_order_result(resp.data())
        except TradeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TradeError.from_exception(exc) from exc

    def _parse_order_result(self, o: Any) -> OrderResult:
        cum_quote = float(_get(o, "cummulativeQuoteQty", default=0) or 0)
        executed = float(_get(o, "executedQty", default=0) or 0)
        avg = (cum_quote / executed) if executed > 0 and cum_quote > 0 else float(
            _get(o, "avgPrice", default=0) or 0
        )
        return OrderResult(
            order_id=int(_get(o, "orderId", default=0) or 0),
            symbol=_get(o, "symbol", default=""),
            side=_get(o, "side", default=""),
            type=_get(o, "type", default=""),
            status=_get(o, "status", default=""),
            executed_qty=executed,
            avg_price=avg,
            client_order_id=_get(o, "clientOrderId", default="") or "",
            order_list_id=int(_get(o, "orderListId", default=0) or 0),
            raw=o if isinstance(o, dict) else {},
        )

    # ── 保护单 ───────────────────────────────────────────────────────────────

    def place_protection(self, plan: OrderPlan, filled_qty: float) -> list[OrderResult]:
        """按 ProtectionPlan 提交保护单；返回结果（失败抛 TradeError）."""
        from pa_agent.trade.protection import build_protection_plan

        results: list[OrderResult] = []
        try:
            pplan = build_protection_plan(plan, filled_qty)
        except ValueError as exc:
            raise TradeError(str(exc)) from exc
        for po in pplan.orders:
            try:
                results.append(self._submit_protection_order(plan, po))
            except Exception as exc:  # noqa: BLE001
                # 已提交部分保留，失败信息带出
                err = TradeError.from_exception(exc)
                err.friendly_message = f"保护单（{po.kind} g{po.group}）失败：{err.friendly_message}"
                raise err from exc
        return results

    def _submit_protection_order(self, plan: OrderPlan, po: ProtectionOrder) -> OrderResult:
        cli = self._client(plan.market)
        if plan.market == SPOT and po.kind == "oco":
            resp = cli.order_oco(
                symbol=plan.symbol,
                side="SELL",
                quantity=po.quantity,
                price=po.take_profit_price,           # TP 限价腿
                stop_price=po.stop_loss_price,        # SL 触发价
                stop_limit_price=po.stop_loss_price,  # SL 限价=触发价（GTC）
                stop_limit_time_in_force="GTC",
                recv_window=_RECV_WINDOW,
            )
            data = resp.data()
            oid = int(_get(data, "orderListId", default=0) or 0)
            orders = _get(data, "orders", default=[]) or []
            return OrderResult(
                order_id=oid,
                symbol=plan.symbol,
                side="SELL",
                type="OCO",
                status=_get(data, "contingencyType", default="OCO"),
                executed_qty=0.0,
                avg_price=0.0,
                raw={"orders": [str(x) for x in orders]},
            )
        if plan.market == FUTURES and po.kind == "take_profit":
            resp = cli.new_algo_order(
                algo_type="CONDITIONAL",
                symbol=plan.symbol,
                side=po.side,
                type="TAKE_PROFIT_MARKET",
                quantity=po.quantity,
                trigger_price=po.take_profit_price,
                reduce_only="true",
                recv_window=_RECV_WINDOW,
            )
            data = resp.data()
            return OrderResult(
                order_id=int(_get(data, "algoId", default=0) or 0),
                symbol=plan.symbol,
                side=po.side,
                type="TAKE_PROFIT_MARKET",
                status="NEW",
                executed_qty=0.0,
                avg_price=0.0,
                raw=data if isinstance(data, dict) else {},
            )
        if plan.market == FUTURES and po.kind == "stop_loss_close":
            resp = cli.new_algo_order(
                algo_type="CONDITIONAL",
                symbol=plan.symbol,
                side=po.side,
                type="STOP_MARKET",
                trigger_price=po.stop_loss_price,
                close_position="true",
                recv_window=_RECV_WINDOW,
            )
            data = resp.data()
            return OrderResult(
                order_id=int(_get(data, "algoId", default=0) or 0),
                symbol=plan.symbol,
                side=po.side,
                type="STOP_MARKET",
                status="NEW",
                executed_qty=0.0,
                avg_price=0.0,
                raw=data if isinstance(data, dict) else {},
            )
        raise TradeError(f"不支持的保护单组合：market={plan.market} kind={po.kind}")

    # ── 平仓 ─────────────────────────────────────────────────────────────────

    def close_position_market(self, symbol: str) -> OrderResult:
        """市价平掉当前市场该交易对全部持仓（先撤保护单由调用方处理）."""
        self.ensure_credentials()
        try:
            if self.market == FUTURES:
                positions = self.positions(symbol)
                if not positions:
                    raise TradeError("当前无持仓，无法平仓")
                amt = sum(p["amount"] for p in positions)
                side = "SELL" if amt > 0 else "BUY"
                qty = abs(amt)
                cli = self._client(FUTURES)
                resp = cli.new_order(
                    symbol=symbol, side=side, type="MARKET",
                    quantity=qty, reduce_only="true",
                    new_order_resp_type="RESULT", recv_window=_RECV_WINDOW,
                )
                return self._parse_order_result(resp.data())
            # 现货：卖出全部可用 base
            base = symbol.replace("USDT", "").replace("USDC", "")
            resp = self._client(SPOT).get_account()
            free = 0.0
            for b in _get(resp.data(), "balances", default=[]) or []:
                if _get(b, "asset", default="") == base:
                    free = float(_get(b, "free", default=0) or 0)
                    break
            if free <= 0:
                raise TradeError(f"无可用 {base} 持仓，无法平仓")
            filters = self.symbol_filters(symbol, SPOT)
            qty = format_by_step(free, filters.step_size)
            resp = self._client(SPOT).new_order(
                symbol=symbol, side="SELL", type="MARKET",
                quantity=qty, new_order_resp_type="RESULT",
                recv_window=_RECV_WINDOW,
            )
            return self._parse_order_result(resp.data())
        except TradeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TradeError.from_exception(exc) from exc
