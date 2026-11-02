# AI 协作流程

solouvm 由人和 AI 共用同一套命令（N-20）。本文描述以 AI 为主力时的标准工作流：主 agent 写 case、提交回归；子 agent 独立分诊；主 agent 审查后决定下一步。需求编号见 [requirements.md](requirements.md) 的 AI 区。

> 状态：设计稿，尚无代码。本文不绑定具体的 agent 框架；附录给出 Claude Code 的示例配置。

## 1. 角色

| 角色 | 由谁担任 | 职责 | 写权限 |
|------|----------|------|--------|
| 主 agent | AI 或人 | 写 case 与 tb、提交回归、审查分诊结论、修改 case、签核 | 全部 |
| 子 agent（分诊） | AI，每个分诊任务一个 | 判断结果是否可信、定位失败原因、登记 bug、写调试文档 | 只能经 solouvm 命令追加记录与调试文档 |
| solouvm | 工具 | 执行、机械判定、组织上下文、强制写权限边界 | — |

核心原则是**作者与审查分离**：写 case 的人不评判自己的结果；评判结果的人不能修改被评判的对象。

## 2. 流程

```text
 主 agent                                  solouvm                          子 agent
────────────────────────────────────────────────────────────────────────────────────────
 1 写 / 改 case 与 tb
   填好 intent / method / expect
   solouvm validate ──────────────────────▶ 检查 case 信息是否完整
 2 solouvm regress ───────────────────────▶ 执行、机械判定（含 expect 检查）
 3 solouvm triage plan ───────────────────▶ 按签名分组，列出分诊任务
 4 为每个任务启动一个子 agent ───────────────────────────────────────────────▶
                                           solouvm triage packet <task> ◀── 5 取上下文包
                                                                            6 只读代码、日志、波形
                                           solouvm bug add ...         ◀── 7 登记 bug
                                           solouvm triage write ...    ◀── 8 写结论与调试文档
                                                                            9 退出
10 solouvm triage review <task>
     --accept / --reject "理由" ─────────▶ 记录审查结果，更新最终状态
11 根据结论：修 case / 修 RTL / 重跑 / 签核
```

## 3. 主 agent 的输入：case 要写清楚

子 agent 只能看到系统文档和 case 文档，判断质量取决于 case 写得是否清楚。`stage` 为 `ready` 及以上的 case，`validate` 要求下列字段齐全（AI-02）：

| 字段 | 回答的问题 | 示例 |
|------|------------|------|
| `intent` | 这个 case 验证什么功能点 | "验证 WRAP burst 跨 4K 边界时的地址回绕" |
| `method` | 激励怎么产生、结果怎么检查 | "随机长度 2/4/8/16 的 WRAP 读写；scoreboard 逐笔比对；SVA 检查地址" |
| `expect` | 通过时一定能观察到的现象，机械可查 | 至少比对 100 笔；仿真时间不短于 50us |
| `spec_refs` | 对应的规格章节 | `["AXI4 A3.4.1", "design_spec 5.2"]` |

`expect` 由 judge 机械检查（J-18），专门拦截常见的虚假 pass：scoreboard 一笔没比、sequence 没启动、objection 提前撤销、checker 被关闭。子 agent 处理机械检查覆盖不到的语义部分。

## 4. 系统文档

项目级的验证说明，通过 `CFG["system_doc"]` 指定一个或多个 markdown 文件（AI-03），每个上下文包都会带上。建议包含：

1. DUT 功能概述与接口；
2. 验证环境结构：agent、scoreboard、参考模型、覆盖率；
3. 日志约定：各组件的 UVM 消息 ID、scoreboard 的比对打印格式；
4. 已知限制与不验证的部分；
5. 调试指引：常用信号路径、波形里该看哪些信号。

`solouvm init` 生成带上述章节标题的骨架。

## 5. 分诊任务

### 5.1 哪些结果需要分诊

`solouvm triage plan` 根据 `CFG["triage"]` 生成任务列表（AI-05）：

| 结果 | 默认策略 |
|------|----------|
| 失败（fail / timeout / killed / xpass） | 全部分诊；**同一签名合并为一个任务**，附带全部相关 job |
| 环境失败（error） | 不分诊，只在报告中列出 |
| 机械检查报警的 pass（expect 不满足以外的弱信号，如仿真时间异常短） | 分诊 |
| `wip` case 的 pass、本轮相对上一轮改过的 case 的 pass | 分诊，查虚假 pass |
| `signoff` case 的 pass | 不分诊 |

每个任务有稳定的 ID：`<run_id>/<序号>`，例如 `20261103-101500-a1b2/3`。

### 5.2 上下文包

`solouvm triage packet <task>` 输出一个 markdown 文档，是子 agent 开始工作时唯一需要读的东西（AI-06）：

1. 任务说明：要回答哪些问题、可以用哪些命令、输出格式；
2. 系统文档（全文，或超长时给出路径与目录）；
3. case 文档：`intent`、`method`、`expect`、`spec_refs`、`doc`、历史记录（已有 bug、上次分诊结论）；
4. 本轮结果：状态、原因、首条错误、错误前后的日志片段、`expect` 的检查结果；
5. 首条错误中 `file(line)` 指向的源码片段；
6. 同签名的历史失败、这个 case 上次通过的轮次与之后的 git 提交；
7. 可用的复现命令与波形查询示例。

