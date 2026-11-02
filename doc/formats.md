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
| `job.facts` | B（`run_job.sh`） | A | `runs/<run_id>/<job_id>/try<N>/`、`build/<mode>/` |
| `launch.pid` | B（`launch.sh`） | A | `runs/<run_id>/` |
| `plan.json` | A | A | `runs/<run_id>/` |
| `env.json` | A | A | `runs/<run_id>/` |
| `results.json` | A | A、CI、用户脚本 | `runs/<run_id>/` |
| `fast_state.json` | A | A | `build/<mode>/` |

## 2. `job.facts`

`key=value`，一行一项。值不含换行；同名键以最后一次出现为准。由 `run_job.sh` 分两段写入（Q1）。

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
env_fail=0
finished_at=2026-11-03T10:21:40+08:00
duration_s=398.214
```

### 2.3 字段

| 键 | 段 | 取值 | 说明 |
|----|----|------|------|
| `schema` | 开始 | `1` | |
| `generator` | 开始 | `solouvm <版本>` | 生成 `run_job.sh` 的版本（N-13） |
| `kind` | 开始 | `job` / `build` / `check` | |
| `run_id` | 开始 | | `kind=build` 时为空 |
| `job_id` | 开始 | | `kind=build` 时为 `build__<mode>` |
| `try` | 开始 | 正整数 | 第几次尝试 |
| `host` | 开始 | | `hostname` 输出 |
| `pid` | 开始 | | `run_job.sh` 自身的 PID |
| `timeout_impl` | 开始 | `timeout` / `gtimeout` / `builtin` | |
| `started_at` | 开始 | ISO 8601 | |
| `state` | 两段 | `running` / `done` / `interrupted` | |
| `rc` | 结束 | 整数 | 被信号终止时为 `128 + 信号号` |
| `signal` | 结束 | 信号名，如 `SIGSEGV`；无则为空 | 超时造成的 TERM / KILL 不写在这里 |
| `timed_out` | 结束 | `0` / `1` | |
| `hung` | 结束 | `0` / `1` | |
| `env_fail` | 结束 | `0` / `1` | 命中环境失败模式 |
| `env_fail_line` | 结束 | 行号 | 命中的第一行 |
| `finished_at` | 结束 | ISO 8601 | |
| `duration_s` | 结束 | 浮点数 | |

`run_job.sh` 写入的值都由它自己产生（时间、数字、主机名等），不含任何用户输入（N-12）。

### 2.4 A 端解析

```text
文件不存在              → 未运行
无 state 或 state=running → 未完成（incomplete）
run_id 与本轮不符         → 残留（error）
schema 不支持            → 报错（error）
其余                     → 交给判定规则（verdict.md §3）
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
    "shard": null, "include_wip": false
  },
  "builds": ["default"],
  "jobs": [
    {
      "job_id": "burst__4417",
      "case": "burst",
      "seed": 4417,
      "build": "default",
      "uvm_test": "burst_test",
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
  "summary": {
    "total": 120, "passed": 113,
    "by_status": {"pass": 110, "xfail": 2, "skip": 1, "fail": 4, "timeout": 1, "killed": 0,
                  "incomplete": 0, "error": 2, "xpass": 0},
    "by_category": {"design": 4, "execution": 1, "environment": 2},
    "duration_s": 2820.5
  },
  "jobs": [
    {
      "job_id": "burst__4417",
      "case": "burst",
      "seed": 4417,
      "status": "fail",
      "raw_status": "fail",
      "category": "design",
      "reason": "UVM_ERROR=1 > 0",
      "first_error": {"line": 812, "text": "UVM_ERROR tb/scb.sv(120) @ 1234ns: …"},
      "signature": {"id": "3fa9c2e1", "text": "UVM_ERROR uvm_test_top.env.scb [SCB] addr <HEX> …"},
      "uvm": {"info": 340, "warning": 2, "error": 1, "fatal": 0, "waived": {"error": 0, "warning": 2}},
      "waivers": [{"id": "AXI/OUTSTANDING", "lines": 2}],
      "known": ["burst#1"],
      "flaky": false,
      "attempts": [
        {"try": 1, "status": "fail", "reason": "UVM_ERROR=1 > 0", "duration_s": 398.214,
         "host": "simbox01", "log": ".solouvm/runs/…/burst__4417/try1/sim.log", "wave": false}
      ],
      "repro": {
        "make": "make WAVE=1 run/burst__4417",
        "solouvm": "solouvm rerun --run 20261103-101500-a1b2 --case burst --seed 4417 --wave"
      }
    }
  ],
  "signatures": [
    {"id": "3fa9c2e1", "text": "…", "count": 3, "cases": ["burst"], "example": "burst__4417"}
  ],
  "waivers": {"used": [{"id": "AXI/OUTSTANDING", "lines": 14}], "unused": [], "expired": []},
  "hints": ["burst#2 can be marked verified (seed 12 passed)"]
}
```

| 字段 | 说明 |
|------|------|
| `summary.passed` | 算通过的数量（pass + xfail + skip） |
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
| fail / xpass | `<failure message="<reason>">` + 首条错误 |
| timeout / killed / incomplete / error | `<error message="<reason>">` |
| skip | `<skipped/>` |
| pass / xfail | 无子元素；xfail 在 `<system-out>` 中注明 |
