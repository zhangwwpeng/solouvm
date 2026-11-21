# 命令行参考

本文列出 solouvm 全部命令、参数与退出码。人和 AI 使用同一套命令（N-16）：默认输出给人看，`--json` 给程序与 AI。命令行输出、错误信息一律为英文（N-15）；本文的说明用中文。

> 状态：设计稿，尚无代码。命令标题中的（M2）等表示所属里程碑，未标注的属于 M1；里程碑划分见 requirements.md §25。

## 1. 约定

```text
solouvm [全局参数] <命令> [命令参数]
```

### 1.1 退出码

| 码 | 含义 |
|----|------|
| 0 | 成功；对回归类命令表示全部通过（含 xfail） |
| 1 | 有不算通过的结果：fail / timeout / killed / error / incomplete / xpass / false_pass；`check` 发现代码错误；`lint-style` 发现违规；`triage status` 有未审查的任务 |
| 2 | 配置、环境或编译错误；选择条件为空；角色不允许；hook 抛出异常 |
| 130 | 被 Ctrl-C 中断 |

### 1.2 全局参数

所有命令都接受：

| 参数 | 说明 |
|------|------|
| `-c, --config PATH` | 配置文件或其所在目录；默认向上查找 `solouvm.conf.py` |
| `-v` / `-vv` | `-v` 把仿真输出同时打到终端；`-vv` 再打印 solouvm 执行的每条外部命令（Z-07） |
| `-q, --quiet` | 只输出错误与最终摘要 |
| `--no-hooks` | 跳过全部 A 端 hook（`pre_hook_<阶段>.py` / `post_hook_<阶段>.py`，W-03） |
| `--provider NAME` | 临时指定 provider（H-01a） |
| `--json` | 以 JSON 输出，结构见 §1.4（N-17） |
| `--limit N` | 列表与日志的输出上限，`0` 表示不截断（N-19） |
| `--role main\|triage` | 运行角色，默认 `main`；`triage` 下只允许只读命令与记录写入（AI-10） |
| `--version` | 打印版本 |

### 1.3 选择参数

用于 `list`、`gen`、`build`、`run`、`regress`、`notes`、`doc` 等需要挑选 case 的命令：

| 参数 | 说明 |
|------|------|
| `--case PATTERN` | 按名字选，支持通配，可重复（T-09） |
| `--tag TAG` | 只选带此 tag 的 case，可重复；多个 tag 之间为"或" |
| `--exclude-tag TAG` | 排除带此 tag 的 case，可重复，优先于 `--tag` |
| `--stage LIST` | 按开发阶段选，如 `ready,signoff`；默认 `wip,ready,signoff`（T-19、M-18） |
| `--seed N` / `--seeds SPEC` | 覆盖种子；`SPEC` 如 `"1-5,42"` 或 `"random:10"` |
| `--build MODE` | 只选使用该编译配置的 case |
| `--shard I/N` | 稳定地把 job 分成 N 份，只取第 I 份（1 起计）（T-16，M2） |
| `--shard-seed KEY` | 分片展开 `random:N` 时的共享随机序列标识（未传时回退 `SOLOUVM_SHARD_SEED` 环境变量或 `run_id`） |
| `--allow-empty` | 选择结果为空时返回 0 而不是 2（T-17） |

选择顺序：先按 `--stage` 过滤，再按 `--case`、`--tag`、`--exclude-tag`、`--build`，然后展开种子成 job，最后分片。`--case` 选中 `planned` 的 case 时报错，选中 `deprecated` 的 case 时照跑并警告（config.md §5.1）。

### 1.4 机器可读输出

`--json` 时，标准输出只有一个 JSON 对象（流式命令为 JSON Lines，每行一个事件）：

```json
{"schema": 1, "command": "list", "ok": true, "data": {...}, "truncated": null, "next": ["solouvm gen --tag nightly"]}
```

出错时：

```json
{"schema": 1, "command": "gen", "ok": false,
 "error": {"code": "CFG_UNKNOWN_KEY", "message": "unknown key 'regres' in CFG",
           "file": "solouvm.conf.py", "line": 12, "hint": "did you mean 'regress'?"}}
```

- `error.code` 是稳定的错误码，完整列表见 [formats.md](formats.md) §9（N-18）；
- `truncated`：输出被截断时给出省略数量与继续查看的命令（N-19），例如 `{"omitted": 1840, "more": "solouvm log burst__4417 --lines 200-400"}`；
- `next`：建议的下一步命令，人类输出模式下以 `next:` 行打印。

