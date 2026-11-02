# 命令行参考

本文列出 solouvm 全部命令、参数与退出码。命令行输出、错误信息一律为英文（N-15）；本文的说明用中文。

> 状态：设计稿，尚无代码。

## 1. 约定

```text
solouvm [全局参数] <命令> [命令参数]
```

### 1.1 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功；对回归类命令表示全部通过（含 xfail） |
| 1 | 有测试失败：fail / timeout / killed / error / incomplete / xpass；`check` 发现代码错误 |
| 2 | 配置、环境或编译错误；hook 抛出异常；选择条件为空 |
| 130 | 被 Ctrl-C 中断 |

### 1.2 全局参数

所有命令都接受：

| 参数 | 说明 |
|------|------|
| `-c, --config PATH` | 配置文件或其所在目录；默认向上查找 `solouvm.conf.py` |
| `-v` / `-vv` | `-v` 把仿真输出同时打到终端；`-vv` 再打印 solouvm 执行的每条外部命令（Z-07） |
| `-q, --quiet` | 只输出错误与最终摘要 |
| `--no-hooks` | 跳过全部 A 端 hook（W-03） |
| `--provider NAME` | 临时指定 provider（H-01a） |
| `--version` | 打印版本 |

### 1.3 选择参数

用于 `list`、`gen`、`build`、`run`、`regress`、`notes`、`doc` 等需要挑选 case 的命令：

| 参数 | 说明 |
|------|------|
| `--case PATTERN` | 按名字选，支持通配，可重复（T-09） |
| `--tag TAG` | 只选带此 tag 的 case，可重复；多个 tag 之间为"或" |
| `--exclude-tag TAG` | 排除带此 tag 的 case，可重复，优先于 `--tag` |
| `--seed N` / `--seeds SPEC` | 覆盖种子；`SPEC` 如 `"1-5,42"` 或 `"random:10"` |
| `--build MODE` | 只选使用该编译配置的 case |
| `--shard I/N` | 稳定地把 job 分成 N 份，只取第 I 份（1 起计）（T-16） |
| `--include-wip` | 筛选时也包含 `stage="wip"` 的 case（M-18） |
| `--allow-empty` | 选择结果为空时返回 0 而不是 2（T-17） |

选择顺序：先按 `stage` 过滤，再按 `--case`、`--tag`、`--exclude-tag`、`--build`，然后展开种子成 job，最后分片。

### 1.4 执行参数

用于 `build`、`run`、`regress`、`rerun`：

| 参数 | 说明 |
|------|------|
| `-j, --jobs N` | 并行数 |
| `--detach` | 启动后立即返回（Z-06） |
| `--wave` | 所有 job 打开波形 |
| `--retry N` | 覆盖 `regress.retry` |
| `--max-fail N` | 失败数达到 N 后停止派发（X-12） |
| `--dry-run` | 只打印将要 push 的文件与在 B 上执行的命令（Z-03） |

## 2. 准备与查看

### `solouvm init`

在当前目录生成最小可跑的项目：`solouvm.conf.py`、`filelist.py`、`case.py`、`.gitignore` 条目，以及使用 `fake` 后端的示例 case（Z-01）。已有同名文件时不覆盖，报错退出。

| 参数 | 说明 |
|------|------|
| `--simulator NAME` | 示例配置使用的后端，默认 `fake` |

### `solouvm config`

打印合并后的最终配置，每个值标注来源：`default` / `conf` / `local` / `cli`（C-08）。

| 参数 | 说明 |
|------|------|
| `--json` | 以 JSON 输出 |
| `KEY` | 只看某个键，如 `solouvm config regress.jobs` |

### `solouvm files`

列出最终文件清单：分组、顺序、每个文件来自哪个条目或哪个 `.f`；项目根之外的路径单独标出（F-10）。

### `solouvm list`

列出将要执行的 job，不生成、不执行（T-11）。接受全部选择参数。

```text
JOB             CASE    SEED  BUILD    TAGS          STAGE
burst__4417     burst   4417  default  nightly,axi   wip
smoke__1        smoke   1     default                ready

2 jobs from 2 cases
```

