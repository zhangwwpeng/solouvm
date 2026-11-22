# 架构

本文说明 solouvm 由哪些部分组成、数据怎么流动、各部分之间的接口和文件格式。"做什么"以 [requirements.md](requirements.md) 为准，本文只回答"怎么组织"；括号里的编号指向对应需求。

> 本文描述 SoloUVM 的实现架构及保留的设计约定。

## 1. 总览

solouvm 分成两个运行时，中间只通过文件和 shell 命令交互：

```text
 A：控制机 —— solouvm（Python）                     B：执行机 —— make + bash
┌──────────────────────────────────────┐          ┌──────────────────────────────────┐
│ load ─ collect ─ plan ─ gen          │          │                                  │
│                          │           │  push    │ 脚本、文件清单、run.mk           │
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
| gen | A | `Plan` | `gen/` 下的脚本、各编译键目录中的文件清单、本轮自包含的 `runs/<run_id>/run.mk`；同时算出编译键（§4.2） | `gen` |
| push | A→B | 源码、本轮生成物清单 | B 上的同一份目录树 | `push` |
| build | B | 文件清单 | `build/<mode>/<key>/` 下的仿真可执行文件、编译事实、成功标记；已有成功标记时跳过 | `build` |
| run | B | 本轮 Makefile | 每个 job 的 `sim.log`、`job.facts` | `run` |
| judge | A | 编译事实、`job.facts`、日志（远端时为粗筛后的行） | `Result` 列表 | `judge` |
| report | A | `Result` 列表、case 记录 | 终端摘要、`results.json`、JUnit 等 | 归入 `judge` |
| history | A | 本轮全部产物 | `runs/<run_id>/` 归档、`latest` 链接 | — |
| triage | A | 判定结果、case 记录、系统文档 | 分诊任务、上下文包；子 agent 写回的记录与调试文档 | — |
| review | A | 分诊结论 | 最终状态、审查记录 | — |

triage 与 review 不在 `regress` 内自动执行：回归结束后由主 agent（或人）显式调用 `triage plan`，再为每个任务启动子 agent，最后逐个 `triage review`。完整流程见 [ai-workflow.md](ai-workflow.md)。

retry（R-01）不是新阶段：judge 之后，以 `TRY=<N+1>` 再执行本轮 `run.mk` 中失败 job 的目标，然后再 judge（§7.3）。本轮 `run.mk` 已包含全部 job 目标，不需要重新 gen。

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
| `validate` | ✓ | ✓ | | | | | | | |
| `explain` / `triage *` | ✓ | ✓ | 读已有 plan | | | | | 读已有结果 | |
| `bug` / `note` / `todo` 写入 | ✓ | ✓ | | | | | | | |
| `check` | ✓ | ✓ | | 生成 fake_top | 视 provider | slang / tb 编译 | | | |
| `new` / `uvm sync` | ✓ | 读 `tbspec.py` | | 生成 SV 组件 | | | | | |

`judge` 也要 collect：判定参数（checks、waiver）和 case 记录（报告里的"已知问题"）都来自当前的 case 定义，这正是"改了规则不用重跑"（J-08）的来源。

## 3. 模块划分

Python 包结构如下，括号内为主要承担的需求区。

```text
solouvm/
  __init__.py          公开 API：Case、Bug、Todo、Log、Note、Expect、Waiver、Triage
  uvm.py               公开 API：Env、Agent、Interface、Signal、Field（转引自 uvmgen.spec）
  cli.py               参数解析、命令分发、退出码；不含业务逻辑            (L)
  ui.py                终端输出：rich 的唯一入口；非终端与 --json 时退化为纯文本 (N-22)
  _vendor/             rich、pygments、markdown-it-py、mdurl；vendor.txt 锁定版本 (N-02)
  output.py            人类输出与 --json 外壳、截断、next 建议、错误码        (N-17 ~ N-21)
  roles.py             角色与命令白名单                                      (AI-10)
  errors.py            异常层级与退出码映射                                  (N-07, N-09)
  model.py             纯数据：Case、Expect、Waiver、Checks、记录类型、Job、Plan、Facts、Result      (T, M)
  config/
    load.py            执行配置文件、合并默认值与本地覆盖、展开 ${VAR}       (C)
    schema.py          CFG 的键、类型、默认值；未知键给出最接近的建议        (C-04)
  collect/
    files.py           FILES 分组、glob 展开、缺失检查                       (F)
    dotf.py            解析已有 .f：-f/-F、+incdir+、+define+、-v、-y         (F-06, F-07)
    cases.py           case.py 与 case_dirs 的收集、记录校验                 (T-01, M-05, M-15)
  plan.py              job 展开、种子、选择、分片、retry 子集                (T, R)
  buildkey.py          编译键计算与哈希缓存                                  (D-05, D-06)
  gen/
    shell.py           shell 与 Makefile 转义；所有生成物都经过这里          (N-12)
    filelist.py        filelist.<组>.f 与 filelist.f                         (F-04)
    makefile.py        build.mk、run.mk、入口 Makefile                       (G)
    scripts.py         run_job.sh、launch.sh 模板                            (X)
  backends/
    base.py            后端接口：编译/运行/种子/波形/覆盖率命令、错误模式    (S)
    vcs.py xrun.py questa.py xezim.py verilator.py fake.py
    fake_sim.sh        假仿真器的 bash 实现（S-06、D22），由 fake.py 渲染进生成物
  providers/
    base.py            Transport、Launcher、Sync 三个接口                    (H)
    transport_local.py transport_ssh.py
    launcher_make.py   launcher_lsf.py
    sync_none.py       sync_rsync.py  sync_git.py
  pipeline.py          load → collect → plan → gen → push → build → run 的编排        (G, X)
  judge/
    facts.py           解析 job.facts                                        (X-04, Q1)
    scan.py            单遍日志扫描器                                        (J-12)
    rules.py           固定优先级判定                                        (J-01 ~ J-07)
    expect.py          expect 检查、仿真结束时间                             (J-18, J-21)
    prefilter.py       远端粗筛的关键词生成                                  (H-16)
    waivers.py         waiver 匹配与记录                                     (J-14)
    signature.py       错误签名归一化                                        (J-11)
    __init__.py        判定编排：编译事实 + 每个 job 的 facts/日志 → Result 列表
  report/
    text.py json.py junit.py html.py                                         (K)
  records/
    write.py           记录写回 Python 源文件：加锁、ast 定位、插入、重载校验、回滚 (M-14)
  triage/
    plan.py            生成分诊任务、按签名合并                              (AI-05)
    packet.py          组装上下文包、长度控制                                (AI-06)
    result.py          写入结论与调试文档、校验                              (AI-07, AI-16)
    review.py          审查与最终状态更新                                    (AI-09)
  explain.py           单个 job 的排查信息、源码片段解析                     (AI-04, AI-15)
  wave.py              波形文本查询（经 transport 在 B 上执行导出工具）      (AI-13)
  notes/
    query.py           跨 case 查询记录                                      (M-06, M-07)
    export.py          markdown 导出                                         (M-08 ~ M-10)
    progress.py        进度总览                                              (M-20)
  check/
    slang.py faketop.py incremental.py                                       (E)
  history.py           runs 目录、env 信息、保留策略                         (Y)
  uvmgen/
    spec.py            tbspec.py 的数据模型：Env、Agent、Signal、Field       (U-02)
    render.py          模板渲染（string.Template），写文件头与用户代码区      (U-01, U-06)
    regions.py         用户代码区的提取、合并、.orphan 输出                   (U-04, U-05)
    pkgfiles.py        维护各 package 的 <pkg>_files.svh                       (U-11)
    templates/         内置模板，按 style 版本组织                            (U-06)
  init.py              起步项目生成（Z-01、U-03）
  hooks.py             A 端 hook 的加载与执行                                (W)
