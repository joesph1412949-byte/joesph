# -*- coding: utf-8 -*-
"""实盘信号守护测试(全离线, 不碰 QMT / 不落真实信号)。

依赖全部注入: provider / account / screen_fn / ticks_fn / now_fn,
状态与信号目录指向 tmp_path。
"""
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.common import next_weekday
from shared.exit_rules import ExitRule, PositionBook, is_limit_down
from prism import trader
from prism.live_account import LiveAccount, calc_buy_volume
from prism.live_daemon import LiveDaemon

MON = datetime(2026, 9, 14, 15, 6)      # 周一 收盘后(选股)
TUE_OPEN = datetime(2026, 9, 15, 9, 27)  # 周二 开盘买入窗口
TUE_AM = datetime(2026, 9, 15, 10, 0)    # 周二 盘中


@pytest.fixture(autouse=True)
def _pause_file_in_tmp(tmp_path, monkeypatch):
    """每个用例都把急停开关指向 tmp —— 绝不读真实 D:/QMT_SIGNALS/paused
    (真实盘那份是用户的状态, 读它会随用户操作让测试随机变红/变绿)。"""
    monkeypatch.setattr(trader, "PAUSE_FILE", tmp_path / "paused")
    return tmp_path / "paused"


# ---------------- 测试替身 ----------------

class _FakeProvider:
    """只需非 None(daemon 用 provider 存在性做闸门)。"""

    class _DS:
        def get_full_market_ticks(self, codes=None):
            return {}

    def __init__(self):
        self.ds = self._DS()


class _FakeAccount:
    """账户替身。pos_exc/pos_none 用来模拟"券商持仓查询失败"这一事实缺失态。"""

    def __init__(self, asset=None, positions=None, pos_exc=None, pos_none=False):
        self._asset = asset
        self._pos = positions or {}
        self._pos_exc = pos_exc
        self._pos_none = pos_none

    def asset(self):
        return self._asset

    def positions(self):
        if self._pos_exc:
            raise self._pos_exc
        if self._pos_none:
            return None
        return {k: dict(v) for k, v in self._pos.items()}

    def total_asset(self):
        a = self._asset
        if not a:
            return None
        v = float(a.get("total_asset") or 0)
        return v if v > 0 else None

    def available_cash(self):
        a = self._asset
        if not a:
            return None
        v = float(a.get("cash") or 0)
        return v if v > 0 else None

    def can_use_map(self):
        pos = self.positions()
        if pos is None:
            return None
        return {c: int(p.get("can_use_volume") or 0) for c, p in pos.items()}


class _FakeBackend:
    """LiveAccount 后端替身: positions 返回指定值或抛异常(不碰 QMT)。"""

    def __init__(self, positions=None, exc=None):
        self._pos = positions
        self._exc = exc

    def connect(self):
        return True

    def asset(self):
        return {"total_asset": 1000000.0, "cash": 1000000.0,
                "market_value": 0.0, "frozen_cash": 0.0}

    def positions(self):
        if self._exc:
            raise self._exc
        return self._pos


def _account(total=1000000.0, positions=None, cash=None, **kw):
    return _FakeAccount({"total_asset": total,
                         "cash": total * 0.5 if cash is None else cash,
                         "market_value": 0.0, "frozen_cash": 0.0},
                        positions or {}, **kw)


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
    # 建仓预算 = min(总资产 100万, 可用现金 50万) = 50万, 逐只扣减(R5)
    assert plans["600000.SH"]["volume"] == 7100              # 50万×15%÷10.55
    assert plans["000001.SZ"]["volume"] == 3100              # (50万-74905)×15%÷20
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


# ---------------- 失败不烧当日 slot(F1 实盘侧; 与 paper 侧同口径) ----------------

