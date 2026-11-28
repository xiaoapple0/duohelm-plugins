"""“先读本地、缺数据才下载”（duohelm-plugins#20）用录制数据回放。

录制文件里没有区分“下载前 / 下载后”的信息（录制时每次读取前都下载了）。这里**模拟**：
:class:`SimulatedStore` 包住 ReplayBackend，对指定的证券让下载前的本地数据为空或缺最近几天，
下载之后才返回录制的完整数据。检查的是：本地有数据时不下载；本地缺数据时下载一次；两条
路径输出的日线和复权因子与直接回放逐行一致（复权和 point-in-time 行为不变）。

不连接 MiniQMT。标记为 local_data：CI 排除，本地用 ``-m local_data`` 运行，缺少录制文件直接失败。
"""

from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest
import xt_scenario as sc
from duohelm_sdk import Freq

from duohelm_xtdata.backend import Instrument, RawBar, RawDivid, ReplayBackend
from duohelm_xtdata.dates import SHANGHAI, shanghai_date
from duohelm_xtdata.localfirst import DailyDownloadError, LocalFirstDaily
from duohelm_xtdata.source import _START_BUFFER as START_BUFFER  # pyright: ignore[reportPrivateUsage]
from duohelm_xtdata.source import XtdataSource

pytestmark = pytest.mark.local_data

HIDE_LAST = 3
"""“本地缺最近几根”的模拟：下载前少最后 3 根。"""


@pytest.fixture(scope="module", autouse=True)
def _require_replay_file() -> None:
    path = sc.replay_file()
    if not path.exists():
        pytest.fail(
            f"缺少本地录制文件 {path}（环境变量 {sc.LOCAL_DATA_ENV} 可指定录制数据目录）",
            pytrace=False,
        )


@pytest.fixture(scope="module")
def replay() -> ReplayBackend:
    return ReplayBackend(sc.replay_file())


def _now_after_last_bar(replay: ReplayBackend) -> datetime:
    """“现在”：平安银行最后一根日线那天的 16:00（收盘后），它是录制日历里的交易日。"""
    last = max(shanghai_date(b.time_ms) for b in replay.daily(sc.LISTED, None))
    return datetime(last.year, last.month, last.day, 16, tzinfo=SHANGHAI)


class SimulatedStore:
    """下载前缺数据的模拟（不是录制的真实行为）。empty：下载前本地没有；behind：下载前缺最后几根。"""

    def __init__(
        self,
        replay: ReplayBackend,
        empty: set[str] = frozenset(),  # pyright: ignore[reportArgumentType]
        behind: set[str] = frozenset(),  # pyright: ignore[reportArgumentType]
        fail: set[str] = frozenset(),  # pyright: ignore[reportArgumentType]
    ) -> None:
        self._replay = replay
        self._empty = set(empty)
        self._behind = set(behind)
        self._fail = set(fail)
        self.downloaded: set[str] = set()
        self.downloads: list[str] = []

    def read_daily(self, symbol: str, start: str | None) -> list[RawBar]:
        bars = self._replay.daily(symbol, start)
        if symbol in self.downloaded:
            return bars
        if symbol in self._empty:
            return []
        if symbol in self._behind:
            return bars[:-HIDE_LAST]
        return bars

    def download_daily(self, symbol: str, start: str | None) -> None:
        self.downloads.append(symbol)
        if symbol in self._fail:
            raise TimeoutError("模拟的下载失败")
        self.downloaded.add(symbol)

    def trading_dates(self, market: str) -> list[int]:
        return self._replay.trading_dates(market)


class LocalFirstReplay:
    """XtdataSource 用的后端：daily 走 LocalFirstDaily，其余直接回放。"""

    def __init__(self, replay: ReplayBackend, store: SimulatedStore, now: datetime) -> None:
        self._replay = replay
        self._local_first = LocalFirstDaily(store, lambda: now)

    def daily(self, symbol: str, start: str | None) -> list[RawBar]:
        return self._local_first.daily(symbol, start)

    def sector(self, name: str) -> list[str]:
        return self._replay.sector(name)

    def expired_a_shares(self) -> list[str]:
        return self._replay.expired_a_shares()

    def instrument(self, symbol: str) -> Instrument | None:
        return self._replay.instrument(symbol)

    def divid(self, symbol: str) -> list[RawDivid]:
        return self._replay.divid(symbol)

    def trading_dates(self, market: str) -> list[int]:
        return self._replay.trading_dates(market)


