# 板块综合评分链（sector_momentum v5）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地"板块综合评分链"——每个申万板块打 0-100 综合分（动量35%/资金流25%/拥挤度30%/宏观10%），回测选股时板块分>75 才买、单笔仓位随分数加成（每高10分相对+5%，封顶45%），并透传到实盘信号。

**Architecture:** 新建纯计算模块 `prism/sector_score.py`（输入 `_slice_mkt` 切片，输出 `{板块码: {"score", "parts"}}`，缺数据权重归一化、全缺 fail-closed 不买）。回测引擎 `backtest.py` 两处挂钩（`_pick` 过滤+记分、`_simulate_equity` 仓位乘数）；实盘 `engine.run_screen` 同语义注入、`trader.generate_signals` 信号透传。配置全在策略 JSON `sector_score` 块。

**Tech Stack:** Python 3.12 标准库 + pytest（无新依赖）；复用现有 registry/engine/backtest 体系。

## Global Constraints

- 设计文档: `docs/superpowers/specs/2026-08-31-sector-score-chain-design.md`（公式以此为准）
- 权重固定: momentum 0.35 / flow 0.25 / crowding 0.30 / macro 0.10；缺项权重按剩余项比例归一化
- 美债分项: `US10Y` 20日变化是**百分点差值**（`close[-1] - close[-21]`），**不得 ×100**（v4 踩过的坑）
- 拥挤度是反向指标: `score = clamp(100 − max(0, ratio−1)×40, 0, 100)`，ratio = 近5日均额 ÷ 近240日均额
- 门槛判定**严格大于**: `score > threshold`（默认 75）；无评分 → 不买（fail-closed）
- 仓位乘数: `min(1 + (score−threshold)/10 × step, cap_ratio/position_ratio)`；评分关闭时恒 1.0
- `_pick` 返回从 4 元组变为 **5 元组** `(code, boards, theme, composite, sec_score)`——所有解包处同步更新
- 全部测试离线（不碰网络）；pytest 必须加 `--import-mode=importlib --basetemp=D:\cc-joesph\pt_btNN`（沙箱拒绝系统 Temp）
- 跑 prism_web/strategy_web 测试前先删 `D:\QMT_SIGNALS\paused`（用户服务会重建，只删文件不动服务）
- 控制台 GBK: 任何打印中文的一次性脚本需 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")`
- git 只本地 commit；**push 必须先问用户**
- 不改卖出规则（trailing/止损/持有期维持 v4）；不动 `D:\QMT_SIGNALS` 下任何服务

---

### Task 1: 评分模块 `prism/sector_score.py`

> ⚠️ 本任务代码块与最终实现有 7 处偏离（测试数据/口径修正），逐条验算记录见
> `.superpowers/sdd/task-1-report.md` §4——以实现 + 测试 + 该报告为准，勿照抄本节代码。

**Files:**
- Create: `prism/sector_score.py`
- Test: `prism/tests/test_sector_score.py`（新建）

**Interfaces:**
- Consumes: mkt 切片结构 `{"sector": {码: {"dates","close","amount"?}}, "sector_flow": {码: {"dates","main_net_in"}}, "global": {"NDX"/"US10Y"/"VIX": {"dates","close"}}}`（由 `_slice_mkt` 产出，Task 2 使用）
- Produces: `compute_scores(mkt) -> {板块码: {"score": float, "parts": {"momentum"/"flow"/"crowding"/"macro": float|None}}}`；`load_config(strategy) -> {"enabled": bool, "threshold": float, "step": float, "cap_ratio": float|None}`；`position_multiplier(score, cfg, base_ratio) -> float`。Task 2/3 依赖这三个函数签名。

- [ ] **Step 1: 写失败测试**

创建 `prism/tests/test_sector_score.py`（全离线，无 fixture 需求——不碰文件系统/注册表）:

