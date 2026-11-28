"""日线先读 MiniQMT 本地数据，缺数据时才下载（duohelm-plugins#20）。

背景：``download_history_data`` 每只约 72 ms（2026-11-28 在维护者的 MiniQMT 上实测，对 5,618 只
本地已有的日线各下载一遍约 403 秒），读取一只日线只要几到几十毫秒；维护者的定时任务每天把
本地日线更新到最近交易日。所以先读本地，只有本地缺数据的证券才下载。

**本地缺数据的判定**（``is_stale``）：读到的日线为空，或最后一根的日期早于“应有的最近交易日”。

“应有的最近交易日”（``expected_last_day``）由交易日历确定，不由本地数据确定：本地数据本身可能
是旧的，用它推断最近交易日会把旧数据当成新数据。取不晚于 ``now`` 的最后一个交易日；当天是
交易日但还没收盘（早于 ``DAILY_READY_TIME``）时，当天的日线还不会有，取前一个交易日。

**停牌不是缺数据**：停牌日没有这一行，停牌中的证券最后一根天然早于最近交易日。区分办法是
“下载过一次”：最后一根早于应有的最近交易日的证券，下载后重读，仍然早于它的，接受为停牌或
退市，不再循环下载；同一个应有的最近交易日内，已经下载过的证券不再下载（``_checked``），所以
同一轮同步里 ``bars`` 之后再调 ``adj_factors`` 不会重复下载。新的交易日到来后才会再试一次。
代价：停牌中的证券、已退市的证券，每个交易日会下载一次（与修复前每次读取都下载相比已少得多）。

**未知不是空**：
- 下载失败（接口报错或超时）：抛 :class:`DailyDownloadError`，不退回本地的旧数据，也不当成没有
  数据。同步失败，下次照常重试
- 下载成功、重读后仍为空：返回空（MiniQMT 没有这只证券的日线，如早年退市的），记 info 日志
- 读取本身抛异常：原样抛出
- 交易日历取不到（返回空）：无法判断是否过期，保守地当成缺数据，退回“先下载再读”
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Protocol

from duohelm_xtdata.dates import SHANGHAI, shanghai_date

if TYPE_CHECKING:
    from duohelm_xtdata.backend import RawBar

logger = logging.getLogger(__name__)

DAILY_READY_TIME = time(15, 30)
"""交易日当天几点之后，才要求本地有当天的日线。A 股 15:00 收盘，留 30 分钟给 MiniQMT 生成当天
日线。这是我们的取值，没有实测；取早了只会多下载一次（下载后仍没有就接受），取晚了
会让收盘后 30 分钟内的同步读到前一天的数据而不下载。"""


class DailyDownloadError(RuntimeError):
    """下载日线失败（接口报错或超时）：本地数据缺失或过期，又拿不到最新的，结果未知。"""


class DailyStore(Protocol):
    """能读本地日线、能下载日线的后端（LiveBackend，或测试里的假后端）。"""

    def read_daily(self, symbol: str, start: str | None) -> list[RawBar]:
        """只读 MiniQMT 本地已有的未复权日线，不下载。"""
        ...

    def download_daily(self, symbol: str, start: str | None) -> None:
        """让 MiniQMT 下载这只证券的日线到本地。失败抛异常。"""
        ...

    def trading_dates(self, market: str) -> list[int]: ...


def expected_last_day(days: Sequence[date], now: datetime) -> date | None:
    """应有的最近交易日（days 升序）。没有符合的返回 None。"""
    local = now.astimezone(SHANGHAI)
    cutoff = local.date() if local.time() >= DAILY_READY_TIME else local.date() - timedelta(days=1)
    i = bisect_right(days, cutoff)
    return days[i - 1] if i > 0 else None


def is_stale(bars: Sequence[RawBar], expected: date | None) -> bool:
    """本地缺数据：为空，或最后一根早于应有的最近交易日。expected 未知时一律当成缺数据。"""
    if not bars:
        return True
    if expected is None:
        return True
    return max(shanghai_date(b.time_ms) for b in bars) < expected


class LocalFirstDaily:
    def __init__(
        self,
        store: DailyStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._now = now if now is not None else lambda: datetime.now(SHANGHAI)
        self._calendar: list[date] = []
        self._calendar_loaded_on: date | None = None
        self._checked: dict[str, date | None] = {}
        """证券 -> 下载时应有的最近交易日。同一个交易日内不再重复下载。"""

    def _expected(self) -> date | None:
        now = self._now()
        today = now.astimezone(SHANGHAI).date()
        if self._calendar_loaded_on != today:
            days = {shanghai_date(t) for t in self._store.trading_dates("SH")}
            self._calendar = sorted(days)
            self._calendar_loaded_on = today
            if not self._calendar:
                logger.warning("交易日历为空，无法判断本地日线是否过期，按缺数据处理（先下载再读）")
        return expected_last_day(self._calendar, now)

    def daily(self, symbol: str, start: str | None) -> list[RawBar]:
        expected = self._expected()
        bars = self._store.read_daily(symbol, start)
        if not is_stale(bars, expected):
            return bars
        if symbol in self._checked and self._checked[symbol] == expected:
            # 这个交易日里已经为它下载过，仍然缺：停牌、退市或本来就没有，接受
            return bars
        try:
            self._store.download_daily(symbol, start)
        except Exception as exc:
            raise DailyDownloadError(
                f"{symbol} 的本地日线{'为空' if not bars else '不是最新的'}，下载又失败，"
                f"结果未知，不使用旧数据：{exc}"
            ) from exc
        self._checked[symbol] = expected
        bars = self._store.read_daily(symbol, start)
        if not bars:
            logger.info("%s：下载后 MiniQMT 仍没有日线，按没有数据处理", symbol)
        elif is_stale(bars, expected):
            logger.debug("%s：下载后最后一根仍早于 %s，按停牌或退市接受", symbol, expected)
        return bars
