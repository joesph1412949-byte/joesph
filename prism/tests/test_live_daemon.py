# -*- coding: utf-8 -*-
"""实盘信号守护测试(全离线, 不碰 QMT / 不落真实信号)。

依赖全部注入: provider / account / screen_fn / ticks_fn / now_fn,
状态与信号目录指向 tmp_path。
"""
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.common import next_weekday
from shared.exit_rules import ExitRule, PositionBook, is_limit_down
from prism.live_account import calc_buy_volume
from prism.live_daemon import LiveDaemon

MON = datetime(2026, 9, 14, 15, 6)      # 周一 收盘后(选股)
TUE_OPEN = datetime(2026, 9, 15, 9, 27)  # 周二 开盘买入窗口
TUE_AM = datetime(2026, 9, 15, 10, 0)    # 周二 盘中


# ---------------- 测试替身 ----------------

class _FakeProvider:
    """只需非 None(daemon 用 provider 存在性做闸门)。"""

    class _DS:
        def get_full_market_ticks(self, codes=None):
            return {}

    def __init__(self):
        self.ds = self._DS()


class _FakeAccount:
    def __init__(self, asset=None, positions=None):
        self._asset = asset
        self._pos = positions or {}

    def asset(self):
        return self._asset

    def positions(self):
        return {k: dict(v) for k, v in self._pos.items()}

    def total_asset(self):
        a = self._asset
        if not a:
            return None
        v = float(a.get("total_asset") or 0)
        return v if v > 0 else None

    def can_use_map(self):
        return {c: int(p.get("can_use_volume") or 0)
                for c, p in self._pos.items()}


def _account(total=1000000.0, positions=None):
    return _FakeAccount({"total_asset": total, "cash": total * 0.5,
                         "market_value": 0.0, "frozen_cash": 0.0},
                        positions or {})


def _strategy(tp=0.15, sl=0.05, hold=5):
    return {"id": "t_live", "sell_rules": {"take_profit_pct": tp,
                                           "stop_loss_pct": sl,
                                           "max_hold_days": hold}}


def _daemon(tmp_path, account=None, screen_fn=None, ticks_fn=None,
            dry_run=False, **kw):
    return LiveDaemon(
        provider=_FakeProvider(),
        account=account if account is not None else _account(),
        strategy=_strategy(),
        state_file=tmp_path / "live_state.json",
        positions_file=tmp_path / "positions.json",
        signal_root=tmp_path / "signals",
        screen_fn=screen_fn, ticks_fn=ticks_fn, dry_run=dry_run, **kw)


