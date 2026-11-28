"""逐个除权日算出除权比例，并用 preClose 法与 xtdata 的 dr 交叉核对（duohelm-plugins#19）。

记号（与 #19 一致）：

- **B**：事件公式。P 为除权日**之前**最近一个价格有效的收盘价，
  ``参考价 = (P - cash + rights_price × rights) / (1 + bonus + rights)``，
  参考价按交易所的做法**四舍五入到 0.01 元**（ETF 到 0.001 元）后，比例 = P / 参考价
- **A**：preClose 法。除权日当天 K 线的 ``上一行 close / preClose``。preClose 是交易所给的
  除权后前收盘价，破产重整的资本公积转增这类“转增股份不分给原股东”的事件，交易所调整了
  参考价公式，只有 A 反映了实际的价格调整
- **C**：xtdata ``get_divid_factors`` 的 dr

B 是默认值。A 只在“干净”时参与核对（见 :func:`_clean_a`），只有 A 与 B、B 与 C 都相差
超过 1% 时才采用 A（重整转增：603363.SH、603007.SH 2024-12-26 等）。其余情形保留 B，
按偏离程度记警告。判定规则和实测依据见 #19 与 README。

计算只用除权日及以前的 K 线和事件，截断某日之后的数据不改变该日及以前的因子。
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_left
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum

logger = logging.getLogger(__name__)

A_FROM = date(2002, 1, 1)
"""这之前的 preClose 不可信（单位变动、缺行、口径不同，#19 实测），不用 A。"""
MAX_GAP = 2
"""除权日与上一行最多相隔几个交易日（按交易日历）。重整转增多在除权前一天停牌，间隔为 2；
间隔更大的（股改复牌等）preClose 常常没有按除权调整，A=1，不可信。"""
UNADJUSTED_TOL = 1e-6
"""|A − 1| 不超过它时，preClose 就是上一收盘，多半没有按除权调整，A 无效（例外见 _judge）。"""
ADOPT_TOL = 0.01
"""A 与 B、B 与 C 都相差超过它时采用 A；A 与 B 相差超过它而不采用时记警告。"""
MINOR_TOL = 0.005
"""A 与 B 相差超过它（且不超过 ADOPT_TOL）时记警告。"""
AGREE_TOL = 1e-3
"""B 与 C 相差不超过它算一致；最终比例与 C 相差超过它时记警告（原有的 dr 核对）。"""


class Kind(StrEnum):
    """每个除权日的核对结果。值出现在警告里，便于以后统计进质量报告。"""

    AGREE = "agree"
    """A 与 B 相差不超过 0.5%，或 A 不干净：采用 B。"""
    ADOPT_A = "adopt_a"
    """A 与 B、B 与 C 都相差超过 1%：采用 A。"""
    EMPTY_EVENT = "empty_event"
    """B≈C≈1 而 A 偏离 1 超过 1%：事件内容可能为空（重整转增没有记进除权数据），采用 B。"""
    A_OFF_BC_AGREE = "a_off_bc_agree"
    """A 与 B 相差超过 1%，B 与 C 一致：多为差异化分红、配股，A 不可靠，采用 B。"""
    A_OFF = "a_off"
    """A 与 B 相差超过 1%，B 与 C 相差 0.1%–1% 或没有 C：判断不了，采用 B。"""
    A_MINOR = "a_minor"
    """A 与 B 相差 0.5%–1%：采用 B。"""


@dataclass(frozen=True)
class Bar:
    """价格有效、成交量为正的日线。"""

    day: date
    close: float
    pre_close: float | None


@dataclass(frozen=True)
class Event:
    day: date
    cash: float
    bonus: float
    rights: float
    rights_price: float
    dr: float | None


@dataclass(frozen=True)
class Decision:
    day: date
    ratio: float
    """最终采用的除权比例（≥1 表示价格下调）。"""
    b: float
    a: float | None
    """干净时的 A；不干净或无效时为 None。"""
    c: float | None
    kind: Kind
    gap: int | None
    """除权日与上一行相隔的交易日数；日期不在交易日历里时为 None。"""


def round_half_up(value: Decimal, step: Decimal) -> Decimal:
    return value.quantize(step, rounding=ROUND_HALF_UP)


def _dec(value: float) -> Decimal:
    # repr 是能还原这个浮点数的最短十进制：0.035 得到 Decimal("0.035")，不是 0.03499999…
    return Decimal(repr(value))


def reference_price(
    prev_close: float, event: Event, *, tick: Decimal = Decimal("0.01")
) -> float | None:
    """除权参考价，四舍五入到 tick。输入不是有限数或结果不是正数时返回 None（事件无效）。"""
    values = (prev_close, event.cash, event.bonus, event.rights, event.rights_price)
    if not all(math.isfinite(v) for v in values):
        return None
    try:
        numerator = (
            _dec(prev_close) - _dec(event.cash) + _dec(event.rights_price) * _dec(event.rights)
        )
        denominator = 1 + _dec(event.bonus) + _dec(event.rights)
        if denominator <= 0:
            return None
        ref = round_half_up(numerator / denominator, tick)
    except InvalidOperation:
        return None
    return float(ref) if ref > 0 else None


