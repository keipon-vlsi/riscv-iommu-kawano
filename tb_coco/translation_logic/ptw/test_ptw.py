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

# # ==========================================
# # 4. テストケース: Stage-1 Only (任意のIOVAとRoot PPN)
# # ==========================================
# @cocotb.test()
# async def test_s1_3level_walk_arbitrary(dut):
#     dut._log.info("--- 実行: Stage-1 Only 任意アドレス変換 ---")
#     tb = PTWTester(dut)
#     await tb.reset()

#     # 1. 任意のIOVAを設定 (例として 0x2A_B123_4000 を使用)
#     iova = 0x1_d6ab_c019 
    
#     # 2. IOVAから、各階層の VPN (Virtual Page Number) を抽出する (各9ビット)
#     vpn2 = (iova >> 30) & 0x1FF  # Root用インデックス
#     vpn1 = (iova >> 21) & 0x1FF  # Lvl1用インデックス
#     vpn0 = (iova >> 12) & 0x1FF  # Lvl0用インデックス
    
#     dut._log.info(f"設定IOVA: {hex(iova)} -> VPN2:{hex(vpn2)}(VPN2*8:{hex(vpn2<<3)}), VPN1:{hex(vpn1)}(VPN2*8:{hex(vpn1<<3)}), VPN0:{hex(vpn0)}(VPN2*8:{hex(vpn0<<3)})")

#     # 3. テスト用のPPNを定義 (ご要望の通り root_ppn=0x1140)
#     root_ppn = 0x1140    
#     lvl1_ppn = 0x11    
#     lvl0_ppn = 0x12    
#     target_ppn = 0x123 
    
#     tb.dut.iosatp_ppn_i.value = root_ppn
#     tb.dut.iohgatp_ppn_i.value = 0 
#     tb.dut.en_1S_i.value = 1
#     tb.dut.en_2S_i.value = 0
    
#     # 4. VPNに基づいて、PTEを配置する「正確な物理アドレス」を計算
#     # アドレス = (PPN << 12) + (VPN * 8)
#     root_pte_addr = (root_ppn << 12) + (vpn2 * 8)
#     lvl1_pte_addr = (lvl1_ppn << 12) + (vpn1 * 8)
#     lvl0_pte_addr = (lvl0_ppn << 12) + (vpn0 * 8)
    
#     # 5. 計算したアドレスにPTEを配置
#     tb.ram.write(root_pte_addr, PteFactory.s1_non_leaf(ppn=lvl1_ppn))
#     tb.ram.write(lvl1_pte_addr, PteFactory.s1_non_leaf(ppn=lvl0_ppn))
#     tb.ram.write(lvl0_pte_addr, PteFactory.s1_leaf(ppn=target_ppn))

#     # 6. PTWをキック (IOVAを渡す)
#     await tb.trigger(iova=iova)
#     result = await tb.wait_completion()
    
#     assert result == "SUCCESS", "3階層変換の途中でエラー(ptw_error_o)が発生しました！"
    
#     # --- 最終結果のチェック ---
#     final_content = int(tb.dut.up_1S_content_o.value) 
#     dut._log.info("--- 最終出力の確認 ---")
#     decode_pte(dut, final_content, "Final_Output_PTE")
    
#     actual_ppn = (final_content >> 10) & 0xFFFFFFFFFFF
#     assert actual_ppn == target_ppn, f"変換結果が間違っています。期待値:{hex(target_ppn)}, 実際:{hex(actual_ppn)}"
    
#     dut._log.info("大成功！任意のIOVAに対しても、正しいオフセット計算を行いPTEを拾ってきました！")

#     await Timer(50, unit="ns")


# ... (中略: decode_pte, PteFactory, MockMemory, PTWTester はそのまま) ...

