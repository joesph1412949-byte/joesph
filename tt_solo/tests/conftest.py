# -*- coding: utf-8 -*-
"""tt_solo 测试公共夹具。"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]      # D:/cc-joesph/tt_solo
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ttcore import config as tt_config          # noqa: E402
from ttcore.state import Ledger                 # noqa: E402


FIXED_NOW = datetime(2026, 9, 14, 10, 0, 0)


@pytest.fixture
def now_fn():
    return lambda: FIXED_NOW


@pytest.fixture
def cfg():
    """测试配置: 下面 overrides 里列出的键钉死, 但**不是**整份自足。

    tt_config.load() 是先读盘上 DEFAULT_CONFIG_PATH(ttcore/tt_config.json) 再深合并
    overrides —— 只有这里列出的键不受用户改盘影响, 没列出的仍从盘上继承。
    当前没钉的顶层键只有 version / env / account_id(无测试依赖: version 谁都不读,
    env/account_id 只在 plan() 输出里透传), 所以可以安全继承。
    grid / risk / session 三个子块已逐叶子钉全; paper_positions 只钉了测试用到的两个
    代码, 其余代码来自 DEFAULT_CONFIG(亦无测试依赖)。
    以后新增依赖某个配置键的测试, 记得在这里一并钉住。
    """
    return tt_config.load(overrides={
        "dry_run": True,
        "paper_total_asset": 500000.0,
        "paper_positions": {"600900.SH": 5000, "600938.SH": 3000},
        "max_units_per_round": 2,
        "grid": {"band_mode": "sigma", "band_k": 1.0, "n_units": 5,
                 "max_units": 5,
                 "ref_mode": "prev_close", "sigma_window": 60},
        # ponytail: session 必须钉住才谈得上自足 —— plan() 由 session_cfg 推 hhmm/phase,
        # test_engine.test_plan_full_shape 断言 "10:00"/"OPEN"; 值对齐 DEFAULT_CONFIG.session。
        "session": {"open_start": "09:30", "open_end": "14:55",
                    "converge_after": "14:30", "hard_stop_after": "14:57"},
        "risk": {"max_single_order_amount": 50000, "max_daily_trades": 20,
                 "max_daily_loss": 3000, "max_price_deviation_pct": 0.05,
                 "max_position_pct": 0.20, "max_net_buy_today_ratio": 0.0,
                 "max_consecutive_failures": 3, "max_slippage_pct": 0.03},
        "symbols": [
            {"code": "600900.SH", "name": "长江电力", "enabled": True,
             "weight": 0.18, "band_pct": 0.53, "n_units": 5,
             "switch": {"dev_max_pct": 4.0, "slope_max_pct": 0.3,
                        "r20_max_pct": 8.0}},
        ],
    })


@pytest.fixture
def ledger(tmp_path, now_fn):
    return Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)


class FakeFeed:
    """可编程行情: 直接给出 snapshot, 绕开真实取数。"""

    def __init__(self, snaps=None, last_source="fake"):
        self.snaps = snaps or {}
        self.last_source = last_source
        self.calls = []

    def set(self, code, ctx):
        self.snaps[code] = ctx

    def snapshot(self, code, count=80, sigma_window=60):
        self.calls.append(code)
        return self.snaps.get(code)

    def closes(self, code, count=80):
        c = self.snaps.get(code)
        return (c or {}).get("_closes")

    def ticks(self, codes):
        return {c: (self.snaps.get(c) or {}).get("tick", {}) for c in codes}


def make_snapshot(code="600900.SH", last=28.45, last_close=28.09,
                  high=None, low=None, open_=None, ma20=None, ma20_prev=None,
                  r20_pct=0.0, sigma=0.01, trend_degree=0.2):
    """构造 MarketFeed.snapshot() 形状的 dict。"""
    return {
        "code": code,
        "tick": {
            "last": last,
            "open": last_close if open_ is None else open_,
            "high": last if high is None else high,
            "low": last if low is None else low,
            "last_close": last_close,
            "volume": 1e6, "amount": 3e7,
        },
        "ind": {
            "last": last, "n": 80,
            "ma20": last if ma20 is None else ma20,
            "ma20_prev": last if ma20_prev is None else ma20_prev,
            "r20_pct": r20_pct, "sigma": sigma,
            "trend_degree": trend_degree,
        },
        "source": "fake",
    }


@pytest.fixture
def fake_feed():
    return FakeFeed


@pytest.fixture
def snap_factory():
    return make_snapshot


@pytest.fixture
def sample_dir():
    return Path(__file__).resolve().parents[1] / "ttcore" / "sample_data"
