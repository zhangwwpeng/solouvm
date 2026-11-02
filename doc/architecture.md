# 架构

本文说明 solouvm 由哪些部分组成、数据怎么流动、各部分之间的接口和文件格式。"做什么"以 [requirements.md](requirements.md) 为准，本文只回答"怎么组织"；括号里的编号指向对应需求。

> 状态：设计稿，尚无代码。

## 1. 总览

solouvm 分成两个运行时，中间只通过文件和 shell 命令交互：

```text
 A：控制机 —— solouvm（Python）                     B：执行机 —— make + bash
┌──────────────────────────────────────┐          ┌──────────────────────────────────┐
│ load ─ collect ─ plan ─ gen          │          │                                  │
│                          │           │  push    │ .solouvm/gen/   生成物           │
│                          └──────────────────────▶ 源码                             │
│                                      │  run     │                                  │
│ launch ──────────────────────────────────────────▶ launch.sh ─ make ─ run_job.sh    │
│                                      │          │                    │             │
│                                      │  read    │ runs/<run_id>/<job>/try1/       │
│ judge ◀───────────────────────────────────────── │   job.facts  sim.log  波形       │
│   │                                  │          │                                  │
│ report ─ history                     │  stop    │                                  │
│                                      │ ────────▶ 终止本轮全部 job                   │
└──────────────────────────────────────┘          └──────────────────────────────────┘
```

两条铁律贯穿全文：

1. **B 上只有 `make`、`bash`、POSIX 工具和仿真器**（N-04）。凡是需要在 B 上发生的事，都必须由 A 事先生成成 Makefile 或 shell 脚本。
2. **B 只记录事实，A 负责判定**（N-10）。B 写下的 `job.facts` 里没有 pass / fail。

A 与 B 是同一台机器时（`local` provider），这套结构不变，只是 push 变成空操作、read 变成读本地文件。

## 2. 流水线

一次完整的 `regress` 依次经过下列阶段。每个阶段的输入输出都是确定的，可以单独测试。

| 阶段 | 位置 | 输入 | 输出 | hook |
|------|------|------|------|------|
| load | A | `solouvm.conf.py`、`solouvm.local.py`、命令行 | `Config` 对象 | `main` 包住全程 |
| collect | A | `filelist.py`、`case.py`、`case_dirs` | 文件清单（分组）、`Case` 列表（含记录） | — |
| plan | A | 配置、case、选择条件、种子 | `Plan`：本轮全部 job | — |
| gen | A | `Plan` | `gen/` 下的共享生成物，`runs/<run_id>/` 下的本轮生成物 | `gen` |
| push | A→B | 源码、生成物 | B 上的同一份目录树 | `push` |
| build | B | 文件清单 | `build/<mode>/` 下的仿真可执行文件、构建事实 | `build` |
| run | B | 本轮 Makefile | 每个 job 的 `sim.log`、`job.facts` | `run` |
| judge | A | `job.facts`、日志流 | `Result` 列表 | `judge` |
| report | A | `Result` 列表、case 记录 | 终端摘要、`results.json`、JUnit 等 | 归入 `judge` |
| history | A | 本轮全部产物 | `runs/<run_id>/` 归档、`latest` 链接 | — |

retry（R-01）不是新阶段，而是 judge 之后对失败 job 再走一遍 gen（子集）→ run → judge。

各命令经过的阶段：

| 命令 | load | collect | plan | gen | push | build | run | judge | report |
|------|:----:|:-------:|:----:|:---:|:----:|:-----:|:---:|:-----:|:------:|
| `list` | ✓ | ✓ | ✓ | | | | | | |
| `gen` | ✓ | ✓ | ✓ | ✓ | | | | | |
| `build` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | | | |
| `run` / `regress` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `rerun` | ✓ | ✓ | 上一轮失败子集 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `judge` | ✓ | ✓ | 读已有 plan | | | | | ✓ | ✓ |
| `notes` / `doc` / `progress` | ✓ | ✓ | | | | | | | 读历史 |
| `check` | ✓ | ✓ | | 生成 fake_top | 视 provider | slang / tb 编译 | | | |

`judge` 也要 collect：判定参数（checks、waiver）和 case 记录（报告里的"已知问题"）都来自当前的 case 定义，这正是"改了规则不用重跑"（J-08）的来源。

## 3. 模块划分

Python 包结构如下，括号内为主要承担的需求区。