总长度有上限（默认约 2 万 token，可配置），超出部分给出路径与查询命令，不直接塞入。

### 5.3 子 agent 要回答的问题

| 问题 | 可能的结论 |
|------|------------|
| 机械判定为 pass：它真的验证了 `intent` 吗？ | `real_pass` / `false_pass` |
| 机械判定为失败：问题在哪里？ | `design_bug`（RTL）/ `tb_bug`（验证环境或 case）/ `spec_unclear` / `env_issue` / `unknown` |

每个结论必须附：置信度（high / medium / low）、证据（日志行号、源码位置、波形时间点）、一段不超过 200 字的摘要。

### 5.4 子 agent 的输出

1. **bug**：结论为 `design_bug` 或 `tb_bug` 时，用 `solouvm bug add` 登记到对应 case，状态 `open`，带复现种子（M-14）。已有同签名的 open bug 时不重复登记，改为用 `note add` 追加一条调试记录。
2. **调试文档**：`docs/debug/<case>/<run_id>-<task序号>.md`，入库。内容：现象、排查过程、关键证据、结论、建议的修复方向。由 `solouvm triage write --doc FILE` 校验格式后放入。
3. **分诊记录**：`solouvm triage write` 在 case 中追加一条 `Triage` 记录，指向调试文档与 bug。

## 6. 权限边界

### 6.1 原则

- 子 agent **只能往下判**：可以把机械判定的 pass 判为 `false_pass`；不能把任何失败改判为通过（AI-08）。
- 子 agent **只能追加，不能修改**：只能新增 bug、log、triage 记录与调试文档；不能改 case 的任何其他字段，包括 `checks`、`waivers`、`expect`、`expect_fail`、`stage`。否则它可以通过放宽条件让 case 变绿。
- `false_pass` 必须经主 agent 审查接受后才改变最终状态；未审查前在报告中标为"待审查"。

### 6.2 solouvm 怎么保证

agent 能读写哪些文件由 agent 框架控制，solouvm 管不到。solouvm 提供的是：

| 机制 | 作用 |
|------|------|
| 记录写入命令（`bug add`、`note add`、`triage write`） | 唯一的合法写入路径；写入前校验、写入后重新加载验证，失败即回滚 |
| 全局参数 `--role triage` | 该角色下，除记录写入与只读命令外的所有命令直接拒绝，退出码 2 |
| 写入审计 | 每条记录带 `author`（角色与 agent 标识）与时间 |
| 子 agent 定义模板 | `solouvm init --agents` 生成框架所需的配置：工具白名单、角色变量、标准流程说明 |

框架层面应把子 agent 的工具限制为：读文件、搜索、执行 `solouvm` 命令；不给通用的文件编辑工具。

## 7. 审查

主 agent 对每个分诊任务执行（AI-09）：

```text
solouvm triage review <task> --accept
solouvm triage review <task> --reject "日志 812 行的错误来自 monitor 采样时机，不是 RTL 问题"
```

| 子 agent 结论 | 接受后的效果 |
|---------------|--------------|
| `real_pass` | 无变化 |
| `false_pass` | 该 job 最终状态改为 `false_pass`（算失败） |
| `design_bug` / `tb_bug` | 对应 bug 保持 open；主 agent 去修 |
| `env_issue` | 状态改为 `error`，可重跑 |
| `spec_unclear` / `unknown` | 主 agent 自行处理，必要时由人介入 |

驳回时，子 agent 登记的 bug 自动标为 `wontfix` 并附驳回理由，调试文档保留（同样是有价值的记录）。

审查记录写入 `Triage` 记录的 `review` 字段。`solouvm triage status` 列出本轮所有任务及其审查状态，主 agent 以此确认没有遗漏。

## 8. 人的位置

整个流程中的任何角色都可以由人担任：人可以手动执行 `triage packet` 阅读上下文、用 `triage write` 写结论，也可以只做最后的审查。命令和数据完全相同。

## 附录 A：Claude Code 示例配置

`solouvm init --agents claude` 生成 `.claude/agents/solouvm-triage.md`（示意）：

```markdown
---
name: solouvm-triage
description: 对一个 solouvm 分诊任务做独立判断：结果是否可信、失败原因、登记 bug、写调试文档。
tools: Read, Grep, Glob, Bash(solouvm --role triage :*)
---

你是验证结果的独立审查者。开始时执行：
  solouvm --role triage triage packet <task>
严格按上下文包中的任务说明工作。你不能修改任何源码；所有输出必须通过
solouvm --role triage bug add / note add / triage write 写入。
完成后用一段话报告结论与置信度，然后结束。
```

两层限制叠加生效：框架层面没有文件编辑工具，Bash 只允许以 `solouvm --role triage` 开头的命令；solouvm 层面在 triage 角色下拒绝记录写入与只读之外的一切命令。白名单的具体写法以所用框架版本的文档为准。

其他框架按同样的要点配置：只读工具 + 带角色参数的 `solouvm` 命令。
