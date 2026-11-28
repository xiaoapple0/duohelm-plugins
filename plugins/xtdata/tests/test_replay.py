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
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

import polars as pl
import pytest
import xt_scenario as sc
from duohelm_sdk import DataSourcePlugin, Freq
from duohelm_sdk.checks import check_adj_factors, check_securities
from duohelm_sdk.schema import BARS_SCHEMA, schema_problems

from duohelm_xtdata.backend import ReplayBackend
from duohelm_xtdata.dates import SHANGHAI, security_type, shanghai_date
from duohelm_xtdata.source import _START_BUFFER as START_BUFFER  # pyright: ignore[reportPrivateUsage]
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
def backend() -> ReplayBackend:
    return ReplayBackend(sc.replay_file())


@pytest.fixture(scope="module")
def source(backend: ReplayBackend) -> XtdataSource:
    return XtdataSource(backend)


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


def test_delisted_bars(backend: ReplayBackend, source: XtdataSource) -> None:
    """xtdata 对退市股只给成交量、价格全为 0：原始数据里有到退市前最后一天的行，插件全部丢弃。"""
    raw = backend.daily(sc.DELISTED, (sc.DELISTED_RANGE[0] - START_BUFFER).strftime("%Y%m%d"))
    assert max(shanghai_date(b.time_ms) for b in raw) == date(2014, 6, 4)
    assert all(b.open == b.high == b.low == b.close == 0 for b in raw)
    assert any(b.volume > 0 for b in raw)
    df = source.bars([sc.DELISTED], Freq.DAY, *sc.DELISTED_RANGE)
    assert df.height == 0


def test_zero_price_delisted_bars_dropped(backend: ReplayBackend, source: XtdataSource) -> None:
    raw = {s: backend.daily(s, None) for s in sc.ZERO_PRICE}
    for symbol, bars in raw.items():
        assert bars, symbol
        assert all(b.open == b.high == b.low == b.close == 0 for b in bars), symbol
        assert any(b.volume > 0 for b in bars), symbol
    df = source.bars(sc.ZERO_PRICE, Freq.DAY, *sc.ZERO_PRICE_RANGE)
    assert schema_problems(df, BARS_SCHEMA) == []
    assert df.height == 0


def test_zero_price_adj_factors_pass_check(source: XtdataSource) -> None:
    """旧代码：000016.SZ 1996-07-17 等配股事件的因子为 0，同步被复权因子检查拦下。"""
    df = source.adj_factors(sc.ZERO_PRICE, *sc.ZERO_PRICE_RANGE)
    assert check_adj_factors(df).passed
    assert df.height == 0  # 没有有效的除权前收盘价，不产生因子


def test_adj_factors(source: XtdataSource) -> None:
    df = source.adj_factors(sc.ADJ_SYMBOLS, *sc.ADJ_RANGE)
    assert check_adj_factors(df).passed
    pingan = df.filter(pl.col("symbol") == "000001.SZ")
    bars = source.bars(["000001.SZ"], Freq.DAY, *sc.LISTED_RANGE)
    prev_close = bars.filter(pl.col("date") == date(2024, 6, 13))["close"].item()
    by_date = dict(zip(pingan["date"].to_list(), pingan["factor"].to_list(), strict=True))
    before = max(d for d in by_date if d < date(2024, 6, 14))
    ratio = by_date[date(2024, 6, 14)] / by_date[before]
    # 每股派 0.719 元；参考价按交易所的做法四舍五入到 0.01 元（#19）
    ref = float(Decimal(repr(prev_close - 0.719)).quantize(Decimal("0.01"), ROUND_HALF_UP))
    assert ref != pytest.approx(prev_close - 0.719, rel=1e-6)  # 取整确实改变了参考价
    assert ratio == pytest.approx(prev_close / ref, rel=1e-9)


def test_index_constituents(source: XtdataSource) -> None:
    df = source.index_constituents(sc.INDEX, *sc.ADJ_RANGE)
    assert df.height == 300
    assert df["in_date"].null_count() == 300


