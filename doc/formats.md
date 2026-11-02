# 文件格式

本文定义 solouvm 读写的全部结构化文件。凡是跨进程、跨机器、跨版本传递的数据，都以本文为准。

> 状态：设计稿，尚无代码。

## 1. 通用规则

- 每个文件都带格式版本：JSON 文件为顶层字段 `"schema"`，`job.facts` 为 `schema=` 行。读到不支持的版本直接报错，提示重新生成（N-08）。
- 版本号只在**不兼容**变化时加一；新增可选字段不加版本号，读取方忽略不认识的字段。
- 时间一律为带时区的 ISO 8601，精确到秒：`2026-11-03T10:15:02+08:00`。
- 时长一律为秒，浮点数，保留 3 位小数。
- 路径一律相对项目根，用 `/` 分隔（N-05）。
- JSON 输出时键按固定顺序、缩进 2 空格、UTF-8、结尾换行，保证同样内容逐字节相同（N-11）。

| 文件 | 写入方 | 读取方 | 位置 |
|------|--------|--------|------|
| `job.facts` | B（`run_job.sh`） | A | `runs/<run_id>/<job_id>/try<N>/`、`build/<mode>/<key>/`、`check/` |
| `.ok` | B（`run_job.sh`） | make、A | `build/<mode>/<key>/`：编译成功标记，空文件 |
| `launch.pid` | B（`launch.sh`） | A | `runs/<run_id>/` |
| `plan.json` | A | A | `runs/<run_id>/` |
| `env.json` | A | A | `runs/<run_id>/` |
| `results.json` | A | A、CI、用户脚本 | `runs/<run_id>/` |
| `triage/tasks.json` | A | A | `runs/<run_id>/` |
| `fast_state.json` | A | A | `check/` |
| `cache/hash.json` | A | A | `.solouvm/cache/`：编译键用的哈希缓存，内部格式，不对外承诺 |
| 调试文档 | 子 agent（经 `triage write`） | 人、AI | `docs/debug/<run_id>/<序号>.md`（入库） |
| `--json` 输出 | A | 程序、AI | 标准输出 |

上表中 `runs/`、`build/`、`check/`、`cache/` 都在 `.solouvm/` 下。

## 2. `job.facts`

`key=value`，一行一项。值不含换行；同名键以最后一次出现为准。由 `run_job.sh` 分两段写入（Q1）。编译、job、check 用同一种格式。

### 2.1 开始段

```text
schema=1
generator=solouvm 0.1.0
kind=job
run_id=20261103-101500-a1b2
job_id=burst__4417
try=1
host=simbox01
pid=48213
timeout_impl=gtimeout
started_at=2026-11-03T10:15:02+08:00
state=running
```

### 2.2 结束段

```text
state=done
rc=0
signal=
timed_out=0
hung=0
finished_at=2026-11-03T10:21:40+08:00
duration_s=398.214
```

### 2.3 字段

| 键 | 段 | 取值 | 说明 |
|----|----|------|------|
| `schema` | 开始 | `1` | |
| `generator` | 开始 | `solouvm <版本>` | 生成 `run_job.sh` 的版本（N-13） |
| `kind` | 开始 | `job` / `build` / `check` | |
| `run_id` | 开始 | | 触发这次执行的轮次；`kind=build` 时是第一次编译这个键的轮次 |
| `job_id` | 开始 | | `kind=build` 时为 `build__<mode>__<key>` |
| `try` | 开始 | 正整数；手动复现时可为任意编号 | 尝试编号，来自 make 变量 `TRY`；judge 只认数字 |
| `host` | 开始 | | `hostname` 输出 |
| `pid` | 开始 | | `run_job.sh` 自身的 PID |
| `timeout_impl` | 开始 | `timeout` / `gtimeout` / `builtin` | |
| `started_at` | 开始 | ISO 8601 | |
| `state` | 两段 | `running` / `done` / `interrupted` | |
| `rc` | 结束 | 整数 | 被信号终止时为 `128 + 信号号` |
| `signal` | 结束 | 信号名，如 `SIGSEGV`；无则为空 | 超时造成的 TERM / KILL 不写在这里 |
| `timed_out` | 结束 | `0` / `1` | |
| `hung` | 结束 | `0` / `1` | |
| `finished_at` | 结束 | ISO 8601 | |
| `duration_s` | 结束 | 浮点数 | |

`run_job.sh` 写入的值都由它自己产生（时间、数字、主机名等），不含任何用户输入（N-12）。环境失败（license 等）不在事实文件中，由 A 读日志判定（X-16）。

