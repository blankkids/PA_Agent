"""Binance 现货 K 线数据源（官方 binance-sdk-spot：REST 回补 + WebSocket 实时流）.

- 历史回补：REST ``GET /api/v3/klines``（api.binance.com，失败切 data-api.binance.vision）
- 实时更新：WebSocket Streams ``<symbol>@kline_<interval>``（SDK 自带断线重连）
- 本地代理：wss://stream.binance.com 在部分网络无法直连。支持在
  config/settings.json 的 ``binance.proxy`` 填 ``http://127.0.0.1:7890``，
  或设置环境变量 ``BINANCE_PROXY``；留空时自动探测常见本地代理端口
  （7890 / 7897 / 10809 / 1080），探测到即优先走代理。

无需 API Key（仅公开行情）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import threading
import time
from typing import Any

from pa_agent.data.base import DataSource, DataSourceTransientError, KlineBar, normalize_kline_bar

logger = logging.getLogger(__name__)

# 本应用周期 → Binance interval（原生支持，无需换算）
_SUPPORTED_TIMEFRAMES: tuple[str, ...] = (
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w",
)

_PRESET_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "TRXUSDT",
    "LTCUSDT", "ETHBTC",
)

_SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,24}$")

# REST 备用域名：data-api.binance.vision 为官方纯行情镜像
_REST_BASES: tuple[str, ...] = ("https://api.binance.com", "https://data-api.binance.vision")
# WebSocket 备用域名：data-stream.binance.vision 为官方纯行情流镜像
_WS_URLS: tuple[str, ...] = (
    "wss://stream.binance.com:9443",
    "wss://data-stream.binance.vision",
)

# 留空 binance.proxy 时按序探测的常见本地代理端口（clash / v2ray / shadowsocks）
_LOCAL_PROXY_PORTS: tuple[int, ...] = (7890, 7897, 10809, 1080)

_WS_CONNECT_TIMEOUT_S = 10.0
_WS_READY_WAIT_S = 10.0
_MAX_CACHE_BARS = 1500


def normalize_binance_symbol(symbol: str) -> str:
    """BTC/USDT / btc usdt → BTCUSDT；无效返回空串。"""
    s = re.sub(r"[\s/\-_]", "", (symbol or "").strip()).upper()
    return s if _SYMBOL_RE.match(s) else ""


def parse_proxy_url(url: str) -> dict[str, Any] | None:
    """把 ``http://127.0.0.1:7890`` 解析为 SDK proxy dict；无效返回 None。"""
    raw = (url or "").strip()
    if not raw:
        return None
    m = re.match(r"^(?:(http|https)://)?([A-Za-z0-9_.\-]+):(\d{2,5})/?$", raw)
    if not m:
        return None
    protocol, host, port = m.group(1) or "http", m.group(2), int(m.group(3))
    if not (1 <= port <= 65535):
        return None
    return {"host": host, "port": port, "protocol": protocol}


def probe_local_proxy() -> dict[str, Any] | None:
    """TCP 探测常见本地代理端口，返回第一个存活的 proxy dict。"""
    for port in _LOCAL_PROXY_PORTS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.3)
        try:
            sock.connect(("127.0.0.1", port))
            return {"host": "127.0.0.1", "port": port, "protocol": "http"}
        except OSError:
            continue
        finally:
            sock.close()
    return None


