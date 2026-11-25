"""eltdx 行情源插件。清单里的 entrypoint 指向这里的 ``plugin``。

导入时不联网：第一次取数时才连接通达信主站。
"""

from duohelm_eltdx.source import EltdxSource

plugin = EltdxSource()

__all__ = ["EltdxSource", "plugin"]
