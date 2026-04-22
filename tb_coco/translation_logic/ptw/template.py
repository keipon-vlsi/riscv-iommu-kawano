import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly
from cocotbext.axi import AxiRam, AxiBus

class PTWTester:
    def __init__(self, dut):
        self.dut = dut
        self.log = dut._log

        # 1. クロックの生成 (100MHz = 10ns)
        cocotb.start_soon(Clock(dut.clk_i, 10, unit="ns").start())

        # 2. 初期値の設定 (MSI, CDWは無効化)
        self._init_ports()

        # 3. AXI Memory (DRAM) のインスタンス化
        # ※注意：後述の「SVラッパー」を経由する前提のプレフィックス名です
        self.ram = AxiRam(AxiBus.from_prefix(dut, "m_axi"), dut.clk_i, dut.rst_ni, size=2**32)

    def _init_ports(self):
        """Initialize all port zero"""
        # Trigger of PTW
        self.dut.init_ptw_i.setimmediatevalue(0)

        # 1st/2nd stage indicators 
        self.dut.en_1S_i.setimmediatevalue(0)
        self.dut.en_2S_i.setimmediatevalue(0)

        # Access type
        self.dut.is_store_i.setimmediatevalue(0)
        self.dut.is_rx_i.setimmediatevalue(0)

        # AXI
        # mem_resp_i

        # IOTLB tags
        self.dut.req_iova_i.setimmediatevalue(0)
        self.dut.pscid_i.setimmediatevalue(0)
        self.dut.gscid_i.setimmediatevalue(0)

        # MSI (Disabled)
        self.dut.msi_en_i.setimmediatevalue(0)
        self.dut.msi_addr_mask_i.setimmediatevalue(0)
        self.dut.msi_addr_pattern_i.setimmediatevalue(0)

        # CDW (Disabled)
        self.dut.cdw_implicit_access_i.setimmediatevalue(0)
        self.dut.pdt_gppn_i.setimmediatevalue(0)
        
        # Context (root pointer from DC/PC)
        self.dut.iosatp_ppn_i.setimmediatevalue(0)
        self.dut.iohgatp_ppn_i.setimmediatevalue(0)

    async def reset(self):
        """Asyncronics reset (Active Low)"""
        self.dut.rst_ni.value = 0
        await RisingEdge(self.dut.clk_i)
        await RisingEdge(self.dut.clk_i)
        self.dut.rst_ni.value = 1
        await RisingEdge(self.dut.clk_i)
        self.log.info("Reset complete.")

    async def trigger_walk(self, iova, iosatp_ppn):
        """PTWにアドレス変換を要求するメソッド"""
        # レジスタ（コンテキスト）の設定
        self.dut.iosatp_ppn_i.value = iosatp_ppn
        self.dut.req_iova_i.value = iova
        
        # 1クロックだけ init_ptw_i をHighにしてキックする
        await RisingEdge(self.dut.clk_i)
        self.dut.init_ptw_i.value = 1
        await RisingEdge(self.dut.clk_i)
        self.dut.init_ptw_i.value = 0

    async def wait_for_completion(self, timeout_cycles=100):
        """変換完了（update_o）またはエラー（ptw_error_o）を待つ"""
        for _ in range(timeout_cycles):
            await RisingEdge(self.dut.clk_i)
            
            # ReadOnlyフェーズで安定した信号をサンプリング
            await ReadOnly() 
            
            if self.dut.ptw_error_o.value == 1:
                cause = int(self.dut.cause_code_o.value)
                self.log.error(f"PTW Error Detected! Cause Code: {cause}")
                return {"status": "error", "cause": cause}
                
            if self.dut.update_o.value == 1:
                vpn = int(self.dut.up_vpn_o.value)
                pte_1s = int(self.dut.up_1S_content_o.value)
                self.log.info(f"PTW Success! VPN: {hex(vpn)}, PTE: {hex(pte_1s)}")
                return {"status": "success", "vpn": vpn, "pte_1S": pte_1s}
                
        self.log.error("PTW Timeout!")
        return {"status": "timeout"}

# ==========================================
# テストケース本体
# ==========================================
@cocotb.test()
async def test_ptw_happy_path(dut):
    tb = PTWTester(dut)
    await tb.reset()

    # 1. 仮想のDRAM (AxiRam) に正解のPTEを書き込んでおく
    # 例: root_ppn = 0x100 (物理アドレス 0x100000) にPTEを仕込む
    root_ppn = 0x100
    root_paddr = root_ppn << 12
    
    # 仕込むPTEデータ (V=1, R=1, W=1 ... 等)
    dummy_pte = 0x00000000_0000000F.to_bytes(8, byteorder='little')
    tb.ram.write(root_paddr, dummy_pte)

    # 2. PTWをトリガー
    test_iova = 0x0000_0000_0000 # オフセット0のアドレス
    await tb.trigger_walk(iova=test_iova, iosatp_ppn=root_ppn)

    # 3. 結果を待つ
    result = await tb.wait_for_completion()
    
    # 4. アサーション (期待値確認)
    assert result["status"] == "success", "変換に失敗しました"