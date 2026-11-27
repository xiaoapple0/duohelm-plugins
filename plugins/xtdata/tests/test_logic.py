"""xtdata 插件逻辑的单元测试：手工构造的后端数据，不连接 MiniQMT，不 import xtquant。"""

from __future__ import annotations

import sys
import time
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl
import pytest
from duohelm_sdk import (
    SDK_VERSION,
    EndpointNotConfiguredError,
    Freq,
    LookupFailedError,
    load_manifest,
)
from duohelm_sdk.schema import SECURITIES_SCHEMA

from duohelm_xtdata import plugin
from duohelm_xtdata.backend import (
    ENDPOINT_ID,
    Instrument,
    LiveBackend,
    MiniQMTUnavailableError,
    RawBar,
    RawDivid,
)
from duohelm_xtdata.config import ConfigError, load_config
from duohelm_xtdata.dates import (
    SHARES_PER_VOLUME_UNIT,
    parse_yyyymmdd,
    security_type,
    shanghai_date,
)
from duohelm_xtdata.source import XtdataSource

PLUGIN_DIR = Path(__file__).resolve().parents[1]
SH = ZoneInfo("Asia/Shanghai")


def ms(day: date) -> int:
    """北京时间当天零点的毫秒时间戳（xtdata 的日线时间戳就是这样）。"""
    return int(datetime(day.year, day.month, day.day, tzinfo=SH).timestamp() * 1000)


def raw(day: date, close: float = 10.0, volume: float = 100.0) -> RawBar:
    return RawBar(ms(day), close, close + 0.1, close - 0.1, close, volume, close * volume * 100)


class FakeBackend:
    def __init__(self) -> None:
        self.sectors: dict[str, list[str]] = {"沪深A股": ["000001.SZ"], "京市A股": []}
        self.expired: list[str] = []
        self.instruments: dict[str, Instrument] = {}
        self.bars: dict[str, list[RawBar]] = {}
        self.divids: dict[str, list[RawDivid]] = {}
        self.dates: list[date] = []
        self.daily_calls: list[tuple[str, str | None]] = []

    def sector(self, name: str) -> list[str]:
        return self.sectors.get(name, [])

    def expired_a_shares(self) -> list[str]:
        return self.expired

    def instrument(self, symbol: str) -> Instrument | None:
        return self.instruments.get(symbol)

    def daily(self, symbol: str, start: str | None) -> list[RawBar]:
        self.daily_calls.append((symbol, start))
        return self.bars.get(symbol, [])

    def divid(self, symbol: str) -> list[RawDivid]:
        return self.divids.get(symbol, [])

    def trading_dates(self, market: str) -> list[int]:
        return [ms(d) for d in self.dates]


# ---- 日期：一律由时间戳按 Asia/Shanghai 换算 ----


CATL_LISTING_MS = 1528646400000  # 2018-06-11 00:00 北京时间


def test_shanghai_date_known_listing() -> None:
    assert shanghai_date(CATL_LISTING_MS) == date(2018, 6, 11)


