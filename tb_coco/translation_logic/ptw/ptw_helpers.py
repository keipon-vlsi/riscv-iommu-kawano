import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, ReadOnly, Timer
import random

# ==========================================
# ★ 魔法のデバッグ用ヘルパー関数 (一番上に配置してどこからでも使えるようにする)
# ==========================================
def decode_pte(dut, pte_val, name="PTE"):
    v = pte_val & 0x1
    r = (pte_val >> 1) & 0x1
    w = (pte_val >> 2) & 0x1
    x = (pte_val >> 3) & 0x1
    u = (pte_val >> 4) & 0x1
    a = (pte_val >> 6) & 0x1
    d = (pte_val >> 7) & 0x1
    ppn = (pte_val >> 10) & 0xFFFFFFFFFFF
    
    dut._log.info(f"  └─ [{name}] Raw: {hex(pte_val)} -> PPN: {hex(ppn)} | V:{v} R:{r} W:{w} X:{x} U:{u} A:{a} D:{d}")

def format_sv39_iova(iova_39):
    """Sv39用: 38ビット目の値を見て、64ビットに符号拡張する"""
    if (iova_39 >> 38) & 1:
        # bit38が1なら、bit63〜39をすべて1で埋める
        return iova_39 | 0xFFFFFF8000000000
    else:
        # bit38が0なら、そのまま（ゼロ拡張）
        return iova_39

def format_sv39x4_gpa(gpa_41):
    """Sv39x4用: そのままゼロ拡張する"""
    return gpa_41

# ==========================================
# ★ Nested検証用 物理メモリマネージャ & S2マッピングツール
# ==========================================
class PhysicalMemoryManager:
    def __init__(self, start_ppn=0x1000):
        self.next_ppn = start_ppn

    def alloc_ppn(self):
        ppn = self.next_ppn
        self.next_ppn += 1
        return ppn

def map_s2_page(ram, pmm, s2_root_ppn, gpa, target_spa=None):
    """
    指定された GPA を SPA に変換するためのS2ページテーブル(3階層)を生成し、
    最終的にデータが格納されるべき SPA を返す魔法の関数
    """
    vpn2 = (gpa >> 30) & 0x7FF
    vpn1 = (gpa >> 21) & 0x1FF
    vpn0 = (gpa >> 12) & 0x1FF

    root_pte_addr = (s2_root_ppn << 12) + (vpn2 * 8)
    if root_pte_addr not in ram.mem:
        l1_ppn = pmm.alloc_ppn()
        ram.write(root_pte_addr, PteFactory.non_leaf(ppn=l1_ppn))
    else:
        l1_ppn = (ram.mem[root_pte_addr] >> 10) & 0xFFFFFFFFFFF

    l1_pte_addr = (l1_ppn << 12) + (vpn1 * 8)
    if l1_pte_addr not in ram.mem:
        l0_ppn = pmm.alloc_ppn()
        ram.write(l1_pte_addr, PteFactory.non_leaf(ppn=l0_ppn))
    else:
        l0_ppn = (ram.mem[l1_pte_addr] >> 10) & 0xFFFFFFFFFFF

    l0_pte_addr = (l0_ppn << 12) + (vpn0 * 8)
    
    # 最終的なSPAを決定
    if target_spa is None:
        spa = pmm.alloc_ppn() << 12
    else:
        spa = target_spa

    # S2のLeaf PTEを配置 (S2のLeafはU=1である点に注意。PteFactory.s2_leafが自動処理します)
    if l0_pte_addr not in ram.mem:
        ram.write(l0_pte_addr, PteFactory.s2_leaf(ppn=(spa >> 12)))
    
    return spa

