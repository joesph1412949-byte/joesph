# -*- coding: utf-8 -*-
"""排板队列状态机测试 — 建委托/三结局/冻结/三重幂等 (全离线, tmp 账本)。

偏离简报示例 1/test5 数字的说明(简报示例自相矛盾, 已按 Step 3 实现公式修正,
意图不变):
- 示例断言 shares==3000/frozen==30000 与其注释"30万/10元→30000股"及实现
  "amount=净值30%→int(amount//(price*100))*100" 冲突: 1M 净值×30%=30万 →
  30万/10元 = 30000 股、冻结 30 万。断言改为 30000/300000, 冻结语义
  (cash 不变、available=cash−frozen)不变。
- 示例 bid_vol=50万股×10元=500万 < 封单门槛 2000万 → 建委托必 None,
  示例却断言成功建单。故合格 tick 的 bid_vol 用 200 万手(=2亿股×10元=20亿
  元, 远超 2000万门槛; 审查 I-1 后 bidVol 原始值=手, 摄入 ×100 折股)。
  门槛拒绝用例(bid_vol=10手数级)见 test_gate_seal_amount。
- test5"两笔都30%各30万"在 100 万现金下永远可并容纳 3 笔(0.3+0.3≤1),
  第二笔不可能"现金不足"。用"现金40万+持仓60万"(净值仍100万)使每笔仍
  冻结30万, 第二笔 available=40万−30万=10万 < 30万 → None, 意图保留。
"""
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism.paper import PaperAccount


def _tick(last_price, volume, bid_vol=0, last_close=None):
    """xtdata 原生字段名 tick 构造。量纲(审查 I-1): volume(lastVolume)=**手**,
    bid_vol(bidVol[0])=**手**(手数口径; create_pending_buy 摄入 ×100 折股,
    check 时 Δ手×100 折股)——mock 数字按此口径构造。"""
    return {"lastPrice": last_price, "lastVolume": volume,
            "bidVol": [bid_vol], "lastClose": last_close}


@pytest.fixture
def acc(tmp_path):
    a = PaperAccount(state_path=tmp_path / "paper.json")
    a.init_account(created="2026-09-01")
    return a


# 净值 100 万 × 30% = 30 万 → 30000 股 @10.0, 冻结 30 万
# _BID_OK 单位=手: 200万手 = 2亿股 ×10元 = 20亿元 ≥ 2000万 → 过封单门槛
_BID_OK = 2_000_000


