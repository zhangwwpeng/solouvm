# 01 安装与环境准备

## 1. 控制机（A）要求

- **Python >= 3.10**：项目声明 `requires-python = ">=3.10"`，代码不使用仅限 3.11+ 的标准库特性。
- **零外部运行时依赖**：终端渲染所需的 `rich` 及其依赖（`pygments`、`markdown_it`、`mdurl`）已内置在 `solouvm/_vendor/` 下，仅由 `solouvm/ui.py` 按需加载；即使 `_vendor/rich` 缺失也可自动降级为纯文本输出。

### 安装方式

分发时直接拷贝整个 `solouvm` 目录，无需 `pip`、网络或额外依赖。**用户自己的验证项目目录下不需要放置或安装 `solouvm` 库，直接调用安装好的 `solouvm` CLI 即可**；安装脚本会在安装阶段预编译全部 `.pyc` 字节码。

1. **当前用户安装（默认，无需 root）**

```bash
./install.sh          # 等价于 ./install.sh --user
```

安装到 `$HOME/.local/lib/solouvm` 与 `$HOME/.local/bin/solouvm`。安装完成后会检测当前 `$PATH`，并检查 `~/.zshrc` 与 `~/.bashrc`，提示或确认如何配置：

```bash
export PATH="$HOME/.local/bin:$PATH"
```

2. **系统级安装（所有用户共享 CLI）**

```bash
sudo ./install.sh --system
# 如需显式指定全局共享的 Python >= 3.10：
sudo ./install.sh --system --python /usr/bin/python3
```

安装到 `/usr/local/lib/solouvm` 与 `/usr/local/bin/solouvm`：自动锁定非个人私有目录的全局 Python、预编译 `.pyc`、设置全部用户可读/可执行权限（`a+rX`），并进行原子替换升级。

3. **自定义目录 / 卸载**

```bash
./install.sh --prefix /opt/eda/solouvm --python /usr/bin/python3.12
./install.sh --uninstall --user        # 或 sudo ./install.sh --uninstall --system
```

### 命令行补全（随 `install.sh` 自动安装）

执行 `./install.sh` 或 `solouvm update` 时，会自动预生成并安装 Bash 与 Zsh 静态补全脚本，并在 `--uninstall` 时一并清理：

| 安装模式 | Zsh 补全路径 | Bash 补全路径 |
|----------|--------------|---------------|
| `--user`（默认） | `$HOME/.local/share/zsh/site-functions/_solouvm` | `$HOME/.local/share/bash-completion/completions/solouvm` |
| `--system` | `/usr/local/share/zsh/site-functions/_solouvm` | `/usr/local/share/bash-completion/completions/solouvm` |
| `--prefix DIR` | `DIR/share/zsh/site-functions/_solouvm` | `DIR/share/bash-completion/completions/solouvm` |

- **`--user` 模式启用**：安装结束会检查 `~/.zshrc` 与 `~/.bashrc`，若未配置可按需添加：

```zsh
# ~/.zshrc
fpath=("$HOME/.local/share/zsh/site-functions" $fpath)
autoload -Uz compinit && compinit
```

```bash
# ~/.bashrc（若系统未通过 bash-completion >= 2 自动加载）
[ -f "$HOME/.local/share/bash-completion/completions/solouvm" ] && source "$HOME/.local/share/bash-completion/completions/solouvm"
```

- **临时手动启用**：

```bash
source <(solouvm completion zsh)        # zsh
eval "$(solouvm completion bash)"       # bash
```

### 更新方式

- **离线环境**：获取新版发布目录后，直接再执行一次 `./install.sh --user`（或 `sudo ./install.sh --system`），脚本会执行原子升级。
- **联网环境**：通过 CLI 直接从 Git 仓库更新（默认地址为 `https://github.com/zhangwwpeng/solouvm`）：

```bash
solouvm update                                         # 按当前安装模式/路径升级
solouvm update --repo https://git.corp.local/eda/solouvm --ref v0.2.0
solouvm update --dry-run                               # 仅打印计划执行的命令
```

### 开发者调试方式（直接从源码运行）

若在开发 `solouvm` 本身，可将仓库根目录加入 `PYTHONPATH` 或使用本地开发虚拟环境：

```bash
PYTHONPATH=/path/to/solouvm python3 -m solouvm --version
# 输出: solouvm 0.1.0
```

## 2. 执行机（B）要求

- 不需要安装 `solouvm` 或 Python。
- 仅需：`bash`、GNU `make`、常用 POSIX 工具（`hostname`、`date`、`awk`、`mkdir`、`kill` 等），以及配置中指定的仿真器可执行程序。
- 超时控制在生成脚本 `run_job.sh` 中按 `timeout` -> `gtimeout` -> bash 后台定时器三级回退处理，兼容 Linux 与 macOS。

## 3. 可选工具

| 工具 | 用途 | 备注 |
|------|------|------|
| `slang` | `solouvm check` 第一阶段快速静态语法检查 | 未实机验证（[requirements.md §26](../requirements.md#26-不确定事项)） |
| `rsync` / `git` | 跨机文件同步（`sync.kind = "rsync" \| "git"`） | 同机或共享盘可用 `sync.kind = "none"` |
| `bsub` / `bkill` / `bjobs` | LSF 集群作业调度（`launcher.kind = "lsf"`） | 参数见各站点环境 |
| `vcs` / `xrun` / `questa` / `iverilog` / `verilator` | RTL/UVM 仿真与覆盖率（`urg`/`imc`/`vcover`）与波形导出（`fsdb2vcd`/`wlf2vcd`） | 本仓库仅用内置 `fake` 完整跑通，实机工具细节见 `§26` |

## 4. 环境自检（`solouvm doctor`）

进入项目目录后运行：

```bash
solouvm doctor
solouvm doctor --json
```

`doctor` 会检查：

1. 当前 provider（`local` 或远端 `ssh` / `lsf` / `rsync` / `git`）连通性与必要命令是否存在。
2. B 侧 `make`、`bash` 与所配仿真器的可执行性 / 版本探测。
3. UVM 与 `check.slang` 工具路径是否就绪。

任何检查失败时 `doctor` 返回退出码 `2` 并给出修复建议。
