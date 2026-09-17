# -*- coding: utf-8 -*-
"""A股成交约束(规格 §6, 借鉴 Vibe-Trading `engines/china_a.py`, MIT)离线测试。

覆盖:
  ① ¥5 最低佣金: `max(名义额×费率, 5元)` —— **买、卖双腿各收一次**(源口径无条件
     取大, is_open 只影响印花税); 小额单被下限抬高成本, 大额单不受影响;
     报告 avg_cost_pct 与净值口径一致(下限差额不漏账)
  ② 100 股整手: 资金折算股数向下取整(余款留现金); 不足一手 → 买不到(计入 skipped)
  ③ 一字板买不到: `one_word=True` → 该笔不建仓并计入 filter_stats;
     `one_word` 缺失 = 未知(既不拦也不当 False), 只记数
  ④ 报告回归: 有交易分支必须有 data_notes 键(既有漏键 bug)

全离线: 假涨停池 + 假K线 + 手搓 day_feed, 不连 QMT/网络。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
from prism import backtest
from prism.engine import load_strategy

START = date(2026, 7, 1)


@pytest.fixture(autouse=True)
def _factors():
    """假因子: N1 门控恒过, A1 评分恒命中 —— 断言只关心成交约束, 不关心选股。"""
    reg.reset()

    @reg.factor(id="N1", name="n", category="node", description="")
    def f_n(ctx):
        return {"score": 1, "note": ""}

    @reg.factor(id="A1", name="a", category="通用", description="")
    def f_a(ctx):
        return {"score": 1, "note": ""}

    yield


def _mk_strategy():
    return load_strategy({
        "id": "bt_costs", "name": "成交约束", "description": "",
        "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0,
             "factors": [{"id": "A1", "op": ">", "threshold": 0}]},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1, "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.30, "stop_loss_pct": 0.5,
                       "max_hold_days": 5},
    })


def _bt(**kw):
    """空 feed 回测器: 只验资金/成交层, 不跑选股。"""
    kw.setdefault("zt_feed", lambda d: [])
    kw.setdefault("kline_feed", lambda c: [])
    return backtest.Backtester(_mk_strategy(), **kw)


def _trade(entry, ret, code="600000.SH", day="2026-07-01",
           exit_date="2026-07-02", **extra):
    """手搓一笔(与 run() 产出的 trade 同型, 只留资金层用到的字段)。

    exit_date 必给: 净值模拟在卖出日才回款(持有期按成本计), 不给则收益率
    永远不落地, 看不出成本/手数差异。extra: 额外字段(如 cost_pct)。
    """
    t = {"date": day, "code": code, "pos_mult": 1.0, "entry": entry,
         "return_pct": ret, "exit_date": exit_date}
    t.update(extra)
    return t


def _pool_zt(days, code="600000.SH"):
    def zf(d):
        return [{"code": code, "boards": 1, "theme": "机器人"}] if d in days else []
    return zf


def _kline(closes, start=START):
    return [((start + timedelta(days=i)).strftime("%Y-%m-%d"), c)
            for i, c in enumerate(closes)]


def _d8(d):
    return d.strftime("%Y%m%d")


# ---------------- ① ¥5 最低佣金 ----------------

def test_min_commission_floor_on_small_trade():
    """① 佣金 max(名义额×费率, ¥5): 小额单**买卖双腿**各触一次下限。

    源口径(Vibe-Trading china_a.calc_commission): `comm = max(notional*rate, min)`
    无条件取大, `is_open` 只决定印花税 → 买、卖两次成交各收一次下限。
    1 万本金 / 10 元 / +10% / 万2.5:
      买入腿: 比例 10000×0.00025 = 2.5 < 5 → 按 5(收益率 10% → 9.975%)
      卖出腿: 回款 10997.5, 比例 2.749375 < 5 → 再扣差额 2.250625
      → 净值 10995.25(只算买入腿的旧口径 = 10997.5, 每往返少算 2.25 元)
    """
    small = _bt(initial_capital=10_000.0, position_ratio=1.0,
                fee_rate=0.00025, slippage=0.0, stamp_duty=0.0,
                transfer_fee=0.0)
    curve, skipped = small._simulate_equity([_trade(entry=10.0, ret=10.0)])
    assert skipped == 0
    assert curve[-1][1] == pytest.approx(10_995.25, abs=0.01)
    # 双腿下限合计 4.750625 元(2.5 买 + 2.250625 卖); 旧口径只扣买入腿 2.5 元
    assert 11_000.0 - curve[-1][1] == pytest.approx(4.750625, abs=0.01)
    # 同一笔放到 100 万本金: 比例佣金 250/275 元 > 5 → 双腿下限都不触发, 仍是 +10%
    big = _bt(initial_capital=1_000_000.0, position_ratio=1.0,
              fee_rate=0.00025, slippage=0.0, stamp_duty=0.0,
              transfer_fee=0.0)
    curve_big, _ = big._simulate_equity([_trade(entry=10.0, ret=10.0)])
    assert curve_big[-1][1] == pytest.approx(1_100_000.0, abs=1.0)


def test_min_commission_zero_restores_pure_proportional():
    """① min_commission=0 → 买卖双腿都没有下限差额(既有"零成本"用例的语义)。

    min_commission 默认 5.0 会改写 fee_rate=0 的"零成本"含义(每腿仍至少 ¥5),
    显式设 0 才是纯比例口径。
    """
    kw = dict(initial_capital=10_000.0, position_ratio=1.0, slippage=0.0,
              stamp_duty=0.0, transfer_fee=0.0, min_commission=0.0)
    no_floor, _ = _bt(fee_rate=0.00025, **kw)._simulate_equity(
        [_trade(entry=10.0, ret=10.0)])
    zero_cost, _ = _bt(fee_rate=0.0, **kw)._simulate_equity(
        [_trade(entry=10.0, ret=10.0)])
    assert no_floor[-1][1] == pytest.approx(11_000.0, abs=0.01)
    assert no_floor[-1][1] == zero_cost[-1][1]


def test_report_cost_is_consistent_with_nav():
    """① 报告成本必须与净值口径一致(不留暗账): 资金层按名义额扣的双腿下限
    差额进 trade["floor_cost_pct"], 报告 avg_cost_pct = cost_pct + 该差额。"""
    bt = _bt(initial_capital=10_000.0, position_ratio=1.0, fee_rate=0.00025,
             slippage=0.0, stamp_duty=0.0, transfer_fee=0.0)
    trades = [_trade(entry=10.0, ret=10.0, cost_pct=0.05)]
    bt._simulate_equity(trades)
    rep = bt._report(trades, [START], gate_notes=[], equity_stats={})
    # (2.5 + 2.250625) / 10000 元 = 0.04750625%
    assert trades[0]["floor_cost_pct"] == pytest.approx(0.047506, abs=1e-6)
    assert rep["avg_cost_pct"] == pytest.approx(0.098, abs=1e-6)   # 0.05 + 0.0475
    # 大额单不触下限 → 报告成本就是单股比例口径(不虚增)
    big = _bt(initial_capital=1_000_000.0, position_ratio=1.0,
              fee_rate=0.00025, slippage=0.0, stamp_duty=0.0,
              transfer_fee=0.0)
    big_trades = [_trade(entry=10.0, ret=10.0, cost_pct=0.05)]
    big._simulate_equity(big_trades)
    assert "floor_cost_pct" not in big_trades[0]
    assert big._report(big_trades, [START], gate_notes=[],
                       equity_stats={})["avg_cost_pct"] == 0.05


def test_report_echoes_cost_config():
    """① min_commission 默认 ¥5 → fee_rate=0 也不是"零成本"; 报告回显成本参数
    (run() 真的接上), 后人拿零成本配置对净值时先看这里(不踩坑)。"""
    closes = [10.0 * (1.02 ** i) for i in range(8)]

    def _run(**kw):
        bt = _bt(zt_feed=_pool_zt({_d8(START)}),
                 kline_feed=lambda c: _kline(closes), **kw)
        return bt.run(START, START + timedelta(days=2), day_feed=lambda d: None)

    rep = _run(fee_rate=0.0, min_commission=0.0)
    assert rep["cost_config"]["fee_rate"] == 0.0
    assert rep["cost_config"]["min_commission"] == 0.0
    dflt = _run()
    assert dflt["cost_config"]["min_commission"] == 5.0
    assert dflt["cost_config"]["fee_rate"] == 0.00025
    assert dflt["cost_config"]["stamp_duty"] == 0.0005


# ---------------- ② 100 股整手 ----------------

def test_position_rounded_down_to_100_share_lots():
    """② 100 股整手: 股数向下取整, 余款留现金(只有整手数吃收益)。"""
    # 10050 元 / 10.0 = 1005 股 → 取整 1000 股 = 10000 元(余 50 元现金)
    # 只有 10000 元吃到 +10% → 回款 11000 + 50 = 11050(不取整会是 11055)
    bt = _bt(initial_capital=10_050.0, position_ratio=1.0,
             fee_rate=0.0, slippage=0.0, stamp_duty=0.0, transfer_fee=0.0,
             min_commission=0.0)
    curve, skipped = bt._simulate_equity([_trade(entry=10.0, ret=10.0)])
    assert skipped == 0
    assert curve[-1][1] == pytest.approx(11_050.0, abs=0.01)
    # 900 元 < 一手(100 股 × 10.0 = 1000 元) → 买不到, 净值原样
    tiny = _bt(initial_capital=900.0, position_ratio=1.0,
               fee_rate=0.0, slippage=0.0, stamp_duty=0.0, transfer_fee=0.0,
               min_commission=0.0)
    curve_t, skipped_t = tiny._simulate_equity([_trade(entry=10.0, ret=50.0)])
    assert skipped_t == 1
    assert curve_t[-1][1] == pytest.approx(900.0, abs=0.01)


# ---------------- ③ 一字板买不到 ----------------

def test_one_word_day_blocks_buy_and_counts():
    """③ one_word=True → 该日不建仓(计入 filter_stats); 次日可正常买。"""
    closes = [10.0 * (1.02 ** i) for i in range(8)]
    by_day = {
        START: {"stock": {"600000.SH": {"one_word": True}}},
        START + timedelta(days=1): {"stock": {"600000.SH": {"one_word": False}}},
    }
    bt = _bt(zt_feed=_pool_zt({_d8(START), _d8(START + timedelta(days=1))}),
             kline_feed=lambda c: _kline(closes))
    rep = bt.run(START, START + timedelta(days=2),
                 day_feed=lambda d: by_day.get(d))
    assert rep["trades"] == 1
    assert [t["date"] for t in rep["trade_log"]] == ["2026-07-02"]
    fs = rep["filter_stats"]
    assert fs["filtered_one_word"] == 1     # 一字板拦下 1 笔
    assert fs["one_word_unknown"] == 0      # 两天都有特征 → 无未知


def test_one_word_unknown_is_not_blocked_but_counted():
    """③ one_word 缺失(1m 缓存没覆盖)= 未知: 不拦(不造假), 但要记数。"""
    closes = [10.0 * (1.02 ** i) for i in range(8)]
    bt = _bt(zt_feed=_pool_zt({_d8(START)}),
             kline_feed=lambda c: _kline(closes))
    rep = bt.run(START, START + timedelta(days=2),
                 day_feed=lambda d: ({"stock": {"600000.SH": {"sealed": True}}}
                                     if d == START else None))
    assert rep["trades"] == 1               # 未知不拦
    fs = rep["filter_stats"]
    assert fs["filtered_one_word"] == 0
    assert fs["one_word_unknown"] == 1


# ---------------- ④ 报告回归: 有交易分支的 data_notes ----------------

def test_report_with_trades_keeps_data_notes():
    """④ 有交易的报告也必须有 data_notes(既有 bug: 有交易分支漏了该键)。"""
    class Feed:
        data_notes = ["1m特征: 个股日覆盖 1/2 (缓存缺失静默降级 → F2/F3 fail-open 0)"]

        def __call__(self, d):
            return None

    closes = [10.0 * (1.02 ** i) for i in range(8)]
    bt = _bt(zt_feed=_pool_zt({_d8(START)}),
             kline_feed=lambda c: _kline(closes))
    rep = bt.run(START, START + timedelta(days=2), day_feed=Feed())
    assert rep["trades"] == 1
    assert rep["data_notes"] == Feed.data_notes
