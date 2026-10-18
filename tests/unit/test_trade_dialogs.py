"""交易 GUI 单测（pytest-qt，fake trader，无网络）."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QDialog, QMessageBox

from pa_agent.config.settings import Settings
from pa_agent.gui.binance_trade_settings_dialog import BinanceTradeSettingsDialog
from pa_agent.gui.decision_panel import DecisionPanel
from pa_agent.gui.order_confirm_dialog import OrderConfirmDialog
from pa_agent.trade.order_plan import FUTURES, SPOT, build_order_plan, SymbolFilters


# ── OrderConfirmDialog ────────────────────────────────────────────────────────

_FILTERS = SymbolFilters("BTCUSDT", 0.01, 0.001, 0.001, 5.0)


def _plan(market=SPOT, tp2=None):
    return build_order_plan(
        {
            "order_type": "市价单", "order_direction": "做多",
            "entry_price": None, "stop_loss_price": 95.0,
            "take_profit_price": 105.0, "take_profit_price_2": tp2,
        },
        "BTCUSDT", market=market, stake_usdt=100.0, leverage=5,
        filters=_FILTERS, last_price=100.0,
    )


def test_confirm_dialog_reject_returns_none(qtbot):
    dlg = OrderConfirmDialog(_plan(), testnet=True, max_stake=500.0)
    qtbot.addWidget(dlg)
    dlg.reject()
    assert dlg.final_stake is None


def test_confirm_dialog_ok_sets_stake(qtbot):
    dlg = OrderConfirmDialog(_plan(), testnet=True, max_stake=500.0)
    qtbot.addWidget(dlg)
    dlg._stake_spin.setValue(123.0)
    dlg._on_confirm()
    assert dlg.final_stake == 123.0


def test_confirm_dialog_stake_capped(qtbot):
    dlg = OrderConfirmDialog(_plan(), testnet=True, max_stake=500.0)
    qtbot.addWidget(dlg)
    assert dlg._stake_spin.maximum() == 500.0


def test_confirm_dialog_tp_split_labels(qtbot):
    plan = _plan(tp2=110.0)
    dlg = OrderConfirmDialog(plan, testnet=False, max_stake=500.0)
    qtbot.addWidget(dlg)
    # 收集全部标签文本：TP 分批说明与实盘徽标必须呈现
    from PyQt6.QtWidgets import QLabel

    all_text = " ".join(l.text() for l in dlg.findChildren(QLabel))
    assert "TP1" in all_text and "出一半" in all_text
    assert "TP2" in all_text and "出另一半" in all_text
    assert "实盘" in all_text


# ── DecisionPanel 执行按钮 ────────────────────────────────────────────────────

_ORDER_DECISION = {
    "order_type": "市价单", "order_direction": "做多",
    "entry_price": None, "stop_loss_price": 95.0,
    "take_profit_price": 105.0, "take_profit_price_2": None,
    "trade_confidence": 80, "diagnosis_confidence": 60,
    "reasoning": "test",
}


def test_execute_button_gated(qtbot):
    p = DecisionPanel()
    qtbot.addWidget(p)
    p.set_decision(_ORDER_DECISION)
    # gate 关 → 不可见
    p.set_execute_enabled(False)
    assert p._execute_order_btn.isVisibleTo(p) is False
    # gate 开 + 有订单 → 可见
    p.set_execute_enabled(True)
    assert p._execute_order_btn.isVisibleTo(p) is True


def test_execute_button_hidden_for_no_order(qtbot):
    p = DecisionPanel()
    qtbot.addWidget(p)
    p.set_execute_enabled(True)
    p.set_decision({**_ORDER_DECISION, "order_type": "不下单",
                    "order_direction": None})
    assert p._execute_order_btn.isVisibleTo(p) is False


def test_execute_button_emits_decision(qtbot):
    p = DecisionPanel()
    qtbot.addWidget(p)
    p.set_decision(_ORDER_DECISION)
    p.set_execute_enabled(True)
    received: list[dict] = []
    p.order_execute_requested.connect(received.append)
    p._execute_order_btn.click()
    assert received and received[0]["order_type"] == "市价单"


# ── BinanceTradeSettingsDialog ────────────────────────────────────────────────

def test_settings_dialog_roundtrip(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "pa_agent.gui.binance_trade_settings_dialog.SETTINGS_JSON_PATH",
        tmp_path / "settings.json",
    )
    s = Settings()
    dlg = BinanceTradeSettingsDialog(s)
    qtbot.addWidget(dlg)
    dlg._enabled_check.setChecked(True)
    dlg._api_key_edit.setText("test-key")
    dlg._api_secret_edit.setText("test-secret")
    dlg._market_combo.setCurrentIndex(1)  # futures
    dlg._leverage_spin.setValue(10)
    dlg._stake_spin.setValue(50.0)
    dlg._on_save()
    assert s.binance.trade_enabled is True
    assert s.binance.api_key == "test-key"
    assert s.binance.api_secret == "test-secret"
    assert s.binance.market == "futures"
    assert s.binance.leverage == 10
    # 落盘无明文
    text = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert "test-secret" not in text


def test_settings_dialog_requires_credentials_when_enabling(qtbot, monkeypatch):
    shown: list[tuple] = []
    monkeypatch.setattr(
        QMessageBox, "warning", staticmethod(lambda *a, **k: shown.append(a)))
    s = Settings()
    dlg = BinanceTradeSettingsDialog(s)
    qtbot.addWidget(dlg)
    dlg._enabled_check.setChecked(True)  # 未填 Key/Secret
    dlg._on_save()
    assert shown, "启用交易缺少凭据时应弹警告且不保存"
    assert s.binance.trade_enabled is False


def test_settings_dialog_rejects_stake_over_max(qtbot):
    from PyQt6.QtWidgets import QMessageBox as QMB

    shown: list[tuple] = []
    # 仅拦截本用例的警告弹窗
    import pa_agent.gui.binance_trade_settings_dialog as dlg_mod

    orig = dlg_mod.QMessageBox.warning

    def _warn(*a, **k):
        shown.append(a)
        return None

    dlg_mod.QMessageBox.warning = staticmethod(_warn)
    try:
        s = Settings()
        dlg = BinanceTradeSettingsDialog(s)
        qtbot.addWidget(dlg)
        dlg._enabled_check.setChecked(False)  # 不触发凭据校验
        dlg._stake_spin.setValue(500.0)
        dlg._max_stake_spin.setValue(100.0)
        dlg._on_save()
        assert shown, "金额超上限应弹警告且不保存"
        assert s.binance.max_stake_usdt != 100.0 or s.binance.stake_usdt == 100.0
    finally:
        dlg_mod.QMessageBox.warning = orig


def test_settings_dialog_live_toggle_keeps_testnet_on_cancel(qtbot, monkeypatch):

    class _FakeBtn:
        def __init__(self, label):
            self.label = label

    class _FakeBox:
        Icon = SimpleNamespace(Warning=1)
        ButtonRole = SimpleNamespace(AcceptRole=1, RejectRole=2)

        def __init__(self, parent=None):
            self._buttons = {}

        def setIcon(self, *_): pass
        def setWindowTitle(self, *_): pass
        def setText(self, *_): pass
        def addButton(self, label, role):
            btn = _FakeBtn(label)
            self._buttons[label] = btn
            return btn

        def exec(self):
            return 0

        def clickedButton(self):
            return self._buttons.get("保持 Testnet")  # 模拟用户点「保持 Testnet」

    monkeypatch.setattr(
        "pa_agent.gui.binance_trade_settings_dialog.QMessageBox", _FakeBox)
    s = Settings()
    dlg = BinanceTradeSettingsDialog(s)
    qtbot.addWidget(dlg)
    dlg._testnet_check.setChecked(False)  # 触发切实盘警告
    assert dlg._testnet_check.isChecked() is True  # 未确认 → 自动回勾 Testnet


# ── TradePanel（fake trader 驱动渲染） ────────────────────────────────────────

def test_trade_panel_renders_balances_and_orders(qtbot):
    from pa_agent.gui.trade_panel import TradePanel

    class _FakeTrader:
        market = "futures"
        testnet = True

        def balances(self, market=None):
            return [{"asset": "USDT", "free": 1000.0, "locked": 5.0}]

        def positions(self, symbol):
            return [{
                "symbol": symbol, "amount": 2.0, "direction": "做多",
                "entry_price": 100.0, "mark_price": 102.5,
                "unrealized_pnl": 5.0, "leverage": "5",
            }]

        def open_orders(self, symbol):
            return [{
                "order_id": 55, "symbol": symbol, "side": "SELL",
                "type": "STOP_MARKET", "price": 0.0, "orig_qty": 2.0,
                "stop_price": 95.0, "status": "NEW",
                "reduce_only": True, "algo": True,
            }]

    panel = TradePanel(_FakeTrader(), "BTCUSDT", market="futures", testnet=True)
    qtbot.addWidget(panel)
    qtbot.waitUntil(lambda: panel._balance_table.rowCount() == 1, timeout=5000)
    qtbot.waitUntil(lambda: panel._position_table.rowCount() == 1, timeout=5000)
    qtbot.waitUntil(lambda: panel._orders_table.rowCount() == 1, timeout=5000)
    assert panel._balance_table.item(0, 0).text() == "USDT"
    assert panel._position_table.item(0, 0).text() == "做多"
    assert panel._orders_table.item(0, 0).text().startswith("A55")
    # 等 QThread 完全退出再关闭，避免 teardown 与线程竞争
    qtbot.waitUntil(
        lambda: panel._query is not None and not panel._query.isRunning(),
        timeout=5000,
    )
    panel.close()


def test_trade_panel_spot_hides_position_tab(qtbot):
    from pa_agent.gui.trade_panel import TradePanel

    class _FakeTrader:
        market = "spot"
        testnet = True

        def balances(self, market=None):
            return []
        def positions(self, symbol):
            return []
        def open_orders(self, symbol):
            return []

    panel = TradePanel(_FakeTrader(), "BTCUSDT", market="spot", testnet=True)
    qtbot.addWidget(panel)
    assert panel._tabs.isTabVisible(panel._pos_tab_idx) is False
    qtbot.waitUntil(
        lambda: panel._query is not None and not panel._query.isRunning(),
        timeout=5000,
    )
    panel.close()
