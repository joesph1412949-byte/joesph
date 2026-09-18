# -*- coding: utf-8 -*-
"""Task 5: 报告 `factor_hits`(因子存活率)+ `data_notes` 补充说明 —— 全离线测试。

规格 2026-09-16 §8 / 简报 Task 5。目的: 杜绝"跑的是残废版策略"——回测里
哪些因子在真实打分、哪些恒 0、哪些一次都没被评估, 必须一眼可见。

覆盖:
  ① `factor_hits` 含门控 + 全部评分因子(策略声明即入表), 每只被评估的候选
     记一次 eval, 门控每个被评估的交易日记一次 eval
  ② **fail-open 0 的因子也记 eval**(否则"恒 0"看不见, 只剩静默)
  ③ rate/status 口径: 实算 / 恒0 / 代理 / 未评估(evals=0 → rate=None, 不拿 0 冒充)
  ④ `data_notes`: day_feed 既有说明原样保留; F3 走分钟级代理 / Y2·Y5 快照类
     历史缺失 / 流通股本用当前值近似 —— **只在确实用到时加**(没用到不加,
     否则既有测试的 `data_notes == feed.data_notes` 断言会被无谓噪音打破)

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
CODE = "600000.SH"


def _mk_strategy(gate_threshold=1, snapshot=False, seal=False):
    """门控 N1/N2(N1 恒命中, N2 恒 0)+ 评分 A1/A2(A1 恒命中, A2 恒 0)。

    snapshot=True: 评分模型额外引用 Y2/Y5(快照类, 回测剔除防未来)。
    seal=True: 评分模型额外引用 F3(1m 特征在场时走分钟级代理)。
    """
    factors = ["A1", "A2"] + (["F3"] if seal else []) \
        + (["Y2", "Y5"] if snapshot else [])
    return load_strategy({
        "id": "bt_fh", "name": "存活率", "description": "",
        "market_gate": {"model": "node", "threshold": gate_threshold,
                        "factors": ["N1", "N2"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0, "factors": factors},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1, "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                       "max_hold_days": 5},
    })


@pytest.fixture(autouse=True)
def _factors():
    """假因子: N1/A1 恒命中; N2/A2 恒 0(模拟缺数据 fail-open); F3/Y2/Y5 恒 0。"""
    reg.reset()
    for fid in ("N1", "A1"):
        reg.FACTORS[fid] = {"id": fid, "name": fid, "category": "测试",
                            "description": "", "func": lambda ctx: {"score": 1}}
    for fid in ("N2", "A2", "F3", "Y2", "Y5"):
        reg.FACTORS[fid] = {"id": fid, "name": fid, "category": "测试",
                            "description": "", "func": lambda ctx: {"score": 0}}
    yield


def _feeds():
    """涨停池: 07-01 / 07-02 各 1 只(门控与候选各被评估 2 次)。"""
    def zf(d):
        if d in ("20260701", "20260702"):
            return [{"code": CODE, "boards": 1, "theme": "机器人"}]
        return []

    def kf(code):
        dates = [(START + timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(8)]
        return list(zip(dates, [10.0 * (1.02 ** i) for i in range(8)]))
    return zf, kf


def _bt(strategy=None, **kw):
    zf, kf = _feeds()
    return backtest.Backtester(strategy or _mk_strategy(),
                               zt_feed=kw.pop("zt_feed", zf),
                               kline_feed=kw.pop("kline_feed", kf), **kw)


# ---------------- ① 覆盖: 门控 + 全部评分因子 ----------------

def test_factor_hits_covers_gate_and_scoring_factors():
    """① 策略声明的门控 + 全部评分因子都进表, 各自带 kind/evals/hits。"""
    rep = _bt().run(START, START + timedelta(days=2))
    fh = rep["factor_hits"]
    assert set(fh) == {"N1", "N2", "A1", "A2"}, "门控与评分因子都要在表里"
    assert fh["N1"]["kind"] == "gate" and fh["N2"]["kind"] == "gate"
    assert fh["A1"]["kind"] == "scoring"
    # 2 个有池的交易日 → 门控各评估 2 次; 2 只候选 → 评分因子各评估 2 次
    assert fh["N1"]["evals"] == 2 and fh["N1"]["hits"] == 2
    assert fh["A1"]["evals"] == 2 and fh["A1"]["hits"] == 2
    assert fh["A1"]["rate"] == 1.0 and fh["A1"]["status"] == "实算"
    assert rep["trades"] == 2, "前提: 这两天真的买了(存活率不是空表自证)"


def test_factor_hits_keeps_fail_open_zero_visible():
    """② 缺数据 fail-open 0 的因子**也记 eval** —— 否则"恒 0"看不见。"""
    rep = _bt().run(START, START + timedelta(days=2))
    fh = rep["factor_hits"]
    for fid in ("N2", "A2"):
        assert fh[fid]["evals"] == 2, "%s 评估过就要记 eval(恒 0 也要可见)" % fid
        assert fh[fid]["hits"] == 0
        assert fh[fid]["rate"] == 0.0 and fh[fid]["status"] == "恒0"


def test_factor_hits_zero_evals_never_fake_a_rate():
    """③ 门控永不过 → 评分因子一次没算: evals=0 / rate=None / 状态"未评估"。

    这正是"残废版策略"的指纹(候选全 0 分被过滤 vs 根本没评估), 必须能区分。
    """
    rep = _bt(_mk_strategy(gate_threshold=2)).run(
        START, START + timedelta(days=2))
    fh = rep["factor_hits"]
    assert rep["trades"] == 0
    assert rep["filter_stats"]["gate_blocked_days"] == 2
    assert fh["N1"]["evals"] == 2, "门控照常被评估"
    assert fh["A1"]["evals"] == 0 and fh["A1"]["hits"] == 0
    assert fh["A1"]["rate"] is None, "没评估过就没有命中率, 不用 0 冒充"
    assert fh["A1"]["status"] == "未评估"
    assert fh["A1"]["kind"] == "scoring"


def test_factor_hits_present_in_zero_trade_report():
    """③ 零交易报告(两个分支共用 base)也带 factor_hits 键。"""
    rep = _bt(zt_feed=lambda d: []).run(START, START + timedelta(days=1))
    assert rep["trades"] == 0
    assert rep["factor_hits"]["N1"]["evals"] == 0
    assert rep["factor_hits"]["N1"]["status"] == "未评估"


# ---------------- ④ data_notes 补充说明 ----------------

def test_data_notes_keeps_feed_notes_and_adds_proxy_snapshot_float():
    """④ day_feed 既有说明原样保留 + F3 代理 / Y2·Y5 / 流通股本 三条补充。"""
    class Feed:
        data_notes = ["1m特征: 个股日覆盖 1/2 (缓存缺失静默降级 → F2/F3 fail-open 0)"]

        def __call__(self, d):
            if d == START:
                return {"stock": {CODE: {"bt_seal_ratio": 0.001,
                                         "float_vol": 3.0e9}}}
            return None

    rep = _bt(_mk_strategy(snapshot=True, seal=True)).run(
        START, START + timedelta(days=2), day_feed=Feed())
    notes = rep["data_notes"]
    assert notes[0] == Feed.data_notes[0], "day_feed 的覆盖说明原样保留(不挤占)"
    joined = "\n".join(notes)
    assert "F3" in joined and "代理" in joined, "F3 走分钟级代理必须标注"
    assert "Y2" in joined and "Y5" in joined, "Y2/Y5 历史缺失期为 0 必须标注"
    assert "流通股本" in joined, "流通股本用当前值近似必须标注"
    # F3 不在策略里 → 不替它发言(报告不为没用到的因子写说明)
    rep2 = _bt(_mk_strategy()).run(START, START + timedelta(days=2),
                                   day_feed=Feed())
    assert not any("代理" in n for n in rep2["data_notes"]), \
        "策略没用到 F3 就不写 F3 的代理说明(其余说明照常)"
    assert any("流通股本" in n for n in rep2["data_notes"]), "其余三条不受影响"


def test_data_notes_adds_nothing_when_data_unused():
    """④ 没用到就不加(向后兼容: 既有 `data_notes == feed.data_notes` 断言)。"""
    rep = _bt().run(START, START + timedelta(days=2),
                    day_feed=lambda d: {"stock": {CODE: {}}})
    assert rep["data_notes"] == [], "没注入 1m 特征/基本面 → 不加补充说明"
