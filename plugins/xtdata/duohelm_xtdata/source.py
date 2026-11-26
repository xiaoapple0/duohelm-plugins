"""MiniQMT（迅投 xtdata）行情源：实现 duohelm-sdk 0.2 的 DataSourcePlugin。

数据特性（2026-11-25 实测，普通版，见 DuoHelm 的 docs/subsystems/data.md）：

- 日期一律由时间戳按 Asia/Shanghai 换算（见 dates.py）
- 能直接列出退市证券（``lists_delisted=True``），但早年退市的不全；平台再用 eltdx 补全
- 停牌日没有这一行；另有少量成交量为 0 的占位行，丢弃
- 成交量单位为手，乘以每手股数换成股
- 除权数据按每股计；用 duohelm_sdk.adjust.factors_from_events 计算因子，并与 xtdata 的
  除权系数 dr 交叉核对
- 沪深两市的交易日相同；普通版没有历史指数成分，只能返回当前成分
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from collections.abc import Sequence
from datetime import date, timedelta

import polars as pl
from duohelm_sdk import Capabilities, Freq
from duohelm_sdk.adjust import EX_EVENTS_SCHEMA, factors_from_events
from duohelm_sdk.schema import ADJ_FACTORS_SCHEMA, BARS_SCHEMA, SECURITIES_SCHEMA

from duohelm_xtdata.backend import Backend, LiveBackend, RawBar
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
        return self._security_rows(list(dict.fromkeys(symbols)))

    def _security_rows(self, codes: Sequence[str]) -> pl.DataFrame:
        expired: set[str] = self._expired_codes() if codes else set()
        rows: list[tuple[object, ...]] = []
        missing = 0
        for symbol in codes:
            info = self._backend.instrument(symbol)
            if info is None:
                missing += 1
                continue
            delist = None
            if symbol in expired:
                delist = self._last_trading_day_before(parse_yyyymmdd(info.expire_date))
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
            closes = _bars_frame(symbol, self._backend.daily(symbol, None), date.min, date.max)
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


def _bars_frame(symbol: str, raw: Sequence[RawBar], start: date, end: date) -> pl.DataFrame:
    """原始日线 -> BARS_SCHEMA：日期按 Asia/Shanghai 换算，成交量换成股，丢弃成交量为 0 的行。"""
    per_unit = SHARES_PER_VOLUME_UNIT[security_type(symbol)]
    rows: list[tuple[object, ...]] = []
    for b in raw:
        day = shanghai_date(b.time_ms)
        if not start <= day <= end or b.volume <= 0:
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
    return pl.DataFrame(rows, schema=BARS_SCHEMA, orient="row")


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
