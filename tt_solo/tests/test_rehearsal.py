# -*- coding: utf-8 -*-
"""tt_solo 历史回放的最小验收断言(自包含: 只 import ttcore)。

三条断言:
  1. 被柜台**拒绝**的卖单**不**进账本(C1: SELL_NOT_ACCEPTED 的另一半);
  2. 同一轮内 `SELL` 总量 **≤** 券商可卖量(C2: 同轮重复卖同一批底仓);
  3. 非交易日(真实 2026-09-19 周六)⇒ `phase=CLOSED`、零意图(I3)。

**零下单**: 本文件不构造 `DirectExecutor`, 也不给任何真后端 —— 用一个纯
Python 假执行器满足 `TTDaemon._exec_direct` 需要的接口(`execute(signals,
dry_run=)` + `.stats`), 柜台受理结果完全由测试给定。信号根/账本/运行时
快照全部指向 tmp_path。
"""
from datetime import datetime

from ttcore import config as tt_config
from ttcore.daemon import TTDaemon
from ttcore.engine import TTEngine
from ttcore.state import Ledger

FRI = datetime(2026, 9, 14, 10, 0, 0)      # 星期一 10:00(交易日、盘中)
SAT = datetime(2026, 9, 19, 10, 0, 0)      # **真实的星期六** 10:00

CODE = "600900.SH"


# ---------------------------------------------------------------- 夹具

class FakeFeed:
    """可编程行情: 直接给 snapshot, 绕开全部取数。"""

    def __init__(self, snaps):
        self.snaps = snaps
        self.last_source = "fake"

    def snapshot(self, code, count=80, sigma_window=60):
        return self.snaps.get(code)

    def closes(self, code, count=80):
        return None

    def ticks(self, codes):
        return {c: (self.snaps.get(c) or {}).get("tick", {}) for c in codes}


def _snap(code=CODE, last=28.45, last_close=28.09, high=28.60, low=28.00):
    return {
        "code": code,
        "tick": {"last": last, "open": last_close, "high": high, "low": low,
                 "last_close": last_close, "volume": 1e6, "amount": 3e7},
        "ind": {"last": last, "n": 80, "ma20": last_close,
                "ma20_prev": last_close, "r20_pct": 0.0, "sigma": 0.01,
                "trend_degree": 0.2},
        "source": "fake",
    }


def _cfg(can_use=1000, weight=0.18):
    """纸面底仓 = can_use(force_paper 下就是券商可卖量口径)。"""
    return tt_config.load(overrides={
        "env": "real", "dry_run": True, "account_id": "",
        "paper_total_asset": 500000.0,
        "paper_positions": {CODE: int(can_use)},
        "max_units_per_round": 2,
        "grid": {"band_mode": "sigma", "band_k": 1.0, "n_units": 5,
                 "max_units": 3, "ref_mode": "prev_close",
                 "sigma_window": 60},
        "session": {"open_start": "09:30", "open_end": "14:55",
                    "converge_after": "14:30", "hard_stop_after": "14:57"},
        "risk": {"max_single_order_amount": 50000, "max_daily_trades": 20,
                 "max_daily_loss": 3000, "max_price_deviation_pct": 0.05,
                 "max_position_pct": 0.20, "max_net_buy_today_ratio": 0.0,
                 "max_consecutive_failures": 3, "max_slippage_pct": 0.03},
        "symbols": [{"code": CODE, "name": "长江电力", "enabled": True,
                     "weight": weight, "band_pct": 0.53, "n_units": 5,
                     "switch": {"dev_max_pct": 4.0, "slope_max_pct": 0.3,
                                "r20_max_pct": 8.0}}],
    })


class FakeExecutor:
    """满足 _exec_direct 接口的假执行器。绝不接触 QMT / 真实账户。

    accept=True → 每笔都"已受理"; accept=False → 每笔都"柜台拒单"
    (seq<0 在真实 DirectExecutor 里就是 ORDER_REJECTED)。
    """

    def __init__(self, accept=True):
        self.accept = accept
        self.seen = []
        self.stats = {}

    def execute(self, intents, dry_run=True):
        self.seen.extend(list(intents))
        out = []
        for it in intents:
            oid = it["order_id"] if isinstance(it, dict) else it.order_id
            if self.accept:
                out.append({"order_id": oid, "ok": True, "code": "SUBMITTED",
                            "seq": 1001})
            else:
                out.append({"order_id": oid, "ok": False,
                            "code": "ORDER_REJECTED",
                            "msg": "柜台拒单(测试)"})
        return out


def _daemon(tmp_path, now_fn, *, cfg=None, feed=None, executor=None,
            dry_run=True, direct=False, env="real"):
    cfg = cfg if cfg is not None else _cfg()
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    eng = TTEngine(cfg, led, feed=feed or FakeFeed({CODE: _snap()}),
                   now_fn=now_fn, force_paper=True)
    root = tmp_path / "sig"
    (root / env).mkdir(parents=True, exist_ok=True)
    (root / env / "armed.txt").write_text(now_fn().strftime("%Y%m%d"),
                                          encoding="utf-8")
    return TTDaemon(cfg, ledger=led, engine=eng, dry_run=dry_run,
                    now_fn=now_fn, signal_root=root,
                    runtime_path=tmp_path / "rt.json",
                    executor=executor, direct=direct)


def _round(cfg, feed, tmp_path, accept):
    """跑一轮直连模式(纯假执行器), 返回 (runtime, executor, ledger)。"""
    ex = FakeExecutor(accept=accept)
    d = _daemon(tmp_path, lambda: FRI, cfg=cfg, feed=feed, executor=ex,
                dry_run=False, direct=True)
    return d.run_once(), ex, d.ledger


