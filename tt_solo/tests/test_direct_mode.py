# -*- coding: utf-8 -*-
"""直连模式的 daemon 集成测试: 闸门串联 + 下单路径。全程 fake, 不碰真钱。

覆盖:
  - dry_run 拦下 → 零报单
  - paused 拦下 → 零报单
  - armed 缺失 → 零报单
  - armed 通过 + live → 真正报单(打到 fake backend)
"""
import pytest
from datetime import datetime

from ttcore import config as tc
from ttcore import risk as tt_risk
from ttcore.daemon import TTDaemon
from ttcore.engine import TTEngine
from ttcore.executor import DirectExecutor
from ttcore.state import Ledger

from .test_executor import FakeBackend


class FakeFeed:
    def __init__(self, snaps):
        self.snaps = snaps
        self.last_source = "fake"

    def set(self, code, ctx):
        self.snaps[code] = ctx

    def snapshot(self, code, count=80, sigma_window=60):
        return self.snaps.get(code)

    def closes(self, code, count=80):
        return (self.snaps.get(code) or {}).get("_closes")

    def ticks(self, codes):
        return {c: (self.snaps.get(c) or {}).get("tick", {}) for c in codes}


def _snap(code, last, last_close, high, low):
    return {
        "code": code,
        "tick": {"last": last, "open": last_close, "high": high, "low": low,
                 "last_close": last_close, "volume": 1e6, "amount": 3e7},
        "ind": {"last": last, "n": 80, "ma20": last_close, "ma20_prev":
                last_close, "r20_pct": 0.0, "sigma": 0.01,
                "trend_degree": 0.2},
        "source": "fake",
    }


def _fixed_now():
    return datetime(2026, 9, 14, 10, 0, 0)


def _env(tmp_path, backend, executor=True, can_use=5000):
    """直连环境工厂: 行情**同时**穿越卖档与买档(才能暴露 C1 的因果链)。"""
    root = tmp_path / "signals"
    (root / "real").mkdir(parents=True, exist_ok=True)
    (root / "real" / "armed.txt").write_text("20260914", encoding="utf-8")
    cfg = tc.load(overrides={"dry_run": False, "env": "real",
                             "paper_total_asset": 500000.0,
                             "paper_positions": {"600900.SH": can_use}})
    feed = FakeFeed({"600900.SH": _snap("600900.SH", 28.50, 28.63,
                                        28.95, 28.30)})
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=_fixed_now)
    eng = TTEngine(cfg, led, feed=feed, now_fn=_fixed_now, force_paper=True)
    ex = DirectExecutor(account_id="88869979", backend=backend,
                        now_fn=_fixed_now) if executor else None
    d = TTDaemon(cfg, ledger=led, engine=eng, dry_run=False,
                 now_fn=_fixed_now, executor=ex, direct=True,
                 signal_root=root, runtime_path=tmp_path / "rt.json")
    return d, led


@pytest.fixture
def direct_env(tmp_path):
    """构造一个 armed 就位、行情会触发卖单的直连环境。"""
    armed_dir = tmp_path / "signals"
    (armed_dir / "real").mkdir(parents=True)
    (armed_dir / "real" / "armed.txt").write_text("20260914",
                                                  encoding="utf-8")

    cfg = tc.load(overrides={"dry_run": False, "env": "real"})
    snaps = {
        "600900.SH": _snap("600900.SH", 28.90, 28.63, 28.95, 28.60),
        "600938.SH": _snap("600938.SH", 33.80, 33.91, 33.85, 33.70),
        "601088.SH": _snap("601088.SH", 47.10, 47.20, 47.30, 47.00),
    }
    feed = FakeFeed(snaps)
    led = Ledger(path=tmp_path / "tt_state.json",
                 now_fn=lambda: __import__("datetime").datetime(
                     2026, 9, 14, 10, 0, 0))
    eng = TTEngine(cfg, led, feed=feed, now_fn=led.now_fn, force_paper=True)
    be = FakeBackend()
    ex = DirectExecutor(account_id="88869979", backend=be, now_fn=led.now_fn)
    d = TTDaemon(cfg, ledger=led, engine=eng, dry_run=False,
                 now_fn=led.now_fn, executor=ex, direct=True,
                 signal_root=armed_dir,
                 runtime_path=tmp_path / "rt.json")
    return d, be, armed_dir


def test_direct_dry_run_no_order(direct_env):
    d, be, _ = direct_env
    d.dry_run = True
    rt = d.run_once()
    assert be.orders_sent == []
    assert rt["blocked"] == "dry_run"
    assert rt["exec_stats"]["submitted"] == 0


