"""录制和回放共用的场景：录制脚本按这里的调用访问 MiniQMT，测试按同样的调用回放。

改动这里的参数后，必须重新运行 scripts/capture_xtdata.py 录制。
录制文件只供本人使用，放在仓库根目录被 .gitignore 忽略的 .local-data/ 下，不进仓库。
（模块名不叫 scenario，避免与 eltdx 插件测试的同名模块冲突。）
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

from duohelm_sdk import Freq

from duohelm_xtdata.source import XtdataSource

LOCAL_DATA_ENV = "DUOHELM_LOCAL_DATA"
REPLAY_NAME = Path("xtdata") / "xtdata-250807.json.gz"

# 录制脚本写到这里：仓库内被忽略的 .local-data/，不受环境变量影响
REPLAY_FILE = Path(__file__).resolve().parents[3] / ".local-data" / REPLAY_NAME


def replay_file() -> Path:
    """回放测试读的录制文件。设置了环境变量 DUOHELM_LOCAL_DATA 时从 <它>/xtdata/ 读，
    没设置时用仓库根目录的 .local-data/（让独立的 git worktree 也能跑回放测试）。"""
    root = os.environ.get(LOCAL_DATA_ENV)
    return Path(root) / REPLAY_NAME if root else REPLAY_FILE


CATL = "300750.SZ"  # 宁德时代：2018-06-11 上市（星期一）。美东时区下日期字符串显示为 2018-06-10
CATL_RANGE = (date(2018, 6, 1), date(2018, 6, 30))
LISTED = "000001.SZ"  # 平安银行
LISTED_RANGE = (date(2024, 6, 3), date(2024, 7, 31))
DELISTED = "600087.SH"  # 长航油运，2014 年退市
DELISTED_RANGE = (date(2014, 5, 5), date(2014, 6, 30))
LOOKUP = ["600087.SH", "000003.SZ", "300750.SZ", "609999.SH"]  # 退市、xtdata 缺失、在市、不存在
CALENDAR_RANGE = (date(2024, 1, 1), date(2024, 12, 31))
ADJ_SYMBOLS = ["000001.SZ", "600519.SH"]
ADJ_RANGE = (date(1990, 1, 1), date(2026, 12, 31))
INDEX = "000300.SH"
ZERO_PRICE = ["000004.SZ", "000005.SZ", "000016.SZ"]
"""已退市、xtdata 只给成交量、价格全为 0 的证券（2026-11-28 实测）。000016.SZ 1996-07-17、
000004.SZ 1993-05-24、000005.SZ 1994-09-23 有配股：价格为 0 时旧代码算出的因子为 0。"""
ZERO_PRICE_RANGE = (date(1990, 12, 19), date(2026, 12, 31))
EX_CHECK = [
    "603363.SH",  # 2024-12-26 重整转增，间隔 1，preClose = 上一收盘，dr = 1：采用 A（= 1）
    "603007.SH",  # 2024-12-26 重整转增，间隔 2，dr 也错：采用 A
    "300093.SZ",  # 2025-11-20 重整转增，间隔 2，A ≈ dr：采用 A
    "000615.SZ",  # 2025-12-29 同上
    "603559.SH",  # 2024-12-26 除权数据为空的重整转增：B = C = 1，只警告
    "600375.SH",  # 2024-12-26 同上
    "002310.SZ",  # 2024-12-30 间隔 2，preClose 没有按除权调整（A = 1），B ≈ dr：保留 B
    "000678.SZ",  # 2006-07-13 停牌 28 个交易日后除权，A 不干净：保留 B
]
"""除权比例核对的样本（duohelm-plugins#19，2026-11-28 实测）。"""

UNIT_SAMPLES = {
    "stock": ["000001.SZ", "600000.SH"],
    "etf": ["510300.SH", "159915.SZ"],
    "bj": ["920000.BJ"],
    "index": ["000001.SH", "399001.SZ"],
}
"""按证券类型核对成交量单位用的样本。"""
UNIT_RANGE = (date(2026, 9, 1), date(2026, 11, 20))


def run(source: XtdataSource) -> None:
    """依次调用插件的每个方法。录制时访问 MiniQMT，测试时从录制文件回放。"""
    source.securities(include_delisted=True)
    source.lookup_securities(LOOKUP)
    source.trading_calendar("CN", *CALENDAR_RANGE)
    source.bars([CATL], Freq.DAY, *CATL_RANGE)
    source.bars([LISTED], Freq.DAY, *LISTED_RANGE)
    source.bars([DELISTED], Freq.DAY, *DELISTED_RANGE)
    for symbols in UNIT_SAMPLES.values():
        source.bars(symbols, Freq.DAY, *UNIT_RANGE)
    source.adj_factors(ADJ_SYMBOLS, *ADJ_RANGE)
    source.bars(ZERO_PRICE, Freq.DAY, *ZERO_PRICE_RANGE)
    source.adj_factors(ZERO_PRICE, *ZERO_PRICE_RANGE)
    source.adj_factors(EX_CHECK, *ADJ_RANGE)
    source.index_constituents(INDEX, *ADJ_RANGE)