# ---------------------------------------------------------------- 1) C1

def test_rejected_sell_is_not_booked(tmp_path):
    """被柜台拒的 SELL 一股都不进账本。

    改坏哪一处会红: `TTDaemon.run_once` 的 direct 分支把
        `accepted = [s for s, r in zip(...) if r.get("ok")]`
    改回 `self._book(signals, ...)`(旧行为: 无条件记账) ⇒ 被拒的 600 股
    也写进 `sold_today`, 下一轮 `net_exposure` 就凭这笔假卖出放行真买入。
    """
    cfg, feed = _cfg(), FakeFeed({CODE: _snap()})
    rt_rej, ex_rej, led_rej = _round(cfg, feed, tmp_path / "rej", False)

    assert rt_rej["counts"]["intents"] >= 1, "本轮必须真有 SELL 意图(否则断言恒真)"
    assert all(it["side"] == "SELL" for it in rt_rej["intents"])
    assert len(ex_rej.seen) == len(rt_rej["intents"]), "卖单确实提交过(才谈得上被拒)"
    assert [r["ok"] for r in rt_rej["exec"]] == [False] * len(ex_rej.seen)
    assert rt_rej["booked"] == 0
    assert led_rej.total_realized_pnl() == 0
    assert led_rej.daily_trades() == 0
    # 账本条目可能已被 plan() 以全 0 建出来(引擎读水位会建符号), 但成交量必须为 0
    sym = led_rej.snapshot()["symbols"].get(CODE) or {}
    assert int(sym.get("sold_today", 0)) == 0
    assert int(sym.get("filled_sell_units", 0)) == 0

    # 负控: 同一套输入换成"柜台受理" → 必须记账(否则上面对比的不是 C1)
    rt_ok, _ex_ok, led_ok = _round(cfg, feed, tmp_path / "ok", True)
    assert rt_ok["booked"] == len(rt_ok["intents"]) > 0
    assert led_ok.snapshot()["symbols"][CODE]["sold_today"] > 0


# ---------------------------------------------------------------- 2) C2

def test_same_round_sell_volume_capped_by_can_use(tmp_path):
    """同一轮 SELL 总量 ≤ 券商可卖量(同轮不得重复卖同一批底仓)。

    构造: 可卖 300 股; 阶梯被 high 穿越 2 档; max_units_per_round=2
    ⇒ 引擎会尝试生成 2 笔(单笔 600 股)。第 1 笔被裁到 300; 第 2 笔必须
    看到"本轮已卖 300"而拒绝。

    改坏哪一处会红: `TTEngine.plan_symbol` 去掉 `sold_run` 累加(或
    `_make_intent` 不把 `sold_run` 并进 `sold_today`) ⇒ 第 2 笔仍拿
    sold_today=0 过闸, 又放行 300 股 → 同轮卖出 600 > 可卖 300。
    """
    cfg, feed = _cfg(can_use=300), FakeFeed({CODE: _snap()})
    rt, _ex, _led = _round(cfg, feed, tmp_path, True)

    sells = [it for it in rt["intents"] if it["side"] == "SELL"]
    total = sum(int(it["volume"]) for it in sells)
    assert sells, "至少要有一笔可执行的 SELL(否则 ≤300 恒真)"
    assert total <= 300, "同轮卖出 %d 股 > 可卖 300 股" % total
    assert total == 300, "第 1 笔应被裁到可卖量 300 股, 实际 %d" % total
    # 第 2 档被"可卖量已被本轮吃掉"拦下, 归因可辨
    blocked = [r for r in rt["rejected"] if r["side"] == "SELL"]
    assert blocked and blocked[0]["reject_code"] in ("NO_BASE_POSITION",
                                                     "SELLABLE_EXCEEDED")

    # 负控: 可卖量放大到 100000 → 两档都该放行(证明上面不是"阶梯没穿越")
    cfg2 = _cfg(can_use=100000)
    rt2, _e2, _l2 = _round(cfg2, feed, tmp_path / "big", True)
    assert len([it for it in rt2["intents"] if it["side"] == "SELL"]) == 2


# ---------------------------------------------------------------- 3) I3

def test_saturday_20260919_is_closed_with_zero_intents(tmp_path):
    """真实 2026-09-19(周六)⇒ phase=CLOSED、零意图。

    改坏哪一处会红: `ttcore/risk.py::session_phase` 删掉
    `if now.weekday() >= 5: return PHASE_CLOSED` 那一档 —— 周六 10:00 会
    落进"盘中"区间, 意图照发(实测 QMT 周六照样推 tick, 且价格是假的:
    09-19 长电 lastPrice 31.10 vs 09-18 真收盘 28.27)。
    """
    cfg, feed = _cfg(), FakeFeed({CODE: _snap()})
    rt = _daemon(tmp_path / "sat", lambda: SAT, cfg=cfg, feed=feed).run_once()

    assert rt["phase"] == "CLOSED"
    assert rt["counts"]["intents"] == 0
    assert rt["intents"] == [] and rt["signals"] == []
    assert rt["rejected"], "被拦下的意图要留归因(否则'零意图'可能是行情缺失)"
    assert {r["reject_code"] for r in rt["rejected"]} == {"SESSION_CLOSED"}

    # 负控: 同一套行情换到交易日 10:00 → 必须有意图(否则上面是空跑)
    rt2 = _daemon(tmp_path / "fri", lambda: FRI, cfg=cfg, feed=feed).run_once()
    assert rt2["phase"] != "CLOSED"
    assert rt2["counts"]["intents"] >= 1
