# -*- coding: utf-8 -*-
"""新旧引擎决策对照: 证明 tt_solo 搬家后行为零变化。

做法: 同一份确定性 FakeFeed + 同一份配置 + 同一个固定时钟, 分别喂给
tt.engine.TTEngine(旧) 与 ttcore.engine.TTEngine(新), 逐字段比对 plan()
输出与账本快照。任一处不同即非零退出。

用法: python tt_solo/tools/compare_legacy.py     # 退出码 0=一致
"""
import json
import sys
from datetime import datetime
from pathlib import Path

TT_SOLO = Path(__file__).resolve().parents[1]        # tt_solo/
REPO = TT_SOLO.parent                                 # D:/cc-joesph
for p in (str(REPO), str(TT_SOLO)):
    if p not in sys.path:
        sys.path.insert(0, p)

FIXED_NOW = datetime(2026, 9, 14, 10, 0, 0)

# 覆盖多标的 / 多开关态 / 多带宽的样本, 让对照真正有区分度
CASES = [
    # (code, last, last_close, high, low, ma20, ma20_prev, r20, sigma)
    ("600900.SH", 28.63, 28.45, 28.80, 28.20, 28.20, 28.10, 3.0, 0.0090),
    ("600938.SH", 33.91, 34.02, 34.60, 33.40, 33.10, 32.80, 5.5, 0.0250),
    ("601088.SH", 47.20, 47.42, 48.10, 46.90, 46.50, 46.30, 4.0, 0.0140),
]


class FakeFeed:
    def __init__(self, snaps):
        self.snaps = snaps
        self.last_source = "fake"

    def snapshot(self, code, count=80, sigma_window=60):
        return self.snaps.get(code)

    def closes(self, code, count=80):
        return None

    def ticks(self, codes):
        return {c: (self.snaps.get(c) or {}).get("tick", {}) for c in codes}


def build_snaps():
    out = {}
    for code, last, lc, hi, lo, ma20, ma20p, r20, sig in CASES:
        out[code] = {
            "code": code,
            "tick": {"last": last, "open": lc, "high": hi, "low": lo,
                     "last_close": lc, "volume": 1e6, "amount": 3e7},
            "ind": {"last": last, "n": 80, "ma20": ma20, "ma20_prev": ma20p,
                    "r20_pct": r20, "sigma": sig, "trend_degree": 0.2},
            "source": "fake",
        }
    return out


OVERRIDES = {
    "dry_run": True,
    "paper_total_asset": 500000.0,
    "paper_positions": {"600900.SH": 5000, "600938.SH": 3000,
                        "601088.SH": 2000},
    "max_units_per_round": 2,
    "grid": {"band_mode": "sigma", "band_k": 1.0, "n_units": 5,
             "max_units": 3, "ref_mode": "prev_close", "sigma_window": 60},
    "symbols": [
        {"code": "600900.SH", "name": "长江电力", "enabled": True,
         "weight": 0.18, "band_pct": 0.53, "n_units": 5},
        {"code": "600938.SH", "name": "中国海油", "enabled": True,
         "weight": 0.15, "band_pct": 1.65, "n_units": 5},
        {"code": "601088.SH", "name": "中国神华", "enabled": True,
         "weight": 0.09, "band_pct": 1.4, "n_units": 5},
    ],
}


def run_side(prefix, tmpdir):
    """在指定包前缀下跑一轮, 返回 (plan, ledger_snapshot)。"""
    if prefix == "tt":
        from tt import config as cfgmod
        from tt.engine import TTEngine
        from tt.state import Ledger
    else:
        from ttcore import config as cfgmod
        from ttcore.engine import TTEngine
        from ttcore.state import Ledger

    cfg = cfgmod.load(overrides=dict(OVERRIDES))
    led = Ledger(path=Path(tmpdir) / "state.json", now_fn=lambda: FIXED_NOW)
    led.load()
    eng = TTEngine(cfg, led, feed=FakeFeed(build_snaps()),
                   now_fn=lambda: FIXED_NOW, force_paper=True)
    plan = eng.plan()
    return plan, led.snapshot()


def strip_volatile(d):
    """去掉与行为无关的字段(路径/时间戳/来源标签)。"""
    d = json.loads(json.dumps(d, default=str))
    for k in ("runtime_at", "updated_at", "at"):
        d.pop(k, None)
    return d


def main():
    import tempfile
    with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
        old_plan, old_led = run_side("tt", a)
        new_plan, new_led = run_side("ttcore", b)

    diffs = []
    for label, old, new in (("plan", old_plan, new_plan),
                            ("ledger", old_led, new_led)):
        o, n = strip_volatile(old), strip_volatile(new)
        if o != n:
            keys = sorted(set(o) | set(n))
            for k in keys:
                if o.get(k) != n.get(k):
                    diffs.append("%s.%s:\n  old=%r\n  new=%r"
                                 % (label, k, o.get(k), n.get(k)))

    if diffs:
        print("!! 新旧行为不一致 (%d 处):" % len(diffs))
        for d in diffs:
            print(" -", d)
        return 1

    print("OK 新旧决策完全一致")
    print("   intents=%d rejected=%d signals=%d"
          % (new_plan["counts"]["intents"], new_plan["counts"]["rejected"],
             len(new_plan["signals"])))
    print("   标的: %s" % ", ".join(
        "%s=%s" % (s["code"], s.get("switch", "?"))
        for s in new_plan["symbols"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
