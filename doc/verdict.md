# 判定规则

本文定义 solouvm 如何把一个 job 的执行事实和日志变成结论。判定只在 A 上进行（N-10），输入是 B 写下的 `job.facts` 和 `sim.log`。

> 状态：设计稿，尚无代码。

## 1. 原则

1. **宁可误报失败，不可误报通过。** 拿不准的情况一律不判 pass。
2. **规则顺序固定，先命中者为准。** 用户只能调参数（上限、正则、waiver），不能调顺序。
3. **能挽回失败的手段只有三个，且范围都很窄**：`pass_regex` 只挽回"缺 Report Summary"；waiver 只豁免被它匹配到的那几行；`expect_fail` 只把 fail 翻成 xfail。
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
| `error` | 环境失败或无法判定：license、没有 facts、残留结果 | | 环境 |
| `skip` | 没有运行，且明确允许缺失（`--allow-missing`） | ✓ | — |
| `false_pass` | 机械判定为 pass，但经分诊并审查确认没有真正验证到 `intent` | | 设计 / 测试 |

报告按类别分开统计（J-16），让"设计有 bug"和"环境不稳"一眼可分。

## 3. 规则顺序

| # | 条件 | 结果 | 原因文本示例 |
|---|------|------|--------------|
| 0a | 没有 `job.facts` | `error`（`--allow-missing` 时 `skip`） | `not run: job.facts missing` |
| 0b | `run_id` 与本轮不符 | `error` | `stale facts from run 20261102-…` |
| 0c | `schema` 或 `generator` 不兼容 | `error` | `facts schema 2 not supported; regenerate` |
| 1 | 只有开始段，或 `state=interrupted` | `incomplete` | `job did not finish (host simbox01)` |
| 2 | `env_fail=1` | `error` | `environment failure: license` |
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
| 13 | `expect` 中任一条不满足 | `fail` | `expect not met: [SCB] compared = 0 < 100` |
| 14 | 以上都没有命中 | `pass` | `clean` |

随后应用 `expect_fail`（§6）。分诊审查可能在此之后把 pass 改为 `false_pass`（§11）。

`expect` 排在最后：前面的规则查的是"有没有报错"，`expect` 查的是"有没有真的做事"。一个既没报错、也没做事的 job，正是最典型的虚假 pass。

几点说明：

- **为什么环境失败排在退出码之前**：license 失败时仿真器一般也返回非 0，如果先看退出码，会被误判为设计失败。
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
2. **逐行统计**：没有 Summary 时，统计形如 `UVM_ERROR <文件>(<行>) @ <时间>: <reporter> [<ID>] <消息>` 的行。

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

上表为初始值，实现前需要用各仿真器的真实日志逐条核对。

### 4.3 首条错误

扫描过程中记下第一条"会导致失败"的行：仿真器错误、未豁免的 UVM_ERROR / UVM_FATAL、`fail_regex` 命中，取行号最小者。它进入报告（J-10），也是失败签名的来源（§8）。

## 5. Waiver

waiver 用来豁免已知、已评估过的告警或错误，而不用放宽整体上限（J-14）。

匹配规则：

- `id`：匹配 UVM 消息中 `[ID]` 部分，完全相等；
- `regex`：对整行做 `re.search`；
- 同时写 `id` 与 `regex` 时，两者都满足才算匹配；
- 只对 `severity` 列出的级别生效，默认 ERROR 与 WARNING；
- 一行被多条 waiver 匹配时，归属第一条。

生效与报告：

- 每条被豁免的行从对应级别的计数中扣除；超过 `max` 的部分不再豁免；
- 过了 `until` 日期的 waiver 不生效，并在报告中列为"已过期"；
- 报告列出本轮生效的 waiver 及各自豁免了多少行；
- 整轮回归中一次都没匹配到的 waiver 列为"未使用"，提示可以删除。

waiver 不作用于仿真器错误（§4.2）以外的硬失败：信号、超时、退出码非 0 都不能被豁免。

## 6. expect_fail

判定得到原始结果之后才应用：

| 原始结果 | expect_fail=False | expect_fail=True |
|----------|-------------------|------------------|
| `pass` | `pass` | `xpass`（不算通过） |
| `fail` | `fail` | `xfail`（算通过） |
| 其他 | 不变 | 不变 |

`timeout`、`killed`、`error`、`incomplete` 不会被翻成 xfail：预期失败指的是"测试按预期报错"，不是"仿真跑挂了"。

结果中同时保留原始结果 `raw_status`，便于追查。

## 7. retry

由 A 驱动（R-01）：

1. 一轮判定结束后，挑出状态在 `regress.retry_on` 中、且剩余次数大于 0 的 job；
2. 生成只含这些 job 的运行目标，运行目录为 `try<N+1>`；
3. 运行、判定，重复直到没有可重试的 job。

最终结果：

- 任一次尝试为 pass，则最终为 pass，并标注 `flaky: passed on try N`（R-04）；
- 否则取最后一次尝试的结果；
- 每次尝试的状态、原因、耗时都记录在 `results.json` 的 `attempts` 中。

带波形重跑（R-03）是额外的一次尝试，打上 `wave` 标记：它**不参与**最终结果，只为留下波形。

## 8. 失败签名

把首条错误归一化，让"同一个问题"在不同 job 中得到同一个签名（J-11）。

归一化步骤（按顺序）：

1. 去掉 UVM 前缀中的文件名、行号、仿真时间：`UVM_ERROR tb/scb.sv(120) @ 1234ns: ` → `UVM_ERROR `；
2. 路径只保留文件名；
3. 十六进制数 → `<HEX>`，十进制数 → `<N>`；
4. 引号内的字符串保留（往往是关键信息），但其中的数字同样替换；
5. 连续空白压成一个空格，截断到 200 字符。

例：

```text
UVM_ERROR tb/scb.sv(120) @ 1234ns: uvm_test_top.env.scb [SCB] addr 0x1f00 expect 0x00 got 0x1f
→ UVM_ERROR uvm_test_top.env.scb [SCB] addr <HEX> expect <HEX> got <HEX>
```

签名取归一化文本的 SHA-1 前 8 位作为短 ID。报告按签名分组：每组列出出现次数、涉及的 case、一条示例 job。

## 9. 与 case 记录的联动

判定完成后、生成报告时执行，**不改变任何状态**：

| 情形 | 报告中的标注 |
|------|--------------|
| 失败 job 所属 case 有 `open` / `fixed` 的 bug | `known: burst#1`（M-11） |
| 失败 job 的种子等于某个 `verified` bug 的复现种子 | `regression? burst#1 was verified`（M-22） |
| 某个 `fixed` bug 的复现种子本轮通过 | `burst#1 can be marked verified`（M-22） |
| 某个 `open` bug 的复现种子本轮通过 | `burst#1 may be fixed`（M-13） |

## 10. 虚假 pass 的弱信号

以下情况不足以直接判 fail，但很可疑，报告中标出并纳入分诊（J-19）：

| 信号 | 说明 |
|------|------|
| 仿真结束时间为 0 或远短于该 case 历史中位数（低于 10%） | objection 可能提前撤销 |
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

## 12. 退出码映射

判定完成后，回归类命令的退出码：

- 存在任一不算通过的状态（含已接受的 `false_pass`）→ 1；
- 否则 → 0。

编译失败、配置错误等在判定之前就已经以 2 退出，见 [cli.md](cli.md) §1.1。