```

依赖方向（违反即视为设计缺陷）：

```text
cli ─▶ 各业务模块 ─▶ model / errors
                 ╲
                  ▶ gen.shell（凡是产出 shell 文本的模块）

providers ✗─▶ judge      provider 只搬运字节，不理解内容
triage    ─▶ judge / explain / records   分诊只读判定结果，写入只经 records.write
records.write 是唯一修改用户 Python 源文件的模块（含 new test 新增 case）
uvmgen 是唯一生成或修改用户目录下 SV 文件的模块（.solouvm/ 内的 fake_top 由 check 生成）
任何模块 ✗─▶ _vendor   只有 ui.py 可以 import _vendor 中的库
judge     ✗─▶ providers  judge 只吃一个"按行迭代"的输入，不关心来自本地还是远端
backends  ✗─▶ providers  后端只产出命令模板与错误模式
```

## 4. 目录布局

### 4.1 目录树

A 和 B 上的目录树形状相同，路径都相对项目根（N-05）。差别只是谁写哪些文件。

```text
<项目根>/
  solouvm.conf.py  solouvm.local.py  filelist.py  case.py  cases/  tbspec.py
  verif/                                    UVM 环境（uvm-style.md §2）
  docs/debug/<run_id>/<序号>.md             调试文档，入库                          [A 写，经 triage write]
  Makefile                                  仅当原本不存在时生成：include .solouvm/Makefile
  .solouvm/                                 整体 gitignore
    Makefile                                手动入口：-include runs/latest/run.mk
    gen/
      bin/run_job.sh  bin/launch.sh         与轮次无关的脚本                        [A 写，push 到 B]
      filelist.f  filelist.<组>.f           最近一次 gen 的文件清单（相对项目根），供人和外部工具使用 [A 写]
    build/<mode>/<key>/                     一个编译键一个目录（D-05）
      filelist.f  filelist.<组>.f           本键的文件清单（相对本目录）            [A 写，push 到 B]
      simv 等产物  build.log  job.facts                                             [B 写]
      .ok                                   编译成功标记，失败时不写                [B 写]
    build/<mode>/<key>.lock/                编译锁（D23）                           [B 写]
    check/                                  check 的 check.f、fake_top.sv、产物与 fast_state.json
    runs/
      <run_id>/
        run.mk                              本轮全部目标，自包含                    [A 写，push 到 B]
        plan.json  env.json                                                         [A 写，只留在 A]
        results.json  results.txt  junit.xml                                        [A 写]
        triage/tasks.json                   本轮分诊任务、结论、审查                [A 写]
        launch.pid                          本轮 make 的主机与进程组 ID             [B 写]
        <job_id>/try<N>/                    第 N 次尝试的运行目录                   [B 写]
          sim.log  job.facts  波形  覆盖率库 ...
      latest -> <run_id>                    只供手动使用（G-07）
    cache/hash.json                         编译键用的 mtime / 大小 / 哈希缓存       [A 写]
    locks/records.lock/                     记录写入锁（M-14）                       [A 写]
    solouvm.log                             solouvm 自身的调试日志（Z-07）           [A 写]
