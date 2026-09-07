# 板块感知层实现计划（孕育期 / 阶段定位 / 资金惯性）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 板块从"捕捉信号"到"感知信号"——孕育期候选 + 五阶段定位 + 资金惯性表，全部观察模式（不进因子打分），GUI 新「板块观察」tab。

**Architecture:** 数据层复用东财 CLIST f62 当日快照（前向累积 flow_rank）+ fetch_kline 上证基准（benchmark）；计算层纯函数 `sector_stage.py` 只吃 mkt 切片（sector_score 同风格）；GUI 只读端点 + 面板。

**Tech Stack:** Python 3.12 / Flask / pytest（全离线罐头注入）。

**Spec:** `docs/superpowers/specs/2026-09-06-sector-perception-design.md`

## Global Constraints

- 全部离线测试：网络实现可注入（EastMoneyProbe http_get），禁真实网络断言
- ponytail 阶梯：needs-to-exist→reuse→stdlib→one-line→minimal；绝不简化掉校验完整性/原子写/指针回落/default保护；`# ponytail:` 标记刻意取舍
- 缺数据 fail-open：信号不可判不计数，不抛异常不 500
- 不进因子/策略/模拟盘链路（用户拍板观察模式）；不动 SEC3 申万 flow 段
- 测试命令：`$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_btNN`（NN 递增，本次从 **162** 起）
- 提交信息中文，`feat:`/`test:`/`docs:` 前缀；每任务独立提交

---

### Task 1: 数据层——flow_rank 前向累积 + benchmark 上证基准

**Files:**
- Modify: `prism/market_data.py`（EastMoneyProbe 两个 fetch 方法；模块级 build_flow_rank/build_benchmark；mkt_snapshot 透出+板块名注入；CLI 两参数）
- Test: `prism/tests/test_market_data.py`（追加）

**Interfaces:**
- Consumes: 现有 `EastMoneyProbe._get_json / fetch_kline / fetch_sector_list` 翻页与 total 断路模式、`_save_cache` 合并语义、`_load_cache`、`SECTOR_FS`、`BACKFILL_BEG`
- Produces（Task 2/3 依赖）:
  - `cache["flow_rank"] = {"dates": [升序...], "rows": {date: [{"code","name","net_in"}...]}}`
  - `cache["benchmark"] = {"dates": [...], "close": [...], "amount": [...]}`
  - `mkt_snapshot()` 含 `"sector"`(rec 带 `name` 键) / `"benchmark"` / `"flow_rank"` 三段
  - `build_flow_rank(probe=None) -> {"dates": n, "sectors_today": n}`
  - `build_benchmark(probe=None, beg=BACKFILL_BEG, end=None) -> {"days": n, "kept_old": bool}`

- [x] **Step 1: 写失败测试**（追加到 test_market_data.py 末尾）

```python
# ---------------------------------------------------------------- 惯性/基准

def test_fetch_flow_rank_parses():
    data = _resp({"total": 3, "diff": [
        {"f12": "BK0486", "f14": "传媒", "f62": 6.174e9},
        {"f12": "BK0433", "f14": "农林牧渔", "f62": 3.016e9},
        {"f12": "BK0000", "f14": "无数据", "f62": "-"}]})
    p, _ = _fake_probe({md.EastMoneyProbe.CLIST_URL: data})
    rows = p.fetch_flow_rank()
    assert [r["code"] for r in rows] == ["BK0486", "BK0433"]
    assert rows[0]["net_in"] == 6.174e9
    assert rows[0]["name"] == "传媒"


def test_build_flow_rank_idempotent(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    d1 = _resp({"total": 2, "diff": [
        {"f12": "BK0486", "f14": "传媒", "f62": 1e9},
        {"f12": "BK0433", "f14": "农林牧渔", "f62": 5e8}]})
    p1 = md.EastMoneyProbe(http_get=FakeGetter({md.EastMoneyProbe.CLIST_URL: d1}))
    r1 = md.build_flow_rank(probe=p1)
    assert r1 == {"dates": 1, "sectors_today": 2}
    # 盘后重跑: 当日覆盖, 不累积重复日期
    d2 = _resp({"total": 2, "diff": [
        {"f12": "BK0486", "f14": "传媒", "f62": 9e8},
        {"f12": "BK0433", "f14": "农林牧渔", "f62": 7e8}]})
    p2 = md.EastMoneyProbe(http_get=FakeGetter({md.EastMoneyProbe.CLIST_URL: d2}))
    r2 = md.build_flow_rank(probe=p2)
    assert r2["dates"] == 1
    fr = md._load_cache()["flow_rank"]
    assert len(fr["dates"]) == 1
    assert fr["rows"][fr["dates"][0]][0]["net_in"] == 9e8


def test_build_benchmark(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    data = _resp({"klines": ["2026-07-01,4147.72,30064854350.00",
                             "2026-07-02,4160.67,32598801572.00"]})
    p, _ = _fake_probe({md.EastMoneyProbe.KLINE_URL: data})
    r = md.build_benchmark(probe=p)
    assert r == {"days": 2, "kept_old": False}
    assert md._load_cache()["benchmark"]["close"][0] == 4147.72


def test_build_benchmark_keeps_old_on_failure(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    md._save_cache({"benchmark": {"dates": ["2026-07-01"], "close": [4000.0],
                                  "amount": [1e10]}})
    p, _ = _fake_probe({}, fail_urls=[md.EastMoneyProbe.KLINE_URL])
    r = md.build_benchmark(probe=p)
    assert r["kept_old"] is True
    assert md._load_cache()["benchmark"]["close"] == [4000.0]


def test_mkt_snapshot_new_segments(_ws_tmp, monkeypatch):
    monkeypatch.setattr(md, "CACHE_PATH", _ws_tmp / "mkt.pkl")
    monkeypatch.setattr(md, "INDEX_PATH", _ws_tmp / "mkt_idx.pkl")
    monkeypatch.setattr(md, "futures_snapshot", lambda: {})
    md._save_cache({
        "kline": {"801010": {"dates": ["2026-09-05"], "close": [100.0],
                             "amount": [1e8]}},
        "sectors": {"801010": {"name": "农林牧渔"}},
        "benchmark": {"dates": ["2026-09-05"], "close": [4000.0],
                      "amount": [1e10]},
        "flow_rank": {"dates": ["2026-09-05"],
                      "rows": {"2026-09-05": [{"code": "BK0486",
                                               "name": "传媒",
                                               "net_in": 1e9}]}}})
    snap = md.mkt_snapshot()
    assert snap["benchmark"]["close"] == [4000.0]
    assert snap["flow_rank"]["dates"] == ["2026-09-05"]
    assert snap["sector"]["801010"]["name"] == "农林牧渔"
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_market_data.py -q -k "flow_rank or benchmark or snapshot_new" --import-mode=importlib`
Expected: FAIL（`fetch_flow_rank`/`build_flow_rank`/`build_benchmark` 属性不存在）

