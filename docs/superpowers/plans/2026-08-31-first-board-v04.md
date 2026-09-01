# 首板 0.04 三维度评估框架 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 F8 行业边际变化（商品期货涨价）+ F9 板块延展性（有高度且扩张）两个因子，新建独立策略 `first_board_v04.json`（F1-F9），回测验证 F8/F9 的加分有效性。

**Architecture:** 数据层新增 futures 商品期货采集（缓存进 `.market_data_cache.pkl` 新键 `futures`，经 `_slice_mkt` 切片防未来函数）；F8/F9 为标准 ctx 因子（fail-open）；策略文件独立，不改 default.json。回测与实盘同一套因子代码。

**Tech Stack:** Python 3.12 / pytest / akshare(商品期货日线) / 东财涨停池 / QMT zt 历史缓存。

## Global Constraints

- 测试命令必须带 `--import-mode=importlib --basetemp=D:\cc-joesph\pt_btNN`（NN 递增编号，已用到 71）
- 全部代码与测试离线可跑（网络数据 mock，不真连东财/akshare）
- default.json 与现有 43 因子行为不得改变
- F9 口径 = **申万板块**（sector_map），非东财 hybk（回测池无 theme 字段，规格 §7 近似方案，已获用户确认）
- F8 阈值固定 +3%（20 日涨幅），F8/F9 权重 1.0 平权（用户已确认）
- git 只本地 commit；**push 前必须问用户**
- 因子返回 `{"score": 0~1, "note": str}`；数据缺失 fail-open 得 0 并在 note 说明（F9 的 zt 历史缺失按 fail-closed 处理，见 Task 4）
- Windows/GBK 控制台：python 脚本开头 `sys.stdout.reconfigure(encoding="utf-8")` 或 `$env:PYTHONIOENCODING='utf-8'`
- 接口契约（跨任务）：`mkt["futures"] = {板块代码: {"name": 板块名, "commodities": {品种代码: {"name": 品种名, "dates": ["YYYY-MM-DD"...], "close": [float...]}}}}`（**板块代码来自缓存 sectors 段，与 sector_map/mkt["sector"] 同源**；行业→品种映射按行业**名称**驱动，规避申万代码版本差异）；`mkt["zt_prev"] = {"date": "YYYY-MM-DD", "codes": [code...]}`（上一交易日涨停代码表）

---

### Task 1: 商品期货数据源探针（产出知识，不提交代码）

**Files:**
- Create: `pt_futures_probe.py`（gitignored pt_ 前缀，不 commit）
- Produce: `pt_futures_ok.json`（可用品种清单，供 Task 2 硬编码确认）

**Interfaces:**
- Produces: 确认 akshare `futures_zh_daily_sina(symbol)` 对目标品种的可用性；输出 JSON `{品种代码: {"name": 品种名, "ok": true, "rows": N, "last_date": "..."}}`

- [ ] **Step 1: 写探针脚本**

```python
# -*- coding: utf-8 -*-
"""商品期货日线探针: akshare sina 接口对候选品种逐个试取, 产出可用清单。
用法: python pt_futures_probe.py  → 打印结果 + 写 pt_futures_ok.json"""
import io
import json
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# 目标品种(品种代码 = sina 主力连续符号): Task 2 COMMODITY_BY_NAME 的候选池
CANDIDATES = [
    ("MA0", "甲醇"), ("TA0", "PTA"), ("ZC0", "动力煤"), ("JM0", "焦煤"),
    ("RB0", "螺纹钢"), ("HC0", "热卷"), ("CU0", "铜"), ("AL0", "铝"),
    ("SC0", "原油"), ("LH0", "生猪"), ("M0", "豆粕"), ("Y0", "豆油"),
    ("SR0", "白糖"), ("FG0", "玻璃"), ("LC0", "碳酸锂"), ("PS0", "工业硅"),
]

def main():
    import akshare as ak
    ok = {}
    for sym, name in CANDIDATES:
        try:
            df = ak.futures_zh_daily_sina(symbol=sym)
            rows = len(df)
            last = str(df["date"].iloc[-1]) if rows else ""
            ok[sym] = {"name": name, "ok": rows >= 60, "rows": rows,
                       "last_date": last}
            print("%-5s %-4s %5d 行 末 %s" % (sym, name, rows, last))
        except Exception as e:
            ok[sym] = {"name": name, "ok": False, "rows": 0,
                       "error": repr(e)[:80]}
            print("%-5s %-4s 失败 %r" % (sym, name, e))
        time.sleep(1.0)  # 礼貌间隔
    with open("pt_futures_ok.json", "w", encoding="utf-8") as f:
        json.dump(ok, f, ensure_ascii=False, indent=1)
    print("可用品种:", sum(1 for v in ok.values() if v["ok"]), "/", len(ok))

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 跑探针**

Run: `python pt_futures_probe.py`
Expected: 打印各品种行数，末行"可用品种: N / 16"。**≥10 个可用即视为通过**（覆盖化工/煤炭/钢铁/有色/农牧核心品种）；不足 10 个 → 报告 BLOCKED 并附 pt_futures_ok.json。

- [ ] **Step 3: 记录结果**

把可用品种表（含列数、末日期）贴进任务报告。若个别品种符号不对（如 ZC0/LC0），在报告里给出实际可用替代符号或标记放弃，Task 2 的 COMMODITY_MAP 按此收敛。

---

### Task 2: 数据层 — futures 采集 + 切片扩展

**Files:**
- Modify: `prism/market_data.py`（新增 COMMODITY_MAP、fetch_futures、缓存键 futures、_snapshot 组装）
- Modify: `prism/backtest.py:71-120`（`_slice_mkt` 增加 futures 分支）
- Test: `prism/tests/test_futures_data.py`（新建）

**Interfaces:**
- Consumes: Task 1 确认的品种符号（写死进 COMMODITY_MAP）
- Produces: `fetch_futures(force=False) -> {品种代码: {"name", "dates", "close"}}`（写入缓存 `futures` 键）；`COMMODITY_BY_NAME: dict[行业名, list[品种代码]]`；`futures_snapshot() -> mkt["futures"]`（键=缓存 sectors 段的板块代码，结构见 Global Constraints）；`_slice_mkt` 对 `futures` 键按 asof 切片

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""futures 数据层测试: COMMODITY_MAP 结构 / fetch 缓存写入 / _slice_mkt 切片。全离线。"""
from datetime import date
from unittest import mock

from prism import market_data as md


def _fake_df(rows):
    import pandas as pd
    return pd.DataFrame(rows)


def test_commodity_by_name_covers_core_sectors():
    # 核心周期行业必须有映射, 每个行业至少 1 个品种(名称驱动, 规避申万版本差异)
    for name in ("基础化工", "煤炭", "钢铁", "有色金属", "石油石化"):
        assert name in md.COMMODITY_BY_NAME, name
        assert md.COMMODITY_BY_NAME[name]


def test_futures_snapshot_keys_follow_sector_cache(monkeypatch):
    # futures 快照键 = 缓存 sectors 段的板块代码(与 sector_map/mkt["sector"] 同源)
    monkeypatch.setattr(md, "_load_cache", lambda: {
        "sectors": {"801030": {"name": "基础化工"},
                    "801950": {"name": "煤炭"},
                    "801150": {"name": "医药生物"}}})
    monkeypatch.setattr(md, "fetch_futures", lambda force=False: {
        "MA0": {"name": "甲醇", "dates": ["2026-07-01"], "close": [1.0]},
        "JM0": {"name": "焦煤", "dates": ["2026-07-01"], "close": [2.0]}})
    out = md.futures_snapshot()
    assert set(out) == {"801030", "801950"}      # 医药生物无映射 → 不出现
    assert "MA0" in out["801030"]["commodities"]
    assert "JM0" in out["801950"]["commodities"]


def test_fetch_futures_writes_cache(monkeypatch):
    df = _fake_df({"date": ["2026-07-01", "2026-07-02"],
                   "close": [2400.0, 2450.0]})
    monkeypatch.setattr(md, "_fetch_futures_daily",
                        lambda sym: df)
    monkeypatch.setattr(md, "_load_cache", lambda: {})
    monkeypatch.setattr(md, "_save_cache", lambda c: None)
    out = md.fetch_futures()
    assert out["MA0"]["close"] == [2400.0, 2450.0]
    assert out["MA0"]["dates"] == ["2026-07-01", "2026-07-02"]


def test_fetch_futures_skips_failed_symbol(monkeypatch):
    def boom(sym):
        raise RuntimeError("net down")
    monkeypatch.setattr(md, "_fetch_futures_daily", boom)
    monkeypatch.setattr(md, "_load_cache", lambda: {})
    monkeypatch.setattr(md, "_save_cache", lambda c: None)
    out = md.fetch_futures()
    assert "MA0" not in out  # fail-open: 失败品种跳过


def test_slice_mkt_slices_futures():
    from prism.backtest import _slice_mkt
    mkt = {"futures": {"801722": {"name": "基础化工", "commodities": {
        "MA0": {"name": "甲醇", "dates": ["2026-07-01", "2026-07-02",
                             "2026-07-03"],
                "close": [1.0, 2.0, 3.0]}}}}}
    out = _slice_mkt(mkt, date(2026, 7, 2))
    ma = out["futures"]["801722"]["commodities"]["MA0"]
    assert ma["dates"] == ["2026-07-01", "2026-07-02"]
    assert ma["close"] == [1.0, 2.0]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest prism/tests/test_futures_data.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt72`