```text
solouvm/
  __init__.py          公开 API：Case、Bug、Todo、Log、Note，以及 Z-08 的函数入口
  cli.py               参数解析、命令分发、退出码；不含业务逻辑            (L)
  errors.py            异常层级与退出码映射                                  (N-07, N-09)
  model.py             纯数据：Case、记录类型、Job、Plan、Facts、Result      (T, M)
  config/
    load.py            执行配置文件、合并默认值与本地覆盖、展开 ${VAR}       (C)
    schema.py          CFG 的键、类型、默认值；未知键给出最接近的建议        (C-04)
  collect/
    files.py           FILES 分组、glob 展开、缺失检查                       (F)
    dotf.py            解析已有 .f：-f/-F、+incdir+、+define+、-v、-y         (F-06, F-07)
    cases.py           case.py 与 case_dirs 的收集、记录校验                 (T-01, M-05, M-15)
  plan.py              job 展开、种子、选择、分片、retry 子集                (T, R)
  gen/
    shell.py           shell 与 Makefile 转义；所有生成物都经过这里          (N-12)
    filelist.py        filelist.<组>.f 与 filelist.f                         (F-04)
    makefile.py        build.mk、run.mk、入口 Makefile                       (G)
    scripts.py         run_job.sh、launch.sh 模板                            (X)
  backends/
    base.py            后端接口：编译/运行/种子/波形/覆盖率命令、错误模式    (S)
    vcs.py xrun.py questa.py iverilog.py verilator.py fake.py
  providers/
    base.py            Transport、Launcher、Sync 三个接口                    (H)
    transport_local.py transport_ssh.py
    launcher_make.py   launcher_lsf.py
    sync_none.py       sync_rsync.py  sync_git.py
  judge/
    facts.py           解析 job.facts                                        (X-04, Q1)
    scan.py            单遍日志扫描器                                        (J-12)
    rules.py           固定优先级判定                                        (J-01 ~ J-07)
    waivers.py         waiver 匹配与记录                                     (J-14)
    signature.py       错误签名归一化                                        (J-11)
  report/
    text.py json.py junit.py html.py                                         (K)
  notes/
    query.py           跨 case 查询记录                                      (M-06, M-07)
    export.py          markdown 导出                                         (M-08 ~ M-10)
    progress.py        进度总览                                              (M-20)
  check/
    slang.py faketop.py incremental.py                                       (E)
  history.py           runs 目录、env 信息、保留策略                         (Y)
  hooks.py             A 端 hook 的加载与执行                                (W)
```

依赖方向（违反即视为设计缺陷）：

```text
cli ─▶ 各业务模块 ─▶ model / errors
                 ╲
                  ▶ gen.shell（凡是产出 shell 文本的模块）

providers ✗─▶ judge      provider 只搬运字节，不理解内容
judge     ✗─▶ providers  judge 只吃一个"按行迭代"的输入，不关心来自本地还是远端
backends  ✗─▶ providers  后端只产出命令模板与错误模式
```

## 4. 目录布局

A 和 B 上的目录树形状相同，路径都相对项目根（N-05）。差别只是谁写哪些文件。

```text
<项目根>/
  solouvm.conf.py  solouvm.local.py  filelist.py  case.py  cases/
  Makefile                                  仅当原本不存在时生成：include .solouvm/Makefile
  .solouvm/                                 整体 gitignore
    Makefile                                稳定入口：include gen/build.mk 与 runs/latest/run.mk
    gen/                                    与具体某轮无关的生成物                  [A 写，push 到 B]
      filelist.rtl.f  filelist.tb.f  filelist.top.f  filelist.f
      build.mk                              build/<mode> 目标
      bin/run_job.sh  bin/launch.sh
      fake_top.sv                           仅 check 使用
    build/<mode>/                           编译产物、build.log、job.facts           [B 写]
    runs/
      <run_id>/
        run.mk                              本轮 job 目标                           [A 写，push 到 B]
        plan.json                           本轮计划                                [A 写，只留在 A]
        env.json                            环境信息（Y-02）                        [A 写]
        results.json  results.txt  junit.xml                                       [A 写]
        launch.pid                          本轮 make 的进程组 ID                   [B 写]
        <job_id>/try<N>/                    第 N 次尝试的运行目录                   [B 写]
          sim.log  job.facts  波形  覆盖率库 ...
      latest -> <run_id>
    solouvm.log                             solouvm 自身的调试日志（Z-07）          [A 写]
```

要点：

