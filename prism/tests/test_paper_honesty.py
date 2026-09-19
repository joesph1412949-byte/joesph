# -*- coding: utf-8 -*-
"""模拟盘账目诚实性修复(F1~F4) — 全离线, 账本一律注入 tmp_path。

- F1 收盘选股失败不占当日幂等键 → 同 slot 真的重试(不再"失败即整日零建仓")
- F2 双腿佣金下限: 佣金 max(成交额×佣金率, ¥5) + 过户费(+卖出印花税) ——
  与 prism/backtest.py 同一笔单腿费用逐分相等(见 test_fee_matches_backtest_*)
- F3 撤单/失效流水在数据上不再冒充 "buy"(side=cancel/expire), _buyable 语义不变
- F4 北交所 92/8/4 开头的 30% 涨跌停档补齐(跌停顺延判别性对照)
"""
import inspect
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism import backtest
from prism.paper import PaperAccount, _limit_ratio


def _acc(tmp_path, **kw):
    a = PaperAccount(state_path=tmp_path / "paper.json", **kw)
    a.init_account(created="2026-09-01")
    return a


def _tick(last_price, volume=1_000_000, bid_vol=2_000_000, last_close=None):
    return {"lastPrice": last_price, "lastVolume": volume,
            "bidVol": [bid_vol], "lastClose": last_close}


# --------------------------------------------------------------- F1
def test_pick_screen_failure_does_not_consume_slot(tmp_path, monkeypatch):
    """F1: 第一次选股抛异常 → 不登记幂等键、返回含 error;
    第二次同 slot **真的重试**(底层调用 2 次)并成功登记。
    修复前: 异常路径先 append ts_key → 当日再调直接 already_done 短路,
    QMT 抖一下 = 当天模拟盘零建仓。"""
    acc = _acc(tmp_path)
    monkeypatch.setattr(PaperAccount, "strategy",
                        property(lambda self: {"execution": {"top_n": 2}}))
    calls = []

    def boom_then_ok(provider):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("QMT 抖了一下")
        return {"environment_ok": True,
                "candidates": [{"code": "600000",
                                "scores": {"composite": 5.0}}]}

    monkeypatch.setattr(acc, "_screen_candidates", boom_then_ok)
    now = datetime(2026, 9, 4, 15, 5)
    out1 = acc.pick_top5_at_close(provider=object(), now=now,
                                  slot="2026-09-04T15:05")
    assert "error" in out1 and "QMT 抖了一下" in out1["error"]
    assert calls == [1]
    assert acc.state["screens_done"] == []          # 失败不占键
    out2 = acc.pick_top5_at_close(provider=object(), now=now,
                                  slot="2026-09-04T15:05")
    assert calls == [1, 1]                          # 第二次真的重试
    assert out2["env_ok"] is True and out2["picked"][0]["code"] == "600000"
    assert acc.state["screens_done"] == ["pickT2026-09-04T15:05"]


# --------------------------------------------------------------- F2
def test_min_commission_floor_small_buy(tmp_path):
    """F2: 1000 股 × 10.0 = 1 万 → 佣金比例 2.5 < 5 → 佣金按下限 5,
    **过户费按比例另计**(下限不覆盖它) → 5 + 0.1 = 5.1(同回测口径)。"""
    acc = _acc(tmp_path)
    done = acc._record_buy("600000", 1000, 10.0,
                           datetime(2026, 9, 2, 10, 0), "screen")
    assert done["fee"] == 5.1
    assert acc.state["cash"] == round(1000000.0 - 10000.0 - 5.1, 2)


def test_min_commission_floor_small_sell(tmp_path):
    """F2: 卖出腿 = max(成交额×佣金率, 5) + 过户费 + 印花税。
    1000×6.0×0.999 = 5994 → 5 + 0.05994 + 2.997 = 8.05694 → 8.06。"""
    acc = _acc(tmp_path)
    acc.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 6.0,
        "buy_date": "2026-09-01", "buy_price": 6.0, "entry_nav": 1e6})
    out = acc._execute_sell("600000.SH", 6.0, "take_profit",
                            now=datetime(2026, 9, 2, 10, 0))
    amount = round(1000 * round(6.0 * 0.999, 4), 2)          # 5994.0
    assert out["fee"] == 8.06
    assert acc.state["cash"] == round(1000000.0 + amount - 8.06, 2)


