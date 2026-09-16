# -*- coding: utf-8 -*-
"""新旧引擎对照: 搬家后的行为差异必须**恰好**是那一条已批准例外。

做法: 同一份确定性 FakeFeed + 同一份配置 + 同一个固定时钟, 分别喂给
tt.engine.TTEngine(旧) 与 ttcore.engine.TTEngine(新), 逐字段比对。

对照三个面:
  A. **决策**: plan() 全字段(intents / rejected / signals / symbols / counts ...)。
  B. **账本**: plan() 只"提议"不成交, 故两边按同一顺序走一遍 daemon._book 的动作
     (plan → 对每个已接受意图 record_fill + set_units → 再 plan),
     再逐字段比对 ledger.snapshot() 与落盘 state(含 FIFO 队列)。
  C. **归档**: 两边同样调用 archive_current()(旧包没有该方法, 调用被跳过),
     新侧产出的 tt_history.jsonl 行须与"参考侧账本口径反算的期望行"逐字段一致。

## 结论的正确读法(务必先看)

**不是"逐位相同"**。已实测存在 1 处决策级分歧, 且是**刻意保留**的:

  北交所 920xxx 的涨跌停比例。旧 tt/risk.py 走 shared.common.limit_ratio_for_code,
  其板块判定只有 ("8","4") → 920xxx 落到 ±10%; 新 ttcore/_vendor.py 用
  ("8","4","92") → ±30%。后果是实打实的: 920001.BJ 第 3 档(+10.5%)在旧侧被
  BAND_OUT 拒单, 在新侧正常放行 —— 存在旧代码会拒、新代码会下的档位。
  0.30 才是对的(北交所确为 ±30%), 0.10 会误杀合法委托, 属主项目侧潜在缺陷
  (见 ttcore/_vendor.py:8-10)。当前部署标的集不含 920xxx, 故该分歧今天潜伏。

  该分歧由 compare_bj() 钉成**断言**: 只允许它, 且只允许那一个档位;
  除它以外的任何分歧都会让本脚本以退出码 1 失败。

用法: python tt_solo/tools/compare_legacy.py     # 退出码 0 = 除上述例外外一致
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

# ---------------------------------------------------------------- 已知例外

# 北交所 920xxx: 旧侧 920xxx=10% 涨跌停 → 第3档(+10.5%)被判 BAND_OUT;
# 新侧 30% → 放行。这是唯一被允许的决策分歧, 且只允许这一个档位。
BJ_CASES = [
    # band_pct=3.5% → 第3档 = 中枢 +10.5%(卡在旧的 10% 之外, 在新的 30% 之内)
    ("920001.BJ", 10.50, 10.00, 11.20, 9.90, 10.60, 10.60, 4.0, 0.035),
]
BJ_OVERRIDES = {
    "dry_run": True,
    "paper_total_asset": 500000.0,
    "paper_positions": {"920001.BJ": 5000},
    "max_units_per_round": 3,
    "grid": {"band_mode": "sigma", "band_k": 1.0, "n_units": 5,
             "max_units": 3, "ref_mode": "prev_close", "sigma_window": 60},
    # 偏离闸门必须放得下第3档的固有偏离(3×3.5%=10.5%), 否则分歧会被它先拦下
    "risk": {"max_price_deviation_pct": 0.12},
    "symbols": [
        {"code": "920001.BJ", "name": "北交所样本", "enabled": True,
         "weight": 0.15, "band_pct": 3.5, "n_units": 5},
    ],
}

SANCTIONED_ORDER_ID = "TT_20260914_920001BJ_SELL_3"

# 挂钟字段: 只反映"何时跑的", 与行为无关, 比对前递归剔除。
#   at          —— record_fill 的事件时间戳(datetime.now(), 两侧各写各的)
#   updated_at  —— 账本落盘时间戳
#   runtime_at  —— 运行时快照时间戳(plan 里没有, 防御性保留)
#   archived_at —— 归档行的落盘时间戳(archive_current 用 datetime.now())
VOLATILE_KEYS = ("runtime_at", "updated_at", "archived_at", "at")


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


def build_snaps(cases=None):
    out = {}
    for code, last, lc, hi, lo, ma20, ma20p, r20, sig in (cases or CASES):
        out[code] = {
            "code": code,
            "tick": {"last": last, "open": lc, "high": hi, "low": lo,
                     "last_close": lc, "volume": 1e6, "amount": 3e7},
            "ind": {"last": last, "n": 80, "ma20": ma20, "ma20_prev": ma20p,
                    "r20_pct": r20, "sigma": sig, "trend_degree": 0.2},
            "source": "fake",
        }
    return out


def strip_volatile(d):
    """递归去掉挂钟时间字段(见 VOLATILE_KEYS)。

    必须递归: events 的每一条都带 at, 归档行带 archived_at —— 只剔顶层会让
    events 因"秒数不同"而处处不等, 把真正的行为差异淹掉。
    """
    def walk(x):
        if isinstance(x, dict):
            return {k: walk(v) for k, v in x.items()
                    if k not in VOLATILE_KEYS}
        if isinstance(x, list):
            return [walk(v) for v in x]
        return x
    return walk(json.loads(json.dumps(d, default=str)))


def diff_paths(old, new, path=""):
    """逐字段差异(点路径)。相同 → []。"""
    out = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(set(old) | set(new)):
            p = "%s.%s" % (path, k) if path else str(k)
            if k not in old:
                out.append("%s: 仅新侧有 = %r" % (p, new[k]))
            elif k not in new:
                out.append("%s: 仅旧侧有 = %r" % (p, old[k]))
            else:
                out += diff_paths(old[k], new[k], p)
    elif isinstance(old, list) and isinstance(new, list):
        if len(old) != len(new):
            out.append("%s: 长度 旧=%d 新=%d" % (path, len(old), len(new)))
        for i, (a, b) in enumerate(zip(old, new)):
            out += diff_paths(a, b, "%s[%d]" % (path, i))
    elif old != new:
        out.append("%s: 旧=%r 新=%r" % (path, old, new))
    return out


def diffs(label, old, new):
    """带标签的差异列表, 例如 'ledger.snapshot.symbols.600900.SH.trips: ...'。"""
    return ["%s.%s" % (label, p)
            for p in diff_paths(strip_volatile(old), strip_volatile(new))]


def leaf_paths(obj, prefix=""):
    if isinstance(obj, dict):
        for k in sorted(obj):
            p = "%s.%s" % (prefix, k) if prefix else str(k)
            for x in leaf_paths(obj[k], p):
                yield x
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            for x in leaf_paths(v, "%s[%d]" % (prefix, i)):
                yield x
    else:
        yield prefix


def leaf_count(obj):
    return sum(1 for _ in leaf_paths(obj))


# ---------------------------------------------------------------- 跑一侧

def _book(led, plan):
    """与 daemon._book 同款: 对每个已接受意图(信号)按挂单价做理论成交 + 推水位。

    plan() 只提议不记账, 账本对照必须显式走这一步, 否则两边都是零账本、
    比对毫无信息量。
    """
    for s in plan["signals"]:
        led.record_fill(s["stock_code"], s["action"], float(s.get("price") or 0),
                        int(s.get("volume") or 0), hhmm=plan.get("hhmm", ""),
                        reason=s.get("reason", ""), order_id=s["order_id"])
        if s.get("unit"):
            led.set_units(s["stock_code"], s["action"], int(s["unit"]))


def run_side(prefix, tmpdir, cases=None, overrides=None, cycles=2):
    """在指定包前缀下跑 cycles 轮 plan→记账, 再归档; 返回对照用的一切。

    返回 {'plans': [...], 'ledger': snapshot, 'state': 落盘 state,
          'history': [归档行...], 'ratio_920': 该侧 920xxx 涨跌停比例}
    """
    if prefix == "tt":
        from tt import config as cfgmod
        from tt.engine import TTEngine
        from tt.state import Ledger
        from tt.risk import limit_ratio_for_code
    else:
        from ttcore import config as cfgmod
        from ttcore.engine import TTEngine
        from ttcore.state import Ledger
        from ttcore._vendor import limit_ratio_for_code

    cfg = cfgmod.load(overrides=dict(overrides or OVERRIDES))
    led = Ledger(path=Path(tmpdir) / "state.json", now_fn=lambda: FIXED_NOW)
    led.load()
    eng = TTEngine(cfg, led, feed=FakeFeed(build_snaps(cases)),
                   now_fn=lambda: FIXED_NOW, force_paper=True)

    plans = []
    for _ in range(int(cycles)):
        plan = eng.plan()
        plans.append(plan)
        _book(led, plan)

    # 归档: 旧包没有这个方法(tt_solo 新增功能) → 调用被跳过, 不报错
    archive = getattr(led, "archive_current", None)
    if callable(archive):
        archive()

    hist = Path(tmpdir) / "tt_history.jsonl"
    rows = []
    if hist.exists():
        rows = [json.loads(ln) for ln in
                hist.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return {
        "plans": plans,
        "ledger": led.snapshot(),
        "state": led.state,
        "history": rows,
        "ratio_920": limit_ratio_for_code("920001.BJ"),
    }


# ---------------------------------------------------------------- 场景 A

def _expected_archive_row(led_snapshot):
    """按 archive_current 的口径, 从**参考侧账本快照**反算期望归档行。

    旧 tt/state.py 没有归档功能(archive_current 是 tt_solo 新增), 没有"旧侧行"
    可直接对; 改为与参考侧账本口径对齐 —— 归档算术一旦偏离账本事实即失败。
    """
    syms = led_snapshot["symbols"]
    return {
        "date": led_snapshot["date"],
        "sold_total": sum(int(v["sold_today"]) for v in syms.values()),
        "bought_total": sum(int(v["bought_today"]) for v in syms.values()),
        "trips": led_snapshot["total_trips"],
        "realized_pnl": led_snapshot["total_realized_pnl"],
        "trades": led_snapshot["daily_trades"],
        "symbols": {c: round(float(v["realized_pnl"]), 2)
                    for c, v in syms.items()},
    }


def compare_main(tmpdir):
    """场景 A(3 只标的 / 2 轮 plan→记账 / 归档)。返回 {'problems', 'evidence'}。"""
    a, b = Path(tmpdir) / "old", Path(tmpdir) / "new"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    old = run_side("tt", a)
    new = run_side("ttcore", b)

    problems = []
    for i, (op, np_) in enumerate(zip(old["plans"], new["plans"])):
        problems += diffs("plan[%d]" % i, op, np_)
    problems += diffs("ledger.snapshot", old["ledger"], new["ledger"])
    problems += diffs("ledger.state", old["state"], new["state"])

    rows = new["history"]
    if len(rows) != 1:
        problems.append("history: 期望恰好 1 行归档, 实际 %d 行"
                        "(archive_current 未生效?)" % len(rows))
    else:
        problems += diffs("history[0]",
                          _expected_archive_row(old["ledger"]), rows[0])

    sold = sum(int(v["sold_today"]) for v in new["ledger"]["symbols"].values())
    bought = sum(int(v["bought_today"])
                 for v in new["ledger"]["symbols"].values())
    evidence = {
        "cycles": len(new["plans"]),
        "counts": [dict(p["counts"], signals=len(p["signals"]))
                   for p in new["plans"]],
        "events": new["ledger"]["daily_trades"],
        "trips": new["ledger"]["total_trips"],
        "realized_pnl": new["ledger"]["total_realized_pnl"],
        "sold_total": sold,
        "bought_total": bought,
        "net_exposure": {c: v["net_exposure"]
                         for c, v in new["ledger"]["symbols"].items()},
        "history": rows[0] if rows else None,
        "fields": {
            "plan": sum(leaf_count(p) for p in new["plans"]),
            "ledger.snapshot": leaf_count(new["ledger"]),
            "ledger.state": leaf_count(new["state"]),
            "history[0]": leaf_count(rows[0]) if rows else 0,
        },
    }
    return {"problems": problems, "evidence": evidence}


# ---------------------------------------------------------------- 场景 B

def _without_exception(plan):
    """去掉被批准的那一个档位, 其余整份 plan 必须仍逐字段一致。"""
    p = dict(plan)
    for key in ("intents", "rejected", "signals"):
        p[key] = [x for x in plan[key]
                  if x.get("order_id") != SANCTIONED_ORDER_ID]
    p["counts"] = dict(plan["counts"])
    p["counts"]["intents"] = len(p["intents"])
    p["counts"]["rejected"] = len(p["rejected"])
    return p


def compare_bj(tmpdir):
    """场景 B(北交所 920xxx): 分歧必须**恰好**是那一条已批准例外。

    返回 {'problems', 'evidence'}。旧侧拒第3档 / 新侧放行; 除此以外零差异。
    """
    a, b = Path(tmpdir) / "old", Path(tmpdir) / "new"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)
    old = run_side("tt", a, BJ_CASES, BJ_OVERRIDES, cycles=1)
    new = run_side("ttcore", b, BJ_CASES, BJ_OVERRIDES, cycles=1)
    op, np_ = old["plans"][0], new["plans"][0]

    old_ok = {i["order_id"]: i for i in op["intents"]}
    new_ok = {i["order_id"]: i for i in np_["intents"]}
    old_rej = {i["order_id"]: i for i in op["rejected"]}
    new_rej = {i["order_id"]: i for i in np_["rejected"]}

    problems = []
    if set(new_ok) - set(old_ok) != {SANCTIONED_ORDER_ID}:
        problems.append("新侧多放行的档位不是唯一那条例外: %r"
                        % sorted(set(new_ok) - set(old_ok)))
    if set(old_ok) - set(new_ok):
        problems.append("有旧侧放行而新侧不放行的档位: %r"
                        % sorted(set(old_ok) - set(new_ok)))
    if set(old_rej) != {SANCTIONED_ORDER_ID}:
        problems.append("旧侧拒单不恰好是那一个档位: %r" % sorted(old_rej))
    if new_rej:
        problems.append("新侧仍有拒单: %r" % sorted(new_rej))
    if old["ratio_920"] != 0.10 or new["ratio_920"] != 0.30:
        problems.append("涨跌停比例根因变了: 旧侧 920xxx=%r(期望 0.1) "
                        "新侧=%r(期望 0.3)"
                        % (old["ratio_920"], new["ratio_920"]))
    if problems:
        return {"problems": problems, "evidence": _bj_evidence(old, new)}

    gl, gr = old_rej[SANCTIONED_ORDER_ID], new_ok[SANCTIONED_ORDER_ID]
    # 同一 order_id / 同一档位 / 同一档位价 / 同一股数 → 证明分歧只可能来自
    # 涨跌停闸门。(刻意不比 reason: 被拒与被接受的档位走的是两套措辞,
    # "档位3" vs "档位3 距中枢+10.50%" —— 那是展示文案, 不是行为参数。)
    for k in ("code", "side", "price", "volume", "order_id"):
        if gl.get(k) != gr.get(k):
            problems.append("例外档位的 %s 不一致: 旧=%r 新=%r"
                            % (k, gl.get(k), gr.get(k)))
    if (gl.get("meta") or {}).get("unit") != (gr.get("meta") or {}).get("unit"):
        problems.append("例外档位的档序号不一致: 旧=%r 新=%r"
                        % ((gl.get("meta") or {}).get("unit"),
                           (gr.get("meta") or {}).get("unit")))
    if gl.get("reject_code") != "BAND_OUT":
        problems.append("旧侧拒单码应为 BAND_OUT, 实际 %r" % gl.get("reject_code"))
    problems += diffs("plan(去例外后)", _without_exception(op),
                      _without_exception(np_))
    return {"problems": problems, "evidence": _bj_evidence(old, new)}


def _bj_evidence(old, new):
    op, np_ = old["plans"][0], new["plans"][0]
    old_rej = [i for i in op["rejected"] if i["order_id"] == SANCTIONED_ORDER_ID]
    new_ok = [i for i in np_["intents"] if i["order_id"] == SANCTIONED_ORDER_ID]
    return {
        "order_id": SANCTIONED_ORDER_ID,
        "old_counts": op["counts"], "new_counts": np_["counts"],
        "old_ratio": old["ratio_920"], "new_ratio": new["ratio_920"],
        "old_reject": old_rej[0] if old_rej else None,
        "new_intent": new_ok[0] if new_ok else None,
    }


# ---------------------------------------------------------------- 入口

def _print_main_evidence(ev):
    print("  场景A(600900.SH/600938.SH/601088.SH, %d 轮 plan→成交记账→日终归档):"
          % ev["cycles"])
    print("    plan 逐字段一致, 各轮 counts=%s"
          % " / ".join("intents=%d rejected=%d signals=%d"
                       % (c["intents"], c["rejected"], c["signals"])
                       for c in ev["counts"]))
    print("    账本(非空, 有区分度): 成交事件=%d trips=%d realized_pnl=%.2f "
          "sold_total=%d bought_total=%d"
          % (ev["events"], ev["trips"], ev["realized_pnl"],
             ev["sold_total"], ev["bought_total"]))
    print("    净敞口: %s" % ", ".join("%s=%+d" % (c, n)
                                       for c, n in ev["net_exposure"].items()))
    row = ev["history"] or {}
    print("    归档 tt_history.jsonl: date=%s sold_total=%s bought_total=%s "
          "trips=%s realized_pnl=%s trades=%s"
          % (row.get("date"), row.get("sold_total"), row.get("bought_total"),
             row.get("trips"), row.get("realized_pnl"), row.get("trades")))
    print("    逐字段比对规模: plan=%d 叶子, ledger.snapshot=%d, ledger.state=%d, "
          "history[0]=%d"
          % (ev["fields"]["plan"], ev["fields"]["ledger.snapshot"],
             ev["fields"]["ledger.state"], ev["fields"]["history[0]"]))
    print("    剔除字段(挂钟, 与行为无关): %s" % ", ".join(VOLATILE_KEYS))


def _print_bj_evidence(ev):
    print("  场景B(920001.BJ, 已知且刻意保留的唯一分歧):")
    print("    涨跌停比例: 旧=%s(shared.common 板块判定 \"8\"/\"4\") "
          "新=%s(ttcore 判定 \"8\"/\"4\"/\"92\")"
          % (ev["old_ratio"], ev["new_ratio"]))
    print("    counts: 旧 intents=%d rejected=%d / 新 intents=%d rejected=%d"
          % (ev["old_counts"]["intents"], ev["old_counts"]["rejected"],
             ev["new_counts"]["intents"], ev["new_counts"]["rejected"]))
    r = ev["old_reject"] or {}
    n = ev["new_intent"] or {}
    print("    旧侧拒单: %s %s 第%s档 价=%s 股=%s → %s"
          % (r.get("code"), r.get("side"), (r.get("meta") or {}).get("unit"),
             r.get("price"), r.get("volume"), r.get("reject_code")))
    print("    新侧放行: %s %s 第%s档 价=%s 股=%s (同一 order_id=%s)"
          % (n.get("code"), n.get("side"), (n.get("meta") or {}).get("unit"),
             n.get("price"), n.get("volume"), n.get("order_id")))
    print("    该例外已被断言: 只许这一处、只许这一个档位; 其余 plan 字段仍逐字段一致。")


def main():
    import tempfile
    with tempfile.TemporaryDirectory() as t:
        res_main = compare_main(Path(t) / "main")
        res_bj = compare_bj(Path(t) / "bj")

    problems = res_main["problems"] + res_bj["problems"]
    if problems:
        print("!! 新旧行为不一致 (%d 处):" % len(problems))
        for p in problems:
            print(" -", p)
        return 1

    print("对照通过 —— 但**不是**\"逐位相同\": 有一处刻意保留的例外。")
    print("  唯一例外(已批准, 非回归): 北交所 920xxx 的涨跌停比例修正")
    print("    旧 tt/risk.py → shared.common.limit_ratio_for_code 的板块判定只有 "
          "\"8\"/\"4\" → 920xxx 被当作 ±10%;")
    print("    新 ttcore/_vendor.py 用 \"8\"/\"4\"/\"92\" → ±30%(正确: 北交所确为 ±30%)。")
    print("    该修正刻意保留、不回退 —— 0.10 会误杀合法委托, 属主项目侧潜在缺陷;")
    print("    但它确实改变决策: 旧引擎会拒单、新引擎会放行的档位是存在的。")
    print("    ⇒ 别把这句读成\"迁移与原版逐位相同\"; 除该例外外的任何分歧都会"
          "以退出码 1 失败(见场景B断言)。")
    _print_main_evidence(res_main["evidence"])
    _print_bj_evidence(res_bj["evidence"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
