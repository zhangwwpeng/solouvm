# 配置参考

本文是 solouvm 全部用户配置的完整参考：项目配置 `solouvm.conf.py`、个人覆盖 `solouvm.local.py`、文件清单 `filelist.py`、case 定义 `case.py`。需求编号见 [requirements.md](requirements.md)。

> 状态：设计稿，尚无代码。键名与默认值在实现前可能调整，调整时同步本文。

## 1. 文件与查找

| 文件 | 导出 | 是否入库 | 作用 |
|------|------|----------|------|
| `solouvm.conf.py` | `CFG = {...}` | 入库 | 项目配置；它所在的目录即项目根 |
| `solouvm.local.py` | `LOCAL = {...}` | 不入库 | 个人覆盖：用哪个 provider、主机、远端目录、并行数 |
| `filelist.py` | `FILES = {...}` 或 `files()` | 入库 | 编译文件，分组 |
| `case.py` | `CASES = [...]` 或 `cases()` | 入库 | case 定义及其记录 |
| `cases/*.py` | 每个文件 `CASE = Case(...)` | 入库 | 可选：一个 case 一个文件（M-15） |
| `tbspec.py` | `ENVS = [...]` | 入库 | UVM 环境描述，供 `solouvm new` / `uvm sync` 生成组件（U-02），见 §6 |
| `pre_hook_<阶段>.py` / `post_hook_<阶段>.py` | `hook(ctx)` | 入库 | A 端 hook，可放项目根目录或 `hooks/` 下；支持 `main`/`gen`/`push`/`build`/`run`/`judge`，可用 `--no-hooks` 跳过；`ctx.log()` / `print()` 归入阶段视口随成功收起，`ctx.echo()` / `solouvm.echo()` 永久显示在终端（W-01..W-03） |

规则：

- solouvm 从当前目录逐级向上查找 `solouvm.conf.py`；`-c PATH` 可指定文件或其所在目录（C-01）。
- 项目根出现 `solouvm.py` 时直接报错：它会遮蔽 solouvm 库本身（C-05）。
- 所有配置文件都是普通 Python 模块，可以 import 项目里的其他模块、读环境变量、做计算。solouvm 只看最终导出的值。
- 配置里的相对路径一律相对项目根。

## 2. `solouvm.conf.py`

### 2.1 完整示例

```python
from solouvm import Waiver

CFG = {
    "project": "axi_vip",
    "simulator": "vcs",
    "top": "tb_top",
    "uvm_test": None,
    "incdirs": ["ip/common/include"],
    "defines": ["SIM"],

    "builds": {
        "default": {},
        "gate": {"defines": ["GATE_SIM"], "compile_opts": ["+delay_mode_zero"]},
        "cov":  {"coverage": True},
    },

    "regress": {
        "seeds": "random:1",
        "jobs": 16,
        "retry": 1,
        "wave_on_fail": "per_signature",
        "max_fail": None,
        "budget": None,
    },

    "run": {
        "timeout": "30m",
        "hang": "10m",
        "opts": ["+UVM_VERBOSITY=UVM_LOW"],
        "env": {"AXI_VIP_DATA": "${PROJ_DATA}/axi"},     # 写进生成物，在 B 上展开
    },

    "checks": {
        "max_uvm_error": 0,
        "max_uvm_fatal": 0,
        "max_uvm_warning": None,
        "fail_regex": ["TEST FAILED"],
        "pass_regex": [],
        "env_fail_regex": ["Failed to obtain license"],
        "waivers": [
            Waiver(id="AXI/OUTSTANDING", reason="vendor VIP 已知告警，见 JIRA-88"),
        ],
    },

    "job": {
        "pre": "ulimit -c 0",
        "post": "",
    },

    "providers": {
        "farm": {
            "launcher": {"kind": "lsf", "queue": "normal", "resources": "rusage[mem=8000]"},
            "setup": "module load vcs/2025.06",
        },
    },

    "check": {"slang": "slang", "uvm_src": "${UVM_HOME}/src", "exclude": ["ip/encrypted/**"]},
    "case_dirs": ["cases"],
    "system_doc": ["doc/verif/system.md"],
    "triage": {"pass_policy": "changed", "packet_limit": 20000, "debug_dir": "docs/debug"},
    "doc": {"lang": "zh", "out": "docs/cases"},
    "history": {"keep": 50},
    "limits": {"max_jobs": 2000},
    "plugins": [],
}
```

### 2.2 顶层键

