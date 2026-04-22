import cocotb
import random
from cocotb.triggers import Timer
from ptw_helpers import *

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