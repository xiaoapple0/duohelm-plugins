"""MiniQMT（迅投 xtdata）行情源插件。清单里的 entrypoint 指向这里的 ``plugin``。

导入时不连接 MiniQMT，也不 import xtquant：第一次取数时才读取本机配置并连接。
"""

from duohelm_xtdata.source import XtdataSource

plugin = XtdataSource()

__all__ = ["XtdataSource", "plugin"]