def _rel(x: float, y: float) -> float:
    return abs(x / y - 1)


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def decide(
    bars: Sequence[Bar],
    events: Iterable[Event],
    calendar_index: dict[date, int],
    *,
    tick: Decimal = Decimal("0.01"),
) -> list[Decision]:
    """每个除权日的最终比例，按日期排序。

    bars 按日期升序。除权日之前没有 K 线（上市前）或事件无效（P、参考价不是有限正数）的
    除权日不产生结果，调用方另行统计。同一天的多个事件比例相乘（xtdata 实测没有这种情况）。
    """
    by_day: dict[date, list[Event]] = {}
    for event in events:
        by_day.setdefault(event.day, []).append(event)
    days = [b.day for b in bars]
    decisions: list[Decision] = []
    for day in sorted(by_day):
        i = bisect_left(days, day)
        if i == 0:
            continue  # 上市前
        prev = bars[i - 1]
        b = 1.0
        valid = True
        for event in by_day[day]:
            ref = reference_price(prev.close, event, tick=tick) if _positive(prev.close) else None
            if ref is None:
                valid = False
                break
            b *= prev.close / ref
        if not valid:
            continue
        drs = [e.dr for e in by_day[day]]
        c = math.prod(d for d in drs if d is not None) if all(_positive(d) for d in drs) else None
        today = bars[i] if i < len(bars) and bars[i].day == day else None
        gap = _gap(prev.day, day, calendar_index)
        a = _clean_a(prev, today, gap)
        decisions.append(_judge(day, a, b, c, gap))
    return decisions


def _gap(prev_day: date, day: date, calendar_index: dict[date, int]) -> int | None:
    if prev_day in calendar_index and day in calendar_index:
        return calendar_index[day] - calendar_index[prev_day]
    return None


def _clean_a(prev: Bar, today: Bar | None, gap: int | None) -> float | None:
    """A 干净时返回 A，否则 None。干净：除权日当天有 K 线、与上一行相隔不超过 MAX_GAP 个
    交易日、preClose 和上一行收盘价都是有限正数、除权日在 2002-01-01 及以后。
    A 等于 1 的另见 :func:`_judge`。"""
    if today is None or today.day < A_FROM or gap is None or not 1 <= gap <= MAX_GAP:
        return None
    if not _positive(today.pre_close) or not _positive(prev.close):
        return None
    assert today.pre_close is not None
    return prev.close / today.pre_close


def _judge(day: date, a: float | None, b: float, c: float | None, gap: int | None) -> Decision:
    if a is not None and abs(a - 1) <= UNADJUSTED_TOL:
        # preClose 等于上一收盘：多半是 preClose 没有按除权调整（停牌后复牌当天除权），A 无效。
        # 只有 dr 也是 1、而事件公式与 dr 相差超过 1% 时才采用 A：A、C 两个来源都说价格没有
        # 调整（603363.SH 2024-12-26，重整转增不分给原股东，公告已核对）。dr 与 B 一致的
        # （002310.SZ 2024-12-30 等）不采用
        adopt = c is not None and abs(c - 1) <= AGREE_TOL and _rel(b, c) > ADOPT_TOL
        kind = Kind.ADOPT_A if adopt else Kind.AGREE
        a = a if adopt else None
    else:
        kind = _kind(a, b, c)
    ratio = a if kind is Kind.ADOPT_A and a is not None else b
    return Decision(day=day, ratio=ratio, b=b, a=a, c=c, kind=kind, gap=gap)


def _kind(a: float | None, b: float, c: float | None) -> Kind:
    if a is None:
        return Kind.AGREE
    bc_agree = c is not None and _rel(b, c) <= AGREE_TOL
    off = _rel(a, b)
    if off > ADOPT_TOL:
        if bc_agree and abs(b - 1) <= UNADJUSTED_TOL:
            return Kind.EMPTY_EVENT
        if c is not None and _rel(b, c) > ADOPT_TOL:
            return Kind.ADOPT_A
        return Kind.A_OFF_BC_AGREE if bc_agree else Kind.A_OFF
    return Kind.A_MINOR if off > MINOR_TOL else Kind.AGREE


_MESSAGES: dict[Kind, str] = {
    Kind.ADOPT_A: "采用 preClose 法的比例 A（A 与 B、B 与 dr 都相差超过 1%，多为破产重整的转增）",
    Kind.EMPTY_EVENT: (
        "除权数据的比例为 1，但 preClose 显示价格调整了，可能是没有记进除权数据的转增，"
        "保留 B，请人工核对公告"
    ),
    Kind.A_OFF_BC_AGREE: "preClose 法与事件公式相差超过 1%，事件公式与 dr 一致，保留 B",
    Kind.A_OFF: "preClose 法与事件公式相差超过 1%，事件公式与 dr 不一致，保留 B，请人工核对",
    Kind.A_MINOR: "preClose 法与事件公式相差 0.5%–1%，保留 B",
}


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.6f}"


def log_decisions(symbol: str, decisions: Iterable[Decision]) -> None:
    """按核对结果记警告。消息以“除权核对[kind]”开头，带 A、B、C 和间隔，便于统计。

    最终比例与 dr 相差超过 0.1% 的（原有的 dr 核对）另记一条；采用 A 的已经在上一条里
    写了 dr，不再重复。"""
    for d in decisions:
        values = f"A={_fmt(d.a)} B={_fmt(d.b)} C={_fmt(d.c)} 采用={d.ratio:.6f} 间隔={d.gap}"
        message = _MESSAGES.get(d.kind)
        if message is not None:
            logger.warning("除权核对[%s] %s %s：%s；%s", d.kind, symbol, d.day, message, values)
        if d.kind is not Kind.ADOPT_A and d.c is not None and _rel(d.ratio, d.c) > AGREE_TOL:
            logger.warning(
                "%s %s：本地除权比例 %.6f 与 xtdata 的 dr %.6f 相差 %.3f%%",
                symbol,
                d.day,
                d.ratio,
                d.c,
                _rel(d.ratio, d.c) * 100,
            )
