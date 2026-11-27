"""成交量单位：eltdx 的 ``volume_wire_value`` 每单位对应多少股。

2026-11-25 按证券类型与 xtdata（成交量为手）逐日核对（xiaoapple0/duohelm-plugins#3）：

- 沪深股票、沪深 ETF、北交所股票：eltdx 的数值是**股数**，等于 xtdata volume（手）x 100
- 指数：eltdx 与 xtdata 的数值相同，都是**手**。用“成交额 / 成交量”检查，结果约为
  100 x A 股平均成交价，所以单位是手，不是股

因此指数要乘 100 才是股数。

证券类型只按交易所和代码前缀判断，**只列出有依据的前缀**。依据只有两类：issue #3 的结论
（沪深股票、ETF、北交所股票是股，指数是手），以及本地录制数据（2026-11-25 的 eltdx 3.3.5
代码表和日线）里实际出现过的代码。没有依据的前缀一律是 ``unknown``，不靠推测补全：

- 指数：上交所 ``000``（录制数据里有 000001 上证指数）；深交所 ``399``（399001 深证成指）
- 股票：上交所 ``600/601/603/605/688/689``，深交所 ``000/001/002/003/300/301``，
  都是录制的代码表里出现过的前缀，另有 ``302``（302132 创业板股票，录制的代码表里
  类别是 A 股；同样的 000001 在上交所是指数、在深交所是平安银行）
- 北交所股票：只有 ``920``（录制的代码表里北交所全是 920 开头）。``899``（北证 50 等指数，
  如 899050）没有核对过，不能当股票
- ETF：依据是 issue #3 的核对结论（沪深 ETF 的数值是股数）加上交易所规定的 ETF 号段：
  上交所 ``510-518``、``560-563``、``588-589``，深交所 ``159``。**这几个号段没有录制数据
  覆盖**。LOF（``16`` 开头）、分级基金（``150`` 开头）、上交所 ``52`` 开头等没有核对过，
  是 ``unknown``
- **其他前缀一律是 ``unknown``**（债券、可转债、B 股、LOF、基金、回购、北交所老代码等）：
  每单位对应多少股没有核对过，不能默认成股，也不能默认成手。``shares_per_volume_unit`` 对它
  抛出 :class:`UnknownSecurityTypeError`，``EltdxSource.bars`` 因此拒绝返回这类证券的日线，
  而不是悄悄返回一个可能差 100 倍的成交量。
"""

from __future__ import annotations

SHARES_PER_VOLUME_UNIT: dict[str, int] = {
    "stock": 1,
    "etf": 1,
    "bj": 1,
    "index": 100,
}
"""eltdx 的 volume_wire_value 每单位对应的股数，按证券类型。没有 ``unknown`` 这一项。

``etf`` 的前缀只有交易所规定的 ETF 号段，没有录制数据覆盖（见模块文档）。"""

_PREFIXES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "SH": (
        ("index", ("000",)),
        ("stock", ("600", "601", "603", "605", "688", "689")),
        (
            "etf",
            (
                *(f"51{i}" for i in range(9)),  # 510-518
                "560",
                "561",
                "562",
                "563",
                "588",
                "589",
            ),
        ),
    ),
    "BJ": (("bj", ("920",)),),
    "SZ": (
        ("index", ("399",)),
        ("stock", ("000", "001", "002", "003", "300", "301", "302")),
        ("etf", ("159",)),
    ),
}


class UnknownSecurityTypeError(ValueError):
    """证券类型无法判断，成交量单位未知。"""


def security_type(symbol: str) -> str:
    """``000001.SZ`` -> ``stock``。返回 stock / etf / bj / index / unknown。"""
    digits, exchange = symbol.split(".")
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
