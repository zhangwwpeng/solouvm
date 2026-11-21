# 02 五分钟快速上手

下面使用内置 `fake` 仿真器，在空目录完成从建工程到查看分诊任务的完整闭环（以下命令均经实际执行验证）。

## 1. 初始化项目

```bash
mkdir demo_uvm && cd demo_uvm
solouvm init
```

实测输出：

```text
initialized fake project (42 files)
next: solouvm validate
next: solouvm regress
```

`init` 会创建：
- `solouvm.conf.py`、`filelist.py`、`case.py`、`tbspec.py`、`Makefile`、`.gitignore`
- `rtl/axi_slv_dummy.sv`、`verif/...` 下整套 Style 1 AXI UVM 示例环境与 `doc/verif/system.md`

> 提示：若只想生成最小配置文件而不带示例 RTL/UVM，可加 `--bare`。默认 `--simulator fake`；实际工程可改为 `--simulator vcs` / `xrun` / `questa` 等。

## 2. 校验与预览执行计划

```bash
solouvm validate
solouvm list
```

实测输出：

```text
0 errors, 0 warnings
JOB               CASE   SEED       BUILD    TAGS  STAGE
smoke__299002437  smoke  299002437  default        wip
1 jobs from 1 cases
```

## 3. 运行回归

```bash
solouvm regress --seed 1
```

实测输出：

```text
[20261118-223739-892e] smoke__1 try1 finished (rc=0)
Run: 20261118-223739-892e  (judged 2026-11-18T22:37:39+08:00)
Summary: 1/1 passed (100.0%)  duration=0.014s
Status: pass=1  xfail=0  skip=0  fail=0  timeout=0  killed=0  incomplete=0  error=0  xpass=0  false_pass=0
Categories: design=0  execution=0  environment=0

Slowest jobs (top 1):
  - smoke__1: 0.014s (pass)
```

产物全部落在 `.solouvm/`（默认已加入 `.gitignore`）：
- `.solouvm/runs/<run_id>/`：`plan.json`、`env.json`、`run.mk`、`results.json`、`results.txt`、`junit.xml`
- `.solouvm/runs/latest`：指向最近一轮的符号链接
- `.solouvm/runs/<run_id>/<job_id>/try1/`：`sim.log`、`job.facts`

## 4. 失败排查与 AI/人工分诊

```bash
solouvm explain smoke__1
solouvm triage plan
solouvm triage status
solouvm progress
```

`explain` 一次性输出 8 节排错上下文（状态、首错日志、源码片段、Expect 检查、失败签名历史、关联 Bug、上次通过至今 Git 提交、复现命令）；`triage plan` 因示例 `smoke` 处于 `stage="wip"`，会自动生成一个 pass 检查任务供确认是否真实通过；`progress` 给出各阶段 case 数与最近一轮通过率。

## 5. 机器可读模式（`--json`）

任意命令加上 `--json` 即输出固定 6 键外壳（`schema`, `command`, `ok`, `data`, `truncated`, `next`）；`regress --json` 等长耗时命令会先逐行输出 `{"schema":1,"event":"progress"|"job_done",...}` 事件，最后输出完整 JSON 结果外壳：

```json
{
  "schema": 1,
  "command": "version",
  "ok": true,
  "data": { "version": "0.1.0" },
  "truncated": null,
  "next": []
}
```
