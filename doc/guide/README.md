# solouvm 中文使用手册

`solouvm` 是面向 SystemVerilog / UVM 仿真回归的 Python Runner 与脚手架工具：在控制机（A）用 Python 编写配置、文件清单与 case，生成仅依赖 `make` 与 `bash` 的构建/运行脚本推送到执行机（B）执行，再回收事实文件（`job.facts`）与日志在 A 上完成确定性判定、分诊与报告。

> **实现状态说明**：里程碑 **M1 ~ M4** 的功能代码与自测已完整实现并主要通过内置 `fake` 仿真器与单元测试/端到端测试验证；以下两类边界需特别留意：
> - **`待定` 需求**（见 [requirements.md §25](../requirements.md#25-里程碑归属)）：`C-07`（配置版本检查）、`F-09`（编译键排除模式）、`T-15`（随机种子历史去重）、`E-06 ~ E-08 / E-10`（静态检查深度选项）、`X-18`（磁盘空间检查）、`H-06 / H-14`（Windows/WSL2、本地并行 SSH）、`K-09 / K-10`（趋势对比与时间过滤）、`V-05`（覆盖率阈值门禁）、`W-01 ~ W-03`（`watch` / `resume` / 交互调试入口）、`AI-17 / AI-18`、`Z-08 / Z-09`（包分发与多用户隔离）、`L-07 / L-18`（`--only` 与子目录运行）**暂未纳入当前发行版本**。
> - **需真实 EDA 环境核验事项**（见 [requirements.md §26](../requirements.md#26-不确定事项)）：涉及 VCS / Xcelium / Questa / Xezim / Verilator / `slang` / Verdi / LSF / 大规模 `.f` 与 `${VAR}` 在实机上的具体参数、输出格式与性能特征，尚未在本仓库无许可证环境中实测。

## 核心架构（A / B 分离）

| 侧 | 职责 | 环境要求 |
|----|------|----------|
| A（控制机） | 配置加载、`gen` 生成、`push`、流式读取日志、`judge` 判定、报告、`triage` 分诊 | Python >= 3.10 + `solouvm`（无第三方运行时依赖） |
| B（执行机） | `make build` 编译、`run_job.sh` / `launch.sh` 执行仿真、生成 `job.facts` | `make`、`bash`、POSIX 工具、仿真器（不装 Python） |

## 章节导航

1. [01 安装与环境准备](01-install.md)
2. [02 五分钟快速上手](02-quickstart.md)
3. [03 配置与文件清单](03-configuration.md)
4. [04 编写 Case、Expect 与 Waiver](04-writing-cases.md)
5. [05 执行、重跑与远端调度](05-running-and-remote.md)
6. [06 判定规则、Interpret 与报告](06-verdict-and-reports.md)
7. [07 记录系统（Notes）与 AI 分诊](07-records-and-triage.md)
8. [08 UVM 脚手架与风格规范](08-uvm-scaffolding.md)
9. [09 Fast Compile、覆盖率与波形查询](09-fast-compile-cov-wave.md)
10. [10 命令行参考](10-cli-reference.md)
11. [11 常见问题与排错](11-troubleshooting.md)

## 相关设计文档与 Agent 技能

- 设计定稿：[doc/cli.md](../cli.md)、[doc/config.md](../config.md)、[doc/verdict.md](../verdict.md)、[doc/formats.md](../formats.md)、[doc/ai-workflow.md](../ai-workflow.md)、[doc/uvm-style.md](../uvm-style.md)
- AI Agent 技能包：[skills/solouvm/SKILL.md](../../skills/solouvm/SKILL.md)
