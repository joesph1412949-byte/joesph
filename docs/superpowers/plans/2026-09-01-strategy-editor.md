# 网页策略编辑器 + 默认切换 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 网页可视化策略编辑器（勾因子/调权重/设门槛/卖出规则 → 保存新策略）+ "设为默认"按钮一处切换三处生效（选股默认/回测可选/模拟盘热重载）。

**Architecture:** 指针文件 `prism/strategies/.active.json` 为默认策略唯一真相源；`engine.active_strategy_id()/set_active_strategy()` 统一读写（原子写、损坏回落 first_board_v04）；校验器 `engine.validate_strategy_payload` 纯函数全规则把关 + `load_strategy` 试载；GUI 两写端点（create/activate）+ 编辑面板纯前端组装。

**Tech Stack:** Python 3.12 / Flask + 原生 JS / pytest（全离线，mock 文件系统与注册表）。

## Global Constraints

- **指针纪律**：`prism/strategies/.active.json`，内容 `{"id": "...", "updated": "..."}`；读写全部经 `engine.active_strategy_id()` / `engine.set_active_strategy(sid)`；原子写（temp+`os.replace`）；缺文件/损坏 JSON/id 对应策略文件不存在 → 一律回落 `"first_board_v04"`
- **payload 结构（spec §3 逐字段）**：`{"name", "models":[{"id","name","weight","factors","weights"}], "gate_factors", "gate_threshold", "candidate_min_model", "sell":{"take_profit_pct","stop_loss_pct","max_hold_days"}}`
- **校验规则（spec §4 全 8 条）**：name 非空≤40；models 1-3 个、每模型 factors 非空全注册、因子不跨模型重复；因子权重缺省 1.0 且为正；gate_factors ⊆ node 类可空；gate_threshold ≥0 整数；candidate_min_model ≥1 整数；sell 0<tp≤0.5、0<sl≤0.5、1≤hold≤30；终检 `load_strategy` 试载；**任何一条不过零写入**
- **cap 自动公式**：`cap = Σ(模型权重 × 该模型因子权重之和)`（复算校验：default 三模型=7.0、v04=9.0）
- **id 自动生成**：`custom_%Y%m%d_%H%M%S`，同秒冲突加 `_2` 序号（在创建端点查目录实现，校验器不含 id 逻辑）
- **default.json 保护**：永不被编辑器写/覆盖（快照测试既有锁定）
- **可修改文件白名单**：`prism/engine.py`（追加）、`prism/paper.py`（strategy getter 重构）、`prism_web/app.py`（fallback 改+2 端点+列表字段）、`prism_web/templates/index.html`、`prism_web/static/app.js`、`.gitignore`（1 行）、新建/追加测试文件。其余一律不动。
- **测试命令**：`--import-mode=importlib --basetemp=D:\cc-joesph\pt_btNN`（NN 从 **102** 起）；`$env:PYTHONIOENCODING='utf-8'` 先行；全离线（策略文件写入用 tmp 注入或 monkeypatch 目录）
- **ponytail 精简约束（用户指定延续）**：阶梯（该不该存在→复用已有→标准库→一行→最小实现）；不加未请求抽象；砍掉的简化用 `# ponytail:` 注释；绝不简化掉：校验完整性、原子写、指针回落、default 保护
- git 只本地 commit，push 前问用户

---

### Task 1: 默认指针机制（engine 读写 + 回落 + gitignore）

**Files:**
- Modify: `prism/engine.py`（追加三件：`STRATEGIES_DIR` 常量、`active_strategy_id()`、`set_active_strategy(sid)`）
- Modify: `.gitignore`（追加一行 `prism/strategies/.active.json`）
- Test: `prism/tests/test_strategy_pointer.py`（新建）

**Interfaces:**
- Produces:
  - `engine.STRATEGIES_DIR = Path(__file__).parent / "strategies"`
  - `active_strategy_id(pointer_path=None) -> str`：缺省读 `STRATEGIES_DIR/.active.json`；缺文件/JSON 损坏/无 id 键/所指策略文件不存在 → `"first_board_v04"`；正常 → 指针 id
  - `set_active_strategy(sid, pointer_path=None) -> None`：原子写 `{"id": sid, "updated": <ISO 时间>}`（temp+os.replace）；不校验 sid 存在性（端点层校验）
  - `ACTIVE_FILENAME = ".active.json"`

- [x] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""默认策略指针测试 — tmp 注入, 全离线。"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism import engine


def test_active_default_when_no_pointer(tmp_path):
    p = tmp_path / "active.json"
    assert engine.active_strategy_id(pointer_path=p) == "first_board_v04"
    assert not p.exists()          # 读不写


def test_active_reads_pointer(tmp_path):
    p = tmp_path / "active.json"
    p.write_text(json.dumps({"id": "first_board_v03"}), encoding="utf-8")
    assert engine.active_strategy_id(pointer_path=p) == "first_board_v03"


def test_active_falls_back_on_corrupt(tmp_path):
    p = tmp_path / "active.json"
    p.write_text("{broken", encoding="utf-8")
    assert engine.active_strategy_id(pointer_path=p) == "first_board_v04"
    assert p.read_text(encoding="utf-8") == "{broken"   # 不覆盖


