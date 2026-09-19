# -*- coding: utf-8 -*-
"""策略编排端到端测试(注入假行情/假账户, 不打网络)。"""
import logging

import pytest

from ttcore import config as tt_config
from ttcore import grid
from ttcore import risk as tt_risk
from ttcore.engine import TTEngine

OPEN = "10:00"


@pytest.fixture
def sym():
    return {"code": "600900.SH", "name": "长江电力", "enabled": True,
            "weight": 0.18, "band_pct": 0.53, "n_units": 5,
            "switch": {"dev_max_pct": 4.0, "slope_max_pct": 0.3,
                       "r20_max_pct": 8.0}}


@pytest.fixture
def eng_factory(cfg, ledger, now_fn, fake_feed):
    """返回 (engine, feed) 工厂 —— 行情由测试自己填。"""
    def _make(snaps=None):
        feed = fake_feed()
        for code, snap in (snaps or {}).items():
            feed.set(code, snap)
        return TTEngine(cfg, ledger, feed=feed, now_fn=now_fn,
                        force_paper=True), feed
    return _make


# ------------------------------------------------------------ 基本决策

def test_sell_intent_generated_on_high(eng_factory, ledger, sym, snap_factory):
    """最高价穿越卖档 → 生成卖出意图, 价格等于对应档位。"""
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=28.57, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    ctx, intents = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")

    assert ctx["ref"] == 28.09                    # 用前收做中枢
    assert ctx["band"] == pytest.approx(0.0053)
    assert ctx["switch"] == "ENABLED"
    sells = [i for i in intents if i.side == "SELL"]
    assert len(sells) == 2                        # 单轮上限 2
    assert sells[0].price == pytest.approx(28.09 * 1.0053, abs=1e-3)
    assert sells[1].price == pytest.approx(28.09 * 1.0106, abs=1e-3)
    assert all(i.ok for i in sells)
    assert all(i.volume % 100 == 0 and i.volume > 0 for i in sells)


def test_no_intent_when_price_inside_band(eng_factory, ledger, sym,
                                          snap_factory):
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.10, last_close=28.09, high=28.10, low=28.08,
        ma20=28.19, ma20_prev=28.19)})
    _, intents = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")
    assert intents == []


def test_buy_intent_on_low_then_blocked_by_exposure(eng_factory, ledger, sym,
                                                    snap_factory):
    """先跌触发买档, 但日内净敞口=0 → 被风控拦下(不会变成净加仓)。"""
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=27.90, last_close=28.09, high=28.10, low=27.80,
        ma20=28.19, ma20_prev=28.19)})
    _, intents = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")
    buys = [i for i in intents if i.side == "BUY"]
    assert buys, "应产生买入意图(交由风控裁决)"
    assert all(not i.ok and i.reject_code == "NET_EXPOSURE" for i in buys)


def test_switch_disabled_skips_symbol(eng_factory, ledger, sym, snap_factory):
    """趋势加速段 → DISABLED, 不产生任何意图。"""
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=197.5, last_close=188.3, high=999, low=1,
        ma20=188.3, ma20_prev=183.3, r20_pct=10.18)})
    ctx, intents = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")
    assert ctx["switch"] == "DISABLED"
    assert intents == []


def test_half_state_halves_units(eng_factory, ledger, sym, snap_factory):
    """HALF → 只做前一半档位(深度先被 max_units 截到 3, 半量后 2 档)。"""
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=30.0, last_close=29.5, high=30.0, low=29.5,
        ma20=29.0, ma20_prev=28.5, r20_pct=3.0)})
    ctx, _ = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")
    assert ctx["switch"] == "HALF"
    assert ctx["n_eff_units"] == 2


# ------------------------------------------------------------ T+1 / 底仓

def test_sell_blocked_without_base_position(eng_factory, ledger, sym,
                                            snap_factory):
    """账户里没有该票底仓 → 卖出被拒(fail-closed)。"""
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=28.57, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    acct = eng.account_state()
    acct["can_use"] = {}                 # 清掉纸面底仓
    acct["positions"] = {}
    _, intents = eng.plan_symbol(sym, acct, OPEN, "OPEN")
    sells = [i for i in intents if i.side == "SELL"]
    assert sells and all(not i.ok for i in sells)
    assert all(i.reject_code in ("NO_BASE_POSITION", "SELLABLE_INSUFFICIENT")
               for i in sells)