```

要点：

- **本轮 `run.mk` 自包含**：其中有本轮用到的编译目标（按编译键）与全部 job 目标，不 include 其他会变化的文件。solouvm 自己启动 make 时一律 `-f .solouvm/runs/<run_id>/run.mk`（G-07）；两轮并发、retry、事后复现都不会串到别的轮次。`latest` 只服务于手动执行。
- **编译产物按键分目录**：输入一变，键就变，产物进新目录；正在被 job 使用的 simv 不会被改写。同一个键已有 `.ok` 时直接复用。
- **每次尝试一个目录**：目录名由 make 变量 `TRY` 决定（默认 1）。retry 用 `try2`、`try3`；手动复现用非数字编号（如 `TRY=r1`），不覆盖原始证据，也不参与判定。
- **`plan.json` 不上 B**：B 不需要它，`run.mk` 里已经包含执行所需的一切。
- **清理**：`clean` 按保留策略删除旧轮次；编译目录只在不再被任何保留轮次引用时删除。

### 4.2 编译键

gen 阶段在 A 上计算（D-05、D-06）：

```text
key = sha1(
    编译模式名、该模式的全部编译选项与后端编译命令模板,
    展开后的有序文件清单,
    清单中每个文件的内容哈希,
    每个 incdir、-y 库目录下全部文件的内容哈希（递归）,
    后端名、provider 的 setup 片段、仿真器版本（取不到时为空）、solouvm 版本
)[:12]
```

- 先比较 mtime 与大小，有变化才重新计算内容哈希，结果缓存在 `.solouvm/cache/hash.json`。
- 有输入在 A 上读不到时（项目根之外且 A 上不存在、或路径里有只在 B 上展开的 `${VAR}`），键里再加入 run_id，相当于这次不复用，并在输出中说明原因。宁可多编一次，也不复用可能过期的产物。
- 不确定：大工程上首次计算的耗时；仿真器版本需要在 B 上执行命令获取，远程时多一次往返，可按主机缓存。
- 不确定：新键从空目录编译，用不上仿真器自身的增量编译。如果全量编译太慢，可以考虑从上一个键的目录复制后再编译。

## 5. 生成物

### 5.1 Makefile

```make
# .solouvm/Makefile —— 手动入口
-include .solouvm/runs/latest/run.mk
```

```make
# .solouvm/runs/<run_id>/run.mk（示意）—— 本轮自包含
ROOT    := $(CURDIR)
RUN_ID  := 20261103-101500-a1b2
RUN_JOB := bash $(ROOT)/.solouvm/gen/bin/run_job.sh
SUBMIT  ?=                                 # launcher 注入，例如 bsub -K -q normal -J ...
TRY     ?= 1
WAVE    ?= 0

