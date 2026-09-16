# -*- coding: utf-8 -*-
"""演练记账(--book-dry-run)测试: dry-run 下也能推进本地账本, 但绝不下单。

背景: `daemon.run_once()` 的 dry-run 分支原本不调 `_book()`, 于是账本
`sold_today` 恒为 0 → `check_net_exposure` 把每一笔买入都判超限 → dry-run
只能演练"高抛", 看不到"低吸回补"。`--book-dry-run` 补上这条腿。

覆盖:
  - 开启后: 账本 sold_today 推进, 且买入因此拿到净敞口额度
  - 默认关闭: 账本纹丝不动(既有行为不变)
  - 两种情况下都零报单
  - 账本隔离守卫: 必须显式 --state, 且不能指向实盘默认账本
"""
import argparse
from datetime import datetime

import pytest

from ttcore import config as tc
from ttcore import risk as tt_risk
from ttcore.daemon import STATE_PATH, TTDaemon, build_daemon
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


@pytest.fixture
def drill_env(tmp_path):
    """dry-run + 直连 executor + 演练记账, 全程 fake。"""
    cfg = tc.load(overrides={"dry_run": True, "env": "real"})
    snaps = {
        "600900.SH": _snap("600900.SH", 28.90, 28.63, 28.95, 28.60),
        "600938.SH": _snap("600938.SH", 33.80, 33.91, 33.85, 33.70),
        "601088.SH": _snap("601088.SH", 47.10, 47.20, 47.30, 47.00),
    }
    feed = FakeFeed(snaps)
    led = Ledger(path=tmp_path / "tt_state.drill.json", now_fn=_fixed_now)
    eng = TTEngine(cfg, led, feed=feed, now_fn=_fixed_now, force_paper=True)
    be = FakeBackend()
    ex = DirectExecutor(account_id="88869979", backend=be, now_fn=_fixed_now)
    d = TTDaemon(cfg, ledger=led, engine=eng, dry_run=True, now_fn=_fixed_now,
                 executor=ex, direct=True, signal_root=tmp_path / "signals",
                 runtime_path=tmp_path / "rt.json", book_dry_run=True)
    return d, be, led, feed


# ---------------------------------------------------------------- 记账行为

def test_book_dry_run_advances_ledger(drill_env):
    """开启演练记账 → 账本 sold_today 推进, 但零报单。"""
    d, be, _, _ = drill_env
    rt = d.run_once()
    assert rt["blocked"] == "dry_run"        # 闸门语义不变
    assert rt["book_dry_run"] is True
    assert rt["booked"] > 0                  # 记了账
    assert be.orders_sent == []              # 但绝无报单
    sold = sum(v["sold_today"] for v in rt["ledger"]["symbols"].values())
    assert sold > 0


def test_book_dry_run_off_by_default(drill_env):
    """默认不开 → 账本纹丝不动(既有行为不变)。"""
    d, be, _, _ = drill_env
    d.book_dry_run = False
    rt = d.run_once()
    assert rt["booked"] == 0
    assert be.orders_sent == []
    sold = sum(v["sold_today"] for v in rt["ledger"]["symbols"].values())
    assert sold == 0


def test_book_dry_run_grants_buy_allowance(drill_env):
    """记账的实际意义: sold_today 有值 → 买入腿才拿得到净敞口额度。

    这正是补齐的那条腿 —— 不开记账时 sold_today 恒为 0, 任何买入都被
    NET_EXPOSURE 拦死; 开记账后闸门才放得行。
    """
    d, be, _, _ = drill_env
    d.run_once()
    sold = sum(v["sold_today"] for v in d.ledger.snapshot()["symbols"].values())
    assert sold > 0, "演练记账应推进 sold_today"
    assert be.orders_sent == []
    v = tt_risk.check_net_exposure("BUY", min(sold, 100), sold, 0, 0)
    assert v.ok, "有卖出额度后同量买入应放行"


def test_book_dry_run_second_round_still_no_order(drill_env):
    """多轮演练始终不下单。"""
    d, be, _, _ = drill_env
    d.run_once()
    d.run_once()
    assert be.orders_sent == []


# ---------------------------------------------------------------- 账本隔离守卫

def _args(**kw):
    base = dict(live=False, sample=True, direct=False, state=None,
                signal_root=None, env=None, fake_now=True,
                book_dry_run=False, once=True, interval=5.0, rounds=None)
    base.update(kw)
    return argparse.Namespace(**base)


def test_guard_requires_explicit_state():
    """--book-dry-run 不给 --state → 拒绝启动(fail-closed)。"""
    with pytest.raises(SystemExit) as ei:
        build_daemon(_args(book_dry_run=True, state=None))
    assert "--state" in str(ei.value)


def test_guard_rejects_default_state():
    """--book-dry-run 指向实盘默认账本 → 拒绝启动。"""
    with pytest.raises(SystemExit) as ei:
        build_daemon(_args(book_dry_run=True, state=str(STATE_PATH)))
    assert "默认账本" in str(ei.value)


def test_guard_allows_separate_state(tmp_path):
    """--book-dry-run + 独立 --state → 正常构建, 且 runtime 快照一并隔离。"""
    p = tmp_path / "tt_state.drill.json"
    d = build_daemon(_args(book_dry_run=True, state=str(p)))
    assert d.book_dry_run is True
    assert d.dry_run is True
    # runtime 快照不能落回面板读的那份, 否则演练数据会盖掉它
    assert d.runtime_path.name == "tt_state.drill.runtime.json"


def test_without_flag_no_guard():
    """不开演练记账时不给 --state 也正常(不影响既有用法)。"""
    d = build_daemon(_args(book_dry_run=False, state=None))
    assert d.book_dry_run is False
    assert d.dry_run is True
