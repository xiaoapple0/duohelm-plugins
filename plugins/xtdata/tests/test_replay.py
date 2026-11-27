"""用录制的 MiniQMT 响应回放：SDK 0.2 契约检查、日期、按类型的成交量单位、已知数据。

不连接 MiniQMT，不 import xtquant。录制文件只在本地（.local-data/，不进仓库）。
整个模块标记为 local_data：CI 用 ``-m "not local_data"`` 排除；本地用 ``-m local_data``
运行，缺少录制文件直接失败。
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import polars as pl
import pytest
import xt_scenario as sc
from duohelm_sdk import DataSourcePlugin, Freq
from duohelm_sdk.checks import check_adj_factors, check_securities
from duohelm_sdk.schema import BARS_SCHEMA, schema_problems

from duohelm_xtdata.backend import ReplayBackend
from duohelm_xtdata.dates import SHANGHAI, security_type, shanghai_date
from duohelm_xtdata.source import XtdataSource

pytestmark = pytest.mark.local_data


@pytest.fixture(scope="module", autouse=True)
def _require_replay_file() -> None:
    """选中了这些测试却缺少录制文件：失败，不跳过。"""
    path = sc.replay_file()
    if not path.exists():
        pytest.fail(
            f"缺少本地录制文件 {path}（环境变量 {sc.LOCAL_DATA_ENV} 可指定录制数据目录）："
            '运行 scripts/capture_xtdata.py 录制，或用 -m "not local_data" 排除回放测试',
            pytrace=False,
        )


@pytest.fixture(scope="module", autouse=True)
def _new_york_time() -> Iterator[None]:
    """POSIX 上把整个模块切换到美东时区。Windows 上不设置 TZ（C 运行库不认识 IANA 时区名，
    设置后反而会把本机时区搞乱），沿用本机时区。"""
    tzset = getattr(time, "tzset", None)
    if tzset is None:
        yield
        return
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    tzset()


@pytest.fixture(scope="module")
def source() -> XtdataSource:
    return XtdataSource(ReplayBackend(sc.replay_file()))


def test_whole_scenario_replays(source: XtdataSource) -> None:
    sc.run(source)
    assert "xtquant" not in sys.modules


def test_is_datasource_plugin(source: XtdataSource) -> None:
    assert isinstance(source, DataSourcePlugin)
    assert source.capabilities().lists_delisted is True


# ---- 日期 ----


def test_catl_listing_date_in_non_utc8_zone(source: XtdataSource) -> None:
    """宁德时代 2018-06-11 上市。在美东时区的机器上，xtdata 的日期字符串会显示 2018-06-10。"""
    df = source.bars([sc.CATL], Freq.DAY, *sc.CATL_RANGE)
    assert df["date"][0] == date(2018, 6, 11)
    assert date(2018, 6, 10) not in df["date"].to_list()
    # 不依赖本机时区：同一个录制的原始时间戳，按美东时区解读就是 2018-06-10（xtdata 的日期字符串
    # 在美东时区的机器上就是这样错的）；插件按 Asia/Shanghai 换算，与本机时区无关。
    first = ReplayBackend(sc.replay_file()).daily(
        sc.CATL, (sc.CATL_RANGE[0] - timedelta(days=5)).strftime("%Y%m%d")
    )[0]
    ny = datetime.fromtimestamp(first.time_ms / 1000, tz=ZoneInfo("America/New_York"))
    assert ny.date() == date(2018, 6, 10)
    assert shanghai_date(first.time_ms) == date(2018, 6, 11)


def test_bar_timestamps_are_fixed_utc8_midnight() -> None:
    """xtdata 日线的时间戳是固定 UTC+8 的零点；夏令时年份按 Asia/Shanghai 是凌晨 1:00，日期不变。"""
    replay = ReplayBackend(sc.replay_file())
    raw = replay.daily("000001.SZ", None)
    assert raw
    utc8 = timezone(timedelta(hours=8))
    assert all(datetime.fromtimestamp(b.time_ms / 1000, tz=utc8).hour == 0 for b in raw)
    local = [datetime.fromtimestamp(b.time_ms / 1000, tz=SHANGHAI) for b in raw]
    in_dst = [t for t in local if t.utcoffset() == timedelta(hours=9)]
    assert in_dst, "录制数据里应有 1986–1991 年夏令时期间的 K 线"
    assert all(t.hour == 1 for t in in_dst)
    # 两种换算给出相同的日期
    by_utc8 = [datetime.fromtimestamp(b.time_ms / 1000, tz=utc8).date() for b in raw]
    assert [shanghai_date(b.time_ms) for b in raw] == by_utc8


# ---- 成交量单位：按证券类型 ----


@pytest.mark.parametrize("kind", ["stock", "etf", "bj"])
def test_volume_unit_vwap_within_range(source: XtdataSource, kind: str) -> None:
    """成交额 ÷ 股数 = 当天均价，必须落在最低价与最高价之间。单位错 10 倍、100 倍都会被抓住。"""
    df = source.bars(sc.UNIT_SAMPLES[kind], Freq.DAY, *sc.UNIT_RANGE)
    assert df.height > 20
    assert {security_type(s) for s in df["symbol"].unique().to_list()} == {kind}
    vwap = df["amount"] / df["volume"]
    ok = (vwap >= df["low"] * 0.995) & (vwap <= df["high"] * 1.005)
    assert ok.all(), df.filter(~ok).head()


def test_index_volume_unit(source: XtdataSource) -> None:
    """指数没有价格可比：成交额 ÷ 股数是成分股的平均成交价，应在 1–100 元之间。"""
    df = source.bars(sc.UNIT_SAMPLES["index"], Freq.DAY, *sc.UNIT_RANGE)
    assert df.height > 20
    avg_price = df["amount"] / df["volume"]
    assert ((avg_price > 1) & (avg_price < 100)).all(), avg_price.describe()


# ---- 证券列表 ----


def test_securities_contract(source: XtdataSource) -> None:
    df = source.securities(include_delisted=True)
    result = check_securities(df)
    assert result.passed, result.details
    delisted = df.filter(pl.col("delist_date").is_not_null())
    assert delisted.height > 300
    assert df.height - delisted.height > 5000
    row = df.filter(pl.col("symbol") == "600087.SH").row(0, named=True)
    assert row["list_date"] == date(1997, 6, 12)
    assert row["delist_date"] == date(2014, 6, 4)


def test_lookup_securities(source: XtdataSource) -> None:
    df = source.lookup_securities(sc.LOOKUP)
    got = set(df["symbol"].to_list())
    assert "600087.SH" in got and "300750.SZ" in got
    assert "609999.SH" not in got
    assert "000003.SZ" not in got  # xtdata 缺少 PT金田A（2002 年退市），需要 eltdx 补全


# ---- 日线、交易日、复权、指数 ----


def test_calendar(source: XtdataSource) -> None:
    cal = source.trading_calendar("CN", *sc.CALENDAR_RANGE)["date"].to_list()
    assert len(cal) == 242
    assert date(2024, 2, 12) not in cal
    assert date(2024, 9, 28) not in cal


def test_listed_bars(source: XtdataSource) -> None:
    df = source.bars([sc.LISTED], Freq.DAY, *sc.LISTED_RANGE)
    assert schema_problems(df, BARS_SCHEMA) == []
    cal = source.trading_calendar("CN", *sc.CALENDAR_RANGE)["date"].to_list()
    lo, hi = sc.LISTED_RANGE
    assert df["date"].to_list() == [d for d in cal if lo <= d <= hi]


def test_delisted_bars(source: XtdataSource) -> None:
    df = source.bars([sc.DELISTED], Freq.DAY, *sc.DELISTED_RANGE)
    assert df["date"].max() == date(2014, 6, 4)


def test_adj_factors(source: XtdataSource) -> None:
    df = source.adj_factors(sc.ADJ_SYMBOLS, *sc.ADJ_RANGE)
    assert check_adj_factors(df).passed
    pingan = df.filter(pl.col("symbol") == "000001.SZ")
    bars = source.bars(["000001.SZ"], Freq.DAY, *sc.LISTED_RANGE)
    prev_close = bars.filter(pl.col("date") == date(2024, 6, 13))["close"].item()
    by_date = dict(zip(pingan["date"].to_list(), pingan["factor"].to_list(), strict=True))
    before = max(d for d in by_date if d < date(2024, 6, 14))
    ratio = by_date[date(2024, 6, 14)] / by_date[before]
    assert ratio == pytest.approx(prev_close / (prev_close - 0.719), rel=1e-9)  # 每股派 0.719 元


def test_index_constituents(source: XtdataSource) -> None:
    df = source.index_constituents(sc.INDEX, *sc.ADJ_RANGE)
    assert df.height == 300
    assert df["in_date"].null_count() == 300