| 键 | 类型 | 默认值 | 说明 | 需求 |
|----|------|--------|------|------|
| `project` | str | 项目根目录名 | 出现在报告与导出文档中 | |
| `simulator` | str | 必填 | 内置后端名：`vcs` / `xrun` / `questa` / `iverilog` / `verilator` / `fake`；其他名字需在 `sim` 中给全命令 | S |
| `top` | str | 必填 | 顶层模块名，`{top}` 占位符取此值 | |
| `uvm_test` | str \| None | `None` | case 未写 `uvm_test` 时的默认值 | |
| `incdirs` | list[str] | `[]` | 全局 `+incdir+`，作用于所有分组 | |
| `defines` | list[str] | `[]` | 全局 `+define+`，写法 `"NAME"` 或 `"NAME=VALUE"` | |
| `builds` | dict | `{"default": {}}` | 编译配置，见 2.3 | D-02 |
| `regress` | dict | 见 2.4 | 回归参数 | |
| `run` | dict | 见 2.5 | 单个 job 的运行参数 | X |
| `checks` | dict | 见 2.6 | 判定参数 | J |
| `job` | dict | `{}` | B 端 hook：`pre` / `post` shell 片段 | W-04 |
| `sim` | dict | `{}` | 覆盖或自定义后端，见 2.7 | S-07 |
| `providers` | dict | `{}` | 命名 provider，见 2.8 | H-01a |
| `provider` | str | `"local"` | 默认使用哪个 provider；通常留给 `solouvm.local.py` 设置 | |
| `check` | dict | 见 2.9 | fast compile 参数 | E |
| `case_dirs` | list[str] | `[]` | 自动收集 `cases/*.py` 的目录 | M-15 |
| `system_doc` | list[str] | `[]` | 项目级验证说明文档，附在每个分诊上下文包中 | AI-03 |
| `triage` | dict | 见 2.10 | 分诊策略 | AI-05 |
| `doc` | dict | `{"lang": "zh", "out": "docs/cases"}` | 导出文档的语言与默认输出目录 | M-08 |
| `history` | dict | `{"keep": 50}` | A 端保留最近多少轮结果 | Y-06 |
| `limits` | dict | `{"max_jobs": 2000}` | 单条命令最多派发的 job 数，超出需加 `--force` | T-18 |
| `plugins` | list[str] | `[]` | 第三方 provider / 后端的导入路径，如 `"myflow.SlurmLauncher"` | H-15 |
| `uvm` | dict | `{"templates": None}` | 项目覆盖的 UVM 模板目录；整个项目只能有一套（U-06） | U-06 |

未知键直接报错，并提示最接近的合法键（C-04）。

### 2.3 `builds`

每项是一套独立的编译配置，编译产物放在 `.solouvm/build/<名字>/<编译键>/`（D-05）。编译键相同且已编译成功时直接复用（D-06）。

| 键 | 类型 | 默认值 | 说明 |
|----|------|--------|------|
| `defines` | list[str] | `[]` | 追加到全局 `defines` |
| `incdirs` | list[str] | `[]` | 追加到全局 `incdirs` |
| `compile_opts` | list[str] | `[]` | 追加到后端编译命令 |
| `run_opts` | list[str] | `[]` | 用这套编译的 job 运行时追加的参数 |
| `coverage` | bool | `False` | 打开后端定义的覆盖率编译、运行参数（V-01） |
| `timeout` | str | `"2h"` | 编译超时 |

`default` 总是存在；case 不写 `build` 时使用它。

### 2.4 `regress`

| 键 | 类型 | 默认值 | 说明 | 需求 |
|----|------|--------|------|------|
| `seeds` | list \| str | `"random:1"` | 列表：固定种子；`"1-5,42"`：范围；`"random:N"`：随机 N 个。不接受裸整数，避免 `3` 与 `[3]` 混淆 | T-06 |
| `jobs` | int | `4` | 并行数（`make -j`） | X-08 |
| `retry` | int | `0` | 失败后最多重跑次数 | R-01 |
| `retry_on` | list[str] | `["error"]` | 哪些状态会触发 retry；默认只重试环境失败 | R-01 |
| `wave_on_fail` | `False` \| `"per_signature"` \| `"all"` | `False` | 失败 job 用相同种子加波形再跑一次；`per_signature` 每个签名只挑一个代表 job。以 AI 分诊为主时建议 `per_signature` | R-03 |
| `max_fail` | int \| None | `None` | 失败数达到后停止派发 | X-12 |
| `budget` | str \| None | `None` | 回归总时长上限，如 `"8h"` | X-19 |

retry 使用相同的种子。同种子下设计失败必然复现，所以 `retry_on` 默认只含 `error`（环境失败）；机器负载导致的超时可以加上 `"timeout"`。`--detach` 时不自动 retry（R-01）。

随机种子在 plan 阶段生成后写入 `plan.json`，同一份 plan 重跑种子不变（T-08）。

### 2.5 `run`