B_default := .solouvm/build/default/3c9e1f2a

.PHONY: build regress run/burst__4417 run/smoke__1

build: $(B_default)/.ok

$(B_default)/.ok:
	$(SUBMIT) $(RUN_JOB) --kind build --dir $(B_default) --log build.log --lock --timeout 7200 \
	    -- vcs -full64 -sverilog -ntb_opts uvm-1.2 -f filelist.f -top tb_top -o simv

regress: run/burst__4417 run/smoke__1

run/burst__4417: $(B_default)/.ok
	$(SUBMIT) $(RUN_JOB) --kind job --dir .solouvm/runs/$(RUN_ID)/burst__4417/try$(TRY) \
	    --run-id $(RUN_ID) --job-id burst__4417 --try $(TRY) --timeout 1800 --hang 600 \
	    -- ../../../../build/default/3c9e1f2a/simv +UVM_TESTNAME=axi_burst_test \
	       +ntb_random_seed=4417 +SOLOUVM_SEED=4417 +SOLOUVM_JOB_ID=burst__4417 +NUM=32 \
	       $(if $(filter 1,$(WAVE)),+fsdb+all=on)
```

规则：

- 所有参数在 A 上经 `gen/shell.py` 转义后写入：先做 shell 转义，再把 `$` 写成 `$$`（N-12）。
- 编译目标是真实的成功标记文件 `.ok`，不是伪目标：标记存在就复用，不存在就编译；项目根下有没有同名目录都不影响（G-02）。
- 命令中的路径都相对于命令的运行目录（编译在编译目录，job 在运行目录），由 A 生成时换算；运行时需要的绝对路径由 `run_job.sh` 计算（N-05）。
- `SUBMIT` 变量是 launcher 的唯一注入点（§6.2）。直接 `make` 时它为空，命令就在本机执行。
- solouvm 启动 make 的方式：`make -C <项目根> -f .solouvm/runs/<run_id>/run.mk -k -j<N> regress`。`-k` 让某个编译模式失败时，其他模式的 job 照常执行；依赖失败编译的 job 不会执行（D-07）。
- 波形开关（X-11）：`WAVE=1` 时追加后端定义的波形参数。
- 手动复现：`make -f .solouvm/runs/<run_id>/run.mk TRY=r1 WAVE=1 run/burst__4417`，结果写到新目录。

### 5.2 run_job.sh

每次编译和每个 job 都经过它，因此编译与仿真的事实格式完全相同（D-04）。

```text
run_job.sh --kind build|job|check --dir DIR [--log NAME] [--run-id ID] [--job-id ID] [--try N]
           [--timeout SEC] [--hang SEC] [--lock] -- COMMAND...
