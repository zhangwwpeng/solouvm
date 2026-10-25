// Demo testbench top. The demo "simulator" is tb/fake_sim.py,
// so this file only needs to be a valid compilation unit.
module tb_top;

    logic clk = 1'b0;
    logic rst_n = 1'b0;
    logic en = 1'b0;
    logic [7:0] count;

    counter #(.WIDTH(8)) u_counter (
        .clk   (clk),
        .rst_n (rst_n),
        .en    (en),
        .count (count)
    );

    always #5 clk = ~clk;

    initial begin
        rst_n = 1'b0;
        #20 rst_n = 1'b1;
        en = 1'b1;
        #200 $finish;
    end

endmodule
