"""日线先读 MiniQMT 本地数据，缺数据时才下载（duohelm-plugins#20）。

背景：``download_history_data`` 每只约 72 ms（2026-11-28 在维护者的 MiniQMT 上实测，对 5,618 只
本地已有的日线各下载一遍约 403 秒），读取一只日线只要几到几十毫秒；维护者的定时任务每天把
本地日线更新到最近交易日。所以先读本地，只有本地缺数据的证券才下载。

**本地缺数据的判定**（``is_stale``、``head_truncated``）：读到的日线为空；或**末尾**缺：最后一根
早于“应有的最近交易日”；或**开头**缺：第一根明显晚于应有的第一天。

开头为什么要查：修复前 ``adj_factors`` 每次用 start="" 下载全历史，开头一定完整；现在依赖本地，
本地开头被截断时，``adj_factors`` 会把第一根当成上市日，更早的除权事件按“上市前”处理且不警告，
之后所有复权因子都不同（静默错误）。规则：应有的第一天 = max(上市日, 请求的 start)；请求的
start 为空时就是上市日。第一根比它晚超过 ``HEAD_TOLERANCE_DAYS`` 天，判为开头被截断。上市日取
合约信息的 OpenDate；取不到上市日又没有 start 可参照时，保守地当成缺数据（下载）。

“应有的最近交易日”（``expected_last_day``）由交易日历确定，不由本地数据确定：本地数据本身可能
是旧的，用它推断最近交易日会把旧数据当成新数据。取不晚于 ``now`` 的最后一个交易日；当天是
交易日但还没收盘（早于 ``DAILY_READY_TIME``）时，当天的日线还不会有，取前一个交易日。

**停牌不是缺数据**：停牌日没有这一行，停牌中的证券最后一根天然早于最近交易日。区分办法是
“下载过一次”：缺数据的证券下载后重读，仍缺的，接受为停牌或退市，不循环下载。记录
（``_checked``）按“下载覆盖到的最早 start”判断：只下载过 start 之后的，之后要全历史（如
``adj_factors``）仍会再下载一次。记录在同一个“应有的最近交易日”内有效，新的交易日再试一次。

**当天的日线可能还没生成**：15:30 之后第一次同步时 MiniQMT 可能还没有当天的日线。下载后仍只缺
最新这一个交易日（前一个交易日有数据）的，记录只保留 ``RETRY_INTERVAL``，过后再读到仍缺就重新
下载，不会因为早上的一次同步让这只证券整天都读旧数据。缺得更多的（停牌多日、退市）记录整个
交易日有效。代价：停牌中、已退市的证券每个交易日下载一次（与修复前每次读取都下载相比已少得多）。

**未知不是空**：
- 下载失败（接口报错或超时）：抛 :class:`DailyDownloadError`，不退回本地的旧数据，也不当成没有
  数据。同步失败，下次照常重试
- 下载成功、重读后仍为空：返回空（MiniQMT 没有这只证券的日线，如早年退市的），记 info 日志
- 读取本身抛异常：原样抛出
- 交易日历取不到（返回空）：无法判断是否过期，保守地当成缺数据，退回“先下载再读”

并发：``daily`` 加锁，同一时刻只有一个读取或下载在进行（MiniQMT 那台机器的内存是硬约束，
串行也符合要求）。插件实例是模块级单例，平台里可能有多个同步任务共用它。
"""

from __future__ import annotations

import logging
import threading
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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


HEAD_TOLERANCE_DAYS = 7
"""本地第一根比应有的第一天晚多少个日历日以内，仍算开头完整（上市日与第一根日线之间、请求的
start 之后紧接着的停牌）。我们的取值，没有实测；取小了只会多下载一次。"""

RETRY_INTERVAL = timedelta(minutes=30)
"""下载后仍只缺最新一个交易日时，隔多久再允许重新下载。MiniQMT 生成当天日线的时间没有实测，
30 分钟是我们的取值。"""

_FULL_HISTORY = date.min
"""``_checked`` 里表示“全历史”的 start。"""


class DailyDownloadError(RuntimeError):
    """下载日线失败（接口报错或超时）：本地数据缺失或过期，又拿不到最新的，结果未知。"""


class DailyStore(Protocol):
    """能读本地日线、能下载日线的后端（LiveBackend，或测试里的假后端）。"""

    def read_daily(self, symbol: str, start: str | None) -> list[RawBar]:
        """只读 MiniQMT 本地已有的未复权日线，不下载。"""
        ...

    def download_daily(self, symbol: str, start: str | None) -> None:
        """让 MiniQMT 下载这只证券从 start（None 为全历史）起的日线到本地。失败抛异常。"""
        ...

    def trading_dates(self, market: str) -> list[int]: ...

    def listing_date(self, symbol: str) -> date | None:
        """上市日（合约信息的 OpenDate）；没有合约信息或没有 OpenDate 时返回 None。"""
        ...


def expected_last_day(days: Sequence[date], now: datetime) -> date | None:
    """应有的最近交易日（days 升序）。没有符合的返回 None。"""
    local = now.astimezone(SHANGHAI)
    cutoff = local.date() if local.time() >= DAILY_READY_TIME else local.date() - timedelta(days=1)
    i = bisect_right(days, cutoff)
    return days[i - 1] if i > 0 else None