| 键 | 类型 | 默认值 | 说明 | 需求 |
|----|------|--------|------|------|
| `timeout` | str \| int | `"20m"` | 墙钟超时；`"90s"` / `"30m"` / `"2h"` / 秒数 | X-02 |
| `hang` | str \| None | `None` | 日志多久不增长视为挂死 | X-15 |
| `opts` | list[str] | `[]` | 所有 job 追加的运行参数 | |
| `env` | dict[str, str] | `{}` | 所有 job 的环境变量 | X-07 |
| `mem_limit` | str \| None | `None` | 单 job 内存上限，如 `"16G"` | X-18 |

### 2.6 `checks`

| 键 | 类型 | 默认值 | 说明 | 需求 |
|----|------|--------|------|------|
| `max_uvm_error` | int | `0` | UVM_ERROR 上限 | J-03 |
| `max_uvm_fatal` | int | `0` | UVM_FATAL 上限 | J-03 |
| `max_uvm_warning` | int \| None | `None` | UVM_WARNING 上限，`None` 表示不检查 | J-03 |
| `fail_regex` | list[str] | `[]` | 命中即失败 | J-01 |
| `pass_regex` | list[str] | `[]` | 只能挽回"缺 Report Summary" | J-04 |
| `env_fail_regex` | list[str] | 后端的 `license_regex` | 命中记为环境失败；在 A 上用 Python 正则匹配 | X-16 |
| `sim_errors` | bool | `True` | 是否把仿真器自身错误、SVA 失败计入判定 | J-15 |
| `waivers` | list[Waiver] | `[]` | 豁免规则，见 §5.4 | J-14 |

case 可以用 `Case(checks={...})` 覆盖其中任意键（T-05）；`waivers` 为追加，不是替换。

### 2.7 `sim`

覆盖内置后端的任意字段，或在 `simulator` 不是内置名时定义完整后端。

| 键 | 说明 | 示例（VCS 内置值） |
|----|------|--------------------|
| `build` | 编译命令 | `vcs -full64 -sverilog -ntb_opts uvm-1.2 -timescale=1ns/1ps -f {filelist} -top {top} -o {simv}` |
| `run` | 运行命令 | `{simv}` |
| `simv` | 编译产物文件名 | `simv` |
| `seed` | 种子参数 | `+ntb_random_seed={seed}` |
| `wave` | `WAVE=1` 时追加的运行参数 | `+fsdb+all=on` |
| `cov_build` / `cov_run` | 覆盖率编译、运行参数 | `-cm line+cond+fsm+tgl+branch` / `-cm line+cond+fsm+tgl+branch -cm_dir {job_dir}/cov.vdb` |
| `compile_only` | fast compile 中只编译 tb 组的命令 | 见 [fast-compile.md](fast-compile.md) |
| `error_regex` | 仿真器自身错误模式 | `^Error-\[`、`^Fatal:`、`Offending '` |
| `license_regex` | license 失败模式，作为 `checks.env_fail_regex` 的默认值 | `Failed to obtain license` |
| `time_regex` | 从结束打印中取仿真结束时间，第 1 个分组为时间值（J-21） | 见 verdict.md §4.4 |
| `version` | 获取仿真器版本的命令，写入 `env.json` | `vcs -ID` |

命令可以写成字符串（按 shell 规则拆分）或列表（原样使用）。上表中的选项是初始值；不确定：各工具版本的确切选项需逐一核对（S-10）。

占位符。所有路径类占位符都展开为**相对于该命令运行目录**的路径：编译命令在编译目录 `.solouvm/build/<mode>/<key>/` 中运行，job 命令在本次尝试的运行目录中运行（N-05）。

| 占位符 | 含义 |
|--------|------|
| `{root}` | 项目根 |
| `{filelist}` | 合并的 `filelist.f` 路径 |
| `{filelist_rtl}` / `{filelist_tb}` / `{filelist_top}` | 分组文件清单 |
| `{top}` | `CFG["top"]` |
| `{build_dir}` | 本次使用的编译目录 `.solouvm/build/<mode>/<key>` |
| `{simv}` | `{build_dir}/{sim.simv}` |
| `{seed}` | 本 job 的种子 |
| `{job_dir}` | 本 job 本次尝试的运行目录 |
| `{job_id}` / `{case}` / `{uvm_test}` | 本 job 的标识 |
| `{fake_top}` / `{check_dir}` | 仅 `compile_only` 使用：fake_top 文件与 check 目录 |

未知占位符直接报错，不原样保留。

### 2.8 `providers`

一个命名 provider 是 transport、launcher、sync 的组合，外加环境准备（见 [architecture.md](architecture.md) §6）。

