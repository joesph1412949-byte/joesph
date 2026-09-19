# -*- coding: utf-8 -*-
"""prism 历史回放验收(rehearsal)的断言测试。

**夹具驱动**: 全部输入来自本文件, 不读 runtime/cache 的真实内容(否则
缓存一刷新测试就红)。夹具是 `screen_fn` / `tick_script` / `account` /
`seed_positions` 四个注入缝 —— 与 prism/live_daemon.py 的构造参数同名同义。

每条断言都要求有判别力(见各测试 docstring 的"改坏哪一处会红")。
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism import trader                                    # noqa: E402
from prism.rehearsal import Rehearsal                       # noqa: E402

DAY = "20260918"          # 周五(缓存覆盖到的最后一个交易日)
NEXT = date(2026, 9, 21)  # 次一工作日(周一)


@pytest.fixture(autouse=True)
def _pause_file_in_tmp(tmp_path, monkeypatch):
    """急停开关指向 tmp —— 绝不读真实 D:/QMT_SIGNALS/paused。"""
    monkeypatch.setattr(trader, "PAUSE_FILE", tmp_path / "paused")
    return tmp_path / "paused"


def _strategy(tp=0.15, sl=0.05, hold=5):
    return {"id": "t_rehearsal",
            "sell_rules": {"take_profit_pct": tp, "stop_loss_pct": sl,
                           "max_hold_days": hold}}


def _screen(picks):
    """run_daily 形状的假返回。picks: [(code, price)]。"""
    sigs = [{"order_id": "x", "action": "BUY", "stock_code": c, "name": c,
             "price": p, "volume": 100, "strategy_id": "t_rehearsal",
             "composite": 1.0} for c, p in picks]

    def fn(now):
        fn.calls.append(now)
        return {"environment_ok": True, "candidates": sigs, "signals": sigs,
                "signals_written": 0, "paused": False, "skipped_no_price": 0}

    fn.calls = []
    return fn


def _book(tmp_path, code="600000.SH", buy_date="2026-09-21", buy_price=10.0,
          volume=1000, can_use=None):
    pos = {"code": code, "name": code, "buy_price": buy_price,
           "buy_date": buy_date, "volume": volume}
    if can_use is not None:
        pos["can_use_volume"] = can_use
    return {code: pos}


class _Acct:
    """账户替身: fail='asset' 模拟拿不到资产事实(查询失败, 不是"资产为 0")。"""

    def __init__(self, total=1_000_000.0, cash=None, positions=None,
                 can_use=None, fail=None):
        self.total = total
        self.cash = total * 0.5 if cash is None else cash
        self._pos = positions or {}
        self._can_use = can_use
        self.fail = fail

    def asset(self):
        if self.fail == "asset":
            return None
        return {"total_asset": self.total, "cash": self.cash,
                "market_value": 0.0, "frozen_cash": 0.0}

    def positions(self):
        if self.fail == "positions":
            return None
        return {k: dict(v) for k, v in self._pos.items()}

    def total_asset(self):
        return self.total if self.asset() else None

    def available_cash(self):
        return self.cash if self.asset() else None

    def can_use_map(self):
        if self.fail == "positions":
            return None
        if self._can_use is not None:
            return dict(self._can_use)
        return {c: int(p.get("can_use_volume", p.get("volume", 0)))
                for c, p in self._pos.items()}


def _run(tmp_path, **kw):
    kw.setdefault("strategy", _strategy())
    kw.setdefault("workdir", tmp_path / "rehearsal")
    return Rehearsal(DAY, **kw).run()


def _steps(res, action=None, hm=None, kind=None):
    return [s for s in res["steps"]
            if (action is None or s["action"] == action)
            and (hm is None or s["hm"] == hm)
            and (kind is None or s["kind"] == kind)]


def _state(tmp_path):
    p = tmp_path / "rehearsal" / "live_state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ---------------- R3-1 急停闸门 ----------------

def test_paused_day_emits_zero_signals_and_burns_no_slot(tmp_path):
    """`paused` 存在 ⇒ 该日零信号, 且不消费任何幂等键。

    改坏哪一处会红: 把 `tick_once` 开头的 `if trader.check_paused()` 分支
    下移(或删掉) —— 选股/发单/巡检会照跑, 于是:
      * steps 里出现非 paused 的 action(开盘会发 BUY);
      * pick_slots 被登记(slot 幂等键被消费) —— 急停期间 QMT 抖一下也
        不该把"今天已经选过了"钉死。
    """
    screen = _screen([("600000.SH", 10.0)])
    (tmp_path / "rehearsal").mkdir(parents=True, exist_ok=True)
    (tmp_path / "rehearsal" / "paused").write_text("1", encoding="utf-8")
    res = _run(tmp_path, screen_fn=screen, pause=True,
               seed_plans=[{"code": "600000.SH", "name": "x", "price": 10.0,
                            "volume": 100, "for_date": str(NEXT)}],
               seed_positions=_book(tmp_path, buy_date="2026-09-11"))

    assert res["steps"], "时间线不能是空的(否则断言恒真)"
    live = _steps(res, kind="live")
    assert len(live) >= 8, "live 时点须覆盖三天的选股/发单/巡检/对账"
    assert {s["action"] for s in res["steps"]} == {"paused"}
    assert all(s.get("buys", []) == [] and s.get("sells", []) == []
               for s in res["steps"])
    assert res["summary"]["signals"] == []
    assert screen.calls == []                       # 选股一次都没跑
    assert _state(tmp_path).get("pick_slots", []) == []


# ---------------- R3-2 T+1 ----------------

def test_same_day_buy_never_produces_sell(tmp_path):
    """当日买入的票当日不产生 SELL(即使券商谎报可卖量)。

    T+1 在本链路上是**双层**防御, 两层都在同一条巡检路径上:
      ① `_do_sell_patrol` 的候选预筛 `buy_date < today`(当日仓根本不进候选);
      ② `exit_rules.evaluate_all(enforce_t1=True)`(进了候选也不放行)。
    因此**只破一层测试仍绿**(这本身是特性, 见报告 R3-2 的变异取证):
    两层同时破才红 —— 那时 09:31 的 -6% 会立刻产出
    `SELL_20260921_600000SH`, sells 从 [] 变成 1 条。
    """
    res = _run(tmp_path,
               screen_fn=_screen([]),
               seed_positions=_book(tmp_path, buy_date="2026-09-21"),
               account=_Acct(positions=_book(tmp_path, buy_date="2026-09-21",
                                             can_use=1000),
                             can_use={"600000.SH": 1000}),
               tick_script={"09:31": {"600000.SH": {"lastPrice": 9.40,
                                                    "lastClose": 10.0}}})
    out = _steps(res, hm="09:31")[0]
    assert out["action"] == "tick"                  # 确实走到了巡检分支
    assert out["sells"] == []                       # 但 T+1 拦住
    assert _state(tmp_path)  # 时间线非空的自证


# ---------------- R3-3 止盈 / 止损 ----------------

def test_take_profit_and_stop_loss_reasons_are_distinguishable(tmp_path):
    """+16% 与 -6% 两个持仓 ⇒ 各产出一条 SELL, reason 可辨识。

    改坏哪一处会红:
      * `shared/exit_rules.ExitRule.evaluate` 里止损优先/止盈阈值任一被改
        (如把 `1 + take_profit_pct` 写成 `1 - ...`) ⇒ 11.6 那条不再产出
        SELL, 或 reason 不含"止盈";
      * `_do_sell_patrol` 只保留一条 hit(如按 code 去重成单条) ⇒ 只剩 1 条。
    """
    book = {}
    book.update(_book(tmp_path, code="600000.SH", buy_price=10.0,
                      buy_date="2026-09-18"))
    book.update(_book(tmp_path, code="000001.SZ", buy_price=10.0,
                      buy_date="2026-09-18"))
    res = _run(tmp_path,
               screen_fn=_screen([]),
               seed_positions=book,
               account=_Acct(positions=book,
                             can_use={"600000.SH": 1000, "000001.SZ": 1000}),
               tick_script={"09:31": {"600000.SH": {"lastPrice": 11.60,
                                                    "lastClose": 10.0},
                                      "000001.SZ": {"lastPrice": 9.40,
                                                    "lastClose": 10.0}}})
    sells = {s["stock_code"]: s for s in _steps(res, hm="09:31")[0]["sells"]}
    assert set(sells) == {"600000.SH", "000001.SZ"}
    assert "止盈" in sells["600000.SH"]["reason"]
    assert "止损" in sells["000001.SZ"]["reason"]
    assert sells["600000.SH"]["action"] == "SELL"


# ---------------- R3-4 失败不烧幂等键 ----------------

def test_asset_unavailable_does_not_burn_pick_slot(tmp_path):
    """资产事实缺失 ⇒ 不登记当日 slot(pick_slots 保持空)。

    这正是实盘侧刚修的行为(F1): 只有**真正跑完**的选股才写当日幂等键。
    旧行为(无条件登记)下, QMT 抖一下当天零建仓 + 次日无计划, 日志与
    "今天没选到票"不可区分。

    改坏哪一处会红: `live_daemon.tick_once` 把
        `if self._do_close_pick(now, st, out): st["pick_slots"] += [slot]`
    改回无条件追加 ⇒ pick_slots 变成 ["2026-09-18T15:05"]。
    """
    screen = _screen([("600000.SH", 10.55)])
    res = _run(tmp_path, screen_fn=screen,
               account=_Acct(fail="asset"))
    pick = _steps(res, action="close_pick")
    assert pick, "15:05 的选股步骤必须存在(否则断言恒真)"
    assert "asset_unavailable" in pick[0]["skipped"]
    assert _state(tmp_path).get("pick_slots", []) == []
    assert _state(tmp_path).get("plans", []) == []

    # QMT 恢复(同一 workdir)⇒ slot 仍未登记, 当日 15:05 真重试并落计划
    screen2 = _screen([("600000.SH", 10.55)])
    res2 = _run(tmp_path, screen_fn=screen2)
    assert screen2.calls[0] == datetime(2026, 9, 18, 15, 5), \
        "恢复后必须先重跑**当日**选股(而不是跳过)"
    assert "2026-09-18T15:05" in _state(tmp_path)["pick_slots"]
    assert {p["for_date"] for p in res2["summary"]["planned"]} \
        == {"2026-09-21", "2026-09-22", "2026-09-23"}


# ---------------- R3-5 确定性 ----------------

def test_same_input_twice_yields_identical_signals(tmp_path):
    """同输入跑两次, 信号列表逐项相同(时间戳字段已在输出里剔除)。

    改坏哪一处会红: 让 `rehearsal` 把 `trader.build_signal` 原样输出
    (created_at = 墙钟 isoformat) ⇒ 两次运行的第 3 个 order 起逐项不同,
    json.dumps 不等。同理, 候选排序若不按 (boards, code) 稳定排序也不等。
    """
    def once(tag):
        return _run(tmp_path / tag,
                    screen_fn=_screen([("000001.SZ", 20.0),
                                       ("600000.SH", 10.55)]),
                    seed_positions=_book(tmp_path, buy_date="2026-09-21"),
                    account=_Acct(positions=_book(tmp_path,
                                                  buy_date="2026-09-21",
                                                  can_use=1000),
                                  can_use={"600000.SH": 1000}),
                    tick_script={"09:31": {"600000.SH": {"lastPrice": 9.40,
                                                         "lastClose": 10.0}}})

    a, b = once("a"), once("b")
    ja = json.dumps(a["summary"]["signals"], sort_keys=True, ensure_ascii=False)
    jb = json.dumps(b["summary"]["signals"], sort_keys=True, ensure_ascii=False)
    assert ja == jb
    assert json.loads(ja), "信号列表不能是空的(否则相等没有判别力)"
    # 时间戳字段必须已被剔除, 否则上面那条相等只是运气
    assert "created_at" not in json.loads(ja)[0]
    # 步骤骨架也要一致
    assert [(s["hm"], s["action"], s["skipped"]) for s in a["steps"]] == \
           [(s["hm"], s["action"], s["skipped"]) for s in b["steps"]]


# ---------------- 零副作用(回放自身) ----------------

def test_rehearsal_never_writes_signals(tmp_path):
    """dry_run ⇒ signal_root 下不落任何 pending 文件, 但预览里能看到内容。"""
    res = _run(tmp_path, screen_fn=_screen([("600000.SH", 10.55)]))
    root = tmp_path / "rehearsal" / "signals"
    assert not list(root.glob("**/pending/*.json"))
    assert not list(root.glob("**/*"))
    writes = [w for s in res["steps"] for w in s["would_write"]]
    assert writes, "至少应有一条 BUY 的落盘预览(否则等于没验收信号内容)"
    assert writes[0]["file"].endswith(".json")
    assert writes[0]["payload"]["action"] == "BUY"