| 参数 | 说明 |
|------|------|
| `--json` | 以 JSON 输出 |

### `solouvm doctor`

检查环境并逐项给出结论与修复建议（Z-02）：

- A：Python 版本、配置能否加载、`make` 是否可用；
- 当前 provider 的 B：ssh 连通性、远端目录可写、`make` / `bash` 存在、超时实现（`timeout` / `gtimeout` / 内置）、仿真器在 PATH 中、`setup` 片段能执行；
- fast compile：slang 是否可用及版本；
- LSF：`bsub` / `bjobs` / `bkill` 是否可用，队列是否存在。

任一项失败时退出码为 2。

## 3. 生成与执行

### `solouvm gen`

收集、生成全部产物，不 push、不执行。接受选择参数。产物位置见 [architecture.md](architecture.md) §4。

### `solouvm build`

gen → push → 在 B 上编译。默认编译所选 case 用到的全部 build mode。

| 参数 | 说明 |
|------|------|
| `--build MODE` | 只编译指定的 build mode，可重复 |

编译失败时打印编译日志中第一个错误的位置与上下文，退出码 2（D-03）。

### `solouvm run` / `solouvm regress`

gen → push → build → run → judge → report。两者流程相同，区别只在默认选择：

| 命令 | 不带选择参数时 |
|------|----------------|
| `regress` | 选择全部可回归的 case（见 config.md §5.1 的 stage 表） |
| `run` | 报错，要求至少给出 `--case` 或 `--tag`，避免误跑全量 |

默认阻塞，期间每隔几秒刷新一行进度（H-13）：

```text
[12:03:41] 37/120 done  pass 33  fail 3  error 1  running 16  queued 67  eta 41m
```

按 Ctrl-C：终止 B 上本轮全部 job，对已完成的 job 判定并输出报告，退出码 130（Q3）。

### `solouvm rerun`

重跑上一轮（或指定轮次）的部分 job，**使用相同种子**（R-02）。

| 参数 | 说明 |
|------|------|
| `--failed` | 重跑全部失败 job（默认） |
| `--status S` | 只重跑指定状态，如 `--status timeout`，可重复 |
| `--run RUN_ID` | 基于哪一轮，默认 `latest` |
| `--note CASE#ID` | 用某个 bug 记录的种子重跑该 case（M-12） |
| `--wave` | 打开波形 |

`rerun` 产生新的 run_id，结果不覆盖原轮次。

### `solouvm check`

fast compile：slang 检查 → fake_top 编译（→ RTL 增量编译，P2）。详见 [fast-compile.md](fast-compile.md)。

| 参数 | 说明 |
|------|------|
| `--slang-only` | 只跑 slang |
| `--no-faketop` | 跳过 fake_top |
| `--where local\|remote` | 覆盖 `check.where` |
| `--format text\|gcc\|json` | 诊断输出格式；`gcc` 为 `file:line:col: error: msg`，便于编辑器解析（E-10） |

发现代码错误时退出码 1；slang 或仿真器无法运行时退出码 2。

## 4. 运行中与运行后

### `solouvm status`

查看某一轮的进度与已完成 job 的初步判定（Z-04）。可以在另一个终端执行，也可以用于 `--detach` 之后。

| 参数 | 说明 |
|------|------|
| `--run RUN_ID` | 默认 `latest` |
| `--watch` | 持续刷新，直到该轮结束 |

### `solouvm log JOB_ID`

经 provider 读取某个 job 的日志（Z-05）。只读日志，不涉及波形与调试。

| 参数 | 说明 |
|------|------|
| `--run RUN_ID` | 默认 `latest` |
| `--try N` | 第几次尝试，默认最后一次 |
| `--tail N` | 只看最后 N 行 |
| `-f, --follow` | 跟随输出，job 结束后自动退出 |
| `--build MODE` | 看编译日志而不是 job 日志 |

### `solouvm stop`

终止某一轮在 B 上的全部 job（Q3）；终止后自动执行一次 `judge`。

| 参数 | 说明 |
|------|------|
| `--run RUN_ID` | 默认 `latest` |

