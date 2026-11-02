# 需求清单

本文列出 solouvm 计划支持的全部功能，包括暂时不确定做不做的。每一条都有编号，后续的架构文档、代码、测试都用编号引用它。

## 0. 约定

**编号**：`<领域代号>-<序号>`，例如 `H-03`、`AI-05`。编号一经发布不再复用；需求删除时标注"已废弃"，不挪号。

**优先级**：

| 级别 | 含义 |
|------|------|
| P0 | 首个可用版本必须有；缺了就跑不起一次完整回归 |
| P1 | 第二阶段；回归规模上来、开始多人使用后必需 |
| P2 | 有明确价值，排期靠后 |
| P3 | 待评估：先记下来，做不做、怎么做以后再定 |

**术语**：

| 术语 | 含义 |
|------|------|
| A / 控制机 | 写代码、运行 solouvm 的机器 |
| B / 执行机 | 实际跑编译与仿真的机器；可以和 A 是同一台 |
| case | `case.py` 里声明的一个测试 |
| job | 一个 case 配一个 seed，是一次仿真的最小单位；编号 `job_id = <case>__<seed>` |
| 事实文件 | B 上每个 job 结束时写下的执行事实：退出码、信号、是否超时、耗时；**不含 pass / fail 判断** |
| provider | 决定"在哪台机器、用什么方式执行命令"的插件，例如 local、ssh、lsf |
| sync | 决定"文件怎么到 B"的插件，例如 none、rsync、git |

**里程碑**：优先级表示重要程度，里程碑决定实现顺序。每条需求属于哪个里程碑见文末 §25。

| 里程碑 | 目标 |
|--------|------|
| M1 核心闭环 | 本机执行：生成 UVM 环境 → 写 case → `validate` → `regress` → 判定（含 expect）→ `explain` → 分诊全流程 → 记录写回；只有 local provider |
| M2 远程与重跑 | ssh / rsync / lsf；retry、失败带波形重跑；运行历史与对比；`uvm sync` |
| M3 fast compile | slang 检查、fake_top；风格检查 |
| M4 周边 | 文档导出、进度总览、覆盖率、波形文本查询、HTML 报告、doctor |
| 待定 | 暂不排期，保留记录 |

## 1. 总体约束

这些是贯穿所有功能的硬约束，违反任何一条都视为缺陷。

| 编号 | 约束 | 优先级 |
|------|------|--------|
| N-01 | 配置只用 Python：`solouvm.conf.py` / `filelist.py` / `case.py`，不解析 YAML / JSON / TOML 配置 | P0 |
| N-02 | A 上不需要安装任何依赖：Python >= 3.10 加上 solouvm 自身即可运行；第三方纯 Python 库只能以 vendor 方式内置（`solouvm/_vendor/`），锁定版本并保留许可证 | P0 |
| N-03 | 生成物离开 solouvm 也能独立使用：在 B 上只靠 `make` 就能编译、跑 job | P0 |
| N-04 | B 上不需要安装 solouvm，也不需要 Python；只需要 `make`、`bash`、POSIX 工具和仿真器 | P0 |
| N-05 | 生成物里只出现相对项目根的路径，整个项目目录可以搬到另一台机器、另一个路径下直接使用 | P0 |
| N-06 | A 和 B 都要支持 Linux 与 macOS；平台差异由生成的脚本在运行时处理 | P0 |
| N-07 | 退出码确定：0 全部通过 / 1 有测试失败 / 2 配置、环境或编译错误 / 130 被中断 | P0 |
| N-08 | 所有 solouvm 读写的结构化文件都带 schema 版本号，版本不符直接报错，不做猜测 | P0 |
| N-09 | 出错时给出原因和下一步建议（hint），不吞异常、不静默降级 | P0 |
| N-10 | 判定只在 A 上做；B 只执行、只记录事实 | P0 |
| N-11 | 同一份输入，生成物内容逐字节相同（排序稳定、不含时间戳以外的随机内容），便于 diff 与缓存 | P1 |
| N-12 | 用户给出的任何字符串（case 名、参数、路径）进入 shell、Makefile、ssh 命令前都必须转义或校验，不允许注入 | P0 |
| N-13 | 生成物记录生成它的 solouvm 版本与格式版本；A 读到不兼容版本的事实文件时报错，提示重新生成 | P0 |
| N-14 | 同一项目可同时跑多轮回归（不同人、不同分支），彼此的运行目录、结果互不覆盖 | P1 |
| N-15 | 命令行输出、错误信息、日志一律英文；面向人阅读的导出文档语言可配置（Q8） | P0 |
| N-16 | 人和 AI 使用同一套命令与数据；不存在"AI 专用"的隐藏接口 | P0 |
| N-17 | 每个命令都支持全局参数 `--json`，输出带 schema 版本的固定结构；默认输出给人看 | P0 |
| N-18 | 出错时（含 `--json` 模式）给出稳定的错误码、出错的文件与行号、修复建议 | P0 |
| N-19 | 输出量有上限：日志、列表、报告默认截断，并注明省略了多少、用什么命令继续查看 | P0 |
| N-20 | 不弹任何交互式确认；需要确认的操作一律通过显式参数表达 | P0 |
| N-21 | 输出中引用的轮次、job、bug、分诊任务一律给出实际 ID，不只写 `latest` | P0 |
| N-22 | 人类输出使用 rich（vendor 内置）：彩色日志、表格、进度条；`--json` 或标准输出不是终端时一律输出纯文本或 JSON，不含颜色与控制字符。rich 只经 `solouvm/ui.py` 使用 | P0 |

## 2. 配置（C）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| C-01 | 从当前目录逐级向上查找 `solouvm.conf.py`，所在目录即项目根；`-c PATH` 可显式指定 | P0 | |
| C-02 | `CFG` 字典提供项目级配置，未写的键取文档化的默认值 | P0 | 键的完整列表见 config 文档 |
| C-03 | 配置字符串支持 `${VAR}` / `${VAR:-默认值}` 引用环境变量；未设置且无默认值时报错 | P0 | |
| C-04 | 加载时校验类型与取值范围，未知键报错并提示最接近的合法键 | P0 | 防止拼写错误被静默忽略 |
| C-05 | 配置文件名禁止使用 `solouvm.py`，检测到时报错说明原因 | P0 | 会遮蔽库本身 |
| C-06 | 本地覆盖文件 `solouvm.local.py`（不入库）：选择使用哪个 provider，并填写个人字段（主机、用户名、远端目录、并行数） | P1 | Q2 |
| C-07 | 支持多套命名配置（profile），例如 `--profile nightly` 切换 seeds、jobs、checks | P3 | 降级：AI 为主力后价值下降 |
| C-08 | `solouvm config` 打印合并后的最终配置及每个值的来源（默认 / 配置文件 / 本地覆盖 / 命令行） | P1 | |

