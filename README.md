# solouvm

用 Python 生成 UVM 验证框架与 UVM Runner。**Python 是唯一配置语言，运行时零第三方依赖。**

```bash
solouvm gen       # 收集文件与 case，生成 filelist.f / plan.json / Makefile
solouvm build     # 编译
solouvm regress   # 编译 + 跑全部 case + 汇总
solouvm end       # 只汇总（幂等，可反复跑）
```

细节见 [doc/getting-started.md](doc/getting-started.md)，可运行示例见 [examples/demo](examples/demo)。

## 1. 状态

| 能力 | 状态 |
|------|------|
| 配置加载（`solouvm.conf.py` / `filelist.py` / `case.py`） | ✅ |
| 文件收集、`filelist.f` 生成 | ✅ |
| case × seed 计划（`plan.json`） | ✅ |
| Makefile 生成（`build` / `run/<case_id>` / `regress`） | ✅ |
| 单 case 执行器 `_exec`（超时、信号、日志重定向、`status.json`） | ✅ |
| 日志判定 `logcheck`（UVM / 非 UVM） | ✅ |
| 聚合 `end`、`results.json` / `results.txt`、退出码 | ✅ |
| `pre/post` hook（main / com / run / end） | ✅ |
| 生成 UVM 框架模板（gen 阶段） | ⬜ |
| 波形（失败自动带波形重跑） | ⬜ |
| 覆盖率（db 收集 / merge） | ⬜ |
| 分片、`rerun-failed`、junit / html 报告 | ⬜ |

决策与理由：[doc/design-decisions.md](doc/design-decisions.md)。

## 2. 设计原则

- **Python 是第一公民**：只支持 Python 配置，不解析 YAML / JSON / TOML。
- **零第三方依赖**：运行时只用标准库；`pytest` 仅用于本仓库自测。
- **声明式收集**：`filelist.py` 返回 `list`，`case.py` 返回 `list[Case]`，由 Runner 收集处理。
- **生成优先，执行可选**：生成物本身可以独立工作（`make`），托管执行只是便利层。
- **环境无关**：项目可整体复制到任意目录；契约文件只存相对路径，不要求安装为包。
- **可扩展**：每个阶段提供 `pre-hook` / `post-hook`。

要求 `Python >= 3.10`。

## 3. Runner 流程

```
main ─ com ─ run ─ user run ─ end
 │      │      │        │       └─ 聚合 status.json -> results.json + 退出码
 │      │      │        └───────── 用户自行提交/执行（或被 regress 托管）
 │      │      └────────────────── 收集 case，生成 Makefile 运行目标
 │      └───────────────────────── 收集文件，生成 filelist.f 与编译目标
 └──────────────────────────────── 解析参数、检查合法性
```

| README 阶段 | 对应命令 | 核心逻辑 |
|-------------|----------|----------|
| main | 所有命令 | `cli.py`：参数解析与校验 |
| com | `gen` / `build` | `collect.py` + `makefile.py`：filelist 与编译目标 |
| run | `gen` / `run` / `regress` | `plan.py` + `makefile.py`：plan 与运行目标 |
| user run | `make regress`（或自己的调度系统） | `executor.py`(`_exec`)：起进程、超时、判定、写 `status.json` |
| end | `end` | `status.py` + `report.py`：聚合、退出码 |

### 3.1 判定归属：判定贴着进程，`end` 只收账

一个 case 的结论取决于 `rc`、信号、是否超时，这些只有**启动进程的人**知道。因此判定发生在 `_exec` 里，写进 `status.json`；`end` **从不读原始日志**，只聚合 `status.json`。

这带来两个直接后果：`end` 幂等、可重复执行；超时不再需要"等一个不会到来的日志文件"。

规则顺序（固定，`pass_regex` 只能救"缺 UVM Report Summary"这一条启发式）：

```
signal(killed) → timeout → rc!=0 → fail_regex → UVM_ERROR/FATAL/WARNING 超上限
→ UVM 日志缺 Summary → 非 UVM 缺 SOLOUVM_DONE → pass
```

### 3.2 执行边界：L1 只生成，L2 托管执行

| 层 | 谁起进程 | 怎么用 |
|----|----------|--------|
| L1 | 用户自己 | `solouvm gen` 之后 `make build` / `make regress`，再由 `solouvm end` 收账 |
| L2 | solouvm 调 `make` | `solouvm regress`（内部仍是 make + `_exec`） |

两条路产出**同样的** `status.json`。solouvm 不规定任务提交方式，也不做 license 排队或调度器适配。

## 4. 用户配置接口

| 文件 | 内容 | 形式 |
|------|------|------|
| `solouvm.conf.py` | `CFG = {...}` | 项目级配置（simulator、jobs、timeout、checks、hook 之外的一切） |
| `filelist.py` | `FILES = [...]` | 编译文件（`+incdir+` / `+define+` 来自 `CFG`；支持 glob） |
| `case.py` | `CASES = [...]` | `list[solouvm.Case]` |

```python
# case.py
from solouvm import Case

CASES = [
    Case(name="smoke", uvm_test="smoke_test"),
    Case(name="num", uvm_test="num_test", params={"NUM": 32}, tags=("nightly",)),
    Case(name="known_bug", uvm_test="num_test", expect_fail=True),
]
```

> 注意：配置文件名是 `solouvm.conf.py`，**不能叫 `solouvm.py`**——同名文件会遮蔽库本身，`python -m solouvm` 会静默导入配置字典。原因见 [design-decisions.md](doc/design-decisions.md) D9。

## 5. Hook

```python
# pre_hook_run.py
def hook(ctx):        # ctx: stage/when/command/root/workdir/project/verbose[+plan]
    ...
```

- 阶段：`main` / `com` / `run` / `end`，各有 `pre` 与 `post`；
- 返回值被忽略；**异常中止命令并返回退出码 2**（hook 属于构建的一部分，不能静默降级）；
- `--no-hooks` 可在 CI 中隔离用户逻辑。

## 6. 契约文件

```
filelist.f  Makefile
.solouvm/plan.json                      # 本次 case × seed（唯一事实来源，schema 1）
.solouvm/out/<case_id>/sim.log          # stdout+stderr，由 _exec 重定向
.solouvm/out/<case_id>/status.json      # 单 case 判定（schema 1）
.solouvm/results.json  results.txt      # 聚合结果
```

`_exec` 只读 `plan.json`，不加载配置、不 import case 代码：配置漂移不影响已生成的结果。

## 7. 退出码

| 码 | 含义 |
|----|------|
| 0 | 全部通过（`xfail` 记通过） |
| 1 | 至少一个 `fail` / `timeout` / `error` / `killed` / `xpass` |
| 2 | 配置或环境错误 |
| 130 | Ctrl-C |

## 8. 文档与示例

| 文档 | 内容 |
|------|------|
| [doc/getting-started.md](doc/getting-started.md) | 快速开始、命令表、生成物、判定规则、hook |
| [doc/design-decisions.md](doc/design-decisions.md) | 每条设计决策的现状 / 理由 / 代价，以及未决策项 |
| [examples/demo](examples/demo) | 可直接跑通的最小工程（内置假仿真器） |

## 9. 开发

```bash
uv sync
uv run pytest                                     # 118 项，含假仿真器驱动的全流程测试
uv run pytest --cov=solouvm                       # 目前 91%
uv run ruff check .
uv run solouvm -c examples/demo/solouvm.conf.py regress
```

## 10. 许可证

MIT，见 [LICENSE](LICENSE)。
