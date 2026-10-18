"""Binance 交易设置对话框.

配置 API Key/Secret、市场（现货/USDT-M 合约）、合约杠杆、Testnet/实盘、
下单金额与上限、总开关。Secret 经 SecretStore（DPAPI）加密落盘。
切实盘需红色风险警告确认。
"""
from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pa_agent.config.paths import SETTINGS_JSON_PATH
from pa_agent.config.settings import Settings, save_settings

logger = logging.getLogger(__name__)


class BinanceTradeSettingsDialog(QDialog):
    """Binance 交易配置模态对话框."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._live_pending_confirm = False
        self.setWindowTitle("Binance 交易设置")
        self.setMinimumWidth(560)
        self._setup_ui()
        self._load_values()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(12)

        self._enabled_check = QCheckBox("启用交易功能（总开关；关闭时不下任何真实订单）")
        self._enabled_check.setToolTip(
            "默认关闭。开启前需填写 API Key/Secret；建议先在 Testnet 验证完整流程。"
        )
        root.addWidget(self._enabled_check)

        # ── API 凭据 ──────────────────────────────────────────────────────────
        cred_box = QGroupBox("API 凭据")
        cred_form = QFormLayout(cred_box)

        self._api_key_edit = QLineEdit()
        self._api_key_edit.setPlaceholderText("Binance API Key（现货/合约权限）")
        cred_form.addRow("API Key:", self._api_key_edit)

        secret_row = QHBoxLayout()
        self._api_secret_edit = QLineEdit()
        self._api_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_secret_edit.setPlaceholderText("留空 = 不修改已保存的 Secret")
        self._secret_visible_btn = QPushButton("显示")
        self._secret_visible_btn.setCheckable(True)
        self._secret_visible_btn.setFixedWidth(56)
        self._secret_visible_btn.toggled.connect(self._toggle_secret_visibility)
        secret_row.addWidget(self._api_secret_edit, stretch=1)
        secret_row.addWidget(self._secret_visible_btn)
        cred_form.addRow("API Secret:", secret_row)

        hint = QLabel(
            "Secret 使用 Windows DPAPI 加密存储于本机 settings.json，不会明文落盘。\n"
            "Testnet Key 申请：testnet.binance.vision"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        cred_form.addRow(hint)
        root.addWidget(cred_box)

        # ── 市场与环境 ────────────────────────────────────────────────────────
        env_box = QGroupBox("市场与环境")
        env_form = QFormLayout(env_box)

        self._market_combo = QComboBox()
        self._market_combo.addItem("现货（Spot）", "spot")
        self._market_combo.addItem("USDT-M 永续合约（可做空）", "futures")
        self._market_combo.setToolTip(
            "合约模式下 K 线行情自动切换为合约数据；做空仅合约可用。"
        )
        env_form.addRow("交易市场:", self._market_combo)

        self._leverage_spin = QDoubleSpinBox()
        self._leverage_spin.setRange(1, 20)
        self._leverage_spin.setDecimals(0)
        self._leverage_spin.setSuffix(" x")
        self._leverage_spin.setToolTip("下单前自动调整到该杠杆（单向持仓）。")
        env_form.addRow("合约杠杆:", self._leverage_spin)

        self._testnet_check = QCheckBox("使用 Testnet 测试网（模拟资金）")
        self._testnet_check.setToolTip("强烈建议先在 Testnet 完整验证后再切实盘。")
        self._testnet_check.toggled.connect(self._on_testnet_toggled)
        env_form.addRow(self._testnet_check)
        root.addWidget(env_box)

        # ── 下单金额 ──────────────────────────────────────────────────────────
        stake_box = QGroupBox("下单金额（USDT）")
        stake_form = QFormLayout(stake_box)
        self._stake_spin = QDoubleSpinBox()
        self._stake_spin.setRange(1, 1_000_000)
        self._stake_spin.setDecimals(2)
        stake_form.addRow("每笔金额:", self._stake_spin)
        self._max_stake_spin = QDoubleSpinBox()
        self._max_stake_spin.setRange(1, 1_000_000)
        self._max_stake_spin.setDecimals(2)
        stake_form.addRow("金额上限:", self._max_stake_spin)
        root.addWidget(stake_box)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ── 显隐切换 ─────────────────────────────────────────────────────────────

    def _toggle_secret_visibility(self, checked: bool) -> None:
        self._api_secret_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self._secret_visible_btn.setText("隐藏" if checked else "显示")

    # ── 实盘警告 ─────────────────────────────────────────────────────────────

    def _on_testnet_toggled(self, checked: bool) -> None:
        # checked=True 表示勾选 Testnet；取消勾选 = 切实盘
        if not checked and self._settings.binance.testnet:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("⚠ 切换到实盘")
            # QMessageBox.setText 支持富文本：正文直接以红色呈现风险提示
            box.setText(
                "<p style='color:#d32f2f;font-weight:bold;font-size:14px'>"
                "即将切换到 Binance 实盘环境！</p>"
                "<p>之后的每一笔订单都将以<b>真实资金</b>执行，可能造成实际资金损失。<br>"
                "请确认你已理解风险并已在 Testnet 完成验证。</p>"
            )
            yes = box.addButton("我已知晓风险，切实盘", QMessageBox.ButtonRole.AcceptRole)
            no = box.addButton("保持 Testnet", QMessageBox.ButtonRole.RejectRole)
            box.exec()
            if box.clickedButton() is no:
                self._testnet_check.blockSignals(True)
                self._testnet_check.setChecked(True)
                self._testnet_check.blockSignals(False)

    # ── 读写 ─────────────────────────────────────────────────────────────────

    def _load_values(self) -> None:
        b = self._settings.binance
        self._enabled_check.setChecked(bool(b.trade_enabled))
        self._api_key_edit.setText(b.api_key or "")
        idx = self._market_combo.findData(b.market)
        self._market_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._leverage_spin.setValue(int(b.leverage))
        self._testnet_check.blockSignals(True)
        self._testnet_check.setChecked(bool(b.testnet))
        self._testnet_check.blockSignals(False)
        self._stake_spin.setValue(float(b.stake_usdt))
        self._max_stake_spin.setValue(float(b.max_stake_usdt))

    def _on_save(self) -> None:
        b = self._settings.binance
        enabled = self._enabled_check.isChecked()
        key = self._api_key_edit.text().strip()
        secret_plain = self._api_secret_edit.text().strip()

        if enabled:
            existing_secret = bool(b.api_secret or b.api_secret_encrypted)
            if not key or not (existing_secret or secret_plain):
                QMessageBox.warning(
                    self, "缺少凭据",
                    "启用交易前请填写 API Key 与 API Secret。",
                )
                return
        stake = float(self._stake_spin.value())
        max_stake = float(self._max_stake_spin.value())
        if stake > max_stake:
            QMessageBox.warning(
                self, "金额超限",
                f"每笔金额 {stake:.2f} 超过金额上限 {max_stake:.2f}，请先调整上限。",
            )
            return

        b.trade_enabled = enabled
        b.api_key = key
        if secret_plain:
            b.api_secret = secret_plain  # save_settings 时加密落盘；留空=不变更
        b.market = self._market_combo.currentData()
        b.leverage = int(self._leverage_spin.value())
        b.testnet = self._testnet_check.isChecked()
        b.stake_usdt = stake
        b.max_stake_usdt = max_stake
        try:
            save_settings(self._settings, SETTINGS_JSON_PATH)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", f"写入 settings.json 失败：{exc}")
            return
        self.accept()
