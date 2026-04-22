`timescale 1ns/1ps

module tb_ptw_wrapper (
    input  logic clk_i,
    input  logic rst_ni,
    input  logic init_ptw_i,
    
    // Python (cocotb) とやり取りするシンプルなポート
    output logic [63:0] mem_rd_req_addr_o,
    output logic        mem_rd_req_valid_o,
    input  logic [63:0] mem_rd_resp_data_i,
    input  logic        mem_rd_resp_valid_i,
    
    // テスト対象の設定ピン
    input  logic [43:0] iosatp_ppn_i,
    input  logic [43:0] iohgatp_ppn_i,
    input  logic [63:0] req_iova_i,
    input  logic        en_1S_i,
    input  logic        en_2S_i,
    
    // 出力
    output logic        update_o,
    output logic [63:0] up_1S_content_o,
    output logic [63:0] up_2S_content_o,
    output logic [28:0] up_vpn_o,
    output logic        ptw_error_o,
    output logic        mem_rd_resp_ready_o
);

    // ==========================================
    // 完全版 ダミーAXI構造体 (Verilatorを黙らせるフルセット)
    // ==========================================
    typedef struct packed {
        logic [3:0]  id;     logic [63:0] addr;   logic [7:0] len;    logic [2:0] size;
        logic [1:0]  burst;  logic        lock;   logic [3:0] cache;  logic [2:0] prot;
        logic [3:0]  qos;    logic [3:0]  region; logic [5:0] atop;   logic       user;
    } dummy_aw_t;

    typedef struct packed {
        logic [63:0] data;   logic [7:0]  strb;   logic       last;   logic       user;
    } dummy_w_t;

    typedef struct packed {
        logic [3:0]  id;     logic [63:0] addr;   logic [7:0] len;    logic [2:0] size;
        logic [1:0]  burst;  logic        lock;   logic [3:0] cache;  logic [2:0] prot;
        logic [3:0]  qos;    logic [3:0]  region; logic       user;
    } dummy_ar_t;

    typedef struct packed {
        logic [3:0]  id;     logic [63:0] data;   logic [1:0] resp;   logic       last;   
        logic        user;
    } dummy_r_t;

    typedef struct packed {
        dummy_aw_t aw;       logic aw_valid;
        dummy_w_t  w;        logic w_valid;
        logic      b_ready;
        dummy_ar_t ar;       logic ar_valid;
        logic      r_ready;
    } tb_axi_req_t;

    typedef struct packed {
        logic       aw_ready;
        logic       w_ready;
        logic [3:0] b_id;    logic [1:0] b_resp;  logic       b_valid; logic b_user;
        logic       ar_ready;
        dummy_r_t   r;       logic r_valid;
    } tb_axi_rsp_t;

    tb_axi_req_t req_from_ptw;
    tb_axi_rsp_t rsp_to_ptw;

    // ==========================================
    // Pythonへの信号接続
    // ==========================================
    assign mem_rd_req_addr_o  = req_from_ptw.ar.addr;
    assign mem_rd_req_valid_o = req_from_ptw.ar_valid;
    assign mem_rd_resp_ready_o = req_from_ptw.r_ready;

    always_comb begin
        rsp_to_ptw = '0;
        rsp_to_ptw.ar_ready = 1'b1;
        rsp_to_ptw.r.data   = mem_rd_resp_data_i;
        rsp_to_ptw.r_valid  = mem_rd_resp_valid_i;
        rsp_to_ptw.r.resp   = 2'b00;
        rsp_to_ptw.r.last   = 1'b1;
    end

    // ==========================================
    // PTWインスタンス化 (全ピンを厳密に接続)
    // ==========================================
    rv_iommu_ptw_sv39x4_pc #(
        .axi_req_t(tb_axi_req_t),
        .axi_rsp_t(tb_axi_rsp_t)
    ) u_ptw (
        .clk_i        (clk_i), 
        .rst_ni       (rst_ni), 
        .init_ptw_i   (init_ptw_i),
        .mem_req_o    (req_from_ptw), 
        .mem_resp_i   (rsp_to_ptw),
        .iosatp_ppn_i (iosatp_ppn_i), 
        .iohgatp_ppn_i(iohgatp_ppn_i), 
        .req_iova_i   (req_iova_i),
        .update_o     (update_o),
        .up_1S_content_o    (up_1S_content_o),
        .up_2S_content_o    (up_2S_content_o),
        .up_vpn_o     (up_vpn_o), 
        .ptw_error_o  (ptw_error_o),
        
        // --- 必須の入力ピン (固定値で安全に縛る) ---
        .en_1S_i      (en_1S_i),
        .en_2S_i      (en_2S_i),
        .is_r_i       (1'b1),
        .is_w_i       (1'b0),
        .is_x_i       (1'b0),
        .is_prev_i    (1'b0),
        .msi_en_i     (1'b0),
        .msi_addr_mask_i   ('0),
        .msi_addr_pattern_i('0),
        .is_cdw_req_i (1'b0),
        .pscid_i      ('0),
        .gscid_i      ('0),
        
        // --- 使わない出力ピン (空括弧で明示的に無視する) ---
        .ptw_active_o       (), 
        .ptw_error_2S_o     (), 
        .ptw_error_2S_int_o (), 
        .cause_code_o       (),
        .up_1S_2M_o         (), 
        .up_1S_1G_o         (), 
        .up_2S_2M_o         (), 
        .up_2S_1G_o         (),
        .up_pscid_o         (), 
        .up_gscid_o         (),  
        .gpaddr_is_msi_o    (), 
        .msi_vpn_o          (), 
        .msi_1S_2M_o        (),
        .msi_1S_1G_o        (),
        .msi_gpte_o         (),
        .fault_gpa_o        ()
    );

    // 波形出力設定
    initial begin
        $dumpfile("dump.vcd");
        $dumpvars(0, tb_ptw_wrapper);
    end

endmodule