## 3. 文件收集（F）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| F-01 | `filelist.py` 导出 `FILES` 列表或 `files()` 函数，路径相对项目根 | P0 | |
| F-02 | 支持 glob 与递归 glob（`**`），按书写顺序展开并去重 | P0 | 编译顺序对 SV 有意义，不能重排 |
| F-03 | 任一条目匹配不到文件时报错，列出所有缺失条目 | P0 | |
| F-04 | 文件分三组：`rtl`（设计）、`tb`（UVM 包、agent、interface）、`top`（`tb_top`、`bind`、层次引用）；分别生成 `filelist.<组>.f`，并生成合并的 `filelist.f` | P0 | Q5；fast compile 的前提 |
| F-04a | DUT 层次引用与 `bind` 只允许出现在 `top` 组；fake_top 编译时据此给出提示（E-05） | P1 | Q5 |
| F-05 | 每组可单独设置 `incdirs` / `defines` | P1 | |
| F-06 | 可导入已有的 `.f` 文件（IP、vendor 提供），支持嵌套 `-f` / `-F`、`+incdir+`、`+define+`、`-v`、`-y`、`+libext+` | P0 | 展开后纳入统一的文件清单 |
| F-07 | 导入的 `.f` 里出现绝对路径或环境变量时，按 N-05 转换或明确报错 | P0 | |
| F-08 | 检测同一文件被重复包含、同名模块定义在多个文件中，给出警告 | P2 | |
| F-09 | 支持编译库（library）划分，如 VCS `-work` / xrun `-makelib`，供多库编译使用 | P3 | |
| F-10 | `solouvm files` 列出最终文件清单及每个文件来自哪个条目或哪个 `.f` | P1 | |

## 4. Case 与计划（T）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| T-01 | `case.py` 导出 `CASES` 列表或 `cases()` 函数，元素为 `Case` | P0 | |
| T-02 | `Case` 字段：`name`（必填、唯一）、`uvm_test`、`params`、`env`、`timeout`、`retry`、`expect_fail`、`tags` | P0 | |
| T-03 | `params` 渲染为运行期 plusarg：`{"N": 32}` → `+N=32`；`True` → `+FLAG`；`False` → 不输出 | P0 | |
| T-04 | `Case.build` 指定该 case 使用哪套编译配置（见 D-02） | P1 | 编译期参数靠它实现 |
| T-05 | 每个 case 可覆盖 `checks`（判定参数） | P1 | |
| T-06 | 种子：固定列表、范围表达式（`"1-5,42"`）、随机 N 个 | P0 | |
| T-07 | 每个 case 可单独指定种子数量（reseed），例如冒烟 1 个、压力 50 个 | P1 | |
| T-08 | 随机生成的种子写入计划文件；用同一份计划重跑，种子不变 | P0 | 可复现 |
| T-09 | 按名字选择 case，支持通配：`--case 'smoke*'` | P0 | 匹配为空时见 T-17 |
| T-10 | 按 tag 选择与排除：`--tag nightly`、`--exclude-tag slow`，可组合 | P0 | |
| T-11 | `solouvm list` 列出将要执行的 job（case × seed × tag × build），不生成、不执行 | P0 | |
| T-12 | 计划文件记录本次所有 job 的完整参数，是 B 端生成物与 A 端判定的唯一依据 | P0 | |
| T-13 | job 调度顺序可配置：默认按 case 名稳定排序；可选"预计耗时长的先跑" | P2 | 缩短回归总时长，需要历史耗时 |
| T-14 | case 分组 / suite：一组 case 共享默认参数，例如 `Suite("axi", cases=[...], timeout="1h")` | P2 | |
| T-15 | case 之间的依赖（A 通过后才跑 B） | P3 | |
| T-16 | 分片：`--shard i/n` 把 job 稳定地分成 n 份，只生成第 i 份 | P1 | 给外部调度器分发用 |
| T-17 | 选择条件匹配不到任何 job 时退出码为 2，并提示最接近的 case 名或 tag；加 `--allow-empty` 时返回 0 | P0 | Q9；防止 CI 空跑显示通过 |
| T-18 | 单条命令派发的 job 数超过 `limits.max_jobs` 时拒绝执行，需显式 `--force` | P0 | 防止人或 AI 误起大量仿真 |

## 5. 编译（D）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| D-01 | 生成编译目标，在 B 上用 `make build` 编译 | P0 | |
| D-02 | 多套编译配置（build modes），例如 `default` / `gate` / `cov`，各自的 defines、编译选项、产物目录独立 | P1 | |
| D-03 | 编译失败统一归为退出码 2，并输出编译日志中的首个错误位置 | P0 | |
| D-04 | 编译日志单独保存（如 `build/<mode>/build.log`），同样写事实文件 | P0 | |
| D-05 | 编译产物按 build mode 分目录存放，多套配置可以共存、并行编译 | P1 | |
| D-06 | 编译输入未变化时跳过编译（依据文件清单与配置的内容哈希） | P2 | |

## 6. Fast compile（E）

目标：在几秒到几十秒内告诉用户"代码能不能编过"，不必等一次完整编译。

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| E-01 | `solouvm check` 调用 slang 对全部文件做语法与语义检查，带上 incdirs / defines | P1 | 调用 `slang` 可执行文件，不引入 pyslang（N-02） |
| E-02 | slang 诊断整理为 `文件:行:列 级别 消息` 的统一格式，并汇总错误数 | P1 | |
| E-03 | 可配置 slang 的排除文件（加密 IP、vendor 私有语法）和警告开关 | P1 | |
| E-04 | UVM 侧 fake_top：生成只 import `tb` 组中各 package、不例化 DUT 也不例化 interface 的顶层，用真实仿真器只编译 `tb` 组 | P1 | 抓 slang 查不出的厂商相关错误；Q5；见 fast-compile.md §3 |
| E-05 | fake_top 编译失败时，区分"tb 自身错误"和"tb 引用了 DUT 层次路径" | P2 | 后者需要提示改写法 |
| E-06 | RTL 侧增量（仅 vcs / xrun）：找出自上次成功编译以来改动的文件，只重编这些文件及其依赖方，再做 link / elab | P2 | Q4：slang 始终全量 |
| E-07 | 改动检测以"上次成功编译"为基线，记录每个文件的 mtime、大小、内容哈希；git 只作加速提示 | P2 | git 看不到未跟踪文件、仓库外 IP |
| E-08 | 依赖传播：package、`include` 文件、宏定义改动时，依赖它的文件一起重编；无法确定依赖时退回全量编译 | P2 | |
| E-09 | `check` 可在 A 本地运行（A 装有 slang 时），也可经 provider 在 B 上运行 | P1 | |
| E-10 | 编辑器集成：输出格式兼容常见 problem matcher（VS Code 等） | P3 | |

