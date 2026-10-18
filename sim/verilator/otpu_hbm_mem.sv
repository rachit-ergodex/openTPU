// Experimental direct-port HBM2 timing adapter. This is not the AWS AXI shell.
// Requests are captured on rising edges; returned data/ready are driven on falling edges.
module otpu_hbm_mem #(
  parameter int WORDS = 1 << 18, D = 128, SID = 0
) (
  input logic clk, rst, dump,
  input logic a_req, a_we,
  input logic [31:0] a_addr, a_wdata,
  input logic [3:0] a_be,
  output logic a_rdy, a_rvalid,
  output logic [31:0] a_rdata, a_rdata2,
  input logic sw_req,
  input logic [31:0] sw_addr, sw_wdata,
  input logic [3:0] sw_be,
  output logic sw_rdy,
  input logic b_req, b_we, b_tag,
  input logic [31:0] b_addr,
  input logic [D/4-1:0] b_wmask,
  input logic [D*8-1:0] b_wdata,
  output logic b_rdy, b_rvalid, b_rtag,
  output logic [D*8-1:0] b_rdata,
  output logic wr_idle, rd_idle
);
  import "DPI-C" function int otpu_hbm_init(input string path);
  import "DPI-C" function string otpu_hbm_error();
  import "DPI-C" function int otpu_hbm_load(input int sid, input string path);
  import "DPI-C" function int otpu_hbm_dump(input int sid, input string path);
  import "DPI-C" function int otpu_hbm_ready(input int sid, input int port_id);
  import "DPI-C" function int otpu_hbm_submit(
    input int sid, input int port_id, input longint unsigned addr,
    input int size, input int we, input int tag,
    input bit [D*8-1:0] data, input bit [D-1:0] mask);
  import "DPI-C" function int otpu_hbm_advance(input longint unsigned cycle);
  import "DPI-C" function int otpu_hbm_poll(
    input int sid, input int port_id, output bit [D*8-1:0] data, output int tag);
  import "DPI-C" function int otpu_hbm_idle(input int sid, input int writes);
  import "DPI-C" function longint unsigned otpu_hbm_count(input int pc, input int kind);
  string dir, config_file;
  longint unsigned cycle = 0;
  bit [D*8-1:0] payload, response;
  bit [D-1:0] mask;
  int rc, tag, got_a, got_b;
  task check(input int value);
    if (value < 0) $fatal(1, "HBM: %s", otpu_hbm_error());
  endtask
  initial begin
    if (D < 8 || D > 4096 || D % 4) $fatal(1, "invalid HBM chunk size");
    if (!$value$plusargs("dir=%s", dir) || !$value$plusargs("hbm_config=%s", config_file))
      $fatal(1, "HBM requires +dir and +hbm_config");
    check(otpu_hbm_init(config_file));
    check(otpu_hbm_load(SID, $sformatf("%s/dram_%0d.bin", dir, SID)));
    a_rdy = 0; b_rdy = 0; sw_rdy = 0;
    a_rvalid = 0; b_rvalid = 0; b_rtag = 0;
    a_rdata = 0; a_rdata2 = 0; b_rdata = 0;
    wr_idle = 1; rd_idle = 1;
  end
  always @(posedge clk) if (!rst) begin
    if (a_req && a_rdy) begin
      payload = '0; payload[31:0] = a_wdata;
      mask = '0; mask[3:0] = a_be;
      rc = otpu_hbm_submit(SID, 0, a_we ? (64'(a_addr) << 2) :
          ((64'(a_addr) << 2) & ~64'd7), a_we ? 4 : 8, int'(a_we), int'(a_addr[0]),
          payload, mask);
      check(rc); if (rc != 1) $fatal(1, "HBM A queue rejected an advertised ready");
    end
    if (b_req && b_rdy) begin
      mask = '0;
      for (int w = 0; w < D/4; w++) mask[4*w +: 4] = {4{b_wmask[w]}};
      rc = otpu_hbm_submit(SID, 1, 64'(b_addr) << 2, D, int'(b_we), int'(b_tag),
                           b_wdata, mask);
      check(rc); if (rc != 1) $fatal(1, "HBM B queue rejected an advertised ready");
    end
    if (sw_req && sw_rdy) begin
      payload = '0; payload[31:0] = sw_wdata;
      mask = '0; mask[3:0] = sw_be;
      rc = otpu_hbm_submit(SID, 2, 64'(sw_addr) << 2, 4, 1, 0, payload, mask);
      check(rc); if (rc != 1) $fatal(1, "HBM SW queue rejected an advertised ready");
    end
    if (dump) begin
      check(otpu_hbm_dump(SID, $sformatf("%s/dram_out_%0d.bin", dir, SID)));
      if (SID == 0)
        for (int pc = 0; pc < 32; pc++)
          $display("HBM pc=%0d read_tx=%0d write_tx=%0d rejected=%0d", pc,
            otpu_hbm_count(pc, 0), otpu_hbm_count(pc, 1), otpu_hbm_count(pc, 2));
    end
  end
  always @(negedge clk) if (!rst) begin
    // All instances call the same idempotent time advance, so S slices share one HBM.
    cycle = cycle + 1;
    check(otpu_hbm_advance(cycle));
    response = '0;
    rc = otpu_hbm_poll(SID, 0, response, tag); check(rc);
    got_a = rc;
    a_rvalid <= (rc == 1);
    if (rc == 1) begin
      a_rdata <= tag == 0 ? response[31:0] : response[63:32];
      a_rdata2 <= tag == 0 ? response[63:32] : response[31:0];
    end
    response = '0;
    rc = otpu_hbm_poll(SID, 1, response, tag); check(rc);
    got_b = rc;
    b_rvalid <= (rc == 1);
    if (rc == 1) begin b_rdata <= response; b_rtag <= 1'(tag); end
    rc = otpu_hbm_ready(SID, 0); check(rc); a_rdy <= (rc == 1);
    rc = otpu_hbm_ready(SID, 1); check(rc); b_rdy <= (rc == 1);
    rc = otpu_hbm_ready(SID, 2); check(rc); sw_rdy <= (rc == 1);
    rc = otpu_hbm_idle(SID, 1); check(rc); wr_idle <= (rc == 1);
    rc = otpu_hbm_idle(SID, 0); check(rc);
    // Keep read-idle low until returned valid pulses have been consumed.
    rd_idle <= (rc == 1) && !got_a && !got_b;
  end
endmodule