# ==========================================
# 4. テストケース: Stage-1 Only ランダム連続テスト
# ==========================================
@cocotb.test()
async def test_s1_3level_walk_random(dut):
    dut._log.info("--- 実行: Stage-1 Only ランダム連続テスト ---")
    tb = PTWTester(dut)
    
    # ★ 連続で実行する回数を指定（まずは10回で試してみてください）
    NUM_TESTS = 10

    for i in range(NUM_TESTS):
        dut._log.info(f"==================================================")
        dut._log.info(f"  テスト実行 {i + 1} / {NUM_TESTS} 回目")
        dut._log.info(f"==================================================")
        
        await tb.reset()
        tb.ram.mem.clear() # ★ 前のテストのメモリデータを綺麗に消去する

        # ----------------------------------------
        # 1. ランダム値の生成
        # ----------------------------------------
        # IOVA (39ビット: 0x0 〜 0x7FFFFFFFFF の間でランダム)
        iova_39 = random.getrandbits(39)
        iova_64 = format_sv39_iova(iova_39)
        
        # PPN (44ビット: 0x1 〜 0xFFFFFFFFFFF の間から「重複しない4つの数」を選ぶ)
        # ※ 重複を許すと、偶然同じページに辞書が置かれてバグるのを防ぐため
        ppns = random.sample(range(1, 1 << 44), 4)
        root_ppn   = ppns[0]
        lvl1_ppn   = ppns[1]
        lvl0_ppn   = ppns[2]
        target_ppn = ppns[3]

        # ----------------------------------------
        # 2. アドレスとVPNの計算
        # ----------------------------------------
        vpn2 = (iova_39 >> 30) & 0x1FF
        vpn1 = (iova_39 >> 21) & 0x1FF
        vpn0 = (iova_39 >> 12) & 0x1FF
        
        dut._log.info(f"設定IOVA: {hex(iova_64)} -> VPN2:{hex(vpn2)}(VPN2*8:{hex(vpn2<<3)}), VPN1:{hex(vpn1)}(VPN2*8:{hex(vpn1<<3)}), VPN0:{hex(vpn0)}(VPN2*8:{hex(vpn0<<3)})")
        dut._log.info(f"  [設定] Target PPN: {hex(target_ppn)}")

        # ----------------------------------------
        # 3. PTWの設定とPTEの配置
        # ----------------------------------------
        tb.dut.iosatp_ppn_i.value = root_ppn
        tb.dut.iohgatp_ppn_i.value = 0 
        tb.dut.en_1S_i.value = 1
        tb.dut.en_2S_i.value = 0
        
        root_pte_addr = (root_ppn << 12) + (vpn2 * 8)
        lvl1_pte_addr = (lvl1_ppn << 12) + (vpn1 * 8)
        lvl0_pte_addr = (lvl0_ppn << 12) + (vpn0 * 8)
        
        tb.ram.write(root_pte_addr, PteFactory.non_leaf(ppn=lvl1_ppn))
        tb.ram.write(lvl1_pte_addr, PteFactory.non_leaf(ppn=lvl0_ppn))
        tb.ram.write(lvl0_pte_addr, PteFactory.s1_leaf(ppn=target_ppn))

        # ----------------------------------------
        # 4. キックと結果確認
        # ----------------------------------------
        await tb.trigger(iova=iova_64)
        result = await tb.wait_completion()
        
        assert result == "SUCCESS", f"テスト {i+1}回目: 途中でエラー(ptw_error_o)が発生しました！"
        
        final_content = int(tb.dut.up_1S_content_o.value) 
        actual_ppn = (final_content >> 10) & 0xFFFFFFFFFFF
        
        # 期待値と一致しているかチェック
        assert actual_ppn == target_ppn, f"テスト {i+1}回目: 期待値 {hex(target_ppn)} != 実際 {hex(actual_ppn)}"
        
        dut._log.info(f"  ★ テスト {i + 1} 回目 クリア！\n")

    # 全ループ終了後の処理
    dut._log.info(f"🎉 全 {NUM_TESTS} 回のランダムテストをノーミスで完走しました！完璧です！")
    await Timer(50, unit="ns")