Expected: FAIL（`COMMODITY_MAP` / `fetch_futures` 不存在；_slice_mkt 不切 futures）

- [ ] **Step 3: 实现 market_data 侧**

在 `prism/market_data.py` 追加（放在既有 fetch 函数旁，遵循该文件现有缓存读写惯例 `_load_cache/_save_cache`；若实际函数名不同以文件为准并同步测试）：

```python
# 行业名 → 商品期货品种(名称驱动, 规避申万代码在 SW2014/SW2021 间的版本差异;
# 板块代码在 futures_snapshot 组装时从缓存 sectors 段按名称反查)。
# 品种代码 = akshare sina 主力连续符号, 可用性以 pt_futures_probe 实测为准,
# 采集失败的品种静默跳过(fail-open)。
COMMODITY_BY_NAME = {
    "基础化工": ["MA0", "TA0"],
    "煤炭": ["JM0"],
    "钢铁": ["RB0", "HC0"],
    "有色金属": ["CU0", "AL0", "SI0"],
    "石油石化": ["SC0"],
    "农林牧渔": ["LH0", "M0"],
    "食品饮料": ["SR0", "Y0"],
    "建筑材料": ["FG0"],
    "电力设备": ["LC0"],
}
# 品种可用性为 Task 1 探针实测(2026-08-31, 16/16 可用): 动力煤 ZC0 停更于
# 2022-12-30(死数据会让 F8 用旧涨幅误判, 删除); 工业硅符号为 SI0(PS0 实为
# 多晶硅)。详证 .superpowers/sdd/task-v04-1-report.md。

_FUTURES_NAMES = {
    "MA0": "甲醇", "TA0": "PTA", "ZC0": "动力煤", "JM0": "焦煤",
    "RB0": "螺纹钢", "HC0": "热卷", "CU0": "铜", "AL0": "铝",
    "SC0": "原油", "LH0": "生猪", "M0": "豆粕", "Y0": "豆油",
    "SR0": "白糖", "FG0": "玻璃", "LC0": "碳酸锂", "PS0": "工业硅",
}


def _fetch_futures_daily(sym):
    """单品种商品期货日线(akshare sina 主力连续)。返回 DataFrame(date, close)。"""
    import akshare as ak
    return ak.futures_zh_daily_sina(symbol=sym)


def fetch_futures(force=False):
    """采集全部映射品种日线 → 缓存 futures 键。
    返回 {品种代码: {"name", "dates", "close"}}; 单品种失败跳过(fail-open)。
    force=True 强制重采(默认走缓存)。"""
    cache = _load_cache()
    fut = {} if force else dict(cache.get("futures") or {})
    todo = [sym for sym in _FUTURES_NAMES if sym not in fut]
    for sym in todo:
        try:
            df = _fetch_futures_daily(sym)
            if df is None or len(df) < 60:
                continue
            dates = [str(d) for d in df["date"].tolist()]
            closes = [float(x) for x in df["close"].tolist()]
            fut[sym] = {"name": _FUTURES_NAMES[sym],
                        "dates": dates, "close": closes}
        except Exception as e:
            logger.warning("期货 %s 采集失败: %r", sym, e)
        time.sleep(0.5)
    cache["futures"] = fut
    _save_cache(cache)
    return fut


def futures_snapshot():
    """组装 mkt["futures"] 快照: {板块代码: {"name", "commodities": {品种: K线}}}。
    板块代码/名称来自缓存 sectors 段(与 sector_map / mkt["sector"] 同源);
    行业 → 品种按名称映射(COMMODITY_BY_NAME), 无映射行业不出现。"""
    cache = _load_cache()
    fut = fetch_futures()
    out = {}
    for scode, rec in (cache.get("sectors") or {}).items():
        name = (rec or {}).get("name") or ""
        syms = COMMODITY_BY_NAME.get(name)
        if not syms:
            continue
        comms = {sym: fut[sym] for sym in syms if sym in fut}
        out[scode] = {"name": name, "commodities": comms}
    return out
```

并在该文件组装市场快照的函数（现为 `_snapshot()` 或等价物——以实际代码为准）里加一行 `"futures": futures_snapshot(),`（与 `"sector_flow"` 同级）。

- [ ] **Step 4: 实现 _slice_mkt futures 分支**

在 `prism/backtest.py` `_slice_mkt` 末尾（sector_flow 分支后）追加：

```python
    # 商品期货同样切片(F8 行业边际变化因子用)
    futs = mkt.get("futures") if isinstance(mkt, dict) else {}
    if futs:
        out["futures"] = {}
        for scode, rec in futs.items():
            comms = {}
            for sym, k in (rec.get("commodities") or {}).items():
                dates = k.get("dates") or []
                keep = [i for i, d in enumerate(dates) if d <= asof_str]
                if keep:
                    comms[sym] = {
                        "name": k.get("name"),
                        "dates": [dates[i] for i in keep],
                        "close": [k["close"][i] for i in keep],
                    }
            out["futures"][scode] = {"name": rec.get("name"),
                                     "commodities": comms}
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest prism/tests/test_futures_data.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt72`
Expected: 4 passed

- [ ] **Step 6: 真网冒烟（一次性，不入测试）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); from prism import market_data as md; s=md.futures_snapshot(); print(len(s), list(s)[:4]); print(list((s.get('801722') or {}).get('commodities') or {}))"`
Expected: 打印板块数（≥8）、'801722' 至少含一个可用品种。失败则按 spec §7 降级路径处理并记录。

- [ ] **Step 7: Commit**

```bash
git add prism/market_data.py prism/backtest.py prism/tests/test_futures_data.py
git commit -m "feat(prism): 商品期货数据层 — COMMODITY_BY_NAME/fetch_futures/切片防未来"
```

---

### Task 3: F8 行业边际变化因子

**Files:**
- Create: `prism/factors/factor_f8_commodity_edge.py`
- Test: `prism/tests/test_f8_commodity_edge.py`

**Interfaces:**
- Consumes: `ctx.get("mkt")["futures"]`（Task 2 结构）+ `ctx.sector_map[code]`
- Produces: 因子 `F8`（0/1，note 说明最强品种涨幅或缺失原因）；失败链路 fail-open（无 mkt/无板块归属/无映射/数据不足 → score 0）

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""F8 行业边际变化测试 — 直接构造 ctx, 全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.context import FactorContext
from prism.registry import get_factor

