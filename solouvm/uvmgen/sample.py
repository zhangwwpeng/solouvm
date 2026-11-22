"""User-region bodies for the runnable AXI sample created by init."""

from __future__ import annotations

import re
from pathlib import Path

_DUT = """\
  axi_slv_dummy u_dut (
    .aclk    (aclk),
    .aresetn (aresetn),
    .awaddr  (u_axi_if.awaddr),
    .awlen   (u_axi_if.awlen),
    .awvalid (u_axi_if.awvalid),
    .awready (u_axi_if.awready)
  );
"""

_BODIES = {
    ("tb_top.sv", "dut"): _DUT,
    ("axi_mst_drv.sv", "drive_item"): """\
    wait (vif.aresetn);
    @(negedge vif.aclk);
    vif.awaddr = item.addr;
    vif.awlen = item.len;
    vif.awvalid = 1'b1;
    do @(posedge vif.aclk); while (!vif.awready);
    @(negedge vif.aclk);
    vif.awvalid = 1'b0;
""",
    ("axi_slv_mon_mon.sv", "run_phase"): """\
    forever begin
      axi_slv_mon_txn item;
      @(vif.mon_mp.mon_cb);
      if (vif.mon_mp.mon_cb.awvalid && vif.mon_mp.mon_cb.awready) begin
        item = axi_slv_mon_txn::type_id::create("item");
        item.addr = vif.mon_mp.mon_cb.awaddr;
        item.len = vif.mon_mp.mon_cb.awlen;
        ap.write(item);
      end
    end
""",
    ("axi_base_vseq.sv", "body"): """\
    begin
      axi_mst_txn item;
      item = axi_mst_txn::type_id::create("item");
      start_item(item, -1, p_sequencer.axi_mst);
      item.addr = 32'h100;
      item.len = 8'd3;
      finish_item(item);
      items++;
    end
""",
    ("axi_scb.sv", "run_phase"): """\
    axi_slv_mon_txn item;
    axi_slv_mon_fifo.get(item);
    compared++;
    if (item.addr == 32'h100 && item.len == 8'd3) begin
      matched++;
    end else begin
      mismatched++;
      `uvm_error("AXI_SCB", $sformatf("unexpected addr=%h len=%0d", item.addr, item.len))
    end
""",
}


def sample_body(path: Path, region: str) -> str | None:
    return _BODIES.get((path.name, region))


def inject_sample(path: Path, text: str) -> str:
    for (filename, region), body in _BODIES.items():
        if filename == path.name:
            text = re.sub(
                rf"(^[ \t]*// solouvm:user begin {region}\n).*?"
                rf"(^[ \t]*// solouvm:user end {region}\n)",
                lambda match, body=body: match[1] + body + match[2],
                text,
                flags=re.MULTILINE | re.DOTALL,
            )
    return text