```python
# -*- coding: utf-8 -*-
"""板块综合评分模块测试 — 全离线, 只测纯计算。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism import sector_score as ss


def _sector_rec(closes, amounts=None, dates=None):
    n = len(closes)
    rec = {"dates": dates or ["2026-07-%02d" % (i + 1) for i in range(n)],
           "close": closes}
    if amounts is not None:
        rec["amount"] = amounts
    return rec


# ---------------- 动量分 ----------------

def test_momentum_full_when_r5_6pct_and_r10_10pct():
    # 11根: 首根100 → 末根110+ (r10=10% 满分, r5>6% 满分)
    closes = [100.0 * (1.01 ** i) for i in range(11)]   # r5≈5.1%, r10≈10.5%
    closes[-6] = closes[-7] * 1.06                       # 保证 r5≥6%
    assert ss._momentum_part(_sector_rec(closes)) == 100.0


def test_momentum_flat_is_zero():
    closes = [100.0] * 11
    assert ss._momentum_part(_sector_rec(closes)) == 0.0


def test_momentum_insufficient_data_is_none():
    assert ss._momentum_part(_sector_rec([100.0, 101.0])) is None


# ---------------- 资金流分 ----------------

def test_flow_full_at_5yi_inflow():
    rec = {"main_net_in": [5e8] * 5}
    assert ss._flow_part(rec) == 100.0


def test_flow_zero_is_50():
    assert ss._flow_part({"main_net_in": [0.0] * 5}) == 50.0


def test_flow_minus_5yi_is_zero():
    assert ss._flow_part({"main_net_in": [-1e8] * 5}) == 0.0   # -5亿→0


def test_flow_missing_is_none():
    assert ss._flow_part({}) is None
    assert ss._flow_part({"main_net_in": [None] * 5}) is None


# ---------------- 拥挤度分(反向) ----------------

def test_crowding_shrink_volume_full():
    rec = _sector_rec([100.0] * 12, amounts=[1e9] * 12)
    assert ss._crowding_part(rec) == 100.0        # ratio=1 → 100


def test_crowding_2x_is_60():
    amounts = [1e9] * 12
    amounts[-5:] = [2e9] * 5
    rec = _sector_rec([100.0] * 12, amounts=amounts)
    assert ss._crowding_part(rec) == 60.0         # ratio=2 → 100-40=60


def test_crowding_3_5x_is_zero():
    amounts = [1e9] * 12
    amounts[-5:] = [3.5e9] * 5
    rec = _sector_rec([100.0] * 12, amounts=amounts)
    assert ss._crowding_part(rec) == 0.0


def test_crowding_needs_amount_history():
    rec = _sector_rec([100.0] * 12, amounts=[1e9] * 3)
    assert ss._crowding_part(rec) is None         # <10日 → 缺失


# ---------------- 宏观分 ----------------

def _glob(ndx=None, us10y=None, vix=None):
    g = {}
    if ndx:
        g["NDX"] = {"dates": ["d"] * len(ndx), "close": ndx}
    if us10y:
        g["US10Y"] = {"dates": ["d"] * len(us10y), "close": us10y}
    if vix:
        g["VIX"] = {"dates": ["d"] * len(vix), "close": vix}
    return g


def test_macro_ndx_up1pct_full():
    assert ss._macro_part(_glob(ndx=[100.0, 101.0])) == 100.0


def test_macro_ndx_down1pct_zero():
    assert ss._macro_part(_glob(ndx=[100.0, 99.0])) == 0.0


def test_macro_us10y_percent_point_not_times_100():
    # 21天: 前20天4.0, 末天5.3 → 变化1.3个百分点 → 0 分(若误×100 会异常)
    us = [4.0] * 20 + [5.3]
    assert ss._macro_part(_glob(us10y=us)) == 0.0
    us2 = [4.0] * 21
    assert ss._macro_part(_glob(us10y=us2)) == 100.0   # 变化0 ≤0.3 → 满分


def test_macro_vix_levels():
    assert ss._macro_part(_glob(vix=[15.0])) == 100.0
    assert ss._macro_part(_glob(vix=[25.0])) == 0.0


def test_macro_all_missing_is_none():
    assert ss._macro_part({}) is None


# ---------------- compute_scores 综合与降级 ----------------

def test_compute_scores_renormalizes_missing_flow():
    # 板块: 动量满分(11根涨10%+), 拥挤满分(平量), 宏观满分(纳指+1%),
    # 资金流缺失 → 权重归一 (0.35+0.30+0.10)=0.75 → score=100
    closes = [100.0 * (1.011 ** i) for i in range(11)]
    mkt = {"sector": {"801110": _sector_rec(closes, amounts=[1e9] * 11)},
           "global": _glob(ndx=[100.0, 101.0])}
    out = ss.compute_scores(mkt)
    assert out["801110"]["score"] == 100.0
    assert out["801110"]["parts"]["flow"] is None


def test_compute_scores_weighted_mix():
    # 动量100 拥挤0 宏观0(无global) 资金流缺失 → 35/75×100 = 46.7
    closes = [100.0 * (1.011 ** i) for i in range(11)]
    amounts = [1e9] * 7 + [3.5e9] * 4       # 近5日3.5倍 → 拥挤0分
    mkt = {"sector": {"801110": _sector_rec(closes, amounts=amounts)}}
    out = ss.compute_scores(mkt)
    assert out["801110"]["score"] == 46.7


def test_compute_scores_no_data_sector_absent():
    out = ss.compute_scores({"sector": {"801999": _sector_rec([])}})
    assert "801999" not in out              # fail-closed: 无K线 → 无评分
    assert ss.compute_scores({}) == {}
    assert ss.compute_scores(None) == {}


# ---------------- load_config / position_multiplier ----------------

def test_load_config_defaults_disabled():
    cfg = ss.load_config({})
    assert cfg == {"enabled": False, "threshold": 75.0,
                   "step": 0.05, "cap_ratio": None}


def test_load_config_full_block():
    cfg = ss.load_config({"sector_score": {
        "enabled": True, "threshold": 80,
        "position": {"step": 0.02, "cap_ratio": 0.5}}})
    assert cfg == {"enabled": True, "threshold": 80.0,
                   "step": 0.02, "cap_ratio": 0.5}


def test_position_multiplier_ramp():
    cfg = {"enabled": True, "threshold": 75.0, "step": 0.05,
           "cap_ratio": None}
    assert ss.position_multiplier(76, cfg, 0.30) == 1.05
    assert ss.position_multiplier(85, cfg, 0.30) == 1.05   # (85-75)/10=1 步
    assert ss.position_multiplier(100, cfg, 0.30) == 1.125
    assert ss.position_multiplier(75, cfg, 0.30) == 0.0    # 严格大于
    assert ss.position_multiplier(74, cfg, 0.30) == 0.0
    assert ss.position_multiplier(None, cfg, 0.30) == 0.0


def test_position_multiplier_cap_binds():
    cfg = {"enabled": True, "threshold": 75.0, "step": 0.05,
           "cap_ratio": 0.21}
    # base 0.20 → 相对乘数封顶 0.21/0.20 = 1.05
    assert ss.position_multiplier(100, cfg, 0.20) == 1.05
```

