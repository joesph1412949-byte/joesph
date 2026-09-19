# -*- coding: utf-8 -*-
"""模拟盘结算与净值测试 — 到期卖出/定格/幂等/缺口补算, 全离线。"""
import logging
import sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism.paper import PaperAccount


NOW = datetime(2026, 9, 8, 15, 5, 0)


@pytest.fixture
def acc(tmp_path):
    a = PaperAccount(state_path=tmp_path / "paper.json")
    a.init_account(created="2026-09-01")
    a.state["cash"] = 600000.0
    a.state["holdings"].append({
        "code": "600000.SH", "shares": 1000, "cost": 9.5,
        "buy_date": "2026-09-01", "buy_price": 9.5, "entry_nav": 1e6})
    return a


def test_settle_expire_sell(acc):
    """持有满5交易日 → 收盘价卖出 + 净值定格。"""
    out = acc.settle_day(lambda code, day=None: 10.2,
                         due_fn=lambda c, bd: True, now=NOW)
    assert len(out["closed"]) == 1
    assert out["closed"][0]["reason"] == "hold_expire"
    sp = out["closed"][0]["price"]              # 10.2×0.999=10.1898
    assert sp == round(10.2 * 0.999, 4)
    assert acc.state["holdings"] == []
    # 无持仓 → nav=现金
    assert out["nav"] == acc.state["cash"]
    assert acc.state["nav_history"][-1]["date"] == "2026-09-08"
    assert acc.state["settled_dates"] == ["2026-09-08"]


def test_settle_mark_to_market(acc):
    """未到期持仓按收盘价盯市定格净值。"""
    out = acc.settle_day(lambda code, day=None: 10.0,
                         due_fn=lambda c, bd: False, now=NOW)
    assert out["closed"] == []
    assert len(acc.state["holdings"]) == 1
    assert out["nav"] == round(600000.0 + 1000 * 10.0, 2)
    assert acc.state["nav_history"][0]["nav"] == 610000.0


def test_settle_idempotent(acc):
    acc.settle_day(lambda c, d=None: 10.0, due_fn=lambda c, b: False, now=NOW)
    out = acc.settle_day(lambda c, d=None: 10.0, due_fn=lambda c, b: True,
                         now=NOW)
    assert out.get("already_done") is True
    assert len(acc.state["holdings"]) == 1      # 幂等: 不再卖


def test_settle_no_price_keeps_holding(acc):
    out = acc.settle_day(lambda c, d=None: None, due_fn=lambda c, b: True,
                         now=NOW)
    assert out["closed"] == [] and len(acc.state["holdings"]) == 1


def test_backfill_nav(acc):
    """缺口日补算: nav_history 末日后交易日逐日盯市; created 之前不补(M-b)。"""
    acc.state["nav_history"] = []               # 全空 → last=None, 从头补
    days = ["2026-08-28", "2026-09-02", "2026-09-03", "2026-09-08",
            "2026-09-09"]
    # 修正并注明: 简报蓝图内部直读真实时钟判"今日", 而系统时钟(2026-09-01)
    # ≠ 测试帧(NOW=9-8) → 简报测试原样必失败(n=0)。按测试意图(9-9 为未来、
    # 9-8 为今日)给 backfill_nav 注入 now=NOW(与 settle_day 同款 now 参数)。
    n = acc.backfill_nav(
        lambda code, day=None: {"2026-09-02": 9.6, "2026-09-03": 9.4,
                                "2026-09-08": 10.0}.get(day), days, now=NOW)
    assert n == 3                 # 08-28 在 created(09-01) 之前 → 不补; 9-9 未来 → 不补
    hist = acc.state["nav_history"]
    assert [h["date"] for h in hist] == ["2026-09-02", "2026-09-03",
                                         "2026-09-08"]
    assert [h["nav"] for h in hist] == [609600.0, 609400.0, 610000.0]
    assert acc.state["settled_dates"] == ["2026-09-08"]   # 今日补算即结算


# ---------------- 结算幂等键: 行情全缺不得烧掉当日 settled_dates ----------------
def _close_raises(code, day=None):
    """取价抛异常形态(生产 close_fn 把异常吞成 None, 两种形态都算"缺")。"""
    raise RuntimeError("行情全挂")


