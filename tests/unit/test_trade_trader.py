"""BinanceTrader 单测（fake SDK 客户端注入，无网络）+ SecretStore + settings 加密往返."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pa_agent.config.settings import Settings
from pa_agent.trade.binance_trader import BinanceTrader
from pa_agent.trade.errors import TradeError
from pa_agent.trade.executions import _sanitize
from pa_agent.trade.order_plan import FUTURES, SPOT, build_order_plan, SymbolFilters


# ── fake SDK ──────────────────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, data):
        self._data = data

    def data(self):
        return self._data


class _FakeSpotClient:
    """记录调用参数并返回固定回执的现货 fake."""

    def __init__(self):
        self.calls: list[tuple] = []

    def time(self):
        self.calls.append(("time", {}))
        return _FakeResp({"serverTime": int(__import__("time").time() * 1000)})

    def get_order(self, symbol=None, order_id=None):
        self.calls.append(("get_order", {"symbol": symbol, "order_id": order_id}))
        return _FakeResp({"orderId": order_id, "status": "FILLED",
                          "executedQty": "0.5", "avgPrice": "101"})

    def new_order(self, **kw):
        self.calls.append(("new_order", kw))
        return _FakeResp({
            "orderId": 111, "symbol": kw.get("symbol", ""),
            "side": kw.get("side"), "type": kw.get("type"),
            "status": "FILLED", "executedQty": kw.get("quantity") or 0.001,
            "cummulativeQuoteQty": kw.get("quote_order_qty") or 100.0,
            "clientOrderId": "c-1", "orderListId": -1,
        })

    def order_oco(self, **kw):
        self.calls.append(("order_oco", kw))
        return _FakeResp({
            "orderListId": 222, "contingencyType": "OCO",
            "orders": [{"orderId": 1}, {"orderId": 2}],
        })

    def get_account(self, omit_zero_balances=None):
        self.calls.append(("get_account", {"omit": omit_zero_balances}))
        return _FakeResp({
            "balances": [
                {"asset": "BTC", "free": "0.5", "locked": "0"},
                {"asset": "ETH", "free": "0", "locked": "0"},
                {"asset": "USDT", "free": "1000", "locked": "5"},
            ]
        })

    def get_open_orders(self, symbol=None):
        return _FakeResp([{"orderId": 9, "symbol": symbol, "side": "SELL",
                           "type": "LIMIT", "price": "105", "origQty": "1",
                           "stopPrice": "0", "status": "NEW", "reduceOnly": False}])

    def delete_order(self, **kw):
        self.calls.append(("delete_order", kw))
        return _FakeResp({"orderId": kw.get("order_id")})

    def delete_open_orders(self, symbol=None):
        self.calls.append(("delete_open_orders", {"symbol": symbol}))
        return _FakeResp({"code": 0})

    def exchange_info(self, symbol=None):
        return _FakeResp({"symbols": [{"symbol": symbol, "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ]}]})


class _FakeFutClient(_FakeSpotClient):
    def check_server_time(self):
        self.calls.append(("check_server_time", {}))
        return _FakeResp({"serverTime": int(__import__("time").time() * 1000)})

    def exchange_information(self):
        """真实 futures SDK：无参、全量列表（不含 BTCUSDT 时应报交易对不存在）."""
        self.calls.append(("exchange_information", {}))
        return _FakeResp({"symbols": [
            {"symbol": "ETHUSDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
            ]},
            {"symbol": "BTCUSDT", "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.1"},
                {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "100"},
            ]},
        ]})

    def cancel_order(self, symbol=None, order_id=None, **kw):
        self.calls.append(("fut_cancel_order",
                           {"symbol": symbol, "order_id": order_id}))
        return _FakeResp({"orderId": order_id})

    def cancel_algo_order(self, algo_id=None, client_algo_id=None, recv_window=None):
        # 真实签名无 symbol（内省核实）；多传 symbol 会被本 fake 显式炸掉以暴露错配
        self.calls.append(("cancel_algo_order",
                           {"algo_id": algo_id, "client_algo_id": client_algo_id}))
        return _FakeResp({"algoId": algo_id})

    def cancel_all_open_orders(self, symbol=None):
        self.calls.append(("cancel_all_open_orders", {"symbol": symbol}))
        return _FakeResp({"code": 0})

    def cancel_all_algo_open_orders(self, symbol=None):
        self.calls.append(("cancel_all_algo_open_orders", {"symbol": symbol}))
        return _FakeResp({"code": 0})

    def change_position_mode(self, dual_side_position=None):
        self.calls.append(("change_position_mode", {"dual": dual_side_position}))
        assert isinstance(dual_side_position, str), \
            "真实 SDK 参数为 Optional[str]，传 bool 会错"

    def change_initial_leverage(self, symbol=None, leverage=1):
        self.calls.append(("change_initial_leverage", {"symbol": symbol, "lev": leverage}))

    def new_algo_order(self, **kw):
        self.calls.append(("new_algo_order", kw))
        return _FakeResp({"algoId": 333})

    def current_all_open_orders(self, symbol=None):
        return _FakeResp([])

    def current_all_algo_open_orders(self, symbol=None):
        return _FakeResp({"orders": [{"algoId": 55, "symbol": symbol, "side": "SELL",
                                      "algoType": "CONDITIONAL", "triggerPrice": "95",
                                      "quantity": "1", "algoStatus": "NEW",
                                      "reduceOnly": True}]})

    def position_information_v3(self, symbol=None):
        return _FakeResp([{"symbol": symbol, "positionAmt": "2", "entryPrice": "100",
                           "unRealizedProfit": "5", "leverage": "5", "markPrice": "102.5"}])

    def account_information_v3(self):
        return _FakeResp({"assets": [{"asset": "USDT", "walletBalance": "1000",
                                      "availableBalance": "900"}]})

    def query_order(self, symbol=None, order_id=None):
        return _FakeResp({"orderId": order_id, "status": "FILLED",
                          "executedQty": "0.5", "avgPrice": "101"})


def _trader(market: str) -> tuple[BinanceTrader, _FakeSpotClient]:
    cfg = SimpleNamespace(
        api_key="k", api_secret="s", market=market, testnet=True,
        leverage=5, proxy="", stake_usdt=100.0,
    )
    t = BinanceTrader(cfg)
    t._proxy_resolved = True  # 跳过 TCP 探测
    fake = _FakeFutClient() if market == FUTURES else _FakeSpotClient()
    if market == FUTURES:
        t._fut_client = fake
    else:
        t._spot_client = fake
    return t, fake


_DECISION_LONG_LIMIT = {
    "order_type": "限价单", "order_direction": "做多",
    "entry_price": 100.0, "stop_loss_price": 95.0,
    "take_profit_price": 105.0, "take_profit_price_2": None,
}


def _plan_for(market: str):
    return build_order_plan(
        _DECISION_LONG_LIMIT, "BTCUSDT", market=market,
        stake_usdt=100.0, leverage=5,
        filters=SymbolFilters("BTCUSDT", 0.01, 0.001, 0.001, 5.0),
    )


# ── 入场 ──────────────────────────────────────────────────────────────────────

def test_spot_limit_entry_params():
    t, fake = _trader(SPOT)
    res = t.place_entry(_plan_for(SPOT))
    kind, kw = fake.calls[0]
    assert kind == "new_order"
    assert kw["type"] == "LIMIT" and kw["side"] == "BUY"
    assert kw["price"] == 100.0 and kw["time_in_force"] == "GTC"
    assert kw["recv_window"] == 10000
    assert res.order_id == 111 and res.status == "FILLED"


def test_futures_entry_sets_leverage_then_orders():
    t, fake = _trader(FUTURES)
    plan = _plan_for(FUTURES)
    res = t.place_entry(plan)
    kinds = [c[0] for c in fake.calls]
    # 杠杆/模式设置先于下单
    assert kinds.index("change_initial_leverage") < kinds.index("new_order")
    _, lev_kw = fake.calls[kinds.index("change_initial_leverage")]
    assert lev_kw["lev"] == 5
    _, kw = fake.calls[kinds.index("new_order")]
    assert kw["type"] == "LIMIT" and kw["quantity"] == plan.quantity


def test_missing_credentials_blocked():
    cfg = SimpleNamespace(api_key="", api_secret="", market=SPOT, testnet=True,
                          leverage=5, proxy="")
    t = BinanceTrader(cfg)
    with pytest.raises(TradeError, match="API Key"):
        t.place_entry(_plan_for(SPOT))


# ── 保护单 ────────────────────────────────────────────────────────────────────

def test_spot_protection_submits_oco():
    t, fake = _trader(SPOT)
    t.place_protection(_plan_for(SPOT), filled_qty=0.5)
    kind, kw = fake.calls[0]
    assert kind == "order_oco"
    assert kw["quantity"] == 0.5
    assert kw["price"] == 105.0 and kw["stop_price"] == 95.0
    assert kw["stop_limit_time_in_force"] == "GTC"


def test_futures_protection_submits_algo_orders():
    t, fake = _trader(FUTURES)
    plan = _plan_for(FUTURES)
    results = t.place_protection(plan, filled_qty=0.5)
    algo_calls = [kw for k, kw in fake.calls if k == "new_algo_order"]
    assert len(algo_calls) == 2  # TP + SL
    tp, sl = algo_calls
    assert tp["type"] == "TAKE_PROFIT_MARKET" and tp["reduce_only"] == "true"
    assert tp["trigger_price"] == 105.0 and tp["quantity"] == 0.5
    assert sl["type"] == "STOP_MARKET" and sl["close_position"] == "true"
    assert sl["trigger_price"] == 95.0
    assert len(results) == 2


# ── 查询/撤单/平仓 ────────────────────────────────────────────────────────────

def test_spot_balances_nonzero():
    t, _ = _trader(SPOT)
    bals = t.balances()
    assert {b["asset"] for b in bals} == {"BTC", "USDT"}
    usdt = next(b for b in bals if b["asset"] == "USDT")
    assert usdt["free"] == 1000 and usdt["locked"] == 5


def test_futures_positions_and_algo_orders():
    t, _ = _trader(FUTURES)
    pos = t.positions("BTCUSDT")
    assert len(pos) == 1 and pos[0]["direction"] == "做多"
    assert pos[0]["entry_price"] == 100.0
    orders = t.open_orders("BTCUSDT")
    assert any(o["algo"] for o in orders)
    t.cancel_order("BTCUSDT", 55, algo=True)


def test_futures_close_market():
    t, fake = _trader(FUTURES)
    res = t.close_position_market("BTCUSDT")
    kind, kw = fake.calls[-1]
    assert kind == "new_order"
    assert kw["side"] == "SELL" and kw["reduce_only"] == "true"
    assert kw["quantity"] == 2.0
    assert res.status == "FILLED"


def test_spot_close_market_sells_free_balance():
    t, fake = _trader(SPOT)
    res = t.close_position_market("BTCUSDT")
    kind, kw = fake.calls[-1]
    assert kind == "new_order" and kw["side"] == "SELL"
    assert kw["quantity"] == pytest.approx(0.5)  # fake 持仓 0.5 BTC


def test_query_order_uses_get_order_on_spot():
    """现货 SDK 无 query_order（Verifier 发现的错配）——必须走 get_order."""
    t, fake = _trader(SPOT)
    st = t.query_order("BTCUSDT", 42)
    kinds = [c[0] for c in fake.calls]
    assert "get_order" in kinds and "query_order" not in kinds
    assert st["status"] == "FILLED"


def test_query_order_uses_query_order_on_futures():
    t, fake = _trader(FUTURES)
    st = t.query_order("BTCUSDT", 42)
    assert st["executed_qty"] == 0.5


def test_verify_clock_market_specific_methods():
    t_spot, fake_spot = _trader(SPOT)
    t_spot.verify_clock()
    assert any(k == "time" for k, _ in fake_spot.calls)
    t_fut, fake_fut = _trader(FUTURES)
    t_fut.verify_clock()
    assert any(k == "check_server_time" for k, _ in fake_fut.calls)


# ── 错误传递 ──────────────────────────────────────────────────────────────────

def test_sdk_error_wrapped_as_trade_error():
    t, fake = _trader(SPOT)

    class _FakeBinanceError(Exception):
        def __init__(self, code, msg):
            super().__init__(msg)
            self.status_code = code
            self.error_message = msg

    def _raise(**kw):
        raise _FakeBinanceError(-2010, "insufficient balance")

    fake.new_order = _raise
    with pytest.raises(TradeError) as ei:
        t.place_entry(_plan_for(SPOT))
    assert "-2010" in ei.value.friendly_message or "余额" in ei.value.friendly_message
    assert ei.value.binance_code == -2010


# ── executions 脱敏 ───────────────────────────────────────────────────────────

def test_executions_sanitize_strips_secret():
    out = _sanitize({"api_secret": "abc", "note": "x", "nested": {"api_key": "k"}})
    assert out["api_secret"] == "***"
    assert out["nested"]["api_key"] == "***"
    assert out["note"] == "x"


# ── SecretStore + settings 往返 ───────────────────────────────────────────────

def test_secret_store_roundtrip():
    from pa_agent.security.secret_store import SecretStore

    ct = SecretStore.encrypt("my-bin-secret")
    assert ct != "my-bin-secret"
    assert SecretStore.decrypt(ct) == "my-bin-secret"


def test_settings_secret_encrypt_on_save(tmp_path):
    from pa_agent.config.settings import save_settings

    s = Settings()
    s.binance.api_key = "pub-key"
    s.binance.api_secret = "plain-secret"
    p = tmp_path / "settings.json"
    save_settings(s, p)
    text = p.read_text(encoding="utf-8")
    assert "plain-secret" not in text
    assert s.binance.api_secret_encrypted  # 已回写内存密文
    import json
    data = json.loads(text)
    assert data["binance"]["api_secret_encrypted"]
    assert "api_secret" not in data["binance"]


def test_settings_secret_decrypt_on_load(tmp_path):
    from pa_agent.config.settings import load_settings, save_settings

    s = Settings()
    s.binance.api_secret = "round-trip-secret"
    p = tmp_path / "settings.json"
    save_settings(s, p)
    loaded = load_settings(p)
    assert loaded.binance.api_secret == "round-trip-secret"


def test_settings_secret_decrypt_failure_blanks(tmp_path, monkeypatch):
    from pa_agent.config.settings import load_settings

    p = tmp_path / "settings.json"
    p.write_text('{"binance": {"api_secret_encrypted": "!!!invalid!!!"}}', encoding="utf-8")
    loaded = load_settings(p)
    assert loaded.binance.api_secret == ""


# ── 第2轮 Verifier 发现的 futures SDK 错配：防回归 ───────────────────────────

def test_symbol_filters_futures_uses_exchange_information_and_filters():
    """futures 无参 exchange_information() 全量返回 → 必须客户端按 symbol 过滤."""
    t, fake = _trader(FUTURES)
    f = t.symbol_filters("BTCUSDT")
    kinds = [c[0] for c in fake.calls]
    assert "exchange_information" in kinds
    assert "exchange_info" not in kinds  # futures 不存在该方法
    assert f.tick_size == 0.1 and f.step_size == 0.001 and f.min_notional == 100.0


def test_symbol_filters_futures_unknown_symbol_raises():
    from pa_agent.trade.errors import TradeError as TE

    t, _ = _trader(FUTURES)
    try:
        t.symbol_filters("NOSUCHUSDT")
        raised = False
    except TE as exc:
        raised = "不存在" in str(exc)
    assert raised, "未知交易对应报 TradeError"


def test_cancel_algo_futures_no_symbol_kwarg():
    """futures cancel_algo_order 签名无 symbol；多传会被 fake 显式暴露."""
    t, fake = _trader(FUTURES)
    t.cancel_order("BTCUSDT", 55, algo=True)
    for kind, kw in fake.calls:
        if kind == "cancel_algo_order":
            assert "symbol" not in kw


def test_cancel_all_futures_calls_both_and_propagates_failure():
    t, fake = _trader(FUTURES)
    n = t.cancel_all("BTCUSDT")
    assert n == 2  # 普通 + algo
    kinds = [c[0] for c in fake.calls]
    assert "cancel_all_open_orders" in kinds and "cancel_all_algo_open_orders" in kinds

    def _boom(symbol=None):
        raise RuntimeError("network down")

    fake.cancel_all_algo_open_orders = _boom
    import pytest as _pytest

    with _pytest.raises(Exception):
        t.cancel_all("BTCUSDT")  # 失败必须上抛（不再吞掉恒返回成功）


def test_position_mode_receives_string():
    t, fake = _trader(FUTURES)
    t.ensure_futures_settings("BTCUSDT", 5)
    for kind, kw in fake.calls:
        if kind == "change_position_mode":
            assert kw["dual"] == "false"
