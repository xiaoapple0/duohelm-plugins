"""MiniQMT（迅投 xtdata）行情源插件。清单里的 entrypoint 指向这里的 ``plugin``。

导入时不连接 MiniQMT，也不 import xtquant：第一次取数时才读取本机配置并连接。加载时检查
xtquant 运行时需要的 pandas、numpy：缺少时记一条带安装方法的警告（插件仍然加载，回放数据
可用），真正连接 MiniQMT 时抛 MissingLiveDependencyError。
"""

import logging

from duohelm_xtdata.backend import LIVE_INSTALL_HINT, missing_live_dependencies
from duohelm_xtdata.source import XtdataSource

_missing = missing_live_dependencies()
if _missing:
    logging.getLogger(__name__).warning(LIVE_INSTALL_HINT.format(missing="、".join(_missing)))

plugin = XtdataSource()

__all__ = ["XtdataSource", "plugin"]
