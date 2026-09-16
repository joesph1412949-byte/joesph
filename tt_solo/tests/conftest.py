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

    tt_config.load() 的顺序是 DEFAULT_CONFIG → 盘上 tt_config.json →
    overrides 逐层深合并, 所以没钉的键仍从盘上文件继承(用户可改)。
    钉住的键**除 paper_positions 外**都对齐部署值 ttcore/tt_config.json —— 测试因此
    跑在生产配置下, 又不受用户改那份文件影响。
    当前没钉的顶层键只剩 version(没有任何代码读它; 状态文件里的 version 是另一码事)。
    grid / risk / session 三个子块已逐叶子钉全。其中 grid 的 n_units=5 配
    max_units=3 就是部署值, 也是 config 校验里写明的**故意解耦**: n_units 定阶梯
    深度与每档金额分母, max_units 定日内实际用几档 —— 底仓只够 3 档时留 n_units=5,
    每档金额才维持 1/5。
    paper_positions **故意不对齐部署值**: 部署有效值是 DEFAULT_CONFIG 的 1000/700
    (两份 JSON 都没写这个键), 这里钉 5000/3000, 使纸面底仓 ≈ 单档 600 股的 8 倍,
    免得 can_use 把卖出量裁成缩量单。这只是余量, 不是现有测试的硬需求 ——
    实际去掉这条钉子改用 1000/700, 154 项仍全过。
    它钉了两个代码: 600900.SH(测试唯一用到的), 和 600938.SH(没有测试读它,
    留着只为把纸面账户形状固定住); 其余代码(601088.SH 等)继承自 DEFAULT_CONFIG。
    以后新增依赖某个配置键的测试, 记得在这里一并钉住。
    """
    return tt_config.load(overrides={
        "env": "real",
        "dry_run": True,
        "account_id": "",
        "paper_total_asset": 500000.0,
        "paper_positions": {"600900.SH": 5000, "600938.SH": 3000},
        "max_units_per_round": 2,
        "grid": {"band_mode": "sigma", "band_k": 1.0, "n_units": 5,
                 "max_units": 3,
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