- [x] **Step 3: 实现**

3a. `EastMoneyProbe` 内（fetch_global_kline 之后）追加两个方法：

```python
    def fetch_flow_rank(self, page_size=100):
        """全行业板块当日主力净流入(f62) → [{code, name, net_in}] 降序。
        f62 非数值(如 '-')的行跳过。失败 → 抛。"""
        items = []
        pn = 1
        while True:
            data = self._get_json(self.CLIST_URL, {
                "pn": pn, "pz": page_size, "po": 1, "np": 1,
                "fltt": 2, "invt": 2, "fid": "f62",
                "fs": SECTOR_FS, "fields": "f12,f14,f62"})
            diff = ((data or {}).get("data") or {}).get("diff") or []
            for it in diff:
                if not isinstance(it, dict):
                    continue
                try:
                    net = float(it.get("f62"))
                except (TypeError, ValueError):
                    continue    # '-' 等无数据行
                items.append({"code": str(it.get("f12") or ""),
                              "name": str(it.get("f14") or ""),
                              "net_in": net})
            total = ((data or {}).get("data") or {}).get("total") or 0
            if not diff or len(items) >= int(total):
                break
            pn += 1
        items.sort(key=lambda r: r["net_in"], reverse=True)
        return items

    def fetch_benchmark_kline(self, beg, end):
        """上证指数日K(secid=1.000001) → [{date, close, amount}]。失败 → 抛。"""
        return self.fetch_kline("1.000001", beg, end, fields2="f51,f53,f57")
```

3b. 模块级（`build_sector_map` 之前）追加：

```python
def build_flow_rank(probe=None):
    """当日行业板块主力净流入快照 → 前向累积落盘 cache["flow_rank"]。

    东财 BK 细分行业口径, 自洽使用(不回填 SEC3 申万 flow 段)。
    当日已在 dates → 覆盖当日行(幂等, 盘后重跑取终值)。
    返回 {"dates": n, "sectors_today": n}。失败 → 抛 MarketDataError。"""
    probe = probe or EastMoneyProbe()
    rows = probe.fetch_flow_rank()
    cache = _load_cache()
    fr = cache.get("flow_rank") or {}
    dates = list(fr.get("dates") or [])
    rmap = dict(fr.get("rows") or {})
    today = date.today().strftime("%Y-%m-%d")
    if today not in dates:
        dates.append(today)
    rmap[today] = rows
    _save_cache({"flow_rank": {"dates": dates, "rows": rmap}})
    return {"dates": len(dates), "sectors_today": len(rows)}


def build_benchmark(probe=None, beg=BACKFILL_BEG, end=None):
    """上证指数日K → cache["benchmark"] 全量替换(单指数成本低, 自愈)。

    拉取失败/为空 → 保留旧缓存(fail-open)。
    返回 {"days": n, "kept_old": bool}。"""
    probe = probe or EastMoneyProbe()
    end = end or date.today().strftime("%Y%m%d")
    old = _load_cache().get("benchmark") or {}
    old_days = len(old.get("dates") or [])
    try:
        kl = probe.fetch_benchmark_kline(beg, end)
    except MarketDataError as e:
        logger.warning("上证基准拉取失败, 保留旧缓存(%d日): %r", old_days, e)
        return {"days": old_days, "kept_old": True}
    if not kl:
        logger.warning("上证基准拉取为空, 保留旧缓存(%d日)", old_days)
        return {"days": old_days, "kept_old": True}
    _save_cache({"benchmark": {"dates": [r["date"] for r in kl],
                               "close": [r["close"] for r in kl],
                               "amount": [r.get("amount") for r in kl]}})
    return {"days": len(kl), "kept_old": False}
```

