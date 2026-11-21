# System Verification Guide

## 1. DUT 功能概述与接口

- Sample DUT: `axi_slv_dummy` (`rtl/axi_slv_dummy.sv`)
- Interface: `axi_if` (`aclk`, `aresetn`, `awaddr`, `awlen`, `awvalid`, `awready`)

## 2. 验证环境结构

- Agents: `axi_mst` (master, coverage enabled) and `axi_slv_mon` (passive monitor)
- Environment: `axi_env` with `axi_scb` scoreboard and `axi_vsqr` virtual sequencer
- Tests: `verif/tests/axi/axi_base_test.sv`, `verif/tests/axi/axi_smoke_test.sv`

## 3. 日志约定

- Component UVM message IDs use uppercase class names (`AXI_MST_DRV`, `AXI_SCB`, etc.).
- Scoreboard summary format in `report_phase`:
  `[AXI_SCB] compared=<N> matched=<M> mismatched=<K> pending=<P>`
- Virtual sequence start/end format:
  `[AXI_BASE_VSEQ] start` / `[AXI_BASE_VSEQ] done items=<N>`

## 4. 已知限制与不验证的部分

- `axi_slv_dummy` is a minimal starter stub; replace it with the real RTL DUT.

## 5. 调试指引

- Top-level testbench: `verif/tb/tb_top.sv` (`u_axi_if`, DUT `u_dut`).
- Key signals: `tb_top.aclk`, `tb_top.aresetn`, `awvalid`, `awready`.
