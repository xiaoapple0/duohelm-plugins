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
