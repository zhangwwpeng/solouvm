# solouvm

SystemVerilog / UVM 仿真回归的 Python Runner。

> **状态：M1 ~ M4 核心与周边功能已实现。** 目前已通过内置 `fake` 仿真器端到端闭环及全套自动化测试；`requirements.md §25` 中的 `待定` 需求未实现，且 `requirements.md §26` 中的真实 EDA/集群工具细节尚待真实环境实测核验。需求见 [doc/requirements.md](doc/requirements.md)，使用手册见 [doc/guide/README.md](doc/guide/README.md)，进度见 [todo.md](todo.md)。

你在控制机上用 Python 写三个文件：项目配置、编译文件清单、case 列表。solouvm 生成一套只依赖 `make` 和 `bash` 的编译与运行脚本，送到执行机上跑仿真，再把结果读回控制机判定、汇总，最后给出一份报告和一个确定的退出码。

控制机和执行机可以是同一台，也可以是笔记本加远端服务器、集群提交节点加 LSF。

## 安装与更新

控制机只需 Python >= 3.10（零运行时第三方依赖）。分发时直接拷贝整个 `solouvm` 目录，用户的验证项目目录下无需存放或安装 `solouvm` 库，只调用 `solouvm` CLI：

```bash
./install.sh                  # 当前用户安装（~/.local/lib/solouvm 与 ~/.local/bin/solouvm，预编译 .pyc 并检查 .zshrc/.bashrc PATH）
sudo ./install.sh --system    # 系统级安装（/usr/local/lib/solouvm 与 /usr/local/bin/solouvm，全用户可读可执行）
```

升级方式：离线直接用新版目录重新执行 `./install.sh`；联网可执行 `solouvm update`（默认拉取 `https://github.com/zhangwwpeng/solouvm`，支持 `--repo URL`、`--ref REF`、`--user`/`--system`/`--prefix`）。详见 [doc/guide/01-install.md](doc/guide/01-install.md)。

## 1. 核心思路

```text
  A：控制机（装 solouvm）                          B：执行机（不装 solouvm）
 ┌────────────────────────────────┐              ┌───────────────────────────────────┐
 │ solouvm.conf.py / filelist.py  │              │                                   │
 │ case.py / tbspec.py            │              │                                   │
 │        │ gen                   │    push      │  .solouvm/gen/bin/run_job.sh      │
 │        ▼                       │ ───────────▶ │  .solouvm/runs/<run_id>/run.mk    │
 │ run.mk、脚本、文件清单、plan   │              │  文件清单 + 源码                  │
 │                                │    run       │        │ make                     │
 │                                │ ───────────▶ │        ▼                          │
 │                                │              │  runs/<run_id>/<job>/try<N>/      │
 │ judge：判定 + 汇总 + 报告      │ ◀─────────── │    sim.log  job.facts（事实）     │
 │ results.json / JUnit / 终端    │    read      │  波形、覆盖率库（留在 B）         │
 └────────────────────────────────┘              └───────────────────────────────────┘
```

四条分工：

1. **A 负责想，B 负责跑。** 收集、生成、判定、报告都在 A 上；B 只编译、执行，并记录事实。
2. **B 上什么都不用装。** 只需要 `make`、`bash`、POSIX 工具和仿真器，不需要 Python，也不需要 solouvm。
3. **B 只记录事实，不下结论。** 每个 job 结束时写下退出码、信号、是否超时、耗时。pass / fail 由 A 读日志后判定，所以改了判定规则不用重跑仿真。
4. **大文件不搬家。** 波形和覆盖率库永远留在 B，A 只流式读取日志和事实文件。

"怎么到 B、怎么在 B 上跑"由两类可替换的插件负责：

| 插件 | 职责 | 实现 |
|------|------|------|
| provider | 在哪台机器、用什么方式执行（实现上分为 transport 与 launcher） | `local`、`ssh`、`lsf`，可叠加，如 `ssh → lsf` |
| sync | 代码和生成物怎么到 B | `none`（共享盘）、`rsync`、`git` |