### 2.4 A 端解析

```text
job 依赖的编译键没有 .ok，且编译事实 rc != 0 → 编译失败（error）
文件不存在                → 未运行
无 state 或 state=running → 未完成（incomplete）；回归仍在进行时见 J-23
run_id 与本轮不符           → 残留（error）
schema 不支持              → 报错（error）
其余                       → 交给判定规则（verdict.md §3）
```

## 3. `launch.pid`

一行，`<host> <pgid>`，例如 `simbox01 48100`。`stop` 时 A 经 transport 在该主机上执行 `kill -TERM -<pgid>`。

## 4. `plan.json`

```json
{
  "schema": 1,
  "run_id": "20261103-101500-a1b2",
  "created_at": "2026-11-03T10:15:00+08:00",
  "parent_run": null,
  "provider": "farm",
  "selection": {
    "case": [], "tag": ["nightly"], "exclude_tag": [], "build": null,
    "stage": ["wip", "ready", "signoff"], "shard": null
  },
  "builds": {"default": "3c9e1f2a"},
  "jobs": [
    {
      "job_id": "burst__4417",
      "case": "burst",
      "seed": 4417,
      "build": "default",
      "uvm_test": "axi_burst_test",
      "params": {"NUM": 32},
      "opts": [],
      "env": {},
      "timeout_s": 3600.0,
      "hang_s": 600.0,
      "retry": 1,
      "expect_fail": false,
      "tags": ["nightly", "axi"],
      "dir": ".solouvm/runs/20261103-101500-a1b2/burst__4417"
    }
  ]
}
```

| 字段 | 说明 |
|------|------|
| `parent_run` | `rerun` 产生的计划指向原轮次 |
| `selection` | 本轮使用的选择条件，用于报告与复现 |
| `builds` | 编译模式 → 本轮使用的编译键（architecture.md §4.2） |
| `jobs[].dir` | job 根目录；每次尝试在其下的 `try<N>/` |

`jobs` 按（case 名、种子）排序。

## 5. `env.json`

```json
{
  "schema": 1,
  "run_id": "20261103-101500-a1b2",
  "solouvm": "0.1.0",
  "python": "3.12.4",
  "a_host": "wp-mbp",
  "b_host": "simbox01",
  "provider": {"name": "farm", "transport": "ssh", "launcher": "lsf", "sync": "rsync"},
  "git": {"commit": "9a2d5cd…", "branch": "main", "dirty": true, "diff": "git.diff"},
  "simulator": {"name": "vcs", "version": "V-2025.06"}
}
```

- `git.diff`：工作区有未提交改动时，把 `git diff HEAD` 保存为同目录下的文件（Y-03），此处记录文件名；没有改动时为 `null`。
- `simulator.version`：由后端定义获取方式（如 `vcs -ID`），在 B 上执行一次；获取失败时为 `null`，不报错。

## 6. `results.json`

```json
{
  "schema": 1,
  "run_id": "20261103-101500-a1b2",
  "judged_at": "2026-11-03T11:02:10+08:00",
  "partial": false,
  "summary": {
    "total": 120, "passed": 113,
    "by_status": {"pass": 110, "xfail": 2, "skip": 1, "fail": 4, "timeout": 1, "killed": 0,
                  "incomplete": 0, "error": 2, "xpass": 0, "false_pass": 0},
    "by_category": {"design": 4, "execution": 1, "environment": 2},
    "duration_s": 2820.5
  },
  "jobs": [
    {
      "job_id": "burst__4417",
      "case": "burst",
      "seed": 4417,
      "status": "fail",
      "judged_status": "fail",
      "raw_status": "fail",
      "category": "design",
      "reason": "UVM_ERROR=1 > 0",
      "first_error": {"line": 812, "text": "UVM_ERROR verif/envs/axi/axi_scb.sv(120) @ 1234ns: …"},
      "signature": {"id": "3fa9c2e1", "text": "UVM_ERROR uvm_test_top.env.scb [AXI_SCB] mismatch: expect <HEX> …"},
      "uvm": {"info": 340, "warning": 2, "error": 1, "fatal": 0, "waived": {"error": 0, "warning": 2}},
      "waivers": [{"id": "AXI_SCB.LATE", "lines": 2}],
      "expect": [{"rule": "scoreboard AXI_SCB min 100", "ok": true, "value": 412}],
      "weak_signals": [],
      "known": ["burst#1"],
      "triage": {"task": "20261103-101500-a1b2/1", "verdict": "design_bug", "confidence": "high",
                 "review": "accepted", "stale": false, "bugs": ["burst#3"],
                 "doc": "docs/debug/20261103-101500-a1b2/1.md"},
      "flaky": false,
      "attempts": [
        {"try": 1, "status": "fail", "reason": "UVM_ERROR=1 > 0", "duration_s": 398.214,
         "host": "simbox01", "log": ".solouvm/runs/…/burst__4417/try1/sim.log", "wave": false}
      ],
      "repro": {
        "make": "make -f .solouvm/runs/20261103-101500-a1b2/run.mk TRY=r1 WAVE=1 run/burst__4417",
        "solouvm": "solouvm run --case burst --seed 4417 --wave"
      }
    }
  ],
  "signatures": [
    {"id": "3fa9c2e1", "text": "…", "count": 3, "cases": ["burst"], "example": "burst__4417"}
  ],
  "waivers": {"used": [{"id": "AXI_SCB.LATE", "lines": 14}], "unused": [], "expired": []},
  "hints": ["burst#2 can be marked verified (seed 12 passed)"]
}
```

