"""LiveBackend 传给 xtdata 的参数。用假的 xtdata 对象，不 import xtquant、不连接 MiniQMT。"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from duohelm_xtdata.backend import LiveBackend
from duohelm_xtdata.config import MiniQMTConfig


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