def test_close_pick_failure_does_not_burn_slot(tmp_path):
    """资产查询失败 ⇒ 不登记当日 pick_slots, 且下一轮(QMT 恢复后)真重试。

    旧行为: 无条件登记 slot ⇒ QMT 抖一下当天零建仓、次日无计划可发,
    日志里只剩 "asset_unavailable", 与"今天没选到票"不可区分。
    """
    calls = []

    def _screen(now):
        calls.append(now)
        return _screen_result([("600000.SH", "浦发", 10.55)])

    d = _daemon(tmp_path, account=_FakeAccount(asset=None), screen_fn=_screen)
    out = d.tick_once(MON)
    assert "asset_unavailable" in out["skipped"]
    assert _read_json(tmp_path / "live_state.json")["pick_slots"] == []
    # QMT 恢复 → slot 仍未登记, 下一轮必须真重试并把计划落下来
    d.account._asset = {"total_asset": 1000000.0, "cash": 500000.0,
                        "market_value": 0.0, "frozen_cash": 0.0}
    d._last_sync_ts = 0
    out2 = d.tick_once(datetime(2026, 9, 14, 15, 7))
    assert len(calls) == 2                     # 真的重试了完整选股
    assert [p["code"] for p in out2["planned"]] == ["600000.SH"]
    assert _read_json(tmp_path / "live_state.json")["pick_slots"] \
        == ["2026-09-14T15:05"]


def test_close_pick_cash_unavailable_does_not_burn_slot(tmp_path):
    calls = []

    def _screen(now):
        calls.append(now)
        return _screen_result([("600000.SH", "浦发", 10.55)])

    d = _daemon(tmp_path, account=_account(cash=0), screen_fn=_screen)
    out = d.tick_once(MON)
    assert "cash_unavailable" in out["skipped"]
    assert _read_json(tmp_path / "live_state.json")["pick_slots"] == []
    d.account._asset["cash"] = 500000.0
    d._last_sync_ts = 0
    d.tick_once(datetime(2026, 9, 14, 15, 7))
    assert len(calls) == 2


def test_close_pick_screen_error_does_not_burn_slot(tmp_path):
    """run_daily 带 error(全缺价拒单) ⇒ 不是"今天没票", 不登记 slot。"""
    calls = []

    def _screen(now):
        calls.append(now)
        return {"environment_ok": True, "candidates": [{"code": "600000.SH"}],
                "signals": [], "signals_written": 0, "paused": False,
                "skipped_no_price": 1,
                "error": "real盘候选缺 up_stop_price, 已拒绝生成信号"}

    d = _daemon(tmp_path, screen_fn=_screen)
    out = d.tick_once(MON)
    assert out["screen"]["error"]
    assert _read_json(tmp_path / "live_state.json")["pick_slots"] == []
    d._last_sync_ts = 0
    d.tick_once(datetime(2026, 9, 14, 15, 7))
    assert len(calls) == 2


def test_close_pick_screen_exception_does_not_burn_slot(tmp_path):
    """选股抛异常 ⇒ tick 抛出(run_forever 下轮重试), slot 绝不登记。"""
    def _boom(now):
        raise RuntimeError("选股炸了")

    d = _daemon(tmp_path, screen_fn=_boom)
    with pytest.raises(RuntimeError):
        d.tick_once(MON)
    assert d._load_state()["pick_slots"] == []


def test_close_pick_no_candidates_burns_slot(tmp_path):
    """反向护栏: 合法结果(门禁不过/今天没票)必须登记 slot。

    不登记的话, 30 秒一轮会把整套选股(涨停池+逐股上下文)重跑到收盘。
    """
    calls = []

    def _screen(now):
        calls.append(now)
        return {"environment_ok": False, "candidates": [], "signals": [],
                "signals_written": 0, "paused": False, "skipped_no_price": 0}

    d = _daemon(tmp_path, screen_fn=_screen)
    d.tick_once(MON)
    assert _read_json(tmp_path / "live_state.json")["pick_slots"] \
        == ["2026-09-14T15:05"]
    d._last_sync_ts = 0
    out2 = d.tick_once(datetime(2026, 9, 14, 15, 7))
    assert len(calls) == 1                     # 不重复跑完整选股
    assert out2["planned"] == []


# ---------------- 次日开盘发单 + 记账 ----------------

def _plan(code="600000.SH", price=10.55, vol=14200,
          for_date="2026-09-15"):
    return {"code": code, "name": "浦发", "price": price, "volume": vol,
            "for_date": for_date, "created": "2026-09-14T15:06:00",
            "strategy_id": "t_live", "composite": 1.0}


def _seed_state(tmp_path, plans, pick_slots=("2026-09-14T15:05",)):
    _write_json(tmp_path / "live_state.json", {
        "version": 1, "pick_slots": list(pick_slots),
        "exit_signaled": {}, "plans": plans})


def _seed_plan(tmp_path, code="600000.SH", price=10.55, vol=14200,
               for_date="2026-09-15"):
    _seed_state(tmp_path, [_plan(code, price, vol, for_date)])


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