def test_min_commission_does_not_inflate_large_order(tmp_path):
    """F2: 30 万买入 → 佣金比例 75 > 5, 下限不触发 → 78.0(与旧口径/回测同值)。"""
    acc = _acc(tmp_path)
    done = acc._record_buy("600000", 30000, 10.0,
                           datetime(2026, 9, 2, 10, 0), "screen")
    assert done["fee"] == round(300000.0 * (0.00025 + 0.00001), 2)   # 78.0


def test_min_commission_is_configurable_and_echoed(tmp_path):
    """F2: min_commission 是构造配置(非算式里的魔法数), 且可被读到(回显)。"""
    acc = _acc(tmp_path, min_commission=3.0)
    assert acc.min_commission == 3.0
    assert acc._record_buy("600000", 1000, 10.0,
                           datetime(2026, 9, 2, 10, 0),
                           "screen")["fee"] == 3.1     # max(2.5, 3) + 过户 0.1


# ---- F2 强一致性守卫: 与回测 prism/backtest.py 同一笔单腿费用**逐分相等** ----
def _bt_cost_defaults():
    """回测侧**真实**成本默认值(读真实签名默认值, 不重写常量)。"""
    sig = inspect.signature(backtest.Backtester.__init__)
    return {k: float(sig.parameters[k].default)
            for k in ("fee_rate", "transfer_fee", "stamp_duty",
                      "min_commission")}


class _BtFloorStub:
    """只喂 `_commission_floor` 真正用到的两个属性(不构造完整 Backtester,
    但仍调用**回测的真实方法**)。"""

    def __init__(self, fee_rate, min_commission):
        self.fee_rate = fee_rate
        self.min_commission = min_commission


def _bt_floor(notional):
    d = _bt_cost_defaults()
    return backtest.Backtester._commission_floor(
        _BtFloorStub(d["fee_rate"], d["min_commission"]), notional)


def _bt_leg_fee(notional, sell):
    """回测侧同笔单腿费用(元): 名义额×费率 + 佣金下限差额。

    费率为回测 `_simulate_trade`(买) / `_simulate_equity` 卖出分支(卖)的内联
    口径 —— 佣金 fee_rate 走**真实** `Backtester._commission_floor`
    (= max(名义额×fee_rate, ¥5) − 名义额×fee_rate), 过户费/印花税按比例另计。
    """
    d = _bt_cost_defaults()
    rate = d["fee_rate"] + d["transfer_fee"] \
        + (d["stamp_duty"] if sell else 0.0)
    return round(notional * rate + _bt_floor(notional), 2)


# 佣金下限的临界点是 ¥20000(名义额×万2.5 = 5): 下方下限生效、上方纯比例,
# 两侧各取样; 另取本账户真实单笔量级(30 万)与极端小额做端到端对照。
_FEE_NOTIONALS = [2000.0, 6000.0, 19999.0, 20001.0, 30000.0, 300000.0,
                  1000000.0]


@pytest.mark.parametrize("sell", [False, True], ids=["buy", "sell"])
@pytest.mark.parametrize("notional", _FEE_NOTIONALS)
def test_fee_matches_backtest_leg_by_leg(tmp_path, notional, sell):
    """F2 守卫: 同一笔单腿费用 paper 与回测**逐分相等**(两侧口径不许再漂移)。"""
    acc = _acc(tmp_path)
    amount = round(notional, 2)
    assert acc._fee(amount, sell=sell) == _bt_leg_fee(amount, sell)


def test_fee_constants_match_backtest_defaults(tmp_path):
    """F2 守卫: 费率常量本身也必须与回测默认值一致(常量漂移也算口径漂移)。"""
    acc = _acc(tmp_path)
    d = _bt_cost_defaults()
    assert (acc.fee_rate, acc.transfer_fee, acc.stamp_duty,
            acc.min_commission) == (d["fee_rate"], d["transfer_fee"],
                                    d["stamp_duty"], d["min_commission"])


# --------------------------------------------------------------- F3
def test_cancel_and_expire_trades_are_not_buys(tmp_path):
    """F3: 未成交流水不再用 side="buy" 冒充成交 —— 撤单=cancel, 失效=expire,
    且不带真实成交的 amount/fee/cash_after(消灭"同表两种 schema")。"""
    acc = _acc(tmp_path)
    now = datetime(2026, 9, 2, 10, 0, 5)
    acc.create_pending_buy("600000", 10.0, now, _tick(10.0))
    acc.create_pending_buy("600001", 10.0, now, _tick(10.0))
    acc.check_pending_buys({"600000": _tick(9.99)},
                           now=datetime(2026, 9, 2, 10, 0, 35))
    acc._dispose_pending("600001", "queue_expire",
                         datetime(2026, 9, 2, 15, 0, 5))
    by_reason = {t["reason"]: t for t in acc.state["trades"]}
    assert by_reason["queue_cancel_break"]["side"] == "cancel"
    assert by_reason["queue_expire"]["side"] == "expire"
    for r in ("queue_cancel_break", "queue_expire"):
        t = by_reason[r]
        assert not {"amount", "fee", "cash_after"} & set(t)   # 无成交字段
        assert t["date"] == "2026-09-02"   # paper_daemon 靠 trades[-1]["date"]
    # 账本里再无"看起来像买入"的行
    assert [t["side"] for t in acc.state["trades"]] == ["cancel", "expire"]


