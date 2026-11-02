# 判定规则

本文定义 solouvm 如何把一个 job 的执行事实和日志变成结论。判定只在 A 上进行（N-10），输入是 B 写下的 `job.facts` 和 `sim.log`。

> 状态：设计稿，尚无代码。

## 1. 原则

1. **宁可误报失败，不可误报通过。** 拿不准的情况一律不判 pass。
2. **规则顺序固定，先命中者为准。** 用户只能调参数（上限、正则、waiver），不能调顺序。
3. **能挽回失败的手段只有三个，且范围都很窄**：`pass_regex` 只挽回"缺 Report Summary"；waiver 只豁免被它匹配到的那几行；`expect_fail` 只把"按预期原因失败"的 fail 翻成 xfail。
4. **判定可重复。** 同样的 facts、日志、配置，结论一定相同；改了配置就重新 `judge`，不用重跑。
5. **AI 分诊只能往下判。** 分诊可以把 pass 判为 `false_pass`，不能把任何失败判为通过（AI-08）。

## 2. 结果状态

| 状态 | 含义 | 算通过 | 类别 |
|------|------|:------:|------|
| `pass` | 通过 | ✓ | — |
| `xfail` | 预期失败，且确实失败 | ✓ | — |
| `fail` | 设计或测试失败 | | 设计 / 测试 |
| `xpass` | 预期失败，却通过了 | | 设计 / 测试 |
| `timeout` | 墙钟超时或挂死 | | 执行异常 |
| `killed` | 被信号终止（非超时） | | 执行异常 |
| `incomplete` | 开始了但没有结束：中断、机器崩溃 | | 执行异常 |
| `error` | 环境失败或无法判定：license、编译失败、没有 facts、残留结果 | | 环境 |
| `skip` | 没有运行，且明确允许缺失（`--allow-missing`） | ✓ | — |
| `false_pass` | 机械判定为 pass，但经分诊并审查确认没有真正验证到 `intent` | | 设计 / 测试 |

报告按类别分开统计（J-16），让"设计有 bug"和"环境不稳"一眼可分。

## 3. 规则顺序

| # | 条件 | 结果 | 原因文本示例 |
|---|------|------|--------------|
| 0a | 本 job 依赖的编译失败 | `error` | `build failed: default/3c9e1f2a, see build.log line 120` |
| 0b | 没有 `job.facts` | `error`（`--allow-missing` 时 `skip`） | `not run: job.facts missing` |
| 0c | `run_id` 与本轮不符 | `error` | `stale facts from run 20261102-…` |
| 0d | `schema` 或 `generator` 不兼容 | `error` | `facts schema 2 not supported; regenerate` |
| 1 | 只有开始段，或 `state=interrupted` | `incomplete` | `job did not finish (host simbox01)` |
| 2 | 日志命中 `env_fail_regex` | `error` | `environment failure at line 3: Failed to obtain license` |
| 3 | `signal` 非空且不是超时造成 | `killed` | `killed by SIGSEGV` |
| 4 | `timed_out=1` | `timeout` | `wall-clock timeout after 30m` |
| 4' | `hung=1` | `timeout` | `no log output for 10m` |
| 5 | `rc != 0` | `fail` | `exit code 3` |
| 6 | 仿真器错误或 SVA 失败（扣除 waiver） | `fail` | `simulator error at line 812: Error-[NOA] ...` |
| 7 | 命中 `fail_regex` | `fail` | `fail_regex 'TEST FAILED' at line 990` |
| 8 | UVM_FATAL 超限 | `fail` | `UVM_FATAL=1 > 0` |
| 9 | UVM_ERROR 超限 | `fail` | `UVM_ERROR=4 > 0 (2 waived)` |
| 10 | UVM_WARNING 超限（仅当设置了上限） | `fail` | `UVM_WARNING=31 > 30` |
| 11 | UVM 日志但缺 Report Summary，且 `pass_regex` 未命中 | `fail` | `UVM log without Report Summary` |
| 12 | 非 UVM 日志，缺 `SOLOUVM_DONE status=pass` | `fail` | `missing SOLOUVM_DONE marker` |
| 13 | `expect` 中任一条不满足 | `fail` | `expect not met: AXI_SCB compared=0 < 100` |
| 14 | 以上都没有命中 | `pass` | `clean` |