def test_create_freezes_cash(acc):
    """建委托: 冻结不改 cash, available=cash−frozen; ΔV=0 → 保持排队。"""
    p = acc.create_pending_buy(
        "600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
        _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    assert p is not None and p["shares"] == 30000    # 30万/10元 → 30000股
    assert p["frozen"] == pytest.approx(300000.0, rel=1e-3)
    assert len(acc.state["pending_buys"]) == 1
    assert acc.state["cash"] == 1_000_000.0          # 冻结不改 cash
    assert acc.available_cash() == pytest.approx(1_000_000.0 - 300000.0)
    out = acc.check_pending_buys({"600000": _tick(10.0, 1_000_000)},
                                 now=datetime(2026, 9, 2, 10, 0, 35))
    assert out == {"filled": [], "canceled": []}     # ΔV=0 不足
    assert len(acc.state["pending_buys"]) == 1       # 保持排队


def test_fill_when_queue_crossed(acc):
    """成交穿越: Δ手×100(股) ≥ queued_shares(股)+shares(股) 且 last==price →
    整单成交: holdings/扣款/流水/清 pending。volume=303万手 → 手差=203万手
    (=2.03亿股 ≥ 2.0003亿股门槛)。"""
    acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                           _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    out = acc.check_pending_buys({"600000": _tick(10.0, 3_030_000)},
                                 now=datetime(2026, 9, 2, 10, 0, 35))
    assert out == {"filled": ["600000"], "canceled": []}
    st = acc.state
    assert len(st["holdings"]) == 1
    h = st["holdings"][0]
    assert h["code"] == "600000" and h["shares"] == 30000 and h["cost"] == 10.0
    fee = 300000.0 * 0.00026                          # 佣金万2.5+过户万0.1
    assert st["cash"] == pytest.approx(1_000_000.0 - 300000.0 - fee)
    assert st["trades"][-1]["reason"] == "queue_fill"
    assert st["pending_buys"] == []                   # 成交移除


def test_cancel_on_break(acc):
    """开板(last<price−0.001) → 撤单: 解冻(cash复原)/未成交流水/撤单幂等键。"""
    acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                           _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    out = acc.check_pending_buys({"600000": _tick(9.99, 1_000_000)},
                                 now=datetime(2026, 9, 2, 10, 0, 35))
    assert out == {"filled": [], "canceled": ["600000"]}
    st = acc.state
    assert st["pending_buys"] == []                   # 解冻(移除)
    assert st["cash"] == 1_000_000.0                  # 复原
    assert acc.available_cash() == 1_000_000.0
    assert st["trades"][-1]["reason"] == "queue_cancel_break"
    assert "600000" in st["canceled_pending_codes"]   # 当日禁排键


def test_expire_at_settle(acc):
    """收盘失效(queue_expire): 清 pending/解冻/expire 流水; 不记撤单禁排键。"""
    acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                           _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    acc._dispose_pending("600000", "queue_expire",
                         datetime(2026, 9, 2, 15, 0, 5))
    st = acc.state
    assert st["pending_buys"] == []
    assert st["cash"] == 1_000_000.0
    assert st["trades"][-1]["reason"] == "queue_expire"
    assert "600000" not in st["canceled_pending_codes"]  # 仅 break 记键
    acc._dispose_pending("600000", "queue_expire",
                         datetime(2026, 9, 2, 15, 0, 10))  # 幂等: 无 pending → no-op
    assert len(st["trades"]) == 1


def test_freeze_blocks_second(acc):
    """两笔各净值30%(=30万): 第一笔冻结后第二笔 available 不足 → None。"""
    acc.state["cash"] = 400000.0
    acc.state["holdings"].append({"code": "600001", "shares": 100000,
                                  "cost": 6.0, "buy_date": "2026-08-28",
                                  "buy_price": 6.0, "entry_nav": 1_000_000.0})
    # 净值 = 40万现金 + 60万持仓 = 100万 → 每笔 30% = 30万冻结
    p1 = acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                                _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    assert p1 is not None and p1["frozen"] == pytest.approx(300000.0)
    assert acc.available_cash() == pytest.approx(100000.0)
    p2 = acc.create_pending_buy("600002", 10.0, datetime(2026, 9, 2, 10, 0, 10),
                                _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    assert p2 is None                                 # available=10万 < 30万
    assert len(acc.state["pending_buys"]) == 1


def test_dup_pending_blocked(acc):
    """同 code 已 pending → _buyable "已在排队中" → 不重复建委托。"""
    acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                           _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    assert acc._buyable("600000", 1_000_000,
                        datetime(2026, 9, 2, 10, 0, 10)) == "已在排队中"
    p2 = acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 10),
                                _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    assert p2 is None
    assert len(acc.state["pending_buys"]) == 1


def test_canceled_code_blocks_retry(acc):
    """开板撤单后当日禁排: canceled_pending_codes 含 code → "今日已撤单"。"""
    acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                           _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    acc.check_pending_buys({"600000": _tick(9.99, 1_000_000)},
                           now=datetime(2026, 9, 2, 10, 0, 35))
    assert "600000" in acc.state["canceled_pending_codes"]
    assert acc._buyable("600000", 1_000_000,
                        datetime(2026, 9, 2, 10, 0, 40)) == "今日已撤单"
    assert acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 40),
                                  _tick(10.0, 1_000_000, bid_vol=_BID_OK)) is None
    assert len(acc.state["pending_buys"]) == 0


def test_gate_seal_amount(acc):
    """封单金额门槛: bidVol0(手)×100×price < 2000万 → 不建委托。
    bid_vol=1万手=100万股 → 封单金额=100万股×10元=1000万 < 2000万。
    (审查 I-1 改口径后重定标: 原 10 万"股"数字在 ×100 门槛下 1 亿已过门,
    按"封单 1000 万元"原意图取 1 万手。)"""
    p = acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                               _tick(10.0, 1_000_000, bid_vol=10_000))
    assert p is None                                  # 100万股×10元=1000万 < 2000万
    assert acc.state["pending_buys"] == []