def test_sell_volume_clipped_to_available(eng_factory, ledger, sym,
                                          snap_factory):
    """可卖量小于单档数量 → 自动缩量到可卖量(仍需整手)。"""
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=28.57, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    acct = eng.account_state()
    acct["can_use"] = {"600900.SH": 500}
    _, intents = eng.plan_symbol(sym, acct, OPEN, "OPEN")
    sells = [i for i in intents if i.side == "SELL"]
    assert sells and all(i.volume <= 500 for i in sells)


def test_session_closed_blocks_all(eng_factory, ledger, sym, snap_factory):
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=28.57, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    _, intents = eng.plan_symbol(sym, eng.account_state(), "20:00", "CLOSED")
    assert intents and all(not i.ok and i.reject_code == "SESSION_CLOSED"
                           for i in intents)


# ------------------------------------------------------------ 同轮累计(C2)

def test_round_accumulates_sell_volume(eng_factory, ledger, sym, snap_factory):
    """同一轮多笔卖出必须累计已卖量: 本轮 ok 卖出总量 ≤ 券商可卖量。

    旧实现: `sold_today` 在循环外只读一次、循环内不累加 → can_use=600 时
    一轮生成 2 笔 SELL×600 全部 ok(=1200 > 可卖 600), 直接违反 T+1 可卖量
    硬约束与 README §5 的"按当日已卖递减, 避免同一轮重复卖同一批底仓"。
    """
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=28.57, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    acct = eng.account_state()
    acct["can_use"] = {"600900.SH": 600}          # 只够一档
    _, intents = eng.plan_symbol(sym, acct, OPEN, "OPEN")
    sells = [i for i in intents if i.side == "SELL"]
    assert [i for i in sells if i.ok], "至少第一档应能卖出"
    ok_qty = sum(i.volume for i in sells if i.ok)
    assert ok_qty <= 600, "本轮 ok 卖出 %d 股 > 可卖 600 股" % ok_qty


def test_round_accumulates_buy_volume(eng_factory, ledger, sym, snap_factory):
    """同一轮多笔买入必须累计: bought - sold ≤ max_net_buy_qty(严格归位=0)。

    旧实现可在一轮内报出 2 笔 BUY×600, 日内净持仓从 -600 变成 +600。
    """
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=27.60, last_close=28.09, high=28.10, low=27.50,
        ma20=28.19, ma20_prev=28.19)})
    acct = eng.account_state()
    ledger.record_fill("600900.SH", "SELL", 28.9, 600, hhmm="10:00")
    _, intents = eng.plan_symbol(sym, acct, OPEN, "OPEN")
    buys = [i for i in intents if i.side == "BUY"]
    assert [i for i in buys if i.ok], "有卖出额度后第一档应能买回"
    net = sum(i.volume for i in buys if i.ok) - 600
    assert net <= 0, "本轮买入后日内净持仓 %+d 股 > 0" % net


# ------------------------------------------------------------ 水位/幂等

def test_units_advance_prevents_duplicate(eng_factory, ledger, sym,
                                          snap_factory):
    """推进档位水位后, 同一行情不再重复产出同一档意图。"""
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=28.57, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    acct = eng.account_state()
    _, first = eng.plan_symbol(sym, acct, OPEN, "OPEN")
    assert len([i for i in first if i.side == "SELL"]) == 2

    ledger.set_units("600900.SH", "SELL", 2)
    _, second = eng.plan_symbol(sym, acct, OPEN, "OPEN")
    assert len([i for i in second if i.side == "SELL"]) == 1   # 只剩第 3 档

    ledger.set_units("600900.SH", "SELL", 3)
    _, third = eng.plan_symbol(sym, acct, OPEN, "OPEN")
    assert [i for i in third if i.side == "SELL"] == []