3c. `mkt_snapshot()`：原三元组循环替换（sector 段注入板块名，新增两段透出）：

```python
    cache = _load_cache()
    snap = {}
    for key, ck in (("global", "global"), ("sector_flow", "flow"),
                    ("benchmark", "benchmark"), ("flow_rank", "flow_rank")):
        v = cache.get(ck)
        if v:
            snap[key] = v
    kline = cache.get("kline") or {}
    if kline:
        # sector 段附加板块名(GUI 阶段面板显示名称; 因子只读 close/amount 不受影响)
        names = {c: (i or {}).get("name") or ""
                 for c, i in (cache.get("sectors") or {}).items()}
        snap["sector"] = {c: dict((rec or {}), name=names.get(c, ""))
                          for c, rec in kline.items()}
```
（后续 futures/zt_prev 两段保持原样。注：`dict(rec, name=...)` 等价 rec2 写法，任选其一。）

3d. CLI（`build_cli` argparse 追加两参数；分支处理放在 `--build-sector-map` 分支之后、stats 兜底之前）：

```python
    ap.add_argument("--build-flow-rank", action="store_true",
                    help="当日板块主力净流入快照(前向累积, 幂等)")
    ap.add_argument("--build-benchmark", action="store_true",
                    help="上证指数日K基准(全量替换, 失败保留旧缓存)")
```

```python
    if args.build_flow_rank:
        r = build_flow_rank()
        print("资金惯性快照:", r)
        return
    if args.build_benchmark:
        r = build_benchmark(beg=args.beg)
        print("上证基准:", r)
        return
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_market_data.py -q --import-mode=importlib`
Expected: 全 PASS（含原有用例）

- [x] **Step 5: 提交**

```bash
git add prism/market_data.py prism/tests/test_market_data.py
git commit -m "feat: 资金惯性快照前向累积+上证基准段(CLIST f62 实测通, fflow 历史仍封)"
```

---

### Task 2: 计算层 prism/sector_stage.py

**Files:**
- Create: `prism/sector_stage.py`
- Test: `prism/tests/test_sector_stage.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 mkt 快照段（`sector` rec 带 `name`；`benchmark`；`flow_rank`）
- Produces（Task 3 依赖，签名逐字）:
  - `sector_table(mkt) -> list[dict]`，行: `{code, name, stage, note, r3, r5, r10, share5, share20, share_chg, hits, signals}`
  - `flow_inertia(flow_rank, top_n=FLOW_TOP_N, need=FLOW_STREAK_MIN) -> list[dict]`，行: `{code, name, streak, last_net_in, systematic}`
  - 常量: `GEST_MAX_R5=8.0, REL_DAYS=3, REL_MIN=2, SHARE_MIN_DAYS=20, STAGE_R5_MAIN=5.0, STAGE_R3_START=4.0, STAGE_R10_START_MAX=5.0, STAGE_R5_PEAK=10.0, STAGE_SHARE_PCT=0.90, FLOW_TOP_N=3, FLOW_STREAK_MIN=5`

- [x] **Step 1: 写失败测试**（新建 test_sector_stage.py）

```python
# -*- coding: utf-8 -*-
"""sector_stage 单元测试 — 罐头 mkt 切片, 全离线确定性。"""
import sys
from datetime import date, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import prism.sector_stage as ss


def _dates(n):
    """n 个伪交易日(ISO 字符串; 与自然日历无关, date 键对齐即可)。"""
    d0 = date(2026, 6, 1)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _mk(sectors, bench=None, n=30):
    """sectors: {code: (closes, amounts)}; bench: 上证 closes(同 n)。"""
    ds = _dates(n)
    sec = {c: {"dates": ds, "close": cl, "amount": am, "name": "S" + c}
           for c, (cl, am) in sectors.items()}
    mkt = {"sector": sec}
    if bench is not None:
        mkt["benchmark"] = {"dates": ds, "close": bench, "amount": [1e8] * n}
    return mkt


def _row(table, code):
    return next(r for r in table if r["code"] == code)


# ---------------- 占比 ----------------

