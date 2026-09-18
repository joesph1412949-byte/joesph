# -*- coding: utf-8 -*-
"""做T中枢 ref 的口径: 盘前 tick 的 lastClose 落后整整一个交易日。

盘前(09:15 前)QMT 的 tick 快照还是上一交易日盘中那一份, 其 lastClose 指向更早
一天 → 引擎若直接拿它当 ref, 档位会整整错一个交易日, 且账本当轮就缓存 ref
→ 盘前启动守护 = 全天档位全错。

这里注入假 xtquant(离线, 绝不连 QMT), 走真实取数链路 get_full_tick /
get_market_data_ex → XtdataBackend → MarketFeed → TTEngine.symbol_context。
"""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from ttcore import market
from ttcore.engine import TTEngine
from ttcore.state import Ledger

CODE = "600900.SH"
# 真实现象时刻: 09-17 07:47 盘前跑 plan(), 长电 ref 被钉成 28.500(9/15 收盘);
# 9/16 收盘 28.46 才是当时该用的前收。
PRE_MARKET = datetime(2026, 9, 17, 7, 47, 0)


def _now():
    return PRE_MARKET


def _dates(end, n=30):
    d = datetime.strptime(end, "%Y%m%d")
    return [(d - timedelta(days=i)).strftime("%Y%m%d")
            for i in range(n - 1, -1, -1)]


def _bars(end, prev, last, n=30):
    """日K: 末根=(end, last), 倒数第二根=prev, 前面小幅波动(保证 sigma>0)。"""
    head = [prev * (1 + 0.004 * ((i % 5) - 2)) for i in range(n - 2)]
    return list(zip(_dates(end, n), head + [prev, last]))


def _tick(session="20260916", last=28.46, last_close=28.50, open_=28.45,
          high=28.47, low=28.40, hhmm="15:00"):
    """假 tick: 字段名照抄 xtdata.get_full_tick 实测形状(time=毫秒时间戳)。"""
    t = datetime.strptime(session + hhmm, "%Y%m%d%H:%M")
    return {"time": int(t.timestamp() * 1000), "lastPrice": last,
            "open": open_, "high": high, "low": low, "lastClose": last_close,
            "volume": 1e6, "amount": 3e7}


class FakeXt:
    """假 xtquant 客户端(注入 XtdataBackend._xtdata, 不 import 真 xtquant)。"""

    def __init__(self, tick=None, bars=None, daily_raises=False,
                 daily_raw=None):
        self.tick = dict(tick or {})        # code -> 原始 tick dict
        self.bars = dict(bars or {})        # code -> [(date, close), ...]
        self.daily_raises = daily_raises
        self.daily_raw = daily_raw          # 直接当 get_market_data_ex 返回值

    def get_full_tick(self, codes):
        return {c: self.tick[c] for c in codes if c in self.tick}

    def get_market_data_ex(self, field_list, stock_list, period="1d", count=-1,
                           **kw):
        if self.daily_raises:
            raise RuntimeError("无法连接xtquant服务")
        if self.daily_raw is not None:
            return self.daily_raw
        out = {}
        for c in stock_list:
            rows = self.bars.get(c)
            if not rows:
                continue
            rows = rows[-int(count):] if count and int(count) > 0 else rows
            out[c] = pd.DataFrame({"close": [r[1] for r in rows]},
                                  index=[r[0] for r in rows])
        return out

    def download_history_data(self, *a, **kw):
        return None


def _feed(xt):
    be = market.XtdataBackend(download_missing=False)
    be._xtdata = xt                      # 绕开 xtquant 懒加载, 不碰真客户端
    return market.MarketFeed(backend=be, fallback=None, now_fn=_now)


def _engine(cfg, tmp_path, xt):
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=_now)
    led.load()
    eng = TTEngine(cfg, led, feed=_feed(xt), now_fn=_now, force_paper=True)
    return eng, led


def _ctx(eng, cfg):
    """盘前跑单标的上下文(与真实现象同一时刻)。"""
    return eng.plan_symbol(cfg["symbols"][0], eng.account_state(),
                           "07:47", "CLOSED")[0]


# ------------------------------------------------------------ 现象复现

def test_premarket_ref_uses_last_completed_daily_bar(cfg, tmp_path):
    """① 盘前: tick 停在 9/16 盘中(lastClose=28.50=9/15 收盘), 日K末根=9/16 收
    28.46 → ref 必须是 28.46(修复前取 28.50, 整整错一个交易日)。"""
    xt = FakeXt(tick={CODE: _tick()},
                bars={CODE: _bars("20260916", 28.50, 28.46)})
    eng, led = _engine(cfg, tmp_path, xt)

    ctx = _ctx(eng, cfg)

    assert ctx["ref"] == pytest.approx(28.46)
    assert led.get_ref(CODE) == pytest.approx(28.46)   # 缓存进账本的也得对
    assert ctx["ref_src"] == "daily"


def test_plan_premarket_ref_is_the_daily_bar_close(cfg, tmp_path):
    """①' 现象复现入口: 盘前 07:47 跑 TTEngine.plan()(MEMORY 记录的那一轮)。"""
    xt = FakeXt(tick={CODE: _tick()},
                bars={CODE: _bars("20260916", 28.50, 28.46)})
    eng, led = _engine(cfg, tmp_path, xt)

    out = eng.plan()

    assert out["ok"] is True
    assert out["hhmm"] == "07:47"
    assert out["symbols"][0]["ref"] == pytest.approx(28.46)
    assert led.get_ref(CODE) == pytest.approx(28.46)