def test_order_id_is_deterministic(eng_factory, ledger, sym, snap_factory):
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=28.57, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    acct = eng.account_state()
    _, a = eng.plan_symbol(sym, acct, OPEN, "OPEN")
    _, b = eng.plan_symbol(sym, acct, OPEN, "OPEN")
    assert [i.order_id for i in a] == [i.order_id for i in b]
    assert a[0].order_id == "TT_20260914_600900SH_SELL_1"


def test_ref_stays_fixed_intraday(eng_factory, ledger, sym, snap_factory):
    """中枢当日固定: 价格变化不改变已定的 ref。"""
    ledger.load()
    eng, feed = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=28.45, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    acct = eng.account_state()
    eng.plan_symbol(sym, acct, OPEN, "OPEN")
    assert ledger.get_ref("600900.SH") == 28.09

    feed.set("600900.SH", snap_factory(
        last=29.50, last_close=29.40, high=29.50, low=29.40,
        ma20=28.19, ma20_prev=28.19))
    ctx, _ = eng.plan_symbol(sym, acct, OPEN, "OPEN")
    assert ctx["ref"] == 28.09          # 仍是当日首次定的中枢


# ------------------------------------------------------------ plan 全量

def test_plan_full_shape(eng_factory, ledger, snap_factory):
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=28.57, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    out = eng.plan()
    assert out["ok"] is True
    assert out["hhmm"] == "10:00"
    assert out["phase"] == "OPEN"
    assert out["counts"]["symbols"] == 1
    assert out["account"]["source"] == "paper"
    assert isinstance(out["signals"], list)
    for s in out["signals"]:
        for k in ("order_id", "action", "stock_code", "price", "volume"):
            assert k in s
        assert s["action"] in ("BUY", "SELL")
        assert s["volume"] % 100 == 0


def test_plan_survives_symbol_exception(cfg, ledger, now_fn):
    """单标的抛异常不拖垮整轮。"""
    ledger.load()

    class BoomFeed:
        last_source = "boom"

        def snapshot(self, code, count=80, sigma_window=60):
            raise RuntimeError("boom")

        def closes(self, code, count=80):
            return None

        def ticks(self, codes):
            return {}

    eng = TTEngine(cfg, ledger, feed=BoomFeed(), now_fn=now_fn,
                   force_paper=True)
    out = eng.plan()
    assert out["ok"] is True
    assert out["counts"]["symbols"] == 1
    assert "异常" in out["symbols"][0]["skip"]


def test_plan_handles_missing_quote(cfg, ledger, now_fn, fake_feed):
    ledger.load()
    eng = TTEngine(cfg, ledger, feed=fake_feed(), now_fn=now_fn,
                   force_paper=True)
    out = eng.plan()
    assert out["counts"]["intents"] == 0
    assert out["symbols"][0]["skip"] == "行情缺失"


# ------------------------------------------------------------ 配置

def test_config_rejects_bad_band_k():
    with pytest.raises(tt_config.ConfigError):
        tt_config.load(overrides={"grid": {"band_k": 99}})


def test_config_rejects_unsuffixed_code():
    with pytest.raises(tt_config.ConfigError):
        tt_config.load(overrides={"symbols": [{"code": "600900"}]})


def test_config_rejects_duplicate_code():
    with pytest.raises(tt_config.ConfigError):
        tt_config.load(overrides={"symbols": [
            {"code": "600900.SH"}, {"code": "600900.SH"}]})


def test_config_rejects_bad_session():
    with pytest.raises(tt_config.ConfigError):
        tt_config.load(overrides={"session": {"open_start": "25:99"}})


def test_config_rejects_risk_out_of_bounds():
    with pytest.raises(tt_config.ConfigError):
        tt_config.load(overrides={"risk": {"max_daily_trades": 99999}})


def test_config_normalizes_time():
    cfg = tt_config.load(overrides={"session": {"open_start": "9:30"}})
    assert cfg["session"]["open_start"] == "09:30"


def test_config_fixed_mode_requires_band():
    with pytest.raises(tt_config.ConfigError):
        tt_config.load(overrides={
            "grid": {"band_mode": "fixed"},
            "symbols": [{"code": "600900.SH", "band_pct": 0}]})