- [ ] **Step 2: 跑测试确认失败**

```
python -m pytest prism\tests\test_sector_score.py -v --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt61
```
预期: 全部 FAIL（`ModuleNotFoundError: No module named 'prism.sector_score'`）。

- [ ] **Step 3: 实现模块**

创建 `prism/sector_score.py`:

```python
# -*- coding: utf-8 -*-
"""板块综合评分(0-100): 动量35% + 资金流25% + 拥挤度30% + 宏观10%。

设计文档: docs/superpowers/specs/2026-08-31-sector-score-chain-design.md
- 纯计算模块: 只依赖 mkt 切片(dict), 不碰网络/缓存/registry。
- 防未来函数: 调用方必须传 _slice_mkt(mkt, asof) 之后的切片(只见当日及之前)。
- 缺数据降级: 分项 None → 权重按剩余分项比例归一化; 全缺 → 无评分(fail-closed)。
- 门槛严格大于: score > threshold(默认75)才可买; 无评分不买。
"""
DEFAULT_WEIGHTS = {"momentum": 0.35, "flow": 0.25,
                   "crowding": 0.30, "macro": 0.10}
DEFAULT_THRESHOLD = 75.0
DEFAULT_STEP = 0.05
DEFAULT_CAP_RATIO = None


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _momentum_part(rec):
    """板块K线 {close} → 动量分或 None。r5(6根)/r10(11根) 各半。"""
    closes = [c for c in (rec.get("close") or []) if c]
    subs = []
    if len(closes) >= 6:
        r5 = (closes[-1] / closes[-6] - 1.0) * 100.0
        subs.append(_clamp(r5 / 6.0 * 100.0))
    if len(closes) >= 11:
        r10 = (closes[-1] / closes[-11] - 1.0) * 100.0
        subs.append(_clamp(r10 / 10.0 * 100.0))
    if not subs:
        return None
    return sum(subs) / len(subs)


def _flow_part(rec):
    """资金流 {main_net_in} → 资金流分或 None。近5日合计, +5亿→100。"""
    vals = [v for v in (rec.get("main_net_in") or [])[-5:]
            if v is not None]
    if not vals:
        return None
    yi = sum(vals) / 1e8
    return _clamp(50.0 + 10.0 * yi)


def _crowding_part(rec):
    """板块K线 {amount} → 拥挤度分(反向)或 None。近5日均额/近240日均额。"""
    amounts = [a for a in (rec.get("amount") or []) if a]
    if len(amounts) < 10:
        return None
    recent = amounts[-5:]
    numer = sum(recent) / len(recent)
    base = amounts[-240:]
    denom = sum(base) / len(base)
    if not denom:
        return None
    ratio = numer / denom
    return _clamp(100.0 - max(0.0, ratio - 1.0) * 40.0)


def _macro_part(glob):
    """global 段 {NDX/US10Y/VIX: {close}} → 宏观分(三者均)或 None。"""
    subs = []
    ndx = [c for c in ((glob.get("NDX") or {}).get("close") or []) if c]
    if len(ndx) >= 2 and ndx[-2]:
        pct = (ndx[-1] / ndx[-2] - 1.0) * 100.0
        subs.append(_clamp(50.0 + pct * 50.0))
    us = [c for c in ((glob.get("US10Y") or {}).get("close") or []) if c]
    if len(us) >= 21:
        chg = us[-1] - us[-21]      # 百分点差值, 勿 ×100!
        subs.append(_clamp(100.0 - max(0.0, chg - 0.3) * 100.0))
    vix = [c for c in ((glob.get("VIX") or {}).get("close") or []) if c]
    if vix:
        subs.append(_clamp(100.0 - max(0.0, vix[-1] - 20.0) * 20.0))
    if not subs:
        return None
    return sum(subs) / len(subs)


def compute_scores(mkt):
    """mkt 切片 → {板块码: {"score": float, "parts": {...}}}。

    分项缺数据 → 剔除并把权重按剩余项比例归一化; 全缺 → 该板块无评分
    (不进返回 dict, 调用方 fail-closed 不买)。"""
    if not isinstance(mkt, dict) or not mkt:
        return {}
    sectors = mkt.get("sector") or {}
    flows = mkt.get("sector_flow") or {}
    glob = mkt.get("global") or {}
    macro = _macro_part(glob)
    out = {}
    for code, rec in sectors.items():
        rec = rec or {}
        parts = {"momentum": _momentum_part(rec),
                 "flow": _flow_part(flows.get(code) or {}),
                 "crowding": _crowding_part(rec),
                 "macro": macro}
        avail = {k: v for k, v in parts.items() if v is not None}
        if not avail:
            continue
        wsum = sum(DEFAULT_WEIGHTS[k] for k in avail)
        score = sum(avail[k] * DEFAULT_WEIGHTS[k] for k in avail) / wsum
        out[str(code)] = {"score": round(score, 1),
                          "parts": {k: (round(v, 1) if v is not None
                                        else None)
                                    for k, v in parts.items()}}
    return out


def load_config(strategy):
    """strategy dict → 规范化评分配置(缺省关闭)。"""
    raw = (strategy or {}).get("sector_score") or {}
    pos = raw.get("position") or {}
    cap = pos.get("cap_ratio")
    return {"enabled": bool(raw.get("enabled")),
            "threshold": float(raw.get("threshold", DEFAULT_THRESHOLD)),
            "step": float(pos.get("step", DEFAULT_STEP)),
            "cap_ratio": float(cap) if cap is not None else None}


def position_multiplier(score, cfg, base_ratio):
    """score → 单笔仓位乘数(相对 position_ratio)。

    每高 threshold 10 分相对 +step; 封顶 cap_ratio/base_ratio(绝对仓位上限);
    score None 或 ≤ threshold → 0.0(不该买)。评分关闭时调用方须用 1.0。"""
    threshold = cfg.get("threshold", DEFAULT_THRESHOLD)
    if score is None or score <= threshold:
        return 0.0
    mult = 1.0 + (score - threshold) / 10.0 * cfg.get("step", DEFAULT_STEP)
    cap = cfg.get("cap_ratio")
    if cap and base_ratio > 0:
        mult = min(mult, cap / base_ratio)
    return mult
```