class BinanceSource(DataSource):
    """币安现货 K 线：REST 回补 + WebSocket 实时推送（binance-sdk-spot）."""

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings
        self._symbol: str = ""
        self._timeframe: str = ""
        self._connected = False

        # 路由：显式配置 > 自动探测；_proxy 同时用于 REST 与 WS
        self._proxy: dict[str, Any] | None = None
        self._proxy_source = ""  # "settings" / "env" / "auto:7890" / "direct"
        self._rest_route: tuple[str, dict | None] | None = None  # (base_url, proxy) 记忆上次可用组合

        # K 线缓存：ts_open_ms → bar dict（WS 与 REST 共同维护）
        self._bars: dict[int, dict[str, Any]] = {}
        self._bars_lock = threading.Lock()
        self._last_ws_msg_mono = 0.0

        # WS 线程（asyncio loop 常驻，按需启停流）
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_ready = threading.Event()
        self._ws_stop_evt: asyncio.Event | None = None
        self._ws_task: asyncio.Task | None = None
        self._last_ws_attempt_mono = 0.0

    # ── 生命周期 ─────────────────────────────────────────────────────────────

    def connect(self) -> None:
        try:
            from binance_sdk_spot.spot import Spot  # noqa: F401
        except ImportError as exc:
            raise DataSourceTransientError(
                "未安装 binance-sdk-spot，请执行: pip install binance-sdk-spot"
            ) from exc

        self._proxy, self._proxy_source = self._resolve_proxy()
        self._connected = True
        if self._proxy:
            logger.info(
                "BinanceSource connected（代理 %s:%s，来源 %s）",
                self._proxy["host"], self._proxy["port"], self._proxy_source,
            )
        else:
            logger.info("BinanceSource connected（直连，来源 %s）", self._proxy_source)

    def disconnect(self) -> None:
        self._stop_ws_stream()
        self._shutdown_ws_loop()
        with self._bars_lock:
            self._bars.clear()
        self._symbol = ""
        self._timeframe = ""
        self._rest_route = None
        self._connected = False
        logger.info("BinanceSource disconnected")

    def _resolve_proxy(self) -> tuple[dict[str, Any] | None, str]:
        """显式配置优先（settings / 环境变量），否则 TCP 探测本地代理端口."""
        explicit = ""
        src = ""
        settings_proxy = getattr(getattr(self._settings, "binance", None), "proxy", "")
        if str(settings_proxy or "").strip():
            explicit = str(settings_proxy).strip()
            src = "settings"
        env_proxy = (os.environ.get("BINANCE_PROXY") or "").strip()
        if env_proxy:
            explicit = env_proxy
            src = "env"
        if explicit:
            if explicit.lower().startswith("socks"):
                raise DataSourceTransientError(
                    f"Binance 代理仅支持 http/https（当前: {explicit}）；"
                    "请改用本地代理的 http 端口。"
                )
            proxy = parse_proxy_url(explicit)
            if proxy is None:
                raise DataSourceTransientError(
                    f"Binance 代理地址无效: {explicit!r}，应形如 http://127.0.0.1:7890"
                )
            return proxy, src
        proxy = probe_local_proxy()
        if proxy is not None:
            return proxy, f"auto:{proxy['port']}"
        return None, "direct"

    # ── 发现 ─────────────────────────────────────────────────────────────────

    def list_symbols(self) -> list[str]:
        return list(_PRESET_SYMBOLS)

    def supported_timeframes(self) -> list[str]:
        return list(_SUPPORTED_TIMEFRAMES)

    def is_symbol_available(self, symbol: str) -> bool:
        return bool(_SYMBOL_RE.match(normalize_binance_symbol(symbol)))

    # ── 订阅 ─────────────────────────────────────────────────────────────────

    def subscribe(self, symbol: str, timeframe: str) -> None:
        if timeframe not in _SUPPORTED_TIMEFRAMES:
            raise ValueError(
                f"Binance 不支持周期 {timeframe!r}，可用: {list(_SUPPORTED_TIMEFRAMES)}"
            )
        code = normalize_binance_symbol(symbol)
        if not code:
            raise ValueError("币对无效，请输入如 BTCUSDT / ETHUSDT（可写 BTC/USDT）")
        self._stop_ws_stream()
        self._symbol = code
        self._timeframe = timeframe
        with self._bars_lock:
            self._bars.clear()
        self._last_ws_msg_mono = 0.0
        self._ws_ready.clear()

        # 1) REST 回补历史（失败则抛错，订阅失败）
        rows = self._rest_fetch_rows(code, timeframe, 400)
        self._store_rows(rows)

        # 2) 启动 WebSocket 实时流（失败不阻断：latest_snapshot 会走 REST 兜底）
        try:
            self._start_ws_stream()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Binance WS 启动失败，暂时以 REST 轮询兜底: %s", exc)
        logger.info("BinanceSource subscribed: %s %s", code, timeframe)

    def unsubscribe(self) -> None:
        self._stop_ws_stream()
        self._symbol = ""
        self._timeframe = ""
        with self._bars_lock:
            self._bars.clear()
        logger.info("BinanceSource unsubscribed")

    # ── 快照 ─────────────────────────────────────────────────────────────────

    def latest_snapshot(self, n: int) -> list[KlineBar]:
        if not self._connected:
            raise DataSourceTransientError("Binance 数据源未连接")
        if not self._symbol or not self._timeframe:
            raise DataSourceTransientError("Binance 未订阅币对/周期")

        self._maybe_rest_refresh_if_stale()
        with self._bars_lock:
            bars = list(self._bars.values())
        if not bars:
            rows = self._rest_fetch_rows(self._symbol, self._timeframe, max(n + 60, 120))
            self._store_rows(rows)
            with self._bars_lock:
                bars = list(self._bars.values())
        if not bars:
            raise DataSourceTransientError(f"Binance 未返回数据: {self._symbol}")

        bars.sort(key=lambda b: b["ts_open"], reverse=True)
        out: list[KlineBar] = []
        for i, b in enumerate(bars[:n]):
            out.append(
                normalize_kline_bar(
                    KlineBar(
                        seq=i + 1,
                        ts_open=float(b["ts_open"]),
                        open=b["open"],
                        high=b["high"],
                        low=b["low"],
                        close=b["close"],
                        volume=b["volume"],
                        amount=b.get("amount", 0.0),
                        closed=bool(b.get("closed", i != 0)),
                    )
                )
            )
        return out

    def _maybe_rest_refresh_if_stale(self) -> None:
        """WS 推送停滞（断线/被墙）时用 REST 刷新，保证数据不断流."""
        from pa_agent.data.bar_close_wait import timeframe_to_seconds

        tf_s = timeframe_to_seconds(self._timeframe) or 60
        now = time.monotonic()
        with self._bars_lock:
            newest = max((b["ts_open"] for b in self._bars.values()), default=0.0)
        ws_alive = (now - self._last_ws_msg_mono) < max(30.0, tf_s * 3)
        newest_fresh = newest >= (time.time() * 1000 - tf_s * 3000 - 2000)
        if ws_alive and newest_fresh:
            return
        self._maybe_restart_ws()
        try:
            rows = self._rest_fetch_rows(self._symbol, self._timeframe, 400)
            self._store_rows(rows)
        except DataSourceTransientError as exc:
            if not self._bars:
                raise
            logger.debug("Binance stale REST refresh failed: %s", exc)

    def _maybe_restart_ws(self) -> None:
        """WS 已死（任务结束且无推送）时低频重启；运行中则交给 SDK 自动重连."""
        fut = self._ws_task
        if fut is not None and not fut.done():
            return
        now = time.monotonic()
        if now - self._last_ws_attempt_mono < 60.0:
            return
        self._last_ws_attempt_mono = now
        logger.info("Binance WS 已停止，尝试重启实时流…")
        try:
            self._start_ws_stream(wait_ready=False)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Binance WS 重启失败: %s", exc)

    # ── REST ─────────────────────────────────────────────────────────────────

    def _rest_fetch_rows(self, symbol: str, interval: str, limit: int) -> list[Any]:
        """带域名/代理路由记忆的 REST klines；返回原始数组行（旧→新）."""
        from binance_common.configuration import ConfigurationRestAPI
        from binance_sdk_spot.spot import Spot

        limit = min(max(limit, 10), 1000)
        routes: list[tuple[str, dict | None]] = []
        if self._rest_route:
            routes.append(self._rest_route)
        pairs: list[tuple[str, dict | None]] = [(b, None) for b in _REST_BASES]
        if self._proxy:
            pairs += [(b, self._proxy) for b in _REST_BASES]
        for p in pairs:
            if p not in routes:
                routes.append(p)

        last_err: Exception | None = None
        for base, proxy in routes:
            try:
                cfg = ConfigurationRestAPI(
                    base_path=base, timeout=8000, retries=0, proxy=proxy
                )
                client = Spot(config_rest_api=cfg)
                resp = client.rest_api.klines(
                    symbol=symbol, interval=interval, limit=limit
                )
                rows = resp.data()
                if rows:
                    self._rest_route = (base, proxy)
                    return list(rows)
                last_err = DataSourceTransientError(
                    f"Binance 未返回数据: {symbol} {interval}"
                )
            except DataSourceTransientError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.debug("Binance REST %s proxy=%s failed: %s", base, proxy, exc)
        raise DataSourceTransientError(f"Binance REST 拉取失败: {last_err}")

    def _store_rows(self, rows: list[Any]) -> None:
        """REST 原始行（旧→新，最后一行为 forming）合并进缓存."""
        with self._bars_lock:
            for i, row in enumerate(rows):
                try:
                    ts = int(row[0])
                    bar = {
                        "ts_open": ts,
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                        "amount": float(row[7]) if len(row) > 7 else 0.0,
                        "closed": i < len(rows) - 1,
                    }
                except (TypeError, ValueError, IndexError) as exc:
                    logger.debug("Binance REST row skip: %r (%s)", row, exc)
                    continue
                # WS 已推送过实时值的 bar 不用 REST 旧数据覆盖
                if ts in self._bars and self._last_ws_msg_mono > 0:
                    continue
                self._bars[ts] = bar
            self._trim_cache_locked()

    def _trim_cache_locked(self) -> None:
        if len(self._bars) <= _MAX_CACHE_BARS:
            return
        keep_from = sorted(self._bars.keys(), reverse=True)[:_MAX_CACHE_BARS][-1]
        for ts in [t for t in self._bars if t < keep_from]:
            self._bars.pop(ts, None)

    # ── WebSocket ────────────────────────────────────────────────────────────

    def _start_ws_stream(self, *, wait_ready: bool = True) -> None:
        self._ensure_ws_loop()
        assert self._ws_loop is not None
        self._ws_ready.clear()
        self._last_ws_attempt_mono = time.monotonic()
        self._ws_stop_evt = asyncio.Event()
        fut = asyncio.run_coroutine_threadsafe(
            self._ws_run(self._symbol, self._timeframe), self._ws_loop
        )
        self._ws_task = fut  # run_coroutine_threadsafe future; 取消用其 cancel()
        if wait_ready and not self._ws_ready.wait(_WS_READY_WAIT_S):
            logger.warning(
                "Binance WS %s %s 在 %.0fs 内未就绪，REST 兜底生效",
                self._symbol, self._timeframe, _WS_READY_WAIT_S,
            )

    def _stop_ws_stream(self) -> None:
        loop = self._ws_loop
        evt = self._ws_stop_evt
        fut = self._ws_task
        self._ws_task = None
        if loop is not None and loop.is_running():
            if evt is not None:
                loop.call_soon_threadsafe(evt.set)
            if fut is not None:
                # 等 _ws_run 在 finally 里完成 unsubscribe / close_connection，
                # 超时才强制 cancel（否则 aiohttp session 泄漏）
                try:
                    fut.result(timeout=2.5)
                except Exception:  # noqa: BLE001
                    fut.cancel()
        self._ws_ready.clear()

    async def _ws_run(self, symbol: str, interval: str) -> None:
        """在专用 loop 上尝试各 (url, 路由) 组合，成功后由 SDK 自动重连保活."""
        from binance_common.configuration import ConfigurationWebSocketStreams
        from binance_sdk_spot.spot import Spot

        stop = self._ws_stop_evt
        sym_lower = symbol.lower()
        attempts: list[tuple[str, dict | None]] = []
        if self._proxy:
            attempts += [(u, self._proxy) for u in _WS_URLS]
        attempts += [(u, None) for u in _WS_URLS]

        conn = None
        client = None
        stream = None
        try:
            for url, proxy in attempts:
                if stop.is_set():
                    return
                try:
                    cfg = ConfigurationWebSocketStreams(
                        stream_url=url,
                        reconnect_delay=3000,
                        reconnect_attempts=8,
                        proxy=proxy,
                    )
                    client = Spot(config_ws_streams=cfg)
                    conn = await asyncio.wait_for(
                        client.websocket_streams.create_connection(),
                        _WS_CONNECT_TIMEOUT_S,
                    )
                    stream = await conn.kline(symbol=sym_lower, interval=interval)
                    stream.on("message", self._on_ws_message)
                    logger.info(
                        "Binance WS 已连接 %s（proxy=%s）",
                        url, f"{proxy['host']}:{proxy['port']}" if proxy else "无",
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Binance WS %s proxy=%s 失败: %s", url, proxy, exc)
                    conn = None
                    client = None
                    continue
            else:
                logger.warning("Binance WS 所有路由均失败，REST 兜底生效")
                return

            await stop.wait()
        finally:
            if stream is not None:
                try:
                    await stream.unsubscribe()
                except Exception:  # noqa: BLE001
                    pass
            if conn is not None:
                try:
                    await conn.close_connection(close_session=True)
                except Exception:  # noqa: BLE001
                    pass

    def _on_ws_message(self, data: Any) -> None:
        """kline 推送（SDK 解析为 KlineResponse 模型或 dict）→ 更新缓存."""
        try:
            k = getattr(data, "k", None)
            if k is None and isinstance(data, dict):
                k = data.get("k")
            if k is None:
                return
            if isinstance(k, dict):
                def get(key: str, default: Any = None) -> Any:
                    return k.get(key, default)  # noqa: E501
            else:
                def get(key: str, default: Any = None) -> Any:
                    return getattr(k, key, default)
            ts = int(get("t") or 0)
            if not ts:
                return
            bar = {
                "ts_open": ts,
                "open": float(get("o") or 0.0),
                "high": float(get("h") or 0.0),
                "low": float(get("l") or 0.0),
                "close": float(get("c") or 0.0),
                "volume": float(get("v") or 0.0),
                "amount": float(get("q") or 0.0),
                "closed": bool(get("x")),
            }
            if bar["open"] <= 0 or bar["close"] <= 0:
                return
            with self._bars_lock:
                self._bars[ts] = bar
                self._trim_cache_locked()
            self._last_ws_msg_mono = time.monotonic()
            self._ws_ready.set()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Binance WS message 处理失败: %s", exc)

    # ── WS 线程管理 ──────────────────────────────────────────────────────────

    def _ensure_ws_loop(self) -> None:
        if self._ws_thread is not None and self._ws_thread.is_alive():
            return

        def _run(loop: asyncio.AbstractEventLoop) -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        self._ws_loop = asyncio.new_event_loop()
        self._ws_thread = threading.Thread(
            target=_run, args=(self._ws_loop,), name="binance-ws", daemon=True
        )
        self._ws_thread.start()

    def _shutdown_ws_loop(self) -> None:
        loop, thread = self._ws_loop, self._ws_thread
        self._ws_loop, self._ws_thread = None, None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=3.0)
        if loop is not None:
            try:
                loop.close()
            except RuntimeError:
                pass
