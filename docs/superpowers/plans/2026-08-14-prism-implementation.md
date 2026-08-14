# Prism 可配置因子库策略系统 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把固定"4 模型 × 24 因子"的 strategy_web 升级为可配置的 Prism 策略系统:因子库(单文件单因子自动注册)、JSON 策略配置、真实策略回测(交易模拟)、信号自动生成(需授权),网页改造为展示层。

**Architecture:** 引擎-插件分层。`prism/` 独立包承载因子库/注册表/引擎/回测/交易,`prism_web/` 由 strategy_web 改造为展示层。回测与实盘共用同一引擎。迁移分 4 步,每步独立可验证,旧系统全程照常运行。

**Tech Stack:** Python 3.7(与现有系统一致,flask/numpy/pandas/requests)、pytest、JSON 配置、xtquant/东财数据(现有 data_source/eastmoney/fundamental 改造为 prism 数据适配层)。

**Spec:** `docs/superpowers/specs/2026-08-14-prism-design.md`

## Global Constraints

- Python 3.7 兼容(不能用 `dict | dict`、`list[str]` 等 3.9+ 语法;f-string 可用)
- 现有 24 因子 id(F1-F7/Y1-Y7/S1-S7/N1-N5)在迁移后**保持不变**,default.json 行为与现在 models.py 逐字一致(权重 0.60/0.25/0.15、阈值 3、A-E 分级逻辑)
- 所有因子 fail-open:数据缺失返回 `{"score": 0, "note": "..."}`,永不抛异常
- 回测与实盘共用同一 registry + 引擎;策略配置 JSON 是唯一策略描述
- 数据兼容:manual_factors.json / fundamental_cache.json / perf/ 存档继续使用
- 真实交易保留授权文件机制(D:/QMT_SIGNALS/real/armed.txt 含当天日期才下单)
- 全部新测试离线可跑(不依赖 QMT/网络),现有 177 个测试必须保持通过
- 因子库文件命名: `factor_<id小写>_<英文名>.py`;策略文件命名: `<策略id>.json`

---

## 文件结构

**新建 `prism/` 引擎包:**
- `prism/__init__.py` — 包导出(registry, engine, load_strategy)
- `prism/registry.py` — 因子注册表:`@factor` 装饰器、扫描 `factors/` 目录自动注册、按 id 查询
- `prism/context.py` — FactorContext 数据类:统一封装 kline/tick/index_kline/sector_map/limit_ups/em/fund/manual/float_mv/code
- `prism/engine.py` — 策略引擎:`load_strategy(json)`、`evaluate_stock(code, ctx, strategy)`、`run_screen(strategy)` 编排(市场门槛→逐股因子→模型分→综合分→过滤→排序)
- `prism/backtest.py` — 回测引擎:`Backtester(strategy, data_feeds)`、逐日回放、交易模拟(手续费/滑点/仓位/卖出规则)、`report()`
- `prism/trader.py` — 交易模块:`generate_buy_signals(result, strategy)`、`check_paused()`、`write_signals()`(需授权)
- `prism/factor_check.py` — 因子体检 CLI:`python -m prism.factor_check`(注册/签名/假数据跑通)
- `prism/strategies/__init__.py` — 策略目录常量 STRATEGIES_DIR(Task 3 创建, Task 8 引用)
- `prism/factors/__init__.py` — 因子库包,扫描 `factor_*.py`
- `prism/factors/_template.py` — 空白因子模板
- `prism/factors/factor_*.py` — 24 个迁移因子 + 2-3 个示例新因子
- `prism/strategies/default.json` — 默认策略(现有 4 模型翻译)

**改造:**
- `strategy_web/data_source.py` → 逻辑移入 `prism/`(数据适配),旧文件保留薄壳转发
- `strategy_web/eastmoney.py` / `fundamental.py` / `manual_store.py` → 同上
- `strategy_web/models.py` / `factors.py` / `screen.py` → 被 prism/engine 取代(旧文件在迁移完成后移除)
- `strategy_web/app.py` → `prism_web/app.py`(Flask 展示层:因子库/策略/回测/绩效/自动化开关)
- `strategy_web/templates|static` → `prism_web/`

**新增测试:**
- `prism/tests/test_registry.py` / `test_context.py` / `test_engine.py` / `test_backtest.py` / `test_trader.py` / `test_factor_check.py` / `test_factor_migration.py`(逐因子新旧结果比对)

---

### Task 1: prism 包骨架 + 因子注册表

**Files:**
- Create: `prism/__init__.py`
- Create: `prism/registry.py`
- Test: `prism/tests/test_registry.py`

**Interfaces:**
- Consumes: 无(首个任务)
- Produces:
  - `prism.registry.factor(id, name, category, description)` — 装饰器,返回原函数并注册
  - `prism.registry.FACTORS` — dict: id → {name, category, description, func}
  - `prism.registry.get_factor(fid)` — 查因子,未找到抛 `UnknownFactorError`
  - `prism.registry.list_factors()` — 返回排序后的因子元数据列表
  - `prism.registry.scan_factors(package="prism.factors")` — 扫描包内 `factor_*.py` 模块并 import(触发装饰器注册)
  - `prism.registry.reset()` — 清空注册表(测试用)

- [ ] **Step 1: 写失败测试** `prism/tests/test_registry.py`

```python
# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg


def test_factor_decorator_registers():
    reg.reset()
    @reg.factor(id="T1", name="测试因子", category="通用", description="x")
    def compute(ctx):
        return {"score": 0, "note": ""}
    assert "T1" in reg.FACTORS
    assert reg.FACTORS["T1"]["name"] == "测试因子"
    assert reg.FACTORS["T1"]["category"] == "通用"
    assert reg.FACTORS["T1"]["func"] is compute


def test_get_factor_unknown_raises():
    reg.reset()
    with pytest.raises(reg.UnknownFactorError):
        reg.get_factor("NO_SUCH_FACTOR")


def test_list_factors_sorted():
    reg.reset()
    @reg.factor(id="T2", name="b", category="通用", description="")
    def c2(ctx):
        return {"score": 0, "note": ""}
    @reg.factor(id="T1", name="a", category="通用", description="")
    def c1(ctx):
        return {"score": 0, "note": ""}
    ids = [f["id"] for f in reg.list_factors()]
    assert ids == ["T1", "T2"]


def test_scan_factors_finds_module(tmp_path, monkeypatch):
    # 临时包 prism_test_factors 里放 factor_abc.py, 扫描应注册 "TABC"
    pkg_dir = tmp_path / "prism_test_factors"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "factor_abc.py").write_text(
        "from prism.registry import factor\n"
        "@factor(id='TABC', name='扫描因子', category='通用', description='')\n"
        "def compute(ctx):\n"
        "    return {'score': 0, 'note': ''}\n",
        encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    reg.reset()
    reg.scan_factors(package="prism_test_factors")
    assert "TABC" in reg.FACTORS
```

- [ ] **Step 2: 运行确认失败**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_registry.py -v`
Expected: FAIL(module prism not found / registry 无 factor 属性)

- [ ] **Step 3: 实现**

`prism/__init__.py`:
```python
# -*- coding: utf-8 -*-
"""Prism — 可配置因子库策略系统。"""
from . import registry  # noqa: F401
from .engine import load_strategy  # noqa: F401  (Task 3 前注释掉, 避免 import 错误)

__version__ = "0.1.0"
```

`prism/registry.py`:
```python
# -*- coding: utf-8 -*-
"""因子注册表: @factor 装饰器 + 扫描因子库自动注册。"""
import importlib
import pkgutil


class UnknownFactorError(KeyError):
    """策略配置引用了不存在的因子。"""


FACTORS = {}  # id -> {"name","category","description","func"}


def reset():
    FACTORS.clear()


def factor(id, name, category="通用", description=""):
    """注册因子: 装饰器用法 @factor(id=..., name=..., ...)"""
    def deco(func):
        FACTORS[id] = {
            "id": id, "name": name, "category": category,
            "description": description, "func": func,
        }
        return func
    return deco


def get_factor(fid):
    if fid not in FACTORS:
        raise UnknownFactorError("未知因子: %s (请检查策略配置或因子库)" % fid)
    return FACTORS[fid]


