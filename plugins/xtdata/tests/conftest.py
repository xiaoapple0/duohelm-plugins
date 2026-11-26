from __future__ import annotations

import pytest

from duohelm_xtdata.backend import LiveBackend


@pytest.fixture(autouse=True)
def _no_miniqmt(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试不连接 MiniQMT、不 import xtquant：任何代码试图建立真实连接都会立即失败。"""

    def refuse(self: LiveBackend) -> object:
        raise AssertionError("测试不得连接 MiniQMT，请使用 ReplayBackend 或手工构造的后端")

    monkeypatch.setattr(LiveBackend, "_get", refuse)