import prism.factors  # noqa: F401  触发因子库扫描注册


def _ctx(scode="BK0475", commodities=None, no_mkt=False):
    mkt = {} if no_mkt else {"futures": {scode: {
        "name": "基础化工", "commodities": commodities or {}}}}
    return FactorContext(code="600000.SH", sector_map={"600000.SH": scode},
                         mkt=mkt)


def _comm(closes, sym="MA0", name="甲醇"):
    dates = ["2026-07-%02d" % (i % 28 + 1) for i in range(len(closes))]
    return {sym: {"name": name, "dates": dates, "close": closes}}


def test_f8_hit():
    # 21 点: 100 → 103.5 (+3.5% > +3%) → 命中
    res = get_factor("F8")["func"](
        _ctx(commodities=_comm([100.0] * 20 + [103.5])))
    assert res["score"] == 1, res["note"]


def test_f8_miss_at_threshold():
    # 恰好 +3.0% 不大于阈值 → 不命中
    res = get_factor("F8")["func"](
        _ctx(commodities=_comm([100.0] * 20 + [103.0])))
    assert res["score"] == 0


def test_f8_any_commodity_hit():
    # 两品种, 仅第二个命中 → 命中(任一品种判定)
    comms = _comm([100.0] * 21, sym="MA0")
    comms["TA0"] = {"name": "PTA", "dates": ["2026-07-01"] * 21,
                    "close": [100.0] * 20 + [105.0]}
    res = get_factor("F8")["func"](_ctx(commodities=comms))
    assert res["score"] == 1, res["note"]


def test_f8_insufficient_data():
    res = get_factor("F8")["func"](_ctx(commodities=_comm([100.0] * 10)))
    assert res["score"] == 0
    assert "不足" in res["note"]


def test_f8_no_commodity():
    res = get_factor("F8")["func"](_ctx(commodities={}))
    assert res["score"] == 0


def test_f8_no_sector():
    res = get_factor("F8")["func"](
        FactorContext(code="600000.SH", sector_map={}, mkt={}))
    assert res["score"] == 0
    assert "无板块归属" in res["note"]


def test_f8_no_mkt():
    # mkt 未注入 → fail-open 0
    res = get_factor("F8")["func"](
        FactorContext(code="600000.SH", sector_map={"600000.SH": "BK0475"}))
    assert res["score"] == 0


def test_f8_registered():
    import prism.registry as reg
    assert "F8" in reg.FACTORS
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest prism/tests/test_f8_commodity_edge.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt73`
Expected: FAIL（`F8` 未注册）

- [ ] **Step 3: 实现因子**

```python
# -*- coding: utf-8 -*-
"""F8 行业边际变化: 所属板块映射的商品期货任一品种 20 日涨幅 > +3% → 1 分。

数据通路: 回测/实盘把市场数据快照注入 ctx._extra["mkt"],
格式 {"futures": {板块代码: {"name": 板块名,
    "commodities": {品种代码: {"name": 品种名, "dates": [...], "close": [...]}}}}};
个股所属板块由 ctx.sector_map[code] 取(板块代码, 与 futures 键同源)。
20 日涨幅 = closes[-1] / closes[-21] - 1(需 ≥21 个收盘点)。
数据缺失/板块未映射/无期货数据 → fail-open 0(并说明缺什么)。"""
from prism.registry import factor

THRESHOLD = 0.03   # 20 日涨幅阈值(设计 §3.1 固定 +3%, 调参范围 +2%~+5%)
WINDOW = 20        # 回看交易日数


@factor(id="F8", name="行业边际变化", category="sector",
        description="映射商品任一品种20日涨幅>+3%(需mkt注入, fail-open)")
def compute(ctx):
    mkt = ctx.get("mkt") or {}
    futs = mkt.get("futures") or {}
    sector_map = ctx.sector_map or {}
    scode = sector_map.get(ctx.code or "")
    if not scode:
        return {"score": 0, "note": "无板块归属"}
    rec = futs.get(scode)
    if not rec:
        return {"score": 0, "note": "板块无期货映射(%s)" % scode}
    best = None
    for sym, k in (rec.get("commodities") or {}).items():
        closes = k.get("close") or []
        if len(closes) < WINDOW + 1:
            continue
        base = closes[-(WINDOW + 1)]
        if not base:
            continue
        gain = closes[-1] / base - 1
        if best is None or gain > best[1]:
            best = (sym, gain)
    if best is None:
        return {"score": 0, "note": "期货数据不足20日(%s)" % scode}
    if best[1] > THRESHOLD:
        return {"score": 1,
                "note": "%s 20日涨%.1f%%" % (best[0], best[1] * 100)}
    return {"score": 0,
            "note": "商品无边际改善(最强%.1f%%)" % (best[1] * 100)}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest prism/tests/test_f8_commodity_edge.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt73`
Expected: 8 passed

- [ ] **Step 5: 因子体检**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m prism.factor_check`
Expected: 全部 PASS（含新 F8，总数 44）

- [ ] **Step 6: Commit**

```bash
git add prism/factors/factor_f8_commodity_edge.py prism/tests/test_f8_commodity_edge.py
git commit -m "feat(prism): F8 行业边际变化因子 — 映射商品20日涨幅>+3%"
```

---

### Task 4: 回测池上下文注入（limit_ups + zt_prev）

**Files:**
- Modify: `prism/backtest.py`（`_stock_ctx` 加 `limit_ups` 参数、`_pick` 加 `prev_pool` 参数并注入 `mkt["zt_prev"]`、`run()` 逐日维护 `prev_pool`）
- Test: `prism/tests/test_backtest_pool_ctx.py`（新建）

**Interfaces:**
- Consumes: 涨停池条目 `{code, boards, ...}`（zt_feed 输出，QMT/东财池均有 boards）
- Produces: 回测个股 `ctx.limit_ups` = 当日池（补 sealed/last 后的 pool_ctx）；`ctx.get("mkt")["zt_prev"]["codes"]` = 最近一个有池交易日的涨停代码表；`_pick(pool, asof, ..., prev_pool=None)` 新可选参数（默认 None 不注入，兼容旧调用）