```

执行步骤：

1. 由脚本自身的位置推出项目根的绝对路径，导出 `SOLOUVM_ROOT`；`kind=job` 时在命令末尾追加 `+SOLOUVM_ROOT=<项目根绝对路径>` 与 `+SOLOUVM_OUTDIR=<DIR 绝对路径>`（X-10）。
2. 创建 `DIR`。`--lock` 时获取 `DIR.lock`（D23）；拿到锁后如果 `.ok` 已经存在（另一轮刚编完同一个键），直接成功退出。
3. 写入 `job.facts` 的开始段（`state=running`）。
4. 进入 `DIR`，依次执行 provider 的 `setup`（H-12）与 `job.pre`（W-04）。
5. 选择超时实现：`timeout` → `gtimeout` → 内置实现（X-03），写入 `timeout_impl`。
6. 让命令在独立的进程组中后台运行，输出写入日志（默认 `sim.log`，编译为 `build.log`）；脚本自己 `wait`，这样收到 TERM 时 trap 能立即执行（bash 在前台子进程结束之前不会执行 trap）。同时启动挂死看门狗（X-15）。
7. 执行 `job.post`，追加 `job.facts` 结束段。
8. 退出码：
   - `kind=job`：始终为 0，让 make 继续调度其他 job，结论由 A 判定；
   - `kind=build`、`kind=check`：与命令的退出码一致；`kind=build` 成功时先写 `.ok` 再退出（D-07）；
   - 脚本自身出错（例如无法写入目录）：非 0。
9. 收到 TERM / INT：终止命令的进程组，结束段写 `state=interrupted`。

环境失败（license 等）不在这里判断，由 A 读日志判定（X-16）。

超时实现的统一行为：先向整个进程组发 TERM，宽限 10 秒后发 KILL。GNU `timeout` 本身会把子进程放进自己的进程组并对整组发信号；内置实现用 `set -m` 让命令成为独立进程组，看门狗 `sleep` 到期后执行 `kill -TERM -<pgid>`。三种实现的行为一致性由 Q-02 的测试保证。

### 5.3 launch.sh

在 B 上启动一轮 `make`，并让它可以被远程终止：

```text
launch.sh --run-id ID [--detach] -- make -f .solouvm/runs/ID/run.mk -k -j16 regress
```

- 用 `set -m` 让 make 成为独立进程组，把主机名与进程组 ID 写入 `runs/<run_id>/launch.pid`；
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
    def read_lines(self, path: str, *, keywords: list[str] | None = None,
                   lines: tuple[int, int] | None = None) -> Iterator[tuple[int, str]]: ...
        # 流式读取，带原始行号（H-07）；keywords 不为空时在 B 上粗筛（H-16）；lines 只取一段
    def read_many(self, pattern: str) -> dict[str, str]: ...           # 一次读回多个小文件
    def put(self, path: str, data: bytes) -> None: ...                 # 向 B 写入单个文件
    def exists(self, path: str) -> bool: ...

class Launcher(Protocol):
    def make_vars(self, plan: Plan) -> dict[str, str]: ...             # 例如 {"SUBMIT": "bsub -K ..."}
    def jobs(self, requested: int) -> int: ...                          # 实际传给 make -j 的值
    def stop(self, transport: Transport, run_id: str) -> None: ...

class Sync(Protocol):
    def push(self, transport: Transport, tree: str, files: Iterable[str]) -> None: ...
        # tree：项目源码树；files：本轮生成物的显式清单
```

- **push 的内容**（H-08）：项目源码树，加上本轮生成物清单：`gen/bin/` 下的脚本、本轮用到的各编译键目录中的文件清单、本轮的 `run.mk`。
  - `rsync`：第一步同步项目树（排除 `.git/` 与整个 `.solouvm/`），第二步用 `--files-from` 传生成物清单（H-09）；
  - `git`：A 的工作区必须干净，否则报错并建议改用 `rsync`；代码经 git 传输，生成物逐个用 `Transport.put` 写入（H-17，不确定：性能）；
  - `none`：共享盘，不传输。
- **LSF 的做法**：Makefile 不变，launcher 只把 `SUBMIT` 设成 `bsub -K -q <队列> -J solouvm_<run_id>_<job_id>`。`-K` 让 bsub 等到 job 结束，于是 `make -j` 自然成为并发上限。终止时先 `bkill -J 'solouvm_<run_id>_*'`，再停 make（不确定：被杀掉的 `bsub -K` 是否会连带终止 LSF 上的 job，所以总是先 bkill）。
- **ssh 的做法**：全部调用系统 `ssh` 命令（H-03），复用 `~/.ssh/config`，建议在其中开启 `ControlMaster`，避免进度轮询时反复握手；读日志时开启压缩。
- **进度轮询**（H-13）：每隔几秒对 `runs/<run_id>/*/try*/job.facts` 调用一次 `read_many`。ssh 下就是一条远程命令，开销固定，与 job 数无关。本机执行时直接读文件。
- **终止**（Q3）：先调用 `Launcher.stop`（LSF 需要 bkill），再通过 transport 读取 `launch.pid`，对 make 进程组发 TERM。
- **第三方扩展**（H-15）：用户在项目中实现上述任一接口并注册名字即可，例如 `CFG["plugins"] = ["myflow.SlurmLauncher"]`。

