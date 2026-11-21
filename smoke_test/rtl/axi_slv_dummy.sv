module axi_slv_dummy (
  input  logic        aclk,
  input  logic        aresetn,
  input  logic [31:0] awaddr,
  input  logic [7:0]  awlen,
  input  logic        awvalid,
  output logic        awready
);
  always_ff @(posedge aclk or negedge aresetn) begin
    if (!aresetn) begin
      awready <= 1'b0;
    end else begin
      awready <= awvalid;
    end
  end
endmodule : axi_slv_dummy
