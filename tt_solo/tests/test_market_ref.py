# -*- coding: utf-8 -*-
"""做T中枢 ref 的口径: 以**今天**为参照系取"最后一根已完成日K"的前收。

第一版启发式("tick 交易日新于日K末根就采信 tick")在真实 QMT 下被推翻: 实测
2026-09-19(周六)13:32, `tick.time/timetag` 是**当前墙钟**(每轮刷新都重打), 而
`tick.lastClose` 仍是陈旧值(600900 给 28.46 = 09-17 收盘; 当日正确前收是 09-18
的 28.27) → 该启发式几乎恒判 "tick 更新", 于是又取回陈旧值, ref 仍错一天。

新口径(不信 tick 时间戳):
  日K末根 == 今天  → 前收 = 上一根 ('daily_prev')   # 盘中/盘后今日K线已在
  否则            → 前收 = 末根   ('daily')         # 盘前/周末/节假日
  无日K           → 回落 tick.lastClose ('tick_fallback')
日K里 close<=0/NaN 的占位行先丢弃再判断。

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
# 真实实测时刻(QMT 在线): 09-19 周六 13:32, 今日无K线, 日K末根=09-18 收 28.27,
# tick.time/timetag = 当前墙钟, tick.lastClose=28.46(09-17, 陈旧)。
SAT = datetime(2026, 9, 19, 13, 32, 0)
INTRADAY = datetime(2026, 9, 18, 10, 30, 0)     # 09-18 盘中(今日K线已在)
PRE_0918 = datetime(2026, 9, 18, 9, 0, 0)       # 09-18 盘前(今日K线还没出现)
PRE_0917 = datetime(2026, 9, 17, 7, 47, 0)      # MEMORY 记录的原始 bug 时刻


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
    """假 tick: 字段名照抄实测形状(time 毫秒 / timetag 字符串 / lastClose)。

    QMT 每轮都会把 time/timetag 重打成**当前墙钟**, 而 lastClose 可能仍旧
    —— 所以 time/timetag 在这里刻意填成"今天", 用来钉住"不许采信 tick 时间戳"。
    """
    t = datetime.strptime(session + hhmm, "%Y%m%d%H:%M")
    return {"time": int(t.timestamp() * 1000), "timetag": "%s %s:00" % (
        t.strftime("%Y%m%d"), hhmm), "lastPrice": last, "open": open_,
        "high": high, "low": low, "lastClose": last_close,
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


def _feed(xt, now):
    be = market.XtdataBackend(download_missing=False)
    be._xtdata = xt                      # 绕开 xtquant 懒加载, 不碰真客户端
    return market.MarketFeed(backend=be, fallback=None, now_fn=lambda: now)


def _engine(cfg, tmp_path, xt, now):
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=lambda: now)
    led.load()
    eng = TTEngine(cfg, led, feed=_feed(xt, now), now_fn=lambda: now,
                   force_paper=True)
    return eng, led


def _ctx(eng, cfg):
    return eng.plan_symbol(cfg["symbols"][0], eng.account_state(),
                           "07:47", "CLOSED")[0]


# ------------------------------------------------------------ 六个场景

def test_saturday_ref_is_last_completed_bar_not_stale_tick(cfg, tmp_path):
    """① 真实环境判据(09-19 周六 13:32): 今日无K线, 日K末根=09-18 收 28.27;
    tick 的 time/timetag 是当前墙钟而 lastClose 陈旧(28.46=09-17)
    → ref 必须 28.27/daily(旧启发式判 "tick_newer" 取 28.46, 又错一天)。"""
    xt = FakeXt(tick={CODE: _tick(session="20260919", hhmm="13:32", last=31.07,
                                  last_close=28.46, open_=28.27, high=31.10,
                                  low=28.20)},
                bars={CODE: _bars("20260918", 28.46, 28.27)})
    eng, led = _engine(cfg, tmp_path, xt, SAT)

    ctx = _ctx(eng, cfg)

    assert ctx["ref"] == pytest.approx(28.27)
    assert ctx["ref_src"] == "daily"
    assert led.get_ref(CODE) == pytest.approx(28.27)


def test_intraday_uses_previous_bar_when_today_bar_exists(cfg, tmp_path):
    """② 09-18 盘中: 今日K线已在(收盘价还在动) → 前收 = 09-17 收盘 28.46。"""
    xt = FakeXt(tick={CODE: _tick(session="20260918", hhmm="10:30", last=28.30,
                                  last_close=28.46, open_=28.40)},
                bars={CODE: _bars("20260918", 28.46, 28.30)})
    eng, led = _engine(cfg, tmp_path, xt, INTRADAY)

    ctx = _ctx(eng, cfg)

    assert ctx["ref"] == pytest.approx(28.46)
    assert ctx["ref_src"] == "daily_prev"
    assert led.get_ref(CODE) == pytest.approx(28.46)


def test_premarket_ref_equals_that_days_intraday_ref(cfg, tmp_path):
    """③ 09-18 盘前: 今日K线还没出现, 末根=09-17=28.46 → 与盘中同值(当日固定)。"""
    xt = FakeXt(tick={CODE: _tick(session="20260917", last=28.46,
                                  last_close=28.50)},
                bars={CODE: _bars("20260917", 28.50, 28.46)})
    eng, _ = _engine(cfg, tmp_path, xt, PRE_0918)

    ctx = _ctx(eng, cfg)

    assert ctx["ref"] == pytest.approx(28.46)
    assert ctx["ref_src"] == "daily"


def test_memory_case_premarket_0917_via_plan(cfg, tmp_path):
    """④ MEMORY 记录的原始 bug(09-17 盘前跑 plan()): 末根=09-16=28.46, 陈旧
    tick.lastClose=28.50 → ref 28.46(修复前 28.50, 整整错一个交易日)。"""
    xt = FakeXt(tick={CODE: _tick(session="20260916", last=28.46,
                                  last_close=28.50)},
                bars={CODE: _bars("20260916", 28.50, 28.46)})
    eng, led = _engine(cfg, tmp_path, xt, PRE_0917)

    out = eng.plan()

    assert out["ok"] is True
    assert out["hhmm"] == "07:47"
    assert out["symbols"][0]["ref"] == pytest.approx(28.46)
    assert out["symbols"][0]["ref_src"] == "daily"
    assert led.get_ref(CODE) == pytest.approx(28.46)


@pytest.mark.parametrize("bad", [0.0, float("nan")])
def test_today_placeholder_row_is_dropped(cfg, tmp_path, bad):
    """⑤ 今日行是占位(close=0/NaN) → 丢弃后再判断: 末根回到 09-18=28.27/daily。"""
    bars = _bars("20260918", 28.46, 28.27) + [("20260919", bad)]
    xt = FakeXt(tick={CODE: _tick(session="20260919", last=31.07,
                                  last_close=28.46)},
                bars={CODE: bars})
    eng, _ = _engine(cfg, tmp_path, xt, SAT)

    ctx = _ctx(eng, cfg)

    assert ctx["ref"] == pytest.approx(28.27)
    assert ctx["ref_src"] == "daily"


def test_no_daily_falls_back_to_tick(cfg, tmp_path):
    """⑥ 无日K(QMT 离线/无数据) → 回落 tick.lastClose, 不抛, 来源标明回落。"""
    xt = FakeXt(tick={CODE: _tick()}, daily_raises=True)
    eng, led = _engine(cfg, tmp_path, xt, SAT)

    ctx = _ctx(eng, cfg)               # 不应抛

    assert ctx["ref"] == pytest.approx(28.50)
    assert ctx["ref_src"] == "tick_fallback"
    assert led.get_ref(CODE) == pytest.approx(28.50)


def test_ref_stays_fixed_after_ledger_caches_it(cfg, tmp_path):
    """⑦ 当日首轮定下的 ref 进账本后不再变(既有语义): 盘中 tick 刷新也不改。"""
    xt = FakeXt(tick={CODE: _tick(session="20260918", hhmm="10:30", last=28.30,
                                  last_close=28.46)},
                bars={CODE: _bars("20260918", 28.46, 28.30)})
    eng, led = _engine(cfg, tmp_path, xt, INTRADAY)
    assert _ctx(eng, cfg)["ref"] == pytest.approx(28.46)

    xt.tick[CODE] = _tick(session="20260918", hhmm="14:50", last=28.90,
                          last_close=28.46)
    xt.bars[CODE] = _bars("20260918", 28.46, 28.90)      # 今日K线收盘价已变
    ctx2 = _ctx(eng, cfg)

    assert ctx2["ref"] == pytest.approx(28.46)      # 仍是首轮定下的中枢
    assert ctx2["ref_src"] == "ledger"
    assert led.get_ref(CODE) == pytest.approx(28.46)


# ------------------------------------------------------------ 口径表

@pytest.mark.parametrize("tick,daily,dprev,today,fallback,want", [
    # 周末/盘前: 末根非今日 → 末根即前收
    ({"last_close": 28.46}, {"date": "20260918", "close": 28.27},
     {"date": "20260917", "close": 28.46}, "20260919", None,
     (28.27, "daily")),
    # 盘中: 末根 == 今日 → 取上一根
    ({"last_close": 28.46}, {"date": "20260918", "close": 28.30},
     {"date": "20260917", "close": 28.46}, "20260918", None,
     (28.46, "daily_prev")),
    # 只有今日一根(上一根已被丢弃/无更早历史) → 回落 tick
    ({"last_close": 28.46}, {"date": "20260918", "close": 28.30}, None,
     "20260918", None, (28.46, "tick_fallback")),
    # today 未知(无时钟) → 按"末根非今日"处理
    ({"last_close": 28.46}, {"date": "20260918", "close": 28.27}, None, None,
     None, (28.27, "daily")),
    # 无日K → 回落 tick
    ({"last_close": 28.50}, None, None, "20260919", None,
     (28.50, "tick_fallback")),
    # 日K close=0(占位行没被上层丢掉) → 同样回落
    ({"last_close": 28.50}, {"date": "20260919", "close": 0.0}, None,
     "20260919", None, (28.50, "tick_fallback")),
    # tick 连前收都没有 → 用上层给的收盘序列末值
    (None, None, None, "20260919", 28.09, (28.09, "tick_fallback")),
    # 什么都没有 → 空(上层 fail-closed)
    (None, None, None, None, None, (None, "")),
])
def test_prev_close_table(tick, daily, dprev, today, fallback, want):
    assert market.prev_close(tick, daily, dprev, today, fallback) == want


# ------------------------------------------------------------ 取数层

def test_daily_bars_reads_last_two_and_drops_placeholders():
    be = market.XtdataBackend(download_missing=False)
    be._xtdata = FakeXt(bars={CODE: _bars("20260918", 28.46, 28.27)
                              + [("20260919", 0.0)]})

    assert be.daily_bars(CODE) == [{"date": "20260917", "close": 28.46},
                                   {"date": "20260918", "close": 28.27}]


@pytest.mark.parametrize("raw", [
    None,                                    # 无数据
    {},                                      # 无该 code
    "garbage",                               # 非 DataFrame
    pd.DataFrame({"close": []}, index=[]),    # 空表
    pd.DataFrame({"close": [float("nan")]}, index=["20260916"]),   # NaN
    pd.DataFrame({"close": [0.0]}, index=["20260916"]),            # 0 价
    pd.DataFrame({"amount": [1.0]}, index=["20260916"]),           # 缺 close 列
])
def test_daily_bars_fails_safe(raw):
    """日K 拿不到/是垃圾 → [](交给上层回落 tick), 绝不抛。"""
    be = market.XtdataBackend(download_missing=False)
    be._xtdata = FakeXt(daily_raw={} if raw is None else {CODE: raw})

    assert be.daily_bars(CODE) == []