def test_config_enabled_symbols_helper():
    cfg = tt_config.load(overrides={"symbols": [
        {"code": "600900.SH", "enabled": True},
        {"code": "603268.SH", "enabled": False}]})
    names = [s["code"] for s in tt_config.enabled_symbols(cfg)]
    assert names == ["600900.SH"]


# ------------------------------------------------------------ max_units (档数解耦)

def test_max_units_caps_depth(eng_factory, ledger, sym, snap_factory):
    """max_units=2 时, 即使价格穿越 5 档也只做 2 档。"""
    ledger.load()
    # 把行情推到远超第5档
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=29.50, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    eng.grid_cfg["max_units"] = 2
    ctx, intents = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")
    assert ctx["n_eff_units"] == 2
    sells = [i for i in intents if i.side == "SELL"]
    assert len(sells) == 2                      # 单轮上限 2 且深度上限 2


def test_production_depth_cap(eng_factory, ledger, sym, snap_factory):
    """生产配置(n_units=5, max_units=3): 穿越第 5 档, 深度仍封顶在 3。"""
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=29.50, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    # 先把前提钉住: 否则夹具改成 n_units=max_units 时本测试会假通过
    assert (eng.grid_cfg["n_units"], eng.grid_cfg["max_units"]) == (5, 3)

    ctx, intents = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")
    assert ctx["high"] >= ctx["ladder"]["sell"][4]   # 最高价已越过第 5 档
    assert ctx["n_eff_units"] == 3                   # 深度由 max_units 截断
    assert ctx["target_sell_units"] == 3             # 阶梯穿 5 档, 目标只 3 档
    sells = [i for i in intents if i.side == "SELL"]
    assert len(sells) == 2                           # 单轮上限 2 是另一道闸门
    assert all(i.meta["target_units"] == 3 for i in sells)


def test_max_units_does_not_change_unit_size(eng_factory, ledger, sym,
                                             snap_factory):
    """max_units 只限制档数, 不改变单档股数(n_units 仍决定分母)。"""
    ledger.load()
    eng, _ = eng_factory({"600900.SH": snap_factory(
        last=28.45, last_close=28.09, high=28.57, low=28.10,
        ma20=28.19, ma20_prev=28.19)})
    a = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")[1]
    vol_a = [i for i in a if i.side == "SELL"][0].volume

    eng.grid_cfg["max_units"] = 2           # 必须与夹具值不同, 否则两次调用同参
    b = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")[1]
    vol_b = [i for i in b if i.side == "SELL"][0].volume
    assert vol_a == vol_b


def test_default_max_units_equals_n_units():
    """配置未设 max_units 时, 应兜底等于 n_units。"""
    import copy
    raw = copy.deepcopy(tt_config.DEFAULT_CONFIG)
    raw["grid"] = {"band_mode": "sigma", "band_k": 1.0, "n_units": 4,
                   "ref_mode": "prev_close", "sigma_window": 60}
    raw.pop("paper_positions", None)
    raw["symbols"] = [{"code": "600900.SH", "name": "X", "enabled": True,
                       "weight": 0.1, "band_pct": 0.5, "n_units": 4}]
    # 直接调 validate, 不走 load() 的深合并 —— 后者会把盘上 tt_config.json
    # 的 max_units=3 并进来, 与"未设 max_units"的前提冲突。
    assert "max_units" not in raw["grid"]
    c = tt_config.validate(raw)
    assert c["grid"]["max_units"] == 4


def test_max_units_above_n_units_rejected():
    """max_units > n_units 属配置错误, 应报错。"""
    with pytest.raises(tt_config.ConfigError):
        tt_config.load(overrides={
            "grid": {"band_mode": "sigma", "band_k": 1.0, "n_units": 3,
                     "max_units": 5, "ref_mode": "prev_close",
                     "sigma_window": 60},
            "symbols": [{"code": "600900.SH", "name": "X", "enabled": True,
                         "weight": 0.1, "band_pct": 0.5, "n_units": 3}],
        })


