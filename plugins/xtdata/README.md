# MiniQMT 行情源插件（迅投 xtdata）

通过已登录的 MiniQMT 读取 A 股行情，实现 duohelm-sdk 0.4.2 起（<0.5）的 `DataSourcePlugin` 和可选的 `check_connection`。DuoHelm 的主数据源（eltdx 插件作为备用和交叉核对）。

## 行情数据只供本人使用

通过 MiniQMT 取得的行情数据只供本人研究使用，不得分发、转售或提供给他人。使用条款以券商和迅投的协议为准，DuoHelm 的 M6 模拟盘、M7 实盘之前需要确认。

## 安装和配置

1. 准备一台运行并登录着 MiniQMT 的机器（可以不是 DuoHelm 所在的机器）。
2. xtquant 用 MiniQMT 自带的或迅投官方发布的版本，**不用 PyPI 上的同名包**。xtquant 运行时需要 pandas 和 numpy（可选依赖组 `live`：`uv sync --extra live`），装在 DuoHelm 的环境里。缺少时插件加载会记一条带安装方法的警告，连接 MiniQMT 时抛 `MissingLiveDependencyError`；只用回放数据（测试）不需要它们。
3. 复制 `miniqmt.example.toml` 为 `miniqmt.toml`，填写 xtquant 所在目录。`miniqmt.toml` 被 `.gitignore` 忽略，**MiniQMT 的实际地址不进任何仓库**。
4. 在 DuoHelm 的插件页确认 MiniQMT 的地址（清单里的 endpoint `miniqmt`，scope 为 `lan`：本机或局域网的 IP 加端口）。页面预先填好默认的 `127.0.0.1:58610`，MiniQMT 在本机时一键确认即可。

### MiniQMT 的地址从哪里来

| 运行方式 | 地址 |
|---|---|
| 在 DuoHelm 平台里 | 用户在插件页确认的地址（`duohelm_sdk.endpoint("miniqmt")`），`miniqmt.toml` 里的 host、port 不用。**还没确认时插件拿不到地址，不会退回到默认地址**，取数抛 `EndpointNotConfiguredError` |
| 不在平台里（录制脚本、独立调试） | `miniqmt.toml` 里的 host、port；没有配置文件时用 `127.0.0.1:58610` |

`check_connection()`（平台在同步任务开始前调用）：地址没确认、缺少 pandas / numpy、找不到 xtquant、`miniqmt.toml` 有误、连不上 MiniQMT 时返回 `ok=False` 和原因，例如“请在 DuoHelm 插件页确认后再试”“请确认 MiniQMT 已打开并登录”。取数时连不上 MiniQMT 抛 `MiniQMTUnavailableError`。

xtquant 是原生扩展，DuoHelm 的 Python 层网络拦截管不到它的连接（DuoHelm #7）。插件只按 `endpoint()` 取地址，平台在用户确认之前不交出地址，所以确认之前插件不会去连 MiniQMT。

## 提供的数据

| 方法 | 实现 | 说明 |
|---|---|---|
| `capabilities()` | — | 只有日线；`lists_delisted=True` |
| `securities()` | 板块“沪深A股”“京市A股”，合约信息 | `include_delisted=True` 时加上过期合约板块“过期沪深A股”。早年退市的不全（如 PT金田A），DuoHelm 再用 eltdx 补全 |
| `lookup_securities()` | 合约信息 | 只返回 MiniQMT 有合约信息的代码；查询失败抛 `LookupFailedError`，见下 |
| `bars()` | `get_market_data_ex`（`fill_data=False`，不复权） | 停牌日没有这一行；丢弃成交量为 0 的行；成交量由手换成股；价格按 0.001 取整 |
| `adj_factors()` | `get_divid_factors` | 每股派息、送股 + 转增、配股、配股价交给 `duohelm_sdk.adjust.factors_from_events`；与 xtdata 的除权系数 `dr` 交叉核对，相差超过 0.1% 的记警告 |
| `trading_calendar("CN")` | `get_trading_dates("SH")`、`("SZ")` | 两市交易日的并集 |
| `index_constituents()` | 指数板块 | **只有当前成分**（普通版取不到历史成分），`in_date`、`out_date` 都为 null |