@pytest.mark.parametrize("zone", ["America/New_York", "UTC", "Europe/London", "Asia/Tokyo"])
def test_shanghai_date_does_not_depend_on_local_zone(
    zone: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """在非东八区时区下断言已知日期。POSIX 上真的切换进程时区；Windows 上不设置 TZ（Windows 的
    C 运行库不认识 IANA 时区名，设置后反而会把本机时区搞乱），换算显式使用 Asia/Shanghai。"""
    tzset = getattr(time, "tzset", None)  # 只有 POSIX 能切换进程时区
    if tzset is not None:
        monkeypatch.setenv("TZ", zone)
        tzset()
    try:
        assert shanghai_date(CATL_LISTING_MS) == date(2018, 6, 11)
        # 陷阱：按美东时区换算同一个时间戳，日期早一天——xtdata 的日期字符串就是这样错的
        ny = datetime.fromtimestamp(CATL_LISTING_MS / 1000, tz=ZoneInfo("America/New_York"))
        assert ny.date() == date(2018, 6, 10)
    finally:
        if tzset is not None:
            monkeypatch.delenv("TZ")
            tzset()


def test_dst_years_both_timestamp_conventions() -> None:
    """1986–1991 年中国实行夏令时（UTC+9）。按 Asia/Shanghai 换算，两种时间戳约定都得到正确
    日期：xtdata 用的“固定 +8 零点”，以及“当地（夏令时）零点”。只按固定 +8 换算，后者会错一天。"""
    fixed_plus8_midnight = int(datetime(1991, 6, 30, 16, tzinfo=UTC).timestamp() * 1000)
    local_dst_midnight = int(datetime(1991, 6, 30, 15, tzinfo=UTC).timestamp() * 1000)
    assert shanghai_date(fixed_plus8_midnight) == date(1991, 7, 1)  # xtdata 的约定
    assert shanghai_date(local_dst_midnight) == date(1991, 7, 1)
    fixed = datetime.fromtimestamp(local_dst_midnight / 1000, tz=timezone(timedelta(hours=8)))
    assert fixed.date() == date(1991, 6, 30)  # 只按固定 +8 换算会错


def test_bars_dates_from_timestamp_not_string() -> None:
    fake = FakeBackend()
    fake.bars["300750.SZ"] = [RawBar(CATL_LISTING_MS, 1.0, 1.1, 0.9, 1.0, 10.0, 1000.0)]
    df = XtdataSource(fake).bars(["300750.SZ"], Freq.DAY, date(2018, 6, 1), date(2018, 6, 30))
    assert df["date"].to_list() == [date(2018, 6, 11)]


@pytest.mark.parametrize(
    ("value", "expected"),
    [("19970612", date(1997, 6, 12)), ("99999999", None), ("0", None), (None, None), ("", None)],
)
def test_parse_yyyymmdd(value: object, expected: date | None) -> None:
    assert parse_yyyymmdd(value) == expected


# ---- 成交量单位：按证券类型 ----


@pytest.mark.parametrize(
    ("symbol", "kind"),
    [
        ("000001.SZ", "stock"),
        ("600000.SH", "stock"),
        ("688981.SH", "stock"),
        ("300750.SZ", "stock"),
        ("510300.SH", "etf"),
        ("588000.SH", "etf"),
        ("159915.SZ", "etf"),
        ("920000.BJ", "bj"),
        ("000001.SH", "index"),
        ("399001.SZ", "index"),
    ],
)
def test_security_type(symbol: str, kind: str) -> None:
    assert security_type(symbol) == kind


@pytest.mark.parametrize("symbol", ["000001.SZ", "510300.SH", "920000.BJ", "000001.SH"])
def test_volume_converted_to_shares_per_type(symbol: str) -> None:
    fake = FakeBackend()
    fake.bars[symbol] = [raw(date(2024, 6, 3), volume=12.0)]
    df = XtdataSource(fake).bars([symbol], Freq.DAY, date(2024, 6, 1), date(2024, 6, 30))
    expected = 12.0 * SHARES_PER_VOLUME_UNIT[security_type(symbol)]
    assert df["volume"].to_list() == [expected]


# ---- 日线 ----


def test_zero_volume_rows_dropped() -> None:
    fake = FakeBackend()
    d = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
    fake.bars["000001.SZ"] = [raw(d[0]), raw(d[1], volume=0.0), raw(d[2])]
    df = XtdataSource(fake).bars(["000001.SZ"], Freq.DAY, d[0], d[2])
    assert df["date"].to_list() == [d[0], d[2]]


def test_prices_rounded() -> None:
    fake = FakeBackend()
    fake.bars["000001.SZ"] = [
        RawBar(ms(date(2024, 6, 3)), 39.199999999999996, 39.5, 39.1, 39.2, 1.0, 1.0)
    ]
    df = XtdataSource(fake).bars(["000001.SZ"], Freq.DAY, date(2024, 6, 1), date(2024, 6, 30))
    assert df["open"].to_list() == [39.2]


def test_query_start_has_buffer() -> None:
    fake = FakeBackend()
    XtdataSource(fake).bars(["000001.SZ"], Freq.DAY, date(2024, 6, 10), date(2024, 6, 30))
    assert fake.daily_calls == [("000001.SZ", "20240605")]


def test_only_daily_and_cn() -> None:
    source = XtdataSource(FakeBackend())
    with pytest.raises(ValueError, match="日线"):
        source.bars(["000001.SZ"], Freq.MINUTE, date(2024, 1, 1), date(2024, 1, 2))
    with pytest.raises(ValueError, match="CN"):
        source.trading_calendar("US", date(2024, 1, 1), date(2024, 1, 2))


def test_calendar_union_and_conversion() -> None:
    fake = FakeBackend()
    fake.dates = [date(2024, 6, 3), date(2024, 6, 4)]
    cal = XtdataSource(fake).trading_calendar("CN", date(2024, 6, 1), date(2024, 6, 30))
    assert cal["date"].to_list() == [date(2024, 6, 3), date(2024, 6, 4)]


# ---- 证券列表 ----


def test_securities_include_delisted() -> None:
    fake = FakeBackend()
    fake.expired = ["600087.SH"]
    fake.dates = [date(2014, 6, 3), date(2014, 6, 4), date(2014, 6, 5)]
    fake.instruments = {
        "000001.SZ": Instrument("000001.SZ", "平安银行", "19910403", "99999999"),
        "600087.SH": Instrument("600087.SH", "退市长油", "19970612", "20140605"),
    }
    source = XtdataSource(fake)
    assert source.capabilities().lists_delisted is True
    listed = source.securities()
    assert listed["symbol"].to_list() == ["000001.SZ"]
    assert listed["delist_date"].to_list() == [None]
    everything = source.securities(include_delisted=True)
    row = everything.filter(pl.col("symbol") == "600087.SH").row(0, named=True)
    assert row["list_date"] == date(1997, 6, 12)
    assert row["delist_date"] == date(2014, 6, 4)  # 摘牌日之前的最后一个交易日


def test_lookup_skips_unknown_codes() -> None:
    fake = FakeBackend()
    fake.expired = ["600087.SH"]  # 过期列表不为空，查询才可信
    fake.instruments = {"000001.SZ": Instrument("000001.SZ", "平安银行", "19910403", None)}
    df = XtdataSource(fake).lookup_securities(["000001.SZ", "000003.SZ"])
    assert df["symbol"].to_list() == ["000001.SZ"]


# ---- lookup_securities 查询失败：抛 LookupFailedError，不返回空或部分结果 ----


class _BoomError(RuntimeError):
    pass


def _lookup_backend() -> FakeBackend:
    fake = FakeBackend()
    fake.expired = ["600087.SH"]
    fake.dates = [date(2014, 6, 3), date(2014, 6, 4), date(2014, 6, 5)]
    fake.instruments = {
        "000001.SZ": Instrument("000001.SZ", "平安银行", "19910403", "99999999"),
        "600087.SH": Instrument("600087.SH", "退市长油", "19970612", "20140605"),
    }
    return fake


def test_lookup_connection_failure_raises_lookup_failed() -> None:
    fake = _lookup_backend()
    boom = MiniQMTUnavailableError("连不上 MiniQMT")

    def broken() -> list[str]:
        raise boom

    fake.expired_a_shares = broken  # type: ignore[method-assign]
    with pytest.raises(LookupFailedError) as info:
        XtdataSource(fake).lookup_securities(["000001.SZ"])
    assert info.value.__cause__ is boom


def test_lookup_instrument_error_raises_lookup_failed() -> None:
    fake = _lookup_backend()

    def broken(symbol: str) -> Instrument | None:
        raise _BoomError("接口报错")

    fake.instrument = broken  # type: ignore[method-assign]
    with pytest.raises(LookupFailedError) as info:
        XtdataSource(fake).lookup_securities(["000001.SZ"])
    assert isinstance(info.value.__cause__, _BoomError)


def test_lookup_partial_failure_raises_instead_of_partial_result() -> None:
    """第一个代码查到了、第二个代码出错：整体抛异常，不返回只含第一个代码的结果。"""
    fake = _lookup_backend()
    original = fake.instrument

    def flaky(symbol: str) -> Instrument | None:
        if symbol == "600087.SH":
            raise TimeoutError("超时")
        return original(symbol)

    fake.instrument = flaky  # type: ignore[method-assign]
    with pytest.raises(LookupFailedError) as info:
        XtdataSource(fake).lookup_securities(["000001.SZ", "600087.SH"])
    assert isinstance(info.value.__cause__, TimeoutError)


def test_lookup_calendar_failure_raises_lookup_failed() -> None:
    fake = _lookup_backend()

    def broken(market: str) -> list[int]:
        raise _BoomError("交易日取不到")

    fake.trading_dates = broken  # type: ignore[method-assign]
    with pytest.raises(LookupFailedError):
        XtdataSource(fake).lookup_securities(["600087.SH"])


def test_lookup_endpoint_not_configured_raises_lookup_failed() -> None:
    fake = _lookup_backend()
    exc = EndpointNotConfiguredError("请到插件页确认 MiniQMT 地址")

    def broken(symbol: str) -> Instrument | None:
        raise exc

    fake.instrument = broken  # type: ignore[method-assign]
    with pytest.raises(LookupFailedError) as info:
        XtdataSource(fake).lookup_securities(["000001.SZ"])
    assert info.value.__cause__ is exc
    assert "插件页" in str(info.value)  # 原因的说明要带给用户


def test_lookup_expired_without_delist_date_raises_lookup_failed() -> None:
    """已过期的合约算不出退市日（交易日历为空）：不能当成在市返回。"""
    fake = _lookup_backend()
    fake.dates = []
    with pytest.raises(LookupFailedError):
        XtdataSource(fake).lookup_securities(["600087.SH"])


def test_lookup_empty_expired_list_raises_lookup_failed() -> None:
    fake = _lookup_backend()
    fake.expired = []
    with pytest.raises(LookupFailedError):
        XtdataSource(fake).lookup_securities(["600087.SH"])


def test_lookup_confirmed_absent_returns_empty_without_raising() -> None:
    """MiniQMT 正常返回“没有这个合约”：确认没有，返回空表，不抛异常。"""
    df = XtdataSource(_lookup_backend()).lookup_securities(["000003.SZ"])
    assert df.height == 0
    assert dict(df.schema) == SECURITIES_SCHEMA


def test_securities_failure_is_not_converted() -> None:
    """只有 lookup_securities 抛 LookupFailedError，securities 的失败保持原异常。"""
    fake = _lookup_backend()

    def broken(name: str) -> list[str]:
        raise _BoomError("板块取不到")

    fake.sector = broken  # type: ignore[method-assign]
    with pytest.raises(_BoomError):
        XtdataSource(fake).securities()


# ---- 复权因子 ----


def test_adj_factors_from_divid() -> None:
    fake = FakeBackend()
    d = [date(2024, 6, 12), date(2024, 6, 13), date(2024, 6, 14)]
    fake.bars["000001.SZ"] = [raw(d[0], 10.0), raw(d[1], 10.0), raw(d[2], 9.0)]
    fake.divids["000001.SZ"] = [RawDivid(ms(d[2]), 1.0, 0.0, 0.0, 0.0, 0.0, 0, 10 / 9)]
    df = XtdataSource(fake).adj_factors(["000001.SZ"], date(2024, 1, 1), date(2024, 12, 31))
    assert df["date"].to_list() == [d[2]]
    assert df["factor"].to_list() == [pytest.approx(10 / 9)]


def test_adj_factors_bonus_plus_gift() -> None:
    fake = FakeBackend()
    d = [date(2024, 6, 13), date(2024, 6, 14)]
    fake.bars["000001.SZ"] = [raw(d[0], 20.0), raw(d[1], 10.0)]
    fake.divids["000001.SZ"] = [
        RawDivid(ms(d[1]), 0.0, 0.5, 0.5, 0.0, 0.0, 0, 2.0)
    ]  # 送 0.5 转 0.5
    df = XtdataSource(fake).adj_factors(["000001.SZ"], date(2024, 1, 1), date(2024, 12, 31))
    assert df["factor"].to_list() == [pytest.approx(2.0)]


def test_dr_mismatch_logged(caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeBackend()
    d = [date(2024, 6, 13), date(2024, 6, 14)]
    fake.bars["000001.SZ"] = [raw(d[0], 10.0), raw(d[1], 9.0)]
    fake.divids["000001.SZ"] = [RawDivid(ms(d[1]), 1.0, 0.0, 0.0, 0.0, 0.0, 0, 1.2)]
    with caplog.at_level("WARNING"):
        XtdataSource(fake).adj_factors(["000001.SZ"], date(2024, 1, 1), date(2024, 12, 31))
    assert any("dr" in r.getMessage() for r in caplog.records)


# ---- 指数成分 ----


def test_index_constituents_current_only() -> None:
    fake = FakeBackend()
    fake.sectors["沪深300"] = ["600000.SH", "000001.SZ"]
    df = XtdataSource(fake).index_constituents("000300.SH", date(2005, 1, 1), date(2026, 1, 1))
    assert df["symbol"].to_list() == ["000001.SZ", "600000.SH"]
    assert df["in_date"].null_count() == 2
    assert df["out_date"].null_count() == 2
    with pytest.raises(ValueError, match="不支持"):
        XtdataSource(fake).index_constituents("399006.SZ", date(2005, 1, 1), date(2026, 1, 1))


# ---- 清单、配置、导入 ----


def test_manifest_declares_miniqmt_endpoint() -> None:
    """MiniQMT 是用户确认的 endpoint（lan 范围），不写在 network 里（SDK 0.4 起只允许公网）。"""
    m = load_manifest(PLUGIN_DIR)
    assert m.id == "xtdata"
    assert m.plugin.kind == "datasource"
    assert m.supports_sdk(SDK_VERSION)
    assert m.supports_sdk("0.4.2") and not m.supports_sdk("0.4.1")  # LookupFailedError 从 0.4.2 起
    assert m.permissions.network == ()
    (ep,) = m.permissions.endpoints
    assert (ep.id, ep.default, str(ep.scope)) == (ENDPOINT_ID, "127.0.0.1:58610", "lan")


def test_no_real_address_committed() -> None:
    """仓库里只有示例配置；示例配置和清单里只有默认的本机地址。"""
    manifest = (PLUGIN_DIR / "duohelm-plugin.toml").read_text(encoding="utf-8")
    assert 'default = "127.0.0.1:58610"' in manifest
    example = (PLUGIN_DIR / "miniqmt.example.toml").read_text(encoding="utf-8")
    assert 'host = "127.0.0.1"' in example
    assert load_config(PLUGIN_DIR / "miniqmt.example.toml").host == "127.0.0.1"


def test_config_defaults_and_errors(tmp_path: Path) -> None:
    assert load_config(tmp_path / "missing.toml").address == "127.0.0.1:58610"
    bad = tmp_path / "bad.toml"
    bad.write_text('[miniqmt]\nport = "abc"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
    for timeout in ('"abc"', "0", "true"):
        bad.write_text(f"[miniqmt]\ndownload_timeout_s = {timeout}\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="download_timeout_s"):
            load_config(bad)
    good = tmp_path / "good.toml"
    good.write_text('[miniqmt]\nhost = "10.0.0.5"\nport = 58611\n', encoding="utf-8")
    assert load_config(good).address == "10.0.0.5:58611"


def test_import_does_not_touch_xtquant() -> None:
    backend = plugin._backend  # pyright: ignore[reportPrivateUsage]
    assert isinstance(backend, LiveBackend)
    assert backend._xt is None  # pyright: ignore[reportPrivateUsage]
    assert "xtquant" not in sys.modules


def test_lookup_expired_code_without_instrument_raises_lookup_failed() -> None:
    """代码在过期列表里、合约信息却是空：数据源自相矛盾，不能记成“确定没有”。"""
    fake = _lookup_backend()
    del fake.instruments["600087.SH"]
    with pytest.raises(LookupFailedError):
        XtdataSource(fake).lookup_securities(["600087.SH"])