## 7. 生成物（G）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| G-01 | 生成 `filelist*.f`、计划文件、`.solouvm/Makefile`、`.solouvm/bin/run_job.sh` | P0 | |
| G-02 | Makefile 目标：`build`、`build/<mode>`、`run/<job_id>`、`regress`、`clean` | P0 | |
| G-03 | 项目根没有 `Makefile` 时生成一个只含 `include .solouvm/Makefile` 的入口；已有时不覆盖 | P0 | |
| G-04 | 生成物在 B 上只需 `make` 即可工作，不调用 solouvm、不调用 Python（N-04） | P0 | |
| G-05 | 生成物带头注释：由 solouvm 生成、勿手改、生成时的 solouvm 版本 | P0 | |
| G-06 | 支持只生成指定子集（选中的 case、某个分片、失败重跑集）的 Makefile | P0 | 供 T-09/T-10/T-16/R-02 使用 |

## 8. 执行（X）

这一节描述 B 上生成脚本 `run_job.sh` 的行为。

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| X-01 | 每个 job 在独立目录中运行，stdout 与 stderr 合并写入 `sim.log` | P0 | 目录布局见 X-17 |
| X-02 | 墙钟超时；超时后对整个进程组先发 TERM，宽限期后发 KILL，不留残余子进程 | P0 | |
| X-03 | 超时实现按顺序探测：`timeout` → `gtimeout` → 内置 bash 实现（`set -m` + 看门狗），Linux 与 macOS 都可用 | P0 | 见 N-06 |
| X-04 | 每个 job 写事实文件 `job.facts`（`key=value`，一行一项）：开始时写 run_id、job_id、主机名、超时实现、开始时间、`state=running`；结束时追加 `state=done`、退出码、信号、是否超时、是否环境失败、结束时间、耗时 | P0 | Q1；不含判定结果 |
| X-05 | 只有开始段、没有结束段的事实文件，A 判为 incomplete；不另写 pending 文件 | P0 | Q1 |
| X-06 | 事实文件带 run_id，A 能识别并拒绝上一轮残留的结果 | P0 | |
| X-07 | 支持 case 级环境变量与 job 前置命令（如 `module load`） | P0 | |
| X-08 | 并行度通过 `make -jN` 控制 | P0 | |
| X-09 | 将种子注入仿真器原生参数（VCS `+ntb_random_seed`、xrun `-svseed`、Questa `-sv_seed`），同时提供 `+SOLOUVM_SEED` | P0 | |
| X-10 | 注入标准 plusarg：`+UVM_TESTNAME`、`+SOLOUVM_SEED`、`+SOLOUVM_JOB_ID`、`+SOLOUVM_OUTDIR` | P0 | |
| X-11 | 波形开关：生成带 `WAVE=1` 时追加后端定义的波形参数 | P1 | 波形留在 B，不回传 |
| X-12 | fail-fast：`--max-fail N`，失败数达到 N 后停止派发新 job | P2 | 需要 A 在运行中判定，依赖 H-07 |
| X-13 | 中断：终止所有正在运行的 job 及其子进程，结束段写 `state=interrupted`；A 对已完成的 job 照常判定，未完成的记为 incomplete，退出码 130 | P0 | Q3 |
| X-14 | 磁盘清理策略：通过的 job 可压缩或删除 `sim.log` 与中间文件 | P2 | |
| X-15 | 挂死检测：日志连续 N 分钟没有增长即判定挂死并终止，比墙钟超时更早发现 | P2 | 由 `run_job.sh` 的看门狗实现 |
| X-16 | license 排队与失败：日志命中可配置的 license 模式时，事实文件标记为"环境失败"，A 判为 `error` 而非 `fail`，并可自动重试 | P1 | EDA 环境常见，不能算成设计 bug |
| X-17 | 每轮回归的运行目录按 run_id 隔离（`runs/<run_id>/<job_id>/`），`latest` 链接指向最近一轮 | P1 | 实现 N-14 |
| X-18 | 资源限制：每个 job 可设内存上限（`ulimit`），超限记为环境失败 | P3 | |
| X-19 | 回归总时长预算：超出后停止派发新 job，已跑的照常判定 | P2 | |

## 9. Provider 与同步（H）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| H-01 | provider 接口包含四个动作：`push`（送文件到 B）、`run`（在 B 上执行 make 目标）、`read`（读 B 上的事实文件与日志）、`stop`（终止 B 上本轮的全部 job） | P0 | 接口先定，实现逐个加 |
| H-01a | provider 配置分层：团队共享的命名 provider 写在 `CFG["providers"]`；个人选择与个人字段写在 `solouvm.local.py`；`--provider NAME` 临时切换。优先级：命令行 > 本地覆盖 > CFG，都未指定时为 `local` | P0 | Q2 |
| H-02 | `local` provider：A 即 B | P0 | |
| H-03 | `ssh` provider：通过系统 `ssh` 命令执行，复用 `~/.ssh/config` 中的主机配置 | P1 | 不引入 paramiko（N-02） |
| H-04 | `lsf` provider：用 `bsub` 提交 job，支持队列、资源参数 | P1 | |
| H-05 | provider 可叠加，例如 `ssh → lsf`：先登录到提交节点，再 `bsub` | P1 | |
| H-06 | `sge` / `slurm` provider | P3 | 预留接口 |
| H-07 | `read` 流式读取远端文件供 A 判定，不在 A 落盘；波形等大文件永不回传 | P0 | |
| H-08 | sync 接口：`none`（共享盘）、`rsync`、`git` | P1 | `none` 为 P0 |
| H-09 | rsync 只同步源码与生成物，可配置排除规则；默认排除 `out/` 与波形 | P1 | |
| H-10 | Ctrl-C 从 A 传到 B，B 上的 job 全部终止 | P1 | |
| H-11 | 远端工作目录可配置（A 与 B 路径不同），依赖 N-05 | P1 | |
| H-12 | 执行前环境准备：provider 级前置脚本（`source setup.sh`、`module load vcs`） | P0 | |
| H-13 | 运行中可在 A 查看进度：已完成、通过、失败、运行中、剩余 | P1 | 基于 `read` 轮询事实文件 |
| H-14 | 多台执行机分摊同一回归 | P3 | |
| H-15 | 第三方 provider：用户在项目里用 Python 写一个类即可注册 | P2 | |

## 10. 判定（J）

