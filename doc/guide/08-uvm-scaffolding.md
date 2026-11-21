# 08 UVM 脚手架与风格规范

为了保证团队和多个 AI Agent 生成的 UVM 环境结构高度一致，solouvm 内置了 25 份 **Style 1** SystemVerilog 模板并严格限定用户可编辑区域（[uvm-style.md](../uvm-style.md)）。

## 1. 标准目录布局

```text
verif/
  agents/<agent>/          # <agent>_if.sv, <agent>_pkg.sv, *_files.svh, tx/drv/mon/sqr/cov...
  envs/<env>/              # <env>_env_pkg.sv, *_env_files.svh, env/scb/vsqr/config...
  tests/<env>/             # <env>_test_pkg.sv, *_test_files.svh, base_test/具体 test/vseq...
  tb/                      # tb_top.sv, binds.sv
```

配合默认 `filelist.py` 通配规则，在对应目录下新建 agent、env 或 test 时**无需改动 `filelist.py`**，只需维护 `<pkg>_files.svh`（由脚手架自动增删行）。

## 2. 声明式规格文件 `tbspec.py`

在项目根目录的 `tbspec.py` 用 Python 描述接口、字段与环境拓扑（`solouvm.uvm`）：

```python
from solouvm.uvm import Agent, Env, Field, Interface, Signal

AXI = Interface(
    name="axi",
    clock="aclk",
    reset=("aresetn", "low"),
    signals=[
        Signal("awaddr", 32, "m2s"),
        Signal("awready", 1, "s2m"),
    ],
)

ENVS = [
    Env(
        name="axi",
        agents=[
            Agent(name="axi_mst", interface=AXI, role="master", fields=[Field("addr", 32)], coverage=True),
            Agent(name="axi_slv_mon", interface=AXI, role="monitor", like="axi_mst"),
        ],
        scoreboard=True,
        ref_model=False,
    ),
]
```

## 3. 生成与双向安全同步命令

```bash
solouvm new agent apb                 # 生成完整 APB agent 骨架
solouvm new env pcie --agents mst,mon # 生成 env 与指定 agent
solouvm new test axi_wrap_test --env axi
solouvm new seq axi_wr_seq --agent axi_mst
solouvm new vseq axi_burst_vseq --env axi
solouvm uvm diff                      # 预览 tbspec.py 变更引发的 diff
solouvm uvm sync [--dry-run]          # 重新渲染生成区，完整保留用户代码区
```

## 4. 用户代码区规则（极其重要）

所有支持 `uvm sync` 的模板文件都包含成对标记：

```systemverilog
// solouvm:user begin <region_name>
// 在此处编写驱动、monitor、约束或检查逻辑
// solouvm:user end <region_name>
```

- 同步机制只会用新模板替换**标记之外**的声明与脚手架代码，原封不动迁移所有 `user begin/end` 内部逻辑。
- **切勿编辑标记之外的代码**：若标记外被修改，`uvm sync` 会报错拒绝覆盖以保护手工修改；若因接口删除等原因导致某用户区不再存在，冲突文件会被写为 `*.orphan` 备份而不直接丢弃。
- **文件级扩展**：用户额外新增的 `.sv`/`.svh` 只放在 `<pkg>_files.svh`（用 `// solouvm:files begin/end` 包裹）即可随包编译。

## 5. 风格静态检查（`lint-style`）

```bash
solouvm lint-style
```

`lint-style` 自动检查：文件命名后缀与所属目录匹配、`// solouvm:user begin/end` 配对与非嵌套完整性、`*_files.svh` 引用的物理文件真实存在且无遗漏或孤立条目、UVM 日志格式化规范等；发现问题返回退出码 `1`。

> 说明：sequence 继承顺序在 `<pkg>_files.svh` 中的重排、顶层 vseq 在大型项目中的日志量、嵌套 env 顺序与 RAL 模型目录生成规范，属 [requirements.md §26](../requirements.md#26-不确定事项) 第 9–12 项待实机工程核验内容。