## 7. 判定

### 7.1 数据流

```text
sim.log ──（远端时先在 B 上粗筛）──▶ scan（单遍）──▶ 扫描结果：UVM 计数、错误行、正则命中、结束时间
                                                        │
                                                 waivers：扣除被豁免的行
                                                        │
编译事实、job.facts ──▶ facts.parse ──▶ rules.decide（最后一步是 expect 检查）──▶ expect_fail ──▶ 机械结果
                                                                                                    │
                                                  已接受且未过期的分诊结论 ──────────────────▶ 最终状态
```

- `scan` 对每行先做廉价的子串预检（例如是否含 `UVM_`），命中再做正则；内存占用恒定（J-12）。多个 job 用多进程并行判定。
- 日志来自 `Transport.read_lines`，本地与远端对判定代码没有区别；远端时只传粗筛后的行，带原始行号（H-16，规则见 verdict.md §4.5）。
- case 记录只用于报告中的标注（已知 bug、提示），不影响机械结果。

### 7.2 规则顺序

固定优先级：依赖的编译失败 / 未运行 / 残留 → 未完成 → 环境失败 → 信号 → 超时 → 退出码 → 仿真器错误 → `fail_regex` → UVM 计数 → 缺 Report Summary → 缺完成标记 → expect → 通过，最后应用 `expect_fail`。逐条定义、原因文本、waiver 与签名规则见 [verdict.md](verdict.md)，本文不重复。

### 7.3 retry

A 对本轮判定后状态在 `regress.retry_on` 中（默认只有 `error`，即环境失败）、且仍有剩余次数的 job，以 `TRY=<N+1>` 重新执行本轮 `run.mk` 中对应的目标，再 judge，直到没有可重试的 job。retry 使用相同的种子。`--detach` 时不自动 retry（R-01）。

最终状态取最后一次尝试；只要某次通过即为通过，但报告标注"重试后通过"（R-04）。带波形重跑（R-03）是同一机制加 `WAVE=1`，不改变最终状态。

## 8. Case 记录

- 记录写回只有一条路径：`records/write.py`（M-14）：
  1. 获取 A 上的记录锁 `.solouvm/locks/records.lock`（`mkdir` 锁），多个子 agent 可以并发调用；
  2. 读入目标文件并记下内容哈希；需要编号时在锁内分配；
  3. 用 `ast` 定位目标 `Case(...)` 节点，按操作类型改写源码：追加记录时在 `notes=[...]` 末尾插入，`notes` 参数不存在时在参数表末尾补上 `notes=[...]`；更新记录（`bug update`、`todo done`、`triage write` 覆盖未审查结论、`triage review`）时按该记录节点的 `lineno`/`end_lineno`/`col_offset`/`end_col_offset` 整体替换；文件缺少 `from solouvm import ...` 中的目标名字时补齐导入；插入文本沿用该参数行首的缩进；
  4. 写回前确认文件内容没有在读与写之间被改动，否则放弃并报错；
  5. 重新加载整个 case 文件并校验，任何一步失败都恢复原文件。
- `new test` 新增 case 走同一条路径：配置了 `case_dirs` 时新建 `cases/<name>.py`；否则在 `case.py` 的 `CASES` 列表末尾插入。
- 记录在 collect 阶段随 `Case` 一起加载和校验（M-05），只存在于 A 的内存中，**不进入任何生成物**，也不影响 job 的执行。
- `notes`、`doc`、`progress` 只读：case 定义 + `runs/` 历史，不触发 gen、push 或 run。
- 报告联动（M-11、M-22）在 report 阶段完成：拿 `Result` 的 case、种子、签名去匹配记录。
- 导出的确定性（M-10）：记录按（case 名、类型、id、日期）稳定排序，导出内容不含生成时间。

## 9. Fast compile