def test_ref_trusts_tick_when_tick_is_newer_than_daily(cfg, tmp_path):
    """② tick 已刷新到当日(9/17)、日K末根还停在 9/15 → 采信 tick 的前收 28.46
    (防"一刀切不用 tick")。"""
    xt = FakeXt(tick={CODE: _tick(session="20260917", last=28.47,
                                  last_close=28.46)},
                bars={CODE: _bars("20260915", 28.51, 28.50)})
    eng, _ = _engine(cfg, tmp_path, xt)

    ctx = _ctx(eng, cfg)

    assert ctx["ref"] == pytest.approx(28.46)
    assert ctx["ref_src"] == "tick_newer"


def test_ref_falls_back_to_tick_when_daily_unavailable(cfg, tmp_path):
    """③ 日K拿不到(QMT 离线/无数据) → 回落 tick.lastClose(旧行为), 不抛异常,
    且来源标明是回落。"""
    xt = FakeXt(tick={CODE: _tick()}, daily_raises=True)
    eng, led = _engine(cfg, tmp_path, xt)

    ctx = _ctx(eng, cfg)               # 不应抛

    assert ctx["ref"] == pytest.approx(28.50)
    assert ctx["ref_src"] == "tick_fallback"
    assert led.get_ref(CODE) == pytest.approx(28.50)


def test_ref_stays_fixed_after_ledger_caches_it(cfg, tmp_path):
    """④ 当日首轮定下的 ref 进账本后不再变(既有语义): 09:15 后 tick 刷新也不改。"""
    xt = FakeXt(tick={CODE: _tick()},
                bars={CODE: _bars("20260916", 28.50, 28.46)})
    eng, led = _engine(cfg, tmp_path, xt)
    assert _ctx(eng, cfg)["ref"] == pytest.approx(28.46)

    xt.tick[CODE] = _tick(session="20260917", last=28.80, last_close=28.40)
    ctx2 = _ctx(eng, cfg)

    assert ctx2["ref"] == pytest.approx(28.46)      # 仍是首轮定下的中枢
    assert ctx2["ref_src"] == "ledger"
    assert led.get_ref(CODE) == pytest.approx(28.46)


# ------------------------------------------------------------ 口径表

@pytest.mark.parametrize("tick,daily,fallback,want", [
    # 盘前: tick 与日K末根同一交易日 → 日K为准(末根 28.46 才是已完成那根)
    ({"last_close": 28.50, "date": "20260916"},
     {"close": 28.46, "date": "20260916"}, 0.0, (28.46, "daily")),
    # tick 更新到日K之后 → 采信 tick
    ({"last_close": 28.46, "date": "20260917"},
     {"close": 28.50, "date": "20260915"}, 0.0, (28.46, "tick_newer")),
    # tick 没有交易日(无法判定新旧) → 日K为准
    ({"last_close": 28.50}, {"close": 28.46, "date": "20260916"}, 0.0,
     (28.46, "daily")),
    # 日K缺失 → 回落 tick
    ({"last_close": 28.50, "date": "20260916"}, None, 28.46,
     (28.50, "tick_fallback")),
    ({"last_close": 28.50}, None, None, (28.50, "tick_fallback")),
    # tick 连前收都没有 → 用上层给的收盘序列末值
    (None, None, 28.09, (28.09, "tick_fallback")),
    # 什么都没有 → 空(上层 fail-closed)
    (None, None, None, (None, "")),
])
def test_prev_close_table(tick, daily, fallback, want):
    assert market.prev_close(tick, daily, fallback) == want


# ------------------------------------------------------------ 取数层

def test_ticks_carry_session_date_and_daily_last_reads_last_bar():
    be = market.XtdataBackend(download_missing=False)
    be._xtdata = FakeXt(tick={CODE: _tick()},
                        bars={CODE: _bars("20260916", 28.50, 28.46)})

    t = be.ticks([CODE])[CODE]

    assert t["date"] == "20260916"          # time(毫秒) → YYYYMMDD
    assert t["last_close"] == pytest.approx(28.50)
    assert be.daily_last(CODE) == {"date": "20260916", "close": 28.46}


def test_tick_without_time_has_no_date():
    raw = _tick()
    raw.pop("time")
    be = market.XtdataBackend(download_missing=False)
    be._xtdata = FakeXt(tick={CODE: raw})

    assert be.ticks([CODE])[CODE]["date"] is None


@pytest.mark.parametrize("raw", [
    None,                                    # 无数据
    {},                                      # 无该 code
    "garbage",                               # 非 DataFrame
    pd.DataFrame({"close": []}, index=[]),    # 空表
    pd.DataFrame({"close": [float("nan")]}, index=["20260916"]),   # NaN
    pd.DataFrame({"close": [0.0]}, index=["20260916"]),            # 0 价
    pd.DataFrame({"amount": [1.0]}, index=["20260916"]),           # 缺 close 列
])
def test_daily_last_fails_safe(raw):
    """日K 拿不到/是垃圾 → None(交给上层回落 tick), 绝不抛。"""
    be = market.XtdataBackend(download_missing=False)
    be._xtdata = FakeXt(daily_raw={} if raw is None else {CODE: raw})

    assert be.daily_last(CODE) is None