# ==========================================
# 5. テストケース: Stage-2 Only ランダム連続テスト
# ==========================================
@cocotb.test()
async def test_s2_only_3level_walk_random(dut):
    dut._log.info("--- 実行: Stage-2 Only ランダム連続テスト ---")
    tb = PTWTester(dut)
    
    NUM_TESTS = 10 

    for i in range(NUM_TESTS):
        dut._log.info(f"==================================================")
        dut._log.info(f"  [Stage-2] テスト実行 {i + 1} / {NUM_TESTS} 回目")
        dut._log.info(f"==================================================")
        
        await tb.reset()
        tb.ram.mem.clear() 

        # ----------------------------------------
        # 1. ランダム値の生成 (Stage-2 仕様)
        # ----------------------------------------
        # IOVA (Stage-2ではGPAとして扱われるため、41ビット幅)
        gpa_41 = random.getrandbits(41)
        iova_64 = format_sv39x4_gpa(gpa_41)
        
        # PPN (重複しない4つの物理ページ番号)
        ppns = random.sample(range(1, 1 << 44), 4)
        root_ppn   = ppns[0] & ~0x3 # For Stage-2, root_ppn must be aligned to 4 pages (i.e., lower 2 bits must be 0)
        lvl1_ppn   = ppns[1]
        lvl0_ppn   = ppns[2]
        target_ppn = ppns[3]

        # ----------------------------------------
        # 2. アドレスとVPNの計算 (Sv39x4 特有の抽出)
        # ----------------------------------------
        # ★ 見落としポイント！VPN2は 11ビット(0x7FF) でマスクする
        vpn2 = (gpa_41 >> 30) & 0x7FF 
        vpn1 = (gpa_41 >> 21) & 0x1FF
        vpn0 = (gpa_41 >> 12) & 0x1FF
        
        dut._log.info(f"設定IOVA: {hex(iova_64)} -> VPN2:{hex(vpn2)}(VPN2*8:{hex(vpn2<<3)}), VPN1:{hex(vpn1)}(VPN2*8:{hex(vpn1<<3)}), VPN0:{hex(vpn0)}(VPN2*8:{hex(vpn0<<3)})")

        # ----------------------------------------
        # 3. PTWの設定とPTEの配置
        # ----------------------------------------
        # ★ 見落としポイント！Stage-2は iohgatp を使う
        tb.dut.iosatp_ppn_i.value = 0
        tb.dut.iohgatp_ppn_i.value = root_ppn 
        
        # Stage-1をOFF、Stage-2をON
        tb.dut.en_1S_i.value = 0
        tb.dut.en_2S_i.value = 1
        
        # オフセットを計算
        root_pte_addr = (root_ppn << 12) + (vpn2 * 8)
        lvl1_pte_addr = (lvl1_ppn << 12) + (vpn1 * 8)
        lvl0_pte_addr = (lvl0_ppn << 12) + (vpn0 * 8)
        
        # ★ s2用のPTE生成メソッドを使う (s2_leaf は内部で U=1 に設定されています)
        tb.ram.write(root_pte_addr, PteFactory.non_leaf(ppn=lvl1_ppn))
        tb.ram.write(lvl1_pte_addr, PteFactory.non_leaf(ppn=lvl0_ppn))
        tb.ram.write(lvl0_pte_addr, PteFactory.s2_leaf(ppn=target_ppn))

        # ----------------------------------------
        # 4. キックと結果確認
        # ----------------------------------------
        await tb.trigger(iova=iova_64)
        result = await tb.wait_completion()
        
        assert result == "SUCCESS", f"Stage-2 テスト {i+1}回目: 途中でエラー発生"
        
        final_content = int(tb.dut.up_2S_content_o.value) 
        actual_ppn = (final_content >> 10) & 0xFFFFFFFFFFF
        
        assert actual_ppn == target_ppn, f"Stage-2 テスト {i+1}回目: 期待値 {hex(target_ppn)} != 実際 {hex(actual_ppn)}"
        
        dut._log.info(f"  ★ Stage-2 テスト {i + 1} 回目 クリア！\n")

    dut._log.info(f"🎉 Stage-2 も全 {NUM_TESTS} 回ノーミスで完走！完璧です！")
    await Timer(50, unit="ns")