```python
"providers": {
    "farm": {
        "transport": {"kind": "local"},                       # 默认 local
        "launcher":  {"kind": "lsf", "queue": "normal",
                      "resources": "rusage[mem=8000]", "extra": []},
        "sync":      {"kind": "none"},                        # 默认 none
        "setup":     "module load vcs/2025.06",
    },
}
```

| 部分 | `kind` | 其他键 |
|------|--------|--------|
| `transport` | `local` | — |
| | `ssh` | `host`（可用 `~/.ssh/config` 里的别名）、`workdir`（B 上的项目根）、`ssh_opts` |
| `launcher` | `make` | — |
| | `lsf` | `queue`、`resources`、`extra`（追加到 bsub 的参数） |
| `sync` | `none` | — |
| | `rsync` | `exclude`（追加的排除模式）、`delete`（是否删除 B 上多余文件，默认 `False`） |
| | `git` | `remote`、`branch`；B 上执行 `git fetch` + `checkout` |
| `setup` | — | 每个 job 前在 B 上 source 或执行的 shell 片段（H-12） |

内置的 `local` provider 永远存在，等于 `{transport: local, launcher: make, sync: none}`。

push 按显式清单进行（H-08、H-09）：

- `rsync`：先同步项目树（排除 `.git/` 与整个 `.solouvm/`，再加上 `exclude` 中的模式），再用 `--files-from` 传本轮生成物：`gen/bin/` 下的脚本、本轮用到的编译目录中的文件清单、本轮的 `run.mk`。
- `git`：要求 A 的工作区干净，否则报错并建议改用 `rsync`；代码经 git 传输，生成物经 transport 单独写入 B（不确定：逐个传输的性能，H-17）。
- `none`：共享盘，不传输。

### 2.9 `check`

| 键 | 默认值 | 说明 |
|----|--------|------|
| `slang` | `"slang"` | slang 可执行文件 |
| `slang_args` | `[]` | 追加参数，如 `["-Wno-unused"]` |
| `uvm_src` | `"${UVM_HOME}/src"` | UVM 源码目录，slang 解析 `uvm_pkg` 时需要（E-11） |
| `exclude` | `[]` | 不交给 slang 的文件 glob（加密 IP 等） |
| `faketop` | `True` | 是否执行 UVM 侧 fake_top 编译 |
| `where` | `"auto"` | `local` / `remote` / `auto`（A 上有 slang 就本地跑） |

### 2.10 `triage`

| 键 | 默认值 | 说明 |
|----|--------|------|
| `pass_policy` | `"changed"` | 哪些 pass 要分诊：`none` / `changed`（`wip` case，以及 `Case(...)` 定义或其 test 所在文件相对上一轮有改动的 case）/ `all` |
| `group_by` | `"signature"` | 失败如何合并为任务：`signature`（可跨 case）/ `case` / `job` |
| `packet_limit` | `20000` | 上下文包的长度上限（约等于 token 数） |
| `debug_dir` | `"docs/debug"` | 调试文档的存放目录（入库），文档路径为 `<debug_dir>/<run_id>/<序号>.md` |
| `weak_signals` | `True` | 是否把 J-19 的虚假 pass 弱信号纳入分诊 |

### 2.12 `packages`

声明外部 Git 或本地路径包（如 VIP / UVM Agent 仓库）：

```python
CFG = {
    "simulator": "vcs",
    "top": "tb_top",
    "packages": {
        "base_uvc": "https://github.com/org/base_uvc.git",  # 简写，默认当作 git URL
        "apb_agent": {
            "git": "https://github.com/org/apb_agent.git",
            "ref": "v1.2.0",       # 可选：branch / tag / commit
            # "subdir": "vip/apb", # 可选：仓库内相对子目录
            # "priority": 50,      # 可选：覆盖包内 filelist.py 的 PRIORITY
        },
    },
}
```

- 每个包必须包含 `git`（远端仓库）或 `path`（本地目录，常用在 `solouvm.local.py` 中联调），不可同时填写；`solouvm.local.py` 切换 `path` 时会自动清掉继承的 `git`/`ref`（反之亦然）。
- Git 包拉取到项目内的 `.solouvm/packages/<name>/`：**运行命令时仅检查目录是否存在，存在即直接复用，不存在才执行单次 `git clone`**；不会自动递归拉取子依赖。需主动拉取最新代码时使用 `solouvm pkg update`。
- **要求每个包目录内必须存在 `filelist.py`**，否则报 `FILES_MISSING`。被宿主作为 package 引入时，仅收集其中的 `rtl` 与 `tb` 组，忽略包自身的 `top` 组（避免与宿主 `tb_top` 冲突）；该仓库独立运行自测时不受影响。
- 包内 `filelist.py` 可选声明 `PRIORITY = 100`（整数，**数字越小优先级越高、越先编译**；数字越大越靠后）与 `DEPENDS = ["base_uvc"]`：按 `(effective_priority, CFG['packages'] 声明顺序)` 排序后依次校验，若 `DEPENDS` 中的包未在宿主 `CFG['packages']` 中配置或尚未加载（优先级倒挂/成环），立即报错 `CFG_INVALID`。
- 推送到执行机 B 时，已克隆到 `.solouvm/packages/` 下的包文件（不含 `.git/`）会自动纳入同步清单。

