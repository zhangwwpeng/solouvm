# 11 常见问题与排错

## 1. 常见错误码速查（`error.code`）

| 错误码 | 退出码 | 常见原因与解决方式 |
|--------|--------|--------------------|
| `CFG_NOT_FOUND` | 2 | 当前目录及上级目录找不到 `solouvm.conf.py`；检查路径或传 `-c DIR` |
| `CFG_UNKNOWN_KEY` | 2 | 配置写了不支持的键（如在 `builds["default"]` 误写 `"opts"`，正确应为 `"compile_opts"` / `"run_opts"`）；看报错里的相近键提示 |
| `CFG_INVALID` / `CFG_LOAD_FAILED` | 2 | 配置类型不合法或 Python 语法/运行期异常；用 `solouvm validate` 查看精确 `文件:行` |
| `FILES_MISSING` | 2 | `-f` / `-F` 文件未找到，或通配结果/文件路径引用失败；运行 `solouvm files` 排查 |
| `CASE_INVALID` / `CASE_PLANNED` | 2 | case 字段不合法，或 `--case` 显式点名了 `stage="planned"` 的用例 |
| `RECORD_INVALID` / `RECORD_CONFLICT` | 2 | `Bug`/`Todo` 日期非 `YYYY-MM-DD`、ID 重复或 `case.py` 同行写导致写锁/冲突 |
| `SELECT_EMPTY` | 2 | `--case`/`--tag`/`--stage` 过滤后剩余 0 个 job；确认拼写或显式加 `--allow-empty` |
| `LIMIT_EXCEEDED` | 2 | 展开 job 数超过 `limits.max_jobs`（默认 2000）；缩小选择器范围或加 `--force` |
| `ROLE_DENIED` | 2 | 使用 `--role triage` 调用了受禁写/运行命令（如 `gen` / `regress` / `todo done` / `triage review`） |
| `BUILD_FAILED` | 2 | `make build` 返回非零；查看报错提示的 `.solouvm/build/<mode>/<key>/build.log` |
| `PROVIDER_FAILED` / `GIT_DIRTY` | 2 | SSH/rsync/LSF 执行失败，或启用 `sync.kind="git"` 时工作区未提交干净 |
| `RUN_ACTIVE` / `RUN_NOT_FOUND` | 2 | 尚有运行中作业或 `--run` 指定了不存在的轮次 |
| `TRIAGE_PENDING` (`status` 返回 `1`) | 1 | `triage status` 检测到仍有未经 `triage review` 的分诊任务 |

## 2. 经典踩坑提示

1. **`Bug` 与 `Todo` 的编号空间共享**：在同一个 `Case` 内，`bug add` 占用了 `#1` 后，紧接着 `todo add` 会自动分配 `#2`；使用 `todo done <case>#<id>` 时以命令行返回的 `ref` 为准。
2. **`run.mk` 产物目录位置**：仿真日志和事实文件保存在 `.solouvm/runs/<run_id>/<job_id>/try<N>/`（注意路径下没有 `jobs/` 中间目录），`launch.sh` 的总运行输出记录在 `.solouvm/runs/<run_id>/launch.log`。
3. **重判而不重跑**：修改 `checks` / `waivers` / `expect` 后不必再跑仿真，直接执行 `solouvm judge` 即可快速刷新 `results.json` 与文本/JUnit 报告。
4. **`--limit` 的双重语义**：在列表类命令（`list` / `notes` / `history` / `files`）中 `--limit N` 截断行数、`--limit 0` 表示全量显示；在 `triage packet` 中 `--limit` 控制分诊包字符预算。
5. **非 UVM 用例触发 Rule 12 失败**：不跑 UVM 的纯 SV 用例若使用自定义脚本而非 UVM Summary，需在仿真结束前打印 `SOLOUVM_DONE status=pass`，否则判定规则 12 会判其 `fail`；另外请同步检查是否意外沿用了需要 `[AXI_SCB]` / `sim time` 的默认 `expect=[...]`。

## 3. 尚未经真实 EDA 工具验证清单

在无商业工具许可证环境外使用 VCS / Xcelium / Questa / Xezim / Verilator / `slang` / Verdi / LSF 时，请先用内置 `solouvm doctor` 与单 case `solouvm run --case <name>` 实测，重点核对 [doc/requirements.md §26](../requirements.md#26-不确定事项) 中的 17 项实机变量（编译键 incdir 耗时、各版本 UVM 选项与结束打印 regex、参数化 interface 下的 `fake_top`、`slang` 参数与 `${VAR}` 展开等）。
