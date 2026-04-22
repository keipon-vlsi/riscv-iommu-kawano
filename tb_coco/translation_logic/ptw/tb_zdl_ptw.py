import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly

# ==========================================
# 1. PTE生成ファクトリー (テストデータの源泉)
# ==========================================
class PteFactory:
    """RISC-V Sv39 PTE 生成ファクトリー"""

    @staticmethod
    def build_pte(v=1, r=0, w=0, x=0, u=0, g=0, a=0, d=0, rsw=0, ppn=0, reserved=0):
        """全ビットを直接指定するベース関数（内部用）"""
        pte = 0
        pte |= (v & 0x1)
        pte |= (r & 0x1) << 1
        pte |= (w & 0x1) << 2
        pte |= (x & 0x1) << 3
        pte |= (u & 0x1) << 4
        pte |= (g & 0x1) << 5
        pte |= (a & 0x1) << 6
        pte |= (d & 0x1) << 7
        pte |= (rsw & 0x3) << 8
        pte |= (ppn & 0xFFFFFFFFFFF) << 10
        pte |= (reserved & 0x3FF) << 54
        return pte

    # ==========================================
    # 1. Stage-1 Non-leaf (辞書)
    # ==========================================
    @classmethod
    def s1_non_leaf(cls, ppn, **kwargs):
        """【S1-NonLeaf】デフォルト: 正常なS1ディレクトリPTE (A,D,Uは必ず0)"""
        params = dict(v=1, r=0, w=0, x=0, u=0, a=0, d=0, ppn=ppn)
        params.update(kwargs) # 引数で指定された値を上書き（フォルト注入用）
        return cls.build_pte(**params)

    # ==========================================
    # 2. Stage-1 Leaf (最終データ)
    # ==========================================
    @classmethod
    def s1_leaf(cls, ppn, **kwargs):
        """【S1-Leaf】デフォルト: 正常なS1データPTE (R/W許可, A/Dセット済)"""
        # Uビットはデバイスの特権モード(DDTE)に依存するが、ここではデフォルト0(カーネル)とする
        params = dict(v=1, r=1, w=1, x=0, u=0, a=1, d=1, ppn=ppn)
        params.update(kwargs)
        return cls.build_pte(**params)

    # ==========================================
    # 3. Stage-2 Non-leaf (辞書)
    # ==========================================
    @classmethod
    def s2_non_leaf(cls, ppn, **kwargs):
        """【S2-NonLeaf】デフォルト: 正常なS2ディレクトリPTE (A,D,Uは必ず0)"""
        params = dict(v=1, r=0, w=0, x=0, u=0, a=0, d=0, ppn=ppn)
        params.update(kwargs)
        return cls.build_pte(**params)

    # ==========================================
    # 4. Stage-2 Leaf (最終データ)
    # ==========================================
    @classmethod
    def s2_leaf(cls, ppn, **kwargs):
        """【S2-Leaf】デフォルト: 正常なS2データPTE (R/W許可, A/Dセット済, S2必須のU=1)"""
        # Stage-2のLeafはアーキテクチャの制約上、U=1が必須
        params = dict(v=1, r=1, w=1, x=0, u=1, a=1, d=1, ppn=ppn)
        params.update(kwargs)
        return cls.build_pte(**params)

# ==========================================
# 2. PTWテスター (信号制御・共通設定)
# ==========================================
class PTWTester:
    def __init__(self, dut):
        self.dut = dut
        self.log = dut._log
        cocotb.start_soon(Clock(dut.clk_i, 10, unit="ns").start())

    def apply_global_config(self):
        """全シナリオ共通のDon't care / 固定ピン設定"""
        self.dut.rst_ni.setimmediatevalue(1)
        
        # Don't care settings (Drive 0 instead of X to avoid simulation artifacts)
        self.dut.cdw_implicit_access_i.setimmediatevalue(0)
        self.dut.msi_en_i.setimmediatevalue(0)
        self.dut.msi_addr_pattern_i.setimmediatevalue(0)
        self.dut.msi_addr_mask_i.setimmediatevalue(0)
        self.dut.pdt_gppn_i.setimmediatevalue(0)
        self.dut.pscid_i.setimmediatevalue(0)
        self.dut.gscid_i.setimmediatevalue(0)
        self.dut.is_rx_i.setimmediatevalue(0)

        # Trigger初期化
        self.dut.init_ptw_i.setimmediatevalue(0)

    async def reset(self):
        """リセットシーケンス"""
        self.dut.rst_ni.value = 0
        await RisingEdge(self.dut.clk_i)
        await RisingEdge(self.dut.clk_i)
        self.dut.rst_ni.value = 1
        await RisingEdge(self.dut.clk_i)

    async def trigger(self):
        """PTWの起動トリガー (1クロックパルス)"""
        await RisingEdge(self.dut.clk_i)
        self.dut.init_ptw_i.value = 1
        await RisingEdge(self.dut.clk_i)
        self.dut.init_ptw_i.value = 0


# ==========================================
# 3. テストシナリオ本体
# ==========================================
@cocotb.test()
async def test_ptw_scenarios(dut):
    """PTWの各種PTEパターンテスト"""
    tb = PTWTester(dut)
    tb.apply_global_config()
    await tb.reset()

    # --- テスト準備 (AxiRam などのメモリモデルがあると仮定) ---
    # ram = AxiRam(...) 
    
    scenarios = [
        {"name": "Normal Leaf",      "pte": PteFactory.normal_leaf(ppn=0x123)},
        {"name": "Invalid PTE",      "pte": PteFactory.invalid()},
        {"name": "Reserved Fault",   "pte": PteFactory.reserved_not_zero()},
        {"name": "Bad Non-Leaf",     "pte": PteFactory.non_leaf_with_flags()},
        {"name": "Bottom Non-Leaf",  "pte": PteFactory.bottom_level_non_leaf()},
        {"name": "Fault Mix",        "pte": PteFactory.fault_mix()},
    ]

    for scenario in scenarios:
        dut._log.info(f"--- 実行中シナリオ: {scenario['name']} ---")
        
        # 1. 生成したPTEをメモリ(AxiRam)の特定アドレスに書き込む
        pte_bytes = scenario["pte"].to_bytes(8, byteorder='little')
        # ram.write(ROOT_ADDR, pte_bytes)  <-- ここでメモリに仕込む
        
        # 2. PTWをキック
        await tb.trigger()
        
        # 3. 完了(update_o) または エラー(ptw_error_o) を待つ処理
        # (前回の wait_for_completion メソッド等をここに呼び出します)
        
        # 4. アサーション (シナリオごとの期待値チェック)
        # assert cause_code == expected ...
        
        await RisingEdge(dut.clk_i) # 次のシナリオとの間隔を空ける