判定全部在 A 上进行，输入是事实文件和日志。

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| J-01 | 判定规则按固定优先级执行，先命中者为准：信号 → 超时 → 退出码非 0 → `fail_regex` → UVM 计数超限 → UVM 缺 Report Summary → 非 UVM 缺完成标记 → 通过 | P0 | 具体规则另立判定文档 |
| J-02 | UVM 计数优先取 Report Summary；缺失时统计 `UVM_ERROR @` / `UVM_FATAL @` 行 | P0 | |
| J-03 | `max_uvm_error` / `max_uvm_fatal` / `max_uvm_warning` 上限可配 | P0 | |
| J-04 | `pass_regex` 只能挽回"缺 Report Summary"这一条，不能覆盖硬失败 | P0 | |
| J-05 | 非 UVM 测试平台用 `SOLOUVM_DONE status=pass` 标记完成 | P0 | |
| J-06 | `expect_fail` 在判定之后应用：fail → xfail（算通过），pass → xpass（算失败） | P0 | |
| J-07 | 结果状态集合：pass / fail / timeout / killed / error / xfail / xpass / skip / incomplete / false_pass | P0 | `false_pass` 只能经分诊审查产生（AI-09） |
| J-08 | 不重跑仿真、只重新判定：`solouvm judge` 可反复执行，规则改动后直接生效 | P0 | 判定在 A 的直接收益 |
| J-09 | 未跑的 job 默认记为 error；`--allow-missing` 时记为 skip | P0 | 分片场景 |
| J-10 | 每个失败 job 记录首条错误行及行号 | P1 | |
| J-11 | 失败归类：把首条错误归一化为签名（去掉时间、数字、地址、路径），按签名分组 | P1 | |
| J-12 | 日志扫描单遍、内存占用恒定，GB 级日志也能处理 | P0 | |
| J-13 | 用户自定义判定函数（Python），在内置规则之后执行，只能把结果改得更严 | P2 | |
| J-14 | waiver：按正则或 UVM 消息 ID 豁免已知的 UVM_ERROR / UVM_WARNING，可设到项目级或 case 级，每条 waiver 必须写理由，报告中列出生效的 waiver | P1 | 不用为一条已知告警放宽整体上限 |
| J-15 | 仿真器自身的错误也计入判定：VCS `Error-[`、xrun `*E,`、Questa `** Error`、SVA 断言失败，模式由后端提供 | P0 | 有些错误不走 UVM 报告 |
| J-16 | 失败分三类：设计 / 测试失败（fail）、环境失败（license、磁盘满、主机故障，记 error）、执行异常（timeout、killed）；报告分开统计 | P1 | 环境失败不该让设计背锅 |
| J-17 | 不稳定（flaky）识别：同一 job 在历史中时过时不过，报告中标出 | P2 | 依赖 Y 区运行历史 |
| J-18 | `Case.expect`：机械可查的期望，如某正则至少匹配 N 次、某计数不小于 N、仿真结束时间不早于 T；不满足即 fail | P0 | 拦截虚假 pass；见 AI-02 |
| J-19 | 内置的虚假 pass 弱信号：仿真结束时间为 0、UVM 日志中没有任何来自 test 的消息、日志行数异常少；只报警，交给分诊 | P1 | 不直接判失败 |

## 11. 重跑（R）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| R-01 | retry：job 失败后由 A 生成仅含失败 job 的 Makefile 再派发，直到通过或次数用尽；每次尝试都保留记录 | P1 | B 上没有判定能力，所以 retry 在 A |
| R-02 | `solouvm rerun --failed`：用上一轮结果中失败 job 的相同种子重跑 | P0 | |
| R-03 | 失败自动带波形重跑：失败 job 用相同种子加 `WAVE=1` 再跑一次，最终结果以首轮为准 | P1 | |
| R-04 | 重跑结果与原结果合并展示，可看出"首轮失败、重跑通过"的不稳定 case | P2 | |

## 12. 报告（K）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| K-01 | 终端摘要：各状态计数、失败列表（job、原因、日志路径） | P0 | |
| K-02 | `results.json`：完整结构化结果 | P0 | |
| K-03 | `results.txt`：与终端摘要相同的纯文本 | P0 | |
| K-04 | JUnit XML，供 Jenkins / GitLab CI 展示 | P0 | |
| K-05 | 失败按签名分组展示（依赖 J-11） | P1 | |
| K-06 | 与上一轮对比：新增失败、新修复、仍失败 | P2 | |
| K-07 | 结果按 run_id 归档，保留最近 N 轮 | P2 | |
| K-08 | 静态 HTML 报告 | P2 | 单文件，无外部资源 |
| K-09 | 历史趋势（通过率、耗时） | P3 | |
| K-10 | testplan 映射：`Case(features=...)`，报告按功能点汇总通过率 | P3 | |
| K-11 | 每个失败 job 附一条可直接复制运行的复现命令（在 B 上的 `make` 命令，以及对应的 `solouvm run` 命令） | P0 | 复现是失败后的第一步 |
| K-12 | 报告附带耗时统计：最慢的 N 个 job、编译耗时、回归总耗时 | P1 | |

## 13. 覆盖率（V）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| V-01 | 覆盖率作为一种 build mode（`cov`），编译与运行参数由后端模板提供 | P2 | |
| V-02 | 每个 job 的覆盖率数据库保存在自己的输出目录 | P2 | |
| V-03 | `solouvm cov merge` 在 B 上合并通过 job 的数据库 | P2 | 经 provider 执行 |
| V-04 | 生成覆盖率报告（urg / imc / vcover），报告留在 B，A 只拿汇总数字 | P2 | |
| V-05 | 增量合并：在已有合并结果上追加新一轮 | P3 | |

## 14. 仿真器后端（S）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| S-01 | 内置 VCS 后端 | P0 | |
| S-02 | 内置 Xcelium（xrun）后端 | P0 | |
| S-03 | 内置 Questa 后端 | P1 | |
| S-04 | 内置 Icarus Verilog 后端 | P1 | 无 UVM，便于开源环境演示 |
| S-05 | 内置 Verilator 后端 | P2 | |
| S-06 | 内置假仿真器后端，用于 solouvm 自测与示例，不需要 EDA 工具 | P0 | |
| S-07 | 自定义后端：在 `CFG["sim"]` 中给出编译、运行、种子、波形、覆盖率等命令模板 | P0 | |
| S-08 | 命令模板占位符：`{filelist}`、`{top}`、`{seed}`、`{build_dir}`、`{job_dir}` 等，清单固定并文档化 | P0 | |
| S-09 | 命令以列表或字符串给出；字符串按 shell 规则拆分，生成物中正确转义 | P0 | |

## 15. Hook（W）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| W-01 | A 上的 hook 阶段：`main`（包住整条命令）、`gen`、`push`、`build`、`run`、`judge`；文件 `pre_hook_<阶段>.py` / `post_hook_<阶段>.py`，函数 `hook(ctx)` | P3 | Q6；降级：分诊流程已覆盖大部分扩展需求 |
| W-02 | hook 抛异常时命令中止，退出码 2 | P3 | 随 W-01 |
| W-03 | `--no-hooks` 跳过全部 hook | P3 | 随 W-01 |
| W-04 | B 上的 hook：`pre_job` / `post_job`，在 `CFG` 中写 shell 片段，生成进 `run_job.sh` | P2 | Q6；B 上没有 Python |