## 2. 人与 AI 协作

solouvm 由人和 AI 共用同一套命令，所有命令都支持 `--json`。以 AI 为主力时的标准流程：

1. **主 agent 写 case**：除了参数，还要写清 `intent`（验证什么）、`method`（怎么激励、怎么检查）、`expect`（通过时一定能观察到的现象）；`solouvm validate` 自检。
2. **提交回归**：`solouvm regress`，执行并机械判定；`expect` 不满足的 job 直接判失败，拦截最常见的虚假 pass。
3. **子 agent 分诊**：`solouvm triage plan` 把失败（按签名合并）和可疑的通过拆成任务；每个任务交给一个子 agent。子 agent 读系统文档、case 文档、代码、日志、波形，判断"是否虚假 pass、失败原因在哪"，登记 bug、写调试文档后退出。
4. **主 agent 审查**：`solouvm triage review` 接受或驳回每个结论，然后修 case、修 RTL 或签核。

子 agent 只有读权限，以及经 solouvm 命令追加记录的权限：它可以把通过判为虚假 pass，但永远不能把失败判为通过，也不能修改 case 的判定条件。详见 [doc/ai-workflow.md](doc/ai-workflow.md)。

## 3. 设计原则

- **Python 是唯一的配置语言**：配置就是 Python 模块，不解析 YAML / JSON / TOML。
- **A 上不需要安装任何依赖**：Python >= 3.10 加上 solouvm 自身即可运行；rich 等第三方库以 vendor 方式内置，拷过去就能用。
- **人看得舒服，AI 读得准确**：终端里用 rich 输出彩色日志、表格和进度条；`--json` 或被程序读取时输出纯文本或 JSON。
- **所有 UVM 环境同一个风格**：agent、env、test、sequence 一律由模板生成，AI 和人只在生成的结构里填逻辑，规范见 [doc/uvm-style.md](doc/uvm-style.md)。
- **生成物可以独立使用**：生成的 `Makefile` 拿到任何一台有 `make` 的机器上都能跑，不依赖 solouvm。
- **只用相对路径**：项目目录可以搬到另一台机器、另一个路径下直接使用。
- **Linux 与 macOS 都支持**：平台差异在生成的脚本里于运行时处理。例如超时依次尝试 `timeout`、`gtimeout`、内置 bash 实现。
- **判定可预期**：规则固定、按优先级执行。挂死、被信号杀死、日志被截断、读到上一轮的残留结果、编译失败后拿旧产物跑，都不会被误判为 pass。
- **出错就停，并说清楚原因**：不吞异常，不静默降级，报错时附带下一步建议。

## 4. 使用示意

以下是目标形态的示例，细节可能随设计调整。

`solouvm.conf.py`：

```python
CFG = {
    "project": "myproj",
    "simulator": "vcs",
    "top": "tb_top",
    "regress": {"seeds": "random:3", "jobs": 16},
    "run": {"timeout": "30m"},
    "checks": {"max_uvm_error": 0, "fail_regex": ["TEST FAILED"]},
    "providers": {                     # 团队共享的命名 provider，入库
        "farm": {
            "launcher": {"kind": "lsf", "queue": "normal"},
            "setup": "module load vcs",
        },
    },
}
```

`solouvm.local.py`（个人配置，不入库，深度合并到 `CFG` 上）：

```python
LOCAL = {
    "provider": "farm",
    "providers": {
        "farm": {
            "transport": {"kind": "ssh", "host": "simbox", "workdir": "/work/wp/myproj"},
            "sync": {"kind": "rsync"},
        },
    },
}
```

`filelist.py`：