随后应用 `expect_fail`（§6）。分诊审查可能在此之后把 pass 改为 `false_pass`（§11）。

`expect` 排在最后：前面的规则查的是"有没有报错"，`expect` 查的是"有没有真的做事"。一个既没报错、也没做事的 job，正是最典型的虚假 pass。

几点说明：

- **编译失败时 job 根本不会执行**（D-07）：0a 让报告写明真正的原因，而不是笼统的"没有事实文件"。
- **为什么环境失败排在退出码之前**：license 失败时仿真器一般也返回非 0，如果先看退出码，会被误判为设计失败。环境失败的匹配在 A 上用 Python 正则进行（X-16），改了模式重新 `judge` 即可生效。
- **为什么 FATAL 排在 ERROR 之前**：两者都是 fail，但原因文本只写第一条命中的，FATAL 信息量更大。
- **UVM 与非 UVM 的区分**：日志中出现过任一 `UVM_INFO` / `UVM_WARNING` / `UVM_ERROR` / `UVM_FATAL` 即视为 UVM 日志。

## 4. 日志扫描

`judge/scan.py` 对日志只读一遍，每行做固定的几次匹配，内存占用与日志大小无关（J-12）。

### 4.1 UVM 计数

两种来源，优先用前者：

1. **Report Summary**：日志末尾的
   ```text
   --- UVM Report Summary ---
   ** Report counts by severity
   UVM_INFO :  340
   UVM_WARNING :    2
   UVM_ERROR :    0
   UVM_FATAL :    0
   ```
   匹配 `^\s*UVM_(INFO|WARNING|ERROR|FATAL)\s*:\s*(\d+)\s*$`，只认 `Report counts by severity` 之后出现的行。
2. **逐行统计**：没有 Summary 时，统计以 `UVM_ERROR` / `UVM_FATAL` / `UVM_WARNING` 开头、形如 `UVM_ERROR <文件>(<行>) @ <时间>: <reporter> [<ID>] <消息>` 的行。文件与行号可能被关闭（`+UVM_REPORT_DISABLE_FILE_LINE`），匹配不依赖它们。

有 Summary 时仍然逐行扫描：waiver 需要知道具体是哪几行，扣除时用"Summary 计数 − 被豁免的行数"。

### 4.2 仿真器错误

模式由后端提供（`sim.error_regex`），可在配置中追加。内置值：

| 后端 | 模式 |
|------|------|
| VCS | `^Error-\[`、`^Fatal:`、`"Offending '`（SVA 失败） |
| Xcelium | `^xmsim: \*E,`、`^xmsim: \*F,`、`^ncsim: \*E,` |
| Questa | `^\*\* Error`、`^\*\* Fatal` |
| Verilator | `^%Error`、`Assertion failed` |
| Icarus | `^ERROR:` |

上表为初始值。不确定：实现前需要用各仿真器的真实日志逐条核对（S-10）。

仿真器错误默认不受 waiver 影响；只有 `severity` 中显式写了 `"SIM"` 的 waiver 才能豁免它（§5）。

### 4.3 首条错误

扫描过程中记下第一条"会导致失败"的行：仿真器错误、未豁免的 UVM_ERROR / UVM_FATAL、`fail_regex` 命中，取行号最小者。它进入报告（J-10），是失败签名的来源（§8），也是 `expect_fail` 正则的匹配对象（§6）。

### 4.4 仿真结束时间

`Expect(sim_time_min=...)` 与弱信号（§10）都需要仿真结束时间（J-21）：

| 来源 | 说明 |
|------|------|
| UVM 日志 | 取 `[TEST_DONE]` 消息中 `@` 后的时间，例如 `UVM_INFO ... @ 52340ns: reporter [TEST_DONE] 'run' phase is ready to proceed ...` |
| 其他 | 取后端 `sim.time_regex` 第 1 个分组；例如 Xcelium 的 `Simulation complete via $finish(1) at time 52340 NS + 0` |

时间统一换算成 ns 后比较。取不到时，`sim_time_min` 判为不满足（原因写明"取不到仿真结束时间"），不默认通过。

不确定：各仿真器结束打印的确切格式、时间单位与精度需逐一核对。

