# 03 配置与文件清单

solouvm 只使用纯 Python 模块作为配置载体，不解析 YAML / JSON / TOML（[config.md](../config.md)）。

## 1. 配置文件与合并优先级

覆盖顺序从高到低：

```text
CLI 命令行参数 > 环境变量 > solouvm.local.py (LOCAL) > solouvm.conf.py (CFG) > 内置 DEFAULTS
```

- **`solouvm.conf.py`**：团队共享、入库，定义顶层字典 `CFG = {...}`。
- **`solouvm.local.py`**：个人本机覆盖、不入库（已写入 `.gitignore`），定义 `LOCAL = {...}`，深度合并到 `CFG` 之上（list/tuple 与标量替换，dict 递归合并）。
- **路径查找**：未指定 `-c/--config` 时，从当前目录向上查找 `solouvm.conf.py`；允许传入文件路径或项目目录给 `-c`。
- **拼写校验**：未知配置键会直接报错 `CFG_UNKNOWN_KEY`（退出码 2）并提示最相近键名。

## 2. `CFG` 核心字段一览

| 键 | 含义 | 默认值 / 说明 |
|----|------|---------------|
| `simulator` | 必填：仿真器名 | 内置 `fake` / `vcs` / `xrun` / `questa` / `iverilog` / `verilator` |
| `top` | 必填：仿真顶层模块名 | 如 `"tb_top"` |
| `project` | 项目名称 | 默认取根目录名 |
| `uvm_test` | 全局默认 UVM test 名 | `None` |
| `incdirs` / `defines` | 全局编译 include 目录与宏 | `[]` |
| `builds` | 编译模式字典 `{mode: {...}}` | 支持 `defines`, `incdirs`, `compile_opts`, `run_opts`, `coverage`, `timeout`（默认 `"2h"`） |
| `regress` | 回归控制 | `seeds` (`"random:1"`), `jobs` (`4`), `retry` (`0`), `retry_on` (`["error"]`), `wave_on_fail` (`False` / `"per_signature"` / `"all"`), `max_fail`, `budget` |
| `run` | 单 job 运行控制 | `timeout` (`"20m"`), `hang`, `opts`, `env`, `mem_limit` |
| `checks` | 默认日志检查规则 | `max_uvm_error` (`0`), `max_uvm_fatal` (`0`), `max_uvm_warning`, `fail_regex`, `pass_regex`, `env_fail_regex`, `sim_errors` (`True`), `waivers` |
| `job` | B 侧单 job 钩子 | `{"pre": "", "post": ""}` |
| `sim` | 覆写内置仿真器命令/正则模板 | 支持 `build`, `run`, `simv`, `seed`, `wave`, `cov_build`, `cov_run`, `compile_only`, `cov_merge`, `cov_report`, `wave_export`, `error_regex`, `license_regex`, `time_regex`, `version` |
| `providers` / `provider` | 调度与远端传输配置 | 默认 `"local"`；每个 provider 含 `transport`, `launcher`, `sync`, `setup` |
| `check` | Fast Compile 配置 | `slang`, `slang_args`, `uvm_src`, `exclude`, `faketop`, `where` (`"auto"`) |
| `case_dirs` | Case 文件目录列表 | 默认 `[]`（自动加载根目录 `case.py`） |
| `system_doc` | 注入分诊包的系统文档路径 | `[]` |
| `triage` | 分诊策略 | `pass_policy` (`"changed"`), `group_by` (`"signature"`), `packet_limit` (`20000`), `debug_dir` (`"docs/debug"`), `weak_signals` (`True`) |
| `doc` / `history` / `limits` / `uvm` / `plugins` | 文档导出、历史保留、上限、模板、插件 | `doc={"lang":"zh","out":"docs/cases"}`；`history={"keep":50}`；`limits={"max_jobs":2000}` |

> 注意：`solouvm.conf.py` 中的构建编译附加参数键名为 **`compile_opts`** 与 **`run_opts`**。此外配置版本字段与检查属 **待定（`C-07`）** 范围，暂未启用。

## 3. `filelist.py`（编译清单）

`filelist.py` 导出 `FILES = {"rtl": [...], "tb": [...], "top": [...]}`，支持按顺序保留与去重：
- Glob 通配（如 `"rtl/**/*.sv"`），按字典序展开。
- `+incdir+<dir>`、`+define+<MACRO>`、`-y <dir>`、`-v <file>`、`+libext+<ext>`。
- `-f <path>` / `-F <path>` 递归导入已有文件清单（`-f` 相对调用目录，`-F` 相对 `.f` 自身所在目录）。
- `${VAR}` / `$VAR` 环境变量：收集期保留原文，运行前校验变量是否已定义；各商业仿真器在 `.f` 中的实际展开差异见 [requirements.md §26](../requirements.md#26-不确定事项) 第 8 项。
- **根外路径**：引用项目根目录之外的路径会照常收集，并在 `solouvm files` 中标记 `[OUTSIDE ROOT]`。若启用远程跨机执行且使用了根外路径，同步前需特别核查远端可达性。

> 提示：编译键哈希包含所有收集输入（含 `incdir` 目录）及排序后的 `FILES` 与相关配置；编译键排除规则（`F-09`）当前为 **待定**。