默认的人类输出同样遵守输出上限：列表超过 50 行、日志超过 200 行时截断并提示，用全局参数 `--limit` 调整。

标准输出不是终端时（被 AI、管道、CI 读取），不输出颜色与控制字符；进度不做原地刷新，改为阶段跳转行、简洁状态行与每完成一个 job 打印一行（N-22）。在交互终端（TTY）下运行 `gen` / `build` / `run` / `regress` / `rerun` / `judge` 时，默认按阶段在底部状态栏上方展开固定高度的可折叠实时视口（滚动展示 hook 中的 `ctx.log()`/`print()` 与最新编译/仿真日志），阶段成功后自动收起并列对齐为单行 `✓ [i/5] stage  ...`，若阶段失败则保留尾部日志便于排查；传 `-v` 可全程保留展开的完整日志（非终端加 `-v` 亦可流式输出完整纯文本日志）。如需在收起后仍永久保留某条重要信息，可显式调用 `ctx.echo(...)` 或 `solouvm.echo(...)`（`--json` 与 `--quiet` 下自动静默）。

`--json` 模式下，`build` / `run` / `regress` / `rerun` 会按执行进度先输出 `stage_start`、`stage_done`、`hook_start`、`hook_done`、`build_cached`、`build_start`、`build_done`、`progress` 与 `job_done` 等 JSON Lines 事件，最后输出总信封对象。

### 1.5 角色与权限

`--role triage` 下允许的命令（AI-10）：

| 类别 | 命令 |
|------|------|
| 只读 | `uvm diff`、`lint-style`、`list`、`files`、`config`、`validate`、`status`、`log`、`explain`、`wave`、`notes`、`history`、`progress`、`schema`、`help`、`completion`、`pkg list`、`triage packet`、`triage status` |
| 记录写入 | `bug add`、`bug update`（只能改自己登记且尚未审查的 bug）、`note add`、`todo add`、`triage write` |

其余命令（`gen`、`build`、`regress`、`rerun`、`stop`、`clean`、`triage plan`、`triage review`、`todo done` 等）一律拒绝，错误码 `ROLE_DENIED`，退出码 2。

### 1.6 执行参数

用于 `build`、`run`、`regress`、`rerun`：

| 参数 | 说明 |
|------|------|
| `-j, --jobs N` | 并行数 |
| `--force` | 允许 job 数超过 `limits.max_jobs`（防止误起大量仿真） |
| `--detach` | 启动后立即返回（Z-06） |
| `--wave` | 所有 job 打开波形 |
| `--retry N` | 覆盖 `regress.retry` |
| `--max-fail N` | 失败数达到 N 后停止派发（X-12） |
| `--dry-run` | 只打印将要 push 的文件与在 B 上执行的命令（Z-03） |

## 2. 准备与查看

### `solouvm init`

在当前目录生成起步项目（Z-01、U-03）：`solouvm.conf.py`、`filelist.py`、`tbspec.py`、`case.py`、系统文档骨架、`.gitignore` 条目，以及按 uvm-style.md 生成的示例 env、agent、test。默认使用 `fake` 后端，生成后直接 `solouvm regress` 即可跑通。已有同名文件时不覆盖，报错退出。

| 参数 | 说明 |
|------|------|
| `--simulator NAME` | 示例配置使用的后端，默认 `fake` |
| `--agents FRAMEWORK` | 同时生成子 agent 定义模板，如 `claude`（AI-12） |
| `--bare` | 只生成配置文件，不生成示例环境 |

### `solouvm config`（M2）

打印合并后的最终配置，每个值标注来源：`default` / `conf` / `local` / `cli`（C-08）。

| 参数 | 说明 |
|------|------|
| `KEY` | 只看某个键，如 `solouvm config regress.jobs` |

### `solouvm files`

列出最终文件清单：分组、顺序、每个文件来自哪个条目或哪个 `.f`；项目根之外的路径单独标出（F-10）。

### `solouvm validate`

只加载与校验配置、文件清单、case、记录，不生成任何文件（Z-10）。问题按 `文件:行` 报告：

