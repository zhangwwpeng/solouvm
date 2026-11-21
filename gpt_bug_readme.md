# solouvm 全工程审查报告

审查对象为 2026-11-20 的 `m1` 工作区，基准提交 `a860223`。审查包含当时已有的未提交源码。范围覆盖配置、文件收集、计划与编译缓存、Makefile 和 Shell 生成、仿真后端、Provider、判定、报告、历史、记录与分诊、UVM 生成、Fast Compile、覆盖率和波形，并对照 README、设计文档、用户手册与测试。第三方 `_vendor` 按集成范围检查。

本次记录 **18 项缺陷和边界问题，12 项经运行复现，1 项核对生成物确认，5 项由源码确认**。P0 表示可能让 CI 接受错误结果或损坏轮次记录，P1 表示主要流程中的错误结果、数据丢失或功能失效，P2 表示特定输入或运行环境中的异常。

## P0

### 01. 残缺的 UVM 摘要会把错误判成通过 · 已复现

在临时项目中用退出码为 0 的模拟命令输出一条 `UVM_ERROR`、满足 smoke case 的 scoreboard 与仿真时长要求，再输出只有 `UVM_INFO` 计数的摘要。`regress` 返回 0，结果为 `pass / clean`。扫描器只要读到任意摘要计数就切换到摘要字典，缺失的 `ERROR` 和 `FATAL` 取 0，同时把摘要标记为存在。应校验摘要完整性，并在残缺时保留正文中已确认的错误。 [扫描器](solouvm/judge/scan.py#L170) · [判定规则](solouvm/judge/rules.py#L265)

### 02. 重用 `SOLOUVM_RUN_ID` 会混合新旧运行产物 · 已复现

用相同 ID 先执行 `regress --seed 11`，再执行 `gen --seed 22`，两条命令均返回 0。`plan.json` 中只有 `smoke__22`，`results.json` 中仍是 `smoke__11`。生成目录接受已存在的 ID，且生成流程会替换计划、Makefile 和环境快照，旧结果与尝试目录仍可被后续命令读取。应原子预留轮次目录并拒绝重复 ID，包括并发生成。 [ID 分配](solouvm/pipeline.py#L90) · [产物写入](solouvm/pipeline.py#L182) · [轮次隔离要求](doc/requirements.md#8-执行x)

## P1

### 03. Git 同步指定分支时，B 可能编译另一份源码 · 已复现

使用临时 Git 仓库和本地裸仓库验证。A 在干净的 `feature` 分支，配置 `sync.branch=main`，同步成功后 B 的 `HEAD` 和文件内容仍对应 `main`。实现读取了 A 的 `HEAD`，但在指定分支时仅推送该分支并检出其远端引用。A 生成的编译键和历史快照描述的是 `feature`，B 可以据此缓存从 `main` 生成的产物。应校验 B 检出的提交与计划对应的 A 提交一致。 [Git 同步](solouvm/providers/sync_git.py#L75) · [环境快照](solouvm/pipeline.py#L193)

### 04. Verilator 编译成功后找不到可执行文件 · 已复现

用 Verilator 5.054 和只含 `initial`、`$display`、`$finish` 的最小 SV 顶层执行 `regress`。编译和链接成功，随后仿真以 127 退出，`sim.log` 显示编译目录下的 `Vsim` 不存在。该配置生成 `verilator --binary ... -o Vsim`，实际程序落在 `obj_dir`，运行命令读取编译目录下的 `Vsim`。应统一构建输出路径和运行路径，并用最小真实后端用例覆盖编译到运行。 [Verilator 后端](solouvm/backends/verilator.py#L9) · [`simv` 路径生成](solouvm/gen/makefile.py#L123)

### 05. Questa 编译步骤中的 `&&` 变成了普通参数 · 生成物已确认

`gen` 输出的 recipe 将 `vlib work '&&' vlog ... '&&' vopt ...` 传给 `run_job.sh`。执行脚本按 argv 数组启动命令，因此 `vlog` 和 `vopt` 不会作为独立命令运行。已检查生成物与执行器，当前环境无 Questa 实机。应显式表达多个顺序执行的编译步骤，并验证各步骤的退出码传播。 [Questa 后端](solouvm/backends/questa.py#L8) · [命令生成](solouvm/gen/makefile.py#L213) · [执行器](solouvm/gen/scripts.py#L471)

### 06. 旧轮次的分诊结论会自动套用到新轮次 · 已复现

对 `audit-a` 的 `smoke__11` 提交并接受 `false_pass`，再开 `audit-b` 执行同一个 case 和 seed。两轮机械结果都是 `pass`，`audit-b` 直接继承 `audit-a/1` 的 `false_pass`，退出码为 1。任务 ID 已包含 run ID，最终判定只匹配 job ID 和机械状态，新轮次的证据无法参与这项判断。应按轮次绑定审查记录，保证历史重判和新轮次各自使用对应证据。 [任务 ID](solouvm/triage/plan.py#L777) · [分诊结论选择](solouvm/judge/__init__.py#L613)

### 07. 连续生成 `.orphan` 会覆盖之前保存的用户代码 · 已复现

在同一 driver 先从模板移除有用户内容的 `build_phase`，执行 `uvm sync` 后，内容进入 `.sv.orphan`。再移除有用户内容的 `fields` 并同步，原 `.orphan` 只剩 `fields`，先前的 `build_phase` 内容丢失。应保留同一源文件的全部历史 orphan 内容，覆盖两次独立模板更新的场景。 [写入 `.orphan`](solouvm/uvmgen/sync.py#L344) · [U-05](doc/requirements.md#21-uvm-环境生成u)

### 08. `random:N` 的多个分片无法重组成同一份计划 · 已复现

分别执行 `gen --seeds random:4 --shard 1/2`、`--shard 2/2` 和无分片的生成命令。两片各有两个 seed，但两片合并与无分片计划的四个 seed 不同。每个进程先独立随机抽样，随后才按索引取分片。外部调度需要从同一份已展开的计划分片，或传入共享的随机序列标识。 [种子展开与分片](solouvm/plan.py#L312) · [T-16](doc/requirements.md#4-case-与计划t)

### 09. 项目绝对路径含空格时，生成的 Makefile 无法启动 job · 已复现

在 `with space` 目录中 `init` 后执行 fake 回归，返回 2，结果为 `build not run`。`RUN_JOB := bash $(ROOT)/.solouvm/gen/bin/run_job.sh` 将包含空格的路径交给 Shell 拆词，实际脚本未启动。应为运行脚本的绝对路径加引号，或从项目根用相对路径启动。 [Makefile 生成](solouvm/gen/makefile.py#L381)

### 10. VCD 中不同的数组元素会合并成一个信号 · 已复现

向解析器传入两个不同 ID 的 `$var wire 8 ... mem [0]` 和 `mem [1]`，在 0 ns 分别赋 `0x1`、`0x2`。查询 `tb.mem*` 只返回 `tb.mem=0x2`。解析器忽略 `reference` 后的索引，两个 ID 共用同一个值槽。应区分数组元素索引与向量位范围，并保留不同 ID 的独立值。 [VCD 变量注册](solouvm/wave.py#L232) · [值更新](solouvm/wave.py#L278)

### 11. Fast Compile 会跳过大小和 mtime 保持相同的源码变更 · 已复现

生成 `fast_state.json` 后将 `wire a` 改成等长的 `wire b`，再恢复原 mtime。增量规划返回 `files_to_compile=[]`、`changed_files=[]`。即便每次重新读取内容以分析依赖，复用 SHA-1 的条件仍只看大小和 mtime。保留时间戳的同步或文件替换可触发。常规编译键也采用同类元数据捷径，应一并验证同尺寸、同 mtime 的输入替换。 [Fast Compile 缓存检查](solouvm/check/incremental.py#L264) · [常规编译键缓存](solouvm/buildkey.py#L83)

### 12. 后续命令默认从当前 provider 读取已有轮次 · 源码确认

通过 `--provider` 在另一台执行机启动历史轮次后，`status`、`log`、`wave`、`stop` 和 `judge` 仍从当前配置加载 provider。即使 `judge` 已读到 `plan.provider`，它仍把当前 provider 传入判定，并重写本地 `results.json`。两台执行机的目录独立时，合法结果可能被覆盖为缺失或未编译。应优先使用用户显式选择，其次使用运行计划保存的 provider。 [当前配置选择](solouvm/cli.py#L70) · [状态命令](solouvm/cli.py#L512) · [历史轮次判定](solouvm/cli.py#L620)

## P2

### 13. `--budget` 只管首次前台 make，且中断构建会被记为编译错误 · 已复现及源码确认

将 build 命令设为休眠 5 秒，执行 `regress --budget 0.1s`。回归按预算中止，结果 `partial=true`，但 job 被报为 `error / build not run`，CLI 返回 2。另据源码，预算在首次前台 make 启动后计时，重试、失败补波形和 detach 路径都未收到预算。应明确预算起点并在各阶段使用同一个截止时间，用部分运行状态表示预算中止。 [计时与中止](solouvm/pipeline.py#L470) · [detach 与首次运行](solouvm/pipeline.py#L711) · [重试与补波形](solouvm/pipeline.py#L772) · [构建前置判定](solouvm/judge/rules.py#L162)

### 14. 非法 `--budget` 会绕过 CLI 的结构化错误输出 · 已复现

`regress --budget bogus --json` 返回 1，stdout 为空，stderr 输出 Python `ValueError` 堆栈。时长解析异常未转换成 CLI 可处理的 `ConfigError`。应让非法参数走与其他用法错误相同的退出码和 JSON 结构。 [参数解析](solouvm/cli.py#L419) · [顶层异常处理](solouvm/cli.py#L1136)

### 15. `--max-fail` 忽略退出码为 0 的机械失败 · 源码确认

在线统计只读取 `job.facts` 的退出码、信号、超时和挂死标记。内置 `uvm_error` 模式写出明确的 UVM 错误并退出 0，最终会判为 `fail`，在线失败计数却不增加。用一批此类 case 无法达到 `--max-fail` 阈值。X-12 要求 A 端在运行中判定，应复用已完成 job 的判定并缓存结果。 [在线计数](solouvm/pipeline.py#L402) · [阈值检查](solouvm/pipeline.py#L518) · [fake 仿真器](solouvm/backends/fake_sim.sh#L55) · [X-12](doc/requirements.md#8-执行x)

### 16. `results.json` 缺少字段级校验 · 源码确认

读取器只验证顶层对象及 schema 号。仍是合法 JSON 的异常文件，例如 `summary.duration_s` 是非数字字符串，会进入 JUnit 或 HTML 渲染器并触发转换异常。应在结果文件入口校验核心字段，并给出文件位置与重判提示。 [结果读取](solouvm/report/json.py#L217) · [JUnit 渲染](solouvm/report/junit.py#L15) · [HTML 渲染](solouvm/report/html.py#L47)

### 17. 计划加载时接受非法时长 · 源码确认

`read_plan()` 对 job 时长只检查 Python 类型并转成 `float`，会接受非有限值及负值。此类值可在读入后流入重跑计划或历史展示。应让持久化计划和配置统一校验时长，文件损坏时报告对应的 job 和字段。 [计划数值读取](solouvm/plan.py#L534) · [配置时长解析](solouvm/units.py#L16)

### 18. SSH 轮询缺少默认时限，回归预算也会被阻塞 · 源码确认

项目支持通过 `ssh_opts` 配置连接参数。默认传输调用没有连接和操作时限。回归循环先同步读取远端 facts，再检查预算，远端命令阻塞时不会走到预算检查。应为短查询和文件操作设置可配置期限，并区分长任务的超时策略。 [SSH 批量读取](solouvm/providers/transport_ssh.py#L281) · [轮询顺序](solouvm/pipeline.py#L504) · [SSH 配置](doc/config.md#28-providers)

## 既有报告复核与验证范围

- `bug_readme.md` 中关于 CLI `--provider` 使 `setup` 遗漏出编译键的结论需要收窄。实测 `--provider local gen` 与带额外 `setup` 的 `--provider alt gen` 生成了不同的键，CLI 已把所选 provider 合并到 `config.provider`。直接调用管线、让 `Config.provider` 与 `Plan.provider` 不同，仍会让编译键和 recipe 取不同的配置。 [CLI 配置合并](solouvm/cli.py#L70) · [编译键选择](solouvm/buildkey.py#L242) · [recipe 选择](solouvm/gen/makefile.py#L375)
- `report --compare` 使用最终状态 `status`，符合面向用户汇总最终结果的用法。机械判定对比属于尚未定义的另一种比较口径。原报告中的同主机编译锁回收、SSH 帧对换行文件名的限制和计划路径边界检查属于特定环境或产物校验边界。 [结果比较](solouvm/report/json.py#L251) · [编译锁](solouvm/gen/scripts.py#L362) · [SSH 帧](solouvm/providers/transport_ssh.py#L281) · [计划字段](solouvm/plan.py#L585)
- 环境为 Python 3.13.4、GNU Make 3.81、Verilator 5.054。全量 `pytest` **802 项通过**，`ruff check --no-cache solouvm tests` 通过。补充验证在临时目录运行。VCS、Xcelium、Questa、Icarus、slang、LSF、商业覆盖率工具和真实 SSH 执行机未做实机回归。
- 顺带检查了本地被 Git 忽略的 `dist/solouvm-0.1.0-py3-none-any.whl`。它包含 CLI 入口声明，但包内没有 `solouvm/cli.py`、fake 后端或 UVM 模板。该归档早于本次工作区，结果只适用于现有归档。本次未重新构建 wheel。 [打包配置](pyproject.toml#L13)

## 验证命令

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -o addopts='' --collect-only -q -p no:cacheprovider
.venv/bin/ruff check --no-cache solouvm tests
```