**注意（行为变化，需在报告向用户说明）:** 注入 `limit_ups` 后，F4/S6（板块共振）在回测中从恒 0 变为真实计算。这不是破坏性改动而是数据补齐——F4 设计意图本就需要池数据；v03 基线与 v04 同口径对比，公平。

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""回测池上下文注入测试 — 今日池(limit_ups)与昨日池(zt_prev)进入个股 ctx。全离线。"""
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
from prism import backtest
from prism.engine import load_strategy
import prism.factors  # noqa: F401  触发真实因子注册


def _mk_strategy(for_factor):
    return {
        "id": "bt_pool", "name": "池注入回测", "description": "",
        "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0,
             "factors": [{"id": for_factor, "op": ">", "threshold": 0}]},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1, "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                       "max_hold_days": 5},
    }


@pytest.fixture(autouse=True)
def _node_factor():
    reg.reset()
    reg.scan_factors("prism.factors", force=True)   # F4 等真实因子

    @reg.factor(id="N1", name="n", category="node", description="")
    def f_n(ctx):
        return {"score": 1, "note": ""}
    yield


SECTOR_MAP = {"600000.SH": "BK0475", "000001.SZ": "BK0475",
              "000002.SZ": "BK0475", "600519.SH": "BK0475"}

POOL_0701 = [{"code": "600000.SH", "boards": 1},
             {"code": "000001.SZ", "boards": 2},
             {"code": "000002.SZ", "boards": 1},
             {"code": "600519.SH", "boards": 1}]
POOL_0702 = [{"code": "600000.SH", "boards": 1},
             {"code": "000001.SZ", "boards": 1},
             {"code": "000002.SZ", "boards": 1}]
POOL_0703 = [{"code": "600000.SH", "boards": 2},
             {"code": "000001.SZ", "boards": 2},
             {"code": "000002.SZ", "boards": 1}]


def _feeds():
    start = date(2026, 7, 1)

    def zf(d):
        return {"20260701": POOL_0701, "20260702": POOL_0702,
                "20260703": POOL_0703}.get(d, [])

    def kf(code):
        closes = [10.0 * (1.01 ** i) for i in range(10)]
        dates = [(start + timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(10)]
        return list(zip(dates, closes))
    return zf, kf


def _bt(for_factor):
    s = load_strategy(_mk_strategy(for_factor))
    zf, kf = _feeds()
    return backtest.Backtester(s, zt_feed=zf, kline_feed=kf)


def test_f4_sector_resonance_hits_with_pool_injection():
    """F4(板块共振≥3家)依赖 ctx.limit_ups: 注入后回测可命中(改前恒 0)。"""
    bt = _bt("F4")
    picked = bt._pick(POOL_0703, asof=date(2026, 7, 3), mkt={},
                      sector_map=SECTOR_MAP)
    assert len(picked) == 3          # 池内 3 只同板块, 家数 3 ≥ 3 → 全过


def test_pick_injects_prev_pool_as_zt_prev():
    """prev_pool → mkt["zt_prev"]["codes"], 个股 ctx 可读; 缺省不注入。"""

    @reg.factor(id="T9", name="t", category="test", description="")
    def f_t(ctx):
        prev = ((ctx.get("mkt") or {}).get("zt_prev") or {}).get("codes")
        hit = prev == ["600000.SH", "000001.SZ", "000002.SZ"]
        return {"score": 1 if hit else 0, "note": ""}

    bt = _bt("T9")
    picked = bt._pick(POOL_0703, asof=date(2026, 7, 3), mkt={},
                      sector_map=SECTOR_MAP, prev_pool=POOL_0702)
    assert len(picked) == 3          # 昨日池代码精确匹配 → 全过
    picked = bt._pick(POOL_0703, asof=date(2026, 7, 3), mkt={},
                      sector_map=SECTOR_MAP)
    assert picked == []              # 不传 prev_pool → zt_prev 缺失 → 无候选


def test_run_maintains_prev_pool_across_days():
    """run() 逐日维护 prev_pool: 7-03 的"昨日"= 7-02 池; 首日/不匹配日不命中。"""

    @reg.factor(id="T9", name="t", category="test", description="")
    def f_t(ctx):
        prev = ((ctx.get("mkt") or {}).get("zt_prev") or {}).get("codes")
        hit = prev == ["600000.SH", "000001.SZ", "000002.SZ"]
        return {"score": 1 if hit else 0, "note": ""}

    bt = _bt("T9")
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3), mkt={},
                 sector_map=SECTOR_MAP)
    dates = {t["date"] for t in rep["trade_log"]}
    assert "2026-07-03" in dates      # 昨日池=7-02 → T9 命中
    assert "2026-07-01" not in dates  # 首日 prev_pool=[] → 不命中
    assert "2026-07-02" not in dates  # 昨日池=7-01(4只) → 不匹配 → 不命中
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest prism/tests/test_backtest_pool_ctx.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt74`
Expected: 3 个测试全 FAIL（_pick 无 prev_pool 参数 / _stock_ctx 不注 limit_ups）

- [ ] **Step 3: 改 backtest.py 三处**

3a. `_stock_ctx` 签名与 extra 组装（保留原 docstring 与 DataFrame 逻辑，只加参数与一行）：

```python
    def _stock_ctx(self, code, kline, mkt=None, sector_map=None,
                   limit_ups=None):
```

在 `if sector_map is not None:` 块之后追加：

```python
        if limit_ups is not None:
            extra["limit_ups"] = limit_ups
```

3b. `_pick` 签名加 `prev_pool=None`（docstring 补一句"prev_pool: 上一有效交易日涨停池, 注入 mkt['zt_prev'] 供 F9"）；在 `mkt_sliced = _slice_mkt(mkt, asof)` 之后追加：

```python
        # F9 板块延展性: 上一交易日涨停代码表(由 run() 维护传入)
        if prev_pool is not None:
            mkt_sliced["zt_prev"] = {"codes": [s["code"] for s in prev_pool]}
```

逐股 ctx 构造改为传入当日池（原 `ctx = self._stock_ctx(code, kline, mkt_sliced, sector_map)`）：

```python
            ctx = self._stock_ctx(code, kline, mkt_sliced, sector_map,
                                  limit_ups=pool_ctx)
```

3c. `run()` 第二步逐日回放循环：循环前（`d = start_date` 附近）初始化 `prev_pool = []`；`self._pick(...)` 调用加 `prev_pool=prev_pool` 实参；`if pool:` 块内（`for ... _pick(...)` 循环结束后）追加 `prev_pool = pool`。改后循环骨架：

```python
        d = start_date
        prev_pool = []
        while d <= end_date:
            if progress:
                progress(d)
            pool = self._pool_for(d)
            if pool:
                dates.append(d)
                for code, boards, theme, composite, sec_score in self._pick(
                        pool, asof=d, em=em, ticks=ticks, mkt=mkt,
                        sector_map=sector_map, prev_pool=prev_pool):
                    ...
                prev_pool = pool   # 今日池成为下一有效交易日的"昨日池"
            d += timedelta(days=1)
```

（`...` 处保持原 `for` 循环体不变。语义：池为空的日（非交易日/缓存缺失）不更新 prev_pool → "昨日"= 最近一个有池记录的交易日，近似安全。）

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `python -m pytest prism/tests/test_backtest_pool_ctx.py prism/tests/test_backtest_mkt.py prism/tests/test_backtest.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt74`
Expected: 全 passed（旧回测测试不受影响：不传 prev_pool 时行为不变；F4/S6 在无 sector_map 的旧测试里仍 0）

- [ ] **Step 5: Commit**

```bash
git add prism/backtest.py prism/tests/test_backtest_pool_ctx.py
git commit -m "feat(prism): 回测池上下文注入 — ctx.limit_ups 与 mkt[zt_prev]"
```

---

### Task 5: F9 板块延展性因子

**Files:**
- Create: `prism/factors/factor_f9_sector_expansion.py`
- Test: `prism/tests/test_f9_sector_expansion.py`

**Interfaces:**
- Consumes: `ctx.sector_map`、`ctx.limit_ups`（Task 4 注入）、`ctx.get("mkt")["zt_prev"]["codes"]`（Task 4 注入）
- Produces: 因子 `F9`（0/1）；双条件 AND：有高度（板块涨停≥3家 且 连板股≥2只）+ 在扩张（今日≥昨日）；昨日池缺失 fail-closed 0（设计 §3.2）

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""F9 板块延展性测试 — 有高度(涨停≥3且连板≥2)且扩张(今日≥昨日)。全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.context import FactorContext
from prism.registry import get_factor

import prism.factors  # noqa: F401


SECTOR_MAP = {"600000.SH": "BK0475", "000001.SZ": "BK0475",
              "000002.SZ": "BK0475", "600519.SH": "BK0475"}

UP3_LB2 = [{"code": "600000.SH", "boards": 2},
           {"code": "000001.SZ", "boards": 2},
           {"code": "000002.SZ", "boards": 1}]


def _ctx(limit_ups, prev_codes, code="600000.SH"):
    mkt = {"zt_prev": {"date": "2026-07-02", "codes": prev_codes}}
    return FactorContext(code=code, sector_map=SECTOR_MAP,
                         limit_ups=limit_ups, mkt=mkt)


def test_f9_hit():
    res = get_factor("F9")["func"](
        _ctx(UP3_LB2, ["600000.SH", "000001.SZ", "600519.SH"]))
    assert res["score"] == 1, res["note"]


def test_f9_not_expanding():
    # 今日 3 < 昨日 4 → 不扩张
    res = get_factor("F9")["func"](
        _ctx(UP3_LB2, ["600000.SH", "000001.SZ", "000002.SZ", "600519.SH"]))
    assert res["score"] == 0
    assert "扩张" in res["note"]


def test_f9_few_limit_ups():
    # 板块涨停 2 家 < 3 → 无高度
    ups = [{"code": "600000.SH", "boards": 2},
           {"code": "000001.SZ", "boards": 1}]
    res = get_factor("F9")["func"](_ctx(ups, ["600000.SH"]))
    assert res["score"] == 0


def test_f9_no_boards():
    # 3 家但连板 0(全首板) → 无高度
    ups = [{"code": "600000.SH", "boards": 1},
           {"code": "000001.SZ", "boards": 1},
           {"code": "000002.SZ", "boards": 1}]
    res = get_factor("F9")["func"](_ctx(ups, ["600000.SH"]))
    assert res["score"] == 0


def test_f9_missing_prev_fail_closed():
    # 昨日池缺失 → fail-closed 0(设计 §3.2: 无昨日基准无法判定扩张)
    res = get_factor("F9")["func"](_ctx(UP3_LB2, []))
    assert res["score"] == 0
    assert "昨日" in res["note"]


def test_f9_no_sector():
    ctx = FactorContext(code="600000.SH", sector_map={},
                        limit_ups=UP3_LB2,
                        mkt={"zt_prev": {"codes": ["x"]}})
    res = get_factor("F9")["func"](ctx)
    assert res["score"] == 0


def test_f9_registered():
    import prism.registry as reg
    assert "F9" in reg.FACTORS
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest prism/tests/test_f9_sector_expansion.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt75`
Expected: FAIL（`F9` 未注册）

- [ ] **Step 3: 实现因子**

```python
# -*- coding: utf-8 -*-
"""F9 板块延展性: 所属板块"有高度"且"在扩张" → 1 分(设计 §3.2)。