```text
case.py:42: error: Case 'burst' is stage 'ready' but has no 'expect' (AI-02)
case.py:57: warning: Case 'neg_len0' uses expect_fail=True; name the expected error (J-20)
cases/axi_wrap.py:18: error: Bug id 3 duplicated in case 'axi_wrap'
solouvm.conf.py:12: error: unknown key 'regres' in CFG; did you mean 'regress'?

3 errors, 1 warning
```

有错误时退出码 2。改完 case 后先跑它，是主 agent 的标准动作。

### `solouvm schema [NAME]`（M2）

打印 JSON Schema：`config`、`case`、`records`、`plan`、`results`、`facts`、`packet`、`output`（Z-11）。不带参数时列出可用名称。

### `solouvm list`

列出将要执行的 job，不生成、不执行（T-11）。接受全部选择参数。

```text
JOB             CASE    SEED  BUILD    TAGS          STAGE
burst__4417     burst   4417  default  nightly,axi   wip
smoke__1        smoke   1     default                ready

2 jobs from 2 cases
```

### `solouvm doctor`（M2）

检查环境并逐项给出结论与修复建议（Z-02）：

- A：Python 版本、配置能否加载、`make` 是否可用；
- 当前 provider 的 B：ssh 连通性、远端目录可写、`make` / `bash` 存在、超时实现（`timeout` / `gtimeout` / 内置）、仿真器在 PATH 中、`setup` 片段能执行；
- fast compile：slang 是否可用及版本；
- LSF：`bsub` / `bjobs` / `bkill` 是否可用，队列是否存在。

任一项失败时退出码为 2。

## 3. UVM 环境

### `solouvm new KIND NAME`

按模板生成组件（U-01），位置见 uvm-style.md §2。`KIND`：

| KIND | 生成 | 必需参数 |
|------|------|----------|
| `agent` | interface、package、txn、cfg、drv、mon、sqr、cov、agent、base_seq | 若 `tbspec.py` 中已有同名 agent，按其描述生成；否则生成带占位信号的骨架 |
| `env` | package、env_cfg、scb、vsqr、env、base_vseq，以及 test package 与 base_test | `--agents a,b` 或使用 `tbspec.py` 中的描述 |
| `test` | `<env>_<name>_test.sv`，并在 `case.py`（或 `cases/`）中登记一个 `stage="wip"` 的 case，带默认的 `Expect(scoreboard="<ENV>_SCB", min=1)`（U-09） | `--env E` |
| `seq` | `<agent>_<name>_seq.sv` | `--agent A` |
| `vseq` | `<env>_<name>_vseq.sv` | `--env E` |

已存在的文件不覆盖，报错退出（`UVMGEN_EXISTS`）；需要按新模板更新已有文件时用 `uvm sync`。新文件会登记到所属 package 的 `<pkg>_files.svh`（U-11），`filelist.py` 不用改（U-07）。

### `solouvm uvm sync`（M2）

按 `tbspec.py` 与当前模板重新生成全部组件，保留用户代码区（U-04、U-05）。

| 参数 | 说明 |
|------|------|
| `--env E` / `--agent A` | 只处理指定组件 |
| `--dry-run` | 等同 `uvm diff` |

输出：更新的文件、新生成的文件、`.orphan` 文件、区域外被手工修改的文件、`tbspec.py` 中已不存在的多余文件。

### `solouvm uvm diff`（M3）

预览 `uvm sync` 的改动，以统一 diff 格式输出（U-10）。

### `solouvm lint-style`（M3）

按 uvm-style.md 检查目录、命名、消息 ID 与禁止事项（U-08），问题按 `文件:行` 报告。

## 4. 生成与执行

### `solouvm gen`

收集、生成全部产物，不 push、不执行。接受选择参数。产物位置见 [architecture.md](architecture.md) §4。

### `solouvm build`

gen → push → 在 B 上编译。默认编译所选 case 用到的全部 build mode。

| 参数 | 说明 |
|------|------|
| `--build MODE` | 只编译指定的 build mode，可重复 |
| `--rebuild` | 忽略已有的成功标记，强制重新编译（产物写入新目录） |

编译键相同且已编译成功时直接复用并提示 `[build/<mode>] cached (<key>)`（D-06）。交互终端默认在下方状态栏上方实时滚动输出 `build.log`（单模式原样输出，多模式带 `[build/<mode>]` 前缀）；非终端模式下加 `-v` 亦可实时流式输出，`--json` 输出 `build_cached`/`build_start`/`build_done` 事件。编译失败时打印编译日志中第一个错误的位置与上下文，退出码 2（D-03）。

