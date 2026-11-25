"""用录制的真实响应回放，检查插件输出符合 SDK 0.2 的契约，以及几项已知的真实数据。

不访问网络。录制文件只在本地（.local-data/，不进仓库）。整个模块标记为 local_data：
CI 用 ``-m "not local_data"`` 排除；本地用 ``-m local_data`` 运行，缺少录制文件直接失败。
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest
import scenario
from duohelm_sdk import DataSourcePlugin, Freq
from duohelm_sdk.checks import check_adj_factors, check_securities
from duohelm_sdk.schema import BARS_SCHEMA, schema_problems

from duohelm_eltdx.backend import ReplayBackend
from duohelm_eltdx.source import EltdxSource

pytestmark = pytest.mark.local_data


@pytest.fixture(scope="module", autouse=True)
def _require_replay_file() -> None:
    """选中了这些测试却缺少录制文件：失败，不跳过。模块级，先于 source 执行。"""
    if not scenario.REPLAY_FILE.exists():
        pytest.fail(
            f"缺少本地录制文件 {scenario.REPLAY_FILE}：运行 scripts/capture_eltdx.py 录制，"
            '或用 -m "not local_data" 排除回放测试',
            pytrace=False,
        )


@pytest.fixture(scope="module")
def source() -> EltdxSource:
    return EltdxSource(ReplayBackend(scenario.REPLAY_FILE))


def test_recorded_with_pinned_eltdx() -> None:
    assert ReplayBackend(scenario.REPLAY_FILE).meta["eltdx"] == "3.3.5"


def test_whole_scenario_replays(source: EltdxSource) -> None:
    scenario.run(source)  # 每次调用都有录制，不会访问网络


def test_is_datasource_plugin(source: EltdxSource) -> None:
    assert isinstance(source, DataSourcePlugin)
    assert source.capabilities().lists_delisted is False


# ---- 证券列表 ----


def test_securities_contract(source: EltdxSource) -> None:
    df = source.securities()
    result = check_securities(df)
    assert result.passed, result.details
    assert df.height > 5000
    assert df.filter(pl.col("symbol") == "000001.SZ")["name"].to_list() == ["平安银行"]
    assert "600087.SH" not in df["symbol"].to_list()  # 代码表只有在市证券
    assert df["delist_date"].null_count() == df.height


def test_lookup_securities(source: EltdxSource) -> None:
    df = source.lookup_securities(scenario.LOOKUP)
    assert check_securities(df).passed
    got = {r["symbol"]: r for r in df.iter_rows(named=True)}
    assert set(got) == {"600087.SH", "688981.SH"}  # 不存在的代码不返回
    assert got["600087.SH"]["list_date"] == date(1997, 6, 12)
    assert got["600087.SH"]["delist_date"] == date(2014, 6, 4)
    assert got["688981.SH"]["list_date"] == date(2020, 7, 16)
    assert got["688981.SH"]["delist_date"] is None


# ---- 日线与交易日 ----


def test_calendar(source: EltdxSource) -> None:
    cal = source.trading_calendar("CN", *scenario.CALENDAR_RANGE)["date"].to_list()
    assert len(cal) == 242
    assert date(2024, 2, 12) not in cal  # 春节
    assert date(2024, 10, 1) not in cal  # 国庆
    assert date(2024, 9, 28) not in cal  # 调休补班的周六，A 股不开市
    assert cal == sorted(cal)


def test_listed_bars_contract(source: EltdxSource) -> None:
    df = source.bars([scenario.LISTED], Freq.DAY, *scenario.LISTED_RANGE)
    assert schema_problems(df, BARS_SCHEMA) == []
    lo, hi = scenario.LISTED_RANGE
    cal = source.trading_calendar("CN", *scenario.CALENDAR_RANGE)["date"].to_list()
    expected = [d for d in cal if lo <= d <= hi]
    assert df["date"].to_list() == expected  # 这段时间平安银行没有停牌
    assert (df["volume"] > 0).all()
    ok = (df["low"] <= df[["open", "close"]].min_horizontal()) & (
        df[["open", "close"]].max_horizontal() <= df["high"]
    )
    assert ok.all()


def test_delisted_bars(source: EltdxSource) -> None:
    df = source.bars([scenario.DELISTED], Freq.DAY, *scenario.DELISTED_RANGE)
    assert schema_problems(df, BARS_SCHEMA) == []
    assert df.height > 0
    assert df["date"].max() == date(2014, 6, 4)  # 最后一个交易日


# ---- 复权因子 ----


def test_adj_factors_contract(source: EltdxSource) -> None:
    df = source.adj_factors(scenario.ADJ_SYMBOLS, *scenario.ADJ_RANGE)
    result = check_adj_factors(df)
    assert result.passed, result.details
    assert set(df["symbol"].unique().to_list()) <= set(scenario.ADJ_SYMBOLS)


def test_adj_factor_known_dividend(source: EltdxSource) -> None:
    """平安银行 2024-06-14 每股派 0.719 元：比例 = 前收 / (前收 - 0.719)。"""
    factors = source.adj_factors(["000001.SZ"], *scenario.ADJ_RANGE)
    bars = source.bars(["000001.SZ"], Freq.DAY, *scenario.LISTED_RANGE)
    prev_close = bars.filter(pl.col("date") == date(2024, 6, 13))["close"].item()
    by_date = dict(zip(factors["date"].to_list(), factors["factor"].to_list(), strict=True))
    before = max(d for d in by_date if d < date(2024, 6, 14))
    ratio = by_date[date(2024, 6, 14)] / by_date[before]
    assert ratio == pytest.approx(prev_close / (prev_close - 0.719), rel=1e-9)


# ---- 指数成分 ----


def test_index_constituents(source: EltdxSource) -> None:
    df = source.index_constituents(scenario.INDEX, *scenario.ADJ_RANGE)
    assert df.height == 300
    assert df["out_date"].null_count() == 300  # 没有调出记录
    assert df["symbol"].str.contains(r"^\d{6}\.(SH|SZ)$").all()
    assert df["in_date"].min() is not None
