"""录制 eltdx 插件测试用的真实响应。**需要网络**，连接通达信主站。

用法（在仓库根目录）：

    uv run python scripts/capture_eltdx.py

按 plugins/eltdx/tests/scenario.py 的场景调用插件，把每次对 eltdx 的调用结果写进
.local-data/eltdx/eltdx-3.3.5.json.gz（被 .gitignore 忽略，只供本人使用，不要提交）。
测试只读这个文件，不访问网络。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "eltdx"), str(ROOT / "plugins" / "eltdx" / "tests")]

import eltdx  # noqa: E402
import scenario  # noqa: E402

from duohelm_eltdx.backend import LiveBackend, RecordingBackend  # noqa: E402
from duohelm_eltdx.source import EltdxSource  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]  Windows 控制台
    recorder = RecordingBackend(LiveBackend())
    scenario.run(EltdxSource(recorder))
    meta = {
        "eltdx": eltdx.__version__,
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "calls": len(recorder.calls),
    }
    recorder.save(scenario.REPLAY_FILE, meta)
    out = scenario.REPLAY_FILE
    size = out.stat().st_size
    print(f"已录制 {len(recorder.calls)} 次调用，写入 {out}（{size / 1024:.0f} KiB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
