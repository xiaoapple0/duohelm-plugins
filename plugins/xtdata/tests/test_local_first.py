"""日线先读本地、缺数据才下载（duohelm-plugins#20）的单元测试。

假的存储和假的 xtdata，不连接 MiniQMT，不 import xtquant。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from duohelm_sdk import Freq

from duohelm_xtdata.backend import Instrument, LiveBackend, RawBar, RawDivid
from duohelm_xtdata.config import MiniQMTConfig
from duohelm_xtdata.localfirst import (
    DailyDownloadError,
    LocalFirstDaily,
    expected_last_day,
    is_stale,
)
from duohelm_xtdata.source import XtdataSource

SH = ZoneInfo("Asia/Shanghai")
SYM = "000001.SZ"


def ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=SH).timestamp() * 1000)


def bar(day: date, close: float = 10.0) -> RawBar:
    return RawBar(ms(day), close, close + 0.1, close - 0.1, close, 100.0, close * 1e4, close)


# 2026-11-23（周一）起的工作日当作交易日
DAYS = [d for d in (date(2026, 11, 23) + timedelta(days=i) for i in range(15)) if d.weekday() < 5]
FRI = date(2026, 11, 27)
MON = date(2026, 11, 30)


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=SH)


def history(last: date) -> list[RawBar]:
    return [bar(d) for d in DAYS if d <= last]


class FakeStore:
    """local：本地现有的；remote：下载之后本地的样子（没有列出的证券下载后不变）。"""

    def __init__(self) -> None:
        self.local: dict[str, list[RawBar]] = {}
        self.remote: dict[str, list[RawBar]] = {}
        self.download_errors: dict[str, Exception] = {}
        self.read_error: Exception | None = None
        self.calendar: list[date] = list(DAYS)
        self.reads: list[tuple[str, str | None]] = []
        self.downloads: list[tuple[str, str | None]] = []

    def read_daily(self, symbol: str, start: str | None) -> list[RawBar]:
        self.reads.append((symbol, start))
        if self.read_error is not None:
            raise self.read_error
        return list(self.local.get(symbol, []))

    def download_daily(self, symbol: str, start: str | None) -> None:
        self.downloads.append((symbol, start))
        if symbol in self.download_errors:
            raise self.download_errors[symbol]
        if symbol in self.remote:
            self.local[symbol] = list(self.remote[symbol])

    def trading_dates(self, market: str) -> list[int]:
        return [ms(d) for d in self.calendar]


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make(store: FakeStore, now: datetime | None = None) -> tuple[LocalFirstDaily, Clock]:
    clock = Clock(now or at(FRI, 16))
    return LocalFirstDaily(store, clock), clock


# ---- 判定规则 ----


def test_expected_last_day_rules() -> None:
    assert expected_last_day(DAYS, at(FRI, 16)) == FRI  # 收盘后：当天
    assert expected_last_day(DAYS, at(FRI, 10)) == date(2026, 11, 26)  # 盘中：前一个交易日
    assert expected_last_day(DAYS, at(date(2026, 11, 28), 12)) == FRI  # 周六：上周五
    assert expected_last_day(DAYS, at(MON, 9)) == FRI  # 周一开盘前：上周五
    assert expected_last_day([], at(FRI, 16)) is None


def test_is_stale_rules() -> None:
    assert is_stale([], FRI)
    assert is_stale(history(date(2026, 11, 26)), FRI)
    assert not is_stale(history(FRI), FRI)
    assert is_stale(history(FRI), None)  # 不知道应有的最近交易日：保守地当成缺数据


# ---- 先读本地 ----


def test_local_data_present_does_not_download() -> None:
    store = FakeStore()
    store.local[SYM] = history(FRI)
    daily, _ = make(store)
    assert daily.daily(SYM, "20261101") == history(FRI)
    assert store.downloads == []
    assert store.reads == [(SYM, "20261101")]


def test_local_empty_downloads_once_then_rereads() -> None:
    store = FakeStore()
    store.remote[SYM] = history(FRI)
    daily, _ = make(store)
    assert daily.daily(SYM, None) == history(FRI)
    assert store.downloads == [(SYM, None)]
    assert len(store.reads) == 2  # 下载前读一次，下载后重读一次


def test_last_bar_before_latest_trading_day_downloads() -> None:
    store = FakeStore()
    store.local[SYM] = history(date(2026, 11, 25))
    store.remote[SYM] = history(FRI)
    daily, _ = make(store)
    assert daily.daily(SYM, None) == history(FRI)
    assert len(store.downloads) == 1


def test_today_bar_not_required_before_market_data_ready() -> None:
    """盘中，当天的日线还不会有：本地最后一根是前一个交易日，不算缺数据。"""
    store = FakeStore()
    store.local[SYM] = history(date(2026, 11, 26))
    daily, _ = make(store, at(FRI, 10))
    daily.daily(SYM, None)
    assert store.downloads == []


# ---- 停牌：下载后仍早于最近交易日，不循环、不报错 ----


def test_suspended_symbol_downloads_once_per_trading_day_and_does_not_loop() -> None:
    store = FakeStore()
    stuck = history(date(2026, 11, 20))
    store.local[SYM] = list(stuck)
    store.remote[SYM] = list(stuck)  # 下载后还是停牌前的最后一根
    daily, clock = make(store)
    assert daily.daily(SYM, None) == stuck  # 不报错，原样返回
    assert len(store.downloads) == 1
    for _ in range(3):  # 同一个应有的最近交易日内再读（如 bars 之后的 adj_factors）
        assert daily.daily(SYM, None) == stuck
    assert len(store.downloads) == 1
    clock.now = at(MON, 16)  # 新的交易日：再试一次
    daily.daily(SYM, None)
    assert len(store.downloads) == 2


def test_empty_after_successful_download_is_empty_not_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """下载成功、MiniQMT 仍没有这只证券的日线（如早年退市）：确实没有，返回空，不重复下载。"""
    store = FakeStore()
    daily, _ = make(store)
    with caplog.at_level(logging.INFO, logger="duohelm_xtdata.localfirst"):
        assert daily.daily("600087.SH", None) == []
        assert daily.daily("600087.SH", None) == []
    assert len(store.downloads) == 1
    assert "仍没有日线" in caplog.text


# ---- 未知不是空：下载失败 ----


def test_download_failure_with_stale_local_raises_instead_of_returning_old_data() -> None:
    store = FakeStore()
    store.local[SYM] = history(date(2026, 11, 25))
    store.download_errors[SYM] = TimeoutError("下载超时")
    daily, _ = make(store)
    with pytest.raises(DailyDownloadError, match="不使用旧数据") as info:
        daily.daily(SYM, None)
    assert isinstance(info.value.__cause__, TimeoutError)


def test_download_failure_with_empty_local_is_not_treated_as_no_data() -> None:
    store = FakeStore()
    store.download_errors[SYM] = ConnectionError("10054")
    daily, _ = make(store)
    with pytest.raises(DailyDownloadError, match="结果未知"):
        daily.daily(SYM, None)


def test_failed_download_is_retried_next_time() -> None:
    """失败不记入“已下载”：下次读取照常重试。"""
    store = FakeStore()
    store.download_errors[SYM] = ConnectionError("10054")
    daily, _ = make(store)
    for _ in range(2):
        with pytest.raises(DailyDownloadError):
            daily.daily(SYM, None)
    assert len(store.downloads) == 2
    del store.download_errors[SYM]
    store.remote[SYM] = history(FRI)
    assert daily.daily(SYM, None) == history(FRI)


def test_read_failure_propagates() -> None:
    store = FakeStore()
    store.read_error = RuntimeError("读取失败")
    daily, _ = make(store)
    with pytest.raises(RuntimeError, match="读取失败"):
        daily.daily(SYM, None)
    assert store.downloads == []


def test_empty_calendar_falls_back_to_download() -> None:
    store = FakeStore()
    store.calendar = []
    store.local[SYM] = history(FRI)
    store.remote[SYM] = history(FRI)
    daily, _ = make(store)
    daily.daily(SYM, None)
    assert len(store.downloads) == 1


def test_calendar_refreshed_on_new_day() -> None:
    store = FakeStore()
    store.local[SYM] = history(FRI)
    daily, clock = make(store)
    daily.daily(SYM, None)
    store.calendar = [*DAYS, date(2026, 12, 7)]
    clock.now = at(date(2026, 12, 7), 16)
    daily.daily(SYM, None)  # 本地最后一根是 11-27，应有 12-07：缺数据
    assert len(store.downloads) == 1


# ---- 通过 XtdataSource：bars 和 adj_factors ----


class StoreBackend:
    """XtdataSource 用的后端：daily 走 LocalFirstDaily，其余手工构造。"""

    def __init__(self, store: FakeStore, local_first: LocalFirstDaily) -> None:
        self.store = store
        self._local_first = local_first
        self.divids: dict[str, list[RawDivid]] = {}

    def daily(self, symbol: str, start: str | None) -> list[RawBar]:
        return self._local_first.daily(symbol, start)

    def divid(self, symbol: str) -> list[RawDivid]:
        return self.divids.get(symbol, [])

    def trading_dates(self, market: str) -> list[int]:
        return self.store.trading_dates(market)

    def sector(self, name: str) -> list[str]:
        return []

    def expired_a_shares(self) -> list[str]:
        return []

    def instrument(self, symbol: str) -> Instrument | None:
        return None


def _source(store: FakeStore) -> XtdataSource:
    daily, _ = make(store)
    backend = StoreBackend(store, daily)
    backend.divids[SYM] = [RawDivid(ms(FRI), 1.0, 0.0, 0.0, 0.0, 0.0, 0, 10 / 9)]
    return XtdataSource(backend)


def _two_day_history() -> list[RawBar]:
    return [bar(date(2026, 11, 26), 10.0), RawBar(ms(FRI), 9.0, 9.1, 8.9, 9.0, 100.0, 9e4, 9.0)]


def test_adj_factors_with_fresh_local_data_does_not_download() -> None:
    store = FakeStore()
    store.local[SYM] = _two_day_history()
    source = _source(store)
    df = source.adj_factors([SYM], date(2026, 1, 1), date(2026, 12, 31))
    assert df["factor"].to_list() == [pytest.approx(10 / 9)]
    assert store.downloads == []


def test_bars_then_adj_factors_download_a_stale_symbol_only_once() -> None:
    """修复前每只证券下载两次（bars 一次、adj_factors 一次）。"""
    store = FakeStore()
    store.local[SYM] = [bar(date(2026, 11, 26))]
    store.remote[SYM] = _two_day_history()
    source = _source(store)
    df = source.bars([SYM], Freq.DAY, date(2026, 11, 1), date(2026, 12, 31))
    assert df.height == 2
    source.adj_factors([SYM], date(2026, 1, 1), date(2026, 12, 31))
    assert len(store.downloads) == 1


def test_bars_download_failure_fails_instead_of_returning_empty() -> None:
    store = FakeStore()
    store.download_errors[SYM] = TimeoutError("超时")
    source = _source(store)
    with pytest.raises(DailyDownloadError):
        source.bars([SYM], Freq.DAY, date(2026, 11, 1), date(2026, 12, 31))


# ---- LiveBackend：传给 xtdata 的调用 ----


class _Frame:
    def __init__(self, rows: list[dict[str, float]]) -> None:
        self._rows = rows

    def __len__(self) -> int:
        return len(self._rows)

    def iterrows(self) -> list[tuple[int, dict[str, float]]]:
        return list(enumerate(self._rows))


class _FakeXt:
    def __init__(self, local: dict[str, list[date]], remote: dict[str, list[date]]) -> None:
        self.local = local
        self.remote = remote
        self.calls: list[tuple[str, Any]] = []
        self.fail_download = False

    def get_trading_dates(self, market: str) -> list[int]:
        self.calls.append(("get_trading_dates", market))
        return [ms(d) for d in DAYS]

    def download_history_data(self, symbol: str, period: str, start: str, end: str) -> None:
        self.calls.append(("download", symbol))
        if self.fail_download:
            raise RuntimeError("download failed")
        self.local[symbol] = self.remote.get(symbol, self.local.get(symbol, []))

    def get_market_data_ex(self, fields: list[str], symbols: list[str], **kwargs: Any) -> Any:
        self.calls.append(("read", list(symbols)))
        rows = [
            {
                "time": float(ms(d)),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 1.0,
                "amount": 100.0,
                "preClose": 1.0,
            }
            for d in self.local.get(symbols[0], [])
        ]
        return {symbols[0]: _Frame(rows)}


def _live(monkeypatch: pytest.MonkeyPatch, fake: _FakeXt) -> LiveBackend:
    monkeypatch.setattr(LiveBackend, "_get", lambda self: fake)  # pyright: ignore[reportUnknownLambdaType]
    return LiveBackend(MiniQMTConfig(download_timeout_s=5), now=Clock(at(FRI, 16)))


def test_live_backend_reads_one_symbol_per_request_and_skips_download_when_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeXt({"A.SZ": [FRI], "B.SZ": [FRI]}, {})
    live = _live(monkeypatch, fake)
    live.daily("A.SZ", "20261101")
    live.daily("B.SZ", "20261101")
    assert [c for c in fake.calls if c[0] == "download"] == []
    assert [c[1] for c in fake.calls if c[0] == "read"] == [["A.SZ"], ["B.SZ"]]  # 不合并成大请求


def test_live_backend_downloads_only_stale_symbol_then_rereads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeXt({"A.SZ": [FRI], "B.SZ": [date(2026, 11, 20)]}, {"B.SZ": [FRI]})
    live = _live(monkeypatch, fake)
    live.daily("A.SZ", None)
    bars = live.daily("B.SZ", None)
    assert [c for c in fake.calls if c[0] == "download"] == [("download", "B.SZ")]
    assert [c[1] for c in fake.calls if c[0] == "read"] == [["A.SZ"], ["B.SZ"], ["B.SZ"]]
    assert len(bars) == 1


def test_live_backend_download_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeXt({}, {})
    fake.fail_download = True
    live = _live(monkeypatch, fake)
    with pytest.raises(DailyDownloadError):
        live.daily("A.SZ", None)