- [ ] **Step 4: 跑测试确认全绿**

```
python -m pytest prism\tests\test_sector_score.py -v --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt61
```
预期: 22 passed。

- [ ] **Step 5: Commit**

```bash
git add prism/sector_score.py prism/tests/test_sector_score.py
git commit -m "feat(prism): 板块综合评分模块(动量35/资金流25/拥挤30/宏观10, 缺数据归一化)"
```

---

### Task 2: 回测引擎挂钩（过滤 + 仓位乘数）

**Files:**
- Modify: `prism/backtest.py`（`_pick` 约 198-259 行、`run` 约 280-340 行、`_simulate_equity` 约 414-469 行）
- Modify: `prism/tests/test_backtest_mkt.py`（追加测试）
- Import: 文件顶部加 `from prism import sector_score`

**Interfaces:**
- Consumes: Task 1 的 `sector_score.load_config/compute_scores/position_multiplier`。
- Produces: `_pick` 返回 **5 元组** `(code, boards, theme, composite, sec_score)`（sec_score: float|None）；trade dict 新增 `"sector_score"` 与 `"pos_mult"` 键；`_simulate_equity` 按 `pos_mult` 放大单笔投入。Task 3 不依赖本任务（实盘走 run_screen 独立实现）。

- [ ] **Step 1: 写失败测试**

在 `prism/tests/test_backtest_mkt.py` 末尾追加:

