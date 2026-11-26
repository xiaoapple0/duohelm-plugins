"""录制 xtdata 插件测试用的真实响应。**需要连接 MiniQMT**（地址见插件目录下的 miniqmt.toml）。

用法（在仓库根目录）：

    uv run python scripts/capture_xtdata.py

按 plugins/xtdata/tests/xt_scenario.py 的场景调用插件，把每次对 xtdata 的调用结果写进
.local-data/xtdata/xtdata-250807.json.gz（被 .gitignore 忽略，只供本人使用，不要提交）。
测试只读这个文件，不连接 MiniQMT、不 import xtquant。
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "xtdata"), str(ROOT / "plugins" / "xtdata" / "tests")]

import xt_scenario  # noqa: E402

from duohelm_xtdata.backend import LiveBackend, RecordingBackend  # noqa: E402
from duohelm_xtdata.source import XtdataSource  # noqa: E402


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]  Windows 控制台
    recorder = RecordingBackend(LiveBackend())
    xt_scenario.run(XtdataSource(recorder))
    meta = {
        "xtquant": "250807",
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "calls": len(recorder.calls),
    }
    recorder.save(xt_scenario.REPLAY_FILE, meta)
    out = xt_scenario.REPLAY_FILE
    print(f"已录制 {len(recorder.calls)} 次调用，写入 {out}（{out.stat().st_size / 1024:.0f} KiB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
