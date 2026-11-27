"""eltdx 行情源：实现 duohelm-sdk 0.4.1 起（<0.5）的 DataSourcePlugin。

数据特性（2026-11-25 用 eltdx 3.3.5 实测，见 DuoHelm 的 docs/subsystems/data.md）：

- 代码表只有在市证券，不能直接列出退市证券（``lists_delisted=False``）；退市证券的
  日线用专用的退市接口取，平台用 ``lookup_securities`` 扫描代码区间发现退市股
- 历史停牌日没有这一行；当前停牌的股票当天有一根成交量为 0 的占位 K 线，丢弃
- 服务器的复权价是线性变换，会出现负数价格，不使用；复权因子由除权除息记录在本地
  按比例计算（``duohelm_sdk.adjust.factors_from_events``）
- 成交量单位随证券类型而不同：股票、ETF、北交所是股，**指数是手**（乘 100 换成股）；
  类型无法判断的证券（债券、B 股等）拒绝返回日线，见 ``units.py``
- 没有交易日历接口：用上证指数和深证成指的日线推出交易日，取并集
- 指数成分只有当前名单和纳入日期，没有调出记录
- 拿不到摘牌日：``lookup_securities`` 的 ``delist_date`` 是最后一根有成交的 K 线的日期，
  是 SDK 口径（摘牌日之前的最后一个交易日）的**下界**。退市前停牌的证券会偏早，如中国北车
  2015-05-06（SDK 口径 2015-05-19）、*ST二重 2014-04-28（2015-05-20）。因此声明
  ``exact_delist_date=False``（SDK 0.4.1 起），平台把这里的退市日记为近似值
  （数据集 ``delist_date_quality``）。同一证券有精确数据源（xtdata）时以精确的为准，这个逻辑在
  平台的 xiaoapple0/duohelm#89 里，平台版本含该改动才生效；在那之前，后同步的数据源会覆盖退市日。
  算退市日前先丢掉成交量为 0 的占位行
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date

import polars as pl
from duohelm_sdk import Capabilities, Freq
from duohelm_sdk.adjust import EX_EVENTS_SCHEMA, factors_from_events
from duohelm_sdk.schema import ADJ_FACTORS_SCHEMA, BARS_SCHEMA, SECURITIES_SCHEMA

from duohelm_eltdx.backend import PAGE_SIZE, Backend, BarKind, DailyBar, LiveBackend
from duohelm_eltdx.codes import exchange_of, to_eltdx, to_symbol
from duohelm_eltdx.units import security_type, shares_per_volume_unit

logger = logging.getLogger(__name__)

CALENDAR_INDICES = ("sh000001", "sz399001")
"""推出交易日用的指数：上证指数、深证成指。北交所沿用深市的交易日。"""
EX_RIGHTS_CATEGORY = 1
"""capital_changes 里“除权除息”的类别。其他类别（股本变化、送认沽权证等）不参与复权。"""
INDEX_CONSTITUENTS_SCHEMA: dict[str, pl.DataType] = {
    "symbol": pl.String(),
    "in_date": pl.Date(),
    "out_date": pl.Date(),
}
_MAX_PAGES = 200


class EltdxSource:
    def __init__(self, backend: Backend | None = None) -> None:
        self._backend: Backend = backend if backend is not None else LiveBackend()
        self._listed: dict[str, str] | None = None

    # ---- 证券列表 ----

    def capabilities(self) -> Capabilities:
        return Capabilities(
            markets=("CN",),
            freqs=(Freq.DAY,),
            fields=("open", "high", "low", "close", "volume", "amount"),
            lists_delisted=False,
            exact_delist_date=False,  # 拿不到摘牌日，退市日是最后一根有成交的 K 线的日期
        )

    def securities(self, *, include_delisted: bool = False) -> pl.DataFrame:
        if include_delisted:
            raise NotImplementedError("eltdx 的代码表只有在市证券，不能直接列出退市证券")
        rows = [
            (to_symbol(code), name, exchange_of(to_symbol(code)), "stock", None, None)
            for code, name in sorted(self._listed_codes().items())
        ]
        return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row")

    def lookup_securities(self, symbols: Sequence[str]) -> pl.DataFrame:
        listed = self._listed_codes()
        rows: list[tuple[object, ...]] = []
        for symbol in dict.fromkeys(symbols):
            code = to_eltdx(symbol)
            # 上市日、退市日只看有成交的 K 线，与 bars() 一致：成交量为 0 的是停牌占位行
            bars = [b for b in self._history(code, "delisted") if b.volume > 0]
            if not bars:
                continue  # 数据源没有这个代码
            is_listed = code in listed
            rows.append(
                (
                    symbol,
                    listed.get(code),  # 退市股的名称取不到
                    exchange_of(symbol),
                    "stock",
                    bars[0].date,
                    # 近似值：eltdx 拿不到摘牌日，只能给出最后一根有成交的 K 线。SDK 的
                    # delist_date 是摘牌日之前的最后一个交易日，退市前停牌的证券会比这里晚
                    None if is_listed else bars[-1].date,
                )
            )
        return pl.DataFrame(rows, schema=SECURITIES_SCHEMA, orient="row")

    # ---- 日线 ----

    def trading_calendar(self, market: str, start: date, end: date) -> pl.DataFrame:
        if market != "CN":
            raise ValueError(f"只支持 CN 市场，收到 {market!r}")
        days: set[date] = set()
        for code in CALENDAR_INDICES:
            days.update(b.date for b in self._since(code, "index", start) if start <= b.date <= end)
        return pl.DataFrame({"date": sorted(days)}, schema={"date": pl.Date()})

    def bars(self, symbols: Sequence[str], freq: Freq, start: date, end: date) -> pl.DataFrame:
        if freq != Freq.DAY:
            raise ValueError(f"eltdx 插件只支持日线，收到 {freq}")
        frames: list[pl.DataFrame] = []
        for symbol in dict.fromkeys(symbols):
            code = to_eltdx(symbol)
            per_unit = shares_per_volume_unit(symbol)  # 类型未知时抛错，不返回单位不明的成交量
            kind = self._bar_kind(symbol, code)
            picked = [
                b
                for b in self._since(code, kind, start)
                if start <= b.date <= end and b.volume > 0  # 成交量为 0：停牌占位行
            ]
            frames.append(_bars_frame(symbol, picked, per_unit))
        if not frames:
            return pl.DataFrame(schema=BARS_SCHEMA)
        return pl.concat(frames).sort("symbol", "date")

    # ---- 复权因子 ----

    def adj_factors(self, symbols: Sequence[str], start: date, end: date) -> pl.DataFrame:
        frames: list[pl.DataFrame] = []
        for symbol in dict.fromkeys(symbols):
            code = to_eltdx(symbol)
            events = self._ex_events(symbol, code)
            if events.height == 0:
                continue
            kind = self._bar_kind(symbol, code)
            # 这里只用收盘价，成交量不参与计算，单位按 1 处理（不要求证券类型已知）
            closes = _bars_frame(
                symbol, [b for b in self._history(code, kind) if b.volume > 0], per_unit=1
            )
            factors = factors_from_events(closes, events, on_invalid="skip")
            skipped = events.height - _matched_events(closes, events)
            if skipped:
                logger.warning(
                    "%s：%d 个除权事件没有产生因子（上市前或参考价不为正）", symbol, skipped
                )
            frames.append(factors.filter(pl.col("date").is_between(start, end)))
        if not frames:
            return pl.DataFrame(schema=ADJ_FACTORS_SCHEMA)
        return pl.concat(frames).sort("symbol", "date")

    def _ex_events(self, symbol: str, code: str) -> pl.DataFrame:
        rows = [
            # eltdx 的数据按“每 10 股”：c1 派现、c2 配股价、c3 送转股、c4 配股
            (r.date, symbol, r.c1 / 10, r.c3 / 10, r.c4 / 10, r.c2)
            for r in self._backend.capital_changes(code)
            if r.category == EX_RIGHTS_CATEGORY
        ]
        return pl.DataFrame(rows, schema=EX_EVENTS_SCHEMA, orient="row")

    # ---- 指数成分 ----

    def index_constituents(self, index: str, start: date, end: date) -> pl.DataFrame:
        """只有当前成分：in_date 为纳入日期，out_date 全为 null（eltdx 没有调出记录）。"""
        code = to_eltdx(index)[2:]
        rows = [
            (to_symbol(m.full_code), m.join_date, None)
            for m in self._backend.index_members(code)
            if m.join_date is None or m.join_date <= end
        ]
        del start  # 没有调出记录，无法按起始日期排除已调出的成分
        frame = pl.DataFrame(rows, schema=INDEX_CONSTITUENTS_SCHEMA, orient="row")
        return frame.sort("symbol")

    # ---- 内部 ----

    def _bar_kind(self, symbol: str, code: str) -> BarKind:
        if security_type(symbol) == "index":
            return "index"
        return "stock" if code in self._listed_codes() else "delisted"

    def _listed_codes(self) -> dict[str, str]:
        if self._listed is None:
            self._listed = {row.full_code: row.name for row in self._backend.code_table()}
        return self._listed

    def _since(self, code: str, kind: BarKind, start: date) -> list[DailyBar]:
        """从最近的一页往前翻，直到翻过 start 或没有更早的数据。按日期升序返回。"""
        pages: list[list[DailyBar]] = []
        for i in range(_MAX_PAGES):
            page = self._backend.daily_page(code, kind, i * PAGE_SIZE)
            if not page:
                break
            pages.append(page)
            if page[0].date <= start or len(page) < PAGE_SIZE:
                break
        else:
            raise RuntimeError(f"{code} 翻了 {_MAX_PAGES} 页仍未到 {start}")
        return [b for page in reversed(pages) for b in page]

    def _history(self, code: str, kind: BarKind) -> list[DailyBar]:
        return self._since(code, kind, date.min)


def _bars_frame(symbol: str, bars: Sequence[DailyBar], per_unit: int) -> pl.DataFrame:
    """日线 -> BARS_SCHEMA。成交量乘 per_unit 换成股（见 units.py）。"""
    rows = [
        (
            b.date,
            symbol,
            b.open_milli / 1000,
            b.high_milli / 1000,
            b.low_milli / 1000,
            b.close_milli / 1000,
            b.volume * per_unit,
            b.amount,
        )
        for b in bars
    ]
    return pl.DataFrame(rows, schema=BARS_SCHEMA, orient="row")


def _matched_events(closes: pl.DataFrame, events: pl.DataFrame) -> int:
    """有前收盘价的事件数（上市前的事件没有）。只用于日志。"""
    if closes.height == 0:
        return 0
    first = closes.get_column("date").min()
    return events.filter(pl.col("date") > first).height
