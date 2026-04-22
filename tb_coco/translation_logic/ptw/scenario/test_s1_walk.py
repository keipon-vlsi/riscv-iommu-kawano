import cocotb
import random
from cocotb.triggers import Timer
from ptw_helpers import *

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