### `solouvm judge`

读取事实文件与日志，按**当前**配置与 case 定义判定、汇总、输出报告（J-08）。可反复执行，不重跑仿真。

| 参数 | 说明 |
|------|------|
| `--run RUN_ID` | 默认 `latest` |
| `--allow-missing` | 未运行的 job 记为 skip 而不是 error（J-09） |

### `solouvm report`

基于已有的 `results.json` 生成其他格式，不重新判定。

| 参数 | 说明 |
|------|------|
| `--run RUN_ID` | 默认 `latest` |
| `--format text\|json\|junit\|html` | 可重复；默认 `text` |
| `--compare RUN_ID` | 与另一轮对比：新增失败、新修复、仍失败（K-06） |
| `-o PATH` | 输出位置 |

### `solouvm history`

| 用法 | 说明 |
|------|------|
| `solouvm history` | 最近 N 轮：时间、git commit、通过率、耗时（Y-04） |
| `solouvm history CASE` | 某个 case 在各轮的结果（Y-05） |

## 5. case 记录

### `solouvm notes`

跨所有 case 查询记录（M-06、M-07）。不带参数时列出全部未解决的 bug（`open` + `fixed`）与未完成的 todo。

```text
ID          TYPE  STATUS  OWNER    SEED  TITLE
burst#1     bug   open    hw-zhao  4417  WRAP 跨 4K 边界地址回绕错误
burst       todo  -       wp       -     补 narrow transfer 场景（due 2026-11-20）

1 open bug, 0 fixed awaiting verification, 1 todo
```

| 参数 | 说明 |
|------|------|
| `--type bug\|todo\|log\|note` | 可重复 |
| `--status S` | 可重复；`--status all` 显示全部 |
| `--owner NAME` | |
| `--since DATE` / `--until DATE` | 按日期过滤 |
| `--overdue` | 只看逾期 todo |
| `--next-id CASE` | 打印该 case 下一个可用的 bug 编号（M-05a） |
| `--json` | 以 JSON 输出 |

另外接受选择参数中的 `--case`、`--tag`。

### `solouvm doc`

导出 markdown（M-08 ~ M-10、M-21）。

| 参数 | 说明 |
|------|------|
| `-o DIR` | 输出目录，默认 `CFG["doc"]["out"]` |
| `--lang zh\|en` | 覆盖 `CFG["doc"]["lang"]` |
| `--with-results` | 附带最近一轮回归结果（默认开启；`--no-results` 关闭） |

导出内容：`index.md`（进度总览、未解决 bug 汇总、case 列表）+ 每个 case 一个 `<case>.md`。目录中由 solouvm 生成、但本次不再产生的旧文件会被删除；非 solouvm 生成的文件不动。

### `solouvm progress`

终端版进度总览（M-20）：各 stage 的 case 数、最近一轮通过率、按 owner 统计的未解决 bug 与 todo、逾期 todo、状态不一致提示（M-22）。

## 6. 其他

### `solouvm cov`

| 用法 | 说明 |
|------|------|
| `solouvm cov merge [--run RUN_ID]` | 在 B 上合并该轮通过 job 的覆盖率库（V-03） |
| `solouvm cov report [--run RUN_ID]` | 在 B 上生成报告，A 只取回汇总数字（V-04） |

### `solouvm clean`

| 参数 | 说明 |
|------|------|
| 无参数 | 删除 A 与 B 上的 `.solouvm/runs/` 中超出保留策略的轮次 |
| `--all` | 删除整个 `.solouvm/`（A 与 B），保留用户文件 |
| `--locks` | 清除残留的编译锁（见 architecture.md §12） |
| `--local-only` | 只清理 A |

## 7. 手动模式

不经 solouvm 执行时，生成物可以直接用 `make`（N-03）：

```bash
solouvm gen --tag nightly          # A
# 把项目同步到 B，在 B 的项目根：
make build
make -j16 regress                   # 或 make run/burst__4417
make WAVE=1 run/burst__4417         # 带波形
# 回到 A（结果文件需能被 A 读到，例如共享盘，或配置了 provider）：
solouvm judge
```