### 2.11 环境变量展开

字符串中可以写 `${VAR}`。按值在哪里使用，分两类处理（C-03）：

| 类别 | 字段 | 处理 |
|------|------|------|
| 只在 A 上使用 | `providers` 中的主机、远端目录、`ssh_opts`；`check` 的各项（`check.where=local` 时）；`system_doc`、`doc`、`triage` 中的路径 | 加载时在 A 上展开；可写 `${VAR:-默认值}`；未设置且无默认值时报错 |
| 写进生成物、在 B 上使用 | `incdirs`、`defines`、`builds.*`、`run.opts`、`run.env`、`sim` 中的命令、`job.pre` / `job.post`、`setup`、文件清单条目 | 原样保留，由 B 上的 shell 或仿真器展开；A 不展开 |

规则：

- 写进 `.f` 文件的值只允许 `${VAR}` 形式，不允许 `${VAR:-默认值}`（仿真器不认）；`validate` 检查。
- 不确定：各仿真器对 `.f` 文件中 `${VAR}` 的展开支持需逐一核对；slang 在 A 上运行时，由 solouvm 用 A 的环境先展开再交给 slang。

## 3. `solouvm.local.py`

个人配置，必须加入 `.gitignore`。导出的 `LOCAL` 会深度合并到 `CFG` 上，优先级：命令行 > `LOCAL` > `CFG`（H-01a）。

```python
LOCAL = {
    "provider": "farm",
    "providers": {
        "farm": {
            "transport": {"kind": "ssh", "host": "simbox", "workdir": "/work/wp/axi_vip"},
            "sync": {"kind": "rsync"},
        },
    },
    "regress": {"jobs": 32},
}
```

合并规则：字典递归合并；列表与标量整体替换。上例中 `farm` 的 launcher 和 setup 来自 `CFG`，transport 和 sync 来自 `LOCAL`。

`solouvm config` 会打印合并后的结果及每个值的来源（C-08）。

## 4. `filelist.py`

### 4.1 分组

```python
FILES = {
    "rtl": [
        "rtl/pkg/axi_pkg.sv",
        "rtl/**/*.sv",
        "-f ip/vendor_ip.f",
    ],
    "tb": [
        "+incdir+verif",
        "verif/agents/*/*_if.sv",
        "verif/agents/*/*_pkg.sv",
        "verif/envs/*/*_env_pkg.sv",
        "verif/tests/*/*_test_pkg.sv",
    ],
    "top": [
        "verif/tb/tb_top.sv",
        "verif/tb/binds.sv",
    ],
}
```

目录布局与上面的 glob 写法见 uvm-style.md §2；`solouvm init` 生成的就是这份。

| 组 | 放什么 | 参与 fake_top |
|----|--------|---------------|
| `rtl` | 设计代码、IP | 否 |
| `tb` | UVM 包、agent、sequence、interface | 是 |
| `top` | `tb_top`、`bind`、任何引用 DUT 层次路径的代码 | 否 |

- 三组都可省略，省略即为空。合并的 `filelist.f` 按 rtl → tb → top 的顺序拼接。
- 也可以直接写列表 `FILES = [...]`，等同于全部放进 `rtl` 组；这时 `check` 无法执行 fake_top，会给出提示。
- 同一文件出现多次时只保留第一次。
- 生成的文件清单中，路径都改写为相对于工具运行目录（编译目录、check 目录）的形式，项目可以整体搬移（N-05）。

### 4.2 条目语法

每个条目是一个字符串，语法与 `.f` 文件保持一致，方便直接从已有 `.f` 复制过来：

| 写法 | 含义 |
|------|------|
| `rtl/a.sv` | 单个文件 |
| `rtl/*.sv`、`rtl/**/*.sv` | glob，按文件名排序后展开 |
| `-f path.f` / `-F path.f` | 导入已有 `.f`；`-f` 内的相对路径相对当前工作目录，`-F` 相对该 `.f` 所在目录（与 Xcelium 的语义一致；各仿真器对 `-f` 的约定不同，实现前逐一核对） |
| `+incdir+dir` | 本组的 include 目录 |
| `+define+NAME=VALUE` | 本组的宏 |
| `-v file` / `-y dir` / `+libext+.v+.sv` | 库文件与库目录 |

导入的 `.f` 按同样规则递归解析（F-06）。其中的绝对路径与环境变量：