```python
FILES = {
    "rtl": ["rtl/**/*.sv", "-f ip/vendor_ip.f"],              # 设计代码，可导入已有 .f
    "tb": [                                                   # UVM 包、agent、interface
        "+incdir+verif",
        "verif/agents/*/*_if.sv",
        "verif/agents/*/*_pkg.sv",
        "verif/envs/*/*_env_pkg.sv",
        "verif/tests/*/*_test_pkg.sv",
    ],
    "top": ["verif/tb/tb_top.sv", "verif/tb/binds.sv"],       # 顶层、bind、DUT 层次引用
}
```

按这个布局（见 [doc/uvm-style.md](doc/uvm-style.md)），新增 agent、env、test 后不用改 `filelist.py`。

`case.py`：

```python
from solouvm import Case

CASES = [
    Case(name="smoke", uvm_test="axi_smoke_test"),                      # 新 case 默认 stage="wip"
    Case(name="burst", uvm_test="axi_burst_test", params={"NUM": 32}, tags=("nightly",)),
    Case(name="len0", uvm_test="axi_len0_test",
         expect_fail=r"\[AXI_SLV_MON\] illegal length 0"),              # 负向测试，写明期望的错误
]
```

case 本身就是文档、进度表和 bug 登记表：说明、负责人、开发阶段、todo、bug、调试记录都写在 case 里，不用另外维护文档。

```python
Case(
    name="burst",
    uvm_test="axi_burst_test",
    owner="wp",
    stage="ready",                  # planned / wip / ready / signoff；ready 起必须写全验收信息
    intent="验证 WRAP burst 跨 4K 边界时的地址回绕",
    method="随机 WRAP 读写；scoreboard 逐笔比对，SVA 检查地址",
    spec_refs=["AXI4 A3.4.1"],
    expect=[Expect(scoreboard="AXI_SCB", min=100)],              # 机械拦截虚假 pass
    doc="验证 AXI burst：INCR / WRAP / FIXED，长度 1–256。",
    notes=[
        Bug(id=1, title="WRAP 跨 4K 边界时地址回绕错误", status="open", seed=4417, found="2026-11-02"),
        Todo("补 narrow transfer 场景"),
        Log("2026-11-02", "4417 必现；addr_q 在第二拍回绕成 0"),
    ],
)
```

日常使用：

```bash
solouvm check                       # 几秒内用 slang 检查语法
solouvm regress --tag nightly       # 生成 → 推到 B → 编译 → 跑 → 读回 → 判定 → 报告
solouvm rerun --failed              # 用相同种子重跑上一轮失败的 job
solouvm judge                       # 不重跑，只按当前规则重新判定
solouvm notes                       # 列出所有 case 中未解决的 bug 与 todo
solouvm doc -o docs/cases           # 每个 case 导出一份 markdown，附总索引
```

不想让 solouvm 托管执行时，也可以手动操作：

```bash
solouvm gen                         # A：只生成
# 把项目目录交给 B 或调度系统，在 B 上：
make build && make -j16 regress
# 回到 A：
solouvm judge
```

## 5. 已实现命令

