"""MiniQMT（迅投 xtdata）行情源：实现 duohelm-sdk 0.4.2 起（<0.5）的 DataSourcePlugin 和连接检查。

MiniQMT 的地址是清单里的 endpoint ``miniqmt``，由用户在 DuoHelm 插件页确认；确认之前
插件拿不到地址，:meth:`XtdataSource.check_connection` 返回提示。

数据特性（2026-11-25 实测，普通版，见 DuoHelm 的 docs/subsystems/data.md）：

- 日期一律由时间戳按 Asia/Shanghai 换算（见 dates.py）
- 能直接列出退市证券（``lists_delisted=True``），但早年退市的不全；平台再用 eltdx 补全
- 停牌日没有这一行；另有少量成交量为 0 的占位行，丢弃
- 已退市证券的日线只有成交量，开高低收和成交额全为 0（2026-11-28 实测，抽查 41 只退市股
  全部如此，复权方式换成 front/back 也拿不到可信的价格）。价格不是有限正数的行是“不知道”，
  不是 0，丢弃并记警告；这类证券的行情要用别的数据源（eltdx）补
- 成交量单位为手，乘以每手股数换成股
- 除权数据按每股计；用 duohelm_sdk.adjust.factors_from_events 计算因子，并与 xtdata 的
  除权系数 dr 交叉核对
- 沪深两市的交易日相同；普通版没有历史指数成分，只能返回当前成分
"""

from __future__ import annotations

import logging
import math
from bisect import bisect_left
from collections.abc import Sequence
from datetime import date, timedelta

import polars as pl
from duohelm_sdk import (
    AddressError,
    Capabilities,
    ConnectionStatus,
    EndpointNotConfiguredError,
    Freq,
    LookupFailedError,
)
from duohelm_sdk.adjust import EX_EVENTS_SCHEMA, factors_from_events
from duohelm_sdk.schema import ADJ_FACTORS_SCHEMA, BARS_SCHEMA, SECURITIES_SCHEMA

from duohelm_xtdata.backend import (
    Backend,
    LiveBackend,
    MiniQMTUnavailableError,
    MissingLiveDependencyError,
    RawBar,
)
from duohelm_xtdata.config import ConfigError
from duohelm_xtdata.dates import (
    SHARES_PER_VOLUME_UNIT,
    parse_yyyymmdd,
    security_type,
    shanghai_date,
)

logger = logging.getLogger(__name__)

LISTED_SECTORS = ("沪深A股", "京市A股")
INDEX_SECTORS: dict[str, str] = {
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
    "000016.SH": "上证50",
}
"""指数代码 -> MiniQMT 的板块名称。普通版只能取当前成分。"""
INDEX_CONSTITUENTS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String(),
    "in_date": pl.Date(),
    "out_date": pl.Date(),
}
DR_TOLERANCE = 1e-3
"""本地算出的除权比例与 xtdata 的 dr 相差超过这个相对值时记警告。"""
_START_BUFFER = timedelta(days=5)
"""传给 xtdata 的起始日期多留几天：日期字符串和时间戳之间可能有时区偏差，最后按换算后的日期过滤。"""