| 字段 | 说明 |
|------|------|
| `partial` | `judge --partial` 产生的部分结果为 `true`（J-23） |
| `summary.passed` | 算通过的数量（pass + xfail + skip） |
| `jobs[].status` | 最终状态，已包含已接受的分诊结论 |
| `jobs[].judged_status` | 机械判定（含 expect_fail）的结果，不受分诊影响 |
| `jobs[].raw_status` | 应用 expect_fail 之前的结果 |
| `jobs[].triage` | 关联的分诊任务；没有时为 `null`。`stale` 为 `true` 表示机械结果已变，该结论不再生效（AI-20） |
| `jobs[].category` | `design` / `execution` / `environment` / `null`（通过时） |
| `jobs[].known` | 关联的未解决 bug（verdict.md §9） |
| `jobs[].attempts[].wave` | 是否为带波形重跑；为 `true` 的尝试不参与最终结果 |
| `hints` | 与 case 记录相关的提示（M-22） |

`results.txt` 与 JUnit XML 均由 `results.json` 派生，不单独定义。

## 7. `fast_state.json`

见 [fast-compile.md](fast-compile.md) §4.1。

## 8. JUnit XML 映射

| results.json | JUnit |
|--------------|-------|
| 一轮 | 一个 `<testsuite name="<project>">` |
| job | `<testcase classname="<case>" name="<job_id>" time="<duration_s>">` |
| fail / xpass / false_pass | `<failure message="<reason>">` + 首条错误；false_pass 附分诊摘要 |
| timeout / killed / incomplete / error | `<error message="<reason>">` |
| skip | `<skipped/>` |
| pass / xfail | 无子元素；xfail 在 `<system-out>` 中注明 |

## 9. `--json` 输出与错误码

所有命令的 `--json` 输出共用一个外壳（N-17）：

| 字段 | 说明 |
|------|------|
| `schema` | 外壳版本，当前为 1 |
| `command` | 执行的命令，如 `"triage plan"` |
| `ok` | 是否成功 |
| `data` | 命令自身的结果；结构见 `solouvm schema <命令>` |
| `truncated` | `null`，或 `{"omitted": N, "more": "<继续查看的命令>"}`（N-19） |
| `next` | 建议的下一步命令列表 |
| `error` | `ok=false` 时存在：`code`、`message`、`file`、`line`、`hint` |

流式命令（`regress` 的进度、`log -f`、`status --watch`）输出 JSON Lines，每行一个事件：`{"schema":1,"event":"job_done","job_id":"…","status":"pass",…}`，最后一行为与上表相同的外壳。

错误码稳定，只增不改（N-18）。首批：

