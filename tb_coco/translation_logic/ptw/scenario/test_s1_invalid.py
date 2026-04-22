import cocotb
import random
from cocotb.triggers import Timer
from ptw_helpers import *

@cocotb.test()
async def test_s1_page_fault_v_0(dut):
    dut._log.info("--- 実行: Stage-1 V=0 (無効ページ) エラー注入テスト ---")
    tb = PTWTester(dut)
    
    # 意図的にエラーを発生させる階層のリスト (2=Root, 1=Lvl1, 0=Lvl0)
    fault_levels = [2, 1, 0]

    for fault_level in fault_levels:
        dut._log.info(f"==================================================")
        dut._log.info(f"  [エラー注入] Level {fault_level} のPTEを V=0 に改ざんします")
        dut._log.info(f"==================================================")
        
        await tb.reset()
        tb.ram.mem.clear()

        # アドレス生成
        iova_39 = random.getrandbits(39)
        iova_64 = format_sv39_iova(iova_39)
        vpn2 = (iova_39 >> 30) & 0x1FF
        vpn1 = (iova_39 >> 21) & 0x1FF
        vpn0 = (iova_39 >> 12) & 0x1FF

        ppns = random.sample(range(1, 1 << 44), 4)
        root_ppn, lvl1_ppn, lvl0_ppn, target_ppn = ppns

        tb.dut.iosatp_ppn_i.value = root_ppn
        tb.dut.iohgatp_ppn_i.value = 0 
        tb.dut.en_1S_i.value = 1
        tb.dut.en_2S_i.value = 0
        
        root_pte_addr = (root_ppn << 12) + (vpn2 * 8)
        lvl1_pte_addr = (lvl1_ppn << 12) + (vpn1 * 8)
        lvl0_pte_addr = (lvl0_ppn << 12) + (vpn0 * 8)
        
        # ----------------------------------------
        # ★ エラー注入ロジック (三項演算子で対象の階層だけ V=0 にする)
        # ----------------------------------------
        pte2_v = 0 if fault_level == 2 else 1
        pte1_v = 0 if fault_level == 1 else 1
        pte0_v = 0 if fault_level == 0 else 1

        tb.ram.write(root_pte_addr, PteFactory.s1_non_leaf(ppn=lvl1_ppn, v=pte2_v))
        tb.ram.write(lvl1_pte_addr, PteFactory.s1_non_leaf(ppn=lvl0_ppn, v=pte1_v))
        tb.ram.write(lvl0_pte_addr, PteFactory.s1_leaf(ppn=target_ppn, v=pte0_v))

        # ----------------------------------------
        # 結果確認 (SUCCESSではなくERRORが返ることを期待する)
        # ----------------------------------------
        await tb.trigger(iova=iova_64)
        result = await tb.wait_completion()
        
        # エラーが起きることを期待しているので、SUCCESSやTIMEOUTならアサートで落とす
        assert result == "ERROR", f"Level {fault_level} のエラー(V=0)を検出できませんでした！ (Result: {result})"
        
        dut._log.info(f"  ★ 期待通り、Level {fault_level} で正しくエラー(ptw_error_o)を検出しました！\n")

    await Timer(50, unit="ns")