- 位于项目根之内的绝对路径，转成相对路径；
- 位于项目根之外的，保持原样，并在 `solouvm files` 中标出——这些文件 B 上必须存在相同路径（F-07）；
- `$VAR` / `${VAR}` 原样保留，由 B 上的仿真器展开，A 不展开（不确定：各仿真器的支持情况，见 §2.11）。

任一条目匹配不到文件时报错，列出全部缺失条目（F-03）。

### 4.3 函数形式

需要动态计算时，定义 `files()` 返回同样结构的值。

### 4.4 包优先级与依赖声明（`PRIORITY` / `DEPENDS`）

任意 `filelist.py`（尤其是通过 `CFG["packages"]` 引入的库）均可在模块顶层可选声明：

```python
PRIORITY = 50                 # 整数，默认 100；数字越小优先级越高（越先编译），数字越大越晚编译
DEPENDS = ["base_uvc"]        # 在本 filelist 之前必须已经加载完成的 package 名字列表

FILES = {
    "tb": ["+incdir+src", "src/apb_if.sv", "src/apb_pkg.sv"],
}
```

- 宿主 `filelist.py` 也可以写 `DEPENDS = ["apb_agent"]`，在所有 package 加载完成后校验其依赖是否已配齐。
- 被作为 package 收集时，`FILES` 中的相对路径、`glob`、`+incdir+`、`-f`/`-F`、`-v`/`-y` 均以该 package 根目录为基准展开，仅并入 `rtl` 与 `tb` 组（跳过 `top`）。

## 5. `case.py`

### 5.1 `Case`

```python
from solouvm import Case, Bug, Todo, Log, Note, Expect

CASES = [
    Case(name="smoke", uvm_test="axi_smoke_test", owner="wp"),     # 默认 stage="wip"
    Case(
        name="burst",
        uvm_test="axi_burst_test",
        params={"NUM": 32, "FAST": True},
        tags=("nightly", "axi"),
        seeds="random:20",
        timeout="1h",
        owner="wp",
        stage="ready",
        intent="验证 AXI burst 的地址计算与数据完整性，重点是 WRAP 跨 4K 边界",
        method="随机 INCR / WRAP / FIXED，长度 1–256；scoreboard 逐笔比对读回数据，SVA 检查地址回绕",
        spec_refs=["AXI4 A3.4.1", "design_spec 5.2"],
        expect=[
            Expect(scoreboard="AXI_SCB", min=100),
            Expect(sim_time_min="50us"),
        ],
        doc="""
        验证 AXI burst：INCR / WRAP / FIXED，长度 1–256。
        """,
        notes=[
            Bug(id=1, title="WRAP 跨 4K 边界地址回绕错误", status="open",
                seed=4417, found="2026-11-02", owner="hw-zhao"),
            Todo("补 narrow transfer 场景", due="2026-11-20"),
            Log("2026-11-02", "4417 必现；addr_q 第二拍回绕成 0"),
        ],
    ),
]
```

| 字段 | 类型 | 默认值 | 说明 | 需求 |
|------|------|--------|------|------|
| `name` | str | 必填 | 全局唯一；只允许字母、数字、`_`、`-`、`.`，且不得包含 `__` | T-02, N-12 |
| `uvm_test` | str \| None | `CFG["uvm_test"]` | 生成 `+UVM_TESTNAME` | |
| `params` | dict | `{}` | 运行期 plusarg：`{"N": 32}` → `+N=32`；`True` → `+FLAG`；`False` → 不输出 | T-03 |
| `opts` | list[str] | `[]` | 原样追加的运行参数 | |
| `env` | dict[str, str] | `{}` | 本 case 的环境变量 | X-07 |
| `build` | str | `"default"` | 使用哪套编译配置 | T-04 |
| `seeds` | list \| str \| None | `None` | 覆盖 `regress.seeds`，写法相同 | T-07 |
| `timeout` / `hang` | str \| None | 继承 `run` | | |
| `retry` | int \| None | 继承 `regress` | | |
| `checks` | dict | `{}` | 覆盖判定参数 | T-05 |
| `expect_fail` | bool \| str | `False` | 预期失败。写正则时，首条错误必须匹配它才算 xfail，否则判 fail；只写 `True` 时任何失败都算 xfail，`ready` 及以上会收到 `validate` 警告 | J-06, J-20 |
| `tags` | tuple[str, ...] | `()` | 标签 | T-10 |
| `owner` | str \| None | `None` | 负责人 | M-16 |
| `stage` | str | `"wip"` | `planned` / `wip` / `ready` / `signoff` / `deprecated` | M-17 |
| `intent` | str | `""` | 验证什么功能点；`ready` 及以上必填 | AI-02 |
| `method` | str | `""` | 激励怎么产生、结果怎么检查；`ready` 及以上必填 | AI-02 |
| `spec_refs` | list[str] | `[]` | 对应的规格章节；`ready` 及以上必填 | AI-02 |
| `expect` | list[Expect] | `[]` | 机械可查的通过条件，见 §5.5；`ready` 及以上必填 | J-18 |
| `doc` | str | `""` | 说明文档，markdown；首尾空白与公共缩进会被去掉 | M-01 |
| `notes` | list | `[]` | 记录；约定写在最后一个参数 | M-02 |

