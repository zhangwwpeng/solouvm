# 09 Fast Compile、覆盖率与波形查询

## 1. 三级增量 Fast Compile（`solouvm check`）

`solouvm check` 旨在几秒内暴露大多数语法、类型与端口连接错误，减少排队等待商业仿真器全编的耗时（[fast-compile.md](../fast-compile.md)）：

```bash
solouvm check                       # 顺序执行可用的快速检查通道
solouvm check --slang-only          # 仅执行 slang 静态检查
solouvm check --no-faketop          # 跳过 fake_top 空顶层编译
solouvm check --where local|remote  # 指定在本机还是远端执行
solouvm check --format gcc          # 支持 text / gcc / json（GCC 诊断格式便于 IDE 跳转）
solouvm check --incremental         # 对 RTL 启用按模块哈希的增量预编译检查
```

三个检查层级：
1. **slang 快速解析**：在本机（若本机无 `slang` 且配置了 `where="auto"` 则探远端）调用 `slang` 解析 RTL/TB，支持按 `check.exclude` 过滤第三方宏不兼容文件。
2. **UVM 侧 `fake_top`**：自动剥离只保留模块壳定义、不例化完整 DUT 的最小顶层，在商业仿真器只做编译不做 elaboration/连接的前提下验证 UVM 测试平台编译。
3. **RTL 增量预检**：按设计单元缓存状态哈希，仅对变更文件重检。

> **重要实机说明**：本环境无 `slang`、VCS、Xcelium 或 Questa 安装。`slang` 对大型 UVM 库的参数与 `${VAR}` 解析表现、参数化 `interface` 下 `fake_top` 能否顺利通过三大商业仿真器等细节，均列于 [requirements.md §26](../requirements.md#26-不确定事项) 第 6–7 项，需在真实 EDA 环境逐项确认；`E-06 ~ E-08` 与 `E-10` 当前为 **待定**。

## 2. 覆盖率合并与汇总（`cov`）

前提：在 `solouvm.conf.py` 对应 `build` 中开启 `"coverage": True`（由构建键与运行命令开启工具覆盖率插桩）。

```bash
solouvm cov merge [--run RUN_ID] [-o DIR]
solouvm cov report [--run RUN_ID] [--merged-dir DIR]
```

- **大库留在 B 侧**：覆盖率数据库永远留在执行机 B 的工作目录，只在 B 上执行工具合库与生成报告摘要，再由 A 读回文本与数值指标。
- 内置支持的合并/报告工具映射：
  - `vcs` -> `urg`
  - `xrun` -> `imc`
  - `questa` -> `vcover`
  - `verilator` -> `verilator_coverage`（仅 report；可配 `CFG["sim"]["cov_merge"]` / `CFG["sim"]["cov_report"]` 自定义命令）

> 注意：真实商业库上的 `urg` / `imc` / `vcover` 文本输出解析细节需按具体版本核对（`§26` 第 5 项）；覆盖率阈值门禁（`V-05`）属 **待定**。

## 3. 文本波形查询（`solouvm wave`）

`solouvm wave` 允许 AI 或用户在终端直接检索任意时间窗口内的信号跳变，而不把几百 MB 的波形库传回控制机：

```bash
solouvm wave smoke__1 --signals "tb_top.dut.aw*" --from 0ns --to 1000ns
solouvm wave smoke__1 --signals tb_top.aclk,tb_top.aresetn --all
```

- **VCD 原生流式解析**：对 `.vcd` 文件直接在 Python 中流式切片、匹配通配信号并按 `--changes`（默认仅输出变化时刻）或 `--all` 渲染 ASCII 表格。
- **专有格式（`.fsdb` / `.wlf` / `.shm`）**：通过执行机侧可配置的 `CFG["sim"]["wave_export"]`（或内置探针命令调用工具转成局部时间窗 VCD / 文本表格）回传结果；具体厂商导出工具在 `requirements.md §26` 第 13 项中标注需按站点环境配置核验。
