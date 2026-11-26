# MiniQMT 行情源插件（迅投 xtdata）

通过已登录的 MiniQMT 读取 A 股行情，实现 duohelm-sdk 0.2 的 `DataSourcePlugin`。DuoHelm 的主数据源（eltdx 插件作为备用和交叉核对）。

## 行情数据只供本人使用

通过 MiniQMT 取得的行情数据只供本人研究使用，不得分发、转售或提供给他人。使用条款以券商和迅投的协议为准，DuoHelm 的 M6 模拟盘、M7 实盘之前需要确认。

## 安装和配置

1. 准备一台运行并登录着 MiniQMT 的机器（可以不是 DuoHelm 所在的机器）。
2. xtquant 用 MiniQMT 自带的或迅投官方发布的版本，**不用 PyPI 上的同名包**。xtquant 运行时需要 pandas 和 numpy，装在 DuoHelm 的环境里。
3. 复制 `miniqmt.example.toml` 为 `miniqmt.toml`，填写 MiniQMT 的地址和 xtquant 所在目录。`miniqmt.toml` 被 `.gitignore` 忽略，**MiniQMT 的实际地址不进任何仓库**。

没有 `miniqmt.toml` 时使用默认地址 `127.0.0.1:58610`，与清单里声明的网络权限一致。连不上 MiniQMT 时，插件抛出 `MiniQMTUnavailableError`，提示打开并登录 MiniQMT。

## 提供的数据

| 方法 | 实现 | 说明 |
|---|---|---|
| `capabilities()` | — | 只有日线；`lists_delisted=True` |
| `securities()` | 板块“沪深A股”“京市A股”，合约信息 | `include_delisted=True` 时加上过期合约板块“过期沪深A股”。早年退市的不全（如 PT金田A），DuoHelm 再用 eltdx 补全 |
| `lookup_securities()` | 合约信息 | 只返回 MiniQMT 有合约信息的代码 |
| `bars()` | `get_market_data_ex`（`fill_data=False`，不复权） | 停牌日没有这一行；丢弃成交量为 0 的行；成交量由手换成股；价格按 0.001 取整 |
| `adj_factors()` | `get_divid_factors` | 每股派息、送股 + 转增、配股、配股价交给 `duohelm_sdk.adjust.factors_from_events`；与 xtdata 的除权系数 `dr` 交叉核对，相差超过 0.1% 的记警告 |
| `trading_calendar("CN")` | `get_trading_dates("SH")`、`("SZ")` | 两市交易日的并集 |
| `index_constituents()` | 指数板块 | **只有当前成分**（普通版取不到历史成分），`in_date`、`out_date` 都为 null |

### 日期：一律由时间戳按 Asia/Shanghai 换算

xtdata 返回的日期字符串是按**运行机器的本机时区**格式化的，本机不在东八区时所有日期会整体错一天（美东时区下宁德时代的上市日显示为 2018-06-10）。插件只用 `time` 字段的毫秒时间戳，按 Asia/Shanghai 换算日期。

xtdata 日线的时间戳是固定 UTC+8 的零点（1986–1991 年夏令时期间按 Asia/Shanghai 换算是凌晨 1:00，日期不变）。按 Asia/Shanghai 换算对“固定 +8 零点”和“当地（含夏令时）零点”两种约定都正确，所以不改用固定 +8。Windows 上的 Python 没有自带时区数据库，所以依赖 `tzdata` 包。

本机时区是不是东八区都不影响结果：回放测试用显式的美东时区解读同一个原始时间戳（得到 2018-06-10），并断言插件输出 2018-06-11；POSIX 上整个回放模块还会把 TZ 切换到美东时区运行。

### 成交量单位

xtdata 的 `volume` 单位是手。2026-11-25 按证券类型与 eltdx（成交量为股）逐日核对：沪深股票、沪深 ETF、北交所股票的 xtdata volume × 100 等于股数；指数的“成交额 ÷ 成交量”约为 100 × 成分股平均成交价，也是手。回放测试按类型检查：成交额 ÷ 股数必须落在当天的最低价与最高价之间。

## 测试

测试不连接 MiniQMT，也不 import xtquant：

- `tests/test_logic.py`：手工构造的数据，覆盖日期换算（含非东八区时区、1991 年夏令时）、按类型的成交量单位、停牌占位行、证券列表、复权、配置文件
- `tests/test_replay.py`（`local_data` 标记）：回放本地录制的真实响应，运行 SDK 的契约检查，并在美东时区下断言已知日期

录制文件只放在本地 `.local-data/xtdata/`（被 `.gitignore` 忽略，不进仓库）。**重新录制需要连接 MiniQMT**：

```bash
uv run python scripts/capture_xtdata.py
```

录制的场景定义在 `tests/xt_scenario.py`，录制脚本和回放测试共用。