| 错误码 | 退出码 | 含义 |
|--------|--------|------|
| `CFG_NOT_FOUND` | 2 | 找不到 `solouvm.conf.py` |
| `CFG_UNKNOWN_KEY` | 2 | 配置中有未知键 |
| `CFG_INVALID` | 2 | 配置值类型或取值不合法 |
| `CFG_LOAD_FAILED` | 2 | 配置文件执行时抛出异常 |
| `FILES_MISSING` | 2 | 文件清单条目匹配不到文件 |
| `CASE_INVALID` | 2 | case 定义不合法（重名、字段错误、ready 但缺验收信息） |
| `CASE_PLANNED` | 2 | 选中了 `stage="planned"` 的 case |
| `RECORD_INVALID` | 2 | 记录不合法（状态与日期不符、bug 重号） |
| `RECORD_CONFLICT` | 2 | 写回时发现文件在读与写之间被改动，已放弃 |
| `RECORD_WRITE_FAILED` | 2 | 写回后校验失败，已回滚 |
| `SELECT_EMPTY` | 2 | 选择条件匹配不到任何 job |
| `LIMIT_EXCEEDED` | 2 | job 数超过 `limits.max_jobs` |
| `ROLE_DENIED` | 2 | 当前角色不允许执行该命令 |
| `BUILD_FAILED` | 2 | 编译失败 |
| `PROVIDER_FAILED` | 2 | ssh、rsync、bsub 等外部命令失败 |
| `GIT_DIRTY` | 2 | 使用 git 同步但 A 的工作区有未提交改动 |
| `RUN_ACTIVE` | 2 | 回归仍在进行，不能直接 `judge`（J-23） |
| `RUN_NOT_FOUND` / `JOB_NOT_FOUND` / `TASK_NOT_FOUND` | 2 | 引用的 ID 不存在 |
| `TRIAGE_INVALID` | 2 | 分诊结论不合法（缺证据、与任务类型不相容） |
| `UVMGEN_EXISTS` | 2 | `new` 要生成的文件已存在 |
| `CHECK_UNAVAILABLE` | 2 | slang 或仿真器无法运行，`check` 无法完成 |
| `HOOK_FAILED` | 2 | hook 抛出异常 |
| `TESTS_FAILED` | 1 | 回归中有不算通过的结果 |
| `CHECK_FAILED` | 1 | `check` 发现代码错误 |
| `STYLE_VIOLATION` | 1 | `lint-style` 发现不符合规范的地方 |
| `TRIAGE_PENDING` | 1 | `triage status`：存在未审查的任务 |
| `INTERRUPTED` | 130 | 被中断 |

## 10. `triage/tasks.json`

```json
{
  "schema": 1,
  "run_id": "20261103-101500-a1b2",
  "policy": {"pass_policy": "changed", "group_by": "signature"},
  "tasks": [
    {
      "id": "20261103-101500-a1b2/1",
      "kind": "fail",
      "cases": ["burst", "burst_long"],
      "jobs": [
        {"job_id": "burst__4417", "judged_status": "fail"},
        {"job_id": "burst__88", "judged_status": "fail"},
        {"job_id": "burst_long__1290", "judged_status": "fail"}
      ],
      "signature": "3fa9c2e1",
      "reason": "UVM_ERROR=1 > 0",
      "stale": false,
      "result": {
        "verdict": "design_bug", "confidence": "high", "summary": "…",
        "evidence": ["log:812", "rtl/axi_slv.sv:120", "wave:1234ns tb_top.dut.addr_q"],
        "bugs": ["burst#3"], "doc": "docs/debug/20261103-101500-a1b2/1.md",
        "author": "triage:agent-7f2c", "at": "2026-11-03T11:20:41+08:00"
      },
      "review": {"decision": "accepted", "reason": null, "author": "main", "at": "…"}
    }
  ]
}
```

- `kind`：`fail`（失败类，按签名合并，可跨 case）或 `pass`（分诊是否虚假 pass）；
- `jobs[].judged_status`：生成任务时各 job 的机械结果；重新 `judge` 后有任何一个变化，`stale` 置为 `true`，结论不再生效（AI-20）；
- `result` 与 `review` 未写入时为 `null`；`result.author` 来自写入时的 `--author` 参数，未给出时为角色名；
- 同样的结论也以 `Triage` 记录写回任务涉及的每个 case（M-24），`tasks.json` 是本轮的索引，case 记录是长期存档；
- bug 登记在失败最多的那个 case 下，其他 case 的 Triage 记录引用它。

## 11. 调试文档

路径 `docs/debug/<run_id>/<任务序号>.md`，入库。按轮次存放，是因为一个分诊任务可能涉及多个 case；各 case 的 Triage 记录与 `doc` 导出都会链接到它。

`triage write --doc FILE`（或 `--doc -` 从标准输入读）校验以下章节存在且非空：

```markdown
## 现象
## 排查过程
## 证据
## 结论
## 建议
```

solouvm 写入时在文档开头补上标题与元信息（任务、涉及的 case、结论与置信度、关联 bug、作者、时间），子 agent 只写上面五个章节的正文。