# ==========================================
# 6. テストケース: Nested (Stage-1 + Stage-2) ランダム連続テスト
# ==========================================
@cocotb.test()
async def test_nested_3level_walk_random(dut):
    dut._log.info("--- 実行: Nested (S1+S2) ランダム連続テスト ---")
    tb = PTWTester(dut)
    
    NUM_TESTS = 10 

    for i in range(NUM_TESTS):
        dut._log.info(f"==================================================")
        dut._log.info(f"  [Nested] テスト実行 {i + 1} / {NUM_TESTS} 回目")
        dut._log.info(f"==================================================")
        
        await tb.reset()
        tb.ram.mem.clear() 
        pmm = PhysicalMemoryManager(start_ppn=0x1000) # メモリ割当ツールを初期化

        # ----------------------------------------
        # 1. 各種アドレスとポインタの生成
        # ----------------------------------------
        iova_39 = random.getrandbits(39)
        iova_64 = format_sv39_iova(iova_39)
        
        vpn2_s1 = (iova_39 >> 30) & 0x1FF
        vpn1_s1 = (iova_39 >> 21) & 0x1FF
        vpn0_s1 = (iova_39 >> 12) & 0x1FF

        # 各種ルートポインタと、S1各階層の「ゲスト物理ページ(GPPN)」を割り当て
        s2_root_ppn  = pmm.alloc_ppn() & ~0x3 # Sv39x4 アライメント
        s1_root_gppn = pmm.alloc_ppn()
        s1_l1_gppn   = pmm.alloc_ppn()
        s1_l0_gppn   = pmm.alloc_ppn()
        
        # 最終的に欲しい結果
        final_gppn = pmm.alloc_ppn()  # S1の変換結果 (ゲスト物理ページ)
        final_sppn = pmm.alloc_ppn()  # S2の変換結果 (システム物理ページ)

        tb.dut.iosatp_ppn_i.value = s1_root_gppn
        tb.dut.iohgatp_ppn_i.value = s2_root_ppn 
        tb.dut.en_1S_i.value = 1
        tb.dut.en_2S_i.value = 1

        dut._log.info(f"  [設定] IOVA: {hex(iova_64)}")
        dut._log.info(f"  [設定] 期待される最終 GPPN: {hex(final_gppn)}")
        dut._log.info(f"  [設定] 期待される最終 SPPN: {hex(final_sppn)}")

        # ----------------------------------------
        # 2. メモリ空間の構築 (2Dウォークの罠を回避)
        # ----------------------------------------
        # ① S1 Root PTE を置くためのS2マッピング
        # まず「S1の辞書があるページ全体(GPA)」をS2でマッピングする
        s1_root_spa_base = map_s2_page(tb.ram, pmm, s2_root_ppn, s1_root_gppn << 12)
        # 変換されたSPAページベースに「オフセット」を足した位置にPTEを配置！
        tb.ram.write(s1_root_spa_base + (vpn2_s1 * 8), PteFactory.non_leaf(ppn=s1_l1_gppn))

        # ② S1 L1 PTE を置くためのS2マッピング
        s1_l1_spa_base = map_s2_page(tb.ram, pmm, s2_root_ppn, s1_l1_gppn << 12)
        tb.ram.write(s1_l1_spa_base + (vpn1_s1 * 8), PteFactory.non_leaf(ppn=s1_l0_gppn))

        # ③ S1 L0 PTE を置くためのS2マッピング
        s1_l0_spa_base = map_s2_page(tb.ram, pmm, s2_root_ppn, s1_l0_gppn << 12)
        tb.ram.write(s1_l0_spa_base + (vpn0_s1 * 8), PteFactory.s1_leaf(ppn=final_gppn))

        # ④ 最終データアクセス(final_gppn)に対するS2マッピング
        # PTWはS1の最終出力(GPPN)を、最後にもう一度S2に通して最終SPAを求める
        target_gpa = final_gppn << 12
        map_s2_page(tb.ram, pmm, s2_root_ppn, target_gpa, target_spa=(final_sppn << 12))

        # ----------------------------------------
        # 3. キックと結果確認
        # ----------------------------------------
        await tb.trigger(iova=iova_64)
        result = await tb.wait_completion()
        
        assert result == "SUCCESS", f"Nested テスト {i+1}回目: 途中でエラー発生"
        
        # 結果のチェック (S1とS2の両方のPTEが正しく出力されているか)
        final_s1_content = int(tb.dut.up_1S_content_o.value) 
        final_s2_content = int(tb.dut.up_2S_content_o.value) 
        
        dut._log.info("--- 最終出力 PTE の確認 ---")
        decode_pte(dut, final_s1_content, "Output_S1_PTE")
        decode_pte(dut, final_s2_content, "Output_S2_PTE")
        
        actual_gppn = (final_s1_content >> 10) & 0xFFFFFFFFFFF
        actual_sppn = (final_s2_content >> 10) & 0xFFFFFFFFFFF
        
        assert actual_gppn == final_gppn, f"S1変換ミス: 期待 GPPN={hex(final_gppn)}, 実際={hex(actual_gppn)}"
        assert actual_sppn == final_sppn, f"S2変換ミス: 期待 SPPN={hex(final_sppn)}, 実際={hex(actual_sppn)}"
        
        dut._log.info(f"  ★ Nested テスト {i + 1} 回目 完璧にクリア！\n")

    dut._log.info(f"🎉 Nested (S1+S2) も全 {NUM_TESTS} 回ノーミスで完走！あなたとPTW、最高のタッグです！")
    await Timer(100, unit="ns")
    