# ---------------- R1 查询失败 vs 券商确认空仓 (必须可区分) ----------------

def test_live_account_positions_none_when_backend_raises():
    acc = LiveAccount(backend=_FakeBackend(exc=RuntimeError("query 炸了")))
    assert acc.positions() is None
    assert acc.can_use_map() is None          # 不冒充"没有可卖量"


def test_live_account_positions_none_when_backend_returns_none():
    acc = LiveAccount(backend=_FakeBackend(positions=None))
    assert acc.positions() is None
    assert acc.can_use_map() is None


def test_live_account_positions_empty_when_backend_confirms_empty():
    acc = LiveAccount(backend=_FakeBackend(positions=[]))     # 券商确认空仓
    assert acc.positions() == {}
    assert acc.can_use_map() == {}


def test_live_account_positions_none_when_not_connected():
    class _NoConnect(_FakeBackend):
        def connect(self):
            return False

    acc = LiveAccount(backend=_NoConnect(positions=[]))
    assert acc.positions() is None


def test_sync_fail_closed_when_positions_query_raises(tmp_path):
    _seed_position(tmp_path)
    before = (tmp_path / "positions.json").read_bytes()
    d = _daemon(tmp_path, account=_account(pos_exc=RuntimeError("query 炸了")))
    assert d._sync_positions() == (0, False)
    assert (tmp_path / "positions.json").read_bytes() == before   # 账本一字不动


def test_sync_fail_closed_when_positions_query_returns_none(tmp_path):
    _seed_position(tmp_path)
    before = (tmp_path / "positions.json").read_bytes()
    d = _daemon(tmp_path, account=_account(pos_none=True))
    assert d._sync_positions() == (0, False)
    assert (tmp_path / "positions.json").read_bytes() == before


def test_sync_fail_closed_through_real_live_account(tmp_path):
    """LiveAccount + 炸掉的后端(控制者复现形状) → 真持仓账本不得被抹。"""
    _seed_position(tmp_path)
    acc = LiveAccount(backend=_FakeBackend(exc=RuntimeError("qmt 掉线")))
    d = _daemon(tmp_path, account=acc)
    assert d._sync_positions() == (0, False)
    assert "600000.SH" in _read_json(tmp_path / "positions.json")


# ---------------- R2 对账自愈: 券商有、账本无 → 保守采纳 ----------------

def test_sync_adopts_broker_position_missing_from_book(tmp_path, caplog):
    _write_json(tmp_path / "positions.json", {})              # 账本被抹空
    d = _daemon(tmp_path, account=_account(positions={
        "000001.SZ": {"open_price": 20.0, "volume": 500,
                      "can_use_volume": 0}}))
    with caplog.at_level(logging.WARNING):
        changed, verified = d._sync_positions(today=date(2026, 9, 15))
    assert verified is True and changed == 1                  # 采纳计入 changed
    pos = _read_json(tmp_path / "positions.json")["000001.SZ"]
    assert pos["buy_price"] == 20.0                           # 成本取券商 open_price
    assert pos["buy_date"] == "2026-09-15"                    # 今日 → T+1 安全
    assert pos["volume"] == 500 and pos["can_use_volume"] == 0
    assert "000001.SZ" in caplog.text and "账本缺失" in caplog.text


def test_sync_skips_adoption_without_open_price(tmp_path, caplog):
    _write_json(tmp_path / "positions.json", {})
    d = _daemon(tmp_path, account=_account(positions={
        "000001.SZ": {"open_price": 0, "volume": 500, "can_use_volume": 0}}))
    with caplog.at_level(logging.WARNING):
        changed, verified = d._sync_positions(today=date(2026, 9, 15))
    assert verified is True and changed == 0                  # 拿不到成本 → 不采纳
    assert _read_json(tmp_path / "positions.json") == {}
    assert "000001.SZ" in caplog.text


def test_load_positions_corrupt_json_warns_and_is_not_overwritten(tmp_path,
                                                                 caplog):
    (tmp_path / "positions.json").write_text("{坏 JSON", encoding="utf-8")
    before = (tmp_path / "positions.json").read_bytes()
    d = _daemon(tmp_path, account=_account(positions={}))
    with caplog.at_level(logging.WARNING):
        assert d._load_positions().all() == {}                # 不炸, 但留痕
    assert "positions.json" in caplog.text
    assert d._sync_positions(today=date(2026, 9, 15)) == (0, True)
    assert (tmp_path / "positions.json").read_bytes() == before   # 未被覆盖


