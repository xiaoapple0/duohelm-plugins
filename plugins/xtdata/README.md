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
| `bars()` | `get_market_data_ex`（`fill_data=False`，不复权） | 停牌日没有这一行；丢弃成交量为 0 的行；丢弃开高低收有任一不是有限正数的行并记警告（xtdata 对已退市证券只给成交量、价格全为 0，退市证券因此没有日线，要靠 eltdx）；成交量由手换成股；价格按 0.001 取整 |
| `adj_factors()` | `get_divid_factors`，日线的 `preClose` | 每股派息、送股 + 转增、配股、配股价按事件公式算除权比例，参考价四舍五入到 0.01 元（ETF 0.001 元）；与 preClose 法、xtdata 的除权系数 `dr` 交叉核对，破产重整的转增等情形采用 preClose 法，见下文“除权比例的核对”；最终比例与 `dr` 相差超过 0.1% 的记警告；上市以后的除权日没有产生因子（除权前没有价格有效的收盘价，或参考价不为正）的，按证券记警告 |
| `trading_calendar("CN")` | `get_trading_dates("SH")`、`("SZ")` | 两市交易日的并集 |
| `index_constituents()` | 指数板块 | **只有当前成分**（普通版取不到历史成分），`in_date`、`out_date` 都为 null |

### 除权比例的核对（duohelm-plugins#19）

每个除权日有三个比例（≥1 表示价格下调）：

- **B**（默认）：事件公式。P 为除权日之前最近一个价格有效的收盘价，参考价 = (P − 派息 + 配股价 × 配股数) / (1 + 送转 + 配股数)，**四舍五入到 0.01 元**（交易所公布的参考价就是这样取整的），比例 = P / 参考价。不取整时，与 `dr` 的偏差中位为 0.13%，低价股更大
- **A**：preClose 法，除权日 K 线的 `上一行 close / preClose`。preClose 是交易所给的除权后前收盘价
- **C**：`get_divid_factors` 的 `dr`

A 只在“干净”时参与核对：除权日当天有 K 线、与上一行相隔不超过 2 个交易日（按交易日历；重整转增多在除权前一天停牌）、preClose 和上一收盘都是有限正数、除权日在 2002-01-01 及以后。A 恰好等于 1（preClose 等于上一收盘）时多半是 preClose 没有按除权调整，视为无效；只有 `dr` 也等于 1、而 B 与 `dr` 相差超过 1% 时例外（603363.SH 2024-12-26）。

| 情形 | 结果 | 警告标记 |
|---|---|---|
| A 与 B 相差超过 1%，B 与 C 也相差超过 1% | **采用 A**（破产重整的资本公积转增：转增股份不分给原股东，交易所调整了参考价公式；603007.SH 2024-12-26 的 `dr` 也是错的） | `除权核对[adopt_a]` |
| B ≈ C ≈ 1，A 偏离 1 超过 1% | 保留 B，请人工核对公告（转增没有记进除权数据，如 603559.SH 2024-12-26） | `除权核对[empty_event]` |
| A 与 B 相差超过 1%，B 与 C 相差不超过 0.1% | 保留 B（多为差异化分红、配股，A 不可靠） | `除权核对[a_off_bc_agree]` |
| A 与 B 相差超过 1%，B 与 C 相差 0.1%–1% 或没有 C | 保留 B | `除权核对[a_off]` |
| A 与 B 相差 0.5%–1% | 保留 B | `除权核对[a_minor]` |
| A 与 B 相差不超过 0.5%，或 A 不干净 | 保留 B | 无（只剩与 `dr` 的 0.1% 核对） |

警告里带有 A、B、C、采用的比例和间隔，便于统计。2026-11-28 对全部在市证券的实测（58,372 个除权事件），采用 A 的有 12 个，全部是 2022 年以后的重整转增。

### 查询失败抛 LookupFailedError

`lookup_securities()` 返回空（或结果里没有某个代码）只表示 **MiniQMT 确认没有这个代码的合约信息**。下面这些情况一律抛 `duohelm_sdk.LookupFailedError`（duohelm-sdk 0.4.2 起，原异常保留为 `__cause__`），不返回空，也不返回只含已拿到部分的结果：

- 连不上 MiniQMT、插件页还没确认地址（`EndpointNotConfiguredError`）、缺少依赖、接口报错或超时
- 任何一个代码查询出错（整批都不返回）
- 返回结构异常：过期沪深 A 股列表为空；代码在过期列表里却没有合约信息；已过期的合约算不出退市日（摘牌日缺失、交易日历为空）。不检查的话，退市股会被当成在市股票返回