## 16. 命令行（L）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| L-01 | 命令：`gen`、`build`、`run`、`regress`、`judge`、`list`、`rerun`、`clean` | P0 | |
| L-02 | 命令：`check`（fast compile） | P1 | |
| L-03 | 命令：`config`、`files` | P1 | |
| L-04 | 命令：`cov`、`report` | P2 | |
| L-05 | 通用参数：`-c`、`-v`、`-q`、`--provider`、`-j` | P0 | |
| L-06 | 选择参数：`--case`、`--tag`、`--exclude-tag`、`--seed` / `--seeds`、`--shard`、`--allow-empty` | P0 | `--shard` 为 P1 |
| L-07 | shell 自动补全（bash / zsh） | P3 | |
| L-08 | 命令：`notes`（查询 case 记录）、`doc`（导出 markdown）、`progress`（进度总览） | P1 | 见 M 区 |
| L-09 | 命令：`init`、`log`、`validate`（P0）；`doctor`、`status`、`stop`、`schema`（P1）；`history`、`help`（P2） | P0 | 见 Z、Y 区；`stop` 见 Q3 |
| L-10 | 命令：`explain`、`triage plan / packet / write / review / status`、`bug add / update`、`note add`、`todo add / done`（P0）；`wave`（P1）；`bisect`（P2） | P0 | 见 AI、M 区 |
| L-11 | 全局参数：`--json`（N-17）、`--role`（AI-10） | P0 | |

## 17. Case 记录（M）

**case 即文档，即 bug 登记表。** 每个 case 自带说明文档、todo、bug、调试记录，不用另外维护文档或 bug 表。记录写在 Python 里（N-01），随代码一起进 git；markdown 只是导出物。

目标写法（细节随 config 文档确定）：

```python
Case(
    name="burst",
    uvm_test="burst_test",
    doc="""
    验证 AXI burst：INCR / WRAP / FIXED，长度 1–256。
    """,
    notes=[
        Bug(id=1, title="WRAP 跨 4K 边界时地址回绕错误", status="open", seed=4417, found="2026-11-02", owner="wp"),
        Bug(id=2, title="长度 256 时 last 早一拍", status="fixed", seed=12, found="2026-10-28", fixed="2026-10-30", link="JIRA-123"),
        Todo("补 narrow transfer 场景"),
        Log("2026-11-02", "4417 必现；dut.axi_slv.addr_q 在第二拍回绕成 0"),
    ],
)
```

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| M-01 | `Case.doc`：case 的说明文档，markdown 文本 | P1 | |
| M-02 | `Case.notes`：记录列表，类型至少有 `Bug`、`Todo`、`Log`（调试记录）、`Note`（一般备注） | P1 | |
| M-03 | `Bug` 字段：标题、状态、发现日期、修复日期、验证日期、复现种子、发现人、负责人（owner）、外部链接、正文 | P1 | |
| M-03a | `Bug` 状态：`open`（未修）→ `fixed`（已修、待验证）→ `verified`（已用复现种子验证通过）；另有 `wontfix`、`duplicate`。"未解决" = open + fixed | P1 | 区分"设计说修了"和"验证确认修了" |
| M-04 | `Bug` 必须写显式整数 `id=`，同一 case 内唯一，完整 ID 为 `<case>#<id>`（如 `burst#3`）；`Todo` / `Log` / `Note` 的 `id` 可选 | P1 | Q7 |
| M-05 | 加载时校验：状态取值合法、日期格式合法、fixed 必须有关闭日期 | P1 | |
| M-05a | `solouvm notes --next-id <case>` 打印该 case 下一个可用的 bug 编号 | P1 | Q7 |
| M-06 | `solouvm notes`：跨所有 case 查询记录，默认列出全部未解决的 bug 与 todo | P1 | 你要的"一下子拿到所有未解决的 bug" |
| M-07 | `notes` 过滤：按类型、状态、case、tag、负责人、日期范围；输出表格或 JSON | P1 | |
| M-08 | `solouvm doc -o <目录>`：每个 case 导出一个 markdown，另生成总索引，包含全部 case 列表与未解决 bug 汇总；语言由 `CFG["doc"]["lang"]` 决定，默认中文 | P1 | Q8 |
| M-09 | 导出的 case 文档包含：说明、参数、tag、最近一次回归结果（如有 results.json）、全部记录按时间排序 | P1 | |
| M-10 | 导出内容确定：同一输入两次导出逐字节相同，便于把导出目录也提交进 git 看 diff | P1 | 对应 N-11 |
| M-11 | 回归报告联动：失败 job 所属 case 有 open bug 时，在报告中标出"已知问题 burst#1" | P2 | 只做标注，不改判定；预期失败仍用 `expect_fail` |
| M-12 | 复现：`solouvm run --note burst#1` 用该 bug 记录的种子重跑 | P2 | |
| M-13 | 关闭提示：open bug 的复现种子连续 N 轮通过时，在报告中提示"可能已修复" | P3 | 与 M-22 合并实现 |
| M-14 | 命令行写入记录：`bug add`、`bug update`、`note add`、`todo add`、`todo done`、`triage write`。写回 Python 源文件：按 M-15 的约定用 `ast` 定位 `notes=[...]` 的末尾插入，写后重新加载校验，失败即回滚 | P0 | AI 分诊的唯一写入路径（AI-08） |
| M-15 | 大项目可以每个 case 一个文件 `cases/<name>.py`，导出 `CASE = Case(...)`；配置 `CFG["case_dirs"] = ["cases"]` 后自动收集。约定 `notes=[...]` 写在 `Case(...)` 的最后一个参数 | P1 | Q11 |
| M-16 | `Case.owner`：case 负责人；`Bug` / `Todo` 未写 owner 时继承 case 的 owner | P1 | |
| M-17 | `Case.stage`：case 开发进度，`planned`（只有计划）→ `wip`（开发中）→ `ready`（可进回归）→ `signoff`（已签核）；另有 `deprecated` | P1 | |
| M-18 | `planned` 与 `deprecated` 的 case 默认不进回归；`wip` 默认不进 nightly，可用参数放开 | P1 | 计划中的 case 可以先登记，不会把回归跑红 |
| M-19 | `Todo` 字段：标题、owner、截止日期、是否完成 | P1 | |
| M-20 | `solouvm progress`：验证进度总览——各 stage 的 case 数、最近回归通过率、按 owner 统计的未解决 bug 与 todo、逾期 todo | P1 | |
| M-21 | `doc` 导出的总索引包含进度总览（M-20 的内容） | P1 | |
| M-22 | 回归后提示状态不一致：`verified` 的 bug 其复现种子又失败（疑似回归）；`fixed` 的 bug 复现种子已通过（可转 verified） | P2 | 只提示，不自动改源文件 |
| M-23 | 记录的数据模型在首版就定下，并带 schema 版本；之后字段只增不改 | P1 | 记录存在用户文件里，改模型等于逼用户迁移 |
| M-24 | 记录类型 `Triage`：分诊任务 ID、结论、置信度、摘要、证据、调试文档路径、关联 bug、审查结果（接受 / 驳回、理由、审查者） | P0 | 见 AI-07 |
| M-25 | 每条记录带 `author`（人名或 agent 角色与标识）与写入时间；命令行写入时自动填写 | P0 | 审计 |

