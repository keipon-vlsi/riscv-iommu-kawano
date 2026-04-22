// Copyright © 2023 Manuel Rodríguez & Zero-Day Labs, Lda.
// SPDX-License-Identifier: Apache-2.0 WITH SHL-2.1

// Licensed under the Solderpad Hardware License v 2.1 (the “License”); 
// you may not use this file except in compliance with the License, 
// or, at your option, the Apache License version 2.0. 
// You may obtain a copy of the License at https://solderpad.org/licenses/SHL-2.1/.
// Unless required by applicable law or agreed to in writing, 
// any work distributed under the License is distributed on an “AS IS” BASIS, 
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. 
// See the License for the specific language governing permissions and limitations under the License.
//
// Author: Manuel Rodríguez <manuel.cederog@gmail.com>
// Date: 16/01/2023
// Acknowledges: SSRC - Technology Innovation Institute (TII)
//
// Description: RISC-V IOMMU Hardware Page Table Walker (PTW). Translation scheme Sv39x4.
//              This module is an adaptation of the CVA6 Sv39 MMU developed by
//              David Schaffenrath and Florian Zaruba; and the CVA6 Sv39x4 TLB
//              developed by Bruno Sá.
//              Includes MSI translation support parameterizable.
//              Includes support for CDW implicit translations when walking the PDT.

