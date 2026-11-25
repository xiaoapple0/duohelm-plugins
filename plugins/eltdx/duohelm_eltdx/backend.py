"""对 eltdx 的最小封装。

插件逻辑只通过 :class:`Backend` 取数，接口里只有简单的数据（日期、整数、浮点数、字符串）：

- :class:`LiveBackend` 连接通达信主站（需要网络）
- :class:`RecordingBackend` 包在 LiveBackend 外面，把每次调用的结果记下来
- :class:`ReplayBackend` 从录制文件回放，测试用，不访问网络
"""

from __future__ import annotations

import atexit
import gzip
import json
import logging
import threading
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal, Protocol, cast

logger = logging.getLogger(__name__)

BarKind = Literal["stock", "delisted", "index"]
PAGE_SIZE = 800  # eltdx 单页上限


@dataclass(frozen=True)
class DailyBar:
    date: date
    open_milli: int
    high_milli: int
    low_milli: int
    close_milli: int
    volume: float
    """股数（eltdx 的 volume_wire_value；volume_lots 是带小数的手数，不用）。"""
    amount: float
    """元。"""


@dataclass(frozen=True)
class CodeRow:
    full_code: str
    name: str


@dataclass(frozen=True)
class CapitalRecord:
    date: date
    category: int
    c1: float
    c2: float
    c3: float
    c4: float


@dataclass(frozen=True)
class IndexMemberRow:
    full_code: str
    join_date: date | None


class Backend(Protocol):
    def code_table(self) -> list[CodeRow]:
        """当前在市的 A 股（代码表只有在市证券）。"""
        ...

    def daily_page(self, full_code: str, kind: BarKind, offset: int) -> list[DailyBar]:
        """未复权日线的一页，按日期升序。offset=0 是最近的一页，往前翻 PAGE_SIZE 根一页。"""
        ...

    def capital_changes(self, full_code: str) -> list[CapitalRecord]: ...

    def index_members(self, index_code: str) -> list[IndexMemberRow]:
        """index_code 为 6 位指数代码，例如 000300。"""
        ...


# ---------------------------------------------------------------- 真实连接


class LiveBackend:
    """连接通达信主站。第一次取数时才连接，进程退出时关闭。"""

    def __init__(self, *, timeout: float = 15.0, attempts: int = 5) -> None:
        self._timeout = timeout
        self._attempts = attempts
        self._client: Any = None
        self._lock = threading.Lock()

    def _get(self) -> Any:
        with self._lock:
            if self._client is None:
                import eltdx  # noqa: PLC0415  导入插件时不联网，也不加载原生模块

                last: Exception | None = None
                for _ in range(self._attempts):
                    client = eltdx.TdxClient.from_hosts(timeout=self._timeout)
                    try:
                        client.connect()
                    except Exception as exc:  # eltdx 的连接错误类型不稳定，统一重试
                        last = exc
                        client.close()
                        continue
                    self._client = client
                    atexit.register(client.close)
                    break
                if self._client is None:
                    raise ConnectionError(f"无法连接通达信主站：{last!r}") from last
            return self._client

    def code_table(self) -> list[CodeRow]:
        rows = self._get().codes.latest_stock_list()
        return [CodeRow(full_code=str(r.full_code), name=str(r.name)) for r in rows]

    def daily_page(self, full_code: str, kind: BarKind, offset: int) -> list[DailyBar]:
        client = self._get()
        if kind == "delisted":
            series = client.delisted_bars.get(full_code, start=offset, count=PAGE_SIZE)
        elif kind == "index":
            series = client.bars.get(
                full_code, period="day", start=offset, count=PAGE_SIZE, kind="index"
            )
        else:
            series = client.bars.get(full_code, period="day", start=offset, count=PAGE_SIZE)
        bars = sorted(series.bars, key=lambda b: b.time)
        return [
            DailyBar(
                date=b.time.date(),
                open_milli=int(b.open_price_milli),
                high_milli=int(b.high_price_milli),
                low_milli=int(b.low_price_milli),
                close_milli=int(b.close_price_milli),
                volume=float(b.volume_wire_value),
                amount=float(b.amount),
            )
            for b in bars
        ]

    def capital_changes(self, full_code: str) -> list[CapitalRecord]:
        response = self._get().corporate.capital_changes(full_code)
        return [
            CapitalRecord(
                date=r.date,
                category=int(r.category_raw),
                c1=float(r.c1_value),
                c2=float(r.c2_value),
                c3=float(r.c3_value),
                c4=float(r.c4_value),
            )
            for r in response.records
            if r.date is not None
        ]

    def index_members(self, index_code: str) -> list[IndexMemberRow]:
        table = self._get().helpers.index_members(index_code)
        rows = getattr(table, "rows", None) or getattr(table, "items", None) or table
        return [
            IndexMemberRow(full_code=str(r.full_code), join_date=_as_date(r.join_date))
            for r in rows
            if r.full_code
        ]