## 18. AI 协作（AI）

主 agent 写 case、提交回归；子 agent 独立分诊；主 agent 审查。完整流程见 [ai-workflow.md](ai-workflow.md)。

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| AI-01 | 支持"主 agent 提交 → 子 agent 分诊 → 主 agent 审查"的完整流程，每一步都有对应命令 | P0 | |
| AI-02 | `Case` 增加 `intent`（验证什么）、`method`（怎么激励、怎么检查）、`spec_refs`（规格出处），与 `expect`（J-18）一起构成 case 的验收信息；`stage` 为 `ready` 及以上时 `validate` 要求齐全 | P0 | 子 agent 判断质量取决于此 |
| AI-03 | `CFG["system_doc"]`：项目级验证说明文档；`init` 生成骨架，每个上下文包都附带 | P0 | |
| AI-04 | `solouvm explain <job_id>`：一次给出排查一个结果所需的信息——原因、首条错误、前后日志、源码片段、expect 检查结果、同签名历史、关联 bug、复现命令 | P0 | 人与 AI 共用 |
| AI-05 | `solouvm triage plan`：按策略生成分诊任务；失败按签名合并为一个任务；pass 按 stage 与是否改动决定是否分诊；策略可在 `CFG["triage"]` 配置 | P0 | |
| AI-06 | `solouvm triage packet <task>`：输出任务的上下文包（任务说明、系统文档、case 文档与历史记录、本轮结果、explain 内容），总长度有上限，超出部分给路径与查询命令 | P0 | |
| AI-07 | `solouvm triage write <task>`：写入 `Triage` 记录（M-24），并把调试文档校验后放到 `docs/debug/<case>/<run_id>-<序号>.md`（入库） | P0 | |
| AI-08 | 子 agent 只能往下判、只能追加：可把 pass 判为 `false_pass`，不能把失败判为通过；只能新增 bug / log / triage 记录与调试文档，不能改 case 的任何其他字段 | P0 | 防止"让 case 变绿" |
| AI-09 | `solouvm triage review <task> --accept \| --reject 理由`：接受后按结论更新最终状态（`false_pass` 生效、`env_issue` 转 error）；驳回时子 agent 登记的 bug 自动标为 `wontfix` 并附理由 | P0 | |
| AI-10 | 全局参数 `--role triage`：该角色下只允许只读命令与记录写入命令，其余一律拒绝（退出码 2） | P0 | 与 agent 框架的工具白名单叠加 |
| AI-11 | `solouvm triage status`：列出某轮全部分诊任务、结论、审查状态，标出未审查项 | P0 | |
| AI-12 | `solouvm init --agents <框架>`：生成子 agent 定义模板（工具白名单、角色参数、标准流程）；首个支持 Claude Code，其余框架给通用说明 | P1 | |
| AI-13 | `solouvm wave <job_id> --signals ... --from T --to T`：以有上限的文本表格返回波形中指定信号的值；在 B 上调用厂商工具导出 | P1 | 具体工具待调研；只读查询，不是交互式调试 |
| AI-14 | `solouvm log` 支持精确读取：`--grep`、`--around 行号`、`--lines 起-止`，输出有上限 | P0 | 扩展 Z-05 |
| AI-15 | 从 UVM 消息的 `file(line)` 与仿真器错误中解析源码位置，在 explain 与上下文包中附带源码片段 | P0 | |
| AI-16 | 子 agent 结论必须附置信度与证据（日志行号、源码位置、波形时间点）；`triage write` 校验缺项即拒绝 | P0 | |
| AI-17 | `solouvm bisect <case> --seed N`：在 git 提交之间二分，找出引入失败的提交 | P2 | |
| AI-18 | MCP server：用标准库实现 stdio JSON-RPC，把只读命令与记录写入暴露为工具 | P3 | |

## 19. 运行历史与可复现（Y）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| Y-01 | A 上保存每轮回归的结果：项目内 `.solouvm/runs/<run_id>/` 下有计划、结果、环境信息；`.solouvm/` 整体加入 `.gitignore` | P1 | Q10 |
| Y-02 | 每轮记录环境信息：git commit 与是否有未提交改动、solouvm 版本、仿真器版本、执行机主机名、provider | P1 | |
| Y-03 | 有未提交改动时，额外保存一份 diff，保证结果可追溯到确切代码 | P2 | |
| Y-04 | `solouvm history`：列出最近 N 轮的时间、提交、通过率、耗时 | P2 | |
| Y-05 | `solouvm history <case>`：某个 case 在各轮的结果，用于看 flaky 与回归点 | P2 | |
| Y-06 | 历史保留策略：保留最近 N 轮或 N 天，可配置 | P2 | |

## 20. 易用性与运维（Z）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| Z-01 | `solouvm init`：在当前目录生成起步项目，内容见 U-03 | P0 | 第一次使用的入口 |
| Z-02 | `solouvm doctor`：检查 A 与 B 的环境——Python 版本、`make`、`bash`、超时实现、仿真器与 slang 是否在 PATH、ssh 连通性、远端目录可写 | P1 | remote 场景排错必需 |
| Z-03 | `--dry-run`：打印将要 push 的文件、在 B 上执行的命令，不实际执行 | P1 | |
| Z-04 | `solouvm status`：查看正在进行的回归进度（同 H-13），可在另一个终端执行 | P1 | |
| Z-05 | `solouvm log <job_id>`：经 provider 读取远端日志，支持 `--tail` / `-f` 跟随 | P0 | 精确读取见 AI-14 |
| Z-06 | 提交后立即返回（detach）：`regress --detach` 只推送并启动，之后用 `status` 看进度、`judge` 出结果、`stop` 终止；默认阻塞并显示进度，结束后自动判定 | P1 | Q3 |
| Z-07 | solouvm 自身的调试日志：`-vv` 打印每条执行的外部命令，并写入 `.solouvm/solouvm.log` | P1 | |
| Z-08 | Python API：`import solouvm` 后可在脚本中调用 gen / regress / judge / notes，返回结构化结果 | P3 | 降级：`--json` 已能满足程序调用 |
| Z-09 | 配置与 case 的变更提示：`gen` 时对比上一轮，提示新增、删除、参数变化的 case | P3 | |
| Z-10 | `solouvm validate`：只加载与校验配置、文件清单、case、记录，按 `文件:行` 报告问题，不生成任何文件 | P0 | 改完 case 立即自检 |
| Z-11 | `solouvm schema [config\|case\|results\|...]`：打印对应结构的 JSON Schema | P1 | |
| Z-12 | `solouvm help <主题>`：内置离线文档，内容与 `doc/` 一致 | P2 | |