class XtdataSource:
    def __init__(self, backend: Backend | None = None) -> None:
        self._backend: Backend = backend if backend is not None else LiveBackend()
        self._calendar: list[date] | None = None
        self._expired: set[str] | None = None

    # ---- 连接检查（SupportsConnectionCheck） ----

    def check_connection(self) -> ConnectionStatus:
        """平台在同步任务开始前调用。地址没确认、缺少依赖或 xtquant、配置文件有误、连不上时
        返回 ok=False 和原因，不抛异常。"""
        backend = self._backend
        if not isinstance(backend, LiveBackend):
            return ConnectionStatus(ok=True, message="使用回放或测试数据，不连接 MiniQMT")
        try:
            backend.connect()
        except (
            EndpointNotConfiguredError,
            AddressError,
            ConfigError,
            MissingLiveDependencyError,
            MiniQMTUnavailableError,
        ) as exc:
            return ConnectionStatus(ok=False, message=str(exc))
        return ConnectionStatus(ok=True, message=f"已连接 MiniQMT（{backend.address()}）")

    # ---- 证券列表 ----

    def capabilities(self) -> Capabilities:
        return Capabilities(
            markets=("CN",),
            freqs=(Freq.DAY,),
            fields=("open", "high", "low", "close", "volume", "amount"),
            lists_delisted=True,
        )

    def securities(self, *, include_delisted: bool = False) -> pl.DataFrame:
        listed: dict[str, None] = {}
        for name in LISTED_SECTORS:
            listed.update(dict.fromkeys(self._backend.sector(name)))
        codes = list(listed)
        if include_delisted:
            codes += [c for c in self._expired_codes() if c not in listed]
        return self._security_rows(codes)

    def lookup_securities(self, symbols: Sequence[str]) -> pl.DataFrame:
        """查询给定代码。返回空（或结果里没有某个代码）只表示 MiniQMT 确认没有它的合约信息；
        连不上、接口报错、返回结构异常、任何一个代码拿不到，一律抛 ``LookupFailedError``
        （保留原异常为 ``__cause__``），不返回空，也不返回只含已拿到部分的结果。"""
        try:
            return self._security_rows(list(dict.fromkeys(symbols)), strict=True)
        except LookupFailedError:
            raise
        except Exception as exc:
            raise LookupFailedError(f"MiniQMT 查询证券失败：{exc}") from exc

    def _security_rows(self, codes: Sequence[str], *, strict: bool = False) -> pl.DataFrame:
        """strict：查询用。已过期的合约算不出退市日时抛错，不静默当成在市。"""
        expired: set[str] = self._expired_codes() if codes else set()
        if strict and codes and not expired:
            # 过期列表为空不可信：不检查的话，已退市的代码会被当成在市，退市日为空
            raise ValueError("MiniQMT 返回的过期沪深 A 股列表为空")
        rows: list[tuple[object, ...]] = []
        missing = 0
        for symbol in codes:
            info = self._backend.instrument(symbol)
            if info is None:
                if strict and symbol in expired:
                    raise ValueError(f"{symbol} 在过期列表里，但 MiniQMT 没有它的合约信息")
                missing += 1
                continue
            delist = None
            if symbol in expired:
                delist = self._last_trading_day_before(parse_yyyymmdd(info.expire_date))
                if strict and delist is None:
                    raise ValueError(
                        f"{symbol} 在过期列表里，但算不出退市日（缺少摘牌日或交易日历）"
                    )
            exchange = symbol.rsplit(".", 1)[1]
            rows.append(
                (symbol, info.name, exchange, "stock", parse_yyyymmdd(info.open_date), delist)
            )
        if missing:
            logger.info("%d 个代码在 MiniQMT 里没有合约信息，已跳过", missing)
        return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row").sort("symbol")

    def _expired_codes(self) -> set[str]:
        if self._expired is None:
            self._expired = set(self._backend.expired_a_shares())
        return self._expired

    def _last_trading_day_before(self, day: date | None) -> date | None:
        """退市日（ExpireDate 是摘牌日）之前的最后一个交易日。"""
        if day is None:
            return None
        calendar = self._trading_days()
        i = bisect_left(calendar, day)
        return calendar[i - 1] if i > 0 else None

    # ---- 日线与交易日 ----

    def trading_calendar(self, market: str, start: date, end: date) -> pl.DataFrame:
        if market != "CN":
            raise ValueError(f"只支持 CN 市场，收到 {market!r}")
        days = [d for d in self._trading_days() if start <= d <= end]
        return pl.DataFrame({"date": days}, schema={"date": pl.Date()})

    def _trading_days(self) -> list[date]:
        if self._calendar is None:
            days: set[date] = set()
            for market in ("SH", "SZ"):
                days.update(shanghai_date(t) for t in self._backend.trading_dates(market))
            self._calendar = sorted(days)
        return self._calendar

    def bars(self, symbols: Sequence[str], freq: Freq, start: date, end: date) -> pl.DataFrame:
        if freq != Freq.DAY:
            raise ValueError(f"xtdata 插件只支持日线，收到 {freq}")
        query_start = (start - _START_BUFFER).strftime("%Y%m%d")
        frames = [
            _bars_frame(symbol, self._backend.daily(symbol, query_start), start, end)
            for symbol in dict.fromkeys(symbols)
        ]
        if not frames:
            return pl.DataFrame(schema=BARS_SCHEMA)
        return pl.concat(frames).sort("symbol", "date")

    # ---- 复权因子 ----

    def adj_factors(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
        frames: list[pl.DataFrame] = []
        for symbol in dict.fromkeys(symbols):
            divids = self._backend.divid(symbol)
            if not divids:
                continue
            events = pl.DataFrame(
                [
                    (
                        shanghai_date(d.time_ms),
                        symbol,
                        d.interest,
                        d.stock_bonus + d.stock_gift,
                        d.allot_num,
                        d.allot_price,
                    )
                    for d in divids
                ],
                schema=EX_EVENTS_SCHEMA,
                orient="row",
            )
            # 价格无效的行在 bars() 里已经警告过，这里不重复
            closes = _bars_frame(
                symbol, self._backend.daily(symbol, None), date.min, date.max, warn=False
            )
            factors = factors_from_events(closes, events, on_invalid="skip")
            _check_against_dr(symbol, factors, {shanghai_date(d.time_ms): d.dr for d in divids})
            frames.append(factors.filter(pl.col("date").is_between(start, end)))
        if not frames:
            return pl.DataFrame(schema=ADJ_FACTORS_SCHEMA)
        return pl.concat(frames).sort("symbol", "date")

    # ---- 指数成分 ----

    def index_constituents(self, index: str, start: date, end: date) -> pl.DataFrame:
        """只有当前成分：普通版取不到历史成分，in_date 和 out_date 都为 null。"""
        del start, end
        sector = INDEX_SECTORS.get(index)
        if sector is None:
            raise ValueError(f"不支持的指数 {index!r}，支持：{sorted(INDEX_SECTORS)}")
        rows = [(s, None, None) for s in sorted(set(self._backend.sector(sector)))]
        return pl.DataFrame(rows, schema=INDEX_CONSTITUENTS_SCHEMA, orient="row")


def _bars_frame(
    symbol: str, raw: Sequence[RawBar], start: date, end: date, *, warn: bool = True
) -> pl.DataFrame:
    """原始日线 -> BARS_SCHEMA：日期按 Asia/Shanghai 换算，成交量换成股，丢弃成交量为 0 的行
    和价格不是有限正数的行（记警告）。

    xtdata 对已退市证券返回的价格全为 0。这些行不能交给平台：平台按主键覆盖写入，会盖掉别的
    数据源写的真实价格；拿来算复权因子时，除权前收盘价为 0 会让比例为 0，之后的因子全部为 0。
    """
    per_unit = SHARES_PER_VOLUME_UNIT[security_type(symbol)]
    rows: list[tuple[object, ...]] = []
    bad_price = 0
    for b in raw:
        day = shanghai_date(b.time_ms)
        if not start <= day <= end or b.volume <= 0:
            continue
        if not all(_positive(x) for x in (b.open, b.high, b.low, b.close)):
            bad_price += 1
            continue
        rows.append(
            (
                day,
                symbol,
                round(b.open, 3),
                round(b.high, 3),
                round(b.low, 3),
                round(b.close, 3),
                b.volume * per_unit,
                b.amount,
            )
        )
    if bad_price and warn:
        logger.warning(
            "%s：%d 行日线的价格不是正数（xtdata 对已退市证券只给成交量），已丢弃",
            symbol,
            bad_price,
        )
    return pl.DataFrame(rows, schema=BARS_SCHEMA, orient="row")


def _positive(value: float) -> bool:
    return math.isfinite(value) and value > 0


def _check_against_dr(symbol: str, factors: pl.DataFrame, dr: dict[date, float]) -> None:
    """本地算出的每次除权比例与 xtdata 的 dr 交叉核对，相差较大的记警告（质量报告用）。"""
    ratios = factors.with_columns(
        (pl.col("factor") / pl.col("factor").shift(1).fill_null(1.0)).alias("ratio")
    )
    for day, ratio in zip(ratios["date"].to_list(), ratios["ratio"].to_list(), strict=True):
        expected = dr.get(day)
        if expected and expected > 0 and abs(ratio - expected) / expected > DR_TOLERANCE:
            logger.warning(
                "%s %s：本地除权比例 %.6f 与 xtdata 的 dr %.6f 相差 %.3f%%",
                symbol,
                day,
                ratio,
                expected,
                abs(ratio - expected) / expected * 100,
            )
