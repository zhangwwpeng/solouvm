# 10 命令行参考

完整设计契约参见 [doc/cli.md](../cli.md)。以下所有子命令共 47 个，已全部接入 `solouvm.cli`。

## 1. 退出码约定

| 退出码 | 含义 |
|--------|------|
| `0` | 命令成功（测试全部通过/无错误/分诊已全部审查） |
| `1` | 命令完成，但存在非通过测试、style 问题、check 问题或未审查分诊任务 |
| `2` | 配置/参数/环境/权限/编译/收集错误，或未匹配到任何 job |
| `130` | 收到 `SIGINT`（Ctrl-C）提前中断 |

## 2. 全局参数（子命令前后均可放置）

| 参数 | 说明 |
|------|------|
| `-c, --config PATH` | 指定配置文件或项目目录（默认从当前目录向上查找） |
| `-v / -vv` | 提高详略度（`-v` 实时打印日志行，`-vv` 跟踪外部命令与 `.solouvm/solouvm.log`） |
| `-q, --quiet` | 仅打印错误与最终摘要 |
| `--json` | 输出 schema-1 标准 JSON 外壳（长耗时运行会先逐行输出 `event` JSONL） |
| `--limit N` | 截断行/条目数（默认 `50`；`0` 表示不截断） |
| `--role main\|triage` | 执行角色权限控制（`triage` 仅允许只读与安全写记录命令） |
| `--provider NAME` | 临时覆盖配置中的 `provider` |
| `--no-hooks` | 跳过 A 侧钩子 |
| `--version` | 打印 `solouvm 0.1.0` 并退出 |

## 3. 全量命令清单（47 个）

| 分类 | 命令 | 主要用途 |
|------|------|----------|
| **准备与检查** | `init [--simulator S] [--bare] [--agents claude]` | 初始化起步项目与示例 UVM 环境 |
| | `config [KEY]` | 打印合并后的最终配置及值来源文件 |
| | `files` | 按组打印收集后的文件清单（标出 `[OUTSIDE ROOT]`） |
| | `validate` | 仅校验配置、文件清单、case 与记录语法 |
| | `schema [NAME]` | 输出 `facts`、`output`、`packet`、`plan` 或 `results` 的 JSON Schema |
| | `list` | 预览选中扩展后的 job 计划表（不生成文件） |
| | `doctor` | 检查 A 与 B 环境连通性及可执行工具 |
| **UVM 脚手架** | `new agent\|env\|test\|seq\|vseq NAME` | 生成 Style 1 UVM 组件骨架 |
| | `uvm sync [--dry-run]` / `uvm diff` | 同步 `tbspec.py` 变更，保留用户代码区 |
| | `lint-style` | 检查 UVM 目录布局、`user begin/end` 与 `*_files.svh` |
| **生成与执行** | `gen` / `build` / `run` / `regress` / `rerun` | 生成产物、编译、定向/全量回归与失败重跑 |
| | `check` | `slang` + `fake_top` + 增量 RTL Fast Compile 检查 |
| **监控与报告** | `status` / `stop` / `log [JOB_ID]` | 查看运行进度、终止远端/B 侧作业、流式读取日志 |
| | `explain <JOB_ID>` / `wave <JOB_ID>` | 输出 8 节排错全览 / 文本波形信号检索 |
| | `judge` / `report` / `history [CASE]` | 独立重写判定结果 / 渲染 text/json/junit/html / 查询历史 |
| **记录与分诊** | `notes` / `bug add\|update` / `note add` / `todo add\|done` | 查询及 AST 安全写回 case 记录 |
| | `triage plan\|packet\|write\|review\|status` | 分诊规划、7 节上下文包、写结论、主代理审查与状态 |
| | `doc` / `progress` | 导出 Markdown 用例手册 / 输出全局进度与矛盾提示 |
| **覆盖率与维护** | `cov merge` / `cov report` / `clean` / `help [TOPIC]` | 合库、覆盖率报告、清理 `.solouvm` 产物、离线读设计文档 |
| | `pkg list\|sync\|update` | 查看、拉取或更新 `CFG["packages"]` 外部包 |
| | `update [--repo URL] [--ref REF] [--user\|--system]` | 联网从 Git 仓库更新 CLI 安装 |
| | `completion <bash\|zsh>` | 生成 Bash/Zsh 命令行补全脚本 |

> 注：`--resume`、`watch` 命令当前属 [requirements.md §25](../requirements.md#25-里程碑归属) 中的 **待定** 需求。
