"""成交量单位：eltdx 的 ``volume_wire_value`` 每单位对应多少股。

2026-11-25 按证券类型与 xtdata（成交量为手）逐日核对（xiaoapple0/duohelm-plugins#3）：

- 沪深股票、沪深 ETF、北交所股票：eltdx 的数值是**股数**，等于 xtdata volume（手）x 100
- 指数：eltdx 与 xtdata 的数值相同，都是**手**。用“成交额 / 成交量”检查，结果约为
  100 x A 股平均成交价，所以单位是手，不是股

因此指数要乘 100 才是股数。

证券类型只按交易所和代码前缀判断（代码前缀的划分是交易所的编码规则，与 xtdata 插件的
``security_type`` 一致，并比它更严格）：

- 上交所：``000`` 开头是指数（000001 上证指数、000300 沪深300）；``600/601/603/605/688/689``
  是股票；``51/52/56/58`` 是 ETF
- 深交所：``399`` 开头是指数（399001 深证成指）；``000/001/002/003/300/301`` 是股票；
  ``15/16`` 是 ETF（159 开头的 ETF、16 开头的 LOF）
- 北交所：全部归为 ``bj``（股票，单位同沪深股票）
- **其他前缀一律是 ``unknown``**（债券、可转债、B 股、基金、回购等）：每单位对应多少股
  没有核对过，不能默认成股，也不能默认成手。``shares_per_volume_unit`` 对它抛出
  :class:`UnknownSecurityTypeError`，``EltdxSource.bars`` 因此拒绝返回这类证券的日线，
  而不是悄悄返回一个可能差 100 倍的成交量。
"""

from __future__ import annotations

SHARES_PER_VOLUME_UNIT: dict[str, int] = {
    "stock": 1,
    "etf": 1,
    "bj": 1,
    "index": 100,
}
"""eltdx 的 volume_wire_value 每单位对应的股数，按证券类型。没有 ``unknown`` 这一项。"""

_PREFIXES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "SH": (
        ("index", ("000",)),
        ("stock", ("600", "601", "603", "605", "688", "689")),
        ("etf", ("51", "52", "56", "58")),
    ),
    "SZ": (
        ("index", ("399",)),
        ("stock", ("000", "001", "002", "003", "300", "301")),
        ("etf", ("15", "16")),
    ),
}


class UnknownSecurityTypeError(ValueError):
    """证券类型无法判断，成交量单位未知。"""


def security_type(symbol: str) -> str:
    """``000001.SZ`` -> ``stock``。返回 stock / etf / bj / index / unknown。"""
    digits, exchange = symbol.split(".")
    if exchange == "BJ":
        return "bj"
    for kind, prefixes in _PREFIXES.get(exchange, ()):
        if digits.startswith(prefixes):
            return kind
    return "unknown"


def shares_per_volume_unit(symbol: str) -> int:
    """该证券的 volume_wire_value 每单位对应的股数；类型未知时抛 UnknownSecurityTypeError。"""
    kind = security_type(symbol)
    if kind == "unknown":
        raise UnknownSecurityTypeError(
            f"{symbol} 的证券类型未知，无法确定成交量单位（股还是手）；"
            "eltdx 插件只支持沪深股票、ETF、北交所股票和指数的日线"
        )
    return SHARES_PER_VOLUME_UNIT[kind]