def _as_date(value: object) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    text = str(value).replace("-", "")
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


# ---------------------------------------------------------------- 录制与回放


def _key(method: str, *args: object) -> str:
    return json.dumps([method, *args], ensure_ascii=False)


def _encode(value: Any) -> Any:
    if isinstance(value, list):
        return [_encode(v) for v in cast(list[Any], value)]
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if hasattr(value, "__dataclass_fields__"):
        return {"$type": type(value).__name__, **{k: _encode(v) for k, v in asdict(value).items()}}
    return value


_TYPES: dict[str, type] = {
    t.__name__: t for t in (DailyBar, CodeRow, CapitalRecord, IndexMemberRow)
}


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in cast(list[Any], value)]
    if isinstance(value, dict):
        obj = cast(dict[str, Any], value)
        if "$date" in obj:
            return date.fromisoformat(str(obj["$date"]))
        if "$type" in obj:
            cls = _TYPES[str(obj["$type"])]
            return cls(**{k: _decode(v) for k, v in obj.items() if k != "$type"})
        return obj
    return value


class RecordingBackend:
    """调用真实后端，同时记录每次调用的结果。``save`` 写出 gzip 压缩的 JSON。"""

    def __init__(self, inner: Backend) -> None:
        self._inner = inner
        self.calls: dict[str, Any] = {}

    def _record(self, key: str, value: Any) -> Any:
        self.calls[key] = _encode(value)
        return value

    def code_table(self) -> list[CodeRow]:
        return self._record(_key("code_table"), self._inner.code_table())

    def daily_page(self, full_code: str, kind: BarKind, offset: int) -> list[DailyBar]:
        key = _key("daily_page", full_code, kind, offset)
        return self._record(key, self._inner.daily_page(full_code, kind, offset))

    def capital_changes(self, full_code: str) -> list[CapitalRecord]:
        key = _key("capital_changes", full_code)
        return self._record(key, self._inner.capital_changes(full_code))

    def index_members(self, index_code: str) -> list[IndexMemberRow]:
        key = _key("index_members", index_code)
        return self._record(key, self._inner.index_members(index_code))

    def save(self, path: Path, meta: dict[str, Any]) -> None:
        body = {"meta": meta, "calls": dict(sorted(self.calls.items()))}
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(raw, mtime=0))


class NotRecordedError(KeyError):
    def __init__(self, key: str) -> None:
        super().__init__(f"录制文件里没有这次调用：{key}（需要重新运行 scripts/capture_eltdx.py）")


class ReplayBackend:
    """从录制文件回放，不访问网络。"""

    def __init__(self, path: Path) -> None:
        body = json.loads(gzip.decompress(path.read_bytes()))
        self.meta: dict[str, Any] = body["meta"]
        self._calls: dict[str, Any] = body["calls"]

    def _get(self, key: str) -> Any:
        if key not in self._calls:
            raise NotRecordedError(key)
        return _decode(self._calls[key])

    def code_table(self) -> list[CodeRow]:
        return self._get(_key("code_table"))

    def daily_page(self, full_code: str, kind: BarKind, offset: int) -> list[DailyBar]:
        return self._get(_key("daily_page", full_code, kind, offset))

    def capital_changes(self, full_code: str) -> list[CapitalRecord]:
        return self._get(_key("capital_changes", full_code))

    def index_members(self, index_code: str) -> list[IndexMemberRow]:
        return self._get(_key("index_members", index_code))