# ---------------- R3 急停开关对守护自身发单生效 ----------------

def test_tick_once_paused_blocks_signals_and_state_change(tmp_path):
    _seed_state(tmp_path, [_plan(), _plan(code="000001.SZ", for_date="2026-09-11")])
    _seed_position(tmp_path)                       # 盘中本应止损卖出
    (tmp_path / "paused").write_text("1", encoding="utf-8")   # 按下急停
    d = _daemon(tmp_path, dry_run=False,
                account=_account(positions={"600000.SH": {"can_use_volume": 1000}}),
                ticks_fn=_ticks(9.4, 10.0))
    state_before = (tmp_path / "live_state.json").read_bytes()
    pos_before = (tmp_path / "positions.json").read_bytes()
    out_open = d.tick_once(TUE_OPEN)               # 开盘窗口本应发 BUY
    out_am = d.tick_once(TUE_AM)                   # 盘中本应发 SELL
    assert out_open["action"] == "paused" and out_am["action"] == "paused"
    assert out_open["buys"] == [] and out_am["sells"] == []
    assert _pending(tmp_path) == []
    assert (tmp_path / "live_state.json").read_bytes() == state_before
    assert (tmp_path / "positions.json").read_bytes() == pos_before


def test_tick_once_not_paused_still_trades(tmp_path):
    """负控: 急停文件不存在(autouse fixture 把开关指向 tmp) → 照常判定卖出。"""
    assert not (tmp_path / "paused").exists()
    _seed_position(tmp_path)
    d = _daemon(tmp_path, dry_run=False,
                account=_account(positions={"600000.SH": {"can_use_volume": 1000}}),
                ticks_fn=_ticks(9.4, 10.0))
    out = d.tick_once(TUE_AM)
    assert out["action"] == "tick" and len(out["sells"]) == 1


# ---------------- R4 拿不到可卖量 → 不得回落全量股数 ----------------

def test_sell_patrol_no_sell_when_book_can_use_is_zero(tmp_path):
    """账本记 can_use_volume=0 且券商查询失败 → 不得按 volume 全量硬卖。"""
    _write_json(tmp_path / "positions.json", {
        "600000.SH": {"code": "600000.SH", "name": "浦发", "buy_price": 10.0,
                      "buy_date": "2026-09-14", "volume": 1000,
                      "can_use_volume": 0}})
    d = _daemon(tmp_path, dry_run=False,
                account=_account(pos_exc=RuntimeError("持仓查询炸了")),
                ticks_fn=_ticks(9.4, 10.0))       # -6% → 止损
    out = d.tick_once(TUE_AM)
    assert out["sells"] == []
    assert _pending(tmp_path) == []


def test_sell_patrol_skips_when_can_use_unknown_everywhere(tmp_path):
    _seed_position(tmp_path)                       # 账本也没记 can_use_volume
    d = _daemon(tmp_path, dry_run=False,
                account=_account(pos_exc=RuntimeError("持仓查询炸了")),
                ticks_fn=_ticks(9.4, 10.0))
    out = d.tick_once(TUE_AM)
    assert out["sells"] == []                      # fail-closed: 本轮不卖
    assert "no_can_use:600000.SH" in out["skipped"]


# ---------------- R5 建仓受可用现金约束 ----------------

def test_close_pick_no_build_when_cash_unavailable(tmp_path):
    d = _daemon(tmp_path, account=_account(cash=0),      # 现金 0 → 拿不到现金事实
                screen_fn=lambda now: _screen_result([("600000.SH", "浦发", 10.55)]))
    out = d.tick_once(MON)
    assert "cash_unavailable" in out["skipped"]
    assert out["planned"] == []
    assert _read_json(tmp_path / "live_state.json")["plans"] == []


def test_close_pick_budget_capped_by_cash_and_decremented(tmp_path):
    d = _daemon(tmp_path, account=_account(total=1000000.0, cash=100000.0),
                screen_fn=lambda now: _screen_result(
                    [("600000.SH", "浦发", 10.0), ("000001.SZ", "平安", 20.0)]))
    out = d.tick_once(MON)
    assert [p["volume"] for p in out["planned"]] == [1500, 600]   # 逐只扣减
    spend = sum(p["volume"] * p["price"] for p in out["planned"])
    assert spend <= 100000.0


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