def is_stale(bars: Sequence[RawBar], expected: date | None) -> bool:
    """末尾缺数据：为空，或最后一根早于应有的最近交易日。expected 未知时一律当成缺数据。"""
    if not bars:
        return True
    if expected is None:
        return True
    return max(shanghai_date(b.time_ms) for b in bars) < expected


def head_truncated(bars: Sequence[RawBar], start: date | None, listing: date | None) -> bool:
    """开头缺数据：第一根比应有的第一天 max(上市日, start) 晚超过 HEAD_TOLERANCE_DAYS 天。
    上市日和 start 都不知道时，无从判断，保守地当成缺数据。bars 为空由 is_stale 处理。"""
    if not bars:
        return False
    refs = [d for d in (listing, start) if d is not None]
    if not refs:
        return True
    first = min(shanghai_date(b.time_ms) for b in bars)
    return first - max(refs) > timedelta(days=HEAD_TOLERANCE_DAYS)


def _parse_start(start: str | None) -> date | None:
    if not start:
        return None
    return date(int(start[:4]), int(start[4:6]), int(start[6:8]))


@dataclass(frozen=True)
class _Checked:
    expected: date | None
    """下载时应有的最近交易日。"""
    covered: date
    """下载覆盖到的最早 start（_FULL_HISTORY 为全历史）。"""
    retry_after: datetime | None
    """不为 None：下载后只缺最新一个交易日，这个时间之后允许重新下载。"""


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
        self._checked: dict[str, _Checked] = {}
        self._listing: dict[str, date | None] = {}
        self._lock = threading.RLock()

    def _expected(self, now: datetime) -> date | None:
        today = now.astimezone(SHANGHAI).date()
        if self._calendar_loaded_on != today:
            days = {shanghai_date(t) for t in self._store.trading_dates("SH")}
            self._calendar = sorted(days)
            self._calendar_loaded_on = today
            if not self._calendar:
                logger.warning("交易日历为空，无法判断本地日线是否过期，按缺数据处理（先下载再读）")
        return expected_last_day(self._calendar, now)

    def _listing_date(self, symbol: str) -> date | None:
        if symbol not in self._listing:
            self._listing[symbol] = self._store.listing_date(symbol)
        return self._listing[symbol]

    def _needs_download(
        self, symbol: str, bars: Sequence[RawBar], expected: date | None, start: date | None
    ) -> bool:
        if is_stale(bars, expected):
            return True
        # 末尾新。有 start 且第一根离 start 不远时，应有的第一天 max(上市日, start) 不会比 start
        # 早，开头肯定完整，不用取合约信息（读取 start 之后的日线时的常见情形）
        first = min(shanghai_date(b.time_ms) for b in bars)
        if start is not None and first - start <= timedelta(days=HEAD_TOLERANCE_DAYS):
            return False
        return head_truncated(bars, start, self._listing_date(symbol))

    def _already_tried(
        self, symbol: str, expected: date | None, start: date | None, now: datetime
    ) -> bool:
        c = self._checked.get(symbol)
        if c is None or c.expected != expected or c.covered > (start or _FULL_HISTORY):
            return False
        return c.retry_after is None or now < c.retry_after

    def daily(self, symbol: str, start: str | None) -> list[RawBar]:
        with self._lock:
            return self._daily(symbol, start)

    def _daily(self, symbol: str, start: str | None) -> list[RawBar]:
        now = self._now()
        expected = self._expected(now)
        start_day = _parse_start(start)
        bars = self._store.read_daily(symbol, start)
        if not self._needs_download(symbol, bars, expected, start_day):
            return bars
        if self._already_tried(symbol, expected, start_day, now):
            # 这个交易日里已经为它下载过，仍然缺：停牌、退市或本来就没有，接受
            return bars
        try:
            self._store.download_daily(symbol, start)
        except Exception as exc:
            raise DailyDownloadError(
                f"{symbol} 的本地日线{'为空' if not bars else '缺数据'}，下载又失败，"
                f"结果未知，不使用旧数据：{exc}"
            ) from exc
        bars = self._store.read_daily(symbol, start)
        self._checked[symbol] = _Checked(
            expected,
            start_day or _FULL_HISTORY,
            now + RETRY_INTERVAL if self._only_latest_missing(bars, expected) else None,
        )
        if not bars:
            logger.info("%s：下载后 MiniQMT 仍没有日线，按没有数据处理", symbol)
        elif self._needs_download(symbol, bars, expected, start_day):
            logger.debug("%s：下载后日线仍缺开头或末尾，按停牌、退市或没有更早的数据接受", symbol)
        return bars

    def _only_latest_missing(self, bars: Sequence[RawBar], expected: date | None) -> bool:
        """下载后只缺最新一个交易日（前一个交易日有数据）：可能是当天的日线还没生成。"""
        if not bars or expected is None or not is_stale(bars, expected):
            return False
        i = bisect_left(self._calendar, expected)
        if i == 0:
            return False
        return max(shanghai_date(b.time_ms) for b in bars) >= self._calendar[i - 1]