def test_direct_paused_no_order(direct_env):
    d, be, root = direct_env
    (root / "paused").write_text("stop", encoding="utf-8")
    rt = d.run_once()
    assert be.orders_sent == []
    assert "paused" in rt["blocked"]


def test_direct_not_armed_no_order(direct_env):
    """放行条不存在 → 不放行。

    注: 这里写空串而不是 unlink()。armed 判定是 "内容是否含当日日期",
    空文件与不存在的文件等价(都读不到当日日期), 既表达同一语义, 又避免
    依赖文件删除权限(某些沙箱/CI 会对 unlink 设守卫)。
    """
    d, be, root = direct_env
    (root / "real" / "armed.txt").write_text("", encoding="utf-8")
    rt = d.run_once()
    assert be.orders_sent == []
    assert "not_armed" in rt["blocked"]


def test_direct_armed_stale_date_no_order(direct_env):
    """放行条是昨天的 → 今天不放行。"""
    d, be, root = direct_env
    (root / "real" / "armed.txt").write_text("20260913", encoding="utf-8")
    rt = d.run_once()
    assert be.orders_sent == []
    assert "not_armed" in rt["blocked"]


def test_direct_armed_live_places_order(direct_env):
    d, be, _ = direct_env
    rt = d.run_once()
    assert rt["blocked"] == ""
    assert rt["armed"] is True
    assert len(be.orders_sent) >= 1
    o = be.orders_sent[0]
    assert o["code"] == "600900.SH"
    assert o["type"] == 24                    # STOCK_SELL
    assert o["volume"] % 100 == 0 and o["volume"] > 0
    assert rt["exec_stats"]["submitted"] == len(be.orders_sent)


def test_direct_second_round_is_idempotent(direct_env):
    """第二轮不再重复报单 —— 两条**互相独立**的机制, 都要钉住:

      1. 账本水位已推进 → 同一档不再产出意图(账本是这一层的前提);
      2. 万一同批 order_id 又出现(账本被重置/盘中升版, 见 test_state.py 的
         "正常发版窗口"), 执行器必须凭 order_id 拒重。

    旧版本用例只断言"订单数没变", 而它变绿靠的是**无条件记账**: 即使柜台
    一股没卖, 账本也会推进水位, 于是第二轮自然没有新意图。断言与机制脱钩,
    所以 C1 修完后必须按设计重写。
    """
    d, be, _ = direct_env
    first = d.run_once()
    n1 = len(be.orders_sent)
    assert n1 >= 1
    assert first["booked"] == n1                 # 只记已受理的
    assert first["ledger_theoretical"] is False  # 直连通道拿得到受理结果

    d.run_once()
    assert len(be.orders_sent) == n1             # 未新增(机制 1)

    # 机制 2: 强行重放同一批 order_id → 执行器必须整批拒
    replay = d.executor.execute(first["signals"], dry_run=False)
    assert [r["code"] for r in replay] == ["ALREADY_PLACED"] * len(replay)
    assert len(be.orders_sent) == n1


# ---------------------------------------------------------------- C1: 记账以受理为准

def test_direct_rejected_orders_are_not_booked(tmp_path):
    """柜台全拒(seq=-1) → 账本一个字都不能推进, 快照 booked 也要如实为 0。

    旧实现无条件 `_book(signals)`: exec_stats={'submitted':0,'failed':2} 的
    同时账本记 sold_today=1200 / filled_sell_units=2。

    **为什么那本假账会让下一轮真买**: 账本的 sold_today 是
    `check_net_exposure` 唯一的额度来源(sold_today 越大, 允许买回的越多)。
    凭空记 1200 股卖出 = 凭空发放 1200 股买入额度; 而且 `filled_sell_units`
    也被推到 2, 于是"这一档已经卖过了"→ 下一轮不再重卖, 只放买单。结果:
    账本净敞口 −1200(看起来已归位), 实际持仓 +1200 股且没有任何对应卖出。
    """
    be = FakeBackend(seq_fn=lambda n: -1)
    d, led = _env(tmp_path, be)
    rt = d.run_once()

    assert rt["exec_stats"]["submitted"] == 0
    assert rt["exec_stats"]["failed"] == 2
    sym = rt["ledger"]["symbols"]["600900.SH"]
    assert sym["sold_today"] == 0
    assert sym["filled_sell_units"] == 0
    assert sym["net_exposure"] == 0
    assert rt["booked"] == 0
    # 没有卖出额度 → 任何买回都必须被净敞口闸门拦下
    assert not tt_risk.check_net_exposure(
        "BUY", 600, sym["sold_today"], sym["bought_today"], 0).ok