```python
# ---------------- v5 板块评分链 ----------------

def _mk_strategy_v5(step=0.05, cap=None, enabled=True, for_factor="SEC1"):
    s = _mk_strategy(for_factor)
    s["sector_score"] = {"enabled": enabled, "threshold": 75,
                         "position": {"step": step, "cap_ratio": cap}}
    return s


def _v5_mkt():
    """板块11根K线(动量满分=100: r5=10%≥6, r10=10%≥10) + 无资金流/宏观
    (降级为仅动量分项, score=100)。6-25 起 11 根 → 选股日 7-05 切片含 7-05。"""
    closes = [100.0] * 6 + [106.0, 107.0, 108.0, 109.0, 110.0]
    return _mkt_snapshot_dates(closes, start="20260625")


def test_sector_score_gate_filters_low_sector():
    """板块分 ≤75 → 剔除(fail-closed: 本例动量满分应通过, 反例看下个测试)。"""
    s = load_strategy(_mk_strategy_v5())
    zf, kf, sector_map = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 5),
                 mkt=_v5_mkt(), sector_map=sector_map)
    assert rep["trades"] == 1
    t = rep["trade_log"][0]
    assert t["sector_score"] is not None and t["sector_score"] > 75
    assert t["pos_mult"] > 1.0            # 满分 → 乘数>1


def test_sector_score_fail_closed_without_data():
    """mkt 无数据 → 板块无评分 → 不买(即使 SEC1 因子可命中)。"""
    s = load_strategy(_mk_strategy_v5())
    zf, kf, sector_map = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 5),
                 mkt=_mkt_snapshot([100.0, 101.0]), sector_map=sector_map)
    assert rep["trades"] == 0             # 仅2根K线 → 动量None+拥挤None → 无分


def test_sector_score_disabled_keeps_old_behavior():
    """enabled=false → 不过滤、pos_mult=1.0、sec_score=None。"""
    s = load_strategy(_mk_strategy_v5(enabled=False))
    zf, kf, sector_map = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 5),
                 mkt=_mkt_snapshot([100.0, 101.0]), sector_map=sector_map)
    assert rep["trades"] == 1             # 评分关闭(SEC1两连阳命中) + 无门槛
    assert rep["trade_log"][0]["sector_score"] is None
    assert rep["trade_log"][0]["pos_mult"] == 1.0


def test_pick_returns_5tuple_with_score():
    s = load_strategy(_mk_strategy_v5())
    zf, kf, sector_map = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    picked = bt._pick([{"code": "600000.SH", "boards": 1, "theme": "机器人"}],
                      asof=date(2026, 7, 5), mkt=_v5_mkt(),
                      sector_map=sector_map)
    assert len(picked) == 1
    code, boards, theme, composite, sec_score = picked[0]
    assert code == "600000.SH" and sec_score > 75


def test_equity_scales_position_by_pos_mult():
    """_simulate_equity 按 pos_mult 放大投入; 封顶 cap_ratio/position_ratio。"""
    s = load_strategy(_mk_strategy_v5(step=0.05, cap=None))
    zf, kf, sector_map = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf,
                             position_ratio=0.3)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 5),
                 mkt=_v5_mkt(), sector_map=sector_map)
    t = rep["trade_log"][0]
    # 满分 100 → 乘数 1.125(无 cap 不封顶)
    assert t["pos_mult"] == 1.125
```

- [ ] **Step 2: 跑测试确认失败**

```
python -m pytest prism\tests\test_backtest_mkt.py -v -k v5 --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt62
```
预期: 新增 5 个测试 FAIL（4 元组解包错误 / `sector_score` 键不存在）。

- [ ] **Step 3: 实现**

3a. `prism/backtest.py` 顶部 import 区加:

```python
from prism import sector_score
```

3b. `_pick` 中，`mkt_sliced = _slice_mkt(mkt, asof)` 之后加:

```python
        # v5 板块综合评分(可选): 切片数据现算一次, 门槛过滤 + 记分
        score_cfg = sector_score.load_config(self.strategy)
        sec_scores = (sector_score.compute_scores(mkt_sliced)
                      if score_cfg["enabled"] else {})
```

`_pick` 候选循环 `if best >= min_model:` 块改为:

```python
            if best >= min_model:
                sec_score = None
                if score_cfg["enabled"]:
                    sec = (sector_map or {}).get(code)
                    if isinstance(sec, dict):
                        sec = sec.get("sector")
                    rec = sec_scores.get(sec) if sec else None
                    sec_score = rec["score"] if rec else None
                    # fail-closed: 无评分/低分板块不买(设计 §4.2)
                    if sec_score is None or sec_score <= score_cfg["threshold"]:
                        continue
                out.append((code, s.get("boards", 0), s.get("theme", ""),
                            scores["composite"], sec_score))
```

（原 `out.append((code, ..., scores["composite"]))` 删除。）

3c. `run()` 中解包与 trade dict:

```python
                for code, boards, theme, composite, sec_score in self._pick(
                        pool, asof=d, em=em, ticks=ticks, mkt=mkt,
                        sector_map=sector_map):
                    kline = self._kline_for(code)
                    tr = self._simulate_trade(code, kline, d, rules)
                    if tr:
                        if score_cfg["enabled"]:
                            mult = sector_score.position_multiplier(
                                sec_score, score_cfg, self.position_ratio)
                        else:
                            mult = 1.0
                        trades.append({
                            "date": d.strftime("%Y-%m-%d"), "code": code,
                            "boards": boards, "theme": theme,
                            "composite": composite,
                            "sector_score": sec_score, "pos_mult": mult,
                            "entry": tr[0], "exit": tr[1],
                            "return_pct": tr[2], "cost_pct": tr[3],
                            "exit_date": tr[4].strftime("%Y-%m-%d")
                            if tr[4] else None,
                        })
```

（`run()` 开头 `rules = dict(cfg_rules, **(sell_rules or {}))` 之后加一行: `score_cfg = sector_score.load_config(self.strategy)`。）

3d. `_simulate_equity` 中 `per_trade` 改为:

```python
            per_trade = day_nav * self.position_ratio * float(
                t.get("pos_mult") or 1.0)
```

- [ ] **Step 4: 跑本文件全部测试**