def test_share_series_ratio():
    n = 25
    mkt = _mk({"A": ([100.0] * n, [3e8] * n),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert abs(a["share5"] - 0.75) < 1e-9
    assert abs(a["share20"] - 0.75) < 1e-9
    assert abs(a["share_chg"]) < 1e-9


# ---------------- 孕育信号 ----------------

def test_gestation_share_struct_hits():
    """无 benchmark: share+struct 两信号 → 孕育期。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 103.0]
    mkt = _mk({"A": (closes, [1e8] * 20 + [4e8] * 10),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "孕育期"
    assert a["signals"] == ["share", "struct"]
    assert a["hits"] == 2


def test_gestation_rel_with_benchmark():
    """上证走平、板块末3日连涨 → rel 信号命中。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 103.0]
    mkt = _mk({"A": (closes, [1e8] * n),
               "B": ([100.0] * n, [1e8] * n)}, bench=[1000.0] * n, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert "rel" in a["signals"]
    assert a["stage"] == "孕育期"


def test_rel_skipped_without_benchmark():
    """benchmark 缺失 → rel 不可判不计数(fail-open)。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 103.0]
    mkt = _mk({"A": (closes, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert "rel" not in a["signals"]


def test_gestation_excluded_when_launched():
    """5日涨 12% ≥ 8% 未启动上限 → 不判孕育(判高潮: 占比分位+大涨)。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 102.0, 104.0, 106.0, 108.0, 112.0]
    mkt = _mk({"A": (closes, [1e8] * 20 + [4e8] * 10),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "高潮期"


# ---------------- 五阶段分支 ----------------

def test_stage_ebb():
    """占比回落 + 5日下跌 → 退潮期。"""
    n = 30
    closes = [100.0] * 24 + [103.0, 102.5, 102.0, 101.5, 101.0, 100.0]
    mkt = _mk({"A": (closes, [4e8] * 20 + [1e8] * 10),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "退潮期"


def test_stage_peak():
    """占比历史分位≥90% 且 5日涨>10% → 高潮期。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 102.0, 104.0, 106.0, 108.0, 112.0]
    mkt = _mk({"A": (closes, [1e8] * 29 + [20e8]),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "高潮期"


def test_stage_main():
    """5日涨6%>5% 且占比升 → 主升期(占比分位命中但涨幅≤10%不判高潮)。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 106.0]
    mkt = _mk({"A": (closes, [1e8] * 20 + [2e8] * 10),
               "B": ([100.0] * n, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "主升期"


def test_stage_start():
    """3日涨4.3%>4% 且 10日涨4.5%≤5% → 启动期(先于孕育判定)。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.0, 100.0, 100.2, 102.0, 104.5]
    mkt = _mk({"A": (closes, [1e8] * n),
               "B": ([100.0] * n, [1e8] * n)}, bench=[1000.0] * n, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "启动期"


def test_stage_rest():
    """全平: 无任何信号 → 休整。"""
    n = 30
    mkt = _mk({"A": ([100.0] * n, [1e8] * n),
               "B": ([100.0] * n, [1e8] * n)}, bench=[1000.0] * n, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "休整"


def test_stage_insufficient_data():
    """K线<11 根 → 数据不足(不抛)。"""
    mkt = _mk({"A": ([100.0] * 10, [1e8] * 10)}, n=10)
    a = _row(ss.sector_table(mkt), "A")
    assert a["stage"] == "数据不足"


def test_sector_table_metrics():
    """r3/r5/r10 数值与行结构。"""
    n = 30
    closes = [100.0] * 24 + [100.0, 100.5, 101.0, 101.5, 102.0, 103.0]
    mkt = _mk({"A": (closes, [1e8] * n)}, n=n)
    a = _row(ss.sector_table(mkt), "A")
    assert abs(a["r5"] - 3.0) < 1e-9
    assert abs(a["r10"] - 3.0) < 1e-9
    assert abs(a["r3"] - (103.0 / 101.0 - 1) * 100) < 1e-9
    assert a["name"] == "SA"
    assert set(a) == {"code", "name", "stage", "note", "r3", "r5", "r10",
                      "share5", "share20", "share_chg", "hits", "signals"}


# ---------------- 资金惯性 ----------------

def test_flow_inertia_streaks():
    """A/B 连续6天前3(系统性增配), C 中途掉出(streak0 剔除), D 后2天上榜。"""
    dates, rows = [], {}
    for i in range(6):
        d = "2026-09-%02d" % (i + 1)
        dates.append(d)
        day = [{"code": "BK_A", "name": "甲", "net_in": 5e8},
               {"code": "BK_B", "name": "乙", "net_in": 4e8},
               {"code": ("BK_C" if i < 4 else "BK_E"), "name": "x",
                "net_in": 1e8}]
        if i >= 4:
            day.append({"code": "BK_D", "name": "丁", "net_in": 2e8})
        rows[d] = day
    out = ss.flow_inertia({"dates": dates, "rows": rows})
    assert [r["code"] for r in out] == ["BK_A", "BK_B", "BK_D"]
    assert out[0]["streak"] == 6 and out[0]["systematic"] is True
    assert out[1]["streak"] == 6 and out[1]["systematic"] is True
    assert out[2]["streak"] == 2 and out[2]["systematic"] is False
    assert out[2]["last_net_in"] == 2e8


def test_flow_inertia_empty():
    assert ss.flow_inertia(None) == []
    assert ss.flow_inertia({}) == []
    assert ss.flow_inertia({"dates": [], "rows": {}}) == []
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_sector_stage.py -q --import-mode=importlib`
Expected: FAIL（No module named prism.sector_stage）

- [x] **Step 3: 实现 sector_stage.py**

```python
# -*- coding: utf-8 -*-
"""板块感知层(纯计算): 孕育期候选 / 五阶段定位 / 资金惯性。

设计: docs/superpowers/specs/2026-09-06-sector-perception-design.md
- 只依赖 mkt 切片(dict), 不碰网络/缓存/registry(sector_score 同风格)。
- 观察模式: 输出仅供复盘面板, 不进因子打分(用户 2026-09-06 拍板)。
- 缺数据降级: 信号不可判不计数, 阶段标"数据不足", 惯性表返回 []。
- 防未来: 调用方必须传 asof 切片(GUI 用 mkt_snapshot 全量, 天然只见过去)。
"""
# 阈值全部模块级常量(复盘观察用, 口径见设计 spec §2)
GEST_MAX_R5 = 8.0         # 孕育期"未启动"上限: 近5日涨幅 < 8%
REL_DAYS = 3              # 跑赢信号观察窗: 近3日
REL_MIN = 2               # 其中≥2日板块日涨幅>上证
SHARE_MIN_DAYS = 20       # 占比信号: 至少20日K线才可比 MA5/MA20
STAGE_R5_MAIN = 5.0       # 主升期: 近5日涨幅 > 5%
STAGE_R3_START = 4.0      # 启动期: 近3日涨幅 > 4%
STAGE_R10_START_MAX = 5.0  # 启动期: 且近10日涨幅 ≤ 5%(跳升刚脱离盘整)
STAGE_R5_PEAK = 10.0      # 高潮期: 近5日涨幅 > 10%
STAGE_SHARE_PCT = 0.90    # 高潮期: 最新日占比处自身历史≥90分位
FLOW_TOP_N = 3            # 惯性表: 每日净流入前3
FLOW_STREAK_MIN = 5       # 系统性增配: 连续上榜≥5天


def _ret_series(dates, closes):
    """日涨幅(%)序列 → {date: ret%}。前收缺失/非正的日期不计。"""
    out = {}
    for i in range(1, len(dates)):
        prev = closes[i - 1]
        if prev and prev > 0:
            out[dates[i]] = (closes[i] / prev - 1.0) * 100.0
    return out


def _share_series(mkt):
    """全板块日成交额占比 → (dates, {code: [share_t...]})(与 dates 对齐,
    某板块当日无数据 → None)。总量 = 当日有数据板块之和(同源, 无需基准)。"""
    secs = mkt.get("sector") or {}
    amounts = {}
    all_dates = set()
    for code, rec in secs.items():
        rec = rec or {}
        m = {}
        for d, a in zip(rec.get("dates") or [], rec.get("amount") or []):
            if a and a > 0:
                m[d] = float(a)
        if m:
            amounts[code] = m
            all_dates.update(m)
    dates = sorted(all_dates)
    totals = [sum(m.get(d, 0.0) for m in amounts.values()) for d in dates]
    shares = {}
    for code, m in amounts.items():
        col = []
        for i, d in enumerate(dates):
            v = m.get(d)
            col.append(v / totals[i] if v and totals[i] > 0 else None)
        shares[code] = col
    return dates, shares


def _mean(vals):
    vs = [v for v in vals if v is not None]
    return sum(vs) / len(vs) if vs else None


def _signals(rec, bench_ret, share_col):
    """孕育期三信号(命中名列表): rel(跑赢上证) / share(占比MA5>MA20) /
    struct(站上5日线且近5日收阳≥3)。不可判的信号不计数(fail-open)。"""
    dates = rec.get("dates") or []
    closes = [c for c in (rec.get("close") or []) if c]
    out = []
    # rel: 近 REL_DAYS 日中≥REL_MIN 日板块日涨幅 > 上证日涨幅(交易日对齐)
    if bench_ret:
        sret = _ret_series(dates, closes)
        start = max(1, len(dates) - REL_DAYS)
        wins = total = 0
        for i in range(start, len(dates)):
            d = dates[i]
            if d in bench_ret and d in sret:
                total += 1
                if sret[d] > bench_ret[d]:
                    wins += 1
        if total > 0 and wins >= REL_MIN:
            out.append("rel")
    # share: 占比 MA5 > MA20
    share5 = _mean(share_col[-5:]) if share_col else None
    share20 = _mean(share_col[-20:]) if share_col else None
    if share5 is not None and share20 is not None and share5 > share20:
        out.append("share")
    # struct: 站上5日线 且 近5日收阳≥3
    if len(closes) >= 6:
        ma5 = sum(closes[-5:]) / 5.0
        up_days = sum(1 for i in range(len(closes) - 5, len(closes))
                      if closes[i] > closes[i - 1])
        if closes[-1] > ma5 and up_days >= 3:
            out.append("struct")
    return out


def _r(closes, n):
    """近 n 日涨幅(%); 数据不足 → None。"""
    if len(closes) <= n:
        return None
    prev = closes[-n - 1]
    if not prev or prev <= 0:
        return None
    return (closes[-1] / prev - 1.0) * 100.0


def _stage_of(closes, share_col, r3, r5, r10, signals, hits):
    """阶段判定(先到先得): 退潮>高潮>主升>启动>孕育>休整。"""
    have_share = [v for v in share_col if v is not None]
    if len(closes) < 11 or len(have_share) < SHARE_MIN_DAYS:
        return "数据不足", "K线或占比不足"
    share5 = _mean(share_col[-5:])
    share20 = _mean(share_col[-20:])
    # 退潮: 占比回落且下跌
    if share5 < share20 and r5 < 0:
        return "退潮期", "占比回落(%.4f<%.4f)且5日跌%.1f%%" % (
            share5, share20, r5)
    # 高潮: 最新日占比处自身历史≥90分位 且 大涨
    last_share = have_share[-1]
    rank = sum(1 for v in have_share if v <= last_share) / len(have_share)
    if r5 > STAGE_R5_PEAK and rank >= STAGE_SHARE_PCT:
        return "高潮期", "占比分位%.0f%%且5日涨%.1f%%" % (rank * 100, r5)
    # 主升: 涨且占比升
    if r5 > STAGE_R5_MAIN and share5 > share20:
        return "主升期", "5日涨%.1f%%且占比升(%.4f>%.4f)" % (
            r5, share5, share20)
    # 启动: 3日跳升且10日涨幅仍小(先于孕育判, 启动非孕育)
    if r3 > STAGE_R3_START and r10 <= STAGE_R10_START_MAX:
        return "启动期", "3日涨%.1f%%且10日涨%.1f%%" % (r3, r10)
    # 孕育: 未启动且信号≥2项
    if r5 < GEST_MAX_R5 and hits >= 2:
        return "孕育期", "信号%d项(%s)" % (hits, "+".join(signals))
    return "休整", "无阶段特征"


def sector_table(mkt):
    """mkt 切片 → 全板块感知表(孕育/阶段, 观察模式)。

    行: {code, name, stage, note, r3, r5, r10, share5, share20,
         share_chg(share5-share20), hits, signals}。mkt 空 → []。"""
    if not isinstance(mkt, dict) or not mkt:
        return []
    bench = mkt.get("benchmark") or {}
    bench_ret = _ret_series(bench.get("dates") or [], bench.get("close") or [])
    _, shares = _share_series(mkt)
    out = []
    for code, rec in sorted((mkt.get("sector") or {}).items()):
        rec = rec or {}
        closes = [c for c in (rec.get("close") or []) if c]
        share_col = shares.get(code) or []
        r3, r5, r10 = _r(closes, 3), _r(closes, 5), _r(closes, 10)
        signals = _signals(rec, bench_ret, share_col)
        hits = len(signals)
        share5 = _mean(share_col[-5:]) if share_col else None
        share20 = _mean(share_col[-20:]) if share_col else None
        if r5 is None or share5 is None or share20 is None:
            stage, note = "数据不足", "K线或占比不足"
        else:
            stage, note = _stage_of(closes, share_col, r3, r5, r10,
                                    signals, hits)
        out.append({"code": str(code), "name": rec.get("name") or "",
                    "stage": stage, "note": note,
                    "r3": r3, "r5": r5, "r10": r10,
                    "share5": share5, "share20": share20,
                    "share_chg": (share5 - share20
                                  if share5 is not None
                                  and share20 is not None else None),
                    "hits": hits, "signals": signals})
    return out


def flow_inertia(flow_rank, top_n=FLOW_TOP_N, need=FLOW_STREAK_MIN):
    """资金惯性表: 每日净流入前 top_n 的板块连续上榜天数(streak)。

    flow_rank: {"dates": [...], "rows": {date: [{code,name,net_in}...]}}
    → [{code, name, streak, last_net_in, systematic(streak≥need)}],
    streak≥1 才输出, 按 streak 降序再净流入降序。输入空/缺 → []。"""
    if not isinstance(flow_rank, dict):
        return []
    dates = sorted(flow_rank.get("dates") or [])
    rows = flow_rank.get("rows") or {}
    if not dates:
        return []
    top_sets = []
    for d in dates:
        day = sorted((r for r in (rows.get(d) or []) if isinstance(r, dict)),
                     key=lambda r: r.get("net_in") or 0, reverse=True)
        top_sets.append({r.get("code") for r in day[:top_n]})
    names, lasts = {}, {}
    for d in dates:      # 升序遍历, 最近一天的名称/净流入生效
        for r in rows.get(d) or []:
            if isinstance(r, dict) and r.get("code"):
                names[r["code"]] = r.get("name") or ""
                lasts[r["code"]] = r.get("net_in")
    out = []
    for code in names:
        streak = 0
        for top in reversed(top_sets):
            if code in top:
                streak += 1
            else:
                break
        if streak >= 1:
            out.append({"code": code, "name": names.get(code, ""),
                        "streak": streak, "last_net_in": lasts.get(code),
                        "systematic": streak >= need})
    out.sort(key=lambda r: (-r["streak"], -(r["last_net_in"] or 0)))
    return out
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_sector_stage.py -q --import-mode=importlib`
Expected: 14 PASS

- [x] **Step 5: 全量回归（确保未破坏其他模块）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt162`
Expected: 基线绿（440 绿 + 1 环境失败量级 + 本任务新增）

- [x] **Step 6: 提交**

```bash
git add prism/sector_stage.py prism/tests/test_sector_stage.py
git commit -m "feat: sector_stage 纯计算层(孕育信号/五阶段判定/资金惯性 streak)"
```

---

### Task 3: GUI——/api/sector_stage 端点 + 「板块观察」tab

**Files:**
- Modify: `prism_web/app.py`（顶部 import + 新路由，放在 `/api/factors` 路由附近）
- Modify: `prism_web/templates/index.html`（nav 按钮 + section）
- Modify: `prism_web/static/app.js`（switchTab 钩子 + loadSectorStage 渲染）
- Modify: `prism_web/static/style.css`（阶段徽标配色）
- Test: `prism_web/tests/test_app.py`（追加）

**Interfaces:**
- Consumes: Task 1 `market_data.mkt_snapshot()`、Task 2 `sector_stage.sector_table/flow_inertia`
- Produces: `GET /api/sector_stage` → `{"ok": bool, "date": "YYYY-MM-DD", "sectors": [行...], "inertia": [行...], "flow_days": int}`（fail-open 永不 500）

- [x] **Step 1: 写失败测试**（追加到 test_app.py 末尾）

```python
# ---------------- 板块观察 ----------------

def _sector_stage_snap():
    """罐头 mkt 快照: A 平淡(休整) + 惯性 1 条。"""
    from datetime import date, timedelta
    ds = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(30)]
    return {
        "sector": {"801010": {"name": "农林牧渔", "dates": ds,
                              "close": [100.0] * 30,
                              "amount": [1e8] * 30}},
        "benchmark": {"dates": ds, "close": [1000.0] * 30,
                      "amount": [1e8] * 30},
        "flow_rank": {"dates": ds[-2:],
                      "rows": {ds[-2]: [{"code": "BK0433",
                                         "name": "农林牧渔",
                                         "net_in": 1e9}],
                               ds[-1]: [{"code": "BK0433",
                                         "name": "农林牧渔",
                                         "net_in": 2e9}]}},
    }


def test_sector_stage_endpoint(client, monkeypatch):
    import prism.market_data as md
    monkeypatch.setattr(md, "mkt_snapshot", _sector_stage_snap)
    r = client.get("/api/sector_stage")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["date"] == "2026-09-30"
    assert data["flow_days"] == 2
    assert data["sectors"][0]["code"] == "801010"
    assert data["sectors"][0]["name"] == "农林牧渔"
    assert data["inertia"][0]["streak"] == 2


def test_sector_stage_endpoint_empty(client, monkeypatch):
    import prism.market_data as md
    monkeypatch.setattr(md, "mkt_snapshot", lambda: {})
    r = client.get("/api/sector_stage")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["sectors"] == [] and data["inertia"] == []


def test_sector_stage_endpoint_error_failopen(client, monkeypatch):
    import prism.market_data as md
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(md, "mkt_snapshot", boom)
    r = client.get("/api/sector_stage")
    assert r.status_code == 200
    assert r.get_json()["ok"] is False
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q -k sector --import-mode=importlib`
Expected: FAIL（404）

- [x] **Step 3: 实现**

3a. app.py 顶部 import 区（`from prism.strategies import STRATEGIES_DIR` 之后）追加：

```python
from prism import market_data as _md
from prism import sector_stage as _ss
```

3b. 新路由（放在 `@app.route("/api/factors")` 之前）：

```python
@app.route("/api/sector_stage")
def api_sector_stage():
    """板块感知观察(孕育期/阶段定位/资金惯性), 只读 fail-open 不 500。"""
    try:
        snap = _md.mkt_snapshot()
        table = _ss.sector_table(snap)
        inertia = _ss.flow_inertia(snap.get("flow_rank"))
        last_date = ""
        for rec in (snap.get("sector") or {}).values():
            ds = (rec or {}).get("dates") or []
            if ds and str(ds[-1]) > last_date:
                last_date = str(ds[-1])
        flow_days = len(((snap.get("flow_rank") or {}).get("dates") or []))
        return jsonify({"ok": True, "date": last_date, "sectors": table,
                        "inertia": inertia, "flow_days": flow_days})
    except Exception as e:  # noqa: BLE001 - 观察面板 fail-open
        logger.warning("sector_stage 快照失败: %r", e)
        return jsonify({"ok": False, "date": "", "sectors": [],
                        "inertia": [], "flow_days": 0, "error": str(e)})
```

3c. index.html：nav 里「市场环境」按钮后加：

```html
    <button class="tab" data-tab="sector" onclick="switchTab('sector')">板块观察</button>
```

`tab-market` section 之后加：

```html
    <section id="tab-sector" class="tab-panel">
      <h2>板块观察 <span id="sector-date" class="hint"></span></h2>
      <p class="hint">孕育期=启动前夜(量价结构改善的观察信号, 非买入信号);
        阶段由板块量价+成交额占比推导。资金惯性为东财行业口径、自启用日起
        前向累积(<span id="sector-flow-days">0</span>天)。</p>
      <h3>资金惯性(每日净流入前3连续上榜)</h3>
      <table id="sector-inertia"><thead><tr>
        <th>板块</th><th>连续天数</th><th>今日净流入(亿)</th><th>标记</th>
      </tr></thead><tbody></tbody></table>
      <h3>阶段定位(申万一级行业)</h3>
      <table id="sector-stage"><thead><tr>
        <th>板块</th><th>阶段</th><th>依据</th><th>近5日涨幅%</th>
        <th>占比变化</th><th>孕育信号</th>
      </tr></thead><tbody></tbody></table>
    </section>
```

3d. app.js：`switchTab` 的 compare 行后加一行钩子：

```js
  if (name === "sector") loadSectorStage();
```

app.js 末尾追加：

```js
// ---------- 板块观察 ----------
const STAGE_BADGE = {
  "孕育期": "stage-gestation", "启动期": "stage-start",
  "主升期": "stage-main", "高潮期": "stage-peak",
  "退潮期": "stage-ebb", "休整": "", "数据不足": ""
};
const STAGE_ORDER = {"孕育期": 0, "启动期": 1, "主升期": 2, "高潮期": 3,
  "退潮期": 4, "休整": 5, "数据不足": 6};

async function loadSectorStage() {
  try {
    const data = await api("/api/sector_stage");
    document.getElementById("sector-date").textContent = data.date || "";
    document.getElementById("sector-flow-days").textContent = data.flow_days ?? 0;
    const tb1 = document.querySelector("#sector-inertia tbody");
    tb1.innerHTML = (data.inertia || []).map(r =>
      `<tr><td>${escHtml(r.name || r.code)}</td><td>${r.streak}</td>` +
      `<td>${r.last_net_in == null ? "-" : (r.last_net_in / 1e8).toFixed(2)}</td>` +
      `<td>${r.systematic ? '<span class="badge stage-main">系统性增配</span>' : ""}</td></tr>`
    ).join("") || `<tr><td colspan="4" class="hint">暂无数据(盘后跑 python -m prism.market_data --build-flow-rank 积累)</td></tr>`;
    const rows = (data.sectors || [])
      .slice()
      .sort((a, b) => (STAGE_ORDER[a.stage] ?? 9) - (STAGE_ORDER[b.stage] ?? 9));
    const tb2 = document.querySelector("#sector-stage tbody");
    tb2.innerHTML = rows.map(r =>
      `<tr><td>${escHtml(r.name || r.code)}</td>` +
      `<td><span class="badge ${STAGE_BADGE[r.stage] || ""}">${escHtml(r.stage)}</span></td>` +
      `<td class="hint">${escHtml(r.note || "")}</td>` +
      `<td>${r.r5 == null ? "-" : r.r5.toFixed(1)}</td>` +
      `<td>${r.share_chg == null ? "-" : r.share_chg.toFixed(4)}</td>` +
      `<td>${r.hits ?? 0}</td></tr>`
    ).join("");
  } catch (e) { /* fail-open: 面板留空 */ }
}
```

3e. style.css 末尾追加：

```css
/* 板块观察: 阶段徽标 */
.badge.stage-gestation { background: #1c4d8f; color: #cfe4ff; }
.badge.stage-start { background: #0e6b6b; color: #c9f7f7; }
.badge.stage-main { background: #8f1c1c; color: #ffd9d9; }
.badge.stage-peak { background: #b45309; color: #ffe9c7; }
.badge.stage-ebb { background: #444; color: #ccc; }
```

- [x] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q --import-mode=importlib`
Expected: 全 PASS

- [x] **Step 5: 提交**

```bash
git add prism_web/app.py prism_web/templates/index.html prism_web/static/app.js prism_web/static/style.css prism_web/tests/test_app.py
git commit -m "feat: 板块观察面板(孕育期/阶段定位/资金惯性, 只读 fail-open)"
```

---

### 收尾（主会话执行, 非子代理任务）

- [x] 全量回归: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt163` → 全绿
- [x] 实跑采集: `python -m prism.market_data --build-benchmark --build-flow-rank` → 落盘核对
- [x] 实跑冒烟: `python -c "from prism import market_data, sector_stage; snap=market_data.mkt_snapshot(); print(sector_stage.sector_table(snap)[:3]); print(sector_stage.flow_inertia(snap.get('flow_rank')))"` → 出数
- [x] GUI 生效说明: 5000 端口 Flask 需重启后浏览器验收（守护/网页由用户 PRISM.bat 管理, agent 不擅自重启）
- [x] 沉淀 skill: `.claude/skills/prism-upgrade-triage/SKILL.md`（对话→数据层评估→保留/降级/去掉→确认→实现工作流）
- [x] 台账 `.superpowers/sdd/progress.md` + MEMORY.md 更新
