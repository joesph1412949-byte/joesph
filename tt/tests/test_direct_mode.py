# -*- coding: utf-8 -*-
"""直连模式的 daemon 集成测试: 闸门串联 + 下单路径。全程 fake, 不碰真钱。

覆盖:
  - dry_run 拦下 → 零报单
  - paused 拦下 → 零报单
  - armed 缺失 → 零报单
  - armed 通过 + live → 真正报单(打到 fake backend)
"""
import pytest

from tt import config as tc
from tt.daemon import TTDaemon
from tt.engine import TTEngine
from tt.executor import DirectExecutor
from tt.state import Ledger

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
    """同一 order_id 第二轮不再重复报单。"""
    d, be, _ = direct_env
    d.run_once()
    n1 = len(be.orders_sent)
    d.run_once()
    assert len(be.orders_sent) == n1          # 未新增