```
python -m pytest prism\tests\test_backtest_mkt.py prism\tests\test_backtest.py -v --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt62
```
预期: 全部 PASS（旧测试只解包 len/不触碰新键, 不受 5 元组影响）。

- [ ] **Step 5: Commit**

```bash
git add prism/backtest.py prism/tests/test_backtest_mkt.py
git commit -m "feat(prism): 回测接入板块评分链 — >75门槛过滤 + 仓位乘数(trade记分/封顶)"
```

---

### Task 3: 实盘链路（engine.run_screen 注入 + trader 信号透传）

**Files:**
- Modify: `prism/engine.py`（`run_screen` 约 148-183 行）
- Modify: `prism/trader.py`（`generate_signals` 约 29-60 行）
- Modify: `prism/tests/test_engine.py`（追加测试）
- Modify: `prism/tests/test_trader.py`（追加测试）

**Interfaces:**
- Consumes: Task 1 的 `load_config/compute_scores`；候选 dict 由 `evaluate_stock` 产出（新增可选键 `sector_score`）。
- Produces: `run_screen` 在策略开启评分且 market_ctx 带 `mkt` 时，对候选做同语义过滤并在候选 dict 附 `sector_score`；信号 JSON 新增可选字段 `sector_score`（无则 null，桥端忽略未知字段，协议向后兼容）。

- [ ] **Step 1: 写失败测试**

`prism/tests/test_engine.py` 末尾追加（沿用该文件既有的构造风格——若文件内已有 `_mk_strategy`/构造 helper 则复用，否则用下方完整定义）:

```python
def test_run_screen_sector_score_gate_and_attach():
    """评分开启+ctx带mkt: 低分板块候选被剔除, 通过者附 sector_score。"""
    from prism import sector_score as ss
    s = {
        "id": "t", "name": "t", "description": "",
        "market_gate": {"model": "node", "threshold": 0, "factors": []},
        "scoring_models": [{"id": "m", "name": "m", "weight": 1.0,
                            "factors": []}],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 0},
        "sector_score": {"enabled": True, "threshold": 75,
                         "position": {"step": 0.05, "cap_ratio": 0.45}},
    }
    # 因子0分全靠 candidate_min_model=0 放行; 评分数据: 801110 满分, 801999 无
    closes = [100.0 * (1.011 ** i) for i in range(11)]
    mkt = {"sector": {"801110": {"dates": ["2026-07-%02d" % (i + 1)
                                                for i in range(11)],
                                 "close": closes}}}
    market_ctx = FactorContext(code="__MKT__", mkt=mkt,
                               sector_map={"600000.SH": "801110",
                                           "000001.SZ": "801999"})
    stock_ctxs = {c: FactorContext(code=c, kline=None)
                  for c in ("600000.SH", "000001.SZ")}
    out = run_screen(s, market_ctx, stock_contexts=stock_ctxs)
    codes = [c["code"] for c in out["candidates"]]
    assert codes == ["600000.SH"]                 # 801999 无评分 → 剔除
    assert out["candidates"][0]["sector_score"] > 75


def test_run_screen_sector_score_disabled_noop():
    """评分关闭/ctx无mkt → 行为与 v4 完全一致, 不附键。"""
    s = {"id": "t", "name": "t", "description": "",
         "market_gate": {"model": "node", "threshold": 0, "factors": []},
         "scoring_models": [{"id": "m", "name": "m", "weight": 1.0,
                             "factors": []}],
         "composite": {"mode": "sum"},
         "filters": {"candidate_min_model": 0}}
    market_ctx = FactorContext(code="__MKT__")
    stock_ctxs = {"600000.SH": FactorContext(code="600000.SH", kline=None)}
    out = run_screen(s, market_ctx, stock_contexts=stock_ctxs)
    assert len(out["candidates"]) == 1
    assert "sector_score" not in out["candidates"][0]
```

`prism/tests/test_trader.py` 末尾追加:

```python
def test_generate_signals_passes_sector_score():
    """候选带 sector_score → 透传信号; 不带 → null(协议兼容)。"""
    result = {"environment_ok": True, "candidates": [
        {"code": "600000.SH", "up_stop_price": 10.0,
         "scores": {"composite": 3}, "sector_score": 82.3},
        {"code": "000001.SZ", "up_stop_price": 9.0,
         "scores": {"composite": 2}},
    ]}
    sigs = trader.generate_signals(result, {"id": "s1"})
    assert sigs[0]["sector_score"] == 82.3
    assert sigs[1]["sector_score"] is None
```

- [ ] **Step 2: 跑测试确认失败**

```
python -m pytest prism\tests\test_engine.py prism\tests\test_trader.py -v --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt63
```
预期: 新增 3 个测试 FAIL（无 `sector_score` 键）。

- [ ] **Step 3: 实现**

3a. `prism/engine.py` 顶部 import 加 `from prism import sector_score`。`run_screen` 的 `candidates = []` 之后、`if environment_ok and stock_contexts:` 块内改造:

