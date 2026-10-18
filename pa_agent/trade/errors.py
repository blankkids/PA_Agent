"""Binance 交易错误与友好化映射."""
from __future__ import annotations

# 币安错误码 → 中文可操作提示（保持与 binance_common.errors 抛出的 status_code 对应）
_BINANCE_CODE_MESSAGES: dict[int, str] = {
    -1013: "下单被拒：数量/价格精度不符或低于最小名义价值（MIN_NOTIONAL）",
    -2010: "下单被拒：余额/保证金不足（可在交易面板查看可用余额后再试）",
    -2011: "撤单被拒：订单已成交或已取消",
    -2019: "下单被拒：保证金不足（合约），请降低杠杆或金额",
    -2020: "下单被拒：超出最大允许持仓",
    -2021: "下单被拒：订单会立即触发（STOP 价格需偏离最新价）",
    -2022: "下单被拒：reduceOnly 订单将超出持仓数量",
    -1021: "时间戳超窗：本机时钟与币安服务器偏差过大，请校准系统时间",
    -1121: "交易对无效",
    -1130: "金额/价格精度超出允许范围",
    -2015: "API Key 无效、无权限或 IP 受限（请检查 Key 的现货/合约权限与 IP 白名单）",
    -2014: "API Key 格式无效",
    -1003: "请求过于频繁，已触发限流，请稍后重试",
}

_DEFAULT_MESSAGE = "币安请求失败"


def friendly_binance_error(exc: Exception) -> str:
    """把 SDK/HTTP 异常转换为中文可操作提示，保留原始码与消息。"""
    code = getattr(exc, "status_code", None)
    raw_msg = str(getattr(exc, "error_message", "") or exc)
    base = _BINANCE_CODE_MESSAGES.get(int(code)) if isinstance(code, (int, float)) else None
    # HTTP 400 时 SDK 把币安码放在 status_code；其他异常可能只有文本
    if base is None:
        for code_key, msg in _BINANCE_CODE_MESSAGES.items():
            if f"code {code_key}" in raw_msg or f'"code":{code_key}' in raw_msg:
                base = msg
                break
    if base is None:
        name = type(exc).__name__
        if "Unauthorized" in name:
            base = "API Key 无效或权限不足，请检查交易设置"
        elif "Network" in name or "Timeout" in name:
            base = "网络不可达：请检查本地代理是否运行（binance.proxy 或自动探测端口）"
        else:
            base = _DEFAULT_MESSAGE
    code_txt = f"[{int(code)}]" if isinstance(code, (int, float)) else ""
    detail = raw_msg.strip()
    if detail and detail not in base:
        return f"{base} {code_txt}（{detail[:120]}）".strip()
    return f"{base} {code_txt}".strip()


class TradeError(Exception):
    """交易执行失败（已含中文友好信息；args 保留原始码供留痕）."""

    def __init__(self, message: str, *, binance_code: int | None = None,
                 raw: str = "") -> None:
        super().__init__(message)
        self.friendly_message = message
        self.binance_code = binance_code
        self.raw = raw

    @classmethod
    def from_exception(cls, exc: Exception) -> "TradeError":
        if isinstance(exc, cls):
            return exc
        code = getattr(exc, "status_code", None)
        code_int = int(code) if isinstance(code, (int, float)) else None
        return cls(
            friendly_binance_error(exc),
            binance_code=code_int,
            raw=str(exc),
        )


class TradeBlockedError(Exception):
    """下单被前置拦截（不发起交易所请求）."""