```text
solouvm check
  ├─ slang 全量检查（E-01 ~ E-03）    A 上有 slang 就在 A 跑，否则经 transport 在 B 跑（E-09）
  ├─ 生成 .solouvm/check/fake_top.sv（E-04）
  │    └─ 只 import tb 组的各 package；是否需要例化 interface 不确定（fast-compile.md §3.2）
  │       用后端的"仅编译"命令编 tb 组 + fake_top
  └─ RTL 增量（E-06 ~ E-08，待定）   只对 vcs / xrun；状态文件 .solouvm/check/fast_state.json
```

`check` 不产生 run_id，不写 `runs/`，不碰回归的编译目录；所有产物都在 `.solouvm/check/`。发现错误时的退出码见 §12。细节在 [fast-compile.md](fast-compile.md)。

## 10. 错误与退出码

| 来源 | 异常 / 情况 | 退出码 |
|------|-------------|--------|
| 配置、collect、plan | `ConfigError` | 2 |
| 环境（找不到 make、ssh 失败、B 上无法写入） | `InfraError` | 2 |
| 编译失败（编译事实中 `rc != 0`） | `BuildError` | 2 |
| 选择条件为空（T-17）、job 数超限（T-18） | `ConfigError` | 2 |
| 角色不允许（AI-10） | `RoleError` | 2 |
| 回归仍在进行时 `judge`（J-23） | `RunActiveError` | 2 |
| 判定后存在不算通过的结果 | — | 1 |
| `check` 发现代码错误 | — | 1 |
| `triage status` 存在未审查的任务 | — | 1 |
| 全部通过（含 xfail） | — | 0 |
| Ctrl-C | `KeyboardInterrupt` | 130 |

所有异常在 `cli.py` 统一捕获，输出 `solouvm: error: <消息>` 与 `hint: <建议>`（N-09、N-15）；`--json` 时输出错误外壳。错误码的完整列表见 [formats.md](formats.md) §9。

## 11. 测试策略

- **假仿真器用 bash 写**（S-06）：测试时 B 就是本机，但假仿真器不依赖 Python，才能真实验证 N-04。它根据 plusarg 模拟：通过、UVM_ERROR、UVM_FATAL、缺 Summary、非零退出、挂死、段错误、license 失败、非 UVM 标记、编译失败。
- **分层测试**：model / plan / buildkey / gen.shell / judge 为纯函数单元测试；gen 产物做快照测试（依赖 N-11 的确定性）；端到端测试在本机真实执行 `make`。
- **关键场景的端到端测试**：编译失败时 job 不执行、不使用旧产物（D-07）；两轮回归并发互不干扰（N-14）；多个进程并发写同一个 case 文件（M-14）；Ctrl-C 后没有残留进程。
- **超时三种实现**（Q-02）：通过环境变量强制 `run_job.sh` 选用指定实现，在 Linux 与 macOS 的 CI 上各跑一遍。
- **输出**：非终端与 `--json` 时，输出中不含 ANSI 控制字符（N-22）。
- **ssh**（Q-03）：容器内起 sshd，用 `ssh localhost` 走完整远程流程，包括粗筛判定与本机读完整日志判定的结果一致。

## 12. 已决问题

初稿中的待决问题已全部给出结论，详见 [decisions.md](decisions.md)；仍需验证的事项汇总在 requirements.md §26。

| 问题 | 结论 |
|------|------|
| 编译锁在 B 崩溃后残留 | 锁目录内记录持有者；同主机且进程已死则自动清理，跨主机需 `clean --locks`（D23） |
| job 数上万时 Makefile 体积 | 首版内联；实测 2 万 job 解析超过 5 秒时改为参数文件（D24） |
| 上千个 `bsub -K` | 首版采用，并发上限即 `make -j`；批量提交模式列为待定（D25） |
| JSON 字段级定义 | 见 [formats.md](formats.md) |
| `check` 发现错误时的退出码 | 1；无法完成检查时为 2（D26） |
| 编译失败后仍用旧产物跑 job | 编译失败非 0 退出；产物按编译键分目录，job 依赖成功标记（D41） |
| 经 `latest` 找本轮目标会串轮次 | 本轮 `run.mk` 自包含，solouvm 一律指定它；尝试编号由 `TRY` 传入（D42） |
| 远程判定要拉回全部日志 | B 上按关键词粗筛、压缩传输，A 上精判（D44） |
