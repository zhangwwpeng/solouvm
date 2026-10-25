# 设计决策记录

本文件记录**已经落到代码里的**决策，以及每条决策的理由。README 只保留结论，细节指向这里。

格式：`D<n> 决策` —— 现状 / 理由 / 代价。

---

## D1 配置入口：三个文件，而不是一个大字典

**现状**

| 文件 | 内容 | 的形式 |
|------|------|--------|
| `solouvm.conf.py` | `CFG = {...}` 项目级配置 | 模块级字典 |
| `filelist.py` | `FILES = [...]` 编译文件 | 模块级 list |
| `case.py` | `CASES = [...]` 测试 case | `list[solouvm.Case]` |

三者都支持"模块级变量"或"同名函数"两种写法（`FILES` / `files()`）。

**理由**

- README 的原意是"用户写的 Python 文件返回 list"——`filelist.py` 与 `case.py` 保留了这个接口，收集逻辑不侵入用户的声明。
- 但项目级参数（simulator、jobs、timeout、checks）不属于任何一个文件，硬塞进 `case.py` 会让"改名一次要动三个文件"。所以单独一个 `CFG`。
- 只加载这三个文件、且只认 Python：没有 YAML/JSON/TOML 解析器，也没有"多格式合并"的例外路径。

**代价**：用户要知道三个文件名。用 CLI 报错 + 快速开始覆盖。

---

## D2 判定归属：`_exec` 判定，`end` 只聚合

**现状**：`status.json` 由 `_exec` 写；`solouvm end` 只读 `status.json`，**从不读原始仿真日志**。

**理由**：这是 README 早期版本（"`end` 解析仿真日志判结果"）最贵的一个坑。真正决定一个 case 结果的信息里，有一半只有"启动进程的人"拿得到：

| 信息 | 唯一来源 |
|------|----------|
| `rc` | `wait()` 的返回值 |
| 被信号杀死(KILL/SEGV) | `wait()` 的负返回值 |
| 超时 | 谁设的 `wait(timeout)` |
| 日志是否被截断/是否还没写完 | 谁重定向的 stdout |

尤其是**超时**：挂死的仿真永远不产出日志，一个"只读日志"的 `end` 会永久等一个不会到来的文件。所以判定必须贴着进程做完。

`end` 因此是幂等的聚合器，可以反复跑、可以只跑一部分、可以在集群上分片后合并。

---

## D3 契约文件（版本化 JSON）

```
<root>/
  filelist.f                        # 编译文件清单（含 +incdir+ / +define+）
  Makefile                          # 若不存在则生成：include .solouvm/Makefile
  .solouvm/
    plan.json                       # 本次要跑的 case × seed（唯一事实来源）
    Makefile                        # build / run/<case_id> / regress / clean
    out/<case_id>/sim.log           # stdout+stderr（_exec 重定向，不依赖仿真器 -l）
    out/<case_id>/status.json       # 单 case 判定结果
    results.json  results.txt       # 聚合结果
```

- 三个 JSON 都有 `"schema": 1`；`plan.json` 的 schema 不匹配时 `_exec` 直接报错而不是猜。
- `plan.json` 存相对项目根的路径，因此整个项目可以整体搬移。
- `_exec` **不加载配置**：所有执行期信息（cmd、timeout、params、checks）都从 plan 读，配置漂移不影响已生成的结果。

---

## D4 执行边界：L1 只生成，L2 `regress` 内置执行

**现状**

| 层 | 命令 | 谁真正起进程 |
|----|------|--------------|
| L1 | `solouvm gen` | 用户自己 `make build` / `make regress` |
| L2 | `solouvm build` / `run` / `regress` | solouvm 调 `make`，`make` 调 `_exec` |

**理由**：README 的"只生成，不执行"是集群无关性的来源，但 D2 又要求有人负责超时与判定。解法是把这个角色做成**生成物**：每个 case 一个 `make run/<case_id>` 目标，目标里调用 `solouvm _exec`。于是：

- 不想被托管的人：`gen` 完把 Makefile 交给自己的调度系统，`solouvm end` 只负责收账；
- 想省事的人：`regress` 帮你 `make -j`。

两条路产出**同样的** `status.json`。solouvm 不做 license 排队、不认任何调度器。

**代价**：`_exec` 必须保持极简、无副作用、可被任意调度器重复调用（这也是它不加载配置的原因）。

---

## D5 一个 Makefile，而不是"编译 Makefile + 运行 Makefile"

README 2.3/2.4 写的是两个 Makefile。实现为一个：`.solouvm/Makefile` 同时含 `build` 与 `run/<case_id>`、`regress`。

**理由**：两个文件必须互相 include 才能表达"运行依赖编译"，`-j` 下的目标顺序会变得脆弱；一个文件里 `run/%: build` 就是普通依赖关系。逻辑上的阶段划分仍在（`gen` 里 com 段写 filelist、run 段写 plan+Makefile，hook 也按阶段触发）。

