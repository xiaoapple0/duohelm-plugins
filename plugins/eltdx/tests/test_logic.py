"""插件逻辑的单元测试：用手工构造的后端数据覆盖边界情况。不访问网络。"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest
from duohelm_sdk import SDK_VERSION, Freq, LookupFailedError, load_manifest
from duohelm_sdk.schema import SECURITIES_SCHEMA

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
from duohelm_eltdx.units import (
    SHARES_PER_VOLUME_UNIT,
    UnknownSecurityTypeError,
    security_type,
    shares_per_volume_unit,
)

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
    assert m.supports_sdk(SDK_VERSION) and m.supports_sdk("0.4.2")
    assert not m.supports_sdk("0.4.1")  # LookupFailedError 从 0.4.2 起才有
    assert not m.supports_sdk("0.5.0")
    assert "static.tdx.com.cn:7615" in m.permissions.network
    assert sum(h.endswith(":7709") for h in m.permissions.network) >= 40


def test_import_does_not_connect() -> None:
    backend = plugin._backend  # pyright: ignore[reportPrivateUsage]
    assert isinstance(backend, LiveBackend)
    assert backend._client is None  # pyright: ignore[reportPrivateUsage]


# ---- 证券列表 ----


def test_capabilities_declare_approximate_delist_date() -> None:
    caps = EltdxSource(FakeBackend()).capabilities()
    assert caps.lists_delisted is False
    assert caps.exact_delist_date is False  # 拿不到摘牌日（duohelm-plugins#7）


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


def test_lookup_delist_date_with_mid_suspension_and_trailing_placeholders() -> None:
    """中间停牌不影响；末尾多根占位行时，退市日取最后一根有成交的 K 线；在市股票不给退市日。"""
    d = days(date(2014, 4, 21), 8)
    fake = FakeBackend()
    fake.series[("sh601268", "delisted")] = [
        bar(d[0]),
        bar(d[1], volume=0.0),  # 中间停牌
        bar(d[2], volume=0.0),
        bar(d[3]),
        bar(d[4], volume=0.0),  # 末尾占位行
        bar(d[5], volume=0.0),
        bar(d[6], volume=0.0),
        bar(d[7], volume=0.0),
    ]
    fake.series[("sz000001", "delisted")] = [bar(d[0]), bar(d[1]), bar(d[2], volume=0.0)]
    df = EltdxSource(fake).lookup_securities(["601268.SH", "000001.SZ"])
    got = {r["symbol"]: r for r in df.iter_rows(named=True)}
    assert got["601268.SH"]["delist_date"] == d[3]
    assert got["000001.SZ"]["delist_date"] is None
    assert got["000001.SZ"]["list_date"] == d[0]


def test_lookup_skips_code_with_only_placeholders() -> None:
    fake = FakeBackend()
    fake.series[("sh601268", "delisted")] = [bar(date(2014, 4, 21), volume=0.0)]
    assert EltdxSource(fake).lookup_securities(["601268.SH"]).height == 0


# ---- lookup_securities 查询失败：抛 LookupFailedError，不返回空或部分结果 ----


class _BoomError(RuntimeError):
    pass


def _delisted_backend() -> FakeBackend:
    fake = FakeBackend()
    fake.series[("sh600087", "delisted")] = [bar(d) for d in days(date(2010, 1, 1), 5)]
    fake.series[("sh601268", "delisted")] = [bar(d) for d in days(date(2012, 1, 1), 5)]
    return fake


def test_lookup_code_table_failure_raises_lookup_failed() -> None:
    fake = _delisted_backend()
    boom = _BoomError("连不上")

    def broken() -> list[CodeRow]:
        raise boom

    fake.code_table = broken  # type: ignore[method-assign]
    with pytest.raises(LookupFailedError) as info:
        EltdxSource(fake).lookup_securities(["600087.SH"])
    assert info.value.__cause__ is boom


def test_lookup_connection_error_raises_lookup_failed() -> None:
    fake = _delisted_backend()

    def broken(full_code: str, kind: BarKind, offset: int) -> list[DailyBar]:
        raise ConnectionError("无法连接通达信主站")

    fake.daily_page = broken  # type: ignore[method-assign]
    with pytest.raises(LookupFailedError) as info:
        EltdxSource(fake).lookup_securities(["600087.SH"])
    assert isinstance(info.value.__cause__, ConnectionError)


def test_lookup_partial_failure_raises_instead_of_partial_result() -> None:
    """第一个代码查到了、第二个代码出错：整体抛异常，不返回只含第一个代码的结果。"""
    fake = _delisted_backend()
    original = fake.daily_page

    def flaky(full_code: str, kind: BarKind, offset: int) -> list[DailyBar]:
        if full_code == "sh601268":
            raise TimeoutError("超时")
        return original(full_code, kind, offset)

    fake.daily_page = flaky  # type: ignore[method-assign]
    with pytest.raises(LookupFailedError) as info:
        EltdxSource(fake).lookup_securities(["600087.SH", "601268.SH"])
    assert isinstance(info.value.__cause__, TimeoutError)


def test_lookup_malformed_response_raises_lookup_failed() -> None:
    """返回结构异常（这里是 K 线没有 volume 属性）也是查询失败。"""
    fake = _delisted_backend()
    fake.daily_page = lambda *_a: [object()]  # type: ignore[method-assign, list-item, return-value]
    with pytest.raises(LookupFailedError) as info:
        EltdxSource(fake).lookup_securities(["600087.SH"])
    assert isinstance(info.value.__cause__, AttributeError)


def test_lookup_too_many_pages_raises_lookup_failed() -> None:
    fake = FakeBackend()
    fake.daily_page = lambda *_a: [bar(date(2020, 1, 1))] * PAGE_SIZE  # type: ignore[method-assign]
    with pytest.raises(LookupFailedError) as info:
        EltdxSource(fake).lookup_securities(["600087.SH"])
    assert isinstance(info.value.__cause__, RuntimeError)


def test_lookup_empty_code_table_raises_lookup_failed() -> None:
    """代码表为空不可信：否则在市股票会被当成退市股。"""
    fake = _delisted_backend()
    fake.codes = []
    with pytest.raises(LookupFailedError):
        EltdxSource(fake).lookup_securities(["600087.SH"])


def test_lookup_confirmed_absent_returns_empty_without_raising() -> None:
    """接口正常返回空：确认数据源没有这个代码，返回空表，不抛异常。"""
    df = EltdxSource(FakeBackend()).lookup_securities(["609999.SH"])
    assert df.height == 0
    assert dict(df.schema) == SECURITIES_SCHEMA


def test_lookup_invalid_symbol_is_not_a_lookup_failure() -> None:
    with pytest.raises(InvalidCodeError):
        EltdxSource(FakeBackend()).lookup_securities(["abc"])


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


# ---- 成交量单位（duohelm-plugins#3） ----


def _series(volume: float) -> list[DailyBar]:
    return [bar(date(2024, 6, 3), volume=volume)]


def test_stock_volume_unchanged() -> None:
    """个股的 volume_wire_value 已经是股数，不换算。"""
    fake = FakeBackend()
    fake.series[("sz000001", "stock")] = _series(1_234_500.0)
    df = EltdxSource(fake).bars(["000001.SZ"], Freq.DAY, date(2024, 6, 1), date(2024, 6, 30))
    assert df["volume"].to_list() == [1_234_500.0]


def test_bj_volume_unchanged() -> None:
    fake = FakeBackend()
    fake.codes.append(CodeRow("bj920000", "x"))
    fake.series[("bj920000", "stock")] = _series(5000.0)
    df = EltdxSource(fake).bars(["920000.BJ"], Freq.DAY, date(2024, 6, 1), date(2024, 6, 30))
    assert df["volume"].to_list() == [5000.0]


@pytest.mark.parametrize(("symbol", "code"), [("510300.SH", "sh510300"), ("159919.SZ", "sz159919")])
def test_etf_volume_unchanged_via_delisted_api(symbol: str, code: str) -> None:
    """ETF 不在 latest_stock_list 代码表里，走真实路径：退市日线接口，成交量乘 1。

    这个接口能不能返回在市 ETF、单位是不是股，都没有核对过（见 units.py）。
    """
    fake = FakeBackend()
    fake.series[(code, "delisted")] = _series(5000.0)
    df = EltdxSource(fake).bars([symbol], Freq.DAY, date(2024, 6, 1), date(2024, 6, 30))
    assert df["volume"].to_list() == [5000.0]
    assert {kind for _, kind, _ in fake.page_calls} == {"delisted"}


@pytest.mark.parametrize(("symbol", "code"), [("000001.SH", "sh000001"), ("399001.SZ", "sz399001")])
def test_index_volume_converted_from_lots_to_shares(symbol: str, code: str) -> None:
    """指数的 volume_wire_value 是手：乘 100 换成股，并走指数接口（不是个股/退市接口）。"""
    fake = FakeBackend()
    fake.series[(code, "index")] = _series(3_000_000.0)
    df = EltdxSource(fake).bars([symbol], Freq.DAY, date(2024, 6, 1), date(2024, 6, 30))
    assert df["volume"].to_list() == [300_000_000.0]
    assert {kind for _, kind, _ in fake.page_calls} == {"index"}


@pytest.mark.parametrize(
    "symbol",
    [
        "110059.SH",
        "127045.SZ",
        "900901.SH",
        "200011.SZ",
        "204001.SH",
        "899050.BJ",  # 北证 50 指数：没有核对过，不能当股票
        "520000.SH",  # 沪 52 开头：没有核对过
        "161005.SZ",  # LOF
        "150001.SZ",  # 分级基金
        "501018.SH",  # LOF
        "161725.SZ",  # LOF
    ],
)
def test_unknown_type_is_rejected_not_defaulted(symbol: str) -> None:
    """债券、可转债、B 股等类型未知：不默认成股也不默认成手，拒绝返回，且不取数。"""
    fake = FakeBackend()
    with pytest.raises(UnknownSecurityTypeError, match="类型未知"):
        EltdxSource(fake).bars([symbol], Freq.DAY, date(2024, 6, 1), date(2024, 6, 30))
    assert fake.page_calls == []


def test_unknown_type_still_gets_adj_factors() -> None:
    """复权因子只用价格，不受成交量单位影响：类型未知不应让它失败。"""
    fake = FakeBackend()
    d = days(date(2024, 6, 1), 2)
    fake.series[("sh110059", "delisted")] = [bar(d[0], 100.0), bar(d[1], 99.0)]
    fake.capital["sh110059"] = [CapitalRecord(d[1], 1, 10.0, 0.0, 0.0, 0.0)]
    df = EltdxSource(fake).adj_factors(["110059.SH"], date(2024, 1, 1), date(2024, 12, 31))
    assert df.height == 1


@pytest.mark.parametrize(
    ("symbol", "kind"),
    [
        ("000001.SZ", "stock"),
        ("300750.SZ", "stock"),
        ("600519.SH", "stock"),
        ("688981.SH", "stock"),
        ("920000.BJ", "bj"),
        ("899050.BJ", "unknown"),
        ("430047.BJ", "unknown"),  # 北交所老代码，录制数据里没有
        ("510300.SH", "etf"),
        ("518880.SH", "etf"),
        ("563300.SH", "etf"),
        ("588000.SH", "etf"),
        ("159919.SZ", "etf"),
        ("302132.SZ", "stock"),  # 创业板 30 开头：300、301、302 都是股票
        ("520000.SH", "unknown"),
        ("161005.SZ", "unknown"),
        ("150001.SZ", "unknown"),
        ("501018.SH", "unknown"),
        ("161725.SZ", "unknown"),
        ("000001.SH", "index"),  # 上证指数；同样的 000001 在深交所是平安银行
        ("000300.SH", "index"),
        ("399001.SZ", "index"),
        ("110059.SH", "unknown"),
        ("127045.SZ", "unknown"),
        ("900901.SH", "unknown"),
        ("200011.SZ", "unknown"),
    ],
)
def test_security_type(symbol: str, kind: str) -> None:
    assert security_type(symbol) == kind
    if kind == "unknown":
        assert kind not in SHARES_PER_VOLUME_UNIT
        with pytest.raises(UnknownSecurityTypeError):
            shares_per_volume_unit(symbol)
    else:
        assert shares_per_volume_unit(symbol) == (100 if kind == "index" else 1)
