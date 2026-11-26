# eltdx 行情源插件

通过 [eltdx](https://github.com/electkismet/eltdx) 3.3.5 读取通达信行情，实现 duohelm-sdk 0.3–0.4 的 `DataSourcePlugin`。

## 许可：仅限个人非商业研究

eltdx 的许可只允许个人学习、数据接口与格式研究及非商业用途，禁止把 eltdx 或**通过它获取的数据**用于商业活动、付费服务、生产服务、转售、营利性数据服务、**自动化交易服务**。使用本插件即表示你同意遵守 eltdx 的许可，并自行遵守访问第三方服务器的相关法律法规和服务协议。

DuoHelm 的 M7 实盘之前必须重新确认许可（见 DuoHelm 的 `docs/subsystems/data.md`）。

## 安装

插件和 DuoHelm 平台共用同一个 Python 环境。eltdx 只是本插件的依赖，不进入平台的 `pyproject.toml`：

```bash
# 在 DuoHelm 的环境里安装插件依赖
uv pip install "eltdx==3.3.5"
# 把插件目录复制或链接到 $DUOHELM_HOME/plugins/eltdx
```

## 提供的数据

| 方法 | 实现 | 说明 |
|---|---|---|
| `capabilities()` | — | 只有日线；`lists_delisted=False` |
| `securities()` | 代码表 | 只有在市的 A 股；上市日由平台根据第一根 K 线补齐 |
| `lookup_securities()` | 退市日线接口 | 用第一根和最后一根**有成交的** K 线的日期作为上市日、退市日；退市股的名称取不到。退市日是近似值，见下 |
| `bars()` | 日线；退市股用退市日线接口 | 未复权；丢弃成交量为 0 的行（停牌股当天的占位 K 线） |
| `adj_factors()` | 除权除息记录（类别 1） | 每 10 股的数据换算成每股，交给 `duohelm_sdk.adjust.factors_from_events` 计算。**不使用**服务器的复权价（线性变换，会出现负数） |
| `trading_calendar("CN")` | 上证指数、深证成指的日线 | 两市交易日的并集；北交所沿用深市 |
| `index_constituents()` | F10 指数成分 | **只有当前成分**：`in_date` 为纳入日期，`out_date` 全为 null |

### 退市日是近似值

SDK 的 `delist_date` 定义为**摘牌日之前的最后一个交易所交易日**。eltdx 拿不到摘牌日，只能给出最后一根有成交的 K 线的日期，这是 SDK 口径的下界：退市前停牌的证券会偏早。2026-11-26 与 xtdata 的摘牌日核对：

| 代码 | eltdx | SDK 口径 |
|---|---|---|
| 600087.SH 长航油运 | 2014-06-04 | 2014-06-04 |
| 601299.SH 中国北车 | 2015-05-06 | 2015-05-19 |
| 601268.SH *ST二重 | 2014-04-28 | 2015-05-20 |
| 000527.SZ 美的电器 | 2013-08-14 | 2013-09-17 |

DuoHelm 同步时优先采用能给出摘牌日的数据源（xtdata）；只有 eltdx 有的代码（如 000003.SZ）才用这里的值，并在质量报告里标为近似值。

不参与复权的除权记录类别：股本变化、送配股上市、非流通股上市、转配股上市、送认沽权证（股改）等。

## 网络

清单里声明了 eltdx 3.3.5 内置的 43 个行情主站（7709 端口）和 F10 网关 `static.tdx.com.cn:7615`。行情连接由 eltdx 的 Rust 原生代码发起，DuoHelm M0 的 Python 层网络拦截管不到它。

## 测试

测试不访问网络：

- `tests/test_logic.py`：用手工构造的数据测边界情况（分页、停牌占位行、代码转换、复权的单位换算）
- `tests/test_replay.py`：用录制的真实响应回放，跑 duohelm-sdk 的契约检查（`check_securities`、`check_adj_factors`、`schema_problems`），并核对几项已知的真实数据

录制文件放在仓库根目录的 `.local-data/eltdx/eltdx-3.3.5.json.gz`，**只供本人使用，不进仓库**（`.gitignore` 忽略 `.local-data/`）。回放测试带 `local_data` 标记：

```bash
uv run pytest -m "not local_data"   # 不需要录制文件，CI 就这样跑
uv run pytest -m local_data         # 只跑回放测试；缺少录制文件直接失败
```

**录制需要网络**：

```bash
uv run python scripts/capture_eltdx.py
```

录制的场景定义在 `tests/scenario.py`，录制脚本和回放测试共用。
