"""除权比例的判定（duohelm-plugins#19）：参考价取整、preClose 法（A）与事件公式（B）、dr（C）。

数字取自 2026-11-28 的 MiniQMT 实测（只读），证券代码只用来标明是哪一类情形。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

import pytest

from duohelm_xtdata.backend import RawBar, RawDivid
from duohelm_xtdata.exrights import Bar, Event, Kind, decide, log_decisions, reference_price
from duohelm_xtdata.source import XtdataSource

CAL = [d for d in (date(2024, 12, 16) + timedelta(days=i) for i in range(40)) if d.weekday() < 5]
"""连续的交易日（工作日，够用）。"""
IDX = {d: i for i, d in enumerate(CAL)}
EX = CAL[10]
"""除权日。"""


def _event(
    *,
    cash: float = 0.0,
    bonus: float = 0.0,
    rights: float = 0.0,
    rights_price: float = 0.0,
    dr: float | None = None,
    day: date = EX,
) -> Event:
    return Event(day, cash, bonus, rights, rights_price, dr)


def _one(
    prev_close: float,
    pre_close: float | None,
    event: Event,
    *,
    gap: int = 1,
    ex_bar: bool = True,
) -> tuple[float, Kind, float | None]:
    """除权日前 gap 个交易日一根 K 线（收盘 prev_close），除权日一根（preClose 为 pre_close）。"""

    prev_day = CAL[IDX[event.day] - gap]
    bars = [Bar(prev_day, prev_close, prev_close)]
    if ex_bar:
        bars.append(Bar(event.day, pre_close or prev_close, pre_close))
    [d] = decide(bars, [event], IDX)
    return d.ratio, d.kind, d.a


# ---- 参考价取整到 0.01 元（四舍五入） ----


@pytest.mark.parametrize(
    ("prev_close", "cash", "expected"),
    [
        (3.78, 0.005, 3.78),  # 3.775：浮点是 3.77499…，round() 会得 3.77
        (10.0, 0.125, 9.88),  # 9.875
        (1.0, 0.015, 0.99),  # 0.985：浮点 round() 得 0.98
        (2.05, 0.0, 2.05),
        (10.0, 0.1249, 9.88),  # 9.8751
        (10.0, 0.1251, 9.87),  # 9.8749
    ],
)
def test_reference_price_rounds_half_up(prev_close: float, cash: float, expected: float) -> None:
    assert reference_price(prev_close, _event(cash=cash)) == expected


def test_reference_price_rounds_after_division() -> None:
    """先算 (P - cash) / (1 + 送转) 再取整，不是先取整再除。"""
    # (2.05 - 0) / 2.234 = 0.91763…，取整 0.92
    assert reference_price(2.05, _event(bonus=1.234)) == 0.92


def test_reference_price_etf_tick() -> None:
    assert reference_price(3.0, _event(cash=0.0015), tick=Decimal("0.001")) == 2.999


@pytest.mark.parametrize(
    "event",
    [
        _event(cash=0.996),  # 参考价 0.004 取整为 0.00
        _event(cash=float("nan")),
        _event(bonus=float("inf")),
        _event(cash=2.0),  # 参考价为负
    ],
)
def test_reference_price_invalid(event: Event) -> None:
    assert reference_price(1.0, event) is None


def test_ratio_uses_rounded_reference() -> None:
    """000001.SZ 2024-06-14 的情形：每股派 0.719 元，参考价 9.321 取整为 9.32。"""
    ratio, kind, _ = _one(10.04, None, _event(cash=0.719, dr=10.04 / 9.32))
    assert ratio == pytest.approx(10.04 / 9.32, rel=1e-12)
    assert ratio != pytest.approx(10.04 / 9.321, rel=1e-6)
    assert kind is Kind.AGREE


# ---- 采用 A ----


def test_adopt_a_restructuring_gap1_with_unadjusted_preclose() -> None:
    """603363.SH 2024-12-26：重整转增每股 2 股，转增股份不分给原股东，preClose = 上一收盘，
    dr = 1。事件公式给 3.0，A 和 C 都是 1：采用 A。"""
    ratio, kind, _ = _one(3.78, 3.78, _event(bonus=2.0, dr=1.0))
    assert kind is Kind.ADOPT_A
    assert ratio == 1.0


def test_adopt_a_restructuring_gap2_dr_also_wrong() -> None:
    """603007.SH 2024-12-26：除权前一天停牌（间隔 2 个交易日）。B 2.155、C 1.155，
    A = 15.39 / 8.28 = 1.8587（公告已核对，A 对）。"""
    ratio, kind, a = _one(15.39, 8.28, _event(bonus=1.155346, dr=1.155405), gap=2)
    assert kind is Kind.ADOPT_A
    assert ratio == a == pytest.approx(15.39 / 8.28)


def test_adopt_a_when_a_matches_dr() -> None:
    """300093.SZ 2025-11-20：A ≈ C ≠ B。"""
    ratio, kind, _ = _one(14.83, 13.06, _event(bonus=1.5, dr=1.135493), gap=2)
    assert kind is Kind.ADOPT_A
    assert ratio == pytest.approx(14.83 / 13.06)


# ---- A 干净但不采用 ----


def test_unadjusted_preclose_not_adopted_when_dr_agrees_with_formula() -> None:
    """002310.SZ 2024-12-30：间隔 2，preClose = 上一收盘（A = 1），B ≈ C ≈ 2.23。
    preClose 没有按除权调整，A 无效，保留 B。"""
    ratio, kind, a = _one(2.05, 2.05, _event(bonus=1.234, dr=2.22826), gap=2)
    assert kind is Kind.AGREE
    assert a is None
    assert ratio == pytest.approx(2.05 / 0.92)


def test_unadjusted_preclose_not_adopted_without_dr() -> None:
    ratio, kind, _ = _one(3.78, 3.78, _event(bonus=2.0, dr=None))
    assert kind is Kind.AGREE
    assert ratio == pytest.approx(3.0)


def test_unadjusted_preclose_not_adopted_when_dr_is_not_one() -> None:
    """A = 1 而 C 既不等于 1 也不等于 B：不采用 A。"""
    ratio, kind, _ = _one(3.78, 3.78, _event(bonus=2.0, dr=1.5))
    assert kind is Kind.AGREE
    assert ratio == pytest.approx(3.0)


def test_empty_event_warns_but_keeps_b() -> None:
    """603559.SH 2024-12-26：除权数据里送转、现金都是 0（B = C = 1），preClose 显示价格
    下调了 1.448 倍（公告为重整转增）。不自动改，保留 B。"""
    ratio, kind, _ = _one(10.83, 7.48, _event(dr=1.0), gap=2)
    assert kind is Kind.EMPTY_EVENT
    assert ratio == 1.0


def test_a_off_but_formula_agrees_with_dr_keeps_b() -> None:
    """差异化分红、配股：A 与 B 差 >1%，B ≈ C。"""
    ratio, kind, _ = _one(10.0, 9.2, _event(cash=1.0, dr=10 / 9))
    assert kind is Kind.A_OFF_BC_AGREE
    assert ratio == pytest.approx(10 / 9)


def test_a_off_and_formula_near_dr_keeps_b() -> None:
    """A 与 B 差 >1%，B 与 C 相差 0.1%–1%：判断不了，保留 B。"""
    ratio, kind, _ = _one(10.0, 9.2, _event(cash=1.0, dr=10 / 9 * 1.005))
    assert kind is Kind.A_OFF
    assert ratio == pytest.approx(10 / 9)


def test_a_off_without_dr_keeps_b() -> None:
    ratio, kind, _ = _one(15.39, 8.28, _event(bonus=1.155346, dr=None), gap=2)
    assert kind is Kind.A_OFF
    assert ratio == pytest.approx(15.39 / 7.14)


def test_a_minor_keeps_b() -> None:
    """A 与 B 相差 0.5%–1%（这里 0.66%）。"""
    ratio, kind, _ = _one(10.0, 9.06, _event(cash=1.0, dr=10 / 9))
    assert kind is Kind.A_MINOR
    assert ratio == pytest.approx(10 / 9)


@pytest.mark.parametrize("pre_close", [9.0, 9.04])  # 相同；相差 0.44%
def test_a_agrees(pre_close: float) -> None:
    ratio, kind, _ = _one(10.0, pre_close, _event(cash=1.0, dr=10 / 9))
    assert kind is Kind.AGREE
    assert ratio == pytest.approx(10 / 9)


# ---- A 不干净：一律保留 B ----

RESTRUCTURING = _event(bonus=1.155346, dr=1.155405)
"""603007.SH 的数字：A 干净时会被采用。"""


def test_gap3_not_clean() -> None:
    ratio, kind, a = _one(15.39, 8.28, RESTRUCTURING, gap=3)
    assert (kind, a) == (Kind.AGREE, None)
    assert ratio == pytest.approx(15.39 / 7.14)


def test_before_2002_not_clean() -> None:
    cal = [date(2001, 12, 3) + timedelta(days=i) for i in range(30)]
    ex = cal[10]
    idx = {d: i for i, d in enumerate(cal)}
    bars = [Bar(cal[9], 15.39, 15.39), Bar(ex, 8.0, 8.28)]
    [d] = decide(bars, [_event(bonus=1.155346, dr=1.155405, day=ex)], idx)
    assert (d.kind, d.a) == (Kind.AGREE, None)
    # 2002-01-01 当天及以后算干净
    jan1 = date(2002, 1, 1)
    idx2 = {jan1 - timedelta(days=1): 0, jan1: 1}
    bars2 = [Bar(jan1 - timedelta(days=1), 15.39, 15.39), Bar(jan1, 8.0, 8.28)]
    [d2] = decide(bars2, [_event(bonus=1.155346, dr=1.155405, day=jan1)], idx2)
    assert d2.kind is Kind.ADOPT_A


def test_no_bar_on_ex_date_not_clean() -> None:
    """停牌期间除权：除权日当天没有 K 线。"""
    ratio, kind, a = _one(15.39, 8.28, RESTRUCTURING, ex_bar=False)
    assert (kind, a) == (Kind.AGREE, None)
    assert ratio == pytest.approx(15.39 / 7.14)


def test_no_bar_on_ex_date_does_not_use_resumption_bar() -> None:
    """停牌期间除权、之后复牌：不用复牌那根 K 线的 preClose 算 A。"""
    bars = [Bar(CAL[9], 15.39, 15.39), Bar(CAL[12], 8.0, 8.28)]
    [d] = decide(bars, [RESTRUCTURING], IDX)
    assert (d.kind, d.a) == (Kind.AGREE, None)
    assert d.ratio == pytest.approx(15.39 / 7.14)


@pytest.mark.parametrize("pre_close", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_invalid_preclose_not_clean(pre_close: float | None) -> None:
    """preClose 缺失（旧录制文件）、不是有限正数。"""
    bars = [Bar(CAL[9], 15.39, 15.39), Bar(EX, 8.0, pre_close)]
    [d] = decide(bars, [RESTRUCTURING], IDX)
    assert (d.kind, d.a) == (Kind.AGREE, None)


def test_dates_outside_calendar_not_clean() -> None:
    bars = [Bar(CAL[9], 15.39, 15.39), Bar(EX, 8.0, 8.28)]
    [d] = decide(bars, [RESTRUCTURING], {})
    assert (d.kind, d.a, d.gap) == (Kind.AGREE, None, None)


# ---- 其它 ----


def test_events_before_listing_or_invalid_give_nothing() -> None:
    bars = [Bar(CAL[5], 1.0, 1.0), Bar(CAL[6], 1.0, 1.0)]
    events = [
        _event(cash=1.0, day=CAL[4]),
        _event(cash=1.0, day=CAL[5]),
        _event(cash=0.5, day=CAL[6]),
    ]
    # CAL[4]：上市前；CAL[5]：参考价 0；CAL[6]：有效
    assert [d.day for d in decide(bars, events, IDX)] == [CAL[6]]


def test_truncation_does_not_change_earlier_decisions() -> None:
    """截断某日之后的 K 线和事件，该日及以前的判定不变（没有未来函数）。"""
    bars = [
        Bar(CAL[0], 15.39, 15.39),
        Bar(CAL[2], 8.0, 8.28),
        Bar(CAL[3], 8.0, 8.0),
        Bar(CAL[4], 4.0, 4.0),
    ]
    events = [
        _event(bonus=1.155346, dr=1.155405, day=CAL[2]),
        _event(bonus=1.0, dr=2.0, day=CAL[4]),
    ]
    full = decide(bars, events, IDX)
    cut = decide(bars[:2], events[:1], {d: i for d, i in IDX.items() if d <= CAL[2]})
    assert full[:1] == cut


def test_warnings_are_tagged_and_not_duplicated(caplog: pytest.LogCaptureFixture) -> None:
    bars = [Bar(CAL[8], 15.39, 15.39), Bar(EX, 8.0, 8.28)]
    decisions = decide(bars, [RESTRUCTURING], IDX)
    with caplog.at_level(logging.WARNING):
        log_decisions("603007.SH", decisions)
    [record] = caplog.records  # 采用 A 的不再另记 dr 偏差
    message = record.getMessage()
    assert message.startswith("除权核对[adopt_a] 603007.SH")
    assert all(s in message for s in ("A=1.858696", "B=2.155462", "C=1.155405", "间隔=2"))


def test_unclean_dr_mismatch_still_warned(caplog: pytest.LogCaptureFixture) -> None:
    """A 不干净时只剩 dr 核对：B 与 C 相差超过 0.1% 记警告。"""
    bars = [Bar(CAL[6], 15.39, 15.39), Bar(EX, 8.0, 8.28)]  # 间隔 4
    decisions = decide(bars, [RESTRUCTURING], IDX)
    with caplog.at_level(logging.WARNING):
        log_decisions("603007.SH", decisions)
    [record] = caplog.records
    assert "dr 1.155405" in record.getMessage()


# ---- 接到插件：adj_factors ----


def _ms(day: date) -> int:
    from datetime import datetime  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    return int(
        datetime(day.year, day.month, day.day, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000
    )


class _Backend:
    def __init__(self, bars: list[RawBar], divids: list[RawDivid]) -> None:
        self._bars, self._divids = bars, divids

    def sector(self, name: str) -> list[str]:
        return []

    def expired_a_shares(self) -> list[str]:
        return []

    def instrument(self, symbol: str) -> None:
        return None

    def daily(self, symbol: str, start: str | None) -> list[RawBar]:
        return self._bars

    def divid(self, symbol: str) -> list[RawDivid]:
        return self._divids

    def trading_dates(self, market: str) -> list[int]:
        return [_ms(d) for d in CAL]


def _raw(day: date, close: float, pre_close: float | None) -> RawBar:
    return RawBar(_ms(day), close, close, close, close, 100.0, close * 1e4, pre_close)


def test_adj_factors_adopts_preclose_ratio() -> None:
    """603007.SH 类：因子用 A，之后的因子在 A 上累乘。"""
    bars = [
        _raw(CAL[7], 15.39, 15.0),
        _raw(CAL[9], 8.0, 8.28),  # 间隔 2
        _raw(CAL[10], 10.0, 8.0),
        _raw(CAL[11], 9.0, 9.0),
    ]
    divids = [
        RawDivid(_ms(CAL[9]), 0.0, 1.155346, 0.0, 0.0, 0.0, 0, 1.155405),
        RawDivid(_ms(CAL[11]), 1.0, 0.0, 0.0, 0.0, 0.0, 0, 10 / 9),
    ]
    df = XtdataSource(_Backend(bars, divids)).adj_factors(["603007.SH"], CAL[0], CAL[-1])
    assert df["date"].to_list() == [CAL[9], CAL[11]]
    a = 15.39 / 8.28
    assert df["factor"].to_list() == [pytest.approx(a), pytest.approx(a * 10 / 9)]


def test_adj_factors_without_preclose_uses_formula() -> None:
    """旧录制文件没有 preClose：只用事件公式。"""
    bars = [_raw(CAL[7], 15.39, None), _raw(CAL[9], 8.0, None)]
    divids = [RawDivid(_ms(CAL[9]), 0.0, 1.155346, 0.0, 0.0, 0.0, 0, 1.155405)]
    df = XtdataSource(_Backend(bars, divids)).adj_factors(["603007.SH"], CAL[0], CAL[-1])
    assert df["factor"].to_list() == [pytest.approx(15.39 / 7.14)]
