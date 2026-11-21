# 04 编写 Case、Expect 与 Waiver

每个验证点都是一个 `Case(...)` 对象，写在根目录 `case.py`（`CASES = [...]`）或 `CFG["case_dirs"]` 下的 `CASE = Case(...)` / `CASES = [...]` 中。

## 1. `Case` 字段速查

| 字段 | 类型与默认值 | 说明 |
|------|--------------|------|
| `name` | 必填 `str` | 允许 `[A-Za-z0-9_.-]+`，禁止含 `__`（`__` 保留给 `<case>__<seed>`） |
| `uvm_test` | `str \| None = None` | 非空时追加 `+UVM_TESTNAME=<name>`；可覆盖全局 `CFG["uvm_test"]` |
| `params` / `opts` / `env` | `{}` / `[]` / `{}` | `+PARAM=VAL` 传参 / 额外命令行选项 / 单 case 环境变量 |
| `build` | `str = "default"` | 关联 `CFG["builds"]` 中的构建模式 |
| `seeds` | `str \| list[int] \| None = None` | 覆盖 `regress.seeds`（支持 `"1-5,42"`、`"random:3"` 或 `[1, 2]`） |
| `timeout` / `hang` / `retry` | `None` | 覆盖全局单 job 超时、无输出挂死判定时长与重试次数 |
| `checks` | `dict = {}` | 覆盖 `max_uvm_error` / `max_uvm_fatal` / `fail_regex` 等，其中 `waivers` **追加**而非替换全局 |
| `expect_fail` | `bool \| str = False` | 负向测试：`True` 或错误匹配正则；仅匹配失败行时判定 `xfail`（算通过） |
| `tags` | `tuple[str, ...] = ()` | 用于 `--tag` / `--exclude-tag` 选择 |
| `owner` / `stage` | `None` / `"wip"` | 阶段：`planned` / `wip` / `ready` / `signoff` / `deprecated` |
| `intent` / `method` / `spec_refs` | `""` / `""` / `[]` | 验证目标、激励与检查方法、规格条目编号 |
| `expect` | `list[Expect] = []` | **机械防虚假 pass 检查条件**（推荐作为默认最后一个执行字段或紧接 `notes` 前） |
| `doc` / `notes` | `""` / `[]` | 详细说明（自动 dedent）与 `Bug` / `Todo` / `Log` / `Note` / `Triage` 记录 |

> **Stage 准入门槛**：`stage="planned"` 不进入默认运行与生成（仅 `--case` 显式点名时会以 `CASE_PLANNED` 报错，退出码 2）；`ready` / `signoff` 起应写全 `intent`、`method` 及 `expect` 等信息（`validate` 会严格检查并报告缺失项）。

## 2. 使用 `Suite` 复用公共配置

`Suite(name=..., cases=[...], ...)` 为一组 case 提供公共默认参数：
- `tags`：去重并保留顺序地合并；`params` / `env`：case 覆盖 suite；`opts`：`suite.opts + case.opts`。
- `build`（当 case 为 `"default"` 时继承）、`stage`（当 case 为 `"wip"` 时继承）、`seeds` / `timeout` / `hang` / `retry` / `owner`（当 case 为 `None` 时继承）。

## 3. 用 `Expect` 拦截虚假 pass

`Expect` 至少填写 `scoreboard`、`regex`、`sim_time_min`、`uvm_info_min` 之一：

```python
expect=[
    Expect(scoreboard="AXI_SCB", min=100),                  # [AXI_SCB] compared=<N> >= 100
    Expect(regex=r"\[BURST_COV\] hit", count_min=4),        # 日志至少命中 4 次
    Expect(sim_time_min="10us"),                            # 仿真结束时间 >= 10us
    Expect(uvm_info_min=1, id="TEST_DONE"),                 # 指定 UVM ID 至少出现 N 次
]
```

任意 `Expect` 不满足即判定为 `fail`（`ready` 阶段也必须至少具备可检验信号或说明，避免“什么都没跑但 UVM_ERROR=0”的虚假 pass）。

## 4. 用 `Waiver` 豁免已知问题

```python
Waiver(
    reason="已知 VIP 在复位前产生一次虚假 WARNING",
    id="VIP_RST",                       # 或使用 regex=r"..."
    severity=("ERROR", "WARNING"),      # 允许 INFO / WARNING / ERROR / FATAL / SIM
    max=2,                              # 单次仿真最多豁免 2 条
    until="2026-12-31",                 # 到期后自动失效，results.json 中报告 expired
)
```

- 仅允许豁免配置里列出的 severity，且受 `max` 次数与 `until` 截止日期约束。
- 全局 `checks.waivers` 与当前 `Case` 匹配的豁免会统一统计入报告 `waivers`（`used` / `unused` / `expired`）。