### `solouvm run` / `solouvm regress`

gen → push → build → run → judge → report。两者流程相同，区别只在默认选择：

| 命令 | 不带选择参数时 |
|------|----------------|
| `regress` | 选择全部可回归的 case（见 config.md §5.1 的 stage 表） |
| `run` | 报错，要求至少给出 `--case` 或 `--tag`，避免误跑全量 |

默认阻塞，执行 `gen` → `push` → `build` → `run` → `judge` 五个核心阶段，并在各阶段切换时使用 `[1/5] Generate` → `[2/5] Sync` → `[3/5] Compile` → `[4/5] Simulate` → `[5/5] Judge` 列对齐展示（对应内部阶段名 `gen`/`push`/`build`/`run`/`judge` 用于 hook 脚本 `pre_hook_<阶段>.py` / `post_hook_<阶段>.py` 与 `--json` 事件）；交互终端默认在每个阶段进行时于固定高度的 `▾ [i/5] <Stage>` 视口内实时滚动 hook 输出与编译/仿真日志，成功后自动收起为列对齐的单行 `✓ [i/5] <Stage>`（失败保留尾部日志，传 `-v` 保持展开），结尾以单张统一表格汇总运行与全部 job 结果，底部状态栏稳定单行显示当前阶段进度与耗时；输出不是终端时改为阶段行与简洁状态行（加 `-v` 可实时输出完整编译与仿真日志），`--json` 时输出 `stage_*`、`hook_*`、`build_*`、`progress` 与 `job_done` 等 JSON Lines 事件（H-13）：

```text
[12:03:41] 37/120 done  pass 33  fail 3  error 1  running 16  queued 67  eta 41m
```

按 Ctrl-C：终止 B 上本轮全部 job，对已完成的 job 判定并输出报告，退出码 130（Q3）。某个编译模式编译失败时，依赖它的 job 不执行、记为 `error`，其他模式的 job 照常执行，最终退出码 2（D-07）。

### `solouvm rerun`

重跑上一轮（或指定轮次）的部分 job，**使用相同种子**（R-02）。

| 参数 | 说明 |
|------|------|
| `--failed` | 重跑全部失败 job（默认） |
| `--status S` | 只重跑指定状态，如 `--status timeout`，可重复 |
| `--run RUN_ID` | 基于哪一轮，默认 `latest` |
| `--note CASE#ID` | 用某个 bug 记录的种子重跑该 case（M-12，M2） |
| `--wave` | 打开波形 |

`rerun` 产生新的 run_id，结果不覆盖原轮次。

### `solouvm check`（M3）

fast compile：slang 检查 → fake_top 编译（→ RTL 增量编译，P2）。详见 [fast-compile.md](fast-compile.md)。

| 参数 | 说明 |
|------|------|
| `--slang-only` | 只跑 slang |
| `--no-faketop` | 跳过 fake_top |
| `--where local\|remote` | 覆盖 `check.where` |
| `--format text\|gcc\|json` | 诊断输出格式；`gcc` 为 `file:line:col: error: msg`，便于编辑器解析（E-10） |

发现代码错误时退出码 1；slang 或仿真器无法运行时退出码 2。

## 5. 运行中与运行后

### `solouvm status`（M2）

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
| `--lines A-B` | 只看第 A 到 B 行（AI-14） |
| `--around N [--context C]` | 第 N 行前后各 C 行，默认 C=30 |
| `--grep REGEX` | 只看匹配的行，带行号 |
| `-f, --follow` | 跟随输出，job 结束后自动退出 |
| `--build MODE` | 看编译日志而不是 job 日志 |

### `solouvm explain JOB_ID`

一次给出排查一个结果所需的信息（AI-04）：

1. 状态、判定原因、类别；
2. 首条错误及前后 30 行日志；
3. 首条错误与 UVM 消息中 `file(line)` 指向的源码片段（AI-15）；
4. `expect` 逐条检查结果与 J-19 弱信号；
5. 失败签名，以及历史上同签名的失败；
6. 关联的 bug 与上次分诊结论；
7. 这个 case 上次通过的轮次，以及之后的 git 提交；
8. 复现命令。

| 参数 | 说明 |
|------|------|
| `--run RUN_ID` | 默认 `latest`，输出中写明实际 run_id |
| `--try N` | 默认最后一次尝试 |

### `solouvm wave JOB_ID`（M4）