### 4.5 远端日志的粗筛

远程执行时，judge 读取的不是完整日志，而是 B 上用 `grep -n -F` 按关键词筛出的行（带原始行号），压缩后传回 A（H-16）。关键词集合由 solouvm 生成，覆盖所有可能命中规则的行：

- 固定关键词：`UVM_`、`SOLOUVM_DONE`、`TEST_DONE`，以及后端错误模式、license 模式中的字面量；
- 从用户正则（`fail_regex`、`pass_regex`、`env_fail_regex`、waiver、expect）中提取的字面量。

粗筛只决定"传哪些行"，不做任何判断；判断仍在 A 上用 Python 正则完成，结果与读完整日志相同。日志总行数单独取（`wc -l`）。`explain` 需要的上下文行按需另取。

不确定：从正则中提取字面量的规则。某个正则提取不到长度不少于 3 的字面量时，该 job 退回整份传输。本机执行（local provider）不经过粗筛。



## 5. Waiver

waiver 用来豁免已知、已评估过的告警或错误，而不用放宽整体上限（J-14）。

匹配规则：

- `id`：匹配 UVM 消息中 `[ID]` 部分，完全相等；
- `regex`：对整行做 `re.search`；
- 同时写 `id` 与 `regex` 时，两者都满足才算匹配；
- 只对 `severity` 列出的级别生效，默认 ERROR 与 WARNING；
- 一行被多条 waiver 匹配时，归属第一条；
- 仿真器错误的级别记为 `SIM`，只有 `severity` 中显式包含 `"SIM"` 的 waiver 才会匹配它。

生效与报告：

- 每条被豁免的行从对应级别的计数中扣除；`max` 按每个 job 计数，超出的部分不再豁免；
- 过了 `until` 日期的 waiver 不生效，并在报告中列为"已过期"；
- 报告列出本轮生效的 waiver 及各自豁免了多少行；
- 整轮回归中一次都没匹配到的 waiver 列为"未使用"，提示可以删除。

信号、超时、退出码非 0、`fail_regex`、`expect` 都不能被豁免。

## 6. expect_fail

判定得到原始结果之后才应用（J-06、J-20）：

| 原始结果 | `expect_fail=False` | `expect_fail=True` | `expect_fail="<正则>"` |
|----------|---------------------|--------------------|------------------------|
| `pass` | `pass` | `xpass`（不算通过） | `xpass`（不算通过） |
| `fail` | `fail` | `xfail`（算通过） | 首条错误匹配正则 → `xfail`；不匹配 → `fail`，原因 `failed for an unexpected reason` |
| 其他 | 不变 | 不变 | 不变 |

`timeout`、`killed`、`error`、`incomplete` 不会被翻成 xfail：预期失败指的是"测试按预期报错"，不是"仿真跑挂了"。

推荐写正则：只写 `True` 时，任何失败都会被当成预期失败，负向测试可能长期掩盖别的问题，`ready` 及以上的 case 会收到 `validate` 警告。xfail 不进分诊；原因不符的失败是 `fail`，会进分诊。

结果中同时保留原始结果 `raw_status`，便于追查。

## 7. retry

由 A 驱动（R-01）：

1. 一轮判定结束后，挑出状态在 `regress.retry_on` 中、且剩余次数大于 0 的 job；
2. 生成只含这些 job 的运行目标，以 `TRY=<N+1>` 运行，目录为 `try<N+1>`；
3. 运行、判定，重复直到没有可重试的 job。

retry 使用相同的种子。同种子下设计失败必然复现，所以 `retry_on` 默认只含 `error`（环境失败）。`--detach` 时 A 不在场，不自动 retry；之后可用 `rerun --status error` 手动补跑。

最终结果：

- 任一次尝试为 pass，则最终为 pass，并标注 `flaky: passed on try N`（R-04）；
- 否则取最后一次尝试的结果；
- 每次尝试的状态、原因、耗时都记录在 `results.json` 的 `attempts` 中。

带波形重跑（R-03）是额外的一次尝试，打上 `wave` 标记：它**不参与**最终结果，只为留下波形。

judge 只认 `try<数字>` 目录；手动复现时用的非数字编号（如 `TRY=r1`）不参与判定。

## 8. 失败签名

