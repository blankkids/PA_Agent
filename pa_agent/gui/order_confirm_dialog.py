"""下单确认对话框：展示完整订单明细，金额可微调（受上限），确认后才执行."""
from __future__ import annotations

import logging

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from pa_agent.trade.order_plan import FUTURES, OrderPlan

logger = logging.getLogger(__name__)


def _badge(text: str, color: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"background:{color};color:white;border-radius:6px;"
        "padding:2px 10px;font-weight:bold;font-size:12px;"
    )
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


class OrderConfirmDialog(QDialog):
    """人工确认下单；返回最终金额（stake）或 None（取消）."""

    def __init__(
        self,
        plan: OrderPlan,
        *,
        testnet: bool,
        max_stake: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._plan = plan
        self._max_stake = float(max_stake)
        self.setWindowTitle("确认下单")
        self.setMinimumWidth(480)
        self._setup_ui(testnet)
        self.final_stake: float | None = None

    def _setup_ui(self, testnet: bool) -> None:
        p = self._plan
        root = QVBoxLayout(self)
        root.setSpacing(10)

        # 徽标行：市场 + 环境
        badges = QHBoxLayout()
        badges.addWidget(_badge("USDT-M 合约" if p.market == FUTURES else "现货",
                                "#7b1fa2" if p.market == FUTURES else "#1565c0"))
        badges.addWidget(_badge("Testnet 模拟" if testnet else "⚠ 实盘 真实资金",
                                "#2e7d32" if testnet else "#d32f2f"))
        badges.addStretch(1)
        root.addLayout(badges)

        title = QLabel(
            f"<b style='font-size:15px'>{p.symbol} · {p.direction} · "
            f"{'市价' if p.order_kind == 'market' else '限价'}入场</b>"
        )
        root.addWidget(title)

        box = QGroupBox("订单明细")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        direction_color = "#2e7d32" if p.direction == "做多" else "#d32f2f"
        d_lbl = QLabel(f"<span style='color:{direction_color};font-weight:bold'>"
                       f"{p.direction}（{'BUY' if p.side == 'BUY' else 'SELL'}）</span>")
        form.addRow("方向:", d_lbl)
        form.addRow("入场:", QLabel(
            "市价（按金额成交）" if p.order_kind == "market" else f"{p.entry_price:.6g}（限价挂单）"
        ))
        form.addRow("数量(估):", QLabel(f"{p.quantity:.8g}"))
        if p.market == FUTURES:
            form.addRow("杠杆:", QLabel(f"{p.leverage} x（名义 {p.stake_usdt * p.leverage:.2f} USDT）"))
        form.addRow("止损 SL:", QLabel(f"<span style='color:#d32f2f'>{p.stop_loss:.6g}</span>"))
        tp1_txt = f"<span style='color:#2e7d32'>{p.take_profit_1:.6g}</span>"
        if p.tp_split:
            tp1_txt += "（出一半）"
        form.addRow("止盈 TP1:", QLabel(tp1_txt))
        if p.tp_split and p.take_profit_2:
            form.addRow("止盈 TP2:", QLabel(
                f"<span style='color:#2e7d32'>{p.take_profit_2:.6g}</span>（出另一半）"))
        else:
            form.addRow("TP2:", QLabel("未设置（TP1 全部止盈）"))
        root.addWidget(box)

        stake_box = QGroupBox("本笔金额（USDT）")
        stake_form = QFormLayout(stake_box)
        self._stake_spin = QDoubleSpinBox()
        self._stake_spin.setRange(1, self._max_stake)
        self._stake_spin.setDecimals(2)
        self._stake_spin.setValue(float(p.stake_usdt))
        self._stake_spin.setToolTip(f"上限 {self._max_stake:.2f} USDT（可在交易设置中调整）")
        stake_form.addRow("金额:", self._stake_spin)
        note = QLabel(f"金额上限：{self._max_stake:.2f} USDT")
        note.setStyleSheet("color:#888;font-size:11px;")
        stake_form.addRow(note)
        root.addWidget(stake_box)

        warn = QLabel(
            "确认后立即提交真实订单；市价入场成交后自动挂止盈/止损保护单。"
        )
        warn.setWordWrap(True)
        warn.setStyleSheet("color:#b26a00;font-size:12px;")
        root.addWidget(warn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确认下单")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._on_confirm)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _on_confirm(self) -> None:
        stake = float(self._stake_spin.value())
        if stake <= 0 or stake > self._max_stake:
            QMessageBox.warning(self, "金额超限",
                                f"金额必须在 (0, {self._max_stake:.2f}] 内。")
            return
        self.final_stake = stake
        self.accept()