- **共享生成物与本轮生成物分开**：`gen/` 只依赖文件清单和后端配置；`run.mk` 只依赖本轮选中的 job。两轮回归同时进行时，各自的 `run.mk` 不会互相覆盖（N-14）。
- **每次尝试一个目录**：首轮是 `try1`，retry 依次是 `try2`、`try3`。路径统一，判定和报告不需要特殊处理首轮。
- **`plan.json` 不上 B**：B 不需要它，Makefile 里已经包含执行所需的一切。
- **并发编译**：两轮回归用同一个 build mode 时，`build/<mode>` 用 `mkdir` 锁串行化（`mkdir` 在 Linux 和 macOS 上都是原子操作，不依赖 `flock`）。不同分支应使用不同的项目副本，不在同一棵源码树上并发。

## 5. 生成物

### 5.1 Makefile

三层 include，保证"手动 `make`"和"solouvm 托管"走同一份规则（N-03）。

```make
# .solouvm/Makefile —— 稳定入口
include .solouvm/gen/build.mk
-include .solouvm/runs/latest/run.mk
```

```make
# .solouvm/gen/build.mk（示意）
RUN_JOB := bash .solouvm/gen/bin/run_job.sh
SUBMIT  ?=                           # launcher 注入，例如 bsub -K -q normal -J ...

build: build/default
build/default:
	$(SUBMIT) $(RUN_JOB) --dir .solouvm/build/default --lock --timeout 7200 \
	    -- vcs -full64 -sverilog -f .solouvm/gen/filelist.f -o .solouvm/build/default/simv
```

```make
# .solouvm/runs/<run_id>/run.mk（示意）
RUN_ID := 20261103-101500-a1b2
JOBS   := run/burst__4417 run/smoke__1

regress: $(JOBS)

run/burst__4417: build/default
	$(SUBMIT) $(RUN_JOB) --dir .solouvm/runs/$(RUN_ID)/burst__4417/try1 \
	    --run-id $(RUN_ID) --job-id burst__4417 --timeout 1800 --hang 600 \
	    --env-fail '(Failed to obtain license|No such license)' \
	    -- ../../../../build/default/simv +UVM_TESTNAME=burst_test +ntb_random_seed=4417 \
	       +SOLOUVM_SEED=4417 +SOLOUVM_JOB_ID=burst__4417 +NUM=32
```

规则：

- 所有参数在 A 上经 `gen/shell.py` 转义后写入：先做 shell 转义，再把 `$` 写成 `$$`（N-12）。
- `SUBMIT` 变量是 launcher 的唯一注入点（见 §6.2）。直接 `make` 时它为空，命令就在本机执行。
- 波形开关（X-11）：`WAVE ?= 0`，后端在 `WAVE=1` 时追加波形参数；retry 带波形时 A 以 `make WAVE=1 ...` 调用。
- 并行度由 `make -jN` 控制（X-08）。

### 5.2 run_job.sh

每次编译和每个 job 都经过它，因此编译与仿真的事实格式完全相同（D-04）。

```text
run_job.sh --dir DIR [--run-id ID] [--job-id ID] [--timeout SEC] [--hang SEC]
           [--env-fail REGEX] [--lock] [--setup FILE] -- COMMAND...
```

执行步骤：

1. 创建 `DIR`；`--lock` 时用 `mkdir DIR.lock` 获取锁，退出时释放（残留锁的处理见 decisions.md D23）。
2. 写入 `job.facts` 的开始段（`state=running`）。
3. 进入 `DIR`，执行 provider 的环境准备（H-12）和 `pre_job` 片段（W-04）。
4. 选择超时实现：`timeout` → `gtimeout` → 内置实现（X-03），写入 `timeout_impl`。
5. 启动命令，stdout 与 stderr 合并写入 `sim.log`；启动挂死看门狗（X-15）。
6. 命令结束后，对 `sim.log` 执行 `grep -E` 检查环境失败模式（X-16）。
7. 执行 `post_job` 片段，追加 `job.facts` 结束段。
8. **始终以 0 退出**，让 make 继续调度其他 job；只有脚本自身出错（如无法写入目录）才非 0。

超时实现的统一行为：先向整个进程组发 TERM，宽限 10 秒后发 KILL。GNU `timeout` 本身会把子进程放进自己的进程组并对整组发信号；内置实现用 `set -m` 让命令成为独立进程组，看门狗 `sleep` 到期后执行 `kill -TERM -<pgid>`。三种实现的行为一致性由 Q-02 的测试保证。