def _outputs(source: XtdataSource) -> tuple[pl.DataFrame, pl.DataFrame]:
    """场景里的两个主要输出：近期日线（含不同证券类型）和全历史复权因子。"""
    bars = source.bars(sc.UNIT_SAMPLES["stock"], Freq.DAY, *sc.UNIT_RANGE)
    adj = source.adj_factors(sc.ADJ_SYMBOLS + sc.ZERO_PRICE, *sc.ADJ_RANGE)
    return bars, adj


@pytest.fixture(scope="module")
def baseline(replay: ReplayBackend) -> tuple[pl.DataFrame, pl.DataFrame]:
    return _outputs(XtdataSource(replay))


def test_local_data_present_path_does_not_download_and_output_identical(
    replay: ReplayBackend, baseline: tuple[pl.DataFrame, pl.DataFrame]
) -> None:
    """本地有数据（录制的数据就是“本地已有”）：不下载，输出与直接回放逐行一致。"""
    store = SimulatedStore(replay)
    out = _outputs(XtdataSource(LocalFirstReplay(replay, store, _now_after_last_bar(replay))))
    # 在市证券（平安银行、浦发银行、贵州茅台）本地有数据：不下载。已退市的三只最后一根永远
    # 早于最近交易日，各下载一次后接受
    assert sorted(store.downloads) == sorted(sc.ZERO_PRICE)
    assert out[0].equals(baseline[0]) and out[1].equals(baseline[1])
    assert baseline[0].height > 0 and baseline[1].height > 0


def test_local_data_missing_path_downloads_once_per_symbol_and_output_identical(
    replay: ReplayBackend, baseline: tuple[pl.DataFrame, pl.DataFrame]
) -> None:
    """（模拟）本地缺数据：一部分证券下载前为空，一部分缺最后几根。每只只下载一次
    （bars 之后的 adj_factors 不再下载），下载后的输出与直接回放逐行一致。"""
    empty = {"000001.SZ", "000004.SZ"}
    behind = {"600000.SH", "600519.SH"}
    store = SimulatedStore(replay, empty=empty, behind=behind)
    out = _outputs(XtdataSource(LocalFirstReplay(replay, store, _now_after_last_bar(replay))))
    assert sorted(store.downloads) == sorted(store.downloaded)  # 没有重复下载
    assert len(store.downloads) == len(set(store.downloads))
    assert set(store.downloads) == empty | behind | set(sc.ZERO_PRICE)
    assert out[0].equals(baseline[0]) and out[1].equals(baseline[1])


def test_simulated_stale_data_differs_from_recorded(replay: ReplayBackend) -> None:
    """对照：模拟里“缺最后几根”的本地数据确实比录制的少，说明上面的测试检查的是真东西。"""
    symbol = sc.UNIT_SAMPLES["stock"][0]
    start = (sc.UNIT_RANGE[0] - START_BUFFER).strftime("%Y%m%d")
    store = SimulatedStore(replay, behind={symbol})
    assert len(store.read_daily(symbol, start)) == len(replay.daily(symbol, start)) - HIDE_LAST


def test_delisted_symbol_stays_stale_after_download_without_looping(
    replay: ReplayBackend,
) -> None:
    """退市证券：最后一根永远早于最近交易日。下载一次后接受，同一天不再下载，不报错。"""
    store = SimulatedStore(replay)
    backend = LocalFirstReplay(replay, store, _now_after_last_bar(replay))
    start = (sc.DELISTED_RANGE[0] - START_BUFFER).strftime("%Y%m%d")
    for _ in range(3):
        assert backend.daily(sc.DELISTED, start) == replay.daily(sc.DELISTED, start)
    assert store.downloads == [sc.DELISTED]


def test_download_failure_is_not_empty_in_replay(replay: ReplayBackend) -> None:
    store = SimulatedStore(replay, empty={sc.LISTED}, fail={sc.LISTED})
    source = XtdataSource(LocalFirstReplay(replay, store, _now_after_last_bar(replay)))
    with pytest.raises(DailyDownloadError):
        source.bars([sc.LISTED], Freq.DAY, *sc.LISTED_RANGE)
