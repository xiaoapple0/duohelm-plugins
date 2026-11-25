"""证券代码转换：eltdx 用 ``sz000001``，DuoHelm 用 ``000001.SZ``。"""

from __future__ import annotations

import re

_TO_EXCHANGE = {"sh": "SH", "sz": "SZ", "bj": "BJ"}
_TO_PREFIX = {v: k for k, v in _TO_EXCHANGE.items()}
_ELTDX = re.compile(r"^(sh|sz|bj)(\d{6})$")
_SYMBOL = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")


class InvalidCodeError(ValueError):
    pass


def to_symbol(full_code: str) -> str:
    """``sz000001`` -> ``000001.SZ``。"""
    m = _ELTDX.fullmatch(full_code.strip().lower())
    if m is None:
        raise InvalidCodeError(f"不是 eltdx 的 A 股代码：{full_code!r}")
    prefix, digits = m.groups()
    return f"{digits}.{_TO_EXCHANGE[prefix]}"


def to_eltdx(symbol: str) -> str:
    """``000001.SZ`` -> ``sz000001``。"""
    m = _SYMBOL.fullmatch(symbol.strip().upper())
    if m is None:
        raise InvalidCodeError(f"证券代码应为 6 位数字加 .SH / .SZ / .BJ：{symbol!r}")
    digits, exchange = m.groups()
    return f"{_TO_PREFIX[exchange]}{digits}"


def exchange_of(symbol: str) -> str:
    return symbol.rsplit(".", 1)[1]
