# 06 判定规则、结果与报告

solouvm 不信任仿真器退出码本身，所有通过与否均由 A 侧在汇总 `job.facts` 与流式扫描 `sim.log` 后按固定优先级表裁决（[verdict.md](../verdict.md)）。

## 1. 状态与类别（Categories）

| 状态 | 类别 | 是否算通过 | 典型含义 |
|------|------|------------|----------|
| `pass` | `None` | 是 | 无错误、满足 `expect` 与完成标记 |
| `xfail` | `None` | 是 | `expect_fail` 负向测试按预期命中失败 |
| `skip` | `None` | 是 | 非阻塞跳过（当前主要保留作报告兼容） |
| `fail` | `design` | 否 | 退出码非 0、仿真器错误、UVM 超限、缺 Summary、`expect` 不满足 |
| `xpass` | `design` | 否 | 配了 `expect_fail` 却意外跑通 |
| `false_pass` | `design` | 否 | 经分诊审查（`triage review --accept`）确认为虚假通过 |
| `timeout` / `killed` / `incomplete` | `execution` | 否 | 超时、被信号杀死、中断或缺失产物 |
| `error` | `environment` | 否 | 构建失败、license 失败、`job.facts` 缺失或损坏 |

## 2. 判定优先级总览（规则 0a -> 14）

先命中者为准，简述如下：

1. **0a ~ 0d（构建与事实校验）**：依赖 `build` 失败 -> `error`；`job.facts` 缺失/损坏/未完成（含 `partial=True` 下正运行 job）-> `incomplete` 或 `error`；生成器版本或 `run_id` 不匹配 -> `error`。
2. **1（License / 环境正则）**：命中 `env_fail_regex` / 后端 `license_regex` -> `error`。
3. **2 ~ 4（终止与超时）**：`timed_out` 或 `hung` -> `timeout`；收到信号杀死 -> `killed`。
4. **5 ~ 7（退出码与显式错误）**：进程 `rc != 0` -> `fail`；未豁免的仿真器报错 -> `fail`；命中 `fail_regex` -> `fail`。
5. **8 ~ 10（UVM 计数上限）**：扣除有效 `Waiver` 后检查 `UVM_FATAL` / `UVM_ERROR` / `UVM_WARNING` 是否越界。
6. **11 ~ 12（完成度标记）**：UVM 日志必须含 `UVM Report Summary`（或显式 `pass_regex`）；非 UVM 日志必须有 `SOLOUVM_DONE status=pass`。
7. **13（`expect` 机械防伪）**：检查 scoreboard 比较次数、正则匹配次数、最短仿真时间、`uvm_info_min`。
8. **14（干净通过）**：全部检查通过 -> `pass`。
9. **叠加处理**：
   - 若 case 设置了 `expect_fail`，把符合预期的失败转为 `xfail`，把干净跑通转为 `xpass`（算失败）。
   - 若存在已被 `main` **accept** 且关联至本轮相同 `judged_status` 的 `Triage(verdict="false_pass")`，则覆盖为 `false_pass`（但 AI/人工分诊**永远不能把失败改成通过**）。

## 3. 失败签名与 `judge` 可重复执行

- `judge` 对每条失败提取首条错误并归一化（屏蔽时间戳、`@ <N>ns`、行号、seed 等噪声）生成稳定 `signature.id`（12 位 hex），便于归类并发故障与追踪历史复现。
- 修改 `solouvm.conf.py` 中的 `checks`、`waivers` 或 `case.py` 后，无需重跑仿真，直接执行 `solouvm judge` 即可按新规则原地重算 `results.json`、`results.txt` 与 `junit.xml`（除 `judged_at` 外确定性一致）。

## 4. 多格式报告输出（`report`）

```bash
solouvm report                              # 终端文本汇总
solouvm report --format junit -o junit.xml  # CI 友好的 JUnit XML
solouvm report --format html -o report.html # 独立静态 HTML 报告
solouvm report --compare <prev_run_id>      # 对比上一轮的新增失败 / 修复通过
```

> 说明：趋势历史面板与按时间范围对比（`K-09` / `K-10`）当前属 **待定**。
