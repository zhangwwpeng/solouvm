# 07 记录系统（Notes）与 AI 分诊

`case.py` 中的 `notes=[...]` 让 case 同时承担需求说明、Bug 登记本与调试日志的角色；命令行写回采用基于 Python `ast` 的精确源码编辑，并配合目录锁防并发写坏文件。

## 1. 记录查询与写入命令

```bash
solouvm notes                               # 默认列出全部未关闭 bug 与未完成 todo
solouvm notes --type log --since 2026-11-01 # 按类型/负责人/日期/标签/overdue 过滤
solouvm bug add smoke --job smoke__1 --title "数据对齐错" --body -
solouvm bug update smoke#1 --status fixed
solouvm note add smoke --text "4417 种子在第 12 拍复位未拉低"
solouvm todo add smoke --title "补 narrow burst" --due 2026-12-01
solouvm todo done smoke#2                   # 注意 Bug 与 Todo 在同一 case 内共享递增 ID 空间
```

- 全局验证进度总览：`solouvm progress` 统计各 `stage` 数量、最近通过率、各 `owner` 维度统计、逾期 `todo` 与状态矛盾提示（如已标记 `fixed` 的种子在新一轮已跑通，提示可升级为 `verified`）。
- 导出双语/纯文档报告：`solouvm doc -o docs/cases --lang zh` 为每个 case 生成 Markdown 及总索引 `index.md`。

## 2. 主次协同分诊工作流（`--role` 权限边界）

solouvm 严格区分 **`main`（默认）** 与 **`triage`（受限子 agent）** 两种角色（[ai-workflow.md](../ai-workflow.md)）：

- **`--role triage` 只允许**：
  - 只读排查：`config`, `files`, `validate`, `schema`, `list`, `uvm diff`, `lint-style`, `status`, `log`, `explain`, `wave`, `history`, `notes`, `progress`, `triage packet`, `triage status`, `help`, `--version`
  - 追加记录：`bug add`, `bug update`（限改自己建且尚未被审查的 Bug）, `note add`, `todo add`, `triage write`
- **`--role triage` 禁止**（触发 `ROLE_DENIED`，退出码 2）：
  - 执行与修改环境：`init`, `new`, `uvm sync`, `gen`, `build`, `run`, `regress`, `rerun`, `check`, `stop`, `judge`, `report`, `todo done`, `triage plan`, `triage review`, `cov merge/report`, `clean`

## 3. 五步标准分诊闭环

1. **生成任务**（`main`）：
   ```bash
   solouvm triage plan --pass-policy changed --group-by signature
   ```
   按失败签名归并失败任务，并根据 `pass_policy`（`none` / `changed` / `all`）或弱信号为高风险通过生成抽查任务，写入 `.solouvm/runs/<run_id>/triage/tasks.json`。
2. **提取上下文包**（`triage`）：
   ```bash
   solouvm --role triage triage packet <task_id>
   ```
   在字符预算 `triage.packet_limit`（默认 20000）内输出 7 节固定结构：任务指令、系统文档、Case 说明、失败结果与日志片段、源码上下文、历史与 Git 提交、本地/波形复现指令。
3. **写回分诊结论**（`triage`）：
   ```bash
   solouvm --role triage triage write <task_id> \
     --verdict design_bug --confidence high \
     --summary "AXI write mux dropped upper byte" \
     --evidence "log:4" --bug smoke#1 --doc - <<'EOF'
   ## 现象
   ...
   ## 排查过程
   ...
   ## 证据
   ...
   ## 结论
   ...
   ## 建议
   ...
   EOF
   ```
   > 必须提供 `--evidence`（匹配 `log:<行>`、`<文件>:<行>` 或 `wave:<时间> <信号>`），且调试文档须含 `## 现象`、`## 排查过程`、`## 证据`、`## 结论`、`## 建议` 五个非空章节。
4. **主角色审查**（`main`）：
   ```bash
   solouvm triage status        # 存在未审查任务时返回退出码 1
   solouvm triage review <task_id> --accept
   solouvm triage review <task_id> --reject "证据不足，需核查第 240 行波形"
   ```
   - `--accept`：把 `Triage.review` 写入 case 并触发重判（若为 `false_pass` 则覆盖结果状态）。
   - `--reject`：标记驳回并将该次提交新建关联但未生效的 Bug 自动置为 `invalid`。
5. **过时标记（Stale）**：若案例随后重跑状态变化，`triage status` 自动把旧任务标为 `stale`，提醒复核。