以文本表格返回波形中指定信号在一段时间内的值（AI-13）。只读查询，不打开 GUI。

```text
solouvm wave burst__4417 --signals tb_top.dut.araddr,tb_top.dut.arvalid --from 1200ns --to 1500ns
```

| 参数 | 说明 |
|------|------|
| `--signals LIST` | 逗号分隔的层次路径，支持 `*` 通配 |
| `--from T` / `--to T` | 时间窗口 |
| `--changes` | 只输出值变化的时刻（默认） |
| `--limit N` | 最多输出多少行，默认 200 |

在 B 上调用后端定义的导出工具（如 Verdi 的 `fsdbreport`）。不确定：具体用哪个工具、命令怎么写，待调研（AI-13）。没有波形时提示用 `rerun --wave` 生成。

### `solouvm stop`（M2）

终止某一轮在 B 上的全部 job（Q3）；终止后自动执行一次 `judge`。

| 参数 | 说明 |
|------|------|
| `--run RUN_ID` | 默认为唯一一个仍在进行的轮次；有多个进行中的轮次时必须指定。输出中写明实际终止的 run_id |

### `solouvm judge`

读取事实文件与日志，按**当前**配置与 case 定义判定、汇总、输出报告（J-08）。可反复执行，不重跑仿真。

| 参数 | 说明 |
|------|------|
| `--run RUN_ID` | 默认 `latest` |
| `--allow-missing` | 未运行的 job 记为 skip 而不是 error（J-09） |
| `--partial` | 回归仍在进行时，只判已完成的 job，结果标为部分结果（J-23，M2）；不加时报 `RUN_ACTIVE` |

### `solouvm report`（M4）

基于已有的 `results.json` 生成其他格式，不重新判定。

| 参数 | 说明 |
|------|------|
| `--run RUN_ID` | 默认 `latest` |
| `--format text\|json\|junit\|html` | 可重复；默认 `text` |
| `--compare RUN_ID` | 与另一轮对比：新增失败、新修复、仍失败（K-06）；默认按包含已接受分诊结论的最终 `status` 比较 |
| `--compare-mode status\|judged` | 指定 `--compare` 使用最终状态 `status`（默认）还是机械判定 `judged_status` |
| `-o PATH` | 输出位置 |

### `solouvm history`（M2）

| 用法 | 说明 |
|------|------|
| `solouvm history` | 最近 N 轮：时间、git commit、通过率、耗时（Y-04） |
| `solouvm history CASE` | 某个 case 在各轮的结果（Y-05） |

## 6. case 记录

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

### `solouvm bug add CASE`

登记 bug，写回 case 源文件（M-14）。

```text
solouvm bug add burst --title "WRAP 跨 4K 边界地址回绕错误" --job burst__4417 \
    --owner hw-zhao --body - <<'EOF'
第二拍 addr_q 回绕成 0，见 log 812 行。
EOF
```

| 参数 | 说明 |
|------|------|
| `--title` | 必填 |
| `--seed` / `--run` / `--job` | 复现信息；给 `--job` 时自动取其种子与失败签名 |
| `--owner` / `--link` | |
| `--body TEXT` | 正文；`--body -` 从标准输入读（AI-19） |
| `--author NAME` | 写入者标识，例如子 agent 的编号；不写时为角色名 |
| `--id N` | 不写时取下一个可用编号（在记录锁内分配，M-14） |

输出新 bug 的完整 ID，例如 `burst#3`。`found` 取当天日期，`status` 为 `open`。

### `solouvm bug update CASE#ID`

| 参数 | 说明 |
|------|------|
| `--status S` | 按 config.md §5.2 的状态约束，自动补 `fixed` / `verified` 日期 |
| `--owner` / `--link` / `--append-body TEXT` | `--append-body -` 从标准输入读 |

triage 角色只能修改自己登记、且尚未被审查的 bug。

### `solouvm note add CASE` / `solouvm todo add CASE` / `solouvm todo done CASE#ID`

`note add` 追加调试记录（`Log`，默认）或一般备注（`--kind note`）；`todo add` 追加 todo；`todo done` 标记完成（仅限 main 角色）。命名上与查看日志的 `solouvm log JOB_ID` 区分开。

写入流程统一：获取记录锁 → 定位 `notes=[...]` → 插入 → 重新加载并校验 → 失败则恢复原文件并报错。文件在读与写之间被改动（例如编辑器同时保存）时放弃写入（`RECORD_CONFLICT`）。正文参数都支持 `-` 从标准输入读。