def test_max_units_zero_rejected():
    with pytest.raises(tt_config.ConfigError):
        tt_config.load(overrides={
            "grid": {"band_mode": "sigma", "band_k": 1.0, "n_units": 3,
                     "max_units": 0, "ref_mode": "prev_close",
                     "sigma_window": 60},
            "symbols": [{"code": "600900.SH", "name": "X", "enabled": True,
                         "weight": 0.1, "band_pct": 0.5, "n_units": 3}],
        })


# ------------------------------------------------------------ 涨跌停闸门中枢(Minor 1)

def test_limit_close_uses_corrected_prev_close(eng_factory, ledger, sym,
                                               snap_factory, monkeypatch):
    """涨跌停闸门的中枢必须是修正后的前收, 不是陈旧的 tick.last_close。

    真机实测(2026-09-19 周六): 600900 的 tick.lastClose=28.46(09-17 收盘),
    正确前收是 09-18 的 28.27 —— 相差一天会让整个涨跌停区间偏移。

    必须**探到闸门实际收到的值**: 光断言 ctx["limit_close"] 只测了算出来的
    字段, 测不到接线(把 limit_close 算对了却继续传 last_close 照样绿)。
    """
    ledger.load()
    snap = snap_factory(last=28.45, last_close=28.46, high=28.57, low=28.10,
                        ma20=28.19, ma20_prev=28.19)
    snap["daily"] = {"date": "20260911", "close": 28.09}    # 正确的最后一根日K
    eng, _ = eng_factory({"600900.SH": snap})

    seen = []
    real_check = tt_risk.RiskGate.check

    def spy(self, **kw):
        seen.append(kw.get("last_close"))
        return real_check(self, **kw)

    monkeypatch.setattr(tt_risk.RiskGate, "check", spy)
    ctx, intents = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")

    assert ctx["last_close"] == 28.46            # tick 原值仍留作展示
    assert ctx["ref"] == pytest.approx(28.09)
    assert ctx["limit_close"] == pytest.approx(28.09)
    assert intents, "本轮没有生成意图 → 探针没探到接线"
    assert seen, "闸门一次都没被调用 → 探针没探到接线"
    assert all(abs(v - 28.09) < 1e-9 for v in seen), \
        "闸门收到的涨跌停中枢仍是陈旧值: %r" % (seen,)

    # 后果具体化: 31.0 按陈旧昨收(28.46×1.10=31.31)算"合法", 按正确昨收
    # (28.09×1.10=30.899)算"超涨停" —— 旧接线会放它过去。
    assert tt_risk.check_limit_band(31.0, ctx["last_close"], sym["code"]).ok
    v = tt_risk.check_limit_band(31.0, ctx["limit_close"], sym["code"])
    assert not v.ok and v.code == "BAND_OUT"


def test_limit_close_in_open_ref_mode_still_prev_close(eng_factory, ledger,
                                                       sym, snap_factory,
                                                       monkeypatch):
    """ref_mode=open 时 ref 是今开(不能当昨收用) → 涨跌停中枢仍取前收。"""
    ledger.load()
    snap = snap_factory(last=28.45, last_close=28.46, high=28.57, low=28.10,
                        ma20=28.19, ma20_prev=28.19, open_=28.30)
    snap["daily"] = {"date": "20260911", "close": 28.09}
    eng, _ = eng_factory({"600900.SH": snap})
    eng.grid_cfg["ref_mode"] = "open"

    seen = []
    real_check = tt_risk.RiskGate.check

    def spy(self, **kw):
        seen.append(kw.get("last_close"))
        return real_check(self, **kw)

    monkeypatch.setattr(tt_risk.RiskGate, "check", spy)
    ctx, intents = eng.plan_symbol(sym, eng.account_state(), OPEN, "OPEN")

    assert ctx["ref"] == pytest.approx(28.30)           # 中枢 = 今开
    assert ctx["limit_close"] == pytest.approx(28.09)   # 涨跌停仍按前收
    assert intents and all(abs(v - 28.09) < 1e-9 for v in seen), seen


# ------------------------------------------------------------ band_mode 现状真相