平台收到它，把这批代码记为“未知”，不记“没找到”，这次同步失败；下次同步照常重查，退市股不会因为一次连接失败被漏掉（见 DuoHelm 的 `docs/subsystems/plugins.md`、`data.md` 的“退市股扫描”）。只有 `lookup_securities()` 这样处理；`securities()` 等其他方法出错仍抛原异常。

### 同步时的下载策略和内存约束（duohelm-plugins#20）

**先读 MiniQMT 本地已有的日线，本地缺数据的证券才下载。** 2026-11-28 在维护者的 MiniQMT 上只读实测：不调用 `download_history_data`，`get_market_data_ex` 就能读到本地已有的 1d（还有 5m、1m，本插件只用 1d），维护者的定时任务每天更新到最近交易日；对 5,618 只本地已有日线的证券各下载一遍约 403 秒（约 72 ms/只），读取一只只要几到几十毫秒。此前每次读日线都先下载，`adj_factors` 再读一遍又下载一遍。

判定规则（`localfirst.py`）：

| 情形 | 处理 |
|---|---|
| 本地有日线，最后一根不早于“应有的最近交易日” | 直接用，不下载 |
| 本地为空，或最后一根早于应有的最近交易日 | 下载这一只（逐只，设超时），再读一次 |
| 下载后最后一根仍早于它 | 接受为停牌（停牌日没有这一行）或退市，不再循环下载；同一个交易日内不再为这只证券下载 |
| 下载后仍为空 | 返回空（MiniQMT 没有这只证券的日线），记 info 日志 |
| 下载失败（接口报错、超时） | 抛 `DailyDownloadError`（原异常为 `__cause__`），**不返回本地的旧数据，也不当成没有数据**；同步失败，下次照常重试 |
| 交易日历取不到（返回空） | 无法判断，退回“先下载再读” |

“应有的最近交易日”由交易日历（`get_trading_dates("SH")`）确定，不由本地数据确定（本地数据可能本身就是旧的）：取不晚于现在（Asia/Shanghai）的最后一个交易日；交易日当天 15:30 之前还没有当天的日线，取前一个交易日。15:30 是我们的取值（A 股 15:00 收盘，留 30 分钟），未实测。代价：停牌中、已退市的证券每个交易日会下载一次，之后同一天不再下载。

`adj_factors` 复用同一套逻辑：本地数据是新的就不下载；同一轮同步里 `bars` 已经为某只证券下载过，`adj_factors` 不会再下载。

**内存约束（维护者在 MiniQMT 那台机器上确认）**：xtdata 请求数据会占用 MiniQMT 机器的内存，请求多了内存会被占满；一次请求全部在市证券的日线、分钟线时出现过 error 10054 断连，很可能就是这个原因。实现上：

- 读取和下载**每次只请求一只证券**，插件内部不合并成大请求。平台按 50 只一批调用插件，一批内也是逐只处理，不需要批间间隔（逐只读取每只几到几十毫秒、下载约 72 ms，都是只读实测）
- 去掉重复下载本身就减轻了内存压力
- 建议批大小：日线按平台的 50 只一批；**分钟线 20 只一批、批间 0.2 秒**（2026-11-28 在维护者的机器上实测稳定）。本插件目前不读分钟线，这条是以后做分钟线时的约束
- 内存占用的具体数值和不同批大小下的对比：**未实测**

没有加“同步时不下载”的配置项：默认的行为（缺数据才下载）已经保证数据不会过期，又不需要用户额外操作。

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
- `tests/test_local_first.py`：先读本地、缺数据才下载的判定规则（本地有数据不下载、为空或过期才下载、停牌不循环、下载失败抛 `DailyDownloadError`、`adj_factors` 不重复下载），用假的存储和假的 xtdata
- `tests/test_replay_local_first.py`（`local_data` 标记）：用录制数据回放“本地有数据”“本地缺数据”两条路径。录制里没有“下载前 / 下载后”的区别，缺数据的情形是**模拟**的（让某些证券下载前为空或缺最后几根）；检查输出与直接回放逐行一致
- `tests/test_replay.py`（`local_data` 标记）：回放本地录制的真实响应，运行 SDK 的契约检查，并在美东时区下断言已知日期

录制文件只放在本地 `.local-data/xtdata/`（被 `.gitignore` 忽略，不进仓库）。独立的 git worktree 里没有它时，可以设置环境变量 `DUOHELM_LOCAL_DATA=<目录>`，回放测试就从 `<目录>/xtdata/xtdata-250807.json.gz` 读（文件不存在照样失败；录制脚本不看这个变量）。**重新录制需要连接 MiniQMT**：

```bash
uv run python scripts/capture_xtdata.py
```

录制的场景定义在 `tests/xt_scenario.py`，录制脚本和回放测试共用。