module rv_iommu_ptw_sv39x4_pc #(

    // MSI translation support
    parameter rv_iommu::msi_trans_t MSITrans    = rv_iommu::MSI_DISABLED,

    /// AXI Full request struct type
    parameter type  axi_req_t       = logic,
    /// AXI Full response struct type
    parameter type  axi_rsp_t       = logic
) (
    input  logic                    clk_i,                  // Clock
    input  logic                    rst_ni,                 // Asynchronous reset active low
    
    // Trigger PTW
    input  logic                    init_ptw_i,

    // Error signaling
    output logic                                ptw_active_o,           // Set when PTW is walking memory
    output logic                                ptw_error_o,            // set when an error occurred (excluding access errors)
    output logic                                ptw_error_2S_o,         // set when the fault occurred in stage 2
    output logic                                ptw_error_2S_int_o,     // set when an error occurred in stage 2 during stage 1 translation
    output logic [(rv_iommu::CAUSE_LEN-1):0]    cause_code_o,

    input  logic                    en_1S_i,        // Enable signal for first-stage translation. Defined by DC/PC
    input  logic                    en_2S_i,        // Enable signal for second-stage translation. Defined by DC only
    input  logic                    is_store_i,     // Indicate whether this translation was triggered by a store or a load
    input  logic                    is_rx_i,        // Indicate whether the access is read-for-execute

    input  axi_rsp_t    mem_resp_i,
    output axi_req_t    mem_req_o,

    // to IOTLB, update logic
    output logic                    update_o,
    output logic                    up_1S_2M_o,
    output logic                    up_1S_1G_o,
    output logic                    up_2S_2M_o,
    output logic                    up_2S_1G_o,
    output logic [riscv::GPPNW-1:0] up_vpn_o,   // GPPN = 29
    output logic [19:0]             up_pscid_o,
    output logic [15:0]             up_gscid_o,
    output riscv::pte_t             up_1S_content_o,
    output riscv::pte_t             up_2S_content_o,

    // IOTLB tags
    input  logic [riscv::VLEN-1:0]  req_iova_i,
    input  logic [19:0]             pscid_i,
    input  logic [15:0]             gscid_i,

    // MSI translation
    input  logic                        msi_en_i,
    input  logic [riscv::GPPNW-1:0]     msi_addr_mask_i,
    input  logic [riscv::GPPNW-1:0]     msi_addr_pattern_i,

    // Bus to send first-stage data to MSI PTW
    output logic                        gpaddr_is_msi_o,
    output logic [riscv::GPPNW-1:0]     msi_vpn_o,
    output logic                        msi_1S_2M_o,
    output logic                        msi_1S_1G_o,
    output riscv::pte_t                 msi_gpte_o,

    // CDW implicit translations (Second-stage only)
    input  logic                        cdw_implicit_access_i,
    input  logic [(riscv::GPPNW-1):0]   pdt_gppn_i, // GPPN of the DC.fsc or PDT entry
    output logic                        cdw_done_o,
    output logic                        flush_cdw_o,

    // from DC/PC
    input  logic [riscv::PPNW-1:0]  iosatp_ppn_i,  // ppn from iosatp
    input  logic [riscv::PPNW-1:0]  iohgatp_ppn_i, // ppn from iohgatp (may be forwarded by the CDW)

    output logic [riscv::GPLEN-1:0] bad_gpaddr_o    // to return the GPA in case of second-stage error
);

    // PTW states
    enum logic[1:0] {
      IDLE,         // 00
      MEM_ACCESS,   // 01
      PROC_PTE,     // 10
      ERROR         // 11
    } state_q, state_n;

    // Page levels: 3 for Sv39x4
    enum logic [1:0] {
        LVL1, LVL2, LVL3
    } main_lvl_q, main_lvl_n, s1_lvl_n, s1_lvl_q;

    // Internal PTW stages
    enum logic [1:0] {
        STAGE_1,            // Comes from a S1_XX memory access
        STAGE_2_INTERMED,   // Comes from a S2_XX memory access different from the last one
        STAGE_2_FINAL       // Comes from the last S2_XX memory accesses
    } ptw_stage_q, ptw_stage_n;

    // To cast input memory port to normal PTE data
    riscv::pte_t pte;
    assign pte = riscv::pte_t'(mem_resp_i.r.data);

    // Register to store leaf first-stage PTE to be updated in the IOTLB
    riscv::pte_t leaf_1Spte_q, leaf_1Spte_n;

    // global bit register
    logic global_mapping_q, global_mapping_n;
    // to register PSCID to be updated
    logic [19:0]  iotlb_update_pscid_q, iotlb_update_pscid_n;
    // to register GSCID to be updated
    logic [15:0]  iotlb_update_gscid_q, iotlb_update_gscid_n;
    // to register the input GVA (VPNs). SV39x4 defines a 39 bit virtual address for first stage
    logic [riscv::VLEN-1:0] iova_q,   iova_n; // VLEN = 64
    // to register the final leaf GPA (GPPNs). SV39x4 defines a 41 bit GPA
    // for second stage
    logic [riscv::GPLEN-1:0] gpaddr_q, gpaddr_n;
    // 4 byte aligned physical pointer
    logic [riscv::PLEN-1:0] ptw_pptr_q, ptw_pptr_n;     // address used to access (read memory)
    // To save GPA_n
    logic [riscv::GPLEN-1:0] gpa_x_q, gpa_x_n;

    // GPA is the address of a virtual IF
    logic gpaddr_is_msi;

    // CDW implicit accesses
    logic cdw_implicit_access_q, cdw_implicit_access_n;

    // To save final GPA
    logic [riscv::GPLEN-1:0] final_gpa;

    // To signal page faults / guest page faults
    logic pf_excep_q, pf_excep_n;

    // To signal data corruption
    logic pt_data_corrupt_q, pt_data_corrupt_n;

    // PTW walking
    assign ptw_active_o    = (state_q != IDLE);

    // Edge-triggered init control
    logic edge_trigger_q, edge_trigger_n;

    // FIFO edge-triggered push control
    always_comb begin : ptw_init_control

        // Default
        edge_trigger_n = edge_trigger_q;

        // Edged signal
        if (!edge_trigger_q && init_ptw_i)
            edge_trigger_n = 1'b1;

        // End of edged signal
        if (edge_trigger_q && !init_ptw_i)
            edge_trigger_n = 1'b0;

    end : ptw_init_control

    // MSI translation support
    generate

        // Enabled
        if (MSITrans != rv_iommu::MSI_DISABLED) begin : gen_msi_support

            // GPA is the address of a virtual interrupt file
            // 1. Is MSI translation enabled?
            // 2. Is this a store access? 
            // 3. Does the GPA match the MSI address pattern when masked with the MSI address mask?
            assign gpaddr_is_msi    = (msi_en_i && is_store_i &&
                                        ((pte.ppn[riscv::GPPNW-1:0] & ~msi_addr_mask_i) == 
                                         (msi_addr_pattern_i & ~msi_addr_mask_i)));

            // Bus to send first-stage data to MSI PTW                            
            assign msi_vpn_o        = iova_q[riscv::SVX-1:12];
            assign msi_1S_2M_o      = (main_lvl_q == LVL2);
            assign msi_1S_1G_o      = (main_lvl_q == LVL1);
            assign msi_gpte_o       = pte;
        end : gen_msi_support

        // Disabled
        else begin : gen_msi_support_disabled

            assign gpaddr_is_msi    = 1'b0;
            assign msi_vpn_o        = '0;
            assign msi_1S_2M_o      = 1'b0;
            assign msi_1S_1G_o      = 1'b0;
            assign msi_gpte_o       = '0;
        end : gen_msi_support_disabled

    endgenerate

    //# IOTLB Update combinational logic
    always_comb begin : iotlb_update
        
        // vpn to be updated in the IOTLB
        up_vpn_o = iova_q[riscv::SVX-1:12];

        up_1S_2M_o = 1'b0;
        up_1S_1G_o = 1'b0;
        up_2S_2M_o = 1'b0;
        up_2S_1G_o = 1'b0;

        // Two-stage
        if(en_2S_i && en_1S_i) begin 

            up_2S_2M_o = (main_lvl_q == LVL2);
            up_2S_1G_o = (main_lvl_q == LVL1);
            up_1S_2M_o = (s1_lvl_q == LVL2);
            up_1S_1G_o = (s1_lvl_q == LVL1);
        end

        // stage 1 only
        else if(en_1S_i) begin

            up_1S_2M_o = (main_lvl_q == LVL2);
            up_1S_1G_o = (main_lvl_q == LVL1);
        end

        // stage 2 only
        else if (en_2S_i) begin
            
            up_2S_2M_o = (main_lvl_q == LVL2);
            up_2S_1G_o = (main_lvl_q == LVL1);
        end

        up_pscid_o = iotlb_update_pscid_q;
        up_gscid_o = iotlb_update_gscid_q;

        // set the global mapping bit
        if(en_2S_i) begin   // if stage 2 is enabled
            up_1S_content_o = leaf_1Spte_q | ({63'b0, global_mapping_q} << 5);
            up_2S_content_o = pte;
        end
        
        else begin
            up_1S_content_o = pte | ({63'b0, global_mapping_q} << 5);
            up_2S_content_o = '0;
        end
    end

    logic [(rv_iommu::CAUSE_LEN-1):0] cause_q, cause_n;

    assign bad_gpaddr_o = ptw_error_2S_o ? ((ptw_stage_q == STAGE_2_INTERMED) ? gpa_x_q[riscv::GPLEN-1:0] : gpaddr_q) : '0;

    //# Page table walker
    always_comb begin : ptw
        automatic logic [riscv::PLEN-1:0] pptr;

        // Default assignments
        // Wires
        final_gpa               = '0;
        pptr                    = '0;

        // Output signals
        // AXI parameters
        // AW
        mem_req_o.aw.id         = 4'b0010; 
        mem_req_o.aw.addr       = '0;                   // Physical address to access
        mem_req_o.aw.len        = 8'b0;                 // 1 beat per burst only
        mem_req_o.aw.size       = 3'b011;               // 64 bits (8 bytes) per beat
        mem_req_o.aw.burst      = axi_pkg::BURST_FIXED; // Fixed start address
        mem_req_o.aw.lock       = '0;
        mem_req_o.aw.cache      = '0;
        mem_req_o.aw.prot       = '0;
        mem_req_o.aw.qos        = '0;
        mem_req_o.aw.region     = '0;
        mem_req_o.aw.atop       = '0;
        mem_req_o.aw.user       = '0;

        mem_req_o.aw_valid      = 1'b0;                 // PTW will never write to memory

        // W
        mem_req_o.w.data        = '0;
        mem_req_o.w.strb        = '0;
        mem_req_o.w.last        = '0;
        mem_req_o.w.user        = '0;

        mem_req_o.w_valid       = 1'b0;                 // PTW will never write to memory

        // B
        mem_req_o.b_ready       = 1'b0;

        // AR
        mem_req_o.ar.id         = 4'b0000;          
        mem_req_o.ar.addr       = {{riscv::XLEN-riscv::PLEN{1'b0}}, ptw_pptr_q};    // Physical address to access
        mem_req_o.ar.len        = 8'b0;                                             // 1 beat per burst only
        mem_req_o.ar.size       = 3'b011;                                           // 64 bits (8 bytes) per beat
        mem_req_o.ar.burst      = axi_pkg::BURST_FIXED;                             // Fixed start address
        mem_req_o.ar.lock       = '0;
        mem_req_o.ar.cache      = '0;
        mem_req_o.ar.prot       = '0;
        mem_req_o.ar.qos        = '0;
        mem_req_o.ar.region     = '0;
        mem_req_o.ar.user       = '0;

        mem_req_o.ar_valid      = 1'b0;                 // to init a request

        // R
        mem_req_o.r_ready       = 1'b0;                 // to signal read completion
        
        ptw_error_o             = 1'b0;
        ptw_error_2S_o          = 1'b0;
        ptw_error_2S_int_o      = 1'b0;
        cause_code_o            = '0;
        update_o                = 1'b0;
        cdw_done_o              = 1'b0;
        flush_cdw_o             = 1'b0;
        gpaddr_is_msi_o         = 1'b0;
        
        // Next state values
        main_lvl_n              = main_lvl_q;
        s1_lvl_n                = s1_lvl_q;
        ptw_pptr_n              = ptw_pptr_q;
        gpa_x_n                 = gpa_x_q;
        state_n                 = state_q;
        ptw_stage_n             = ptw_stage_q;
        leaf_1Spte_n            = leaf_1Spte_q;
        global_mapping_n        = global_mapping_q;
        iotlb_update_pscid_n    = iotlb_update_pscid_q;
        iotlb_update_gscid_n    = iotlb_update_gscid_q;
        iova_n                  = iova_q;
        gpaddr_n                = gpaddr_q;
        cause_n                 = cause_q;
        cdw_implicit_access_n   = cdw_implicit_access_q;
        pf_excep_n              = pf_excep_q;
        pt_data_corrupt_n       = pt_data_corrupt_q;

        case (state_q)                 
            // Process normal PTEs
            PROC_PTE: begin
                // we wait for RVALID to start reading
                if (mem_resp_i.r_valid) begin
                    
                    // Why not leave ready out?
                    mem_req_o.r_ready   = 1'b1;
                        
                    // We need to save global configuration for non-leaf PTEs marked as global
                    if (pte.g && ptw_stage_q == STAGE_1)
                        global_mapping_n = 1'b1;

                    // Invalid PTE
                    // "If pte.v = 0, or if pte.r = 0 and pte.w = 1, stop and raise a page-fault exception corresponding to the original access type".
                    if (!pte.v || (!pte.r && pte.w)) begin
                        pf_excep_n    = 1'b1;
                        state_n         = ERROR;
                    end

                    // reserved bits in PTE must be 0
                    if ((|pte.reserved) != 1'b0) begin
                        pf_excep_n    = 1'b1;
                        state_n         = ERROR;  // GPPN bits [44:29] MUST be all zero
                        ptw_stage_n     = ptw_stage_q;
                        update_o        = 1'b0;
                        cdw_done_o      = 1'b0;
                    end

                    // Since sv39x4 regard GPPN as {VPN[2], VPN[1], VPN[0]}, GPPN[44:29] must be 0
                    if (ptw_stage_q == STAGE_1 && (|pte.ppn[riscv::PPNW-1:riscv::GPPNW]) != 1'b0) begin
                        pf_excep_n    = 1'b1;
                        state_n         = ERROR;  // GPPN bits [44:29] MUST be all zero
                        ptw_stage_n     = STAGE_2_INTERMED;    // to throw guest page fault
                        update_o        = 1'b0;
                        cdw_done_o      = 1'b0;
                    end
                    
                    /*
                        # Note about IOPMP faults for PTW accesses:
                        IOPMP access faults are reported as failing AXI transactions. If accessing (reading) a PTE
                        violates an IOPMP check, the read transaction is responded with an AXI error in the R channel.
                        Custom data can be placed in the RDATA bus to differentiate IOPMP access faults from other 
                        AXI errors.
                    */

                    // Check for AXI errors
                    if (mem_resp_i.r.resp != axi_pkg::RESP_OKAY) begin
                        cause_n = rv_iommu::PT_DATA_CORRUPTION;
                        state_n = ERROR;
                        pt_data_corrupt_n = 1'b1;
                        update_o = 1'b0;
                        cdw_done_o  = 1'b0;
                    end
                end
            end

            // Propagate error to IOMMU
            // We do need to propagate the bad GPA
            ERROR: begin
                state_n     = IDLE;
                ptw_error_o = 1'b1;

                // Set cause code and flags
                if(pt_data_corrupt_q)
                    cause_code_o = cause_q;
                else begin
                    if (pf_excep_q) begin
                        if (ptw_stage_q != STAGE_1) begin
                            ptw_error_2S_o   = 1'b1;
                            if (is_store_i) cause_code_o = rv_iommu::STORE_GUEST_PAGE_FAULT;
                            else            cause_code_o = rv_iommu::LOAD_GUEST_PAGE_FAULT;
                        end
                        else begin
                            if (is_store_i) cause_code_o = rv_iommu::STORE_PAGE_FAULT;
                            else            cause_code_o = rv_iommu::LOAD_PAGE_FAULT;
                        end
                    end
                end
                ptw_error_2S_int_o = (ptw_stage_q == STAGE_2_INTERMED) ? 1'b1 : 1'b0;
                flush_cdw_o = cdw_implicit_access_q;
            end
        endcase
    end

    // sequential process
    always_ff @(posedge clk_i or negedge rst_ni) begin
        if (~rst_ni) begin
            state_q                 <= IDLE;
            ptw_stage_q             <= STAGE_1;
            main_lvl_q              <= LVL1;
            s1_lvl_q                <= LVL1;
            iotlb_update_pscid_q    <= '0;
            iotlb_update_gscid_q    <= '0;
            iova_q                  <= '0;
            gpaddr_q                <= '0;
            ptw_pptr_q              <= '0;
            gpa_x_q                 <= '0;
            global_mapping_q        <= 1'b0;
            leaf_1Spte_q            <= '0;
            cause_q                 <= '0;
            cdw_implicit_access_q   <= 1'b0;
            pf_excep_q              <= 1'b0;
            pt_data_corrupt_q       <= 1'b0;
            edge_trigger_q          <= 1'b0;

        end else begin
            state_q                 <= state_n;
            ptw_stage_q             <= ptw_stage_n;
            ptw_pptr_q              <= ptw_pptr_n;
            gpa_x_q                 <= gpa_x_n;
            main_lvl_q              <= main_lvl_n;
            s1_lvl_q                <= s1_lvl_n;
            iotlb_update_pscid_q    <= iotlb_update_pscid_n;
            iotlb_update_gscid_q    <= iotlb_update_gscid_n;
            iova_q                  <= iova_n;
            gpaddr_q                <= gpaddr_n;
            global_mapping_q        <= global_mapping_n;
            leaf_1Spte_q            <= leaf_1Spte_n;
            cause_q                 <= cause_n;
            cdw_implicit_access_q   <= cdw_implicit_access_n;
            pf_excep_q              <= pf_excep_n;
            pt_data_corrupt_q       <= pt_data_corrupt_n;
            edge_trigger_q          <= edge_trigger_n;
        end
    end

endmodule


   //-----------------------------
    //#  IOMMU fault CAUSE encoding
    //-----------------------------

    // max 12 bits to encode CAUSE
    // cause encondings 275 to 2047 are reserved. Encodings 2048 through 4095 are for custom use.
    localparam CAUSE_LEN = 12;

    // Fault/event cases
    // localparam logic [CAUSE_LEN-1:0] INSTR_ACCESS_FAULT     = 1;  // Illegal access as governed by PMPs and PMAs
    // localparam logic [CAUSE_LEN-1:0] LD_ADDR_MISALIGNED     = 4;  // Read address misaligned
    // localparam logic [CAUSE_LEN-1:0] LD_ACCESS_FAULT        = 5;  // Illegal access as governed by PMPs and PMAs
    // localparam logic [CAUSE_LEN-1:0] ST_ADDR_MISALIGNED     = 6;  // Write/AMO address misaligned
    // localparam logic [CAUSE_LEN-1:0] ST_ACCESS_FAULT        = 7;  // Illegal write/AMO access as governed by PMPs and PMAs
    // localparam logic [CAUSE_LEN-1:0] INSTR_PAGE_FAULT       = 12; // Instruction page fault
    localparam logic [CAUSE_LEN-1:0] LOAD_PAGE_FAULT        = 13; // Load/read page fault
    localparam logic [CAUSE_LEN-1:0] STORE_PAGE_FAULT       = 15; // Store/write/AMO page fault
    // localparam logic [CAUSE_LEN-1:0] INSTR_GUEST_PAGE_FAULT = 20; // Instruction guest page fault
    localparam logic [CAUSE_LEN-1:0] LOAD_GUEST_PAGE_FAULT  = 21; // Load/read guest-page fault
    localparam logic [CAUSE_LEN-1:0] STORE_GUEST_PAGE_FAULT = 23; // Store/write/AMO guest-page fault

    // // Extended IOMMU fault cases
    // localparam logic [CAUSE_LEN-1:0] ALL_INB_TRANSACTIONS_DISALLOWED    = 256;  // IOMMU off / ATS requested and not supported
    // localparam logic [CAUSE_LEN-1:0] DDT_ENTRY_LD_ACCESS_FAULT          = 257;  // PMP/PMA fault when accessing 'ddtp' or 'DC' 
    // localparam logic [CAUSE_LEN-1:0] DDT_ENTRY_INVALID                  = 258;  // When either 'ddtp' or 'DC' are not valid
    // localparam logic [CAUSE_LEN-1:0] DDT_ENTRY_MISCONFIGURED            = 259;  // Configuration checks failed (See section 2.1.4)
    // localparam logic [CAUSE_LEN-1:0] TRANS_TYPE_DISALLOWED              = 260;
    // localparam logic [CAUSE_LEN-1:0] MSI_PTE_LD_ACCESS_FAULT            = 261;  // PMP/PMA checkn fault when accessing MSI PTE
    // localparam logic [CAUSE_LEN-1:0] MSI_PTE_INVALID                    = 262;
    // localparam logic [CAUSE_LEN-1:0] MSI_PTE_MISCONFIGURED              = 263;
    // localparam logic [CAUSE_LEN-1:0] MRIF_ACCESS_FAULT                  = 264;
    // localparam logic [CAUSE_LEN-1:0] PDT_ENTRY_LD_ACCESS_FAULT          = 265;
    // localparam logic [CAUSE_LEN-1:0] PDT_ENTRY_INVALID                  = 266;
    // localparam logic [CAUSE_LEN-1:0] PDT_ENTRY_MISCONFIGURED            = 267;
    // localparam logic [CAUSE_LEN-1:0] DDT_DATA_CORRUPTION                = 268;
    // localparam logic [CAUSE_LEN-1:0] PDT_DATA_CORRUPTION                = 269;
    // localparam logic [CAUSE_LEN-1:0] MSI_PT_DATA_CORRUPTION             = 270;
    // localparam logic [CAUSE_LEN-1:0] MSI_MRIF_DATA_CORRUPTION           = 271;
    // localparam logic [CAUSE_LEN-1:0] INTERN_DATAPATH_FAULT              = 272;
    // localparam logic [CAUSE_LEN-1:0] MSI_ST_ACCESS_FAULT                = 273;
    localparam logic [CAUSE_LEN-1:0] PT_DATA_CORRUPTION                 = 274;



state_q                 <= IDLE;
ptw_stage_q             <= STAGE_1;
main_lvl_q              <= LVL1;
s1_lvl_q                <= LVL1;
iotlb_update_pscid_q    <= '0;
iotlb_update_gscid_q    <= '0;
iova_q                  <= '0;
gpaddr_q                <= '0;
ptw_pptr_q              <= '0;
gpa_x_q                 <= '0;
global_mapping_q        <= 1'b0;
leaf_1Spte_q            <= '0;
cause_q                 <= '0;
cdw_implicit_access_q   <= 1'b0;
pf_excep_q              <= 1'b0;
pt_data_corrupt_q       <= 1'b0;
edge_trigger_q          <= 1'b0;