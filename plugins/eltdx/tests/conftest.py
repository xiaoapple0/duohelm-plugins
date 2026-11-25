from __future__ import annotations

import pytest

from duohelm_eltdx.backend import LiveBackend


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试不访问网络：任何代码试图建立真实连接都会立即失败。"""

    def refuse(self: LiveBackend) -> object:
        raise AssertionError("测试不得连接通达信主站，请使用 ReplayBackend 或 FakeBackend")

    monkeypatch.setattr(LiveBackend, "_get", refuse)
