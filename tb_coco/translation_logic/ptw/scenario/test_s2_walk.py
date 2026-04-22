import cocotb
import random
from cocotb.triggers import Timer
from ptw_helpers import *

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