def test_active_falls_back_when_strategy_missing(tmp_path):
    p = tmp_path / "active.json"
    p.write_text(json.dumps({"id": "no_such_strategy"}), encoding="utf-8")
    assert engine.active_strategy_id(pointer_path=p) == "first_board_v04"


def test_set_active_roundtrip(tmp_path):
    p = tmp_path / "active.json"
    engine.set_active_strategy("first_board_v03", pointer_path=p)
    assert json.loads(p.read_text(encoding="utf-8"))["id"] == "first_board_v03"
    assert engine.active_strategy_id(pointer_path=p) == "first_board_v03"
    assert not (tmp_path / "active.json.tmp").exists()   # 原子写无残留


def test_real_pointer_is_v04():
    """仓库真实指针(若存在)不得指向不存在的策略; 缺省回落 v04。"""
    rid = engine.active_strategy_id()
    assert (engine.STRATEGIES_DIR / ("%s.json" % rid)).is_file()
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_strategy_pointer.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt102`
Expected: FAIL（engine 无 active_strategy_id 属性 → AttributeError）

- [x] **Step 3: 实现（engine.py 追加）**

```python
# ---------------- 默认策略指针(策略编辑器 spec §5) ----------------
STRATEGIES_DIR = Path(__file__).parent / "strategies"
ACTIVE_FILENAME = ".active.json"
_ACTIVE_FALLBACK = "first_board_v04"


def active_strategy_id(pointer_path=None):
    """读默认策略指针; 缺文件/损坏/所指策略不存在 → 回落 first_board_v04。"""
    p = Path(pointer_path) if pointer_path \
        else STRATEGIES_DIR / ACTIVE_FILENAME
    try:
        sid = json.loads(p.read_text(encoding="utf-8")).get("id")
    except Exception:
        return _ACTIVE_FALLBACK
    if not sid or not (STRATEGIES_DIR / ("%s.json" % sid)).is_file():
        return _ACTIVE_FALLBACK
    return sid