### `solouvm triage plan`

按 `CFG["triage"]` 为某一轮生成分诊任务（AI-05）。只能由 main 角色执行。

```text
TASK                         KIND   JOBS  CASE        SIGNATURE / REASON
20261103-101500-a1b2/1       fail   3     burst       3fa9c2e1 UVM_ERROR ... [AXI_SCB] mismatch: expect <HEX> ...
20261103-101500-a1b2/2       pass   1     axi_wrap    stage=wip
20261103-101500-a1b2/3       pass   1     smoke       weak signal: sim time 0ns

3 tasks
next: solouvm triage packet 20261103-101500-a1b2/1
```

| 参数 | 说明 |
|------|------|
| `--run RUN_ID` | 默认 `latest` |
| `--pass-policy none\|changed\|all` | 覆盖配置 |

重复执行时不重复创建已存在的任务。

### `solouvm triage packet TASK`

输出任务的上下文包（markdown），子 agent 的起点（AI-06）。内容见 [ai-workflow.md](ai-workflow.md) §5.2。

| 参数 | 说明 |
|------|------|
| `--limit N` | 覆盖 `triage.packet_limit` |

### `solouvm triage write TASK`

写入分诊结论（AI-07、AI-16）。

```text
solouvm --role triage triage write 20261103-101500-a1b2/1 \
    --verdict design_bug --confidence high \
    --summary "WRAP 地址计算未对 4K 边界取模，addr_q 第二拍回绕成 0" \
    --evidence "log:812" --evidence "rtl/axi_slv.sv:120" --evidence "wave:1234ns tb_top.dut.addr_q" \
    --bug burst#3 --doc - < triage.md
```

| 参数 | 说明 |
|------|------|
| `--verdict` | `real_pass` / `false_pass` / `design_bug` / `tb_bug` / `spec_unclear` / `env_issue` / `unknown` |
| `--confidence` | `high` / `medium` / `low` |
| `--summary` | 不超过 200 字 |
| `--evidence` | 至少一条；格式 `log:行号`、`文件:行号`、`wave:时间 信号` |
| `--bug` | 关联的 bug，可重复 |
| `--doc FILE` | 调试文档；`--doc -` 从标准输入读（AI-19）。校验必需章节后写到 `docs/debug/<run_id>/<序号>.md` |
| `--author NAME` | 写入者标识，不写时为角色名 |

约束：结论必须与任务类型相容——pass 任务只能是 `real_pass` / `false_pass`；失败任务不能是 `real_pass`（AI-08）。同一任务重复写入时，覆盖未审查的上一次结论。任务涉及多个 case 时，Triage 记录写到每个 case（AI-07）。

子 agent 没有写文件的工具，可以用 heredoc 把正文直接接在命令后面：

```text
solouvm --role triage triage write <task> --verdict ... --doc - <<'EOF'
## 现象
...
EOF
```

### `solouvm triage review TASK`

主 agent 审查（AI-09）。

| 参数 | 说明 |
|------|------|
| `--accept` | 接受结论，按 ai-workflow.md §7 更新最终状态 |
| `--reject REASON` | 驳回，理由必填；子 agent 登记的 bug 自动改为 `invalid` |

### `solouvm triage status`

列出某轮全部分诊任务、结论、置信度、审查状态，未审查与已过期的排在前面（AI-11、AI-20）。存在未审查任务时退出码 1，便于脚本判断"本轮是否已全部审完"。

### `solouvm doc`（M4）

导出 markdown（M-08 ~ M-10、M-21）。

| 参数 | 说明 |
|------|------|
| `-o DIR` | 输出目录，默认 `CFG["doc"]["out"]` |
| `--lang zh\|en` | 覆盖 `CFG["doc"]["lang"]` |
| `--with-results` | 附带最近一轮回归结果（默认开启；`--no-results` 关闭） |

导出内容：`index.md`（进度总览、未解决 bug 汇总、case 列表）+ 每个 case 一个 `<case>.md`。目录中由 solouvm 生成、但本次不再产生的旧文件会被删除；非 solouvm 生成的文件不动。

### `solouvm progress`（M4）

终端版进度总览（M-20）：各 stage 的 case 数、最近一轮通过率、按 owner 统计的未解决 bug 与 todo、逾期 todo、状态不一致提示（M-22）。