def _write_json(p, data):
    Path(p).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _read_json(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _pending(tmp_path):
    return list((tmp_path / "signals" / "real" / "pending").glob("*.json"))


def _screen_result(picks):
    """picks: [(code, name, price)] → run_daily 形状的假返回。"""
    sigs = [{"order_id": "x", "action": "BUY", "stock_code": c, "name": n,
             "price": p, "volume": 100, "strategy_id": "t_live",
             "composite": 1.0} for c, n, p in picks]
    return {"environment_ok": True, "candidates": sigs, "signals": sigs,
            "signals_written": 0, "paused": False, "skipped_no_price": 0}


# ---------------- 仓位计算 ----------------

def test_calc_buy_volume_by_net_asset():
    # 100 万 × 15% ÷ 10.55 = 14218.0 → 整手 14200
    assert calc_buy_volume(10.55, 1000000.0, 0.15) == 14200


def test_calc_buy_volume_invalid_inputs():
    assert calc_buy_volume(0, 1000000.0) == 0
    assert calc_buy_volume(10.0, None) == 0
    assert calc_buy_volume(10.0, 0) == 0
    assert calc_buy_volume(10.0, 1000000.0, 0) == 0
    # 不足一手 → 0(宁可不买也不错买)
    assert calc_buy_volume(100.0, 1000.0, 0.15) == 0


def test_next_weekday_skips_weekend():
    assert next_weekday(datetime(2026, 9, 11).date()).isoformat() == "2026-09-14"
    assert next_weekday(datetime(2026, 9, 14).date()).isoformat() == "2026-09-15"


# ---------------- 卖出规则层 ------------

def test_exit_rule_enforce_t1_blocks_same_day():
    r = ExitRule("600000.SH", "浦发", 10.0, "2026-09-15",
                 today=datetime(2026, 9, 15).date(), enforce_t1=True)
    assert r.evaluate(9.0) == (None, None)          # 当日买入, 跌停也不卖
    r2 = ExitRule("600000.SH", "浦发", 10.0, "2026-09-14",
                  today=datetime(2026, 9, 15).date(), enforce_t1=True)
    assert r2.evaluate(9.0)[0] == "SELL"            # 次日可卖


def test_evaluate_all_can_use_filter():
    book = PositionBook()
    book.add("600000.SH", "浦发", 10.0, "2026-09-10", volume=1000)
    book.add("000001.SZ", "平安", 10.0, "2026-09-10", volume=2000)
    prices = {"600000.SH": 9.0, "000001.SZ": 9.0}   # 两只都止损
    hits = book.evaluate_all(prices, can_use={"600000.SH": 0,
                                              "000001.SZ": 2000})
    assert [c for c, _p, _a, _r in hits] == ["000001.SZ"]


def test_is_limit_down():
    assert is_limit_down("600000.SH", 9.0, 10.0) is True     # 主板 -10%
    assert is_limit_down("600000.SH", 9.4, 10.0) is False
    assert is_limit_down("300001.SZ", 8.0, 10.0) is True     # 创业板 -20%
    assert is_limit_down("600000.SH", 9.0, None) is False    # 缺昨收 fail-open


# ---------------- 收盘选股 → 计划 ----------------

def test_close_pick_plans_with_sized_volume(tmp_path):
    d = _daemon(tmp_path, screen_fn=lambda now: _screen_result(
        [("600000.SH", "浦发", 10.55), ("000001.SZ", "平安", 20.0)]))
    out = d.tick_once(MON)
    assert out["action"] == "close_pick"
    st = _read_json(tmp_path / "live_state.json")
    plans = {p["code"]: p for p in st["plans"]}
    assert set(plans) == {"600000.SH", "000001.SZ"}
    assert plans["600000.SH"]["volume"] == 14200             # 100万×15%÷10.55
    assert plans["000001.SZ"]["volume"] == 7500              # 150000÷20
    assert plans["600000.SH"]["for_date"] == "2026-09-15"    # 次一工作日
    assert out["planned"][0]["price"] == 10.55
    # 收盘只落计划, 不写信号(避免桥端非交易时段拒单)
    assert _pending(tmp_path) == []


def test_close_pick_skips_when_asset_unavailable(tmp_path):
    d = _daemon(tmp_path, account=_FakeAccount(asset=None),
                screen_fn=lambda now: _screen_result([("600000.SH", "浦发", 10.0)]))
    out = d.tick_once(MON)
    assert "asset_unavailable" in out["skipped"]
    assert _read_json(tmp_path / "live_state.json")["plans"] == []


def test_close_pick_respects_max_positions(tmp_path):
    book = PositionBook()
    pos = {}
    for i in range(5):
        code = "60000%d.SH" % i
        book.add(code, "x", 10.0, "2026-09-10", volume=100)
        pos[code] = {"can_use_volume": 100}
    _write_json(tmp_path / "positions.json", book.all())
    # 券商持仓与账本一致 → 对账不会清仓, slots = 5-5 = 0
    d = _daemon(tmp_path, max_positions=5, account=_account(positions=pos),
                screen_fn=lambda now: _screen_result([("600000.SH", "浦发", 10.0)]))
    out = d.tick_once(MON)
    assert "max_positions" in out["skipped"]
    assert _read_json(tmp_path / "live_state.json")["plans"] == []


def test_close_pick_only_once_per_day(tmp_path):
    calls = []

    def _screen(now):
        calls.append(now)
        return _screen_result([("600000.SH", "浦发", 10.55)])

    d = _daemon(tmp_path, screen_fn=_screen)
    d.tick_once(MON)
    d._last_sync_ts = 0
    out2 = d.tick_once(datetime(2026, 9, 14, 15, 8))
    assert len(calls) == 1                     # 幂等: 当日只选一次
    assert out2["planned"] == []


# ---------------- 次日开盘发单 + 记账 ----------------

def _seed_plan(tmp_path, code="600000.SH", price=10.55, vol=14200,
               for_date="2026-09-15"):
    _write_json(tmp_path / "live_state.json", {
        "version": 1, "pick_slots": ["2026-09-14T15:05"],
        "exit_signaled": {},
        "plans": [{"code": code, "name": "浦发", "price": price, "volume": vol,
                   "for_date": for_date, "created": "2026-09-14T15:06:00",
                   "strategy_id": "t_live", "composite": 1.0}]})


def test_open_send_writes_signal_and_records_position(tmp_path):
    _seed_plan(tmp_path)
    d = _daemon(tmp_path, dry_run=False)
    out = d.tick_once(TUE_OPEN)
    assert out["action"] == "open_send"
    files = _pending(tmp_path)
    assert len(files) == 1
    sig = json.loads(files[0].read_text(encoding="utf-8"))
    assert sig["order_id"] == "BUY_20260915_600000SH"   # 确定性 id(幂等)
    assert sig["stock_code"] == "600000.SH"
    assert sig["price"] == 10.55 and sig["volume"] == 14200
    book = _read_json(tmp_path / "positions.json")
    assert book["600000.SH"]["buy_date"] == "2026-09-15"
    assert book["600000.SH"]["volume"] == 14200
    # 计划已消费
    assert _read_json(tmp_path / "live_state.json")["plans"] == []


def test_open_send_dry_run_has_no_side_effects(tmp_path):
    _seed_plan(tmp_path)
    d = _daemon(tmp_path, dry_run=True)
    out = d.tick_once(TUE_OPEN)
    assert out["action"] == "open_send" and len(out["buys"]) == 1
    assert _pending(tmp_path) == []
    assert not (tmp_path / "positions.json").exists()
    # 计划保留(演练无副作用, 可随时切 --live)
    assert len(_read_json(tmp_path / "live_state.json")["plans"]) == 1


def test_stale_plan_discarded(tmp_path):
    _seed_plan(tmp_path, for_date="2026-09-11")     # 上周五的计划, 周一才启动
    d = _daemon(tmp_path, dry_run=False)
    d.tick_once(datetime(2026, 9, 14, 9, 27))
    assert _read_json(tmp_path / "live_state.json")["plans"] == []
    assert _pending(tmp_path) == []


# ---------------- 盘中卖出巡检 ----------------

def _seed_position(tmp_path, code="600000.SH", buy_date="2026-09-14",
                   volume=1000, buy_price=10.0):
    _write_json(tmp_path / "positions.json", {
        code: {"code": code, "name": "浦发", "buy_price": buy_price,
               "buy_date": buy_date, "volume": volume}})


def _ticks(last, last_close):
    return lambda codes: {c: {"lastPrice": last, "lastClose": last_close}
                          for c in codes}


def test_sell_patrol_triggers_stop_loss(tmp_path):
    _seed_position(tmp_path)
    d = _daemon(tmp_path, dry_run=False,
                account=_account(positions={"600000.SH": {"can_use_volume": 1000}}),
                ticks_fn=_ticks(9.4, 10.0))       # -6% → 止损
    out = d.tick_once(TUE_AM)
    assert out["action"] == "tick"
    assert len(out["sells"]) == 1
    files = _pending(tmp_path)
    assert len(files) == 1
    sig = json.loads(files[0].read_text(encoding="utf-8"))
    assert sig["action"] == "SELL"
    assert sig["order_id"] == "SELL_20260915_600000SH"
    assert sig["volume"] == 1000
    assert "止损" in sig["reason"]


def test_sell_patrol_skips_t1_same_day(tmp_path):
    _seed_position(tmp_path, buy_date="2026-09-15")     # 当日买入
    d = _daemon(tmp_path, dry_run=False,
                # 券商报可卖 1000(数据不准) → enforce_t1 仍须拦住
                account=_account(positions={"600000.SH": {"can_use_volume": 1000}}),
                ticks_fn=_ticks(9.4, 10.0))
    out = d.tick_once(TUE_AM)
    assert out["sells"] == []
    assert _pending(tmp_path) == []


def test_sell_patrol_skips_limit_down(tmp_path):
    _seed_position(tmp_path)
    d = _daemon(tmp_path, dry_run=False,
                account=_account(positions={"600000.SH": {"can_use_volume": 1000}}),
                ticks_fn=_ticks(9.0, 10.0))       # 跌停 → 卖不出, 顺延
    out = d.tick_once(TUE_AM)
    assert out["sells"] == []
    assert any(s.startswith("limit_down") for s in out["skipped"])


def test_sell_patrol_idempotent_same_day(tmp_path):
    _seed_position(tmp_path)
    d = _daemon(tmp_path, dry_run=False,
                account=_account(positions={"600000.SH": {"can_use_volume": 1000}}),
                ticks_fn=_ticks(9.4, 10.0))
    d.tick_once(TUE_AM)
    d._last_sync_ts = 0
    out2 = d.tick_once(datetime(2026, 9, 15, 10, 1))
    assert out2["sells"] == []                 # 当日已发过 → 不重复
    assert len(_pending(tmp_path)) == 1


def test_sell_patrol_take_profit(tmp_path):
    _seed_position(tmp_path, buy_price=10.0)
    d = _daemon(tmp_path, dry_run=False,
                account=_account(positions={"600000.SH": {"can_use_volume": 1000}}),
                ticks_fn=_ticks(11.6, 10.0))       # +16% ≥ 15%
    out = d.tick_once(TUE_AM)
    assert len(out["sells"]) == 1 and "止盈" in out["sells"][0]["reason"]


def test_sell_patrol_dry_run_no_write(tmp_path):
    _seed_position(tmp_path)
    d = _daemon(tmp_path, dry_run=True,
                account=_account(positions={"600000.SH": {"can_use_volume": 1000}}),
                ticks_fn=_ticks(9.4, 10.0))
    out = d.tick_once(TUE_AM)
    assert len(out["sells"]) == 1 and _pending(tmp_path) == []


# ---------------- 券商对账 ----------------

def test_sync_removes_vanished_position(tmp_path):
    _seed_position(tmp_path)
    d = _daemon(tmp_path, account=_account(positions={}))   # 券商已无持仓
    changed, verified = d._sync_positions()
    assert verified is True and changed == 1
    assert _read_json(tmp_path / "positions.json") == {}


def test_sync_fail_closed_when_account_unavailable(tmp_path):
    _seed_position(tmp_path)
    d = _daemon(tmp_path, account=_FakeAccount(asset=None, positions={}))
    changed, verified = d._sync_positions()
    assert verified is False and changed == 0
    assert "600000.SH" in _read_json(tmp_path / "positions.json")  # 账本未被动


def test_sync_writes_back_can_use_volume(tmp_path):
    _seed_position(tmp_path)
    d = _daemon(tmp_path, account=_account(
        positions={"600000.SH": {"can_use_volume": 0}}))
    d._sync_positions()
    assert _read_json(tmp_path / "positions.json")["600000.SH"]["can_use_volume"] == 0
