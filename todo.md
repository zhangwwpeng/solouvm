# TODO

实现进度。需求本身（内容、优先级、理由）见 [doc/requirements.md](doc/requirements.md)，里程碑划分见其 §25，不确定事项见其 §26。

## 文档

- [x] README.md
- [x] doc/requirements.md（含里程碑、不确定事项）
- [x] doc/architecture.md
- [x] doc/config.md
- [x] doc/cli.md
- [x] doc/verdict.md
- [x] doc/fast-compile.md
- [x] doc/formats.md
- [x] doc/decisions.md
- [x] doc/ai-workflow.md
- [x] doc/uvm-style.md
- [x] 第一轮自查评审，按评审意见修订（2026-11-03）
- [x] 用户最终 review：D41–D56 已确认（2026-11-14）
- [x] 评审发现的 7 处不一致已修订（2026-11-14）
- [x] doc/guide/ 中文使用手册
- [x] skills/solouvm/ AI Agent Skill
- [ ] 逐项验证 requirements.md §26 的不确定事项（需要真实工具环境）

## 依赖

- [x] vendor rich 15.0.0、pygments 2.20.0、markdown-it-py 4.2.0、mdurl 0.1.2

## 代码

- [x] M1 核心闭环（本机），含 `fake` 端到端流程与 CLI 分发。见[需求与里程碑](doc/requirements.md#25-里程碑归属)。
- [x] M2 远程与重跑（`ssh`/`lsf`、`rsync`/`git`、`rerun`/`status`/`stop`、`bug`/`note`/`todo`、`history`/`report --compare`/HTML）
- [x] M3 fast compile（`solouvm check`：slang、`fake_top`、`incremental` 与 `lint-style`、`uvm sync/diff`）
- [x] M4 周边（`cov merge/report`、`wave`、`progress`、`doc`、`doctor`、`validate`、`help`、弱信号）