把首条错误归一化，让"同一个问题"在不同 job 中得到同一个签名（J-11）。

归一化步骤（按顺序）：

1. 去掉 UVM 前缀中的文件名、行号、仿真时间：`UVM_ERROR verif/envs/axi/axi_scb.sv(120) @ 1234ns: ` → `UVM_ERROR `；
2. 路径只保留文件名；
3. 十六进制数 → `<HEX>`，十进制数 → `<N>`；
4. 引号内的字符串保留（往往是关键信息），但其中的数字同样替换；
5. 连续空白压成一个空格，截断到 200 字符。

例：

```text
UVM_ERROR verif/envs/axi/axi_scb.sv(120) @ 1234ns: uvm_test_top.env.scb [AXI_SCB] mismatch: expect 0x00 got 0x1f (addr 0x1f00)
→ UVM_ERROR uvm_test_top.env.scb [AXI_SCB] mismatch: expect <HEX> got <HEX> (addr <HEX>)
```

签名取归一化文本的 SHA-1 前 8 位作为短 ID。报告按签名分组：每组列出出现次数、涉及的 case、一条示例 job。

没有首条错误行的失败（超时、被信号终止、缺 Report Summary、缺完成标记、expect 不满足），签名取"判定原因类别 + 日志中最后一条 UVM 消息的 ID"，例如 `timeout|AXI_MST_DRV`、`expect|AXI_SCB`（J-22）。这样同一处卡死的超时会合并，不同位置的超时不会被混在一起；没有任何 UVM 消息时只用原因类别。

## 9. 与 case 记录的联动

判定完成后、生成报告时执行，**不改变任何状态**：

| 情形 | 报告中的标注 |
|------|--------------|
| 失败 job 所属 case 有 `open` / `fixed` 的 bug，或任一未解决 bug 的 `signature` 与之相同 | `known: burst#1`（M-11） |
| 失败 job 的种子等于某个 `verified` bug 的复现种子 | `regression? burst#1 was verified`（M-22） |
| 某个 `fixed` bug 的复现种子本轮通过 | `burst#1 can be marked verified`（M-22） |
| 某个 `open` bug 的复现种子本轮通过 | `burst#1 may be fixed`（M-13） |

## 10. 虚假 pass 的弱信号

以下情况不足以直接判 fail，但很可疑，报告中标出并纳入分诊（J-19）：

| 信号 | 说明 |
|------|------|
| 仿真结束时间为 0 或远短于该 case 历史中位数（低于 10%）；时间来源见 §4.4 | objection 可能提前撤销 |
| UVM 日志中没有任何来自 `uvm_test_top` 的消息 | test 可能根本没运行 |
| 日志行数远少于该 case 历史中位数（低于 10%） | 激励可能没发出去 |
| case 没有任何 `expect` | 无法机械确认做过事 |

历史中位数取自 A 上的运行历史（Y-01），没有历史时只检查前两条中的"为 0"部分与最后一条。

## 11. 分诊对最终状态的影响

分诊结论只有经主 agent 审查接受后才生效（AI-09）：

| 机械结果 | 分诊结论（已接受） | 最终状态 |
|----------|--------------------|----------|
| `pass` | `real_pass` | `pass` |
| `pass` | `false_pass` | `false_pass` |
| 失败类 | `design_bug` / `tb_bug` / `spec_unclear` / `unknown` | 不变 |
| 失败类 | `env_issue` | `error`（环境类） |
| 任意 | 驳回或未审查 | 不变；报告中标出"待审查" |

`results.json` 同时保留机械结果 `judged_status` 与最终状态 `status`，便于看出哪些结论来自分诊。

分诊结论会过期（AI-20）：`Triage` 记录保存了分诊时的机械结果。重新 `judge` 后，如果某个 job 的机械结果变了（例如改了规则、改了 expect），对应的分诊结论不再作用于最终状态，任务标为"过期"，需要重新分诊。

## 12. 退出码映射

判定完成后，回归类命令的退出码：

- 存在任一不算通过的状态（含已接受的 `false_pass`）→ 1；
- 否则 → 0。

编译失败时退出码为 2（依赖它的 job 记为 `error`，见 §3 的 0a）；配置错误等在判定之前就已经以 2 退出，见 [cli.md](cli.md) §1.1。