### 5.3 launch.sh

在 B 上启动一轮 `make`，并让它可以被远程终止：

```text
launch.sh --run-id ID [--detach] -- make -j16 -f .solouvm/Makefile regress
```

- 用 `set -m` 让 make 成为独立进程组，把进程组 ID 写入 `runs/<run_id>/launch.pid`；
- `--detach` 时用 `nohup` 后台运行并立即返回；
- 收到 TERM 或 INT 时，对 make 进程组转发 TERM；各 `run_job.sh` 收到后终止仿真并写入 `state=interrupted`。

### 5.4 契约文件

`job.facts`、`launch.pid`、`plan.json`、`env.json`、`results.json` 的字段级定义统一放在 [formats.md](formats.md)，本文不重复。

## 6. Provider 与 Sync

### 6.1 三层拆分

需求里的"provider"在实现上拆成两层，再加上独立的 sync：

| 接口 | 回答的问题 | 实现 |
|------|------------|------|
| Transport | 怎么在 B 上执行一条 shell 命令、读一个文件 | `local`、`ssh` |
| Launcher | job 交给谁调度 | `make`（直接 `make -j`）、`lsf`（每个 job `bsub`） |
| Sync | 文件怎么到 B | `none`、`rsync`、`git` |

需求里的组合由此得到：

| 场景 | Transport | Launcher | Sync |
|------|-----------|----------|------|
| 本机 | local | make | none |
| 共享盘 + 远端服务器 | ssh | make | none |
| 笔记本 + 远端服务器 | ssh | make | rsync |
| 集群（A 能直接 bsub） | local | lsf | none |
| 集群（先 ssh 到提交节点） | ssh | lsf | none / rsync |

`CFG["providers"]` 里的一个命名 provider 就是这三者的一个组合（H-01a）。

### 6.2 接口

```python
class Transport(Protocol):
    def exec(self, argv: list[str], *, cwd: str, check: bool = True) -> int: ...
    def spawn(self, argv: list[str], *, cwd: str) -> Process: ...      # 阻塞运行，可中断
    def read_lines(self, path: str) -> Iterator[str]: ...              # 流式读取（H-07）
    def read_many(self, pattern: str) -> dict[str, str]: ...           # 一次读回多个小文件
    def exists(self, path: str) -> bool: ...

class Launcher(Protocol):
    def make_vars(self, plan: Plan) -> dict[str, str]: ...             # 例如 {"SUBMIT": "bsub -K ..."}
    def jobs(self, requested: int) -> int: ...                          # 实际传给 make -j 的值
    def stop(self, transport: Transport, run_id: str) -> None: ...

class Sync(Protocol):
    def push(self, transport: Transport, files: Iterable[str]) -> None: ...
```

- **LSF 的做法**：Makefile 不变，launcher 只把 `SUBMIT` 设成 `bsub -K -q <队列> -J solouvm_<run_id>_<job_id>`。`-K` 让 bsub 等到 job 结束，于是 `make -j` 自然成为并发上限。终止时用 `bkill -J 'solouvm_<run_id>_*'`。
- **ssh 的做法**：全部调用系统 `ssh` 命令（H-03），复用 `~/.ssh/config`，建议在其中开启 `ControlMaster`，避免进度轮询时反复握手。
- **进度轮询**（H-13）：每隔几秒对 `runs/<run_id>/*/try*/job.facts` 调用一次 `read_many`。ssh 下就是一条远程命令，开销固定，与 job 数无关。
- **终止**（Q3）：先调用 `Launcher.stop`（LSF 需要 bkill），再通过 transport 读取 `launch.pid`，对 make 进程组发 TERM。
- **第三方扩展**（H-15）：用户在项目中实现上述任一接口并注册名字即可，例如 `CFG["plugins"] = ["myflow.SlurmLauncher"]`。

## 7. 判定

### 7.1 数据流

```text
job.facts ─▶ facts.parse ─┐
                          ├─▶ rules.decide ─▶ waivers ─▶ expect_fail ─▶ Result
sim.log ──▶ scan（单遍）──┘                                   ▲
                                                 case 记录 ───┘（只用于标注已知 bug）
```

- `scan` 每行做一次正则匹配，内存占用恒定（J-12），输出：UVM 计数、是否有 Report Summary、仿真器错误、`fail_regex` / `pass_regex` 命中、完成标记、首条错误行。
- 日志来自 `Transport.read_lines`，本地与远端对判定代码没有区别。