@pytest.mark.parametrize("dead", [
    pytest.param(lambda code, day=None: None, id="returns-none"),
    pytest.param(_close_raises, id="raises"),
])
def test_settle_all_prices_missing_does_not_burn_key(acc, dead):
    """行情全缺(拿不到任何有效收盘价)→ 不登记 settled_dates, 下一轮真重算。

    旧行为: `_nav_at` 用 `close_fn(...) or h["cost"]` 兜底盯市, 却照样 append
    settled_dates ⇒ 一次瞬时故障(行情全挂) = 当天净值永久定格在成本价口径,
    且 paper_daemon 的 `d not in settled_dates` 门此后永不重算。
    判别力落在"键没被占 **且** 下一轮真的重算"(不是"净值没变"这种弱断言)。"""
    out = acc.settle_day(dead, due_fn=lambda c, bd: True, now=NOW)
    assert out.get("error")                    # 不静默成功
    assert acc.state["settled_dates"] == []    # 未占当日结算键
    assert acc.state["nav_history"] == []      # 也没落成本价口径的平点
    assert len(acc.state["holdings"]) == 1     # 缺价不卖(原语义保留)
    # 下一轮行情恢复 → 真的重算 + 登记(旧行为下这里恒 already_done)
    out2 = acc.settle_day(lambda c, d=None: 10.2, due_fn=lambda c, bd: True,
                          now=NOW)
    assert out2.get("already_done") is None
    assert acc.state["settled_dates"] == ["2026-09-08"]
    assert acc.state["nav_history"][-1]["date"] == "2026-09-08"


def test_settle_normal_prices_unchanged(acc, caplog):
    """反向: 行情全部正常 → 照常登记, nav/返回/日志与改动前逐字节一致。"""
    with caplog.at_level(logging.WARNING, logger="prism.paper"):
        out = acc.settle_day(lambda c, d=None: 10.0, due_fn=lambda c, bd: False,
                             now=NOW)
    assert acc.state["settled_dates"] == ["2026-09-08"]
    assert acc.state["nav_history"] == [{"date": "2026-09-08", "nav": 610000.0}]
    assert out == {"closed": [], "nav": 610000.0}    # 无 cost_fallback 等新键
    assert caplog.text == ""                         # 正常路径零日志


@pytest.mark.parametrize("dead", [
    pytest.param(lambda code, day=None: None, id="returns-none"),
    pytest.param(_close_raises, id="raises"),
])
def test_settle_partial_prices_missing_settles_with_cost_fallback(acc, dead,
                                                                 caplog):
    """个别票缺价 → 维持现状(成本价兜底 + 照常结算, 不重跑整天),
    但日志与返回点名"几只用了成本价兜底"。"""
    acc.state["holdings"].append({
        "code": "000001.SZ", "shares": 500, "cost": 12.0,
        "buy_date": "2026-09-01", "buy_price": 12.0, "entry_nav": 1e6})
    with caplog.at_level(logging.WARNING, logger="prism.paper"):
        out = acc.settle_day(
            lambda code, d=None: dead(code) if code.startswith("000") else 10.0,
            due_fn=lambda c, bd: False, now=NOW)
    assert acc.state["settled_dates"] == ["2026-09-08"]     # 照常占键(不重跑)
    assert out["nav"] == round(600000.0 + 1000 * 10.0 + 500 * 12.0, 2)
    assert out["cost_fallback"] == 1
    assert "成本价" in caplog.text and "1/2" in caplog.text


def test_settle_zero_price_is_not_missing(acc, caplog):
    """0 价/停牌 ≠ 缺价: close_fn 返回 0 是**有值**(真值判断会把它当缺) →
    照常结算且不报成本价兜底。防止用 `or cost` 的真值当缺价判据。"""
    with caplog.at_level(logging.WARNING, logger="prism.paper"):
        out = acc.settle_day(lambda c, d=None: 0.0, due_fn=lambda c, bd: False,
                             now=NOW)
    assert acc.state["settled_dates"] == ["2026-09-08"]     # 有值 → 照常占键
    assert "cost_fallback" not in out
    assert caplog.text == ""


def test_backfill_today_all_missing_does_not_burn_key(acc):
    """同源第二处: backfill 补算**今日**时行情全缺 → 不写成本价平点、不占
    当日结算键(旧行为: 收盘后重启一次补算就把今天烧掉 → 15:00 的 settle_day
    永不重算, 该卖的到期仓当天也不卖)。往日缺口口径不变。"""
    acc.state["nav_history"] = []
    n = acc.backfill_nav(lambda code, day=None: None,
                         ["2026-09-07", "2026-09-08"], now=NOW)
    assert n == 1                                   # 昨日缺口照旧补(口径不变)
    assert [h["date"] for h in acc.state["nav_history"]] == ["2026-09-07"]
    assert acc.state["settled_dates"] == []         # 今日不占键
    # 行情恢复 → 今日真的重算(旧行为下 settled_dates 已含今日, 此处恒 0)
    n2 = acc.backfill_nav(lambda code, day=None: 10.0,
                          ["2026-09-07", "2026-09-08"], now=NOW)
    assert n2 == 1
    assert acc.state["settled_dates"] == ["2026-09-08"]
    assert acc.state["nav_history"][-1] == {"date": "2026-09-08",
                                            "nav": 610000.0}