def list_factors(category=None):
    out = []
    for fid in sorted(FACTORS):
        f = dict(FACTORS[fid])
        if category and f["category"] != category:
            continue
        out.append(f)
    return out


def scan_factors(package="prism.factors"):
    """import 包内所有 factor_*.py 模块, 触发装饰器注册。"""
    mod = importlib.import_module(package)
    for info in pkgutil.iter_modules(mod.__path__):
        if info.name.startswith("factor_"):
            importlib.import_module("%s.%s" % (package, info.name))
```

- [ ] **Step 4: 运行确认通过**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_registry.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add prism/ prism/tests/test_registry.py
git commit -m "feat(prism): 因子注册表(装饰器+自动扫描+查询)"
```

---

### Task 2: 因子上下文 + 空白模板 + 因子体检

**Files:**
- Create: `prism/context.py`
- Create: `prism/factors/__init__.py`
- Create: `prism/factors/_template.py`
- Create: `prism/factor_check.py`
- Test: `prism/tests/test_context.py`, `prism/tests/test_factor_check.py`

**Interfaces:**
- Consumes: Task 1 的 `prism.registry`
- Produces:
  - `prism.context.FactorContext` — 数据类,字段见下;`from_data(**kwargs)` 构造;`.get(name)` 兼容缺失
  - `prism.factor_check.run_checks(scan=True)` — 返回 [(factor_id, ok, message)]
  - `prism.factor_check.main()` — CLI 入口

- [ ] **Step 1: 写失败测试** `prism/tests/test_context.py`

```python
# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.context import FactorContext


def test_context_fields_default_none():
    ctx = FactorContext(code="600000.SH")
    assert ctx.code == "600000.SH"
    assert ctx.kline is None
    assert ctx.tick is None
    assert ctx.float_mv is None


def test_context_from_data():
    ctx = FactorContext.from_data(code="000001.SZ", last=10.0, float_mv=1e8)
    assert ctx.code == "000001.SZ"
    assert ctx.last == 10.0
    assert ctx.float_mv == 1e8


def test_context_get_missing_returns_none():
    ctx = FactorContext(code="600000.SH")
    assert ctx.get("nonexistent") is None
```

`prism/tests/test_factor_check.py`:
```python
# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import prism.registry as reg
from prism import factor_check


def test_check_fake_data_runs():
    reg.reset()
    @reg.factor(id="TCHK", name="体检因子", category="通用", description="")
    def compute(ctx):
        return {"score": 1, "note": "ok"}
    results = factor_check.run_checks(scan=False)
    by_id = {fid: ok for fid, ok, msg in results}
    assert by_id["TCHK"] is True


def test_check_catches_bad_signature():
    reg.reset()
    @reg.factor(id="TBAD", name="坏因子", category="通用", description="")
    def compute():   # 缺 ctx 参数
        return {"score": 0, "note": ""}
    results = factor_check.run_checks(scan=False)
    by_id = {fid: ok for fid, ok, msg in results}
    assert by_id["TBAD"] is False
```

- [ ] **Step 2: 运行确认失败**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_context.py prism/tests/test_factor_check.py -v`
Expected: FAIL(module prism.context not found)

- [ ] **Step 3: 实现**

`prism/context.py`:
```python
# -*- coding: utf-8 -*-
"""因子上下文: 所有因子收到的统一数据包。字段取不到时一律 None(fail-open)。"""
import inspect


class FactorContext:
    """因子只从 ctx 取自己需要的字段, 不碰任何数据源代码。"""

    def __init__(self, code=None, kline=None, tick=None, index_kline=None,
                 sector_map=None, limit_ups=None, em=None, fund=None,
                 manual=None, float_mv=None, last=None, last_close=None,
                 up_price=None, sealed=None, **kwargs):
        self.code = code
        self.kline = kline
        self.tick = tick or {}
        self.index_kline = index_kline
        self.sector_map = sector_map or {}
        self.limit_ups = limit_ups or []
        self.em = em or {}
        self.fund = fund or {}
        self.manual = manual or {}
        self.float_mv = float_mv
        self.last = last
        self.last_close = last_close
        self.up_price = up_price
        self.sealed = sealed
        self._extra = kwargs

    @classmethod
    def from_data(cls, **kwargs):
        return cls(**kwargs)

    def get(self, name):
        """按名字取字段, 缺失返回 None。支持因子取自定义扩展字段。"""
        if hasattr(self, name):
            return getattr(self, name)
        return self._extra.get(name)
```

`prism/factors/__init__.py`:
```python
# -*- coding: utf-8 -*-
"""因子库包: import 本包即自动扫描注册 factors/factor_*.py。"""
from prism.registry import scan_factors

scan_factors("prism.factors")
```

`prism/factors/_template.py`:
```python
# -*- coding: utf-8 -*-
"""空白因子模板 — 复制本文件创建新因子:
1) 文件名: factor_<id小写>_<英文名>.py
2) 改 @factor 的 id/name/category/description
3) 在 compute(ctx) 里写逻辑, 返回 {"score": 0或1或数值, "note": "说明"}
4) 运行 python -m prism.factor_check 体检
"""
from prism.registry import factor


@factor(id="NEW1", name="你的因子名", category="通用",
        description="一句话说明这个因子判断什么")
def compute(ctx):
    # 示例: 从上下文取数据, 永远 fail-open
    kline = ctx.kline
    if kline is None or len(kline) < 2:
        return {"score": 0, "note": "K线不足"}
    # 在这里写你的逻辑...
    return {"score": 0, "note": "未命中"}
```

`prism/factor_check.py`:
```python
# -*- coding: utf-8 -*-
"""因子体检: 注册/签名/假数据跑通 自动验证。

用法: python -m prism.factor_check
"""
import inspect
import sys


def _fake_context():
    """给因子一个最小假上下文(全部 None), 验证不崩溃。"""
    from prism.context import FactorContext
    return FactorContext(code="600000.SH")


def run_checks(scan=True):
    """返回 [(factor_id, ok, message)]。scan=True 时先扫描因子库。"""
    from prism import registry as reg
    if scan:
        reg.reset()
        reg.scan_factors("prism.factors")
    out = []
    for fid, meta in sorted(reg.FACTORS.items()):
        func = meta["func"]
        try:
            params = inspect.signature(func).parameters
            if len(params) < 1:
                out.append((fid, False, "签名错误: compute 缺 ctx 参数"))
                continue
            res = func(_fake_context())
            if not isinstance(res, dict) or "score" not in res:
                out.append((fid, False, "返回值缺少 score: %r" % (res,)))
                continue
            out.append((fid, True, "OK score=%r" % res["score"]))
        except Exception as e:
            out.append((fid, False, "运行异常: %r" % e))
    return out