### 7.2 规则顺序

固定优先级：未运行 / 残留 → 未完成 → 环境失败 → 信号 → 超时 → 退出码 → 仿真器错误 → `fail_regex` → UVM 计数 → 缺 Report Summary → 缺完成标记 → 通过，最后应用 `expect_fail`。逐条定义、原因文本、waiver 与签名规则见 [verdict.md](verdict.md)，本文不重复。

### 7.3 retry

A 对本轮判定为 `fail` / `timeout` / `killed` / `error`（环境失败）且仍有剩余次数的 job，生成只含这些 job 的 `run.mk` 片段（运行目录为 `try<N+1>`），再次 run → judge。最终状态取最后一次尝试；只要某次通过即为通过，但报告标注"重试后通过"（R-04）。带波形重跑（R-03）是同一机制加 `WAVE=1`，且不改变最终状态。

## 8. Case 记录

- 记录在 collect 阶段随 `Case` 一起加载和校验（M-05），只存在于 A 的内存中，**不进入任何生成物**，也不影响 job 的执行。
- `notes`、`doc`、`progress` 只读：case 定义 + `runs/` 历史，不触发 gen、push 或 run。
- 报告联动（M-11、M-22）在 report 阶段完成：拿 `Result` 的 case 与种子去匹配记录。
- 导出的确定性（M-10）：记录按（case 名、类型、id、日期）稳定排序，导出内容不含生成时间。

## 9. Fast compile

```text
solouvm check
  ├─ slang 全量检查（E-01 ~ E-03）    A 上有 slang 就在 A 跑，否则经 transport 在 B 跑（E-09）
  ├─ 生成 gen/fake_top.sv（E-04）     只 import tb 组的包、实例化 interface
  │    └─ 用后端的"仅编译"命令编 tb 组 + fake_top
  └─ RTL 增量（E-06 ~ E-08，P2）     只对 vcs / xrun；状态文件 build/<mode>/fast_state.json
```

`check` 不产生 run_id，不写 `runs/`；结果直接输出到终端。发现错误时的退出码见 §12。细节在 `doc/fast-compile.md`。

## 10. 错误与退出码

| 来源 | 异常 / 情况 | 退出码 |
|------|-------------|--------|
| 配置、collect、plan | `ConfigError` | 2 |
| 环境（找不到 make、ssh 失败、B 上无法写入） | `InfraError` | 2 |
| 编译失败（build 的 facts 中 `rc != 0`） | `BuildError` | 2 |
| 选择条件为空（T-17） | `ConfigError` | 2 |
| 判定后存在失败、超时、环境失败、incomplete、xpass | — | 1 |
| 全部通过（含 xfail） | — | 0 |
| Ctrl-C | `KeyboardInterrupt` | 130 |

所有异常在 `cli.py` 统一捕获，输出 `solouvm: error: <消息>` 与 `hint: <建议>`（N-09、N-15）。

## 11. 测试策略

- **假仿真器用 bash 写**（S-06）：测试时 B 就是本机，但假仿真器不依赖 Python，才能真实验证 N-04。它根据 plusarg 模拟：通过、UVM_ERROR、UVM_FATAL、缺 Summary、非零退出、挂死、段错误、license 失败、非 UVM 标记。
- **分层测试**：model / plan / gen.shell / judge 为纯函数单元测试；gen 产物做快照测试（依赖 N-11 的确定性）；端到端测试在本机真实执行 `make`。
- **超时三种实现**（Q-02）：通过环境变量强制 `run_job.sh` 选用指定实现，在 Linux 与 macOS 的 CI 上各跑一遍。
- **ssh**（Q-03）：容器内起 sshd，用 `ssh localhost` 走完整远程流程。

## 12. 已决问题

初稿中的待决问题已全部给出结论，详见 [decisions.md](decisions.md)：

| 问题 | 结论 |
|------|------|
| 编译锁在 B 崩溃后残留 | 锁目录内记录持有者；同主机且进程已死则自动清理，跨主机需 `clean --locks`（D23） |
| job 数上万时 Makefile 体积 | 首版内联；实测 2 万 job 解析超过 5 秒时改为参数文件（D24） |
| 上千个 `bsub -K` | 首版采用，并发上限即 `make -j`；批量提交模式列为 P3（D25） |
| JSON 字段级定义 | 见 [formats.md](formats.md) |
| `check` 发现错误时的退出码 | 1；无法完成检查时为 2（D26） |
