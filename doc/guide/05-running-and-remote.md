# 05 执行、重跑与远端调度

## 1. 选择器、种子展开与分片（`T` 区）

`list`、`gen`、`run`、`regress` 共用同一套选择参数：

- `--case PAT`、`--tag TAG`、`--exclude-tag TAG`、`--stage wip,ready`、`--build MODE`
- `--seed N` 或 `--seeds SPEC`（如 `"1-10"`、`"random:5"`）
- `--shard I/N`：按 `1 <= I <= N` 将排好序的 job 切分给多个机器/CI 节点并行执行（支持 `--order name|duration`）
- `--allow-empty`：选出 0 个 job 时不报 `SELECT_EMPTY`（默认退出码 2，防止 CI 因拼错 tag 误报绿灯）

> 说明：`solouvm run` 必须显式指定 `--case`、`--tag` 或 `--stage`；全量回归请用 `solouvm regress`。此外 `T-15`（随机种子历史去重）与 `L-07`（`--only`）属于 **待定** 功能。

## 2. 执行控制与快速止损

| 参数 | 作用 |
|------|------|
| `-j N / --jobs N` | 并行度（覆盖 `regress.jobs`） |
| `--force` | 突破 `limits.max_jobs` 上限（默认 2000） |
| `--retry N` | 失败后自动重试次数（受 `regress.retry_on` 状态集合控制） |
| `--wave` | 为本轮生成波形启用标记（传给后端 `wave` 选项） |
| `--max-fail N` | 累计约 N 个失败后提前终止本轮并标记 `partial: true` |
| `--budget 30m` | 回归总时间预算，超时即中止并仅判定已完成 job |
| `--rebuild` | 忽略构建键缓存，带 run salt 强制全新编译 |
| `--dry-run` | 只打印生成的 `make` 命令，不实际编译与运行 |
| `--detach` | 后台启动 `launch.sh` 并立即返回；配合 `status` / `stop` / `log` 管理 |

## 3. 脱离 `solouvm` 手动运行与并发隔离

`solouvm gen` 会在 `.solouvm/runs/<run_id>/run.mk` 生成自包含 Makefile，并在根目录保留入口 `Makefile`：

```bash
solouvm gen
make -j16 build && make -j16 regress
solouvm judge
```

- 同一项目支持通过环境变量 `SOLOUVM_RUN_ID=<id>`（禁止设为 `"latest"`）或唯一自动 ID（`YYYYMMDD-HHMMSS-<4hex>`）并行发起多轮 `regress`，各轮产物互不覆盖。
- 按 `Ctrl-C`（`SIGINT`）会向 B 侧 `launch.sh` 进程组发送信号清理子进程，判定已完成部分并将 `results.json` 标记 `partial: true`，进程返回退出码 `130`。

## 4. 上次失败重跑（`rerun`）

```bash
solouvm rerun --failed                 # 默认重跑上一轮非 pass/xfail/skip 的 job，保留原种子
solouvm rerun --status timeout         # 仅重跑指定状态的 job
solouvm rerun --note axi_scb#1         # 重跑指定 Bug/记录关联过的 job
solouvm rerun --failed --wave          # 为失败用例开启波形重跑（写入最新轮新 try 目录）
```

## 5. 跨机调度与同步（M2 Provider 插件）

在 `solouvm.conf.py` 定义团队 `providers` 并在个人 `solouvm.local.py` 选用或补充 `ssh.workdir`：

| 层 | 可选 `kind` | 说明 |
|----|-------------|------|
| `transport` | `local` / `ssh` | 控制命令传输与文件同步通道；`ssh` 需配置 `host`、`workdir`、`ssh_opts` |
| `launcher` | `make` / `lsf` | `lsf` 通过 `SUBMIT="bsub -K ..."` 提交队列作业（`stop` 时调用 `bkill`） |
| `sync` | `none` / `rsync` / `git` | 共享盘用 `none`；跨机增量同步用 `rsync` 或 `git`（要求工作区干净，否则报 `GIT_DIRTY`） |

> 注意：`ssh` + `lsf` + `git` 的真实大型站点参数与表现需按实机环境核验（[requirements.md §26](../requirements.md#26-不确定事项) 第 15–16 项）；Windows/WSL2 原生支持（`H-06`）与本地并发 SSH（`H-14`）当前属 **待定**。