```python
    candidates = []
    score_cfg = sector_score.load_config(strategy)
    mkt_extra = market_ctx.get("mkt") if market_ctx is not None else None
    sec_scores = (sector_score.compute_scores(mkt_extra)
                  if score_cfg["enabled"] and mkt_extra else {})
    if environment_ok and stock_contexts:
        min_model = (strategy.get("filters") or {}).get(
            "candidate_min_model", 3)
        for code, ctx in stock_contexts.items():
            ev = evaluate_stock(code, ctx, strategy)
            best = max([ev["scores"][m["id"]]
                        for m in strategy["scoring_models"]], default=0)
            if best >= min_model:
                if score_cfg["enabled"] and sec_scores:
                    # sector_map 是市场级数据 → 从 market_ctx 取(非个股ctx)
                    sec = (market_ctx.get("sector_map") or {}).get(code)
                    if isinstance(sec, dict):
                        sec = sec.get("sector")
                    rec = sec_scores.get(sec) if sec else None
                    sec_score = rec["score"] if rec else None
                    if sec_score is None or sec_score <= score_cfg["threshold"]:
                        continue
                    ev["sector_score"] = sec_score
                candidates.append(ev)
        candidates.sort(key=lambda c: c["scores"]["composite"], reverse=True)
```

3b. `prism/trader.py` `generate_signals` 的 out dict 加一行（`"composite"` 之后）:

```python
            "sector_score": c.get("sector_score"),
```

- [ ] **Step 4: 跑测试确认全绿**

```
python -m pytest prism\tests\test_engine.py prism\tests\test_trader.py -v --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt63
```
预期: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add prism/engine.py prism/trader.py prism/tests/test_engine.py prism/tests/test_trader.py
git commit -m "feat(prism): 实盘选股接入板块评分门(run_screen) + 信号透传sector_score"
```

---

### Task 4: 策略 JSON v5 + 配置测试

**Files:**
- Modify: `prism/strategies/sector_momentum.json`（加 `sector_score` 块、更新 description）
- Modify: `prism/tests/test_m67_strategy.py`（`test_sector_momentum_strategy_loads` 追加断言）

**Interfaces:**
- Consumes: Task 1 的配置 schema（`enabled/threshold/position.step/position.cap_ratio`）。
- Produces: v5 策略文件（Task 6 回测直接用）。

- [ ] **Step 1: 更新失败测试**

`test_sector_momentum_strategy_loads` 末尾追加:

```python
    # v5: 板块综合评分链(设计 §4.1)
    ss = s["sector_score"]
    assert ss["enabled"] is True
    assert ss["threshold"] == 75
    assert ss["position"]["step"] == 0.05
    assert ss["position"]["cap_ratio"] == 0.45
```

- [ ] **Step 2: 跑测试确认失败**

```
python -m pytest prism\tests\test_m67_strategy.py::test_sector_momentum_strategy_loads -v --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt64
```
预期: FAIL（KeyError: 'sector_score'）。

- [ ] **Step 3: 更新策略 JSON**

`prism/strategies/sector_momentum.json` — `description` 改为:

```json
  "description": "8.23文档落地v5: 板块强度(SEC1/SEC2/SEC3/SEC4/SEC6) + 形态动量(M1/M6), 门槛需宏观环境确认(N1+至少2个宏观), 卖出移动止盈(trailing 8%/5%)。新增板块综合评分链: 动量35%+资金流25%+拥挤度30%+宏观10% → 板块分>75才买, 仓位每高10分相对+5%封顶45%(缺数据权重归一化)。SEC因子需市场数据层注入(mkt)。",
```

`"sell_rules"` 之后加:

```json
  ,
  "sector_score": {
    "enabled": true,
    "threshold": 75,
    "position": {"step": 0.05, "cap_ratio": 0.45}
  }
