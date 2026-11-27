"""回放测试的录制文件路径解析（不需要录制文件，CI 会跑）。"""

from __future__ import annotations

from pathlib import Path

import pytest
import xt_scenario as scenario


def test_default_is_repo_local_data(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(scenario.LOCAL_DATA_ENV, raising=False)
    repo_root = Path(__file__).resolve().parents[3]
    assert scenario.replay_file() == repo_root / ".local-data" / "xtdata" / "xtdata-250807.json.gz"
    assert scenario.replay_file() == scenario.REPLAY_FILE


def test_env_var_overrides_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(scenario.LOCAL_DATA_ENV, str(tmp_path))
    assert scenario.replay_file() == tmp_path / "xtdata" / "xtdata-250807.json.gz"
    # 录制脚本用的 REPLAY_FILE 不受环境变量影响
    assert scenario.replay_file() != scenario.REPLAY_FILE


def test_empty_env_var_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(scenario.LOCAL_DATA_ENV, "")
    assert scenario.replay_file() == scenario.REPLAY_FILE