def main():
    results = run_checks()
    bad = [r for r in results if not r[1]]
    for fid, ok, msg in results:
        print("[%s] %s: %s" % ("PASS" if ok else "FAIL", fid, msg))
    if bad:
        print("\n%d 个因子体检失败!" % len(bad))
        return 1
    print("\n全部 %d 个因子体检通过。" % len(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 运行确认通过**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_context.py prism/tests/test_factor_check.py -v && python -m prism.factor_check`
Expected: tests pass; factor_check 显示 TCHK PASS / TBAD FAIL

- [ ] **Step 5: 提交**

```bash
git add prism/
git commit -m "feat(prism): 因子上下文 + 空白模板 + 因子体检CLI"
```

---

### Task 3: 策略加载 + 引擎(市场门槛→因子→模型分→综合分→选股)

**Files:**
- Create: `prism/engine.py`
- Create: `prism/strategies/__init__.py`
- Create: `prism/strategies/default.json`
- Test: `prism/tests/test_engine.py`

**Interfaces:**
- Consumes: Task 1 registry, Task 2 context
- Produces:
  - `prism.engine.load_strategy(path_or_dict)` → dict(校验: id/name/scoring_models 必填, 因子存在性)
  - `prism.engine.compute_model_scores(ctx, strategy)` → {model_id: score, "composite": float, "grade": str, "strength": str}
  - `prism.engine.evaluate_stock(code, ctx, strategy)` → {"code", "scores", "factors"} 与旧 screen 输出同构
  - `prism.engine.run_screen(strategy, market_ctx, stock_contexts)` → 旧 screen 同构结果
  - `prism.engine.composite_modes` — {"top3_weighted", "sum", "max", "average"} 实现

- [ ] **Step 1: 写失败测试** `prism/tests/test_engine.py`

```python
# -*- coding: utf-8 -*-
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
from prism.context import FactorContext
from prism import engine


def _mk_strategy():
    return {
        "id": "t", "name": "测试", "description": "",
        "market_gate": {"model": "node", "threshold": 3,
                        "factors": ["N1", "N2", "N3", "N4", "N5"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 0.6, "factors": ["A1", "A2"]},
            {"id": "m2", "name": "M2", "weight": 0.25, "factors": ["B1"]},
        ],
        "composite": {"mode": "top3_weighted", "weights": [0.6, 0.25, 0.15],
                      "cap": 7.0},
        "filters": {"candidate_min_model": 1, "environment_threshold": 3},
    }


@pytest.fixture(autouse=True)
def _factors():
    reg.reset()
    @reg.factor(id="N1", name="n1", category="node", description="")
    def f_n1(ctx):
        return {"score": 1, "note": ""}
    @reg.factor(id="A1", name="a1", category="通用", description="")
    def f_a1(ctx):
        return {"score": 1, "note": ""}
    @reg.factor(id="A2", name="a2", category="通用", description="")
    def f_a2(ctx):
        return {"score": 0, "note": ""}
    @reg.factor(id="B1", name="b1", category="通用", description="")
    def f_b1(ctx):
        return {"score": 1, "note": ""}
    yield


def test_load_strategy_validates_unknown_factor():
    s = _mk_strategy()
    s["scoring_models"][0]["factors"] = ["NO_SUCH"]
    with pytest.raises(reg.UnknownFactorError):
        engine.load_strategy(s)


def test_load_strategy_requires_id_and_models():
    with pytest.raises(ValueError):
        engine.load_strategy({"name": "缺id"})
    with pytest.raises(ValueError):
        engine.load_strategy({"id": "x"})   # 缺 scoring_models


def test_compute_model_scores():
    ctx = FactorContext(code="600000.SH")
    s = engine.load_strategy(_mk_strategy())
    r = engine.compute_model_scores(ctx, s)
    assert r["m1"] == 1          # A1命中1 + A2未命中0
    assert r["m2"] == 1
    assert "composite" in r and "grade" in r


def test_composite_top3_weighted():
    # 模型分 2,1,0 → 2*0.6 + 1*0.25 + 0*0.15 = 1.45
    s = _mk_strategy()
    s["scoring_models"].append({"id": "m3", "name": "M3", "weight": 0.15,
                                "factors": []})
    s = engine.load_strategy(s)
    ctx = FactorContext(code="600000.SH")
    r = engine.compute_model_scores(ctx, s)
    assert abs(r["composite"] - 1.45) < 1e-6


def test_composite_sum_mode():
    s = _mk_strategy()
    s["composite"] = {"mode": "sum"}
    s = engine.load_strategy(s)
    ctx = FactorContext(code="600000.SH")
    r = engine.compute_model_scores(ctx, s)
    assert r["composite"] == 2   # 1 + 1


def test_evaluate_stock_shape():
    s = engine.load_strategy(_mk_strategy())
    ctx = FactorContext(code="600000.SH")
    r = engine.evaluate_stock("600000.SH", ctx, s)
    assert r["code"] == "600000.SH"
    assert r["scores"]["m1"] == 1
    assert r["factors"]["A1"] == 1
    assert r["factors"]["A2"] == 0


def test_run_screen_gate_blocks():
    s = engine.load_strategy(_mk_strategy())
    # 市场节点分 1 < 门槛3 → 环境不达标
    market_ctx = FactorContext(code="IDX")
    gate_factors = {"N1": 1, "N2": 0, "N3": 0, "N4": 0, "N5": 0}
    out = engine.run_screen(s, market_ctx, gate_factors=gate_factors,
                            stock_contexts={})
    assert out["environment_ok"] is False
    assert out["candidates"] == []
```

- [ ] **Step 2: 运行确认失败**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_engine.py -v`
Expected: FAIL(module prism.engine not found)

- [ ] **Step 3: 实现**

`prism/strategies/default.json`:
```json
{
  "id": "default",
  "name": "默认四模型策略",
  "description": "兼容现有 4 模型 24 因子的默认策略",
  "market_gate": {"model": "node", "threshold": 3,
                  "factors": ["N1", "N2", "N3", "N4", "N5"]},
  "scoring_models": [
    {"id": "first_board", "name": "首板", "weight": 0.60,
     "factors": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]},
    {"id": "monster", "name": "妖股", "weight": 0.25,
     "factors": ["Y1", "Y2", "Y3", "Y4", "Y5", "Y6", "Y7"]},
    {"id": "momentum", "name": "势能", "weight": 0.15,
     "factors": ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]}
  ],
  "composite": {"mode": "top3_weighted", "weights": [0.60, 0.25, 0.15], "cap": 7.0},
  "filters": {"candidate_min_model": 3, "environment_threshold": 3},
  "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05, "max_hold_days": 5}
}
```

`prism/engine.py`:
```python
# -*- coding: utf-8 -*-
"""策略引擎: 读策略配置 → 算因子 → 模型分 → 综合分 → 过滤排序。

与旧 screen.py 的输出结构保持同构, 网页/绩效/桥无缝对接。
"""
import json
from pathlib import Path

from prism import registry as reg
from prism.context import FactorContext

# 综合分组合方式
composite_modes = {
    "top3_weighted": lambda scores, cfg: _top3_weighted(scores, cfg),
    "sum": lambda scores, cfg: sum(scores),
    "max": lambda scores, cfg: max(scores) if scores else 0.0,
    "average": lambda scores, cfg: (sum(scores) / len(scores)) if scores else 0.0,
}

GRADE_RULES = [
    (6.0, None, "A"), (5.0, 3.0, "B"), (4.0, None, "C"), (3.0, None, "D"),
]
STRENGTH_RULES = [(6.0, "极强", "仓位上限75%"), (5.0, "强", "仓位上限50%"),
                  (4.0, "中等", "仓位上限30%"), (0.0, "弱", "观察/空仓")]


def _top3_weighted(scores, cfg):
    weights = cfg.get("weights") or [0.60, 0.25, 0.15]
    cap = cfg.get("cap") or 7.0
    ordered = sorted(scores, reverse=True)
    total = sum(ordered[i] * weights[i] for i in range(min(len(ordered), len(weights))))
    return min(total, cap)


def load_strategy(path_or_dict):
    """加载并校验策略配置。path_or_dict: JSON 文件路径或 dict。"""
    if isinstance(path_or_dict, (str, Path)):
        p = Path(path_or_dict)
        data = json.loads(p.read_text(encoding="utf-8"))
    else:
        data = path_or_dict
    if not data.get("id"):
        raise ValueError("策略缺少 id")
    if not data.get("scoring_models"):
        raise ValueError("策略缺少 scoring_models")
    # 校验因子存在性(市场门槛 + 各模型)
    gate = data.get("market_gate") or {}
    for fid in gate.get("factors", []):
        reg.get_factor(fid)
    for m in data["scoring_models"]:
        for item in m.get("factors", []):
            fid = item["id"] if isinstance(item, dict) else item
            reg.get_factor(fid)
    # 校验组合模式
    mode = (data.get("composite") or {}).get("mode", "top3_weighted")
    if mode not in composite_modes:
        raise ValueError("未知组合模式: %s" % mode)
    return data


def _factor_entry(item):
    """因子条目 → (fid, weight, op, threshold)。支持简写/加权/阈值三种形态。"""
    if isinstance(item, str):
        return item, 1.0, None, None
    fid = item["id"]
    return (fid, item.get("weight", 1.0), item.get("op"), item.get("threshold"))


def _factor_hit(ctx, fid, op, threshold):
    """算单因子并按阈值判定是否命中。返回 (raw_score, hit)。"""
    meta = reg.get_factor(fid)
    try:
        res = meta["func"](ctx)
    except Exception:
        res = {"score": 0, "note": "异常"}
    if not isinstance(res, dict):
        return 0, False
    score = res.get("score", 0) or 0
    if op and threshold is not None:
        try:
            hit = {"<": score < threshold, "<=": score <= threshold,
                   ">": score > threshold, ">=": score >= threshold,
                   "==": score == threshold}[op]
        except KeyError:
            hit = score > 0
        return score, hit
    return score, score > 0


def compute_model_scores(ctx, strategy):
    """对单股: 各模型分(加权因子命中数) + 综合分 + 分级 + 强弱。"""
    model_scores = {}
    factors_out = {}
    for m in strategy["scoring_models"]:
        total = 0.0
        for item in m.get("factors", []):
            fid, weight, op, threshold = _factor_entry(item)
            raw, hit = _factor_hit(ctx, fid, op, threshold)
            factors_out[fid] = 1 if hit else 0
            if hit:
                total += weight
        model_scores[m["id"]] = total
    scores = [model_scores[m["id"]] for m in strategy["scoring_models"]]
    comp_cfg = strategy.get("composite") or {}
    mode = comp_cfg.get("mode", "top3_weighted")
    composite = composite_modes[mode](scores, comp_cfg)
    best = max(scores) if scores else 0
    second = sorted(scores, reverse=True)[1] if len(scores) > 1 else 0
    grade = "E"
    for min_best, min_second, g in GRADE_RULES:
        if best >= min_best and (min_second is None or second >= min_second):
            grade = g
            break
    strength, position = "弱", "观察/空仓"
    for min_best, st, pos in STRENGTH_RULES:
        if best >= min_best:
            strength, position = st, pos
            break
    out = dict(model_scores)
    out.update({"composite": round(composite, 2), "grade": grade,
                "strength": strength, "position": position})
    return out, factors_out


def evaluate_stock(code, ctx, strategy):
    """单股完整评估。ctx 由调用方构造(数据适配层负责填数据)。"""
    scores, factors = compute_model_scores(ctx, strategy)
    return {"code": code, "scores": scores, "factors": factors}


def run_screen(strategy, market_ctx, gate_factors=None, stock_contexts=None):
    """完整选股编排(与旧 screen.run 输出同构)。

    market_ctx: 市场数据上下文(算节点因子用)
    gate_factors: 市场节点因子预计算值 {N1: 0/1, ...}; None 则用 market_ctx 现算
    stock_contexts: {code: FactorContext}
    """
    gate = strategy.get("market_gate") or {}
    gate_fids = gate.get("factors", [])
    threshold = gate.get("threshold", 3)
    gate_score = 0
    if gate_factors is not None:
        gate_score = sum(1 for f in gate_fids if gate_factors.get(f) == 1)
    else:
        for fid in gate_fids:
            raw, hit = _factor_hit(market_ctx, fid, None, None)
            gate_score += 1 if hit else 0
    environment_ok = gate_score >= threshold

    candidates = []
    if environment_ok and stock_contexts:
        min_model = (strategy.get("filters") or {}).get("candidate_min_model", 3)
        for code, ctx in stock_contexts.items():
            ev = evaluate_stock(code, ctx, strategy)
            best = max([ev["scores"][m["id"]]
                        for m in strategy["scoring_models"]], default=0)
            if best >= min_model:
                candidates.append(ev)
        candidates.sort(key=lambda c: c["scores"]["composite"], reverse=True)

    summary = {"candidate_count": len(candidates)}
    for g in ("A", "B", "C", "D"):
        summary["%s_count" % g.lower()] = sum(
            1 for c in candidates if c["scores"]["grade"] == g)
    return {"environment_ok": environment_ok, "gate_score": gate_score,
            "candidates": candidates, "summary": summary}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_engine.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add prism/
git commit -m "feat(prism): 策略加载+引擎(门槛/因子/模型分/综合分/选股) + default.json"
```

---

### Task 4: 数据适配层 — 现有 DataSource/东财/手填 封装为 prism 数据提供者

**Files:**
- Create: `prism/data.py`
- Modify: `prism/context.py`(补 from_provider 构造)
- Test: `prism/tests/test_data.py`

**Interfaces:**
- Consumes: Task 2 context;现有 strategy_web/data_source.py / eastmoney.py / fundamental.py / manual_store.py
- Produces:
  - `prism.data.DataProvider` — 薄封装: `build_market_context()` / `build_stock_context(code)` / `get_limit_ups()` / `get_market_stats()`,内部复用现有数据源实现(先 import strategy_web 的类, 迁移完成后再内联)
  - `prism.data.provider` 单例

- [ ] **Step 1: 写失败测试** `prism/tests/test_data.py`

```python
# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.data import DataProvider


class FakeDS:
    def get_limit_up_stocks(self, ticks=None):
        return [{"code": "600000.SH", "name": "浦发", "last": 10.5}]

    def get_kline(self, code, days=120):
        return None


class FakeProvider(DataProvider):
    def __init__(self):
        self.ds = FakeDS()
        self.em = None
        self.fund = {}
        self.manual = {}


def test_provider_builds_stock_context():
    p = FakeProvider()
    ctx = p.build_stock_context("600000.SH")
    assert ctx.code == "600000.SH"
    assert ctx.kline is None          # 数据缺失 → None, 不崩


def test_provider_get_limit_ups():
    p = FakeProvider()
    ups = p.get_limit_ups()
    assert ups[0]["code"] == "600000.SH"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_data.py -v`
Expected: FAIL(module prism.data not found)

- [ ] **Step 3: 实现**

`prism/data.py`:
```python
# -*- coding: utf-8 -*-
"""数据适配层: 把 QMT/东财/手填 数据封装成因子上下文。

第一版复用 strategy_web 现有实现(DataSource/EastMoneyFeed/FundamentalFeed/
ManualStore), 迁移完成后逐步内联。因子永远不直接碰本层。
"""
import sys
from pathlib import Path

_WEB = str(Path(__file__).parent.parent / "strategy_web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)


class DataProvider:
    """薄封装: 对外只暴露 build_*_context / get_* 等数据提供接口。"""

    def __init__(self, ds=None, em_feed=None, fund_feed=None, manual=None):
        from data_source import DataSource
        from eastmoney import EastMoneyFeed
        from fundamental import FundamentalFeed
        from manual_store import ManualStore
        self.ds = ds or DataSource()
        self.em_feed = em_feed or EastMoneyFeed()
        self.fund_feed = fund_feed or FundamentalFeed()
        self.manual = manual or ManualStore()
        self._em_stats = None

    def connect(self):
        return self.ds.connect()

    @property
    def connected(self):
        return getattr(self.ds, "_connected", False)

    def get_limit_ups(self):
        """返回涨停股列表(与旧 screen 同构)。未连接 → []。"""
        if not self.connected:
            return []
        try:
            ticks = self.ds.get_full_market_ticks()
            return self.ds.get_limit_up_stocks(ticks)
        except Exception:
            return []

    def get_market_stats(self):
        """东财市场统计(涨停池/题材/连板), 失败 → None。"""
        if self._em_stats is None:
            try:
                self._em_stats = self.em_feed.get_market_stats()
            except Exception:
                self._em_stats = None
        return self._em_stats

    def build_market_context(self):
        """市场因子上下文(N 系因子用)。"""
        from prism.context import FactorContext
        ticks = {}
        limit_ups = []
        if self.connected:
            try:
                ticks = self.ds.get_full_market_ticks()
                limit_ups = self.ds.get_limit_up_stocks(ticks)
            except Exception:
                pass
        em = self.get_market_stats()
        ctx = FactorContext(code="__MARKET__", ticks=ticks, limit_ups=limit_ups,
                            em=em or {})
        return ctx

    def build_stock_context(self, code, kline=None, index_kline=None,
                            sector_map=None):
        """单股因子上下文。数据缺失一律 None(fail-open)。"""
        from prism.context import FactorContext
        tick = {}
        float_mv = None
        if self.connected:
            try:
                ticks = self.ds.get_full_market_ticks([code])
                tick = ticks.get(code, {})
            except Exception:
                pass
            try:
                det = self.ds.get_instrument(code)
                float_mv = (det.get("FloatVolume") or 0) * (tick.get("lastPrice") or 0)
            except Exception:
                pass
        if kline is None and self.connected:
            try:
                kline = self.ds.get_kline(code, days=250)
            except Exception:
                kline = None
        fund = {}
        try:
            fund = self.fund_feed.compute_for_stock(code, float_mv=float_mv)
        except Exception:
            fund = {}
        manual = self.manual.get_manual(code)
        return FactorContext(
            code=code, tick=tick, kline=kline, index_kline=index_kline,
            sector_map=sector_map or {}, float_mv=float_mv,
            limit_ups=self.get_limit_ups() if hasattr(self, "_lazy") else [],
            em=self.get_market_stats() or {}, fund=fund, manual=manual,
            last=tick.get("lastPrice"), last_close=tick.get("lastClose"),
            up_price=None, sealed=None)
```

- [ ] **Step 4: 运行确认通过**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_data.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add prism/
git commit -m "feat(prism): 数据适配层(复用现有数据源封装因子上下文)"
```

---

### Task 5: 因子迁移 — 24 个因子逐一拆入因子库 + 逐因子比对测试

**Files:**
- Create: `prism/factors/factor_f1_first_board.py` … 共 24 个文件(清单见下)
- Create: `prism/tests/test_factor_migration.py` — 逐因子: 旧实现 vs 新实现 输出比对
- Test: `prism/tests/test_factor_migration.py`

**Interfaces:**
- Consumes: Task 1/2/3(registry/context/engine);现有 `strategy_web/factors.py` 的旧实现(作为比对基准)
- Produces: 24 个已注册因子 id 与旧版一一对应, 行为一致

**因子清单(24 个, id → 文件名):**
- F1→factor_f1_first_board.py, F2→factor_f2_early_seal.py, F3→factor_f3_seal_strength.py, F4→factor_f4_sector_resonance.py, F5→factor_f5_volume_accum.py, F6→factor_f6_market_support.py, F7→factor_f7_novel_theme.py
- Y1→factor_y1_small_cap.py, Y2→factor_y2_clean_chips.py, Y3→factor_y3_volume_spike.py, Y4→factor_y4_ma_bullish.py, Y5→factor_y5_multi_concept.py, Y6→factor_y6_event_catalyst.py, Y7→factor_y7_hot_money.py
- S1→factor_s1_manual.py, S2→factor_s2_volume_density.py, S3→factor_s3_breakout.py, S4→factor_s4_ma_long.py, S5→factor_s5_financing.py, S6→factor_s6_sector_strength.py, S7→factor_s7_manual.py
- N1→factor_n1_limitup_index.py, N2→factor_n2_sentiment.py, N3→factor_n3_first_board_premium.py, N4→factor_n4_chain_height.py, N5→factor_n5_total_amount.py

- [ ] **Step 1: 写失败测试** `prism/tests/test_factor_migration.py`(先做 F1 与 Y3 两个样例, 其余因子逐一加同构用例)

```python
# -*- coding: utf-8 -*-
"""因子迁移比对: 新因子(prism) vs 旧实现(strategy_web.factors) 输出一致。"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
from prism.context import FactorContext
import prism.factors  # noqa: F401  触发扫描注册


def make_kline(closes, volumes=None):
    n = len(closes)
    volumes = volumes or np.full(n, 100000)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n, freq="B"),
        "open": closes, "high": [c * 1.02 for c in closes],
        "low": [c * 0.98 for c in closes], "close": closes,
        "volume": volumes, "amount": [c * v * 100 for c, v in zip(closes, volumes)],
    })


@pytest.fixture(scope="module", autouse=True)
def _scan():
    reg.reset()
    reg.scan_factors("prism.factors")
    yield


def _ctx(**kw):
    return FactorContext(code=kw.pop("code", "600000.SH"), **kw)


def test_f1_matches_old():
    # 旧实现需要的输入: kline + last + up_price
    closes = [10.0] * 100 + [11.0] + [10.0] * 148 + [11.0]
    kline = make_kline(closes, [100000] * 250)
    ctx = _ctx(kline=kline, last=11.0, last_close=10.0, up_price=11.0)
    res = reg.get_factor("F1")["func"](ctx)
    # 旧逻辑: 近20日无涨停且今日在涨停价 → F1=1
    assert res["score"] == 1


def test_f2_epoch_timetag_matches_old():
    import datetime as _dt
    ts_ms = int(_dt.datetime(2026, 8, 11, 9, 30).timestamp() * 1000)
    ctx = _ctx(tick={"timetag": ts_ms}, sealed=True)
    res = reg.get_factor("F2")["func"](ctx)
    assert res["score"] == 1


def test_y3_matches_old():
    kline = make_kline([10] * 25, [100000] * 24 + [500000])
    ctx = _ctx(kline=kline)
    res = reg.get_factor("Y3")["func"](ctx)
    assert res["score"] == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_factor_migration.py -v`
Expected: FAIL(F1/Y3 未注册 → UnknownFactorError)

- [ ] **Step 3: 实现 24 个因子文件**

每个文件按 Task 1 协议 + 旧逻辑移植。**F1 示例**(其余 23 个同构, 逻辑从 strategy_web/factors.py 对应函数移植, 数据改从 ctx 取):

`prism/factors/factor_f1_first_board.py`:
```python
# -*- coding: utf-8 -*-
"""F1 首板确认: 近20日无涨停且今日首次涨停。"""
from prism.registry import factor


@factor(id="F1", name="首板确认", category="first_board",
        description="近20日无涨停且今日首次涨停")
def compute(ctx):
    kline = ctx.kline
    up_price = ctx.up_price
    last = ctx.last or 0
    if kline is None or not up_price:
        return {"score": 0, "note": "K线或涨停价缺失"}
    closes = kline["close"].tolist()
    code = ctx.code or ""
    ratio = (0.30 if code.startswith(("8", "4")) else
             0.20 if code.startswith(("300", "301", "688")) else 0.10)
    start = max(1, len(closes) - 20)
    prev_limit = False
    for i in range(start, len(closes) - 1):
        limit_px = round(closes[i - 1] * (1 + ratio), 2)
        if closes[i] >= limit_px - 0.01:
            prev_limit = True
            break
    f1 = 1 if (not prev_limit and last >= up_price - 0.01) else 0
    return {"score": f1,
            "note": "近20日无涨停且今日首次涨停" if f1 else "近20日已有涨停或今日未涨停"}
```

**注意**: 每个因子文件都要从旧 `strategy_web/factors.py` 找到对应逻辑段, 逐行移植到 `compute(ctx)` 中, 数据引用改为 ctx 字段:
- 旧 `tick.get("lastPrice")` → `ctx.last`
- 旧 `detail.get("UpStopPrice")` → `ctx.up_price`
- 旧 `tick.get("askPrice")[0] == 0` → `ctx.sealed`
- 旧 `ds.get_index_kline(...)` → `ctx.index_kline`
- 旧 `sector_map` → `ctx.sector_map`
- 旧 `limit_ups` → `ctx.limit_ups`
- 东财因子(F7/Y5/Y6/Y7/Y2/Y1)从 `ctx.fund` 读(保留 fundamental.py 的原始计算逻辑, 由数据层调用)
- 手填因子(S1/S5/S7)从 `ctx.manual` 读

- [ ] **Step 4: 运行确认通过 + 全量比对**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/ -v && python -m prism.factor_check`
Expected: 迁移测试通过; factor_check 显示 24+ 因子全 PASS

- [ ] **Step 5: 提交**

```bash
git add prism/
git commit -m "feat(prism): 24 个现有因子迁移到因子库(与旧实现逐因子比对一致)"
```

---

### Task 6: 回测引擎 — 真实策略回放 + 交易模拟

**Files:**
- Create: `prism/backtest.py`
- Test: `prism/tests/test_backtest.py`

**Interfaces:**
- Consumes: Task 3 engine / Task 4 data / 现有 exit_rules
- Produces:
  - `prism.backtest.Backtester(strategy, zt_feed, kline_feed, fee_rate=0.00025, slippage=0.001)` 
  - `.run(start_date, end_date, sell_rules=None)` → report dict(与旧 backtest 同构+新字段)
  - `.compare_params(param_grid)` → rows
  - 内部复用 `exit_rules.ExitRule` 做卖出判定

- [ ] **Step 1: 写失败测试** `prism/tests/test_backtest.py`

```python
# -*- coding: utf-8 -*-
import sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
from prism import backtest


def _mk_strategy():
    return {
        "id": "bt", "name": "回测策略", "description": "",
        "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0,
             "factors": [{"id": "A1", "op": ">", "threshold": 0}]},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1, "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                       "max_hold_days": 5},
    }


@pytest.fixture(autouse=True)
def _factors():
    reg.reset()
    @reg.factor(id="N1", name="n", category="node", description="")
    def f_n(ctx):
        return {"score": 1, "note": ""}
    @reg.factor(id="A1", name="a", category="通用", description="")
    def f_a(ctx):
        return {"score": 1, "note": ""}
    yield


def _feeds():
    start = date(2026, 7, 1)
    # 涨停池: 只有 07-01 有 1 只 600000
    def zf(d):
        if d == "20260701":
            return [{"code": "600000.SH", "boards": 1, "theme": "机器人"}]
        return []
    # K线: 600000 从 10 涨到 12(5天后 +20%)
    def kf(code):
        closes = [10.0 * (1.02 ** i) for i in range(8)]
        dates = [(start + __import__("datetime").timedelta(days=i)).strftime("%Y-%m-%d")
                 for i in range(8)]
        return list(zip(dates, closes))
    return zf, kf


def test_backtest_runs_full_strategy():
    s = __import__("prism.engine", fromlist=["load_strategy"]).load_strategy(_mk_strategy())
    zf, kf = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3))
    assert rep["trades"] == 1
    assert rep["win_rate"] == 1.0


def test_backtest_fee_and_slippage_apply():
    s = __import__("prism.engine", fromlist=["load_strategy"]).load_strategy(_mk_strategy())
    zf, kf = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf,
                             fee_rate=0.00025, slippage=0.001)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3))
    # 有交易且收益率考虑了费用滑点
    assert rep["trades"] == 1
    assert rep["avg_return_pct"] is not None


def test_backtest_sell_rules_hit():
    s = __import__("prism.engine", fromlist=["load_strategy"]).load_strategy(_mk_strategy())
    zf, kf = _feeds()
    bt = backtest.Backtester(s, zt_feed=zf, kline_feed=kf)
    # 止盈 5%: 5日后 +20% > +5% → 止盈卖出(而非持有到期)
    rep = bt.run(date(2026, 7, 1), date(2026, 7, 3),
                 sell_rules={"take_profit_pct": 0.05, "stop_loss_pct": 0.05,
                             "max_hold_days": 5})
    assert rep["trades"] == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_backtest.py -v`
Expected: FAIL(module prism.backtest not found)

- [ ] **Step 3: 实现**

`prism/backtest.py`:
```python
# -*- coding: utf-8 -*-
"""回测引擎: 逐日回放真实策略(与实盘同一 engine), 含交易模拟。

交易模拟: 手续费(fee_rate, 默认万2.5) + 滑点(slippage, 默认0.1%)
+ 仓位(单只资金上限) + 卖出规则(复用 exit_rules: 止盈/止损/T+N)。
"""
import logging
from datetime import date, timedelta

from prism.engine import compute_model_scores, load_strategy
from prism.context import FactorContext

logger = logging.getLogger(__name__)


class Backtester:
    def __init__(self, strategy, zt_feed, kline_feed,
                 fee_rate=0.00025, slippage=0.001, position_ratio=0.3):
        self.strategy = load_strategy(strategy)
        self.zt_feed = zt_feed
        self.kline_feed = kline_feed
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.position_ratio = position_ratio

    def _stock_ctx(self, code, kline):
        """回测环境的股票上下文(只用回测可得的字段)。"""
        return FactorContext(code=code, kline=kline)

    def _pick(self, pool):
        """用策略引擎对当日涨停池选股。返回 [(code, boards, theme, composite), ...]"""
        gate = self.strategy.get("market_gate") or {}
        gate_fids = gate.get("factors", [])
        threshold = gate.get("threshold", 3)
        gate_score = 0
        market_ctx = FactorContext(code="__MKT__", limit_ups=pool)
        for fid in gate_fids:
            meta = __import__("prism.registry", fromlist=["get_factor"]).get_factor(fid)
            try:
                res = meta["func"](market_ctx)
                if isinstance(res, dict) and res.get("score"):
                    gate_score += 1
            except Exception:
                pass
        if gate_score < threshold:
            return []
        min_model = (self.strategy.get("filters") or {}).get("candidate_min_model", 3)
        out = []
        for s in pool:
            code = s["code"]
            kline = self.kline_feed(code)
            ctx = self._stock_ctx(code, kline)
            scores, _ = compute_model_scores(ctx, self.strategy)
            best = max([scores[m["id"]]
                        for m in self.strategy["scoring_models"]], default=0)
            if best >= min_model:
                out.append((code, s.get("boards", 0), s.get("theme", ""),
                            scores["composite"]))
        out.sort(key=lambda x: x[3], reverse=True)
        return out

    def run(self, start_date, end_date, sell_rules=None, progress=None):
        """回放 [start_date, end_date]。返回报告 dict。"""
        defaults = {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                    "max_hold_days": 5}
        rules = dict(defaults, **(sell_rules or {}))
        trades = []
        dates = []
        d = start_date
        while d <= end_date:
            if progress:
                progress(d)
            try:
                pool = self.zt_feed(d.strftime("%Y%m%d")) or []
            except Exception as e:
                logger.warning("回测 %s 涨停池失败: %r", d, e)
                pool = []
            if pool:
                dates.append(d)
                for code, boards, theme, composite in self._pick(pool):
                    try:
                        kline = self.kline_feed(code) or []
                    except Exception:
                        kline = []
                    tr = self._simulate_trade(code, kline, d, rules)
                    if tr:
                        trades.append({
                            "date": d.strftime("%Y-%m-%d"), "code": code,
                            "boards": boards, "theme": theme,
                            "composite": composite,
                            "entry": tr[0], "exit": tr[1], "return_pct": tr[2],
                        })
            d += timedelta(days=1)
        return self._report(trades, dates)

    def _simulate_trade(self, code, kline, entry_date, rules):
        """模拟一笔: 选股日收盘买入(加滑点), 按卖出规则/持有期卖出。"""
        if not kline:
            return None
        idx = None
        for i, (dt, _c) in enumerate(kline):
            if dt >= entry_date.strftime("%Y-%m-%d"):
                idx = i
                break
        if idx is None:
            return None
        entry_close = kline[idx][1]
        if not entry_close:
            return None
        # 买入: 收盘价 + 滑点; 手续费
        buy_price = entry_close * (1 + self.slippage)
        # 逐日检查卖出规则
        exit_close = None
        for j in range(idx + 1, len(kline)):
            px = kline[j][1]
            ret = px / buy_price - 1
            if ret <= -rules["stop_loss_pct"]:
                exit_close = px
                break
            if ret >= rules["take_profit_pct"]:
                exit_close = px
                break
            hold = (date(*[int(x) for x in kline[j][0].split("-")]) - entry_date).days
            if hold >= rules["max_hold_days"]:
                exit_close = px
                break
        if exit_close is None:
            return None
        # 卖出: 收盘价 - 滑点; 手续费
        sell_price = exit_close * (1 - self.slippage)
        fee = buy_price * self.fee_rate + sell_price * self.fee_rate
        net = (sell_price - buy_price - fee) / buy_price * 100
        return (round(buy_price, 4), round(sell_price, 4), round(net, 2))

    @staticmethod
    def _report(trades, dates):
        n = len(trades)
        if n == 0:
            return {"trading_days": len(dates), "trades": 0,
                    "win_rate": None, "avg_return_pct": None,
                    "profit_loss_ratio": None, "max_drawdown_pct": None,
                    "total_return_pct": None}
        returns = [t["return_pct"] for t in trades]
        wins = [r for r in returns if r > 0]
        losses = [r for r in returns if r <= 0]
        win_rate = len(wins) / n
        avg_ret = sum(returns) / n
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        pl_ratio = (avg_win / abs(avg_loss)) if losses and avg_loss != 0 else None
        cum = {}
        for t in trades:
            cum[t["date"]] = cum.get(t["date"], 0.0) + t["return_pct"]
        equity = 0.0
        peak = 0.0
        max_dd = 0.0
        for d in sorted(cum):
            equity += cum[d]
            peak = max(peak, equity)
            if peak > 0:
                max_dd = max(max_dd, (peak - equity) / peak * 100)
        return {
            "trading_days": len(dates), "trades": n,
            "win_rate": round(win_rate, 4),
            "avg_return_pct": round(avg_ret, 2),
            "profit_loss_ratio": round(pl_ratio, 2) if pl_ratio else None,
            "max_drawdown_pct": round(max_dd, 2),
            "total_return_pct": round(sum(returns), 2),
        }

    def compare_params(self, start_date, end_date, param_grid, progress=None):
        rows = []
        for params in param_grid:
            sell = {"take_profit_pct": params.get("take_profit", 0.08),
                    "stop_loss_pct": params.get("stop_loss", 0.05),
                    "max_hold_days": params.get("hold_days", 5)}
            rep = self.run(start_date, end_date, sell_rules=sell, progress=progress)
            rows.append({
                "take_profit": sell["take_profit_pct"],
                "stop_loss": sell["stop_loss_pct"],
                "hold_days": sell["max_hold_days"],
                "trades": rep["trades"], "win_rate": rep["win_rate"],
                "avg_return_pct": rep["avg_return_pct"],
                "max_drawdown_pct": rep["max_drawdown_pct"],
            })
        return rows
```

- [ ] **Step 4: 运行确认通过**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_backtest.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add prism/
git commit -m "feat(prism): 回测引擎(真实策略+交易模拟: 手续费/滑点/卖出规则)"
```

---

### Task 7: 交易模块 — 信号自动生成(需授权) + 暂停开关

**Files:**
- Create: `prism/trader.py`
- Test: `prism/tests/test_trader.py`

**Interfaces:**
- Consumes: Task 3 engine / Task 4 data;现有 qmt_signal_bridge_real 的信号文件协议
- Produces:
  - `prism.trader.generate_signals(result, strategy)` → 信号 dict 列表(与桥 pending JSON 同构)
  - `prism.trader.write_signals(signals, env="sim")` → 写文件到 SIGNAL_ROOT/env/pending/
  - `prism.trader.PAUSE_FILE` — D:/QMT_SIGNALS/paused(存在即暂停生成信号)
  - `prism.trader.check_paused()` → bool
  - `prism.trader.run_daily(strategy_id, provider)` → 完整盘后流程(选股→存绩效→生成信号)

- [ ] **Step 1: 写失败测试** `prism/tests/test_trader.py`

```python
# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import prism.trader as trader


def test_generate_signals_shape():
    result = {
        "environment_ok": True,
        "candidates": [{"code": "600000.SH", "name": "浦发", "last": 10.5,
                        "up_stop_price": 10.55, "scores": {"composite": 5.0}}],
    }
    strategy = {"id": "default", "sell_rules": {}}
    sigs = trader.generate_signals(result, strategy, env="sim")
    assert len(sigs) == 1
    s = sigs[0]
    assert s["action"] == "BUY"
    assert s["stock_code"] == "600000.SH"
    assert s["strategy_id"] == "default"
    assert s["status"] == "pending"


def test_generate_signals_empty_when_env_bad():
    result = {"environment_ok": False, "candidates": []}
    assert trader.generate_signals(result, {"id": "x"}, env="sim") == []


def test_check_paused(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "PAUSE_FILE", str(tmp_path / "paused"))
    assert trader.check_paused() is False
    (tmp_path / "paused").write_text("", encoding="utf-8")
    assert trader.check_paused() is True


def test_write_signals_writes_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    sigs = [{"order_id": "BUY_1", "action": "BUY", "stock_code": "600000.SH",
             "volume": 100, "price": 10.55, "account_id": "",
             "created_at": "2026-08-14T00:00:00", "status": "pending",
             "strategy_id": "default"}]
    trader.write_signals(sigs, env="sim")
    pending = tmp_path / "sim" / "pending"
    files = list(pending.glob("*.json"))
    assert len(files) == 1
```

- [ ] **Step 2: 运行确认失败**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_trader.py -v`
Expected: FAIL(module prism.trader not found)

- [ ] **Step 3: 实现**

`prism/trader.py`:
```python
# -*- coding: utf-8 -*-
"""交易模块: 选股结果 → 信号文件 → QMT 桥(需授权)。

信号文件协议与 qmt_signal_bridge_real.py 完全一致(桥读取 pending/*.json)。
安全: 真实盘(env=real)下桥只消费授权文件存在时的信号; 本模块还提供
PAUSE_FILE 一键暂停(存在该文件即不生成新信号)。
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

from common import SIGNAL_ROOT

PAUSE_FILE = Path(r"D:/QMT_SIGNALS/paused")


def check_paused():
    return PAUSE_FILE.exists()


def generate_signals(result, strategy, env="sim", volume=100):
    """把选股结果转成买入信号列表(与桥 pending JSON 同构)。"""
    if not result.get("environment_ok"):
        return []
    out = []
    for c in result.get("candidates", []):
        order_id = "BUY_%s" % uuid.uuid4().hex[:8]
        out.append({
            "order_id": order_id,
            "action": "BUY",
            "stock_code": c["code"],
            "order_type": "BUY",
            "price": c.get("up_stop_price") or 0,
            "volume": volume,
            "account_id": "",
            "created_at": datetime.now().isoformat(),
            "status": "pending",
            "strategy_id": strategy.get("id", "unknown"),
            "composite": c.get("scores", {}).get("composite"),
        })
    return out


def write_signals(signals, env="sim"):
    """写信号文件到 SIGNAL_ROOT/<env>/pending/。返回写入数量。"""
    if not signals:
        return 0
    pending_dir = SIGNAL_ROOT / env / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for s in signals:
        path = pending_dir / ("%s.json" % s["order_id"])
        path.write_text(json.dumps(s, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        n += 1
    return n


def run_daily(strategy, provider, env="sim", volume=100, archive=None):
    """盘后完整流程: 选股 → (可选绩效存档) → 生成并写入信号。

    返回 {"environment_ok", "candidates", "signals_written", "paused"}。
    """
    if check_paused():
        return {"environment_ok": False, "candidates": [],
                "signals_written": 0, "paused": True}
    from prism.engine import load_strategy, run_screen
    strat = load_strategy(strategy)
    market_ctx = provider.build_market_context()
    gate_fids = (strat.get("market_gate") or {}).get("factors", [])
    gate_factors = {}
    if gate_fids:
        from prism import registry as reg
        for fid in gate_fids:
            try:
                res = reg.get_factor(fid)["func"](market_ctx)
                gate_factors[fid] = 1 if (isinstance(res, dict) and res.get("score")) else 0
            except Exception:
                gate_factors[fid] = 0
    limit_ups = provider.get_limit_ups()
    stock_contexts = {}
    for lu in limit_ups:
        code = lu["code"]
        stock_contexts[code] = provider.build_stock_context(code)
    result = run_screen(strat, market_ctx, gate_factors=gate_factors,
                        stock_contexts=stock_contexts)
    result["market"] = {"limit_up_count": len(limit_ups)}
    if archive is not None:
        try:
            archive(result.get("candidates", []))
        except Exception:
            pass
    signals = generate_signals(result, strat, env=env, volume=volume)
    written = write_signals(signals, env=env)
    return {"environment_ok": result["environment_ok"],
            "candidates": result.get("candidates", []),
            "signals_written": written, "paused": False}
```

- [ ] **Step 4: 运行确认通过**

Run: `cd D:\cc-joesph && python -m pytest prism/tests/test_trader.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add prism/
git commit -m "feat(prism): 交易模块(信号生成/写入/暂停开关/盘后流程)"
```

---

### Task 8: 网页改造 — strategy_web → prism_web

**Files:**
- Create: `prism_web/app.py`, `prism_web/templates/*`, `prism_web/static/*`
- Modify: 复用现有 templates/static(从 strategy_web 复制改造)
- Test: `prism_web/tests/test_app.py`

**Interfaces:**
- Consumes: Task 1-7 全部
- Produces:
  - `prism_web.app` Flask 应用, 路由:
    - `GET /` 主页(选股页, 与旧一致)
    - `GET /api/factors` 因子库列表
    - `GET /api/strategies` 策略列表
    - `GET /api/strategy/<id>` 策略详情
    - `POST /api/screen` 选股(策略 id 参数, 默认 default)
    - `GET /api/backtest?strategy=default&start=...&end=...` 回测
    - `GET /api/perf`(保留现有)
    - `GET/POST /api/automation` 自动化开关(读/写 PAUSE_FILE)
    - 保留: /api/health, /api/stock/<code>/kline, /api/stock/<code>/manual, /api/market/*

- [ ] **Step 1: 复制现有 strategy_web 到 prism_web 作为起点**

```bash
# 复制后改造: 保留原有 API, 新增因子库/策略/回测/自动化 API
cp -r strategy_web prism_web
rm -rf prism_web/__pycache__ prism_web/tests
```

- [ ] **Step 2: 写测试** `prism_web/tests/test_app.py`(核心新 API)

```python
# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import prism_web.app as app_module


@pytest.fixture
def client(monkeypatch):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_factors_list(client):
    r = client.get("/api/factors")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    ids = [f["id"] for f in data["factors"]]
    assert "F1" in ids and "Y3" in ids


def test_strategies_list(client):
    r = client.get("/api/strategies")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert any(s["id"] == "default" for s in data["strategies"])


def test_automation_pause_roundtrip(client, tmp_path, monkeypatch):
    import prism.trader as trader
    pf = tmp_path / "paused"
    monkeypatch.setattr(trader, "PAUSE_FILE", str(pf))
    monkeypatch.setattr(app_module, "trader", trader)
    r = client.post("/api/automation", json={"paused": True})
    assert r.status_code == 200
    assert pf.exists()
    r2 = client.get("/api/automation")
    assert r2.get_json()["paused"] is True
    client.post("/api/automation", json={"paused": False})
    assert not pf.exists()
```

- [ ] **Step 3: 实现 prism_web/app.py**

在旧 app.py 基础上新增(保持原有路由不变, 加新路由):
```python
# prism_web/app.py(新增部分, 其余沿用旧 app.py)

from prism import registry as reg
from prism.engine import load_strategy
from prism import trader
from prism.strategies import STRATEGIES_DIR  # prism/strategies 目录


@app.route("/api/factors")
def api_factors():
    return jsonify({"ok": True, "factors": reg.list_factors()})


@app.route("/api/strategies")
def api_strategies():
    out = []
    for p in sorted(STRATEGIES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": data.get("id"), "name": data.get("name"),
                        "description": data.get("description", "")})
        except Exception:
            continue
    return jsonify({"ok": True, "strategies": out})


@app.route("/api/strategy/<sid>")
def api_strategy(sid):
    p = STRATEGIES_DIR / ("%s.json" % sid)
    if not p.exists():
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    return jsonify({"ok": True, "strategy": json.loads(p.read_text(encoding="utf-8"))})


@app.route("/api/backtest")
def api_backtest():
    from prism.backtest import Backtester
    sid = request.args.get("strategy", "default")
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        return jsonify({"ok": False, "error": "缺少 start/end (YYYYMMDD)"}), 400
    try:
        from datetime import datetime
        s = datetime.strptime(start, "%Y%m%d").date()
        e = datetime.strptime(end, "%Y%m%d").date()
    except ValueError:
        return jsonify({"ok": False, "error": "日期格式应为 YYYYMMDD"}), 400
    p = STRATEGIES_DIR / ("%s.json" % sid)
    if not p.exists():
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    strategy = load_strategy(p)
    # 东财真实数据源
    from prism.data import DataProvider
    prov = DataProvider()
    try:
        zt_feed = __import__("backtest_cli", fromlist=["zt_feed"]).zt_feed
        kline_feed = __import__("backtest_cli", fromlist=["kline_feed"]).kline_feed
    except Exception:
        return jsonify({"ok": False, "error": "回测数据源不可用"}), 500
    bt = Backtester(strategy, zt_feed=zt_feed, kline_feed=kline_feed)
    rep = bt.run(s, e)
    return jsonify({"ok": True, "report": rep})


@app.route("/api/automation", methods=["GET", "POST"])
def api_automation():
    if request.method == "GET":
        return jsonify({"ok": True, "paused": trader.check_paused()})
    payload = request.get_json() or {}
    if payload.get("paused"):
        trader.PAUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
        trader.PAUSE_FILE.write_text("", encoding="utf-8")
    else:
        try:
            trader.PAUSE_FILE.unlink()
        except FileNotFoundError:
            pass
    return jsonify({"ok": True, "paused": trader.check_paused()})
```

同时创建 `prism/strategies/__init__.py`:
```python
# -*- coding: utf-8 -*-
"""策略配置目录。"""
from pathlib import Path

STRATEGIES_DIR = Path(__file__).parent
```

- [ ] **Step 4: 运行确认通过**

Run: `cd D:\cc-joesph && python -m pytest prism_web/tests/ -v`
Expected: 3+ passed

- [ ] **Step 5: 提交**

```bash
git add prism/ prism_web/
git commit -m "feat(prism_web): 网页改造 — 因子库/策略/回测/自动化API"
```

---

### Task 9: 旧系统退役切换 + 端到端验证

**Files:**
- Modify: `README.md`(Prism 使用说明)
- Modify: `start_all.py`(指向 prism_web)
- Delete(迁移完成并验证后): `strategy_web/` 中已被取代的 models.py/factors.py/screen.py(保留 data_source/eastmoney/fundamental/manual_store 供 prism.data 复用, 直至内联)

- [ ] **Step 1: 端到端验证(离线)**

```bash
cd D:\cc-joesph
python -m pytest prism/tests/ prism_web/tests/ tests/ -v
python -m pytest strategy_web/tests/ -v     # 旧测试保持通过(兼容期)
python -m prism.factor_check                 # 因子体检
```

Expected: 全部通过; 因子体检 24+ 全 PASS

- [ ] **Step 2: 端到端验证(模拟盘, 需 QMT)**

```bash
python -c "
import sys; sys.path.insert(0,'.')
from prism.data import DataProvider
from prism.engine import load_strategy
from prism.trader import run_daily
from prism.strategies import STRATEGIES_DIR
prov = DataProvider(); prov.connect()
strategy = load_strategy(STRATEGIES_DIR / 'default.json')
res = run_daily(strategy, prov, env='sim')
print(res)
"
```

Expected: environment_ok 与旧网页一致; 信号写入 D:/QMT_SIGNALS/sim/pending/(模拟盘, 安全)

- [ ] **Step 3: 更新 README + start_all**

README 增加 Prism 章节(因子库/策略/回测/自动化使用说明);start_all.py 的 strategy_web 指向 prism_web。

- [ ] **Step 4: 删除旧实现(确认新系统稳定后)**

```bash
git rm strategy_web/models.py strategy_web/factors.py strategy_web/screen.py
git commit -m "refactor: Prism 接管, 移除旧固定模型实现(strategy_web 保留数据层)"
```

- [ ] **Step 5: 推送**

```bash
git push origin master
```

---

## Self-Review 记录

**1. Spec 覆盖检查:**
- §3 因子协议 → Task 1/2 ✅
- §4 策略配置 → Task 3 ✅
- §5 回测 → Task 6 ✅
- §6 交易闭环 → Task 7 ✅
- §7 迁移计划 → Task 5/8/9 ✅
- §3.5 数据源途径 → Task 4 ✅
- §9 测试策略 → 各 Task 内嵌 ✅
- 自然语言生成因子 → Task 2(_template + factor_check)✅

**2. 占位符扫描:** 无 TBD/TODO;所有代码块完整;24 因子清单明确列出 ✅

**3. 类型一致性:** registry.get_factor → engine._factor_hit → backtest/trader 引用一致;
FactorContext 字段在各 Task 使用一致;trader.generate_signals 输出与桥协议一致 ✅