def set_active_strategy(sid, pointer_path=None):
    """写默认策略指针(原子写; sid 存在性由调用方校验)。"""
    p = Path(pointer_path) if pointer_path \
        else STRATEGIES_DIR / ACTIVE_FILENAME
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(
        {"id": sid, "updated": datetime.now().isoformat(timespec="seconds")},
        ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
```

（engine.py 头部补 import：`import json, os, datetime`——检查既有 import，缺则补。）

- [x] **Step 4: 跑测试确认通过 + 全量 paper 回归（getter 未动，零影响）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_strategy_pointer.py prism/tests/test_paper_account.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt102`
Expected: 6+5 passed

- [x] **Step 5: Commit**

```bash
git add prism/engine.py .gitignore prism/tests/test_strategy_pointer.py
git commit -m "feat(prism): 默认策略指针 — 原子读写/损坏回落/gitignore"
```

---

### Task 2: 消费方接线（选股 fallback + 模拟盘热重载）

**Files:**
- Modify: `prism_web/app.py`（/api/screen fallback 一行改）
- Modify: `prism/paper.py`（strategy getter 重构为动态读指针）
- Test: `prism_web/tests/test_app.py`（追加 1）、`prism/tests/test_strategy_pointer.py`（追加 2）

**Interfaces:**
- Consumes: Task 1 `active_strategy_id(pointer_path=None)`；Task 2 之前 PaperAccount 的 getter（惰性 load + scan_factors——保留其注册副作用）
- Produces:
  - /api/screen 无参数 → 用指针 id（缺省仍 first_board_v04，现测试不破坏）
  - `PaperAccount.strategy`：每次访问读指针，id 与已载策略不同才重载；**指针 JSON 损坏 → 保留当前已载策略不中断**（active_strategy_id 回落 v04——若当前已载的正是 v04 则无变化；若当前载的是用户自定义且指针坏了回落 v04 会切换——按 spec §10"坏文件保留旧策略"更稳：getter 捕获 `load_strategy` 异常保留旧策略；指针读取本身回落 v04 属正常语义。实现：`try: 新策略 = load_strategy(...) except Exception: 保留旧`）

- [x] **Step 1: 写失败测试**

test_strategy_pointer.py 追加：

```python
def test_paper_hot_reload(tmp_path, monkeypatch):
    """指针变化 → PaperAccount.strategy 下次访问换策略(账本不变)。"""
    import shutil
    from prism.paper import PaperAccount
    # tmp 需有真实 v03 策略文件(回落检查要求所指文件存在)
    shutil.copy(Path(engine.__file__).parent / "strategies"
                / "first_board_v03.json", tmp_path / "first_board_v03.json")
    acc = PaperAccount(state_path=tmp_path / "paper.json")
    acc.init_account()
    assert acc.strategy["id"] == "first_board_v04"      # 缺省
    # 写指针指向 v03 → 热重载
    engine.set_active_strategy("first_board_v03", pointer_path=
                               tmp_path / engine.ACTIVE_FILENAME)
    monkeypatch.setattr(engine, "STRATEGIES_DIR", tmp_path)
    assert acc.strategy["id"] == "first_board_v03"
    assert acc.state["cash"] == 1000000.0               # 账本不动
```

test_app.py 追加：

```python
def test_screen_default_follows_pointer(client, tmp_path, monkeypatch):
    """指针指向 v03 → 无参数选股用 v03。"""
    import prism.engine as engine
    from prism.paper import PaperAccount
    monkeypatch.setattr(app_module.ds_obj, "_connected", True)
    monkeypatch.setattr(app_module, "SNAPSHOT_PATH", tmp_path / "s.json")
    monkeypatch.setattr(app_module, "perf_store_obj",
                        type("FakePerf", (), {"archive_daily": lambda s, c, d=None: None})())
    monkeypatch.setattr(app_module, "run_screen",
                        lambda st, mc, gate_factors=None, stock_contexts=None:
                        {"environment_ok": True, "gate_score": 1, "candidates": [],
                         "summary": {"candidate_count": 0}})
    seen = {}
    def fake_screen(strategy, market_ctx, gate_factors=None, stock_contexts=None):
        seen["id"] = strategy["id"]
        return {"environment_ok": True, "gate_score": 1, "candidates": [],
                "summary": {"candidate_count": 0}}
    monkeypatch.setattr(app_module, "run_screen", fake_screen)
    monkeypatch.setattr(app_module, "DataProvider",
                        lambda ds=None, manual=None: _stub_provider())
    # 把真实指针临时指向 v03(测试后恢复)
    real_ptr = engine.STRATEGIES_DIR / engine.ACTIVE_FILENAME
    old = real_ptr.read_text(encoding="utf-8") if real_ptr.exists() else None
    try:
        engine.set_active_strategy("first_board_v03")
        r = client.post("/api/screen")
        assert r.status_code == 200 and seen["id"] == "first_board_v03"
    finally:
        if old is not None:
            real_ptr.write_text(old, encoding="utf-8")
        else:
            real_ptr.unlink(missing_ok=True)
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_strategy_pointer.py prism_web/tests/test_app.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt103`
Expected: 新 2 测试 FAIL（getter 不读指针 / fallback 仍是 v04 硬编码——注意 test_screen_default_follows_pointer 会过(指针未接线时 seen=first_board_v04≠v03 FAIL ✓ 判别力成立)

- [x] **Step 3: 实现**

app.py /api/screen fallback（现 L299-300）改：

```python
        from prism.engine import active_strategy_id
        sid = (payload.get("strategy") or request.form.get("strategy")
               or request.args.get("strategy") or active_strategy_id())
```

paper.py `strategy` getter 重构（保留 Task 2(p2) 的 scan_factors 副作用）：

```python
    @property
    def strategy(self):
        """策略(动态读默认指针; 指针变化→热重载; 坏文件保留旧策略)。"""
        from prism.engine import active_strategy_id, load_strategy
        sid = active_strategy_id()
        if self._strategy is None or self._strategy.get("id") != sid:
            try:
                self._strategy = load_strategy(
                    self.strategy_path.parent.parent / "strategies"
                    / ("%s.json" % sid))
                from prism import registry as reg
                reg.scan_factors(force=True)
            except Exception:
                if self._strategy is None:
                    raise            # 首载且失败 → 无退路, 照抛
                pass                 # 已有旧策略 → 保留(spec §10)
        return self._strategy
```

（路径推导注意：`self.strategy_path` 缺省是 `prism/strategies/first_board_v04.json` → `parent.parent` = prism/；用 `engine.STRATEGIES_DIR` 更直白——实现时用 `from prism import engine; engine.STRATEGIES_DIR`。）

- [x] **Step 4: 跑测试确认通过 + 全量回归抽查**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt103`
Expected: 全 passed（359+2=361，存量不破坏——尤其 test_screen_default_strategy_is_v04 仍过：指针缺省回落 v04 ✓）

- [x] **Step 5: Commit**

```bash
git add prism_web/app.py prism/paper.py prism/tests/test_strategy_pointer.py prism_web/tests/test_app.py
git commit -m "feat(prism): 默认指针接线 — 选股fallback+模拟盘热重载"
```

---

### Task 3: 校验器（validate_strategy_payload 纯函数）

**Files:**
- Modify: `prism/engine.py`（追加 `validate_strategy_payload` 与 `_num` 小助手）
- Test: `prism/tests/test_strategy_validator.py`（新建）

**Interfaces:**
- Consumes: `registry.FACTORS`（id→{name,category,description,func}，测试用 fake reg 注入或真注册表+scan）
- Produces:
  - `validate_strategy_payload(payload, reg=None) -> (ok: bool, errors: [str], strategy_dict: dict|None)`
  - strategy_dict 结构见 spec §3 产出（`id` 置 None 由端点填；`composite.cap` 自动 = Σ(模型权重×该模型对齐后因子权重之和)，round 2）
  - 因子权重对齐规则：用户给的 weights 截断到 factors 长度、不足补 1.0

- [x] **Step 1: 写失败测试**

```python
# -*- coding: utf-8 -*-
"""策略校验器测试 — 真注册表(已含全部44因子), 全离线。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism import registry as reg
from prism.engine import validate_strategy_payload


def _good(**over):
    p = {"name": "我的组合",
         "models": [{"id": "first_board", "name": "首板", "weight": 1.0,
                     "factors": ["F1", "F8"], "weights": [1, 1]}],
         "gate_factors": ["N1"], "gate_threshold": 1,
         "candidate_min_model": 3,
         "sell": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                  "max_hold_days": 5}}
    p.update(over)
    return p


@pytest.fixture(scope="module", autouse=True)
def _scan():
    reg.scan_factors(force=True)


def test_valid_payload():
    ok, errs, s = validate_strategy_payload(_good())
    assert ok is True and errs == []
    assert s["composite"]["cap"] == 2.0        # 1.0×(1+1)
    assert s["market_gate"] == {"model": "node", "threshold": 1,
                                "factors": ["N1"]}
    assert s["sell_rules"]["take_profit_pct"] == 0.08
    assert s["id"] is None                     # id 由端点生成


def test_name_empty():
    ok, errs, _ = validate_strategy_payload(_good(name="  "))
    assert ok is False and any("名称" in e for e in errs)


def test_factor_unknown():
    ok, errs, _ = validate_strategy_payload(_good(
        models=[{"id": "m", "name": "m", "weight": 1.0,
                 "factors": ["F1", "ZZ9"], "weights": []}]))
    assert ok is False and any("ZZ9" in e for e in errs)


def test_factor_dup_across_models():
    ok, errs, _ = validate_strategy_payload(_good(
        models=[{"id": "a", "name": "a", "weight": 0.5,
                 "factors": ["F1", "F2"], "weights": []},
                {"id": "b", "name": "b", "weight": 0.5,
                 "factors": ["F2", "Y1"], "weights": []}]))
    assert ok is False and any("重复" in e for e in errs)


def test_gate_must_be_node():
    ok, errs, _ = validate_strategy_payload(_good(gate_factors=["F1"]))
    assert ok is False and any("node" in e for e in errs)


def test_gate_can_be_empty():
    ok, errs, s = validate_strategy_payload(_good(gate_factors=[]))
    assert ok is True and s["market_gate"]["factors"] == []


def test_sell_out_of_range():
    ok, errs, _ = validate_strategy_payload(_good(
        sell={"take_profit_pct": 0.8, "stop_loss_pct": 0.05,
              "max_hold_days": 5}))
    assert ok is False and any("止盈" in e for e in errs)


def test_weights_alignment():
    """weights 长度不齐 → 截断+补1.0; cap 按对齐后求和。"""
    ok, errs, s = validate_strategy_payload(_good(
        models=[{"id": "m", "name": "m", "weight": 0.5,
                 "factors": ["F1", "F8", "F9"], "weights": [2.0, 0.0, 3.0]}]))
    assert ok is False and any("因子权重" in e for e in errs)   # 0.0 非法
    ok2, _, s2 = validate_strategy_payload(_good(
        models=[{"id": "m", "name": "m", "weight": 0.5,
                 "factors": ["F1", "F8", "F9"], "weights": [2.0]}]))
    assert ok2 is True
    assert s2["scoring_models"][0]["weights"] == [2.0, 1.0, 1.0]
    assert s2["composite"]["cap"] == round(0.5 * 4.0, 2)
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_strategy_validator.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt104`
Expected: FAIL（函数不存在）

- [x] **Step 3: 实现（engine.py 追加）**

```python
def _num(v, default):
    try:
        return float(v) if v is not None else float(default)
    except Exception:
        return float("nan")


def validate_strategy_payload(payload, reg=None):
    """策略编辑器 payload 全规则校验(spec §4)。返回 (ok, errors, strategy)。"""
    from prism import registry as _reg
    reg = reg or _reg
    errors = []
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 40:
        errors.append("策略名称: 需非空且不超过40字")
    models = payload.get("models") or []
    if not 1 <= len(models) <= 3:
        errors.append("评分模型: 需1-3个")
    seen = set()
    model_rows, model_weights = [], []
    for i, m in enumerate(models, 1):
        fs = list(m.get("factors") or [])
        if not fs:
            errors.append("模型%d: 至少勾选1个因子" % i)
        for fid in fs:
            if fid not in reg.FACTORS:
                errors.append("模型%d: 因子 %s 不存在" % (i, fid))
            if fid in seen:
                errors.append("因子 %s 在多个模型重复" % fid)
            seen.add(fid)
        raw_w = [float(x) for x in (m.get("weights") or [])][:len(fs)]
        fws = raw_w + [1.0] * (len(fs) - len(raw_w))
        if any(w <= 0 for w in fws):
            errors.append("模型%d: 因子权重需为正数" % i)
        mw = _num(m.get("weight"), 1.0)
        if not (mw > 0):
            errors.append("模型%d: 模型权重需为正数" % i)
        model_rows.append({"id": m.get("id") or "model_%d" % i,
                           "name": m.get("name") or m.get("id") or "自定义",
                           "weight": mw, "factors": fs, "weights": fws})
        model_weights.append(mw)
    gate = list(payload.get("gate_factors") or [])
    for fid in gate:
        meta = reg.FACTORS.get(fid)
        if meta is None or meta.get("category") != "node":
            errors.append("门槛因子 %s 不是环境门槛类(node)" % fid)
    gt = payload.get("gate_threshold")
    try:
        gt = int(gt) if gt is not None else 0
    except Exception:
        gt = -1
    if gt < 0:
        errors.append("门槛线: 需≥0整数")
    try:
        cmm = int(payload.get("candidate_min_model"))
    except Exception:
        cmm = -1
    if cmm < 1:
        errors.append("候选资质线: 需≥1整数")
    s = payload.get("sell") or {}
    tp = _num(s.get("take_profit_pct"), 0.08)
    sl = _num(s.get("stop_loss_pct"), 0.05)
    hold = _num(s.get("max_hold_days"), 5)
    if not 0 < tp <= 0.5:
        errors.append("止盈比例需在 0~50%")
    if not 0 < sl <= 0.5:
        errors.append("止损比例需在 0~50%")
    if not 1 <= hold <= 30:
        errors.append("持有天数需 1~30")
    if errors:
        return False, errors, None
    cap = round(sum(m["weight"] * sum(m["weights"]) for m in model_rows), 2)
    strategy = {
        "id": None, "name": name, "description": "网页编辑器生成",
        "market_gate": {"model": "node", "threshold": gt, "factors": gate},
        "scoring_models": model_rows,
        "composite": {"mode": "top3_weighted", "weights": model_weights,
                      "cap": cap},
        "filters": {"candidate_min_model": cmm, "environment_threshold": gt},
        "sell_rules": {"take_profit_pct": tp, "stop_loss_pct": sl,
                       "max_hold_days": int(hold)}}
    return True, [], strategy
```

- [x] **Step 4: 跑测试确认通过（顺带回归 paper 不受影响）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_strategy_validator.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt104`
Expected: 8 passed

- [x] **Step 5: Commit**

```bash
git add prism/engine.py prism/tests/test_strategy_validator.py
git commit -m "feat(prism): 策略校验器 — 全规则把关+cap自动+试载前置"
```

---

### Task 4: 创建/激活端点 + 列表 active 字段

**Files:**
- Modify: `prism_web/app.py`（`/api/strategies` 加 active 字段；追加 create/activate 两端点）
- Test: `prism_web/tests/test_app.py`（追加 4）

**Interfaces:**
- Consumes: Task 1 `set_active_strategy/active_strategy_id`、Task 3 `validate_strategy_payload`、现有 `_valid_sid`/`STRATEGIES_DIR`/`load_strategy`
- Produces:
  - `POST /api/strategies/create`：校验（400+errors 零写入）→ id=`custom_%Y%m%d_%H%M%S`（同秒冲突 `_2` 序号）→ `load_strategy` 试载（400 失败）→ 原子写 `{id}.json` → `{"ok": true, "id"}`
  - `POST /api/strategies/<sid>/activate`：`_valid_sid` ∧ 文件存在（404）→ `set_active_strategy(sid)` → `{"ok": true, "active"}`
  - `GET /api/strategies`：响应加 `"active": active_strategy_id()`

- [x] **Step 1: 写失败测试（追加到 test_app.py）**

```python
# ---------------- 策略编辑器端点 ----------------

_EDITOR_PAYLOAD = {
    "name": "测试组合",
    "models": [{"id": "first_board", "name": "首板", "weight": 1.0,
                "factors": ["F1", "F8"], "weights": [1, 1]}],
    "gate_factors": ["N1"], "gate_threshold": 1,
    "candidate_min_model": 3,
    "sell": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
             "max_hold_days": 5}}


def test_strategy_create_ok(client, tmp_path, monkeypatch):
    import prism.engine as engine
    monkeypatch.setattr(app_module, "STRATEGIES_DIR", tmp_path)
    r = client.post("/api/strategies/create", json=_EDITOR_PAYLOAD)
    assert r.status_code == 200
    sid = r.get_json()["id"]
    assert sid.startswith("custom_")
    p = tmp_path / ("%s.json" % sid)
    assert p.is_file()
    s = engine.load_strategy(p)          # 试载即可用
    assert s["id"] == sid and s["composite"]["cap"] == 2.0
    # 清理: 测试不污染真实目录(tmp 注入已保证)


def test_strategy_create_reject_and_no_write(client, tmp_path, monkeypatch):
    bad = dict(_EDITOR_PAYLOAD, models=[{"id": "m", "name": "m",
                                        "weight": 1.0,
                                        "factors": ["F1", "ZZ9"],
                                        "weights": []}])
    r = client.post("/api/strategies/create", json=bad)
    assert r.status_code == 400
    assert r.get_json()["ok"] is False and r.get_json()["errors"]
    assert not any(tmp_path.glob("custom_*.json"))


def test_strategy_activate(client, tmp_path, monkeypatch):
    import prism.engine as engine
    monkeypatch.setattr(app_module, "STRATEGIES_DIR", tmp_path)
    monkeypatch.setattr(engine, "STRATEGIES_DIR", tmp_path)
    (tmp_path / "my.json").write_text(
        engine._json.dumps({"id": "my", "name": "x",
                            "scoring_models": []}), encoding="utf-8")
    r404 = client.post("/api/strategies/no_such/activate")
    assert r404.status_code == 404
    r = client.post("/api/strategies/my/activate")
    assert r.status_code == 200 and r.get_json()["active"] == "my"
    assert engine.active_strategy_id() == "my"     # 指针已写(tmp 注入)


def test_strategies_list_has_active(client, monkeypatch):
    import prism.engine as engine
    r = client.get("/api/strategies")
    assert r.status_code == 200
    assert r.get_json()["active"] == engine.active_strategy_id()
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt105`
Expected: 新 4 测试 FAIL（404/无 active 字段）

- [x] **Step 3: 实现（app.py）**

`api_strategies` 返回行改：

```python
    from prism.engine import active_strategy_id
    return jsonify({"ok": True, "strategies": out,
                    "active": active_strategy_id()})
```

`api_strategy` 函数后追加两端点：

```python
@app.route("/api/strategies/create", methods=["POST"])
def api_strategy_create():
    from prism.engine import validate_strategy_payload
    payload = request.get_json(silent=True) or {}
    ok, errors, strat = validate_strategy_payload(payload)
    if not ok:
        return jsonify({"ok": False, "errors": errors}), 400
    _dt = _dt_module
    base = "custom_" + _dt.now().strftime("%Y%m%d_%H%M%S")
    sid = base
    n = 2
    while (STRATEGIES_DIR / ("%s.json" % sid)).exists():
        sid = "%s_%d" % (base, n)
        n += 1
    strat["id"] = sid
    strat["description"] = ("网页编辑器生成 %s"
                            % _dt.now().isoformat(timespec="seconds"))
    try:
        load_strategy(strat)            # 终极校验: 试载(spec §4-7)
    except Exception as e:
        return jsonify({"ok": False,
                        "errors": ["引擎试载失败: %r" % e]}), 400
    p = STRATEGIES_DIR / ("%s.json" % sid)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(_json.dumps(strat, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)
    return jsonify({"ok": True, "id": sid})


@app.route("/api/strategies/<sid>/activate", methods=["POST"])
def api_strategy_activate(sid):
    from prism.engine import set_active_strategy
    if not _valid_sid(sid):
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    p = STRATEGIES_DIR / ("%s.json" % sid)
    if not p.is_file():
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    set_active_strategy(sid)
    return jsonify({"ok": True, "active": sid})
```

（`_dt_module` = app.py 顶部已有的 `import datetime as _dt` 同名核对——实现时按现有导入实际名替换；`os` 若未导入则补。）

- [x] **Step 4: 跑测试确认通过 + 全 app 回归**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt105`
Expected: 31 passed（27+4）

- [x] **Step 5: Commit**

```bash
git add prism_web/app.py prism_web/tests/test_app.py
git commit -m "feat(prism_web): 策略创建/激活端点 — 校验+原子写+试载+默认切换"
```

---

### Task 5: 前端编辑面板（策略页）

**Files:**
- Modify: `prism_web/templates/index.html`（策略 section 内追加编辑条与面板）
- Modify: `prism_web/static/app.js`（追加编辑器函数组 + **重构 `loadStrategies` 的列表模板**加默认徽标与两按钮；其余函数不动）
- Test: `prism_web/tests/test_app.py`（追加 1 个 DOM 冒烟）

**Interfaces:**
- Consumes: Task 4 两端点（create/activate）、现有 `GET /api/factors`（id/name/category/description）、现有 `GET /api/strategy/<sid>`（复制底稿数据源）、现有 `api(url)` fetch 助手
- Produces: 编辑面板完整交互（新建/复制底稿/分组勾选/权重/门槛/卖出/保存→刷新列表；保存**不自动激活**——用户手动点"设为默认"）

- [x] **Step 1: 写失败测试（追加到 test_app.py）**

```python
def test_strategy_editor_dom(client):
    """编辑面板骨架全部渲染(模板完整性)。"""
    r = client.get("/")
    html = r.get_data(as_text=True)
    for mark in ("btn-new-strategy", "strategy-editor", "ed-name",
                 "ed-models", "ed-gate", "ed-tp", "ed-sl", "ed-hold"):
        assert mark in html, "模板缺 %s" % mark
```

- [x] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py::test_strategy_editor_dom -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt106`
Expected: FAIL（模板缺元素）

- [x] **Step 3: 实现 HTML（index.html 策略 section 内、`#strategy-layout` 之前追加）**

```html
      <div id="editor-bar" class="filter-row">
        <button id="btn-new-strategy" onclick="openEditor()">➕ 新建策略</button>
        <span id="active-badge" class="hint"></span>
      </div>
      <div id="strategy-editor" style="display:none; border:1px solid #ccc; padding:12px; margin-bottom:12px;">
        <h3>新建策略</h3>
        <label>策略名称 <input id="ed-name" maxlength="40"></label>
        <div id="ed-models"></div>
        <button onclick="addEditorModel()">＋ 加一个评分模型</button>
        <h4>市场环境门槛</h4>
        <div id="ed-gate"></div>
        <p>门槛线 <input id="ed-gate-threshold" type="number" value="1" style="width:60px">
          资质线 <input id="ed-min-model" type="number" value="3" style="width:60px">（候选至少命中几个因子才算入选）</p>
        <h4>卖出规则</h4>
        <p>止盈% <input id="ed-tp" type="number" step="0.5" value="8" style="width:60px">
          止损% <input id="ed-sl" type="number" step="0.5" value="5" style="width:60px">
          持有天数 <input id="ed-hold" type="number" value="5" style="width:60px"></p>
        <div style="margin-top:10px;">
          <button id="btn-save-strategy" onclick="saveStrategy()">保存</button>
          <button onclick="closeEditor()">取消</button>
        </div>
        <div id="ed-errors" class="error"></div>
      </div>
```

- [x] **Step 4: 实现 JS（app.js 文件尾追加函数组 + loadStrategies 模板增强）**

文件尾追加：

```js
// ---------- 策略编辑器 ----------
let _factorCache = null;

async function editorFactors() {
  if (_factorCache) return _factorCache;
  const data = await api("/api/factors");
  _factorCache = data.factors || [];
  return _factorCache;
}

async function openEditor(template) {
  document.getElementById("strategy-editor").style.display = "block";
  document.getElementById("ed-errors").textContent = "";
  const factors = await editorFactors();
  const gateBox = document.getElementById("ed-gate");
  gateBox.innerHTML = factors.filter(f => f.category === "node").map(f =>
    `<label style="margin-right:10px;"><input type="checkbox" data-gate="${f.id}"` +
    `${template && (template.gate_factors || []).includes(f.id) ? " checked" : ""}> ${f.id} ${f.name || ""}</label>`).join("");
  const box = document.getElementById("ed-models");
  box.innerHTML = "";
  const models = (template && template.models) || [null];
  for (const m of models) addEditorModel(m, factors);
  document.getElementById("ed-name").value = "";
  if (template) {
    document.getElementById("ed-gate-threshold").value = template.gate_threshold ?? 1;
    document.getElementById("ed-min-model").value = template.candidate_min_model ?? 3;
    document.getElementById("ed-tp").value = ((template.sell || {}).take_profit_pct ?? 0.08) * 100;
    document.getElementById("ed-sl").value = ((template.sell || {}).stop_loss_pct ?? 0.05) * 100;
    document.getElementById("ed-hold").value = (template.sell || {}).max_hold_days ?? 5;
  }
}

function addEditorModel(m, factors) {
  factors = factors || [];
  const box = document.getElementById("ed-models");
  const div = document.createElement("div");
  div.className = "ed-model";
  const cats = {};
  factors.forEach(f => (cats[f.category] = cats[f.category] || []).push(f));
  div.innerHTML =
    `<h4>评分模型 <input class="ed-mname" placeholder="模型名" value="${m ? (m.name || "") : ""}">` +
    ` 权重 <input class="ed-mweight" type="number" step="0.05" value="${m ? m.weight : 1.0}" style="width:60px"></h4>` +
    Object.keys(cats).sort().map(cat =>
      `<div><b>${cat}</b> ` + cats[cat].map(f =>
        `<label style="margin-right:8px;" title="${(f.description || "").replace(/"/g, "&quot;")}">` +
        `<input type="checkbox" data-fid="${f.id}"${m && (m.factors || []).includes(f.id) ? " checked" : ""}> ${f.id}</label>`).join("") + `</div>`).join("<br>");
  box.appendChild(div);
}

async function copyToEditor(sid) {
  const data = await api("/api/strategy/" + sid);
  if (!data.ok) { alert(data.error); return; }
  const s = data.strategy;
  await openEditor({
    models: (s.scoring_models || []).map(m => ({
      name: m.name, weight: m.weight, factors: m.factors, weights: m.weights})),
    gate_factors: (s.market_gate || {}).factors || [],
    gate_threshold: (s.market_gate || {}).threshold,
    candidate_min_model: (s.filters || {}).candidate_min_model,
    sell: s.sell_rules || {},
  });
  alert("已载入「" + (s.name || sid) + "」为底稿, 改名后保存为新策略");
}

function collectEditorPayload() {
  const models = [];
  document.querySelectorAll("#ed-models .ed-model").forEach(div => {
    const fs = [...div.querySelectorAll("input[data-fid]:checked")].map(i => i.dataset.fid);
    if (!fs.length) return;
    models.push({id: "", name: div.querySelector(".ed-mname").value || ("模型" + (models.length + 1)),
                 weight: parseFloat(div.querySelector(".ed-mweight").value) || 1.0,
                 factors: fs, weights: fs.map(() => 1.0)});
  });
  const gate = [...document.querySelectorAll("input[data-gate]:checked")].map(i => i.dataset.gate);
  return {name: document.getElementById("ed-name").value.trim(),
          models, gate_factors: gate,
          gate_threshold: parseInt(document.getElementById("ed-gate-threshold").value) || 0,
          candidate_min_model: parseInt(document.getElementById("ed-min-model").value) || 3,
          sell: {take_profit_pct: (parseFloat(document.getElementById("ed-tp").value) || 8) / 100,
                 stop_loss_pct: (parseFloat(document.getElementById("ed-sl").value) || 5) / 100,
                 max_hold_days: parseInt(document.getElementById("ed-hold").value) || 5}};
}

async function saveStrategy() {
  const btn = document.getElementById("btn-save-strategy");
  const errBox = document.getElementById("ed-errors");
  btn.disabled = true;
  errBox.textContent = "";
  try {
    const res = await api("/api/strategies/create", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(collectEditorPayload())});
    if (!res.ok) {
      errBox.innerHTML = (res.errors || [res.error || "未知错误"]).map(e => `<div>${e}</div>`).join("");
      return;
    }
    closeEditor();
    await loadStrategies();
  } catch (e) { errBox.textContent = "保存失败: " + e; }
  finally { btn.disabled = false; }
}

function closeEditor() {
  document.getElementById("strategy-editor").style.display = "none";
}

async function activateStrategy(sid, silent) {
  const res = await api(`/api/strategies/${sid}/activate`, {method: "POST"});
  if (!res.ok && !silent) alert(res.error || "切换失败");
  if (res.ok) await loadStrategies();
}
```

`loadStrategies` 重构（替换整个函数——改列表模板 + 默认徽标；`detail` 死变量顺带删除）：

```js
async function loadStrategies() {
  const list = document.getElementById("strategy-list");
  try {
    const data = await api("/api/strategies");
    const strategies = data.strategies || [];
    const activeId = data.active;
    const badge = document.getElementById("active-badge");
    if (badge) {
      const a = strategies.find(s => s.id === activeId);
      badge.textContent = a ? `当前默认: ${a.name || activeId}` : "";
    }
    list.innerHTML = strategies.map(s => `
      <div class="strategy-item" onclick="showStrategy('${s.id}')">
        <div class="strategy-item-name">${s.id === activeId ? "★ " : ""}${s.name || s.id}</div>
        <div class="strategy-item-id">${s.id}</div>
        <div style="margin-top:6px;">
          <button onclick="event.stopPropagation(); activateStrategy('${s.id}')"${s.id === activeId ? " disabled" : ""}>设为默认</button>
          <button onclick="event.stopPropagation(); copyToEditor('${s.id}')">复制为底稿</button>
        </div>
      </div>`).join("") || `<div class="hint">暂无策略</div>`;
    if (strategies.length) showStrategy(strategies[0].id);
  } catch (e) {
    list.innerHTML = `<div class="error">策略加载失败: ${e}</div>`;
  }
}
```

- [x] **Step 5: 跑测试确认通过 + 全 app 回归**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt106`
Expected: 全 passed（存量+新）

- [x] **Step 6: Commit**

```bash
git add prism_web/templates/index.html prism_web/static/app.js prism_web/tests/test_app.py
git commit -m "feat(prism_web): 策略编辑面板 — 新建/复制底稿/分组勾选/设为默认"
```

---

### Task 6: 全量回归 + 真实冒烟 + 收尾

**Files:**
- Modify: `.superpowers/sdd/progress.md`（台账追加）
- Modify: `docs/superpowers/plans/2026-09-01-strategy-editor.md`（勾选全部步骤）

**Interfaces:**
- Consumes: Task 1-5 全部
- Produces: 全量回归绿 + 真实冒烟结果 + 用户使用说明更新

- [x] **Step 1: 全量回归**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt107`
Expected: 全 passed（基线 367 + 新增 ~19 = 约 386）

- [x] **Step 2: 真实冒烟（用户在场；**注意守护进程在跑，改指针会热生效——冒烟后必须恢复 v04 默认**）**

1. `python -m prism.paper --summary` → 记录当前默认（应 first_board_v04）
2. 手动构造 payload 调 create（curl 或浏览器面板）→ 得到 custom_xxx.json
3. `POST /api/strategies/custom_xxx/activate` → 指针切换
4. `python -m prism.paper --summary` → 确认模拟盘策略热重载（新实例读指针）
5. **恢复**：`POST /api/strategies/first_board_v04/activate` + 删除 `prism/strategies/custom_xxx.json`（冒烟产物不入库）+ 确认指针回落 v04
6. 浏览器打开"策略"页：新建面板、设为默认按钮、★徽标可见（用户视觉验收）

- [x] **Step 3: 台账与勾选 + Commit**

```bash
git add docs/superpowers/plans/2026-09-01-strategy-editor.md
git commit -m "docs(plan): 策略编辑器实施完成(6任务/全量回归/真实冒烟)"
```
（台账 .superpowers 为 gitignored，仅本地追加记录）

- [x] **Step 4: 向用户交付**

报告要点：网页哪里用（策略页 ➕ 新建/设为默认/复制底稿）、保存即出现在列表、默认切换三处生效且模拟盘不重启热生效、披露限制（因子参数不可改/组合有效性需自行回测）、冒烟结果