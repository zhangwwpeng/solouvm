# solouvm

面向 SystemVerilog / UVM 验证的 **Python Runner**：用 Python 声明文件与 case，生成 `filelist.f` / `plan.json` / `Makefile`，执行仿真、判定结果、汇总报告。

**Python 是唯一配置语言，运行时零第三方依赖。** UVM 验证框架（目录骨架 / 模板）生成在规划中，见 [§9 路线图](#9-状态与路线图)。

```bash
solouvm gen       # 收集文件与 case，生成 filelist.f / plan.json / Makefile（不编译、不执行）
solouvm build     # gen + 编译
solouvm regress   # gen + 编译 + 跑全部 case + 汇总
solouvm end       # 只汇总（幂等，可反复跑）
```

上手见 [doc/getting-started.md](doc/getting-started.md)，可直接跑通的示例见 [examples/demo](examples/demo)（内置假仿真器，不需要 EDA 工具）。

## 1. 定位：做什么，不做什么

| 做                                                       | 不做                                     |
|----------------------------------------------------------|------------------------------------------|
| 从 Python 声明收集编译文件与 case                        | 解析 YAML / JSON / TOML 配置             |
| 生成可独立使用的 `filelist.f` 与 `Makefile`              | 任务提交、license 排队、调度器适配       |
| 单 case 执行：超时、信号、日志重定向、retry、判定        | 替代仿真器自身的功能（编译选项、波形等） |
| 聚合结果、给出确定的退出码                               | 读原始日志做聚合（聚合只读 `status.json`）|
| 每个阶段的 `pre` / `post` hook                           |                                          |

## 2. 设计原则

- **Python 是第一公民**：配置就是 Python 模块，只认 Python。
- **零第三方依赖**：运行时只用标准库；`pytest` / `ruff` 仅用于本仓库自测。
- **声明式收集**：`filelist.py` 给出 `list`，`case.py` 给出 `list[Case]`，收集与处理由 Runner 负责。
- **生成优先，执行可选**：生成物（`Makefile`）脱离 solouvm 也能用；托管执行只是便利层。
- **判定贴着进程**：谁起进程谁判定，聚合只收账（见 §3.2）。
- **环境无关**：契约文件只存相对路径，项目可整体搬移；不要求把 solouvm 安装为包。
- **可扩展**：每个阶段提供 `pre` / `post` hook，hook 失败即中止，不静默降级。

## 3. 核心模型

### 3.1 流程阶段

```text
main ─ com ─ run ─ exec ─ end
 │      │     │     │      └─ 聚合 status.json -> results.json / results.txt + 退出码
 │      │     │     └──────── 每个 case 一次 _exec：起进程、超时、判定、写 status.json
 │      │     └────────────── 收集 case × seed，生成 plan.json 与运行目标
 │      └──────────────────── 收集文件，生成 filelist.f 与编译目标
 └─────────────────────────── 解析参数、加载配置、检查合法性
```

| 阶段 | 产物 / 职责                                   | 实现                           | 有 hook |
|------|-----------------------------------------------|--------------------------------|---------|
| main | 参数与配置校验，包住整个命令                  | `cli.py` / `config.py`         | ✅      |
| com  | `filelist.f`、编译目标                        | `collect.py` / `makefile.py`   | ✅      |
| run  | `plan.json`、`run/<case_id>` / `regress` 目标 | `plan.py` / `makefile.py`      | ✅      |
| exec | `out/<case_id>/sim.log` + `status.json`       | `executor.py`（`solouvm _exec`）| —       |
| end  | `results.json` / `results.txt`、退出码        | `status.py` / `report.py`      | ✅      |

`gen` / `build` / `run` / `regress` 都会先走 main → com → run；exec 由生成的 `Makefile` 调用，不经过 hook。

### 3.2 判定归属：`_exec` 判定，`end` 只收账

一个 case 的结论取决于 `rc`、信号、是否超时，这些只有**启动进程的人**知道。所以判定在 `_exec` 里完成并写入 `status.json`；`end` **从不读原始日志**，只聚合 `status.json`。由此：

- `end` 幂等，可反复执行、可只汇总部分 case；
- 挂死的仿真由 `_exec` 超时杀掉并判 `timeout`，不存在"等一个永远不来的日志"。

判定顺序固定，先命中者为准：

```text
被信号杀死 → killed
超时        → timeout
rc != 0     → fail
fail_regex  → fail
UVM_ERROR / UVM_FATAL / UVM_WARNING 超上限 → fail   （WARNING 仅在配置了上限时生效）
UVM 日志缺 Report Summary，且 pass_regex 未命中 → fail
非 UVM：SOLOUVM_DONE status=fail 或缺失该标记 → fail
其余 → pass
```

`pass_regex` 只能救"缺 Report Summary"这一条启发式，不能覆盖任何硬失败。判定之后再应用 `expect_fail`：`fail → xfail`（算通过），`pass → xpass`（算失败）。

### 3.3 执行边界：L1 只生成，L2 托管执行

| 层 | 命令                                | 谁起进程                                    |
|----|-------------------------------------|---------------------------------------------|
| L1 | `solouvm gen`，之后 `solouvm end`   | 用户：`make build` / `make regress` / 自己的调度系统 |
| L2 | `solouvm build` / `run` / `regress` | solouvm 调 `make`，`make` 调 `_exec`        |

两条路产出**同样的** `status.json`。`_exec` 只读 `plan.json`，不加载配置、不 import 用户代码，因此可以被任意调度器在任意目录重复调用。

## 4. 用户接口

### 4.1 三个配置文件（放在项目根）

| 文件              | 导出            | 内容                                                         |
|-------------------|-----------------|--------------------------------------------------------------|
| `solouvm.conf.py` | `CFG = {...}`   | 项目级配置：simulator、top、incdirs、defines、seeds、jobs、timeout、checks |
| `filelist.py`     | `FILES = [...]` | 编译文件，路径相对项目根，支持 glob                          |
| `case.py`         | `CASES = [...]` | `list[solouvm.Case]`                                         |

`FILES` / `CASES` 也可以写成同名函数（`files()` / `cases()`）。

```python
# case.py
from solouvm import Case

CASES = [
    Case(name="smoke", uvm_test="smoke_test"),
    Case(name="num", uvm_test="num_test", params={"NUM": 32}, tags=("nightly",)),
    Case(name="known_bug", uvm_test="num_test", expect_fail=True),
]
```

> 配置文件必须叫 `solouvm.conf.py`，**不能叫 `solouvm.py`**：同名文件会遮蔽库本身，`python -m solouvm` 会静默导入配置而不是 solouvm。见 [design-decisions.md](doc/design-decisions.md) D9。

### 4.2 Hook

```python
# <项目根>/pre_hook_run.py      文件名：{pre,post}_hook_{main,com,run,end}.py
def hook(ctx):  # ctx: stage / when / command / root / workdir / project / verbose [/ plan]
    ...
```

- 返回值忽略；**抛异常 → 命令中止，退出码 2**；
- `post_hook_main` 在异常路径上也会执行，可用于清理；
- `ctx["plan"]` 从 `post_hook_run` 起可用；
- `--no-hooks` 跳过所有 hook（CI 中隔离用户逻辑）。

## 5. 命令

| 命令                                  | 作用                                         |
|---------------------------------------|----------------------------------------------|
| `solouvm gen`                         | 收集并生成全部产物，不编译、不执行           |
| `solouvm build`                       | `gen` + `make build`                         |
| `solouvm run --case NAME [--case …]`  | `gen` + 只跑指定 case + 汇总这些 case        |
| `solouvm regress`                     | `gen` + 跑 plan 中全部 case + 汇总           |
| `solouvm end [--allow-missing]`       | 只汇总；`--allow-missing` 把未跑的 case 记为 `skip` |
| `solouvm clean`                       | 删除运行目录与结果文件                       |
| `solouvm _exec`                       | 内部命令，由生成的 `Makefile` 调用           |

常用参数：`-c PATH`（指定配置）、`-v` / `-q`、`--no-hooks`、`--seed N`（可重复）/ `--seeds 1-5,42`、`-j N`。

## 6. 契约文件

```text
<root>/
  filelist.f                          # 编译文件（含 +incdir+ / +define+）
  Makefile                            # 不存在时生成，只有一行 include .solouvm/Makefile
  .solouvm/
    Makefile                          # build / run/<case_id> / regress / clean
    plan.json                         # 本次 case × seed，执行期唯一事实来源
    out/<case_id>/sim.log             # stdout + stderr，由 _exec 重定向
    out/<case_id>/status.json         # 单 case 判定
    results.json  results.txt         # 聚合结果
```

- `case_id = <case>__<seed>`，同时是目录名和 make 目标名；
- 三个 JSON 都带 `"schema": 1`，版本不符直接报错，不猜；
- `end` 只统计 `plan.json` 中的 case，不扫描 `out/*`，不会混入历史结果。

## 7. 退出码

| 码  | 含义                                                       |
|-----|------------------------------------------------------------|
| 0   | 全部通过（`xfail` 计为通过）                               |
| 1   | 至少一个 `fail` / `timeout` / `error` / `killed` / `xpass` |
| 2   | 配置、环境或 hook 错误（没有产出可判定的结果）             |
| 130 | Ctrl-C 中断                                                |

> 已知例外：编译失败目前直接透传 `make` 的退出码，计划归入 2。

## 8. 环境要求

- Python >= 3.10，Linux / macOS（进程组超时清理依赖 POSIX）；
- 无需安装：`uv run solouvm …`、`python -m solouvm …`（`PYTHONPATH` 指向仓库）或 `pip install -e .` 任选；
- 内置仿真器：`vcs` / `xrun` / `iverilog`，以及调试用的 `echo`；其他仿真器通过 `CFG["sim"]` 自定义编译与运行命令。

## 9. 状态与路线图

| 能力                                                       | 状态 |
|------------------------------------------------------------|------|
| 配置加载（`solouvm.conf.py` / `filelist.py` / `case.py`）  | ✅   |
| 文件收集、`filelist.f` 生成                                | ✅   |
| case × seed 计划（`plan.json`）                            | ✅   |
| Makefile 生成（`build` / `run/<case_id>` / `regress`）     | ✅   |
| 单 case 执行器 `_exec`（超时、信号、日志重定向、retry）    | ✅   |
| 日志判定（UVM / 非 UVM）、`expect_fail`                    | ✅   |
| 聚合 `end`、`results.json` / `results.txt`、退出码         | ✅   |
| `pre` / `post` hook（main / com / run / end）              | ✅   |
| 编译失败归类为退出码 2                                     | ⬜   |
| UVM 验证框架骨架生成（命令名待定）                         | ⬜   |
| 失败自动带波形重跑                                         | ⬜   |
| 覆盖率 db 收集 / merge                                     | ⬜   |
| 分片 `--shard i/n`、重跑失败 case                          | ⬜   |
| junit / html 报告                                          | ⬜   |

## 10. 文档

| 文档                                               | 内容                                         |
|----------------------------------------------------|----------------------------------------------|
| [doc/getting-started.md](doc/getting-started.md)   | 从空目录到第一个 case 通过；配置示例、生成物 |
| [doc/design-decisions.md](doc/design-decisions.md) | 每条设计决策的现状 / 理由 / 代价，未决事项   |
| [doc/python-craft.md](doc/python-craft.md)         | 这份代码的 Python 写法思路与练习             |
| [examples/demo](examples/demo)                     | 最小可运行工程（假仿真器）                   |

README 是总纲：只写结论与边界。细节以 `doc/` 为准，二者冲突时修 `doc/` 或修 README，不留两套说法。

## 11. 开发

```bash
uv sync
uv run pytest                 # 含假仿真器驱动的全流程测试，不需要 EDA 工具
uv run pytest --cov=solouvm
uv run ruff check .
uv run solouvm -c examples/demo/solouvm.conf.py regress
```

## 12. 许可证

MIT，见 [LICENSE](LICENSE)。