# ==========================================
# 1. 完全版 PTE ファクトリー
# ==========================================
class PteFactory:
    @staticmethod
    def build_pte(v=1, r=0, w=0, x=0, u=0, g=0, a=0, d=0, rsw=0, ppn=0, reserved=0):
        pte = 0 | (v & 1) | ((r & 1) << 1) | ((w & 1) << 2) | ((x & 1) << 3)
        pte |= ((u & 1) << 4) | ((g & 1) << 5) | ((a & 1) << 6) | ((d & 1) << 7)
        pte |= ((rsw & 3) << 8) | ((ppn & 0xFFFFFFFFFFF) << 10) | ((reserved & 0x3FF) << 54)
        return pte

    @classmethod
    def non_leaf(cls, ppn, **kwargs):
        params = dict(v=1, r=0, w=0, x=0, u=0, a=0, d=0, ppn=ppn)
        params.update(kwargs)
        return cls.build_pte(**params)
    
    @classmethod
    def s1_non_leaf(cls, ppn, **kwargs):
        params = dict(v=1, r=0, w=0, x=0, u=0, a=0, d=0, ppn=ppn)
        params.update(kwargs)
        return cls.build_pte(**params)

    @classmethod
    def s2_non_leaf(cls, ppn, **kwargs):
        params = dict(v=1, r=0, w=0, x=0, u=0, a=0, d=0, ppn=ppn)
        params.update(kwargs)
        return cls.build_pte(**params)   


    @classmethod
    def s1_leaf(cls, ppn, **kwargs):
        params = dict(v=1, r=1, w=1, x=0, u=0, a=1, d=1, ppn=ppn)
        params.update(kwargs)
        return cls.build_pte(**params)

    @classmethod
    def s2_leaf(cls, ppn, **kwargs):
        params = dict(v=1, r=1, w=1, x=0, u=1, a=1, d=1, ppn=ppn) # For stage-2 leaf, U-bit is set
        params.update(kwargs)
        return cls.build_pte(**params)

# ==========================================
# 2. Mock Memory (完全サイクルセーフ版 + PTE自動翻訳付き)
# ==========================================
class MockMemory:
    def __init__(self, dut):
        self.dut = dut
        self.mem = {}  
        cocotb.start_soon(self.serve())

    def write(self, addr, data_int):
        self.mem[addr] = data_int
        self.dut._log.info(f"  [MockMem] アドレス {hex(addr)} に PTE {hex(data_int)} を配置")

    async def serve(self):
        self.dut.mem_rd_resp_valid_i.value = 0
        self.dut.mem_rd_resp_data_i.value = 0
        
        next_valid = 0
        next_data = 0
        
        while True:
            await RisingEdge(self.dut.clk_i)
            self.dut.mem_rd_resp_valid_i.value = next_valid
            self.dut.mem_rd_resp_data_i.value = next_data
            
            await ReadOnly()
            
            if self.dut.mem_rd_resp_valid_i.value == 1:
                if self.dut.mem_rd_resp_ready_o.value == 1:
                    next_valid = 0
            
            if self.dut.mem_rd_req_valid_o.value == 1:
                addr = int(self.dut.mem_rd_req_addr_o.value)
                data = self.mem.get(addr, 0)
                self.dut._log.info(f"  [AXI] PTWがアドレス {hex(addr)} を要求！データ {hex(data)} を返します。")
                
                # ★ ここに追加！データを返す瞬間に翻訳してログに出す
                decode_pte(self.dut, data, name=f"Fetched @ {hex(addr)}")
                
                next_valid = 1
                next_data = data

# ==========================================
# 3. テスタークラス
# ==========================================
class PTWTester:
    def __init__(self, dut):
        self.dut = dut
        self.ram = MockMemory(dut)
        cocotb.start_soon(Clock(dut.clk_i, 10, unit="ns").start())

    async def reset(self):
        await RisingEdge(self.dut.clk_i)

        self.dut.rst_ni.value = 0
        self.dut.init_ptw_i.value = 0
        self.dut.iosatp_ppn_i.value = 0
        self.dut.iohgatp_ppn_i.value = 0
        self.dut.req_iova_i.value = 0
        self.dut.en_1S_i.value = 0
        self.dut.en_2S_i.value = 0

        await Timer(20, unit="ns") 
        self.dut.rst_ni.value = 1
        await Timer(20, unit="ns") 

    async def trigger(self, iova):
        self.dut.req_iova_i.value = iova
        await RisingEdge(self.dut.clk_i)
        self.dut.init_ptw_i.value = 1
        await RisingEdge(self.dut.clk_i)
        self.dut.init_ptw_i.value = 0

    async def wait_completion(self):
        for _ in range(100): 
            await RisingEdge(self.dut.clk_i)
            await ReadOnly()
            if self.dut.ptw_error_o.value == 1:
                return "ERROR"
            if self.dut.update_o.value == 1:
                return "SUCCESS"
        return "TIMEOUT"