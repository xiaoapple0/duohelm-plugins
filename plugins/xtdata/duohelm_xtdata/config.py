"""本机配置：MiniQMT 的连接地址和 xtquant 的位置。

配置文件是插件目录下的 ``miniqmt.toml``，**不提交**（被 .gitignore 忽略）；仓库里只有
``miniqmt.example.toml``。MiniQMT 的实际地址不进任何仓库。没有配置文件时用默认值
127.0.0.1:58610，与清单里声明的网络权限一致。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

PLUGIN_DIR = Path(__file__).resolve().parents[1]
CONFIG_FILE = PLUGIN_DIR / "miniqmt.toml"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 58610


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class MiniQMTConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    xtquant_path: Path | None = None
    """xtquant 所在目录的上一级（目录里有 xtquant/ 子目录）。MiniQMT 自带的或迅投官方发布的
    版本，不用 PyPI 上的同名包。为 None 时按普通 import 查找。"""
    download_timeout_s: float = 60.0
    """单只证券下载的超时。批量下载已退市合约时曾经长时间不返回，所以逐只下载并设超时。"""

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


def load_config(path: Path = CONFIG_FILE) -> MiniQMTConfig:
    if not path.exists():
        return MiniQMTConfig()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path.name} 格式不对：{exc}") from exc
    raw_section = raw.get("miniqmt", {})
    if not isinstance(raw_section, dict):
        raise ConfigError(f"{path.name} 里 [miniqmt] 应为表")
    section = cast(dict[str, Any], raw_section)
    host = str(section.get("host", DEFAULT_HOST))
    port = section.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or not 0 < port < 65536:
        raise ConfigError(f"{path.name} 里的 port 应为 1–65535 的整数")
    xt_raw = section.get("xtquant_path")
    xt_path = Path(str(xt_raw)) if xt_raw else None
    timeout = float(section.get("download_timeout_s", 60.0))
    return MiniQMTConfig(host=host, port=port, xtquant_path=xt_path, download_timeout_s=timeout)