# ---- 除权比例核对（#19）：真实数据 ----


def _ratios(source: XtdataSource, symbol: str) -> dict[date, float]:
    df = source.adj_factors([symbol], *sc.ADJ_RANGE)
    factors = df["factor"].to_list()
    days = df["date"].to_list()
    return {
        d: f / (factors[i - 1] if i else 1.0)
        for i, (d, f) in enumerate(zip(days, factors, strict=True))
    }


def _bar(
    backend: ReplayBackend, symbol: str, day: date, before: bool = False
) -> tuple[float, float]:
    """day 那一根（before 时为它之前的一根）K 线的 (close, preClose)。"""
    rows = [b for b in backend.daily(symbol, None) if b.volume > 0 and b.close > 0]
    i = next(i for i, b in enumerate(rows) if shanghai_date(b.time_ms) >= day)
    b = rows[i - 1] if before else rows[i]
    assert b.pre_close is not None, "录制文件里没有 preClose，需要重新录制"
    return b.close, b.pre_close


@pytest.mark.parametrize(
    ("symbol", "day"),
    [
        ("603007.SH", date(2024, 12, 26)),
        ("300093.SZ", date(2025, 11, 20)),
        ("000615.SZ", date(2025, 12, 29)),
    ],
)
def test_restructuring_adopts_preclose_ratio(
    backend: ReplayBackend, source: XtdataSource, symbol: str, day: date
) -> None:
    prev_close, _ = _bar(backend, symbol, day, before=True)
    _, pre_close = _bar(backend, symbol, day)
    assert _ratios(source, symbol)[day] == pytest.approx(prev_close / pre_close, rel=1e-12)


def test_restructuring_with_unadjusted_preclose_and_dr_one(source: XtdataSource) -> None:
    """603363.SH 2024-12-26：每股转增 2 股，事件公式给 3.0；preClose 与 dr 都说价格没有调整。"""
    assert _ratios(source, "603363.SH")[date(2024, 12, 26)] == pytest.approx(1.0)


@pytest.mark.parametrize("symbol", ["603559.SH", "600375.SH"])
def test_empty_event_kept_and_warned(
    source: XtdataSource, symbol: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING"):
        ratios = _ratios(source, symbol)
    assert ratios.get(date(2024, 12, 26), 1.0) == pytest.approx(1.0)
    assert any(
        r.getMessage().startswith(f"除权核对[empty_event] {symbol} 2024-12-26")
        for r in caplog.records
    )


def test_unadjusted_preclose_keeps_formula(backend: ReplayBackend, source: XtdataSource) -> None:
    """002310.SZ 2024-12-30：间隔 2，preClose 等于上一收盘（没有按除权调整），dr 与事件公式一致。"""
    day = date(2024, 12, 30)
    prev_close, _ = _bar(backend, "002310.SZ", day, before=True)
    _, pre_close = _bar(backend, "002310.SZ", day)
    assert pre_close == pytest.approx(prev_close)
    [d] = [x for x in backend.divid("002310.SZ") if shanghai_date(x.time_ms) == day]
    assert _ratios(source, "002310.SZ")[day] == pytest.approx(d.dr, rel=2e-3)
    assert _ratios(source, "002310.SZ")[day] > 2.0


def test_long_suspension_keeps_formula(backend: ReplayBackend, source: XtdataSource) -> None:
    """000678.SZ 2006-07-13：停牌 28 个交易日后除权，A 不干净，保留事件公式。"""
    day = date(2006, 7, 13)
    prev_close, _ = _bar(backend, "000678.SZ", day, before=True)
    [d] = [x for x in backend.divid("000678.SZ") if shanghai_date(x.time_ms) == day]
    ref = Decimal(repr(prev_close - d.interest)) / Decimal(repr(1 + d.stock_bonus + d.stock_gift))
    expected = prev_close / float(ref.quantize(Decimal("0.01"), ROUND_HALF_UP))
    assert _ratios(source, "000678.SZ")[day] == pytest.approx(expected, rel=1e-9)
