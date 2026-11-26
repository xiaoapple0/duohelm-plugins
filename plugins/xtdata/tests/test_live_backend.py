"""LiveBackend 传给 xtdata 的参数。用假的 xtdata 对象，不 import xtquant、不连接 MiniQMT。"""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Iterator
from typing import Any

import pytest
from duohelm_sdk import Address, EndpointNotConfiguredError
from duohelm_sdk import context as sdk_context

import duohelm_xtdata
from duohelm_xtdata.backend import (
    LiveBackend,
    MiniQMTUnavailableError,
    MissingLiveDependencyError,
    missing_live_dependencies,
)
from duohelm_xtdata.config import MiniQMTConfig
from duohelm_xtdata.source import XtdataSource

_ORIGINAL_GET = LiveBackend._get  # pyright: ignore[reportPrivateUsage]  conftest 在每个测试里替换它


class _Frame:
    def __init__(self, rows: list[dict[str, float]]) -> None:
        self._rows = rows

    def __len__(self) -> int:
        return len(self._rows)

    def iterrows(self) -> list[tuple[int, dict[str, float]]]:
        return list(enumerate(self._rows))


class _FakeXt:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def download_history_data(self, *args: Any) -> None:
        self.calls.append(("download_history_data", args, {}))

    def get_market_data_ex(self, *args: Any, **kwargs: Any) -> dict[str, _Frame]:
        self.calls.append(("get_market_data_ex", args, kwargs))
        row = {"time": 1528646400000.0, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}
        return {args[1][0]: _Frame([{**row, "volume": 1.0, "amount": 100.0}])}


def test_daily_requests_unadjusted_unfilled_bars(monkeypatch: pytest.MonkeyPatch) -> None:
    """不复权、不补齐停牌日（fill_data=True 会把停牌日补成前一天的价格，造成停牌成交）。"""
    fake = _FakeXt()
    backend = LiveBackend(MiniQMTConfig(download_timeout_s=5))
    monkeypatch.setattr(LiveBackend, "_get", lambda self: fake)  # pyright: ignore[reportUnknownLambdaType]
    bars = backend.daily("300750.SZ", "20180601")
    assert len(bars) == 1 and bars[0].time_ms == 1528646400000
    (_, args, kwargs) = next(c for c in fake.calls if c[0] == "get_market_data_ex")
    assert args[1] == ["300750.SZ"]
    assert kwargs["period"] == "1d"
    assert kwargs["dividend_type"] == "none"
    assert kwargs["fill_data"] is False
    assert "xtquant" not in sys.modules


def _hide(monkeypatch: pytest.MonkeyPatch, package: str) -> None:
    """让 importlib.util.find_spec 报告 package 未安装。"""
    real = importlib.util.find_spec

    def find_spec(name: str, *args: Any) -> Any:
        return None if name == package else real(name, *args)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)


def test_missing_pandas_gives_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺少 pandas 时，连接前就报错并给出安装方法；不去 import xtquant。"""
    monkeypatch.setattr(LiveBackend, "_get", _ORIGINAL_GET)  # 撤掉 conftest 的“禁止连接”补丁
    _hide(monkeypatch, "pandas")
    assert missing_live_dependencies() == ["pandas"]
    with pytest.raises(MissingLiveDependencyError, match="uv sync --extra live"):
        LiveBackend(MiniQMTConfig()).sector("沪深A股")
    assert "xtquant" not in sys.modules


# ---- MiniQMT 的地址和连接检查 ----


@pytest.fixture
def platform() -> Iterator[None]:
    """模拟在 DuoHelm 平台里运行；结束后恢复为不在平台里。"""
    yield
    sdk_context._reset()  # pyright: ignore[reportPrivateUsage]


def _in_platform(confirmed: dict[str, Address]) -> None:
    sdk_context._install(confirmed, unconfirmed=["miniqmt"])  # pyright: ignore[reportPrivateUsage]


def test_address_outside_platform_from_config() -> None:
    backend = LiveBackend(MiniQMTConfig(host="10.0.0.5", port=58611))
    assert backend.address() == Address("10.0.0.5", 58611)


@pytest.mark.usefixtures("platform")
def test_address_in_platform_ignores_config() -> None:
    """在平台里用插件页确认的地址，miniqmt.toml 里的地址不用。"""
    _in_platform({"miniqmt": Address("192.168.31.5", 58610)})
    backend = LiveBackend(MiniQMTConfig(host="10.0.0.5", port=58611))
    assert backend.address() == Address("192.168.31.5", 58610)


@pytest.mark.usefixtures("platform")
def test_unconfirmed_endpoint_does_not_fall_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在平台里、地址没确认：不退回到默认地址或 miniqmt.toml，不 import xtquant。"""
    monkeypatch.setattr(LiveBackend, "_get", _ORIGINAL_GET)  # 撤掉 conftest 的“禁止连接”补丁
    _in_platform({})
    backend = LiveBackend(MiniQMTConfig())
    with pytest.raises(EndpointNotConfiguredError, match="插件页确认"):
        backend.address()
    with pytest.raises(EndpointNotConfiguredError, match="插件页确认"):
        backend.sector("沪深A股")
    status = XtdataSource(backend).check_connection()
    assert status.ok is False and "插件页确认" in status.message
    assert "xtquant" not in sys.modules


def test_check_connection_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(self: LiveBackend) -> object:
        raise MiniQMTUnavailableError(
            "连不上 MiniQMT（127.0.0.1:58610）：请确认 MiniQMT 已打开并登录"
        )

    monkeypatch.setattr(LiveBackend, "_get", unavailable)
    status = XtdataSource(LiveBackend(MiniQMTConfig())).check_connection()
    assert status.ok is False and "已打开并登录" in status.message


def test_check_connection_reports_missing_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LiveBackend, "_get", _ORIGINAL_GET)
    _hide(monkeypatch, "pandas")
    status = XtdataSource(LiveBackend(MiniQMTConfig())).check_connection()
    assert status.ok is False and "uv sync --extra live" in status.message
    assert "xtquant" not in sys.modules


def test_check_connection_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LiveBackend, "_get", lambda self: object())  # pyright: ignore[reportUnknownLambdaType]
    status = XtdataSource(LiveBackend(MiniQMTConfig())).check_connection()
    assert status.ok is True and "127.0.0.1:58610" in status.message


def test_plugin_load_warns_when_pandas_missing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """插件加载时缺少 numpy：记一条带安装方法的警告，插件照样加载（回放数据可用）。"""
    _hide(monkeypatch, "numpy")
    with caplog.at_level("WARNING", logger="duohelm_xtdata"):
        importlib.reload(duohelm_xtdata)
    assert "numpy" in caplog.text and "uv sync --extra live" in caplog.text
    assert duohelm_xtdata.plugin is not None