`stage` 与选择的关系（M-18、T-19）：

| stage | `regress` 默认 | `--stage` 显式包含它 | `--case NAME` 选中它 |
|-------|----------------|----------------------|----------------------|
| `planned` | 不跑 | 报错：case 尚未实现 | 报错：case 尚未实现 |
| `wip` | 跑 | 跑 | 跑 |
| `ready` / `signoff` | 跑 | 跑 | 跑 |
| `deprecated` | 不跑 | 跑，并给出警告 | 跑，并给出警告 |

CI 或夜间回归只想跑已就绪的 case 时，用 `regress --stage ready,signoff`。

### 5.2 记录类型

| 类型 | 字段 | 说明 |
|------|------|------|
| `Bug` | `id`（必填，int）、`title`、`status`、`found`、`fixed`、`verified`、`seed`、`signature`、`run`、`job`、`reporter`、`owner`、`link`、`body` | 完整 ID 为 `<case>#<id>`（M-04）；`signature` 用于跨 case 判断"已知问题"与去重 |
| `Todo` | `title`、`owner`、`due`、`done`（默认 `False`）、`id`（可选） | |
| `Log` | `date`、`text` | 调试记录 |
| `Note` | `text`、`date`（可选） | 一般备注 |
| `Triage` | `task`、`verdict`、`confidence`、`summary`、`evidence`、`doc`、`bugs`、`judged_status`、`review` | 只由 `solouvm triage write` / `triage review` 写入（M-24）；`judged_status` 是分诊时的机械判定结果（`{job_id: status}` 映射），之后结果变化时该记录视为过期（AI-20） |

所有记录都有可选字段 `author` 与 `at`（写入时间）；经命令行写入时自动填写（M-25）。`Bug.reporter` 是发现问题的人，`author` 是写下这条记录的人或 agent，两者可以不同。

`Todo`、`Log`、`Note` 支持位置参数简写：`Todo("标题")`、`Log("日期", "内容")`、`Note("内容")`。

`Bug.status` 与日期字段的约束（M-03a、M-05）：

| status | 必须有 | 含义 |
|--------|--------|------|
| `open` | `found` | 未修 |
| `fixed` | `found`、`fixed` | 已修，待验证 |
| `verified` | `found`、`fixed`、`verified` | 已用复现种子验证 |
| `wontfix` | `found` | 是问题但不修，`body` 写理由 |
| `invalid` | `found` | 不是问题，例如分诊结论被驳回，`body` 写理由 |
| `duplicate` | `found`、`link` | 重复，`link` 指向原 bug，如 `"burst#1"` |

日期格式统一为 `YYYY-MM-DD`。`owner` 未写时继承 case 的 `owner`（M-16）。

### 5.3 一个 case 一个文件

配置 `CFG["case_dirs"] = ["cases"]` 后，solouvm 收集 `cases/` 下每个 `*.py` 导出的 `CASE`（单个）或 `CASES`（列表），再与 `case.py` 的结果合并。`case.py` 可以不存在。

```python
# cases/burst.py
from solouvm import Case, Bug

CASE = Case(
    name="burst",
    uvm_test="axi_burst_test",
    notes=[
        Bug(id=1, title="WRAP 跨 4K 边界地址回绕错误", status="open", seed=4417, found="2026-11-02"),
    ],
)
```

### 5.4 `Waiver`

```python
Waiver(id="AXI_SCB.LATE", reason="已知的响应延迟告警，见 JIRA-88")    # 按 UVM 消息 ID
Waiver(regex=r"\[AXI_SCB\] late response", reason="...", max=3)
Waiver(regex=r"Offending 'axi_wrap_chk'", severity=("SIM",), reason="...")  # 豁免仿真器报出的断言失败
Waiver(regex=r"...", reason="...", until="2026-12-31")                    # 到期后失效
```

| 字段 | 说明 |
|------|------|
| `id` | UVM 消息 ID，匹配 `UVM_ERROR <文件>(<行>) @ <时间>: <reporter> [ID] <消息>` 中的 `[ID]`，完全相等 |
| `regex` | 对整行匹配；`id` 与 `regex` 至少写一个 |
| `severity` | 作用于哪些级别，默认 `("ERROR", "WARNING")`；`FATAL` 与仿真器自身的错误 `SIM` 必须显式写出 |
| `reason` | 必填 |
| `max` | 每个 job 最多豁免多少行，超出部分照常计数；默认不限 |
| `until` | 失效日期，过期后不再生效并在报告中提示 |