口径(设计修正, 已获用户确认): "题材"统一用申万板块(sector_map)代理——
回测历史池无 hybk 题材字段, 申万口径 live/backtest 一致、全窗口可回测
(F4 板块共振同口径)。昨日池由 mkt["zt_prev"]["codes"] 注入(回测 Task 4 /
实盘 Task 7 接线)。
有高度: 板块内当日涨停 ≥3 家 且 连板股(boards≥2) ≥2 只;
在扩张: 板块今日涨停家数 ≥ 昨日。
昨日池缺失 → fail-closed 0(无昨日基准无法判定扩张)。"""
from prism.registry import factor
from prism._utils import sector_count


@factor(id="F9", name="板块延展性", category="sector",
        description="板块有高度(涨停≥3且连板≥2)且扩张(今日≥昨日)")
def compute(ctx):
    sector_map = ctx.sector_map or {}
    scode = sector_map.get(ctx.code or "")
    if not scode:
        return {"score": 0, "note": "无板块归属"}
    ups = ctx.limit_ups or []
    prev = ((ctx.get("mkt") or {}).get("zt_prev") or {}).get("codes") or []
    if not prev:
        return {"score": 0, "note": "昨日涨停池缺失"}
    today_n = sector_count(ctx.code or "", sector_map, ups)
    if today_n < 3:
        return {"score": 0, "note": "板块涨停%d家(<3)" % today_n}
    lb = sum(1 for c in ups
             if sector_map.get(c.get("code")) == scode
             and (c.get("boards") or 0) >= 2)
    if lb < 2:
        return {"score": 0, "note": "板块连板%d只(<2)" % lb}
    prev_n = sum(1 for c in prev if sector_map.get(c) == scode)
    if today_n < prev_n:
        return {"score": 0,
                "note": "今日%d家<昨日%d家 不扩张" % (today_n, prev_n)}
    return {"score": 1,
            "note": "板块涨停%d家 连板%d只 昨日%d家" % (today_n, lb, prev_n)}
```

- [ ] **Step 4: 跑测试确认通过 + 因子体检**

Run: `python -m pytest prism/tests/test_f9_sector_expansion.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt75`
Expected: 7 passed

Run: `$env:PYTHONIOENCODING='utf-8'; python -m prism.factor_check`
Expected: 全部 PASS（含 F8/F9；总数以实际为准——存量 42 + F8 + F9 = 44，SEC5 本就不存在）

- [ ] **Step 5: Commit**

```bash
git add prism/factors/factor_f9_sector_expansion.py prism/tests/test_f9_sector_expansion.py
git commit -m "feat(prism): F9 板块延展性因子 — 有高度且扩张(申万口径)"
```

---

### Task 6: first_board_v04 策略 + v03 基线 + default 快照保护

**Files:**
- Create: `prism/strategies/first_board_v04.json`
- Create: `prism/strategies/first_board_v03.json`（对照基线：F1-F7，其余与 v04 完全一致）
- Test: `prism/tests/test_first_board_strategies.py`（新建）

**Interfaces:**
- Consumes: F1-F9（全部已注册）
- Produces: 可被 `backtest_cli --strategy first_board_v04|first_board_v03` 加载的策略；**default.json 内容快照锁定**（防误改）

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""first_board_v03/v04 策略文件测试 + default.json 快照保护。全离线。"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.engine import load_strategy
import prism.factors  # noqa: F401
import prism.registry as reg

STRAT_DIR = Path(__file__).parent.parent / "strategies"

DEFAULT_SNAPSHOT = {
    "id": "default",
    "name": "默认四模型策略",
    "description": "兼容现有 4 模型 24 因子的默认策略。势能模型原手填因子 S1/S5/S7 已替换为 K线自动因子 M1/M2/M5(全自动计算, 可回测验证)。",
    "market_gate": {"model": "node", "threshold": 3,
                    "factors": ["N1", "N2", "N3", "N4", "N5"]},
    "scoring_models": [
        {"id": "first_board", "name": "首板", "weight": 0.60,
         "factors": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]},
        {"id": "monster", "name": "妖股", "weight": 0.25,
         "factors": ["Y1", "Y2", "Y3", "Y4", "Y5", "Y6", "Y7"]},
        {"id": "momentum", "name": "势能", "weight": 0.15,
         "factors": ["M1", "M2", "S2", "S3", "S4", "S6", "M5"]}
    ],
    "composite": {"mode": "top3_weighted", "weights": [0.60, 0.25, 0.15],
                  "cap": 7.0},
    "filters": {"candidate_min_model": 3, "environment_threshold": 3},
    "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                   "max_hold_days": 5}
}


def _load(name):
    return load_strategy(STRAT_DIR / ("%s.json" % name))


def test_v04_loads_with_registered_factors():
    s = _load("first_board_v04")
    assert s["id"] == "first_board_v04"
    fids = s["scoring_models"][0]["factors"]
    assert fids == ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9"]
    for fid in fids:
        assert fid in reg.FACTORS, "%s 未注册" % fid
    assert s["composite"]["cap"] == 9.0
    assert s["market_gate"] == {"model": "node", "threshold": 1,
                                "factors": ["N1"]}
    assert s["filters"]["candidate_min_model"] == 3


def test_v03_baseline_matches_v04_except_factors():
    v03 = _load("first_board_v03")
    v04 = _load("first_board_v04")
    assert v03["scoring_models"][0]["factors"] == [
        "F1", "F2", "F3", "F4", "F5", "F6", "F7"]
    assert v03["composite"]["cap"] == 7.0
    for key in ("market_gate", "filters", "sell_rules"):
        assert v03[key] == v04[key], key      # 除因子/上限外完全同构


def test_default_snapshot_unchanged():
    with open(STRAT_DIR / "default.json", encoding="utf-8") as f:
        assert json.load(f) == DEFAULT_SNAPSHOT


def test_v04_runs_end_to_end_smoke():
    """空数据 ctx 全链路跑通: 因子 fail-open 0 → 无候选, 不抛异常。"""
    from prism.context import FactorContext
    from prism.engine import run_screen
    s = _load("first_board_v04")
    market_ctx = FactorContext(code="__MKT__", mkt={}, sector_map={})
    stock = FactorContext(code="600000.SH", kline=None, sector_map={},
                          limit_ups=[], mkt={})
    out = run_screen(s, market_ctx, gate_factors={"N1": 1},
                     stock_contexts={"600000.SH": stock})
    assert out["environment_ok"] is True
    assert out["candidates"] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest prism/tests/test_first_board_strategies.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt76`
Expected: FAIL（两个策略文件不存在）

- [ ] **Step 3: 创建策略文件**

`prism/strategies/first_board_v04.json`：

```json
{
  "id": "first_board_v04",
  "name": "首板0.04 三维度评估",
  "description": "F1-F7 基础上增加 F8 行业边际变化(商品期货20日涨幅>+3%)与 F9 板块延展性(有高度且扩张), 九因子 1.0 平权。",
  "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
  "scoring_models": [
    {"id": "first_board", "name": "首板", "weight": 1.0,
     "factors": ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9"]}
  ],
  "composite": {"mode": "top3_weighted", "weights": [1.0], "cap": 9.0},
  "filters": {"candidate_min_model": 3, "environment_threshold": 1},
  "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05, "max_hold_days": 5}
}
```

`prism/strategies/first_board_v03.json`：

```json
{
  "id": "first_board_v03",
  "name": "首板0.03 基线(F1-F7)",
  "description": "v04 的对照组: 仅 F1-F7 七因子 1.0 平权, 门槛/过滤/卖出与 v04 完全一致。",
  "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
  "scoring_models": [
    {"id": "first_board", "name": "首板", "weight": 1.0,
     "factors": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]}
  ],
  "composite": {"mode": "top3_weighted", "weights": [1.0], "cap": 7.0},
  "filters": {"candidate_min_model": 3, "environment_threshold": 1},
  "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05, "max_hold_days": 5}
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest prism/tests/test_first_board_strategies.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt76`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add prism/strategies/first_board_v04.json prism/strategies/first_board_v03.json prism/tests/test_first_board_strategies.py
git commit -m "feat(prism): first_board_v04 策略 + v03 基线 + default 快照保护"
```

---

### Task 7: 实盘接线 — mkt 快照 / sector_map / zt_prev 下发到个股 ctx

**背景（勘察发现）:** 实盘通路缺失——`provider.build_market_context()` 不组装 mkt、`build_stock_context(code)` 不传 sector_map、`run_screen` 也不下发。F4/SEC*/F8/F9 在实盘拿不到数据（恒 0/fail-open）。本任务补齐三段接线：数据组装（market_data + zt_history）→ provider 注入 → engine 下发。

**Files:**
- Modify: `prism/zt_history.py`（新增 `prev_day_pool`）
- Modify: `prism/market_data.py`（新增 `mkt_snapshot`）
- Modify: `prism/data.py`（`build_market_context` 注入 mkt/sector_map）
- Modify: `prism/engine.py`（`run_screen` 下发到个股 ctx）
- Modify: `prism/factors/factor_f9_sector_expansion.py`（docstring 补一条契约：`zt_prev["codes"]` 必须与 sector_map 键同格式（带 .SH/.SZ 后缀），否则 prev_n 恒 0 → 扩张恒真——Task 5 审查 Minor#1）
- Test: `prism/tests/test_live_mkt_injection.py`（新建）

**Interfaces:**
- Consumes: `.zt_history_index.pkl`（`{YYYYMMDD: [池条目]}`）、market_data 缓存、Task 2 `futures_snapshot`
- Produces: `zt_history.prev_day_pool(today=None) -> {"date": "YYYY-MM-DD"或None, "codes": [...]}`；`market_data.mkt_snapshot() -> {"sector","global","sector_flow","futures","zt_prev"}`（单段失败降级缺键）；provider `market_ctx._extra["mkt"/"sector_map"]`；`run_screen` 个股 ctx 下发（`_extra["mkt"]` setdefault + `sector_map` 字段为空时填充）

**行为说明（报告需向用户说明）:** 接线后 SEC1/SEC3/SEC4/F4/F8/F9 在实盘按设计生效（此前无数据恒 0）。default.json 文件不变，其 F4 因子从"恒 0"变为"真实计算"——属数据补齐修复而非策略变更。

- [ ] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""实盘 mkt/sector_map 接线测试 — 快照组装 + provider 注入 + run_screen 下发。全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
import prism.zt_history as zt_history
from prism import market_data as md
from prism.context import FactorContext
from prism.data import DataProvider
from prism.engine import load_strategy, run_screen


@pytest.fixture(autouse=True)
def _fresh_registry():
    reg.reset()
    yield
    reg.reset()
    reg.scan_factors("prism.factors", force=True)   # 恢复真实因子库


class _FakeEM:
    def get_market_stats(self):
        return None


class _FakeManual:
    def get_manual(self, code):
        return {}


class _FakeProvider(DataProvider):
    """绕过真实构造(不实例化线上数据源), 同 test_data.py 模式。"""

    def __init__(self):
        self.ds = None
        self.em_feed = _FakeEM()
        self.fund_feed = None
        self.manual = _FakeManual()
        self._em_stats = None
        self._limit_ups_cache = None


# ---------------- zt_history.prev_day_pool ----------------

def test_prev_day_pool_picks_latest_before_today(monkeypatch):
    monkeypatch.setattr(zt_history, "_load_index", lambda: {
        "20260701": [{"code": "600000.SH"}],
        "20260702": [{"code": "600000.SH"}, {"code": "000001.SZ"}],
        "20260703": [{"code": "x"}]})
    monkeypatch.setattr(
        zt_history, "qmt_zt_feed",
        lambda day, cache=None, index=None:
            [{"code": "600000.SH"}, {"code": "000001.SZ"}])
    out = zt_history.prev_day_pool(today="2026-07-03")
    assert out == {"date": "2026-07-02", "codes": ["600000.SH", "000001.SZ"]}


def test_prev_day_pool_empty_index(monkeypatch):
    monkeypatch.setattr(zt_history, "_load_index", lambda: {})
    out = zt_history.prev_day_pool(today="2026-07-03")
    assert out == {"date": None, "codes": []}


# ---------------- market_data.mkt_snapshot ----------------

def test_mkt_snapshot_assembles_all_sections(monkeypatch):
    monkeypatch.setattr(md, "_load_cache", lambda: {
        "kline": {"BK0475": {"dates": ["2026-07-01"], "close": [1.0]}},
        "global": {"NDX": {"dates": ["2026-07-01"], "close": [1.0]}},
        "flow": {"BK0475": {"dates": ["2026-07-01"], "main_net_in": [1.0]}}})
    monkeypatch.setattr(md, "futures_snapshot", lambda: {
        "BK0475": {"name": "基础化工",
                   "commodities": {"MA0": {"close": [1.0]}}}})
    monkeypatch.setattr(zt_history, "prev_day_pool",
                        lambda today=None: {"date": "2026-07-02",
                                            "codes": ["600000.SH"]})
    snap = md.mkt_snapshot()
    assert snap["sector"]["BK0475"]["close"] == [1.0]
    assert "NDX" in snap["global"]
    assert "BK0475" in snap["sector_flow"]
    assert snap["futures"]["BK0475"]["commodities"]["MA0"]["close"] == [1.0]
    assert snap["zt_prev"]["codes"] == ["600000.SH"]


def test_mkt_snapshot_fail_open(monkeypatch):
    monkeypatch.setattr(md, "_load_cache", lambda: {})

    def boom():
        raise RuntimeError("net down")
    monkeypatch.setattr(md, "futures_snapshot", boom)
    monkeypatch.setattr(zt_history, "prev_day_pool", boom)
    assert md.mkt_snapshot() == {}          # 全缺 → 空快照, 不抛


# ---------------- provider 注入 ----------------

def test_build_market_context_injects_mkt_and_sector_map(monkeypatch):
    monkeypatch.setattr(md, "_load_cache", lambda: {
        "kline": {},
        "sector_map": {"600000": {"sector": "BK0475", "name": "浦发"}}})
    monkeypatch.setattr(md, "mkt_snapshot", lambda: {
        "sector": {}, "zt_prev": {"date": None, "codes": ["600000.SH"]}})
    p = _FakeProvider()
    ctx = p.build_market_context()
    assert ctx.get("mkt")["zt_prev"]["codes"] == ["600000.SH"]
    assert ctx.get("sector_map")["600000.SH"] == "BK0475"


def test_build_market_context_fail_open(monkeypatch):
    def boom():
        raise RuntimeError("cache locked")
    monkeypatch.setattr(md, "_load_cache", boom)
    p = _FakeProvider()
    ctx = p.build_market_context()
    assert ctx.get("mkt") is None           # fail-open: 不抛, 无 mkt


# ---------------- run_screen 下发 ----------------

def _mk_strategy(for_factor):
    return {
        "id": "live_wire", "name": "下发测试", "description": "",
        "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0,
             "factors": [{"id": for_factor, "op": ">", "threshold": 0}]},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1, "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                       "max_hold_days": 5},
    }


def test_run_screen_downgrades_mkt_and_sector_map():
    @reg.factor(id="N1", name="n", category="node", description="")
    def f_n(ctx):
        return {"score": 1, "note": ""}

    @reg.factor(id="T8", name="t", category="test", description="")
    def f_t(ctx):
        ok = ((ctx.get("mkt") or {}).get("zt_prev") or {}).get("codes") \
            == ["x"] \
            and (ctx.sector_map or {}).get("600000.SH") == "BK0475"
        return {"score": 1 if ok else 0, "note": ""}

    s = load_strategy(_mk_strategy("T8"))
    market_ctx = FactorContext(code="__MKT__",
                               mkt={"zt_prev": {"codes": ["x"]}},
                               sector_map={"600000.SH": "BK0475"})
    stock = FactorContext(code="600000.SH", kline=None, sector_map={},
                          limit_ups=[])
    out = run_screen(s, market_ctx, gate_factors={"N1": 1},
                     stock_contexts={"600000.SH": stock})
    assert len(out["candidates"]) == 1      # 下发后 T8 在个股 ctx 命中
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest prism/tests/test_live_mkt_injection.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt77`
Expected: FAIL（`prev_day_pool`/`mkt_snapshot` 不存在；下发测试 T8 无候选）

- [ ] **Step 3: 实现（四个文件）**

3a. `prism/zt_history.py` 追加：

```python
def prev_day_pool(today=None):
    """上一交易日涨停代码表(F9 昨日基准)。

    today: "YYYY-MM-DD" 或 date; 默认今天。取索引中 < today 的最大日期,
    池条目取其 code 列表。无更早数据/空索引 → {"date": None, "codes": []}。
    只读本地索引, 不触网。
    """
    index = _load_index() or {}
    if not index:
        return {"date": None, "codes": []}
    if today is None:
        import datetime as _dt
        today = _dt.date.today()
    t = today.strftime("%Y%m%d") if hasattr(today, "strftime") \
        else str(today).replace("-", "")
    days = sorted(d for d in index if str(d) < t)
    if not days:
        return {"date": None, "codes": []}
    day = days[-1]
    items = qmt_zt_feed(day, index=index) or []
    return {"date": "%s-%s-%s" % (day[:4], day[4:6], day[6:8]),
            "codes": [it.get("code") for it in items if it.get("code")]}
```

3b. `prism/market_data.py` 追加：

```python
def mkt_snapshot():
    """组装引擎/实盘用的市场数据快照(run_screen 下发 ctx._extra["mkt"])。

    结构与回测 CLI 注入一致: sector/global/sector_flow + futures(F8)
    + zt_prev(F9)。全部读本地缓存(fetch_futures force=False 走缓存),
    单段失败降级缺键(fail-open), 不抛。
    """
    cache = _load_cache()
    snap = {}
    for key, ck in (("sector", "kline"), ("global", "global"),
                    ("sector_flow", "flow")):
        v = cache.get(ck)
        if v:
            snap[key] = v
    try:
        snap["futures"] = futures_snapshot()
    except Exception as e:
        logger.warning("futures 快照失败: %r", e)
    try:
        from prism.zt_history import prev_day_pool
        snap["zt_prev"] = prev_day_pool()
    except Exception as e:
        logger.warning("zt_prev 快照失败: %r", e)
    return snap
```

3c. `prism/data.py` `build_market_context` 在 `return ctx` 前追加：

```python
        # v04 接线: mkt 快照 + 行业映射注入 _extra(F8/F9/SEC* 因子用)。
        # 全部读本地缓存, 失败 fail-open(缺数据 → 因子得 0, 不阻塞选股)。
        try:
            from prism import market_data as _md
            cache = _md._load_cache()
            if cache:
                ctx._extra["mkt"] = _md.mkt_snapshot()
                smap = {}
                for c6, rec in (cache.get("sector_map") or {}).items():
                    if rec and rec.get("sector"):
                        suffix = ".SH" if c6.startswith("6") else ".SZ"
                        smap[c6 + suffix] = rec["sector"]
                ctx._extra["sector_map"] = smap
        except Exception:
            pass
```

3d. `prism/engine.py` `run_screen`：在 `if environment_ok and stock_contexts:` 块内首行（`min_model = ...` 之前）追加：

```python
        # v04 接线: 市场级数据(个股 ctx 构建时拿不到)统一下发到个股 ctx。
        # mkt 进 _extra(ctx.get("mkt")); sector_map 是字段, 个股未填时兜底。
        sec_map = (market_ctx.get("sector_map")
                   if market_ctx is not None else None)
        if mkt_extra:
            for ctx in stock_contexts.values():
                if isinstance(getattr(ctx, "_extra", None), dict):
                    ctx._extra.setdefault("mkt", mkt_extra)
        if sec_map:
            for ctx in stock_contexts.values():
                if not (getattr(ctx, "sector_map", None) or {}):
                    ctx.sector_map = sec_map
```

（`mkt_extra` 已在块外定义：`mkt_extra = market_ctx.get("mkt") ...`。）

- [ ] **Step 4: 跑测试确认通过 + 回归**

Run: `python -m pytest prism/tests/test_live_mkt_injection.py prism/tests/test_engine.py prism/tests/test_data.py prism/tests/test_app.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt77`
Expected: 全 passed（engine 下发对旧策略无行为影响：旧 ctx 未用 mkt 的因子不变）

- [ ] **Step 5: Commit**

```bash
git add prism/zt_history.py prism/market_data.py prism/data.py prism/engine.py prism/tests/test_live_mkt_injection.py
git commit -m "feat(prism): 实盘 mkt/sector_map/zt_prev 接线 — 市场级数据下发个股 ctx"
```

---

### Task 8: 回测对比验证（v03 vs v04，设计 §5 标准）

**Files:**
- Create: `pt_fb_compare.py`（pt_ 前缀已 gitignore，**不 commit**）

**Interfaces:**
- Consumes: Task 6 两策略文件、market_data 缓存 + futures 缓存（Task 2 采集）、zt/kline feed（复用 `backtest_cli.zt_feed/kline_feed`）、Task 4 注入链路
- Produces: 对比报告（全区间 + 样本外后半段）：笔数/均收益/胜率/**后续10日均涨幅** + §5 三项判定 PASS/FAIL

- [ ] **Step 1: 写对比脚本**

```python
# -*- coding: utf-8 -*-
"""v03 vs v04 回测对比验证(设计 §5)。
用法: python pt_fb_compare.py --start 20260101 --end 20260831
判定: ① v04 后续10日均涨幅 ≥ v03  ② v04 笔数 ≥ v03×70%
      ③ OOS(后半段) v04 后续10日均涨幅 ≥ v03(改善方向样本外保持)