## 21. UVM 环境生成（U）

所有 UVM 环境都由 solouvm 按同一套模板生成，AI 与人只在生成的结构里填写逻辑，保证环境风格长期一致。风格规范见 [uvm-style.md](uvm-style.md)。

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| U-01 | `solouvm new agent\|env\|test\|seq\|vseq NAME`：按模板生成对应组件的全部文件，放到 uvm-style.md §2 规定的位置 | P0 | |
| U-02 | `tbspec.py`：用 Python 描述 env 与 agent（接口信号、时钟复位、transaction 字段、主动 / 被动、是否带 scoreboard 与 coverage），生成 interface、transaction、driver / monitor 骨架、coverage 桩 | P0 | |
| U-03 | `solouvm init` 生成完整的起步项目：配置文件、`tbspec.py`、一个示例 env 与 test、`filelist.py`、示例 case，用 `fake` 后端开箱可跑 | P0 | 与 Z-01 合并 |
| U-04 | 受保护的用户代码区：生成文件中 `// solouvm:user begin/end <名>` 之间的内容在重新生成时保留；`solouvm uvm sync` 按当前模板与 `tbspec.py` 重新生成全部组件 | P1 | 模板升级可批量作用到所有环境 |
| U-05 | 重新生成时，模板中已不存在的用户代码区内容写入 `.orphan` 文件，不丢弃；区域外的手工修改报告出来，不静默覆盖 | P1 | |
| U-06 | 模板内置在 solouvm 中并带版本号；团队可在项目中覆盖模板，但整个项目只允许一套，生成文件头部记录风格与模板版本 | P0 | |
| U-07 | 目录与 `filelist.py` 的约定使新增组件无需修改 `filelist.py`（按依赖顺序的 glob） | P0 | uvm-style.md §2 |
| U-08 | `solouvm lint-style`：检查目录、命名、消息 ID、禁止事项是否符合 uvm-style.md | P2 | |
| U-09 | 生成的组件自带 uvm-style.md §5.2 要求的打印；`new test` 同时在 case 中加入对应的 `expect` | P0 | 与判定、分诊直接衔接 |
| U-10 | `solouvm uvm diff`：预览 `uvm sync` 将改动哪些文件 | P2 | |

## 22. 自测与质量（Q）

| 编号 | 需求 | 优先级 | 备注 |
|------|------|--------|------|
| Q-01 | 用假仿真器覆盖全流程：通过、UVM_ERROR、UVM_FATAL、缺 Summary、非零退出码、挂死、段错误、非 UVM 标记 | P0 | |
| Q-02 | `run_job.sh` 的三种超时实现在 Linux 与 macOS 上都有真实测试 | P0 | |
| Q-03 | ssh provider 用本机 sshd 或容器做集成测试 | P1 | |
| Q-04 | CI 矩阵：Python 3.10–最新，Linux + macOS | P1 | |

## 23. 不做

| 事项 | 理由 |
|------|------|
| YAML / JSON / TOML 配置 | 违反 N-01 |
| 在 B 上做判定、聚合 | 违反 N-10 |
| 波形、覆盖率数据库回传到 A | 体积大，留在 B 查看即可 |
| 交互式调试：拉起 Verdi / SimVision 等 GUI | 交给人在 B 上自行使用；只读的文本化波形查询见 AI-13 |
| 替代仿真器自身功能（编译选项含义、波形格式） | 只做编排 |
| IP 依赖 / 包管理 | 交给 FuseSoC / Bender，经 F-06 导入其 `.f` |
| lint、formal、综合流程 | 超出仿真回归范围；E 区的 slang 检查只用于快速报错 |
| 通知（邮件、IM） | 用 `post_hook_judge` 实现 |

## 24. 待决问题

以下问题均已于 2026-11-03 按推荐答案确认，结论已同步到对应需求。这里保留推理过程，后续整理进 `doc/decisions.md`。新的待决问题追加在末尾。

### Q1 事实文件的格式 —— 已定（2026-11-03）

**推荐**：`key=value` 纯文本，一行一项，文件名 `job.facts`。

```text
schema=1
run_id=20261103-101500-a1b2
job_id=burst__4417
host=simbox01
timeout_impl=gtimeout
started_at=2026-11-03T10:15:02+0800
state=running
```

job 结束时追加：

```text
state=done
rc=0
signal=
timed_out=0
env_fail=0
finished_at=2026-11-03T10:21:40+0800
duration_s=398
```

**理由**：
- shell 用 `printf` 就能写，不用处理 JSON 转义。
- 开始时写一段、结束时追加一段：只有前半段说明 job 中途死了（X-05），不用另外写"pending"文件。
- 同名键以最后一次出现为准，A 端解析只需十几行。

**约束**：值里不允许出现换行；所有值由 `run_job.sh` 自己产生，不含用户输入（N-12）。

**影响**：X-04、X-05、X-06、N-13。

### Q2 provider 配置写在哪里 —— 已定（2026-11-03）

**推荐**：两处都写，各管一层。

| 位置 | 写什么 | 是否入库 |
|------|--------|----------|
| `CFG["providers"]` | 团队共享的命名 provider，例如 `"farm": {"kind": "lsf", "queue": "normal"}` | 入库 |
| `solouvm.local.py` | 个人选择用哪个，以及个人字段：主机、用户名、远端目录 | 不入库 |
| 命令行 `--provider NAME` | 临时切换 | — |

优先级：命令行 > 本地覆盖 > `CFG`；都没写时为 `local`。

**理由**：队列、资源参数是团队共识，应该入库；主机名和远端目录因人而异，入库会互相覆盖。

**影响**：C-06、H-01 至 H-05、H-11、L-05。

### Q3 一次 regress 的时序 —— 已定（2026-11-03）

**推荐**：默认阻塞。A 推送、启动后一直等待，期间显示进度（H-13），全部结束后自动 `judge`。

- `--detach`：启动后立即返回，之后用 `status` 查看进度，用 `judge` 出结果（Z-06）。
- 阻塞期间按 Ctrl-C：终止 B 上的全部 job，然后对已完成的部分照常判定，未完成的记为 incomplete，退出码 130。
- detach 之后想停：`solouvm stop`。

**理由**：日常交互最常见的是"跑完给我结果"；夜间回归、CI 用 detach。

**影响**：H-10、H-13、X-13、Z-04、Z-06；L 区新增 `stop` 命令。

### Q4 fast compile 是否做增量 —— 已定（2026-11-03）

**推荐**：slang 永远全量，不做增量；增量编译（E-06 至 E-08）只针对 vcs / xrun，保持 P2。