def test_create_rejects_zero_volume_tick(acc):
    """C 守卫: 半残 tick(行情未就绪, lastVolume 缺失/0) → 不建委托。
    (2026-09-04 实测: 守护启动秒建单 base_volume=0 破坏 ΔV 口径。)"""
    p1 = acc.create_pending_buy(
        "600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
        _tick(10.0, 0, bid_vol=_BID_OK))
    p2 = acc.create_pending_buy(
        "600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
        {"lastPrice": 10.0, "bidVol": [_BID_OK]})          # lastVolume 键缺失
    assert p1 is None and p2 is None
    assert acc.state["pending_buys"] == []


def test_fill_on_break_when_queue_eaten(acc):
    """A 路径②(真实打板主成交通道): 炸板(last<price)但初始队列已被吃穿
    (Δ手×100 ≥ queued+shares) → 按涨停价成交, 不撤单。"""
    acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                           _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    # last=9.97(开板), Δ手=2_030_000手=2.03亿股 ≥ 2.0003亿股(队列+本单) → 成交
    out = acc.check_pending_buys({"600000": _tick(9.97, 3_030_000)},
                                 now=datetime(2026, 9, 2, 10, 0, 35))
    assert out == {"filled": ["600000"], "canceled": []}
    st = acc.state
    assert len(st["holdings"]) == 1
    assert st["holdings"][0]["cost"] == 10.0               # 涨停价成交
    assert st["holdings"][0]["buy_price"] == 10.0
    assert st["pending_buys"] == []


def test_cancel_on_break_records_dvol(acc):
    """A: 开板但队列未吃穿(ΔV 不足) → 照常撤单, 流水新增 dvol_shares
    (事后可查"当时排到没有/差多少")。"""
    acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                           _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    out = acc.check_pending_buys({"600000": _tick(9.98, 1_500_000)},
                                 now=datetime(2026, 9, 2, 10, 0, 35))
    assert out == {"filled": [], "canceled": ["600000"]}
    t = acc.state["trades"][-1]
    assert t["reason"] == "queue_cancel_break"
    assert t["dvol_shares"] == (1_500_000 - 1_000_000) * 100   # 5000万股


def test_gate_tail_no_queue(acc):
    """尾盘不排: now>=14:30 → 不建委托(其余门槛全过)。"""
    p = acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 14, 30, 5),
                               _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    assert p is None
    assert acc.state["pending_buys"] == []


def test_fill_price_no_slip(acc):
    """排队成交价 = 挂单价(无 +0.1% 上滑): holdings cost 恰为 10.0。
    (同 test_fill_when_queue_crossed 的 Δ手 穿越构造。)"""
    acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                           _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    acc.check_pending_buys({"600000": _tick(10.0, 3_030_000)},
                           now=datetime(2026, 9, 2, 10, 0, 35))
    h = acc.state["holdings"][0]
    assert h["cost"] == 10.0                          # 非 10.01
    assert h["buy_price"] == 10.0
    fee = 300000.0 * 0.00026
    assert acc.state["cash"] == pytest.approx(1_000_000.0 - 300000.0 - fee)


def test_buyable_rejection_reasons(acc):
    """_buyable 拒绝链(生产入口 create_pending_buy 走同一判定, 且不改账本):
    已持仓 / 仓位已满 / 现金不足 / 今日已交易。"""
    now = datetime(2026, 9, 2, 10, 0, 5)
    tick = _tick(10.0, 1_000_000, bid_vol=_BID_OK)

    def hold(code):
        return {"code": code, "shares": 100, "cost": 9.0, "buy_date": "2026-09-01",
                "buy_price": 9.0, "entry_nav": 1e6}

    acc.state["holdings"].append(hold("600000"))
    assert acc._buyable("600000", 1e6, now) == "已持仓"
    assert acc.create_pending_buy("600000", 10.0, now, tick) is None
    acc.state["holdings"] = [hold("60000%d" % i) for i in range(5)]
    assert acc._buyable("000001", 1e6, now) == "仓位已满"          # max_positions=5
    acc.state["holdings"] = []
    acc.state["cash"] = 8000.0
    assert acc._buyable("000001", 50000.0, now) == "现金不足"      # 8000 < 5万×30%
    acc.state["cash"] = 1_000_000.0
    acc.state["trades"].append({"side": "buy", "code": "000001",
                                "date": "2026-09-02", "reason": "queue_fill"})
    assert acc._buyable("000001", 1e6, now) == "今日已交易"
    assert acc.state["pending_buys"] == []                        # 判定不建委托