"""对 xtquant.xtdata 的最小封装。

插件逻辑只通过 :class:`Backend` 取数，接口只返回**原始**数据：毫秒时间戳、原始成交量
（手）和原始价格。日期按 Asia/Shanghai 换算、单位换算都在插件逻辑里做，回放测试因此能
覆盖它们。

- :class:`LiveBackend` 连接 MiniQMT（在平台里用插件页确认的地址，否则用本机配置文件），
  只有它会 import xtquant
- :class:`RecordingBackend` 包在 LiveBackend 外面记录每次调用的结果
- :class:`ReplayBackend` 从录制文件回放；测试只用它和手工构造的后端，不 import xtquant
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from duohelm_sdk import Address, endpoint
from duohelm_sdk.context import in_platform

from duohelm_xtdata.config import MiniQMTConfig, load_config

BAR_FIELDS = ["time", "open", "high", "low", "close", "volume", "amount"]
ENDPOINT_ID = "miniqmt"
"""清单 ``[[permissions.endpoints]]`` 里 MiniQMT 的 id。"""


LIVE_DEPENDENCIES = ("pandas", "numpy")
"""xtquant 运行时需要、插件本身不 import 的包（duohelm-plugins 的 ``[live]`` 可选依赖组）。"""
LIVE_INSTALL_HINT = (
    "连接 MiniQMT 需要 pandas 和 numpy（xtquant 运行时依赖），当前环境缺少：{missing}。"
    "安装：uv sync --extra live，或 pip install pandas numpy"
)


class MiniQMTUnavailableError(ConnectionError):
    """连不上 MiniQMT。"""


class MissingLiveDependencyError(ImportError):
    """缺少连接 MiniQMT 所需的 pandas / numpy。"""


def missing_live_dependencies() -> list[str]:
    """检查 pandas、numpy 是否已安装（只查找，不 import）。"""
    return [name for name in LIVE_DEPENDENCIES if importlib.util.find_spec(name) is None]


@dataclass(frozen=True)
class RawBar:
    time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    """xtdata 的原始成交量，单位为手（见 dates.SHARES_PER_VOLUME_UNIT）。"""
    amount: float
    """元。"""


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str | None
    open_date: str | None
    """OpenDate，8 位日期字符串。"""
    expire_date: str | None
    """ExpireDate，8 位日期字符串；在市证券可能是占位值。"""


@dataclass(frozen=True)
class RawDivid:
    time_ms: int
    interest: float
    stock_bonus: float
    stock_gift: float
    allot_num: float
    allot_price: float
    gugai: int
    dr: float


class Backend(Protocol):
    def sector(self, name: str) -> list[str]:
        """板块的当前成分（代码已是 600000.SH 格式）。"""
        ...

    def expired_a_shares(self) -> list[str]:
        """过期（已退市）的沪深 A 股。会先下载过期合约列表。"""
        ...

    def instrument(self, symbol: str) -> Instrument | None: ...

    def daily(self, symbol: str, start: str | None) -> list[RawBar]:
        """未复权日线（不补齐停牌日），按时间升序。start 为 YYYYMMDD，None 表示全部历史。"""
        ...

    def divid(self, symbol: str) -> list[RawDivid]: ...

    def trading_dates(self, market: str) -> list[int]:
        """交易日的毫秒时间戳。"""
        ...


# ---------------------------------------------------------------- 真实连接


class LiveBackend:
    """连接 MiniQMT。第一次取数时才 import xtquant 并连接。"""

    def __init__(self, config: MiniQMTConfig | None = None) -> None:
        self._config = config
        self._xt: Any = None
        self._lock = threading.Lock()

    def address(self) -> Address:
        """MiniQMT 的地址。在平台里是用户在插件页确认的地址：还没确认时抛
        ``duohelm_sdk.EndpointNotConfiguredError``，不退回到默认地址。不在平台里用本机配置。"""
        config = self._load_config()
        return endpoint(ENDPOINT_ID, default=config.address)

    def _load_config(self) -> MiniQMTConfig:
        if self._config is None:
            self._config = load_config()
        return self._config

    def connect(self) -> None:
        """连接 MiniQMT（已连接时什么都不做）。连接检查用。"""
        self._get()

    def _get(self) -> Any:
        with self._lock:
            if self._xt is None:
                # 先取地址：平台里没确认时直接报错，不 import xtquant
                address = self.address()
                missing = missing_live_dependencies()
                if missing:
                    raise MissingLiveDependencyError(
                        LIVE_INSTALL_HINT.format(missing="、".join(missing))
                    )
                config = self._load_config()
                if config.xtquant_path is not None and str(config.xtquant_path) not in sys.path:
                    sys.path.insert(0, str(config.xtquant_path))
                from xtquant import xtdata  # pyright: ignore  # noqa: PLC0415  本机安装，测试不导入

                xt = cast(Any, xtdata)
                xt.enable_hello = False
                try:
                    xt.connect(ip=address.host, port=address.port)
                    xt.get_trading_dates("SH", count=1)
                except Exception as exc:
                    where = (
                        "DuoHelm 插件页里确认的地址"
                        if in_platform()
                        else "插件目录下 miniqmt.toml 里的地址"
                    )
                    raise MiniQMTUnavailableError(
                        f"连不上 MiniQMT（{address}）：请确认 MiniQMT 已打开并登录，"
                        f"以及{where}：{exc}"
                    ) from exc
                self._xt = xt
            return self._xt

    def _download(self, symbol: str, period: str, start: str = "") -> None:
        """逐只下载，设超时。批量下载已退市合约时曾经长时间不返回。"""
        xt = self._get()
        assert self._config is not None
        done = threading.Event()
        errors: list[BaseException] = []

        def run() -> None:
            try:
                xt.download_history_data(symbol, period, start, "")
            except BaseException as exc:  # 在线程里捕获，交给调用方
                errors.append(exc)
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        if not done.wait(self._config.download_timeout_s):
            raise TimeoutError(
                f"{symbol} 下载 {period} 超过 {self._config.download_timeout_s:g} 秒"
            )
        if errors:
            raise errors[0]

    def sector(self, name: str) -> list[str]:
        return [str(c) for c in self._get().get_stock_list_in_sector(name)]

    def expired_a_shares(self) -> list[str]:
        xt = self._get()
        xt.download_history_contracts()
        return [str(c) for c in xt.get_stock_list_in_sector("过期沪深A股")]

    def instrument(self, symbol: str) -> Instrument | None:
        detail = self._get().get_instrument_detail(symbol, True)
        if not detail:
            return None
        d = cast(dict[str, Any], detail)

        def text(key: str) -> str | None:
            value = d.get(key)
            return None if value in (None, "") else str(value)

        return Instrument(symbol, text("InstrumentName"), text("OpenDate"), text("ExpireDate"))

    def daily(self, symbol: str, start: str | None) -> list[RawBar]:
        self._download(symbol, "1d", start or "")
        data = self._get().get_market_data_ex(
            BAR_FIELDS,
            [symbol],
            period="1d",
            start_time=start or "",
            dividend_type="none",
            fill_data=False,  # 不补齐停牌日：停牌日没有这一行
        )
        frame = data.get(symbol)
        if frame is None or len(frame) == 0:
            return []
        return [
            RawBar(
                time_ms=int(row["time"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
                amount=float(row["amount"]),
            )
            for _, row in frame.iterrows()
        ]

    def divid(self, symbol: str) -> list[RawDivid]:
        frame = self._get().get_divid_factors(symbol)
        if frame is None or len(frame) == 0:
            return []
        return [
            RawDivid(
                time_ms=int(row["time"]),
                interest=float(row["interest"]),
                stock_bonus=float(row["stockBonus"]),
                stock_gift=float(row["stockGift"]),
                allot_num=float(row["allotNum"]),
                allot_price=float(row["allotPrice"]),
                gugai=int(float(row["gugai"] or 0)),
                dr=float(row["dr"]),
            )
            for _, row in frame.iterrows()
        ]

    def trading_dates(self, market: str) -> list[int]:
        return [int(t) for t in self._get().get_trading_dates(market)]


# ---------------------------------------------------------------- 录制与回放


def _key(method: str, *args: object) -> str:
    return json.dumps([method, *args], ensure_ascii=False)


_TYPES: dict[str, type] = {t.__name__: t for t in (RawBar, Instrument, RawDivid)}


def _encode(value: Any) -> Any:
    if isinstance(value, list):
        return [_encode(v) for v in cast(list[Any], value)]
    if hasattr(value, "__dataclass_fields__"):
        return {"$type": type(value).__name__, **asdict(value)}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return [_decode(v) for v in cast(list[Any], value)]
    if isinstance(value, dict):
        obj = cast(dict[str, Any], value)
        if "$type" in obj:
            return _TYPES[str(obj["$type"])](**{k: v for k, v in obj.items() if k != "$type"})
        return obj
    return value


class RecordingBackend:
    """调用真实后端，同时记录结果。``save`` 写出 gzip 压缩的 JSON（只放 .local-data/）。"""

    def __init__(self, inner: Backend) -> None:
        self._inner = inner
        self.calls: dict[str, Any] = {}

    def _record(self, key: str, value: Any) -> Any:
        self.calls[key] = _encode(value)
        return value

    def sector(self, name: str) -> list[str]:
        return self._record(_key("sector", name), self._inner.sector(name))

    def expired_a_shares(self) -> list[str]:
        return self._record(_key("expired_a_shares"), self._inner.expired_a_shares())

    def instrument(self, symbol: str) -> Instrument | None:
        return self._record(_key("instrument", symbol), self._inner.instrument(symbol))

    def daily(self, symbol: str, start: str | None) -> list[RawBar]:
        return self._record(_key("daily", symbol, start), self._inner.daily(symbol, start))

    def divid(self, symbol: str) -> list[RawDivid]:
        return self._record(_key("divid", symbol), self._inner.divid(symbol))

    def trading_dates(self, market: str) -> list[int]:
        return self._record(_key("trading_dates", market), self._inner.trading_dates(market))

    def save(self, path: Path, meta: dict[str, Any]) -> None:
        body = {"meta": meta, "calls": dict(sorted(self.calls.items()))}
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(raw, mtime=0))


class NotRecordedError(KeyError):
    def __init__(self, key: str) -> None:
        super().__init__(f"录制文件里没有这次调用：{key}（需要重新运行 scripts/capture_xtdata.py）")


class ReplayBackend:
    """从录制文件回放，不访问网络，不 import xtquant。"""

    def __init__(self, path: Path) -> None:
        body = json.loads(gzip.decompress(path.read_bytes()))
        self.meta: dict[str, Any] = body["meta"]
        self._calls: dict[str, Any] = body["calls"]

    def _get(self, key: str) -> Any:
        if key not in self._calls:
            raise NotRecordedError(key)
        return _decode(self._calls[key])

    def sector(self, name: str) -> list[str]:
        return self._get(_key("sector", name))

    def expired_a_shares(self) -> list[str]:
        return self._get(_key("expired_a_shares"))

    def instrument(self, symbol: str) -> Instrument | None:
        return self._get(_key("instrument", symbol))

    def daily(self, symbol: str, start: str | None) -> list[RawBar]:
        return self._get(_key("daily", symbol, start))

    def divid(self, symbol: str) -> list[RawDivid]:
        return self._get(_key("divid", symbol))

    def trading_dates(self, market: str) -> list[int]:
        return self._get(_key("trading_dates", market))