"""
import argparse
import io
import sys
from datetime import date

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")

from backtest_cli import kline_feed, zt_feed
from prism import backtest, market_data as md
from prism.backtest import _parse_kline_date
from prism.engine import load_strategy

SIDS = ("first_board_v03", "first_board_v04")


def _mkt_sector_map():
    cache = md._load_cache()
    mkt = {"sector": cache.get("kline") or {},
           "global": cache.get("global") or {},
           "sector_flow": cache.get("flow") or {},
           "futures": md.futures_snapshot()}
    smap = {}
    for c6, rec in (cache.get("sector_map") or {}).items():
        if rec and rec.get("sector"):
            suffix = ".SH" if c6.startswith("6") else ".SZ"
            smap[c6 + suffix] = rec["sector"]
    print("市场数据: 板块 %d, futures %d, 映射 %d" % (
        len(mkt["sector"]), len(mkt["futures"]), len(smap)), file=sys.stderr)
    return mkt, smap


def _forward10(bt, trades):
    """每笔 entry 日起第 10 个交易日收盘涨幅(相对 entry close)。"""
    out = []
    for t in trades:
        kline = bt._kline_for(t["code"])
        if not kline:
            continue
        entry = date.fromisoformat(t["date"])
        idx = None
        for i, (d, _px) in enumerate(kline):
            dt = _parse_kline_date(d)
            if dt is not None and dt >= entry:
                idx = i
                break
        if idx is None or idx + 10 >= len(kline):
            continue
        base = kline[idx][1]
        if not base:
            continue
        out.append(kline[idx + 10][1] / base - 1)
    return out


def _run(sid, start, end, mkt, smap):
    s = load_strategy("prism/strategies/%s.json" % sid)
    bt = backtest.Backtester(s, zt_feed=zt_feed, kline_feed=kline_feed)
    rep = bt.run(start, end, mkt=mkt, sector_map=smap)
    trades = rep.get("trade_log") or []
    fwd = _forward10(bt, trades)
    rets = [t["return_pct"] for t in trades]
    return {
        "trades": len(trades),
        "avg_ret": sum(rets) / len(rets) if rets else 0.0,
        "win": (sum(1 for r in rets if r > 0) / len(rets)) if rets else 0.0,
        "fwd10_avg": sum(fwd) / len(fwd) if fwd else 0.0,
        "fwd10_n": len(fwd),
    }


def _fmt(tag, r):
    return ("%s: 笔数 %4d  均收益 %+6.2f%%  胜率 %5.1f%%  "
            "后10日均涨 %+6.2f%%(n=%d)"
            % (tag, r["trades"], r["avg_ret"] * 100, r["win"] * 100,
               r["fwd10_avg"] * 100, r["fwd10_n"]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True, help="YYYYMMDD")
    ap.add_argument("--end", required=True, help="YYYYMMDD")
    args = ap.parse_args()
    start = date(int(args.start[:4]), int(args.start[4:6]), int(args.start[6:8]))
    end = date(int(args.end[:4]), int(args.end[4:6]), int(args.end[6:8]))
    mkt, smap = _mkt_sector_map()

    full = {sid: _run(sid, start, end, mkt, smap) for sid in SIDS}
    mid = start + (end - start) / 2
    oos = {sid: _run(sid, mid, end, mkt, smap) for sid in SIDS}

    print("== 全区间 %s ~ %s ==" % (start, end))
    for sid in SIDS:
        print(_fmt("  " + sid, full[sid]))
    print("== OOS(后半段) %s ~ %s ==" % (mid, end))
    for sid in SIDS:
        print(_fmt("  " + sid, oos[sid]))

    ok1 = full["first_board_v04"]["fwd10_avg"] >= \
        full["first_board_v03"]["fwd10_avg"]
    ok2 = full["first_board_v04"]["trades"] >= \
        0.7 * full["first_board_v03"]["trades"]
    ok3 = oos["first_board_v04"]["fwd10_avg"] >= \
        oos["first_board_v03"]["fwd10_avg"]
    print("§5 判定: ①后10日≥旧版 %s  ②笔数≥70%% %s  ③OOS同向 %s"
          % (ok1, ok2, ok3))
    print("VERDICT:", "PASS" if (ok1 and ok2 and ok3) else "FAIL")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 采集期货数据（若 Task 2 Step 6 后未跑过）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -c "import sys; sys.stdout.reconfigure(encoding='utf-8'); from prism import market_data as md; print(len(md.fetch_futures()))"`
Expected: ≥10

- [ ] **Step 3: 跑对比**

Run: `$env:PYTHONIOENCODING='utf-8'; python pt_fb_compare.py --start 20260101 --end 20260831`
Expected: 打印两段对比表 + §5 判定。**首次以 FAIL 为预期结果之一——判定数字本身是产出**（事实记录，不是脚本错误）。

- [ ] **Step 4: 判定与调参（人工决策，最多 2 轮，顺序固定）**

1. 若 FAIL：先调 **F8 阈值**（`factor_f8_commodity_edge.py` THRESHOLD，+2%→+3%→+5% 即 0.02/0.03/0.05 三档），重跑对比；
2. 仍 FAIL：再放宽 **F9 为"满足其一"**（高度 or 扩张），重跑对比；
3. 2 轮后仍 FAIL：**保持 v03/v04 并存，报告用户实测数字，由用户决策**（不回改已批准设计以外的内容）。每轮调参改动连同数字记录进任务报告。

- [ ] **Step 5: 归档结果**

把最终对比表（两段 × 两策略）与 §5 判定原文贴进任务报告；若发生调参，注明最终 THRESHOLD/F9 口径与对应测试断言是否需要同步修改（改了就要改测试并重跑）。

---

### Task 9: 全量回归 + 收尾

**Files:** 无新增（只验证）

- [ ] **Step 1: 全量测试**

Run: `python -m pytest prism/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt78`
Expected: 全 passed（440 基线 + 本轮新增 ≈ 470），0 failed

- [ ] **Step 2: 因子体检**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m prism.factor_check`
Expected: 44 因子全 PASS（42 存量 + F8 + F9）

- [ ] **Step 3: 提交完整性核对**

Run: `git log --oneline -8` + `git status`
Expected: 本计划 6 个 commit（Task 2/3/4/5/6/7 各 1；Task 1 探针与 Task 8 对比脚本为 pt_* 不提交）；工作区只剩 pt_* 与报告类未跟踪文件

- [ ] **Step 4: 报告**

实现子代理产出报告：各任务测试证据（RED/GREEN 输出摘要）、§5 最终数字、调参轨迹（如有）、行为变化清单（F4 回测激活 / 实盘因子生效）。**push 前必须征得用户同意。**

---

## Self-review（计划完成时已核对）

1. **spec 覆盖**：§3.1 F8 → Task 3；§3.2 F9 → Task 5（口径修正：题材=申万板块代理，已注明）；§4 策略 → Task 6；§5 验证 → Task 8（含调参顺序与 2 轮上限）；§6 测试 → 各任务 TDD；§7 期货可用性风险 → Task 1 探针 + Task 2 降级路径；勘察新发现的实盘通路缺失 → Task 7。
2. **无占位符**：所有新文件给完整代码；对既有函数的修改给出精确锚点（签名行/插入点）与完整新增片段，Task 4 循环骨架中 `...` 仅为"保持原循环体"引用标记，非待写代码。
3. **类型一致**：`mkt["futures"]`（板块代码→{name, commodities{品种→{name,dates,close}}}）在 Task 2/3/7/8 一致；`mkt["zt_prev"]={"date","codes"}` 在 Task 4/5/7/8 一致；因子返回 `{"score":0/1,"note":str}`。
4. **风险**：探针品种不足 10 个 → Task 1 BLOCKED 上报；期货接口全挂 → F8 纯 fail-open（v04 退化为 F1-F7+F9），策略仍可用；调参上限 2 轮，最终决策权在用户。

## 执行方式

- **1. 子代理驱动（推荐，前两轮均选此）**：按 SDD 流程逐任务派实现+审查子代理，台账记录。
- **2. Inline**：我在本会话逐任务直接实现。