def test_band_of_prefers_band_pct_regardless_of_band_mode():
    """`band_of` 的代码级判据: band_pct>0 就优先, **与 band_mode 无关**。

    部署配置写着 band_mode="sigma", 但每个标的都有 band_pct>0
    ⇒ band_k / sigma 从未生效过, 100% 走 band_pct。README §1 那句
    "band_mode: sigma 时带宽不取 band_pct" 与代码不符。
    """
    assert grid.band_of({"band_pct": 0.53}, sigma=0.05, band_k=3.0,
                        band_mode="sigma") == pytest.approx(0.0053)
    assert grid.band_of({"band_pct": 0.53}, sigma=0.05, band_k=3.0,
                        band_mode="fixed") == pytest.approx(0.0053)
    # 没有 band_pct 时 sigma 才生效
    assert grid.band_of({}, sigma=0.01, band_k=1.0,
                        band_mode="sigma") == pytest.approx(0.01)
    # sigma 拿不到且无 band_pct → None(该标的当日跳过)
    assert grid.band_of({}, sigma=None, band_k=1.0, band_mode="sigma") is None


def test_config_cross_check_covers_sigma_mode(caplog):
    """交叉校验要按 band_of 的**实际取值口径**生效, 不能被 band_mode 包着。

    旧实现包在 `if band_mode == "fixed"` 里 ⇒ sigma + band_pct=5.0、3 档
    = 15% > 5% 也校验通过(静默失效)。触发时给显眼 WARNING(不抛异常, 见下)。
    """
    with caplog.at_level(logging.WARNING, logger="tt_config"):
        tt_config.load(overrides={
            "grid": {"band_mode": "sigma", "band_k": 1.0, "n_units": 5,
                     "max_units": 3, "ref_mode": "prev_close",
                     "sigma_window": 60},
            "symbols": [{"code": "600900.SH", "name": "X", "enabled": True,
                         "weight": 0.1, "band_pct": 5.0, "n_units": 5}],
        })
    assert "600900.SH" in caplog.text
    assert "15.00%" in caplog.text


def test_config_cross_check_silent_when_compliant(caplog):
    with caplog.at_level(logging.WARNING, logger="tt_config"):
        tt_config.load(overrides={
            "grid": {"band_mode": "sigma", "band_k": 1.0, "n_units": 5,
                     "max_units": 3, "ref_mode": "prev_close",
                     "sigma_window": 60},
            "symbols": [{"code": "600900.SH", "name": "X", "enabled": True,
                         "weight": 0.1, "band_pct": 0.53, "n_units": 5}],
        })
    assert caplog.text == ""


def test_deployed_config_loads_and_flags_only_603268(caplog):
    """**部署配置必须能加载**(硬拦会让守护升级后起不来), 超限标的要点名。

    实测表(2026-09-19):
        600900.SH  min(5,3)×0.53% = 1.59%  ✓
        600938.SH  min(5,3)×1.65% = 4.95%  ✓
        601088.SH  min(5,3)×1.40% = 4.20%  ✓
        603268.SH  min(5,3)×3.41% = 10.23% ✗ (enabled=false)
    """
    with caplog.at_level(logging.WARNING, logger="tt_config"):
        cfg = tt_config.load()
    assert cfg["grid"]["band_mode"] == "sigma"
    assert "603268.SH" in caplog.text
    assert "10.23%" in caplog.text
    for other in ("600900.SH", "600938.SH", "601088.SH"):
        assert other not in caplog.text


def test_deployed_config_worst_deviation_table():
    """把上面那张表钉成断言, 免得配置改了这份记录悄悄失真。"""
    cfg = tt_config.load()
    mu = cfg["grid"]["max_units"]
    md = cfg["risk"]["max_price_deviation_pct"]
    worst = {s["code"]: min(s["n_units"], mu) * s["band_pct"] / 100.0
             for s in cfg["symbols"]}
    assert worst["603268.SH"] == pytest.approx(0.1023)
    assert worst["603268.SH"] > md
    assert max(v for k, v in worst.items() if k != "603268.SH") <= md
