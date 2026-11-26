"""插件逻辑的单元测试：用手工构造的后端数据覆盖边界情况。不访问网络。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from duohelm_sdk import Freq, load_manifest

from duohelm_eltdx import plugin
from duohelm_eltdx.backend import (
    PAGE_SIZE,
    BarKind,
    CapitalRecord,
    CodeRow,
    DailyBar,
    IndexMemberRow,
    LiveBackend,
)
from duohelm_eltdx.codes import InvalidCodeError, to_eltdx, to_symbol
from duohelm_eltdx.source import EltdxSource

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def bar(d: date, close: float = 10.0, volume: float = 1000.0) -> DailyBar:
    milli = round(close * 1000)
    return DailyBar(d, milli, milli + 100, milli - 100, milli, volume, volume * close)


class FakeBackend:
    def __init__(self) -> None:
        self.codes = [CodeRow("sz000001", "平安银行"), CodeRow("sh600000", "浦发银行")]
        self.series: dict[tuple[str, BarKind], list[DailyBar]] = {}
        self.capital: dict[str, list[CapitalRecord]] = {}
        self.members: dict[str, list[IndexMemberRow]] = {}
        self.page_calls: list[tuple[str, BarKind, int]] = []

    def code_table(self) -> list[CodeRow]:
        return self.codes

    def daily_page(self, full_code: str, kind: BarKind, offset: int) -> list[DailyBar]:
        self.page_calls.append((full_code, kind, offset))
        series = self.series.get((full_code, kind), [])
        end = len(series) - offset
        return series[max(0, end - PAGE_SIZE) : max(0, end)]

    def capital_changes(self, full_code: str) -> list[CapitalRecord]:
        return self.capital.get(full_code, [])

    def index_members(self, index_code: str) -> list[IndexMemberRow]:
        return self.members.get(index_code, [])


def days(start: date, n: int) -> list[date]:
    return [start + timedelta(days=i) for i in range(n)]


# ---- 代码转换 ----


@pytest.mark.parametrize(
    ("eltdx", "symbol"),
    [("sz000001", "000001.SZ"), ("sh600000", "600000.SH"), ("bj920000", "920000.BJ")],
)
def test_code_roundtrip(eltdx: str, symbol: str) -> None:
    assert to_symbol(eltdx) == symbol
    assert to_eltdx(symbol) == eltdx


@pytest.mark.parametrize("bad", ["000001", "hk00700", "sz00001", "000001.HK", "600000.sh."])
def test_bad_codes(bad: str) -> None:
    with pytest.raises(InvalidCodeError):
        if "." in bad:
            to_eltdx(bad)
        else:
            to_symbol(bad)


# ---- 清单与导入 ----


def test_manifest_valid() -> None:
    m = load_manifest(PLUGIN_DIR)
    assert m.id == "eltdx"
    assert m.plugin.kind == "datasource"
    assert m.supports_sdk("0.2.0")
    assert "static.tdx.com.cn:7615" in m.permissions.network
    assert sum(h.endswith(":7709") for h in m.permissions.network) >= 40


def test_import_does_not_connect() -> None:
    backend = plugin._backend  # pyright: ignore[reportPrivateUsage]
    assert isinstance(backend, LiveBackend)
    assert backend._client is None  # pyright: ignore[reportPrivateUsage]


# ---- 证券列表 ----


def test_include_delisted_not_supported() -> None:
    with pytest.raises(NotImplementedError):
        EltdxSource(FakeBackend()).securities(include_delisted=True)


def test_lookup_routes_and_dates() -> None:
    fake = FakeBackend()
    fake.series[("sh600087", "delisted")] = [bar(d) for d in days(date(2010, 1, 1), 5)]
    fake.series[("sz000001", "delisted")] = [bar(d) for d in days(date(2020, 1, 1), 3)]
    df = EltdxSource(fake).lookup_securities(["600087.SH", "000001.SZ", "609999.SH"])
    got = {r["symbol"]: r for r in df.iter_rows(named=True)}
    assert set(got) == {"600087.SH", "000001.SZ"}
    assert got["600087.SH"]["delist_date"] == date(2010, 1, 5)
    assert got["600087.SH"]["name"] is None
    assert got["000001.SZ"]["delist_date"] is None  # 仍在代码表里
    assert got["000001.SZ"]["name"] == "平安银行"


def test_lookup_dates_ignore_zero_volume_placeholders() -> None:
    """上市日、退市日只看有成交的 K 线：首尾的停牌占位行（成交量为 0）不算。"""
    d = days(date(2014, 4, 21), 6)
    fake = FakeBackend()
    fake.series[("sh601268", "delisted")] = [
        bar(d[0], volume=0.0),
        bar(d[1]),
        bar(d[2]),
        bar(d[3]),
        bar(d[4], volume=0.0),
        bar(d[5], volume=0.0),
    ]
    row = EltdxSource(fake).lookup_securities(["601268.SH"]).row(0, named=True)
    assert row["list_date"] == d[1]
    assert row["delist_date"] == d[3]


def test_lookup_skips_code_with_only_placeholders() -> None:
    fake = FakeBackend()
    fake.series[("sh601268", "delisted")] = [bar(date(2014, 4, 21), volume=0.0)]
    assert EltdxSource(fake).lookup_securities(["601268.SH"]).height == 0


# ---- 日线 ----


def test_paging_stops_once_start_is_covered() -> None:
    fake = FakeBackend()
    series = [bar(d) for d in days(date(2020, 1, 1), PAGE_SIZE * 3)]
    fake.series[("sz000001", "stock")] = series
    start = series[-PAGE_SIZE - 10].date  # 在倒数第二页里
    df = EltdxSource(fake).bars(["000001.SZ"], Freq.DAY, start, series[-1].date)
    assert df.height == PAGE_SIZE + 10
    assert [offset for _, _, offset in fake.page_calls] == [0, PAGE_SIZE]  # 只翻两页


def test_paging_reads_full_history() -> None:
    fake = FakeBackend()
    series = [bar(d) for d in days(date(2000, 1, 1), PAGE_SIZE * 2 + 5)]
    fake.series[("sz000001", "stock")] = series
    df = EltdxSource(fake).bars(["000001.SZ"], Freq.DAY, date(1990, 1, 1), date(2030, 1, 1))
    assert df.height == len(series)
    assert df["date"].is_sorted()


def test_zero_volume_rows_dropped() -> None:
    fake = FakeBackend()
    d = days(date(2024, 1, 1), 3)
    fake.series[("sz000001", "stock")] = [bar(d[0]), bar(d[1], volume=0.0), bar(d[2])]
    df = EltdxSource(fake).bars(["000001.SZ"], Freq.DAY, d[0], d[2])
    assert df["date"].to_list() == [d[0], d[2]]


def test_delisted_symbols_use_delisted_api() -> None:
    fake = FakeBackend()
    fake.series[("sh600087", "delisted")] = [bar(date(2014, 6, 4))]
    df = EltdxSource(fake).bars(["600087.SH"], Freq.DAY, date(2014, 1, 1), date(2014, 12, 31))
    assert df.height == 1
    assert ("sh600087", "stock", 0) not in fake.page_calls


def test_prices_from_milli() -> None:
    fake = FakeBackend()
    fake.series[("sz000001", "stock")] = [
        DailyBar(date(2024, 1, 2), 12200, 12510, 12150, 12340, 1.0, 2.0)
    ]
    row = (
        EltdxSource(fake)
        .bars(["000001.SZ"], Freq.DAY, date(2024, 1, 1), date(2024, 1, 3))
        .row(0, named=True)
    )
    assert (row["open"], row["high"], row["low"], row["close"]) == (12.2, 12.51, 12.15, 12.34)


def test_only_daily_and_cn() -> None:
    source = EltdxSource(FakeBackend())
    with pytest.raises(ValueError, match="日线"):
        source.bars(["000001.SZ"], Freq.MINUTE, date(2024, 1, 1), date(2024, 1, 2))
    with pytest.raises(ValueError, match="CN"):
        source.trading_calendar("US", date(2024, 1, 1), date(2024, 1, 2))


def test_calendar_is_union_of_exchanges() -> None:
    fake = FakeBackend()
    fake.series[("sh000001", "index")] = [bar(date(1991, 4, 5))]
    fake.series[("sz399001", "index")] = [
        bar(date(1991, 4, 5)),
        bar(date(1991, 4, 6)),
    ]  # 深市周六开市
    cal = EltdxSource(fake).trading_calendar("CN", date(1991, 1, 1), date(1991, 12, 31))
    assert cal["date"].to_list() == [date(1991, 4, 5), date(1991, 4, 6)]


# ---- 复权因子 ----


def test_adj_factors_convert_per_10_and_filter_category() -> None:
    fake = FakeBackend()
    d = days(date(2024, 6, 1), 4)
    fake.series[("sz000001", "stock")] = [
        bar(d[0], 10.0),
        bar(d[1], 10.0),
        bar(d[2], 9.0),
        bar(d[3], 9.0),
    ]
    fake.capital["sz000001"] = [
        CapitalRecord(d[2], 1, 10.0, 0.0, 0.0, 0.0),  # 每 10 股派 10 元 = 每股 1 元
        CapitalRecord(d[3], 5, 0.0, 0.0, 99.0, 99.0),  # 股本变化：不参与复权
    ]
    df = EltdxSource(fake).adj_factors(["000001.SZ"], date(2024, 1, 1), date(2024, 12, 31))
    assert df["date"].to_list() == [d[2]]
    assert df["factor"].to_list() == [pytest.approx(10 / 9)]


def test_adj_factors_bonus_and_rights_mapping() -> None:
    fake = FakeBackend()
    d = days(date(2024, 6, 1), 2)
    fake.series[("sz000001", "stock")] = [bar(d[0], 10.0), bar(d[1], 5.0)]
    # 每 10 股送转 10 股（c3），配 3 股（c4），配股价 5 元（c2）
    fake.capital["sz000001"] = [CapitalRecord(d[1], 1, 0.0, 5.0, 10.0, 3.0)]
    df = EltdxSource(fake).adj_factors(["000001.SZ"], date(2024, 1, 1), date(2024, 12, 31))
    ref = (10 + 5 * 0.3) / (1 + 1.0 + 0.3)
    assert df["factor"].to_list() == [pytest.approx(10 / ref)]


def test_adj_factors_ignore_suspended_placeholder_close() -> None:
    """停牌占位行（成交量 0）不能当作前收盘价。"""
    fake = FakeBackend()
    d = days(date(2024, 6, 1), 3)
    fake.series[("sz000001", "stock")] = [
        bar(d[0], 10.0),
        bar(d[1], 99.0, volume=0.0),
        bar(d[2], 9.0),
    ]
    fake.capital["sz000001"] = [CapitalRecord(d[2], 1, 10.0, 0.0, 0.0, 0.0)]
    df = EltdxSource(fake).adj_factors(["000001.SZ"], date(2024, 1, 1), date(2024, 12, 31))
    assert df["factor"].to_list() == [pytest.approx(10 / 9)]


# ---- 指数成分 ----


def test_index_constituents_current_only() -> None:
    fake = FakeBackend()
    fake.members["000300"] = [
        IndexMemberRow("sh600000", date(2005, 4, 8)),
        IndexMemberRow("sz000001", date(2030, 1, 1)),  # 晚于 end：排除
    ]
    df = EltdxSource(fake).index_constituents("000300.SH", date(2005, 1, 1), date(2025, 1, 1))
    assert df["symbol"].to_list() == ["600000.SH"]
    assert df["out_date"].null_count() == 1
    assert df.schema == pl.Schema({"symbol": pl.String, "in_date": pl.Date, "out_date": pl.Date})