先实现 E-01 至 E-03，在实际工程上测出全量 slang 的耗时，写进 `doc/fast-compile.md`。只有当实测超过 30 秒时，再评估 slang 增量。

**理由**：slang 解析一般很快，增量带来的依赖分析复杂度远大于收益；真正慢的是商用仿真器的编译和 elab。

**影响**：E-06 至 E-08。

### Q5 tb 能否直接引用 DUT 层次路径 —— 已定（2026-11-03）

**推荐**：允许，但限定位置。层次引用（如 `tb_top.dut.u_core.state`）和 `bind` 只能写在 `top` 组的文件里。

文件分三组：

| 组 | 内容 | 参与 fake_top 编译 |
|----|------|--------------------|
| `rtl` | 设计代码 | 否 |
| `tb` | UVM 包、agent、sequence、interface | 是 |
| `top` | `tb_top`、`bind`、层次引用 | 否 |

fake_top 编译 `tb` 组时，如果报出找不到层次路径的错误，提示用户把相关代码挪到 `top` 组（E-05）。

**理由**：完全禁止层次引用不现实；限定位置之后，fake_top 只编译 `tb` 组就一定能过。

**影响**：F-04（分组从两组改为三组）、E-04、E-05。

### Q6 Hook 阶段清单 —— 已定（2026-11-03）

**推荐**：

| 位置 | 阶段 | 形式 |
|------|------|------|
| A | `main`（包住整条命令）、`gen`、`push`、`build`、`run`、`judge` | `pre_hook_<阶段>.py` / `post_hook_<阶段>.py`，函数 `hook(ctx)` |
| B | `pre_job`、`post_job` | `CFG` 里写 shell 片段，生成进 `run_job.sh`（W-04） |

**理由**：A 端阶段与命令流水线一一对应；B 端没有 Python，只能是 shell 片段。

**影响**：W-01、W-04。

### Q7 case 记录的 ID —— 已定（2026-11-03）

**推荐**：`Bug` 必须写显式整数 `id=`，在同一个 case 内唯一，完整 ID 为 `<case>#<id>`，例如 `burst#3`。`Todo`、`Log`、`Note` 的 `id` 可选。

- 加载时检查是否重号（M-05）。
- `solouvm notes --next-id burst` 打印下一个可用编号，免得手数。

**理由**：显式编号绝对稳定，人也好记、好口头引用（"burst 3 号 bug"）；"日期 + 序号"同一天插入仍然会变。

**影响**：M-04、M-05、M-12。

### Q8 命令行输出的语言 —— 已定（2026-11-03）

**推荐**：命令行输出、错误信息、日志一律英文；`doc` 导出的 markdown 由 `CFG["doc"]["lang"]` 决定，默认中文。

**理由**：英文输出便于 grep、贴进 issue、和仿真器自身的英文日志混在一起看；导出文档是给人读的，用团队的语言。

**影响**：N-09、M-08。

### Q9 选择条件匹配不到任何 job —— 已定（2026-11-03）

**推荐**：退出码 2，并提示最接近的 case 名或 tag。加 `--allow-empty` 时返回 0。

**理由**：CI 里 tag 拼错时，返回 0 会让一次什么都没跑的回归显示为通过，这是最危险的静默失败。

**影响**：N-07、T-09、T-10、L-06。

### Q10 运行历史放在哪里 —— 已定（2026-11-03）

**推荐**：放在项目内 `.solouvm/runs/<run_id>/`，整个 `.solouvm/` 加入 `.gitignore`；用 Y-06 的保留策略控制体积。

**理由**：历史跟着项目和分支走，多个工作副本互不干扰，删项目即清理；放用户目录会让不同项目、不同分支的历史混在一起。

**影响**：Y-01、Y-06、X-17。

### Q11 case 记录的存放形式 —— 已定（2026-11-03）

**推荐**：数据模型只有一种，即 `Case(notes=[...])`；文件组织支持两种：

- 小项目：全部写在 `case.py`。
- 大项目：每个 case 一个文件 `cases/<name>.py`，导出 `CASE = Case(...)`；`CFG["case_dirs"] = ["cases"]` 后自动收集，不用在 `case.py` 里逐个 import。

推荐约定：`notes=[...]` 写在 `Case(...)` 的最后一个参数。M-14（命令行自动写回）仍为 P3，等这个约定用一段时间后再评估。

> 后续修订：引入 AI 分诊后，M-14 提升为 P0，见 decisions.md D30。

**理由**：一个 case 一个文件，git 冲突少、记录多了也好找；约定好 `notes` 的位置，为以后安全地自动写回留出余地。

**影响**：M-02、M-14、M-15、T-01。

## 25. 里程碑归属

未列出的编号随所在区域的整体安排；有冲突时以本表为准。

| 里程碑 | 需求 |
|--------|------|
| M1 | N-01 ~ N-13、N-15 ~ N-22；C-01 ~ C-05；F-01 ~ F-04、F-04a、F-06、F-07、F-10；T-01 ~ T-03、T-06、T-08 ~ T-12、T-17、T-18；D-01、D-03、D-04；G-01 ~ G-06；X-01 ~ X-10、X-13；H-01、H-01a、H-02、H-07、H-12；J-01 ~ J-12、J-15、J-18；R-02；K-01 ~ K-04、K-11；S-01、S-02、S-06 ~ S-09；L-01、L-05、L-06、L-08 ~ L-11；M-01 ~ M-07、M-05a、M-14 ~ M-19、M-23 ~ M-25；AI-01 ~ AI-12、AI-14 ~ AI-16；Y-01、Y-02；Z-01、Z-05、Z-07、Z-10；U-01 ~ U-03、U-06、U-07、U-09；Q-01、Q-02、Q-04 |
| M2 | N-14；C-06、C-08；F-05；T-04、T-05、T-07、T-16；D-02、D-05；X-11、X-16、X-17；H-03 ~ H-05、H-08 ~ H-11、H-13；J-14、J-16、J-19；R-01、R-03、R-04；K-05 ~ K-07、K-12；S-03、S-04；W-04；M-11 ~ M-13、M-22；Y-03 ~ Y-06；Z-02 ~ Z-04、Z-06、Z-11；U-04、U-05；Q-03 |
| M3 | E-01 ~ E-05、E-09；L-02；U-08、U-10 |
| M4 | F-08；T-13、T-14；D-06；X-12、X-14、X-15、X-19；H-15；J-13、J-17；K-08；V-01 ~ V-04；S-05；L-03、L-04；M-08 ~ M-10、M-20、M-21；AI-13；Z-12 |
| 待定 | C-07；F-09；T-15；E-06 ~ E-08、E-10；X-18；H-06、H-14；K-09、K-10；V-05；W-01 ~ W-03；AI-17、AI-18；Z-08、Z-09；L-07 |
