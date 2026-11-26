"""日期与单位换算。不依赖 xtquant，可以直接测试。

**日期一律由毫秒时间戳按 Asia/Shanghai 换算。** xtdata 返回的日期字符串（DataFrame 的索引）
是按运行机器的本机时区格式化的：本机不在东八区时会整体错一天（2026-11-25 在美东时区实测，
宁德时代上市日显示为 2018-06-10）。

xtdata 日线的时间戳是**固定 UTC+8 的零点**（2026-11-25 用录制数据核对：1986–1991 年夏令时
期间的 K 线，按 Asia/Shanghai 换算是凌晨 1:00，不是 0:00）。按 Asia/Shanghai 换算日期，对
“固定 +8 零点”和“当地（含夏令时）零点”两种约定都正确；只按固定 +8 换算，遇到按夏令时当地
零点给时间戳的数据源会错一天。Windows 上的 Python 没有自带时区数据库，需要 tzdata 包。
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")

SHARES_PER_VOLUME_UNIT: dict[str, int] = {
    "stock": 100,
    "etf": 100,
    "bj": 100,
    "index": 100,
}
"""xtdata 的 volume 单位是“手”：每单位对应的股数。

2026-11-25 按证券类型与 eltdx（成交量为股）逐日核对：沪深股票、沪深 ETF、北交所股票的
xtdata volume × 100 = eltdx 股数；指数两边单位相同，按“成交额 ÷ 成交量 ≈ 100 × A 股平均
成交价”判断也是手。回放测试按类型检查：成交额 ÷ 股数必须落在当天的最低价与最高价之间。
"""


def shanghai_date(ms: float) -> date:
    """xtdata 的毫秒时间戳 -> 交易日（北京时间的日期）。"""
    return datetime.fromtimestamp(int(ms) / 1000, tz=SHANGHAI).date()


def parse_yyyymmdd(value: object) -> date | None:
    """xtdata 合约信息里的 8 位日期字符串（如 OpenDate、ExpireDate）。无效或占位值返回 None。"""
    text = str(value).strip() if value is not None else ""
    if len(text) != 8 or not text.isdigit():
        return None
    year, month, day = int(text[:4]), int(text[4:6]), int(text[6:])
    if not 1980 <= year <= 2100:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def security_type(symbol: str) -> str:
    """按代码判断证券类型，用于成交量单位换算。"""
    digits, exchange = symbol.split(".")
    if exchange == "BJ":
        return "bj"
    if exchange == "SH":
        if digits.startswith("000"):
            return "index"
        if digits.startswith(("51", "52", "56", "58")):
            return "etf"
    if exchange == "SZ":
        if digits.startswith("399"):
            return "index"
        if digits.startswith(("15", "16")):
            return "etf"
    return "stock"
