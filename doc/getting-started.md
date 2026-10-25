# 快速开始

目标：从一个空目录走到"有一个 case 通过了"，并且知道每一步生成了什么。

## 1. 准备

不需要安装。三种方式任选：

```bash
# uv（推荐，也用于跑测试）
uv sync

# 或直接用仓库里的包
PYTHONPATH=/path/to/solouvm python3 -m solouvm --version

# 或 pip 安装
pip install -e .
```

要求 Python >= 3.10，运行时**零第三方依赖**。

## 2. 最小项目

```
myproj/
  solouvm.conf.py     # 项目配置（CFG）
  filelist.py         # 编译文件清单（FILES）
  case.py             # 测试 case（CASES）
  rtl/dut.sv
  tb/tb_top.sv
```

`solouvm.conf.py`

```python
CFG = {
    "project": "myproj",
    "simulator": "vcs",              # 内置：vcs / xrun / iverilog / echo
    "top": "tb_top",
    "incdirs": ["tb"],
    "defines": ["DEBUG"],
    "regress": {"seeds": [1, 2], "retry": 0, "jobs": 8},
    "run": {"timeout": "20m"},
    "checks": {"max_uvm_error": 0, "fail_regex": ["TEST FAILED"]},
}
```

`filelist.py`（支持 glob，路径相对项目根）

```python
FILES = ["rtl/*.sv", "tb/*.sv"]
```

`case.py`

```python
from solouvm import Case

CASES = [
    Case(name="smoke", uvm_test="smoke_test"),
    Case(name="regress_num", uvm_test="num_test", params={"NUM": 32}, tags=("nightly",)),
    Case(name="known_bug", uvm_test="num_test", params={"NUM": 7}, expect_fail=True),
]
```

## 3. 跑起来

```bash
solouvm gen          # 收集 + 生成 filelist.f / plan.json / Makefile
solouvm build        # 编译
solouvm regress      # 编译 + 跑全部 case + 汇总
solouvm end          # 只汇总（幂等，可反复跑）
```

不用 solouvm 托管执行时（L1 模式）：

```bash
solouvm gen
make build           # 交给自己的调度系统
make regress         # 或 make run/smoke__1
solouvm end --allow-missing
```

## 4. 生成物长什么样

```
filelist.f
Makefile                              # include .solouvm/Makefile
.solouvm/
  plan.json
  Makefile
  out/smoke__1/sim.log
  out/smoke__1/status.json
  results.json
  results.txt
```

`status.json`（节选）

```json
{
  "schema": 1,
  "run_id": "20261025-223001-9f3ac1",
  "case_id": "smoke__1",
  "case": "smoke",
  "seed": 1,
  "status": "pass",
  "raw_status": "pass",
  "verdict": null,
  "rc": 0,
  "signal": null,
  "timeout": false,
  "uvm": {"error": 0, "fatal": 0, "warning": 2, "info": 340},
  "checks_fired": [],
  "reason": "UVM report summary clean",
  "attempts": [{"n": 1, "status": "pass", "rc": 0, "duration_s": 12.4}],
  "cmd": ["/abs/.solouvm/simv", "+UVM_TESTNAME=smoke_test", "+SOLOUVM_SEED=1", "..."],
  "log": "sim.log"
}
```

## 5. 命令与退出码

| 命令 | 作用 |
|------|------|
| `solouvm gen` | 收集 + 生成所有产物，不编译 |
| `solouvm build` | `gen` + `make build` |
| `solouvm run --case NAME` | `gen` + 只跑指定 case；只汇总这些 case |
| `solouvm regress` | `gen` + 跑 plan 里全部 case + 汇总 |
| `solouvm end [--allow-missing]` | 只汇总 |
| `solouvm clean` | 删除 `out/` 与 `results.*` |
| `solouvm _exec` | 内部命令，由生成的 Makefile 调用 |

通用参数：`-c PATH`、`-v`（把仿真输出透到终端）、`-q`、`--no-hooks`、`--seed/--seeds`（`gen`/`run`/`regress`）、`-j N`。

| 退出码 | 含义 |
|--------|------|
| 0 | 全部通过（`xfail` 也算通过） |
| 1 | 至少一个 `fail/timeout/error/killed/xpass` |
| 2 | 配置或环境错误（没跑出可判定的结果） |
| 130 | 被 Ctrl-C 中断 |

## 6. Hook

```python
# pre_hook_run.py
def hook(ctx):
    print("planning for", ctx["root"])
```

```python
# post_hook_end.py
def hook(ctx):
    ...   # 推 dashboard、发邮件、拷波形
```

可用阶段：`main` / `com` / `run` / `end`，每个都有 `pre` 与 `post`。hook 抛异常 → 命令以退出码 2 中止。详见 [design-decisions.md](design-decisions.md) D10。

## 7. 判定规则

判定发生在 `_exec` 里（因为它才知道 rc/信号/超时），顺序固定：

```
signal → killed
timeout → timeout
rc != 0 → fail
fail_regex → fail
UVM_ERROR/FATAL/WARNING 超上限 → fail
UVM 日志缺 Report Summary 且 pass_regex 未命中 → fail
非 UVM 缺 SOLOUVM_DONE → fail
```

`pass_regex` 只能救"缺 Summary"这一条启发式，不能覆盖硬失败。完整表见 [design-decisions.md](design-decisions.md) D6。

## 8. 非 UVM 项目

没有 UVM report 的仿真（纯 SV testbench、Verilator、C 模型）在自己最后一行打印：

```
SOLOUVM_DONE status=pass
```

否则判定为 fail——"仿真提前死掉"比"忘了打标记"更常见。

## 9. 跑仓库自带的测试

```bash
uv run pytest
```

测试用 `tests/fake_sim.py` 假仿真器覆盖 pass / UVM_ERROR / UVM_FATAL / 缺 Summary / 非零 rc / 挂死 / 段错误 / 非 UVM 打点，不需要装任何 EDA 工具。
