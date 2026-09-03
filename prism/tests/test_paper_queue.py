# -*- coding: utf-8 -*-
"""排板队列状态机测试 — 建委托/三结局/冻结/三重幂等 (全离线, tmp 账本)。

偏离简报示例 1/test5 数字的说明(简报示例自相矛盾, 已按 Step 3 实现公式修正,
意图不变):
- 示例断言 shares==3000/frozen==30000 与其注释"30万/10元→30000股"及实现
  "amount=净值30%→int(amount//(price*100))*100" 冲突: 1M 净值×30%=30万 →
  30万/10元 = 30000 股、冻结 30 万。断言改为 30000/300000, 冻结语义
  (cash 不变、available=cash−frozen)不变。
- 示例 bid_vol=50万股×10元=500万 < 封单门槛 2000万 → 建委托必 None,
  示例却断言成功建单。故合格 tick 的 bid_vol 用 200 万股(=2000万门槛恰过)。
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
    """xtdata 原生字段名 tick 构造: 手数口径 volume/bidVol。"""
    return {"lastPrice": last_price, "lastVolume": volume,
            "bidVol": [bid_vol], "lastClose": last_close}


@pytest.fixture
def acc(tmp_path):
    a = PaperAccount(state_path=tmp_path / "paper.json")
    a.init_account(created="2026-09-01")
    return a


# 净值 100 万 × 30% = 30 万 → 30000 股 @10.0, 冻结 30 万
_BID_OK = 2_000_000          # 200万股×10元 = 2000万 → 过封单门槛


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
    """ΔV=queued+shares 且 last==price → 整单成交: holdings/扣款/流水/清 pending。"""
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
    """封单金额门槛: bidVol1×price < 2000万 → 不建委托。"""
    p = acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                               _tick(10.0, 1_000_000, bid_vol=100_000))
    assert p is None                                  # 100万×10=1000万 < 2000万
    assert acc.state["pending_buys"] == []


def test_gate_tail_no_queue(acc):
    """尾盘不排: now>=14:30 → 不建委托(其余门槛全过)。"""
    p = acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 14, 30, 5),
                               _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    assert p is None
    assert acc.state["pending_buys"] == []


def test_fill_price_no_slip(acc):
    """排队成交价 = 挂单价(无 +0.1% 上滑): holdings cost 恰为 10.0。"""
    acc.create_pending_buy("600000", 10.0, datetime(2026, 9, 2, 10, 0, 5),
                           _tick(10.0, 1_000_000, bid_vol=_BID_OK))
    acc.check_pending_buys({"600000": _tick(10.0, 3_030_000)},
                           now=datetime(2026, 9, 2, 10, 0, 35))
    h = acc.state["holdings"][0]
    assert h["cost"] == 10.0                          # 非 10.01
    assert h["buy_price"] == 10.0
    fee = 300000.0 * 0.00026
    assert acc.state["cash"] == pytest.approx(1_000_000.0 - 300000.0 - fee)