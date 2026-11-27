# eltdx 行情源插件

通过 [eltdx](https://github.com/electkismet/eltdx) 3.3.5 读取通达信行情，实现 duohelm-sdk 0.4.1 起（<0.5）的 `DataSourcePlugin`。

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
| `bars()` | 日线；退市股用退市日线接口，指数用指数日线接口 | 未复权；成交量换成**股**（单位见下）；丢弃成交量为 0 的行（停牌股当天的占位 K 线） |
| `adj_factors()` | 除权除息记录（类别 1） | 每 10 股的数据换算成每股，交给 `duohelm_sdk.adjust.factors_from_events` 计算。**不使用**服务器的复权价（线性变换，会出现负数） |
| `trading_calendar("CN")` | 上证指数、深证成指的日线 | 两市交易日的并集；北交所沿用深市 |
| `index_constituents()` | F10 指数成分 | **只有当前成分**：`in_date` 为纳入日期，`out_date` 全为 null |

### 成交量单位

`bars()` 的 `volume` 一律是**股**。eltdx 的原始成交量（`volume_wire_value`）单位随证券类型而不同，插件按证券类型换算（`duohelm_eltdx/units.py`）：

| 类型 | 判断依据（交易所 + 代码前缀，只列有依据的） | 原始单位 | 乘以 |
|---|---|---|---|
| 股票 | 沪 600/601/603/605/688/689；深 000/001/002/003/300/301/302（录制的代码表里出现过；302 是创业板） | 股 | 1 |
| 北交所股票 | 北 920（录制的代码表里北交所全是 920；899 是指数，没有核对过） | 股 | 1 |
| 指数 | 沪 000（如上证指数 000001）；深 399（如深证成指 399001） | **手** | **100** |
| ETF | 沪 510–518、560–563、588–589；深 159（依据：issue #3 的结论加交易所规定的 ETF 号段，**没有录制数据覆盖**；ETF 不在代码表里，仍走退市日线接口，那个接口能否返回在市 ETF、单位是否为股都没有核对过） | 股 | 1 |
| 未知 | 其他前缀：债券、可转债、B 股、LOF（16 开头）、分级基金（150 开头）、沪 52 开头、回购、北交所老代码等 | 没有核对过 | — |

指数的单位是手：2026-11-25 与 xtdata 交叉核对，两边的数值相同，用“成交额 ÷ 成交量”检查，结果约为 100 × A 股平均成交价（xiaoapple0/duohelm-plugins#3）。升级前返回的指数成交量比实际股数小 100 倍；个股不受影响。**之前入库的指数成交量偏小 100 倍，需要重新取**（xiaoapple0/duohelm#73）。

**类型未知的证券不返回日线**：`bars()` 抛出 `UnknownSecurityTypeError`（`ValueError` 的子类），不会默认成股，也不会默认成手，因为默认错了会悄悄差 100 倍。要支持新的类型，先与另一个数据源核对单位，再在 `units.py` 里加前缀。`adj_factors()` 只用价格，不受影响。

### 退市日是近似值

SDK 的 `delist_date` 定义为**摘牌日之前的最后一个交易所交易日**。eltdx 拿不到摘牌日，只能给出最后一根有成交的 K 线的日期，这是 SDK 口径的下界：退市前停牌的证券会偏早。2026-11-26 与 xtdata 的摘牌日核对：

| 代码 | eltdx | SDK 口径 |
|---|---|---|
| 600087.SH 长航油运 | 2014-06-04 | 2014-06-04 |
| 601299.SH 中国北车 | 2015-05-06 | 2015-05-19 |
| 601268.SH *ST二重 | 2014-04-28 | 2015-05-20 |
| 000527.SZ 美的电器 | 2013-08-14 | 2013-09-17 |

因此插件声明 `Capabilities.exact_delist_date=False`（duohelm-sdk 0.4.1 起），DuoHelm 同步时把 eltdx 给出的退市日记为近似值（数据集 `delist_date_quality` 的 `exact` 为 False）。同一证券有精确数据源（xtdata 的摘牌日）时以精确的为准、eltdx 的值只用于 xtdata 没有的证券，这个逻辑在平台的 xiaoapple0/duohelm#89 里，平台版本含该改动才生效；在那之前，后同步的数据源会覆盖退市日（见 xiaoapple0/duohelm-plugins#4）。

`lookup_securities` 算上市日、退市日时，先丢掉成交量为 0 的行（停牌占位行），所以 `delist_date` 取的是最后一根真正有成交的 K 线，与 `bars()` 的过滤一致。日线读取本身的行为没有改变。

升级到这个版本之前同步过 eltdx 的，升级后再同步一次：平台每次都会重新查询全部退市股，把它们改为近似值。

不参与复权的除权记录类别：股本变化、送配股上市、非流通股上市、转配股上市、送认沽权证（股改）等。

## 网络

清单里声明了 eltdx 3.3.5 内置的 43 个行情主站（7709 端口）和 F10 网关 `static.tdx.com.cn:7615`。行情连接由 eltdx 的 Rust 原生代码发起，DuoHelm M0 的 Python 层网络拦截管不到它。

## 测试

测试不访问网络：

- `tests/test_logic.py`：用手工构造的数据测边界情况（分页、停牌占位行、代码转换、复权的单位换算、成交量单位：个股不变、指数乘 100、类型未知拒绝）
- `tests/test_replay.py`：用录制的真实响应回放，跑 duohelm-sdk 的契约检查（`check_securities`、`check_adj_factors`、`schema_problems`），并核对几项已知的真实数据（包括成交量单位：个股的成交额 ÷ 股数落在当天最低价与最高价之间，指数的成交额 ÷ 股数在 1–100 元之间）

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