详细语义见 [verdict.md](verdict.md) §5。

### 5.5 `Expect`

通过时一定能观察到的现象，由 judge 机械检查（J-18）。任一条不满足，job 判为 fail，原因写明是哪一条。

```python
Expect(scoreboard="AXI_SCB", min=100)                       # AXI_SCB 至少比对 100 笔（按 uvm-style.md §5.2 的打印格式）
Expect(regex=r"\[AXI_BURST_VSEQ\] start", count_min=1)    # 至少出现 1 次
Expect(regex=r"\[AXI_SCB\] .*mismatch", count_max=0)      # 一次都不能出现
Expect(sim_time_min="50us")                                 # 仿真结束时间不早于 50us
Expect(uvm_info_min=1, id="AXI_SCB")                        # 消息 ID 为 AXI_SCB 的 UVM_INFO 至少 1 条
```

| 字段 | 说明 |
|------|------|
| `scoreboard` | scoreboard 的消息 ID；按 uvm-style.md §5.2 的汇总打印 `[<ID>] compared=<N> ...` 取 `N`，配合 `min` 使用。推荐写法 |
| `regex` | 对日志逐行匹配 |
| `min` / `max` | 对比对笔数（`scoreboard`）或 `regex` 第 1 个捕获分组的数值做上下限检查，取最后一次匹配 |
| `count_min` / `count_max` | 对匹配行数做上下限检查 |
| `sim_time_min` | 仿真结束时间的下限；时间来源见 verdict.md §4.4 |
| `uvm_info_min` + `id` | 指定消息 ID 的 UVM_INFO 数量下限 |
| `reason` | 可选，失败时显示的说明 |

写 `expect` 的目的是拦截虚假 pass：scoreboard 一笔没比、sequence 没启动、objection 提前撤销、checker 被关闭。每个 `ready` 及以上的 case 至少应有一条 `scoreboard` 类的 expect；环境里有多个 scoreboard 时，每个都写一条，避免一个比了 0 笔却被另一个的打印掩盖。

## 6. `tbspec.py`

描述 UVM 环境，供 `solouvm new` 与 `solouvm uvm sync` 生成组件（U-02）。生成结果的结构与写法见 [uvm-style.md](uvm-style.md)。

```python
from solouvm.uvm import Env, Agent, Interface, Signal, Field

AXI = Interface(
    name="axi",
    clock="aclk",
    reset=("aresetn", "low"),
    signals=[
        Signal("awaddr", 32, "m2s"),
        Signal("awlen", 8, "m2s"),
        Signal("awvalid", 1, "m2s"),
        Signal("awready", 1, "s2m"),
    ],
)

ENVS = [
    Env(
        name="axi",
        agents=[
            Agent(
                name="axi_mst",
                interface=AXI,
                role="master",
                fields=[
                    Field("addr", 32),
                    Field("len", 8, constraint="len inside {[0:255]}"),
                    Field("burst", "axi_burst_e"),
                ],
                coverage=True,
            ),
            Agent(name="axi_slv_mon", interface=AXI, role="monitor", like="axi_mst"),
        ],
        scoreboard=True,
        ref_model=False,
    ),
]
```

| 类型 | 字段 | 说明 |
|------|------|------|
| `Interface` | `name`、`clock`、`reset`（信号名, `"low"` / `"high"`）、`signals`、`params` | 生成 `<name>_if.sv`；多个 agent 共用 |
| `Signal` | `name`、`width`、`dir` | `dir` 以接口协议的主从方向描述：`"m2s"` 由主方驱动，`"s2m"` 由从方驱动 |
| `Agent` | `name`、`interface`、`role`、`fields`、`coverage`、`like` | `role`：`master` 驱动 `m2s` 信号、`slave` 驱动 `s2m` 信号、`monitor` 只采样；`like` 复用另一个 agent 的 transaction 字段 |
| `Field` | `name`、`type`（位宽整数或类型名）、`rand`（默认 `True`）、`constraint` | |
| `Env` | `name`、`agents`、`scoreboard`（默认 `True`）、`ref_model`、`coverage` | |

规则：

- 名字遵守 uvm-style.md §3 的命名规则，加载时校验；
- 删除 `tbspec.py` 中的组件不会自动删除已生成的文件，`uvm sync` 只报告"多余的文件"；
- 模板版本记录在生成文件头部，`uvm sync` 按当前 solouvm 内置（或项目覆盖）的模板版本重新生成。