**代价**：与早期文档措辞不一致，已在本文件登记。

---

## D6 判定规则：固定优先级，`pass_regex` 只能救一条

```
1. signal              -> killed
2. timeout             -> timeout
3. rc != 0             -> fail
4. fail_regex 命中      -> fail
5. UVM_ERROR > 上限     -> fail
6. UVM_FATAL > 上限     -> fail
7. UVM_WARNING > 上限   -> fail（仅当配置为整数时生效）
8. UVM 日志但无 Report Summary，且 pass_regex 未命中 -> fail
9. 非 UVM：SOLOUVM_DONE status=fail                   -> fail
10. 非 UVM：没有 SOLOUVM_DONE                          -> fail
11. 其余                                               -> pass
```

- `pass_regex` 只作用于第 8 条（"没有 Summary"这个**启发式**），不能覆盖 1–7 的硬失败。
- `fail_regex` 优先于 `pass_regex`。
- 自己的超时 kill 会产生负 rc（SIGTERM），此时**不算 signal**，仍判 `timeout`：否则"超时"会被误报成"被信号杀死"。
- UVM 判定用 `\bUVM_(INFO|ERROR|FATAL|WARNING)\b`，不能简单判 `"UVM" in line`——`SOLOUVM_DONE` 里就含 `UVM`。
- 计数优先用最后一次 `UVM_ERROR : N` summary；没有 summary 时退化为 `UVM_ERROR @` 行数。
- 非 UVM 环境（纯 SV/Verilator 等）用 `SOLOUVM_DONE status=pass|fail` 打点；没有标记一律 fail（"仿真提前死掉"比"没写标记"更常见）。

---

## D7 `retry` 与 `expect_fail` 都在 `_exec`，不在 `end`

- `retry`：需要**立即**知道判定才能同 seed 重跑，所以只能在执行层；`attempts[]` 全量记录，`duration_s` 是各次之和。
- `expect_fail`：在判定之后做**状态映射**（`fail -> xfail`、`pass -> xpass`），`logcheck` 本身不认识 `expect_fail`，便于单测。
- 聚合退出码把 `xpass` 算作失败（应该失败的 case 通过了，说明测试失效），`xfail` 算通过。

---

## D8 `end` 的幂等与完整性

- 只统计 `plan.json` 里的 case，**不 glob `out/*`**，避免混入历史结果；
- `run_id` 不匹配 → `error`（陈旧状态）；`pending` → `error`（写了占位但没跑完）；
- 缺失 → `error`；`--allow-missing` 才降级为 `skip`（分片/部分运行）；
- 重复执行 `end` 结果不变（除 `finished_at`），`results.json` 原子写入。

---

## D9 配置文件不能叫 `solouvm.py`

**现状**：配置文件名是 `solouvm.conf.py`。

**理由**：这是实测踩到的坑。项目根放 `solouvm.py` 后，在项目根执行 `python -m solouvm` 时 `sys.path[0]` 是当前目录，**配置文件会遮蔽同名包**，`import solouvm` 拿到的是配置字典，整个库静默失效（表现为"命令无输出、退出码 0"）。改名后不再有同名冲突，且不依赖"安装与否"。

**代价**：早期文档里的 `solouvm.py` 一律读作 `solouvm.conf.py`。

---

## D10 Hook：签名小、语义硬

```
文件：<root>/{pre,post}_hook_{main,com,run,end}.py
函数：def hook(ctx): ...
ctx ：{"stage", "when", "command", "root", "workdir", "project", "verbose"[, "plan"]}
返回：忽略
异常：中止命令，退出码 2
```

- 阶段：`main`（所有命令外层）、`com`（filelist/编译产物）、`run`（plan/运行产物）、`end`（聚合）。
- 没有 `hook` 可调用对象、或 hook 抛异常，都终止流程：hook 属于构建的一部分，坏 hook 不能静默降级成"跳过"。
- `post_hook_main` 在异常路径上也会执行（`finally`），便于清理；`pre_hook_*` 报错则后续阶段不执行。
- `--no-hooks` 用于 CI 里隔离用户逻辑。
- `ctx["plan"]` 只在 `post_hook_run` 及之后可用。

---

## 尚未决策 / 未实现

| 项 | 状态 |
|----|------|
| `gen`（生成 UVM 框架模板/目录骨架） | 未实现；README 的"生成 UVM 验证框架"目前没有代码 |
| `waves`（失败自动带波形重跑） | 未实现 |
| `coverage`（db 收集/merge） | 未实现 |
| 分片（`--shard i/n`） | 未实现；`plan.json` 已具备前提（同一次抽种子） |
| junit / html 报告 | 未实现（仅 text + json） |
| `rerun-failed` / `resume` | 未实现 |
| 编译失败的错误分类（`BuildError` → 退出码 2） | 目前编译失败只透出 make 的退出码 |