### 查询失败抛 LookupFailedError

`lookup_securities()` 返回空（或结果里没有某个代码）只表示 **MiniQMT 确认没有这个代码的合约信息**。下面这些情况一律抛 `duohelm_sdk.LookupFailedError`（duohelm-sdk 0.4.2 起，原异常保留为 `__cause__`），不返回空，也不返回只含已拿到部分的结果：

- 连不上 MiniQMT、插件页还没确认地址（`EndpointNotConfiguredError`）、缺少依赖、接口报错或超时
- 任何一个代码查询出错（整批都不返回）
- 返回结构异常：过期沪深 A 股列表为空；代码在过期列表里却没有合约信息；已过期的合约算不出退市日（摘牌日缺失、交易日历为空）。不检查的话，退市股会被当成在市股票返回

平台收到它，把这批代码记为“未知”，不记“没找到”，这次同步失败；下次同步照常重查，退市股不会因为一次连接失败被漏掉（见 DuoHelm 的 `docs/subsystems/plugins.md`、`data.md` 的“退市股扫描”）。只有 `lookup_securities()` 这样处理；`securities()` 等其他方法出错仍抛原异常。

### 日期：一律由时间戳按 Asia/Shanghai 换算

xtdata 返回的日期字符串是按**运行机器的本机时区**格式化的，本机不在东八区时所有日期会整体错一天（美东时区下宁德时代的上市日显示为 2018-06-10）。插件只用 `time` 字段的毫秒时间戳，按 Asia/Shanghai 换算日期。

xtdata 日线的时间戳是固定 UTC+8 的零点（1986–1991 年夏令时期间按 Asia/Shanghai 换算是凌晨 1:00，日期不变）。按 Asia/Shanghai 换算对“固定 +8 零点”和“当地（含夏令时）零点”两种约定都正确，所以不改用固定 +8。Windows 上的 Python 没有自带时区数据库，所以依赖 `tzdata` 包。

本机时区是不是东八区都不影响结果：回放测试用显式的美东时区解读同一个原始时间戳（得到 2018-06-10），并断言插件输出 2018-06-11；POSIX 上整个回放模块还会把 TZ 切换到美东时区运行。

### 成交量单位

xtdata 的 `volume` 单位是手。2026-11-25 按证券类型与 eltdx（成交量为股）逐日核对：沪深股票、沪深 ETF、北交所股票的 xtdata volume × 100 等于股数；指数的“成交额 ÷ 成交量”约为 100 × 成分股平均成交价，也是手。回放测试按类型检查：成交额 ÷ 股数必须落在当天的最低价与最高价之间。

## 测试

测试不连接 MiniQMT，也不 import xtquant：

- `tests/test_logic.py`：手工构造的数据，覆盖日期换算（含非东八区时区、1991 年夏令时）、按类型的成交量单位、停牌占位行、证券列表、复权、配置文件、清单
- `tests/test_live_backend.py`：用假的 xtdata 对象检查传给 xtdata 的参数；MiniQMT 地址的来源（平台里确认的地址、没确认时不退回默认地址）和 `check_connection`
- `tests/test_replay.py`（`local_data` 标记）：回放本地录制的真实响应，运行 SDK 的契约检查，并在美东时区下断言已知日期

录制文件只放在本地 `.local-data/xtdata/`（被 `.gitignore` 忽略，不进仓库）。**重新录制需要连接 MiniQMT**：

```bash
uv run python scripts/capture_xtdata.py
```

录制的场景定义在 `tests/xt_scenario.py`，录制脚本和回放测试共用。
