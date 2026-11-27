"""录制和回放共用的场景：录制脚本按这里的调用访问真实主站，测试按同样的调用回放。

改动这里的参数后，必须重新运行 scripts/capture_eltdx.py 录制。
录制文件只供本人使用，放在仓库根目录被 .gitignore 忽略的 .local-data/ 下，不进仓库。
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from duohelm_sdk import Freq

from duohelm_eltdx.source import EltdxSource

LOCAL_DATA_ENV = "DUOHELM_LOCAL_DATA"
REPLAY_NAME = Path("eltdx") / "eltdx-3.3.5.json.gz"

# 录制脚本写到这里：仓库内被忽略的 .local-data/，不受环境变量影响
REPLAY_FILE = Path(__file__).resolve().parents[3] / ".local-data" / REPLAY_NAME


def replay_file() -> Path:
    """回放测试读的录制文件。设置了环境变量 DUOHELM_LOCAL_DATA 时从 <它>/eltdx/ 读，
    没设置时用仓库根目录的 .local-data/（让独立的 git worktree 也能跑回放测试）。"""
    root = os.environ.get(LOCAL_DATA_ENV)
    return Path(root) / REPLAY_NAME if root else REPLAY_FILE


LISTED = "000001.SZ"  # 平安银行
LISTED_RANGE = (date(2024, 6, 3), date(2024, 7, 31))
DELISTED = "600087.SH"  # 长航油运，2014 年退市
DELISTED_RANGE = (date(2014, 5, 5), date(2014, 6, 30))
LOOKUP = ["600087.SH", "609999.SH", "688981.SH"]  # 退市、不存在、在市
CALENDAR_RANGE = (date(2024, 1, 1), date(2024, 12, 31))
ADJ_SYMBOLS = ["000001.SZ", "600087.SH"]
ADJ_RANGE = (date(1990, 1, 1), date(2026, 12, 31))
INDEX = "000300.SH"


def run(source: EltdxSource) -> None:
    """依次调用插件的每个方法。录制时访问真实主站，测试时从录制文件回放。"""
    source.securities()
    source.bars([LISTED], Freq.DAY, *LISTED_RANGE)
    source.bars([DELISTED], Freq.DAY, *DELISTED_RANGE)
    source.lookup_securities(LOOKUP)
    source.trading_calendar("CN", *CALENDAR_RANGE)
    source.adj_factors(ADJ_SYMBOLS, *ADJ_RANGE)
    source.index_constituents(INDEX, *ADJ_RANGE)