## 7. 其他

### `solouvm cov`（M4）

| 用法 | 说明 |
|------|------|
| `solouvm cov merge [--run RUN_ID]` | 在 B 上合并该轮通过 job 的覆盖率库（V-03） |
| `solouvm cov report [--run RUN_ID]` | 在 B 上生成报告，A 只取回汇总数字（V-04） |

### `solouvm bisect CASE`（待定）

在两个 git 提交之间二分，找出引入失败的提交（AI-17）。

| 参数 | 说明 |
|------|------|
| `--seed N` | 用于判断的种子 |
| `--good REV` / `--bad REV` | 默认为该 case 上次通过的提交与当前提交 |

### `solouvm clean`

| 参数 | 说明 |
|------|------|
| 无参数 | 删除 A 与 B 上超出保留策略的轮次，以及不再被任何保留轮次引用的编译目录 |
| `--run RUN_ID` | 只删除指定轮次；破坏性操作不接受 `latest` |
| `--all` | 删除整个 `.solouvm/`（A 与 B），保留用户文件 |
| `--locks` | 清除残留的编译锁（D23） |
| `--local-only` | 只清理 A |

### `solouvm help TOPIC`（M4）

输出内置的离线文档（Z-12），内容与 `doc/` 一致，例如 `solouvm help verdict`。不带参数时列出主题。

### `solouvm update`

联网从 Git 仓库更新当前 `solouvm` CLI 安装（调用仓库根目录的 `install.sh`；离线场景请直接获取新版目录后执行 `./install.sh`）。受 `--role triage` 禁止。

| 参数 | 说明 |
|------|------|
| `--repo URL` | Git 仓库地址（默认：`https://github.com/zhangwwpeng/solouvm`） |
| `--ref REF` | 指定分支、标签或提交号 |
| `--user` / `--system` | 指定更新为用户级（`~/.local`）或系统级（`/usr/local`）；不传时优先沿用 `.install-meta` 记录 |
| `--prefix DIR` | 安装或更新到自定义前缀目录 |
| `--python PATH` | 指定目标 Python >= 3.10 解释器路径 |
| `--dry-run` | 只打印计划执行的 git 与安装命令，不实际下载或修改安装 |

### `solouvm completion SHELL`

输出静态命令行补全脚本（支持 `bash` 或 `zsh`），从 CLI 解析器定义直接生成、零外部运行时依赖。只读命令，允许在 `--role triage` 下执行。

- 执行 `./install.sh`（以及 `solouvm update`）时，会自动生成并安装到 `<prefix>/share/zsh/site-functions/_solouvm` 与 `<prefix>/share/bash-completion/completions/solouvm`（`--uninstall` 时同步清理）。
- 也可在当前会话中手动临时启用：

```bash
source <(solouvm completion zsh)        # zsh
eval "$(solouvm completion bash)"       # bash
```

### `solouvm pkg list | sync | update`

管理 `CFG["packages"]` 中声明的 Git/本地包：

| 子命令 | 说明 | `--role triage` |
|--------|------|------------------|
| `solouvm pkg list` | 只读检查各包状态、优先级、依赖、`filelist.py` 与本地 commit（不触发 clone） | 允许 |
| `solouvm pkg sync [NAME...]` | 克隆缺失的 git 包（已有目录直接跳过），并校验 `PRIORITY` 与 `DEPENDS` 顺序 | 禁止 |
| `solouvm pkg update [NAME...]` | 重新克隆并原子替换指定或全部 git 包（跳过本地 `path` 包） | 禁止 |

## 8. 手动模式

不经 solouvm 执行时，生成物可以直接用 `make`（N-03）。手动入口 `.solouvm/Makefile` 指向最近一次 `gen` 的轮次（`latest`）：

```bash
solouvm gen --tag nightly                     # A
# 把项目同步到 B，在 B 的项目根：
make build
make -j16 regress                             # 或 make run/burst__4417
make TRY=r1 WAVE=1 run/burst__4417            # 带波形复现，写到新目录，不覆盖原始结果
make -f .solouvm/runs/<run_id>/run.mk TRY=r2 run/burst__4417   # 复现指定轮次
# 回到 A（结果文件需能被 A 读到，例如共享盘，或配置了 provider）：
solouvm judge
```

编译失败时 `make build` 返回非 0，`make build && make regress` 会在编译失败处停下（D-07）。
