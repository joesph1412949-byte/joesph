# -*- coding: utf-8 -*-
# ============================================================
# [已过期 · 2026-09-13 整理时判定]
# 本脚本写于 2026-08-16，其假 provider 的数据是按当时的 default.json
# 策略设计的。09-03 因子体系改为「36 因子四层」后，假数据不再满足
# 门控 N1-N8（缺 index_kline / sh_index_kline / 板块等市场级字段），
# run_screen 的 gate_score 恒为 0 → 不再产出候选，断言必然失败。
# 若要复活：需按新因子体系重造假市场上下文。
# 保留原因：它是「如何验证 sim 信号落盘格式」的参考实现。
# tt 侧的等价验证（当前有效）见 tt/tests/test_env_sim.py。
# ============================================================
"""Task 9 Step 2 模拟盘冒烟: 假 provider 驱动 prism.trader.run_daily(env='sim')。

不连 QMT / 不连东财 / 不写 real 目录。验证全链路:
  选股结果生成 -> 信号写入 D:/QMT_SIGNALS/sim/pending/
  -> 信号 JSON 可被桥协议解析(消费字段 order_id/action/stock_code/price/volume/account_id)。
"""
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from prism import registry as reg
from prism.context import FactorContext
from prism.strategies import STRATEGIES_DIR
from prism.engine import active_strategy_id, load_strategy
from prism.trader import run_daily
from common import SIGNAL_ROOT

reg.scan_factors("prism.factors", force=True)


class FakeProvider:
    """假 provider: 固定 50 只涨停 + 全市场成交额 2.4 万亿 + 东财市场统计。

    门槛因子: N1(50>均值30) / N2(50>=50) / N5(2.4e12>=2e12) 命中 -> gate>=3。
    个股: 前 2 只 manual 因子 S1/S5/S7 全 1 -> momentum 模型分 3 >= 3 -> 候选。
    """

    def __init__(self):
        self.codes = ["600%03d.SH" % i for i in range(50)]
        self.limit_ups = [
            {"code": c, "name": "假股%s" % c, "sealed": True,
             "last": 10.0, "up_stop_price": 11.0, "float_volume": 1e8}
            for c in self.codes]
        # 50 * 4.8e10 = 2.4e12 >= 2e12 (N5)
        self.ticks = {c: {"amount": 4.8e10, "lastPrice": 10.0,
                          "lastClose": 9.0} for c in self.codes}

    def build_market_context(self):
        return FactorContext(code="__MARKET__", ticks=dict(self.ticks),
                             limit_ups=list(self.limit_ups),
                             em={"daily_counts": [40, 30, 20],
                                 "yesterday_codes": [], "max_boards": 3})

    def get_limit_ups(self):
        return list(self.limit_ups)

    def build_stock_context(self, code):
        manual = {"S1": 1, "S5": 1, "S7": 1} if code in self.codes[:2] else {}
        return FactorContext(code=code, manual=manual,
                             limit_ups=list(self.limit_ups),
                             em={"daily_counts": [40, 30, 20]},
                             last=10.0, up_price=11.0)


def main():
    print("== 模拟盘冒烟: 假 provider 驱动 run_daily(env=sim) ==")
    # 1) 策略加载（跟随 .active.json 指针；原写死 default.json，
    #    该文件已于 09-04 策略切换时删除，故改为解析当前激活策略）
    strategy = load_strategy(
        STRATEGIES_DIR / ("%s.json" % active_strategy_id()))
    print("[1] 策略加载 OK: id=%s models=%s"
          % (strategy["id"], ",".join(m["id"] for m in strategy["scoring_models"])))

    # 2) 盘后流程(sim)
    res = run_daily(strategy, FakeProvider(), env="sim", volume=100)
    print("[2] run_daily 返回: environment_ok=%s gate_score=%s candidates=%d signals_written=%d paused=%s"
          % (res.get("environment_ok"), res.get("gate_score"),
             len(res.get("candidates", [])), res.get("signals_written"),
             res.get("paused")))
    assert res["environment_ok"] is True, "模拟盘环境判定失败: %r" % res
    assert res.get("signals_written", 0) >= 1, "未生成任何信号: %r" % res
    for c in res.get("candidates", []):
        print("     候选: %s %s grade=%s composite=%s" % (
            c["code"], c["scores"]["grade"], c["scores"]["grade"],
            c["scores"]["composite"]))

    # 3) 信号落盘: 只写 sim, 不写 real
    pending_dir = SIGNAL_ROOT / "sim" / "pending"
    files = sorted(glob.glob(str(pending_dir / "BUY_*.json")))
    print("[3] 信号目录: %s 文件数=%d" % (pending_dir, len(files)))
    assert len(files) == res.get("signals_written", 0), \
        "落盘数量与返回值不一致: %d vs %d" % (len(files), res.get("signals_written"))
    real_pending = SIGNAL_ROOT / "real" / "pending"
    real_files = glob.glob(str(real_pending / "*.json"))
    assert not real_files, "冒烟不应写 real 目录: %r" % real_files

    # 4) 桥协议解析: 消费字段必须齐全(order_id/action/stock_code/price/volume/account_id)
    consumed = ("order_id", "action", "stock_code", "price", "volume", "account_id")
    for f in files:
        sig = json.loads(Path(f).read_text(encoding="utf-8"))
        for key in consumed:
            assert key in sig, "信号缺桥消费字段 %s: %s" % (key, f)
        assert sig["action"] == "BUY"
        assert sig["status"] == "pending"
        assert sig["stock_code"].endswith((".SH", ".SZ"))
        print("     信号: %s %s %s x%d @%s (桥字段齐全)"
              % (sig["order_id"], sig["action"], sig["stock_code"],
                 sig["volume"], sig["price"]))

    # 5) 清理本次冒烟产生的信号文件(保持环境干净)
    for f in files:
        Path(f).unlink()
    print("[5] 已清理 %d 个冒烟信号文件" % len(files))
    print("== 模拟盘冒烟 PASS ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