def test_direct_books_only_accepted_subset(tmp_path):
    """部分受理: 只记被受理的那笔(第 1 笔 ok, 第 2 笔柜台拒)。"""
    be = FakeBackend(seq_fn=lambda n: 1000 if n == 1 else -1)
    d, led = _env(tmp_path, be)
    rt = d.run_once()

    assert rt["exec_stats"]["submitted"] == 1
    assert rt["exec_stats"]["failed"] == 1
    sym = rt["ledger"]["symbols"]["600900.SH"]
    assert sym["sold_today"] == rt["exec"][0]["volume"]
    assert sym["filled_sell_units"] == 1
    assert rt["booked"] == 1


def test_direct_rejected_sell_blocks_same_round_buy(tmp_path):
    """C1 的在途委托半边: 卖出被拒时, 本轮**不能**照发已经排好的买单。

    引擎的"先卖后买"把同轮卖出量预先算进了买单的净敞口额度 —— 那只在卖出
    真被受理后才成立。柜台全拒时若买单照发, 就是净持仓凭空 +1200 股。
    """
    be = FakeBackend(seq_fn=lambda n: -1)
    d, led = _env(tmp_path, be)
    rt = d.run_once()

    assert be.orders_sent, "卖单应已提交(只是被拒)"
    assert all(o["type"] == 24 for o in be.orders_sent), \
        "卖出未被受理, 柜台不该看到任何买单: %r" % (be.orders_sent,)
    assert "SELL_NOT_ACCEPTED" in [r["code"] for r in rt["exec"]]
    assert sum(v["bought_today"]
               for v in rt["ledger"]["symbols"].values()) == 0


def test_direct_accepted_sell_authorizes_same_round_buy(tmp_path):
    """反向: 卖出被受理时, 同轮买单照发且严格归位 —— 别把闸门做成恒闭。"""
    be = FakeBackend()                       # 全受理
    d, led = _env(tmp_path, be)
    rt = d.run_once()

    types = [o["type"] for o in be.orders_sent]
    assert types.count(24) == 2 and types.count(23) == 2
    assert types.index(24) < types.index(23), "必须先卖后买"
    sym = rt["ledger"]["symbols"]["600900.SH"]
    assert sym["sold_today"] == sym["bought_today"]      # 严格归位
    assert rt["booked"] == 4


def test_direct_without_executor_books_nothing(tmp_path):
    """未注入 executor → 日志说"跳过下单", 账本也必须一步不动。"""
    d, led = _env(tmp_path, FakeBackend(), executor=False)
    rt = d.run_once()

    assert rt.get("exec") is None
    assert rt["booked"] == 0
    assert sum(v["sold_today"]
               for v in rt["ledger"]["symbols"].values()) == 0


def test_direct_no_executor_leaves_ledger_untouched_over_rounds(tmp_path):
    """未注入 executor 时连跑两轮, 账本始终空白 —— 不会积累出买入额度。

    注: 引擎内部仍会按"先卖后买"产出买单意图(那是引擎的既定设计), 但执行器
    缺失 ⇒ 没有任何受理结果 ⇒ 账本一分不记 ⇒ 下一轮买单依旧被净敞口拦下。
    """
    d, led = _env(tmp_path, FakeBackend(), executor=False)
    d.run_once()
    rt = d.run_once()

    assert rt["booked"] == 0
    for v in rt["ledger"]["symbols"].values():
        assert (v["sold_today"], v["bought_today"]) == (0, 0)
        assert not tt_risk.check_net_exposure(
            "BUY", 600, v["sold_today"], v["bought_today"], 0).ok


def test_signal_channel_marks_ledger_theoretical(tmp_path):
    """信号文件通道拿不到受理结果 → 必须显式标注 ledger_theoretical。"""
    root = tmp_path / "signals"
    (root / "real").mkdir(parents=True, exist_ok=True)
    (root / "real" / "armed.txt").write_text("20260914", encoding="utf-8")
    cfg = tc.load(overrides={"dry_run": False, "env": "real",
                             "paper_total_asset": 500000.0,
                             "paper_positions": {"600900.SH": 5000}})
    feed = FakeFeed({"600900.SH": _snap("600900.SH", 28.50, 28.63,
                                        28.95, 28.30)})
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=_fixed_now)
    eng = TTEngine(cfg, led, feed=feed, now_fn=_fixed_now, force_paper=True)
    d = TTDaemon(cfg, ledger=led, engine=eng, dry_run=False,
                 now_fn=_fixed_now, signal_root=root,
                 runtime_path=tmp_path / "rt.json")
    rt = d.run_once()

    assert rt["blocked"] == ""
    assert rt["signals_written"] > 0
    assert rt["ledger_theoretical"] is True
    assert rt["booked"] == rt["signals_written"]
    assert list((root / "real" / "pending").glob("*.json"))