| 命令 | 作用 |
|------|------|
| `init` | 生成起步项目：配置、示例 UVM 环境、示例 case，开箱可跑 |
| `new agent\|env\|test\|seq\|vseq` | 按统一模板生成 UVM 组件 |
| `uvm sync` | 按模板与 `tbspec.py` 重新生成组件，保留用户代码区 |
| `gen` | 收集文件和 case，生成全部产物 |
| `build` | 生成，并在 B 上编译 |
| `run` / `regress` | 生成、编译，跑选中的 / 全部 job，然后判定 |
| `judge` | 读取事实文件与日志，判定、汇总、出报告；可反复执行 |
| `rerun` | 重跑上一轮失败的 job |
| `list` | 列出将要执行的 job，不生成、不执行 |
| `check` | fast compile：slang 检查、UVM 侧 fake_top 编译、RTL 增量编译 |
| `notes` | 跨所有 case 查询记录，默认列出全部未解决的 bug 与 todo |
| `doc` | 把每个 case 的说明与记录导出为 markdown，附总索引 |
| `progress` | 验证进度总览：各阶段 case 数、通过率、按 owner 统计的未解决 bug 与 todo |
| `doctor` | 检查控制机与执行机的环境 |
| `status` / `log` / `stop` | 查看进行中的回归 / 读取执行机上的日志 / 终止本轮回归 |
| `validate` | 只校验配置、case、记录，不生成任何文件 |
| `explain` | 一次给出排查一个 job 所需的全部信息 |
| `triage plan / packet / write / review / status` | 分诊：生成任务、取上下文包、写结论、审查、查看进度 |
| `bug add / update`、`note add`、`todo add / done` | 经命令行写入 case 记录 |
| `wave` | 以文本方式查询波形中的信号值 |
| `cov merge / report` | 在执行机 B 合并并汇总覆盖率 |
| `clean` | 清理生成物与运行目录 |
| `update` | 联网从 Git 仓库拉取新版并执行安装更新 |

以上是主要命令，完整 43 个子命令参数与使用方式见 [doc/guide/10-cli-reference.md](doc/guide/10-cli-reference.md) 与 [doc/cli.md](doc/cli.md)。

退出码：0 全部通过；1 有测试失败；2 配置、环境或编译错误；130 被中断。

## 6. 不做什么

- 不解析 YAML / JSON / TOML 配置。
- 不在执行机上做判定。
- 不把波形、覆盖率库回传到控制机。
- 不管交互式调试：Verdi 等 GUI 在执行机上自行使用；只提供只读的文本化波形查询。
- 不做 IP 依赖管理：交给 FuseSoC / Bender，再导入它们输出的 `.f`。
- 不做 RTL lint、formal、综合流程（`lint-style` 只检查 UVM 环境是否符合风格规范）。

## 7. 文档

| 文档 | 内容 |
|------|------|
| [doc/guide/README.md](doc/guide/README.md) | 中文使用手册（安装、快速上手、配置、Case、分诊、UVM 脚手架、CLI、排错） |
| [skills/solouvm/SKILL.md](skills/solouvm/SKILL.md) | 供 AI Agent 使用的 `solouvm` 工作流与角色权限技能说明 |
| [doc/requirements.md](doc/requirements.md) | 全部功能需求、优先级、已决问题 |
| [doc/architecture.md](doc/architecture.md) | 流水线、模块划分、目录布局、生成物、provider 接口 |
| [doc/config.md](doc/config.md) | `solouvm.conf.py` / `solouvm.local.py` / `filelist.py` / `case.py` 完整参考 |
| [doc/cli.md](doc/cli.md) | 全部命令、参数与退出码 |
| [doc/verdict.md](doc/verdict.md) | 判定规则、waiver、expect_fail、retry、失败签名 |
| [doc/fast-compile.md](doc/fast-compile.md) | slang 检查、fake_top、RTL 增量编译 |
| [doc/formats.md](doc/formats.md) | `job.facts`、`plan.json`、`results.json` 等文件格式 |
| [doc/ai-workflow.md](doc/ai-workflow.md) | 主 agent / 子 agent 分诊流程、权限边界、上下文包 |
| [doc/uvm-style.md](doc/uvm-style.md) | UVM 环境风格规范：目录、命名、消息约定、用户代码区 |
| [doc/decisions.md](doc/decisions.md) | 设计决策记录：理由、代价、备选 |
| [todo.md](todo.md) | 实现进度 |

M1 ~ M4 代码、文档与自测已就绪，细节以 `doc/` 为准，待真实 EDA 环境验证项见 [doc/requirements.md §26](doc/requirements.md#26-不确定事项)。

README 是总纲，只写结论与边界；细节以 `doc/` 为准，二者冲突时修正其一，不留两套说法。

## 8. 许可证

MIT，见 [LICENSE](LICENSE)。
