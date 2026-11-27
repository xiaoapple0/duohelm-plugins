# AGENTS.md

给在本仓库工作的所有 coding agent 的说明。平台侧的规则见
[DuoHelm 的 AGENTS.md](https://github.com/xiaoapple0/duohelm/blob/main/AGENTS.md)，本仓库同样适用。

## 录制数据不进仓库

本仓库是公开仓库。录制的真实行情响应（例如 eltdx 回放测试用的录制文件）
**只供本人使用，不得提交、推送或上传到任何地方**。

- 录制文件放在仓库根目录的 `.local-data/` 下，这个目录被 `.gitignore` 忽略
- `.gitignore` 还忽略 `.gz`、`.zst`、`.parquet`、`.pkl`、`.feather`、`.arrow` 文件，不要用 `git add -f` 绕过
- 需要录制文件的测试标记为 `local_data`：CI 用 `-m "not local_data"` 显式排除；
  本地用 `-m local_data` 运行，缺少录制文件直接失败。不要改成跳过，也不要为了让 CI
  跑回放测试而把录制文件放进仓库

## 每次推送前必须运行的检查

每次 `git push` 之前，在仓库根目录运行下面两条命令（Git Bash / Linux / macOS），**两条的输出都必须为空**。
检查按大小和类型判断，不按文件名：

```bash
# 1. 整个 git 历史里超过 100 KB 的文件（uv.lock 除外）
git rev-list --all --objects | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' | awk '$1 == "blob" && $2 > 102400 && $3 != "uv.lock"'
# 2. 整个 git 历史里的数据文件：.gz .zst .parquet .pkl .feather .arrow
git rev-list --all --objects | git cat-file --batch-check='%(objecttype) %(objectsize) %(rest)' | awk '$1 == "blob"' | grep -iE '\.(gz|zst|parquet|pkl|feather|arrow)$'
```

只要有任何输出，就不要推送：停下来，把输出交给维护者。不要自行改写已推送的历史，
清理方式由维护者决定。

注意：`--all` 包括远程跟踪分支，`git fetch` 拉下来的旧对象也会被检查到。

## 阶段 3：自动合并（维护者 2026-11-27 决定，可随时收回）

与 [DuoHelm 的 AGENTS.md](https://github.com/xiaoapple0/duohelm/blob/main/AGENTS.md) 的“阶段 3：M0–M5 自动合并”相同，下面只写本仓库不同或需要补充的地方。

### 范围

满足主仓库阶段 3 的全部条件，并且不触及以下任何一项的 PR，可以自动合并。触及的打 `needs-maintainer` 标签，写摘要，由维护者合并：

- 规则文件：本文件（AGENTS.md）、`.github/`
- 依赖：新增或移除依赖（包括 `pyproject.toml` 的依赖和 `uv.lock` 里因此产生的变化）
- 插件清单 `plugins/*/duohelm-plugin.toml` 里的网络地址（endpoints）和权限声明：它们决定插件能连到哪里，属于安全边界
- 凭据处理、交易执行
- 平台契约（插件接口、清单格式、数据标准格式、SDK 公开 API）：本仓库只按契约实现，不改契约；需要改的，到主仓库开 issue

### 合并前必须完成

1. `gh pr view` 确认 state 为 OPEN
2. 在本地把 PR 合并到最新的 main 上，运行：`uv run ruff check .`、`uv run ruff format --check .`、`uv run pyright`、`uv run pytest -m "not local_data"`，以及上面“每次推送前必须运行的检查”的两条命令（输出必须为空）
3. CI 全部通过（被取消的运行不算通过）
4. 审查：在本仓库的 worktree 里运行主仓库的 `scripts/review-bundle.sh` 生成审查材料，reviewer 按主仓库 `.claude/agents/reviewer.md` 审查，结论是“通过”；“需修改”的，修改后最多再审一轮，仍不通过就交给维护者
5. 高风险改动（插件输出的日期与时区、成交量和价格的单位、复权因子、退市日和证券列表、指数成分）还要：
   - 对每条被保护的行为做一次破坏验证，确认测试会变红
   - 用本机 `.local-data/` 里的录制数据运行 `uv run pytest -m local_data`，作为真实数据的对照，结果写进审查记录；本机没有对应的录制数据、或回放测试没有覆盖这次改动的，交给维护者合并
   - 打 `auto-merged-high-risk` 标签，每晚回顾时优先复查

### 合并

- 使用 `gh pr merge --match-head-commit <sha>`
- 合并前在 PR 里写审查记录：审查的 commit、运行的检查、结果、破坏验证和录制数据回放的明细
- 审查记录和 PR 说明里不写录制文件的内容、本机路径和真实的连接地址

### 自动暂停

主仓库的暂停条件同样适用：主仓库有标题以“自动合并暂停”开头的打开的 issue 时，本仓库也不合并（`gh issue list -R xiaoapple0/duohelm --state open --search "自动合并暂停 in:title"`）。另外，本仓库 main 最新提交的 CI 失败或被取消时，也不合并（`gh run list --branch main --limit 1`）。