```

- [ ] **Step 4: 跑测试确认通过**

```
python -m pytest prism\tests\test_m67_strategy.py -v --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt64
```
预期: 全部 PASS。

- [ ] **Step 5: Commit**

```bash
git add prism/strategies/sector_momentum.json prism/tests/test_m67_strategy.py
git commit -m "feat(prism): sector_momentum v5 — 板块评分链配置(>75门槛/仓位加成封顶45%)"
```

---

### Task 5: 全量回归 + 数据验收

**Files:**
- 无代码改动（验证任务）

**Interfaces:**
- Consumes: Task 1-4 全部产出；后台重建完成的数据缓存。
- Produces: 全绿证明 + 数据就绪结论（Task 6 的前置）。

- [ ] **Step 1: 数据验收**

```
python -m prism.market_data --stats
python -m prism.zt_history --stats
```
预期: `资金流板块数: ≥16`（15 个等东财解封后可补, 评分已支持降级）；`K线板块数: 31`；个股映射 ≥4761；zt 缓存 5000+ 只、范围覆盖回测区间（后台 pwsh-2 完成后）。若 zt 未完成，等待后台任务结束再验收。

- [ ] **Step 2: 全量回归**

PowerShell:

```powershell
Remove-Item D:\QMT_SIGNALS\paused -Force -ErrorAction SilentlyContinue
python -m pytest prism\tests prism_web\tests strategy_web\tests --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt65 2>&1 | Select-Object -Last 15
```
预期: `passed` 无 fail（基线 402 + 本轮新增约 30 = 430±）；若 test_trader 有 paused 相关失败，确认 paused 已删后重跑一次。

- [ ] **Step 3: Commit（如有零星修复）**

```bash
git add -A
git commit -m "test: v5 评分链全量回归通过"
```
（无改动则跳过。）

---

### Task 6: 回测验证 v4 vs v5 vs v5a（消融）

**Files:**
- Create: `prism/strategies/sector_momentum_v4_base.json`（v5 复制后 `sector_score.enabled=false`，作基线）
- Create: `prism/strategies/sector_momentum_v5a_gateonly.json`（复制后 `position.step=0`，门槛-only 消融）
- Create: `pt_v5_compare.py`（汇总三份报告为一行式对比表的一次性脚本, gitignored 前缀 pt_）

**Interfaces:**
- Consumes: Task 4 的 v5 策略；backtest_cli `--strategy <id> --start 20260101 --end 20260817 --use-market-data [--oos]`。
- Produces: 对比结论（写进最终汇报），决定是否调参（threshold/step/权重在 JSON 内调, 不改码）。

- [ ] **Step 1: 生成基线与消融策略**

复制 `prism/strategies/sector_momentum.json` 两次:
- `sector_momentum_v4_base.json`: `id` 改 `"sector_momentum_v4_base"`，`sector_score.enabled` 改 `false`（其余全同）
- `sector_momentum_v5a_gateonly.json`: `id` 改 `"sector_momentum_v5a_gateonly"`，`sector_score.position.step` 改 `0`（门槛生效, 仓位恒 1.0）

- [ ] **Step 2: 跑三组全区间回测（各组约 1-3 分钟, 用 --use-market-data）**

```
python backtest_cli.py --strategy sector_momentum_v4_base --start 20260101 --end 20260817 --use-market-data > pt_v4_full.json 2>pt_v4_full.err
python backtest_cli.py --strategy sector_momentum_v5a_gateonly --start 20260101 --end 20260817 --use-market-data > pt_v5a_full.json 2>pt_v5a_full.err
python backtest_cli.py --strategy sector_momentum --start 20260101 --end 20260817 --use-market-data > pt_v5_full.json 2>pt_v5_full.err
```
预期: 三份 JSON 报告（`total_return_pct`/`max_drawdown_pct`/`win_rate`/`trades`）。

- [ ] **Step 3: 跑三组 OOS（4-8月切分, 重点看 7-8 月切换期）**

```
python backtest_cli.py --strategy sector_momentum_v4_base --start 20260401 --end 20260817 --use-market-data --oos > pt_v4_oos.json 2>pt_v4_oos.err
python backtest_cli.py --strategy sector_momentum_v5a_gateonly --start 20260401 --end 20260817 --use-market-data --oos > pt_v5a_oos.json 2>pt_v5a_oos.err
python backtest_cli.py --strategy sector_momentum --start 20260401 --end 20260817 --use-market-data --oos > pt_v5_oos.json 2>pt_v5_oos.err
```

- [ ] **Step 4: 汇总对比**

写 `pt_v5_compare.py`（utf-8 包装 stdout, 读 6 个 JSON, 打印 对比表: 策略 × {全区间收益/回撤/胜率/笔数, OOS样本内/OOS样本外收益/回撤}）并运行:

```
python pt_v5_compare.py
```
判定标准（设计 §5）: OOS 样本外收益/回撤 **不差于 v4** 且样本内收益保持 >70% → 通过; 否则按 `threshold(70-85)/step(0.02-0.08)/权重` 顺序调参重跑 Step 2-3（改 JSON 即可, 不改码）。

- [ ] **Step 5: 记录结论 + Commit**

结论写进最终用户汇报；策略与对比脚本处置:
- v4 基线/消融 JSON 保留（参数档案）
- `pt_v5_compare.py` 与 pt_*.json 均 gitignored（pt_ 前缀），无需提交

```bash
git add prism/strategies/sector_momentum_v4_base.json prism/strategies/sector_momentum_v5a_gateonly.json
git commit -m "feat(prism): v5回测验证配套 — v4基线/门槛-only消融策略"
```

---

## Self-Review 结论

- **Spec 覆盖**: §3 评分定义→Task 1；§4.1 配置→Task 4；§4.2 引擎→Task 2；§4.3 实盘透传→Task 3；§5 验证→Task 6；§7 测试→Task 1-5 各步。无缺口。
- **占位符**: 无 TBD/TODO；所有代码块完整可落地。
- **类型一致性**: `compute_scores(mkt)`/`load_config(strategy)`/`position_multiplier(score, cfg, base_ratio)` 三签名在 Task 2/3 引用处一致；5 元组在 run() 解包处一致；`sector_score`/`pos_mult` 键名在 trade dict 与测试断言一致。