def test_cancel_expire_do_not_block_today_retrade(tmp_path):
    """F3 回归守卫: 走真实路径产生撤单/失效/成交三种流水后,
    (a) 按 side 过滤的消费者看不到任何"买入"行(旧语义下撤单/失效都是 buy);
    (b) _buyable 的"今日已交易"只认真实成交(side=="buy" 且 reason in
        screen/queue_fill), 撤单(禁排键)/失效都不占键。"""
    acc = _acc(tmp_path)
    now = datetime(2026, 9, 2, 10, 0, 5)
    acc.create_pending_buy("600000", 10.0, now, _tick(10.0))     # 开板撤单
    acc.check_pending_buys({"600000": _tick(9.99)},
                           now=datetime(2026, 9, 2, 10, 0, 35))
    acc.create_pending_buy("600001", 10.0, now, _tick(10.0))     # 收盘失效
    acc._dispose_pending("600001", "queue_expire",
                         datetime(2026, 9, 2, 15, 0, 5))
    assert [t for t in acc.state["trades"] if t.get("side") == "buy"] == []
    assert acc._buyable("600000", 1e6, now) == "今日已撤单"       # 撤单键仍在
    assert acc._buyable("600001", 1e6, now) is None              # 失效≠今日已交易
    acc._execute_buy("600002", 10.0, now=now, slip=0.0)          # 真实成交
    acc.state["holdings"] = []                                   # 当日卖出后再买
    assert acc._buyable("600002", 1e6, now) == "今日已交易"       # 真成交占键


# --------------------------------------------------------------- F4
@pytest.mark.parametrize("code,expected", [
    ("600000", 0.10), ("000001", 0.10), ("600000.SH", 0.10),   # 主板
    ("300750", 0.20), ("301001", 0.20), ("688981", 0.20),      # 创业/科创
    ("689009", 0.20),                                          # 科创CDR
    ("920001", 0.30), ("830799", 0.30), ("430001", 0.30),      # 北交所
    ("400001", 0.05), ("420001", 0.05),                        # 老三板(非北交所 30%)
])
def test_limit_ratio_by_board(code, expected):
    """F4: 北交所 92/8/4 开头 30%(此前误按 10% → -11% 的非跌停股被当跌停不卖)。

    权威规格 = shared/exit_rules.limit_ratio; 全副本逐码钉在
    datasource/tests/test_common.py::test_limit_ratio_consistent_with_exit_rules。"""
    assert _limit_ratio(code) == expected


def test_bse_limit_down_band_30pct_discriminates(tmp_path, monkeypatch):
    """F4 判别性对照: 现价 8.9 / 昨收 10.0(-11%):
    北交所 830799(30% 档, 跌停价 7.0)→ 非跌停 → 照常止损卖出;
    主板 600000(10% 档, 跌停价 9.0)→ 跌停 → 顺延不卖。"""
    pin = {"sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                          "max_hold_days": 5}}
    monkeypatch.setattr(PaperAccount, "strategy", property(lambda self: pin))
    now = datetime(2026, 9, 2, 10, 0, 0)

    def _mk(code):
        a = PaperAccount(state_path=tmp_path / ("%s.json" % code[:6]))
        a.init_account(created="2026-09-01")
        a.state["cash"] = 600000.0
        a.state["holdings"].append({
            "code": code, "shares": 1000, "cost": 10.0,
            "buy_date": "2026-09-01", "buy_price": 10.0, "entry_nav": 1e6})
        return a

    bse = _mk("830799.BJ")
    out = bse.sell_check({"830799.BJ": {"lastPrice": 8.9, "lastClose": 10.0}},
                         now=now)
    assert [o["reason"] for o in out] == ["stop_loss"]
    main = _mk("600000.SH")
    out2 = main.sell_check({"600000.SH": {"lastPrice": 8.9, "lastClose": 10.0}},
                           now=now)
    assert out2 == [] and len(main.state["holdings"]) == 1
