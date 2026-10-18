"""Binance 交易面板：余额 / 合约持仓 / 挂单（撤单）/ 市价平仓.

非模态对话框；查询在后台线程执行，结果经信号回主线程渲染。
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pa_agent.trade.binance_trader import BinanceTrader
from pa_agent.trade.errors import TradeError

logger = logging.getLogger(__name__)


class _QueryWorker(QThread):
    """后台查询：balances / positions / open_orders."""

    done = pyqtSignal(str, object)   # (kind, data 或 str 错误)
    failed = pyqtSignal(str, str)    # (kind, friendly error)

    def __init__(self, trader: BinanceTrader, symbol: str, kinds: list[str]) -> None:
        super().__init__()
        self._trader = trader
        self._symbol = symbol
        self._kinds = kinds

    def run(self) -> None:
        for kind in self._kinds:
            try:
                if kind == "balances":
                    self.done.emit(kind, self._trader.balances())
                elif kind == "positions":
                    self.done.emit(kind, self._trader.positions(self._symbol))
                elif kind == "orders":
                    self.done.emit(kind, self._trader.open_orders(self._symbol))
            except TradeError as exc:
                self.failed.emit(kind, exc.friendly_message)
            except Exception as exc:  # noqa: BLE001
                self.failed.emit(kind, str(exc))


class _ActionWorker(QThread):
    """后台动作：撤单/撤全部/平仓（含留痕）."""

    done = pyqtSignal(str, str)      # (action, 摘要)
    failed = pyqtSignal(str, str)

    def __init__(self, trader: BinanceTrader, symbol: str) -> None:
        super().__init__()
        self._trader = trader
        self._symbol = symbol
        self._job: tuple[str, dict] | None = None

    def submit(self, action: str, **kw) -> None:
        if self.isRunning():
            return
        self._job = (action, kw)
        self.start()

    def _log(self, action: str, **fields) -> None:
        from pa_agent.trade.executions import append_execution_event

        append_execution_event({
            "action": action,
            "market": self._trader.market,
            "env": "testnet" if self._trader.testnet else "live",
            "symbol": self._symbol,
            **fields,
        })

    def run(self) -> None:
        action, kw = self._job or (None, {})
        try:
            if action == "cancel":
                self._trader.cancel_order(self._symbol, int(kw["order_id"]),
                                          algo=bool(kw.get("algo")))
                self._log("cancel", order_id=kw["order_id"], status="canceled")
                self.done.emit("cancel", f"已撤销订单 {kw['order_id']}")
            elif action == "cancel_all":
                self._trader.cancel_all(self._symbol)
                self._log("cancel_all", status="canceled-all")
                self.done.emit("cancel_all", "已撤销全部挂单")
            elif action == "close":
                # 先撤保护挂单再市价平仓（同后台线程，避免 GUI 卡顿与裸露冲突）
                self._trader.cancel_all(self._symbol)
                self._log("cancel_all", status="canceled-all-before-close")
                res = self._trader.close_position_market(self._symbol)
                self._log("close", order_id=res.order_id, qty=res.executed_qty,
                          avg_price=res.avg_price, status=res.status)
                self.done.emit(
                    "close",
                    f"平仓完成：成交 {res.executed_qty:.8g}，均价 {res.avg_price:.6g}",
                )
        except TradeError as exc:
            self._log(f"{action}-failed" if action else "unknown-failed",
                      error=exc.friendly_message, binance_code=exc.binance_code)
            self.failed.emit(action or "?", exc.friendly_message)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(action or "?", str(exc))


class TradePanel(QDialog):
    """账户与持仓管理面板（非模态）."""

    def __init__(
        self,
        trader: BinanceTrader,
        symbol: str,
        *,
        market: str,
        testnet: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._trader = trader
        self._symbol = symbol
        self._market = market
        self._testnet = testnet
        self.setWindowTitle(f"交易面板 · {symbol}")
        self.setMinimumSize(640, 480)
        self.setModal(False)
        self._query: _QueryWorker | None = None
        self._action: _ActionWorker | None = None
        self._setup_ui()
        self.refresh()

    # ── UI ───────────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)

        head = QHBoxLayout()
        env_txt = "USDT-M 合约" if self._market == "futures" else "现货"
        env_color = "#7b1fa2" if self._market == "futures" else "#1565c0"
        env_name = "Testnet" if self._testnet else "实盘"
        if not self._testnet:
            env_name = f'<b style="color:#d32f2f">{env_name}</b>'
        head.addWidget(QLabel(
            f"<b>{self._symbol}</b> · <span style='color:{env_color}'>{env_txt}</span>"
            f" · {env_name}"
        ))
        head.addStretch(1)
        refresh_btn = QPushButton("刷新")
        refresh_btn.clicked.connect(self.refresh)
        head.addWidget(refresh_btn)
        root.addLayout(head)

        self._tabs = QTabWidget()

        # 余额表
        self._balance_table = QTableWidget(0, 3)
        self._balance_table.setHorizontalHeaderLabels(["资产", "可用", "冻结/占用"])
        self._balance_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._balance_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tabs.addTab(self._balance_table, "余额")

        # 持仓表（合约）
        self._position_table = QTableWidget(0, 6)
        self._position_table.setHorizontalHeaderLabels(
            ["方向", "数量", "开仓均价", "标记价", "未实现盈亏", "杠杆"])
        self._position_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._position_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._pos_tab_idx = self._tabs.addTab(self._position_table, "持仓（合约）")

        # 挂单表
        self._orders_table = QTableWidget(0, 7)
        self._orders_table.setHorizontalHeaderLabels(
            ["订单ID", "方向", "类型", "价格/触发", "数量", "reduceOnly", "操作"])
        self._orders_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self._orders_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._tabs.addTab(self._orders_table, "挂单")

        root.addWidget(self._tabs, stretch=1)

        foot = QHBoxLayout()
        self._status_label = QLabel("就绪")
        self._status_label.setStyleSheet("color:#666;")
        foot.addWidget(self._status_label, stretch=1)
        self._cancel_all_btn = QPushButton("撤销全部挂单")
        self._cancel_all_btn.clicked.connect(self._on_cancel_all)
        foot.addWidget(self._cancel_all_btn)
        self._close_btn = QPushButton("市价平仓")
        self._close_btn.setStyleSheet("color:#d32f2f;font-weight:bold;")
        self._close_btn.clicked.connect(self._on_close_position)
        foot.addWidget(self._close_btn)
        root.addLayout(foot)

        if self._market != "futures":
            self._tabs.setTabVisible(self._pos_tab_idx, False)

    # ── 刷新 ─────────────────────────────────────────────────────────────────

    def refresh(self) -> None:
        if self._query is not None and self._query.isRunning():
            return
        self._status_label.setText("查询中…")
        kinds = ["balances", "orders"] + (["positions"] if self._market == "futures" else [])
        self._query = _QueryWorker(self._trader, self._symbol, kinds)
        self._query.done.connect(self._on_query_done)
        self._query.failed.connect(self._on_query_failed)
        self._query.start()

    def _on_query_done(self, kind: str, data) -> None:
        if kind == "balances":
            self._render_balances(data)
        elif kind == "positions":
            self._render_positions(data)
        elif kind == "orders":
            self._render_orders(data)
        self._status_label.setText("就绪")

    def _on_query_failed(self, kind: str, err: str) -> None:
        self._status_label.setText(f"查询失败：{err[:80]}")

    def _render_balances(self, rows: list[dict]) -> None:
        self._balance_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self._balance_table.setItem(i, 0, QTableWidgetItem(str(r.get("asset", ""))))
            self._balance_table.setItem(i, 1, QTableWidgetItem(f"{r.get('free', 0):.8g}"))
            self._balance_table.setItem(i, 2, QTableWidgetItem(f"{r.get('locked', 0):.8g}"))

    def _render_positions(self, rows: list[dict]) -> None:
        self._position_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            color = "#2e7d32" if r["direction"] == "做多" else "#d32f2f"
            d = QTableWidgetItem(r["direction"])
            d.setForeground(Qt.GlobalColor.darkGreen if r["direction"] == "做多"
                            else Qt.GlobalColor.red)
            self._position_table.setItem(i, 0, d)
            self._position_table.setItem(i, 1, QTableWidgetItem(f"{r['amount']:.8g}"))
            self._position_table.setItem(i, 2, QTableWidgetItem(f"{r['entry_price']:.6g}"))
            self._position_table.setItem(i, 3, QTableWidgetItem(f"{r['mark_price']:.6g}"))
            self._position_table.setItem(i, 4, QTableWidgetItem(f"{r['unrealized_pnl']:.4f}"))
            self._position_table.setItem(i, 5, QTableWidgetItem(f"{r['leverage']}x"))

    def _render_orders(self, rows: list[dict]) -> None:
        self._orders_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self._orders_table.setItem(i, 0, QTableWidgetItem(
                f"{'A' if r.get('algo') else ''}{r.get('order_id', '')}"))
            self._orders_table.setItem(i, 1, QTableWidgetItem(str(r.get("side", ""))))
            self._orders_table.setItem(i, 2, QTableWidgetItem(str(r.get("type", ""))))
            price = r.get("stop_price") or r.get("price") or 0
            self._orders_table.setItem(i, 3, QTableWidgetItem(f"{price:.6g}"))
            self._orders_table.setItem(i, 4, QTableWidgetItem(f"{r.get('orig_qty', 0):.8g}"))
            self._orders_table.setItem(i, 5, QTableWidgetItem(
                "是" if r.get("reduce_only") else ""))
            btn = QPushButton("撤单")
            btn.clicked.connect(
                lambda _, oid=r.get("order_id"), algo=r.get("algo"):
                    self._on_cancel_one(int(oid), bool(algo)))
            self._orders_table.setCellWidget(i, 6, btn)

    # ── 动作 ─────────────────────────────────────────────────────────────────

    def _on_cancel_one(self, order_id: int, algo: bool) -> None:
        self._submit_action("cancel", order_id=order_id, algo=algo)

    def _on_cancel_all(self) -> None:
        ret = QMessageBox.question(
            self, "撤销全部挂单",
            f"确定撤销 {self._symbol} 的全部挂单（含止盈/止损保护单）？",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._submit_action("cancel_all")

    def _on_close_position(self) -> None:
        ret = QMessageBox.question(
            self, "市价平仓",
            f"确定市价平掉 {self._symbol} 全部持仓，并撤销该交易对保护挂单？\n"
            f"环境：{'Testnet' if self._testnet else '实盘（真实资金）'}",
        )
        if ret != QMessageBox.StandardButton.Yes:
            return
        self._submit_action("close")

    def _submit_action(self, action: str, **kw) -> None:
        if self._action is not None and self._action.isRunning():
            self._status_label.setText("有操作进行中，请稍候…")
            return
        self._status_label.setText("执行中…")
        if self._action is None:
            self._action = _ActionWorker(self._trader, self._symbol)
            self._action.done.connect(self._on_action_done)
            self._action.failed.connect(self._on_action_failed)
        self._action.submit(action, **kw)

    def _on_action_done(self, action: str, summary: str) -> None:
        self._status_label.setText(summary)
        self.refresh()

    def _on_action_failed(self, action: str, err: str) -> None:
        self._status_label.setText(f"失败：{err[:80]}")
        QMessageBox.warning(self, "操作失败", err)
