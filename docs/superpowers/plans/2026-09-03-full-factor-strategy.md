# 全因子四层策略 + 因子库清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 删 M1-M5 + S1/S5/S7 因子与 5 个断链策略，新建 36 因子四层策略 full_factor_v1 并设为模拟盘主力，"新因子默认入策略"惯例写入 MEMORY.md。

**Architecture:** 纯数据层变更——删因子文件与策略 JSON、手写新策略 JSON（`engine.load_strategy` 试载验证）、指针经 `engine.set_active_strategy` 切换（不手工改 .active.json）；测试面同步（4 个测试文件有死引用）。

**Tech Stack:** Python 3.12 / pytest 全离线 / 现有 engine.load_strategy + set_active_strategy（零新代码路径）。

## Global Constraints

- **删除清单（因子 8）**：`prism/factors/` 下 `factor_m1_momentum.py`、`factor_m2_volume_surge.py`、`factor_m3_ma_bullish.py`、`factor_m4_breakout.py`、`factor_m5_low_volatility.py`、`factor_s1_manual.py`、`factor_s5_financing.py`、`factor_s7_manual.py`
- **删除清单（策略 5）**：`prism/strategies/` 下 `default.json`、`five_factor.json`、`sector_momentum.json`、`sector_momentum_v4_base.json`、`sector_momentum_v5a_gateonly.json`
- **新策略 JSON**：逐字用 spec §3 的 full_factor_v1 定义（36 因子四层，权重 0.60/0.25/0.15，cap 9.05，gate N1-N8 threshold 3，sell 0.08/0.05/5）——见 Task F2 Step 1 全文
- **保留**：`first_board_v04.json`、`first_board_v03.json` 及其余因子文件（36 = N1-N8 8 + F1-F9 9 + Y1-Y8 8 + M6/M7/S2/S3/S4/S6/SEC1-4/SEC6 11）
- **测试命令**：`$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt123`
- **守护在跑**（pwsh-9，策略=default）：删 default.json 后指针暂缺 → 引擎回落 first_board_v04（既有逻辑，安全）；Task F2 激活后热重载到新策略。守护日志可验证
- **引用面已勘察**（写死的死引用清单，实现者逐个处置，不得跳过）：
  - `prism/tests/test_factor_migration.py`：L29 因子清单含 S1-S7、L339-347 S1/S5/S7 手填测试组、L616 S1 分类断言、L621-624 test_default_strategy_loads
  - `prism/tests/test_first_board_strategies.py`：L15-26 default 内联快照常量 + L63-64 test_default_snapshot_unchanged
  - `prism/tests/test_m67_strategy.py`：L97-106 test_sector_momentum_strategy_loads（sector_momentum.json 已删；M6/M7 因子自身测试保留）
  - `prism_web/tests/test_app.py`：L51/55/59 strategies 列表/详情断言 default、L169/372 backtest strategy=default、L225/295/338 screen strategy=default、L241-247 与 L326-349 手填 S1 相关用例
  - **不受影响**（勿动）：test_backtest*/test_engine/test_trader/test_backtest_pool_ctx/test_live_mkt_injection 里的 "m1"/"M1" 是**内联模型 id**（因子为夹具内注册的 A1/AS1 等），与因子 M1 无关
- **ponytail 约束**：零新代码路径（删除+数据文件+测试同步）；不建"因子自动入策略"脚本（YAGNI，MEMORY.md 惯例约束）；不简化掉 grep 死引用清零验证
- git 只本地 commit，push 前问用户

---

### Task F1: 删除因子与断链策略 + 测试面同步

**Files:**
- Delete: 上列 8 因子文件 + 5 策略文件
- Modify: `prism/tests/test_factor_migration.py`、`prism/tests/test_first_board_strategies.py`、`prism/tests/test_m67_strategy.py`、`prism_web/tests/test_app.py`
- Test: 全量回归（基线 423，删除死测试后以实际为准，必须全绿）

- [ ] **Step 1: 写"删除后状态"失败测试（先落测试再删，TDD）**

`prism/tests/test_factor_migration.py` 末尾追加（文件头部核对有 `from prism import registry as reg` 与 `from pathlib import Path`，缺则补）：

```python
def test_removed_factors_absent():
    """2026-09-03 清理: M1-M5 与 S1/S5/S7 已删除, 不再注册。"""
    for fid in ("M1", "M2", "M3", "M4", "M5", "S1", "S5", "S7"):
        assert fid not in reg.FACTORS, "%s 应已删除" % fid
    assert len(reg.FACTORS) == 36


def test_full_factor_v1_loads():
    """验收: full_factor_v1 四层 36 因子可加载且因子全部注册。"""
    from prism.engine import load_strategy
    s = load_strategy(Path(__file__).parent.parent / "strategies"
                      / "full_factor_v1.json")
    assert s["id"] == "full_factor_v1"
    fids = [f for m in s["scoring_models"] for f in m["factors"]]
    gate = s["market_gate"]["factors"]
    assert len(fids) == 28            # 9 首板 + 8 妖股 + 11 势能板块
    assert len(gate) == 8             # N1-N8
    assert len(set(fids) | set(gate)) == 36   # 评分与门控无重叠, 合计 36
    assert all(f in reg.FACTORS for f in set(fids) | set(gate))
```

- [ ] **Step 2: 跑新测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_factor_migration.py::test_removed_factors_absent prism/tests/test_factor_migration.py::test_full_factor_v1_loads -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt123`
Expected: FAIL（因子仍在注册表 44 个 / full_factor_v1.json 不存在）

- [ ] **Step 3: 执行删除 + 死引用清理**

```bash
git rm prism/factors/factor_m1_momentum.py prism/factors/factor_m2_volume_surge.py prism/factors/factor_m3_ma_bullish.py prism/factors/factor_m4_breakout.py prism/factors/factor_m5_low_volatility.py prism/factors/factor_s1_manual.py prism/factors/factor_s5_financing.py prism/factors/factor_s7_manual.py
git rm prism/strategies/default.json prism/strategies/five_factor.json prism/strategies/sector_momentum.json prism/strategies/sector_momentum_v4_base.json prism/strategies/sector_momentum_v5a_gateonly.json
```

死引用清理（逐个处置，全局约束里的勘察清单）：
1. `test_factor_migration.py`：删 L339-347 的 S1/S5/S7 手填参数化测试组（两个 test 函数）；删 L616 `assert reg.FACTORS["S1"]...` 行；L29 的因子 id 清单里去掉 `"S1","S5","S7"`（该清单是"迁移 24 因子"的存量核对——按现存活因子语义改为去掉三个手填 id）；`test_default_strategy_loads`（L621-624）整段删除（Task F2 的 test_full_factor_v1_loads 取代其验收位）
2. `test_first_board_strategies.py`：删 L15-26 的 default 内联常量与 L63 起的 `test_default_snapshot_unchanged`；模块 docstring（L2）改为 `"""first_board_v03/v04 策略文件测试。全离线。"""`
3. `test_m67_strategy.py`：删 `test_sector_momentum_strategy_loads`（L97-106）——M6/M7 因子函数自身的测试**保留**
4. `test_app.py`：L51 `assert any(s["id"] == "default" ...)` → `assert any(s["id"] == "full_factor_v1" ...)`；L55/59 `/api/strategy/default` → `/api/strategy/full_factor_v1` 及断言 id 同步；L169/372 `strategy=default` → `strategy=first_board_v04`（回测路径用 v04，测试环境已验证可跑）；L225/295/338 `strategy=default` → `strategy=first_board_v04`；L241-247 手填端点用例：`/api/stock/002859.SZ/manual` post S1 仍期待 400（S1 不在注册表 → 同样拒），**跑完核对仍绿**；L326-349 用例：payload 里 `"S1": 1` 与 `am["S1"] == "auto"` 断言——S1 因子已不存在，改为选一个现存非手填因子（如 `"F1": 1`，断言 `am["F1"] == "auto"`），语义不变（非手填因子归 auto）
5. 若实现中发现勘察清单外的死引用（grep `default\.json|five_factor|sector_momentum` 与 `"(M1|S1|S5|S7)"` 于 tests/ 与源码），同规则处置并在报告注明

- [ ] **Step 4: 跑测试确认通过（因子清理后）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt123`
Expected: test_removed_factors_absent / test_full_factor_v1_loads 仍 FAIL（full_factor_v1.json 未建——Task F2），**其余全绿**；若 full_factor_v1 缺文件导致 load 报错其他测试连带红，核对只有那 2 个新测试红

- [ ] **Step 5: Commit（分两笔：删除一笔、测试清理与新增一笔）**

```bash
git add -A prism/factors prism/strategies
git commit -m "chore(prism): 删除M1-M5/S1/S5/S7因子与5个断链策略(全因子策略前置)"
git add prism/tests prism_web/tests
git commit -m "test(prism): 因子清理测试面同步 — 死引用清零+36因子/full_factor_v1验收测试"
```

---

### Task F2: 新策略 full_factor_v1.json + 指针切换 + MEMORY.md 惯例

**Files:**
- Create: `prism/strategies/full_factor_v1.json`
- Modify: `MEMORY.md`（现状小节 + 新增惯例小节）
- Test: Task F1 的 test_full_factor_v1_loads 转绿

**Interfaces:**
- Consumes: engine `load_strategy(path)`（现有）；`engine.set_active_strategy(sid)`（现有）；engine 回落逻辑（指针所指文件缺失 → first_board_v04）
- Produces: 策略文件 `full_factor_v1.json`（守护/回测/选股经指针消费）；MEMORY.md 惯例条目

- [ ] **Step 1: 创建策略文件（逐字，spec §3）**

`prism/strategies/full_factor_v1.json`：

```json
{
  "id": "full_factor_v1",
  "name": "全因子四层 v1",
  "description": "36因子四层框架(环境门控+首板+妖股+势能板块)。惯例: 今后新增因子默认归入对应层组合进本策略(特殊要求除外)。2026-09-03 起为模拟盘主力策略。",
  "market_gate": {"model": "node", "threshold": 3,
                  "factors": ["N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8"]},
  "scoring_models": [
    {"id": "first_board", "name": "首板", "weight": 0.60,
     "factors": ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9"]},
    {"id": "monster", "name": "妖股", "weight": 0.25,
     "factors": ["Y1", "Y2", "Y3", "Y4", "Y5", "Y6", "Y7", "Y8"]},
    {"id": "momentum", "name": "势能板块", "weight": 0.15,
     "factors": ["M6", "M7", "S2", "S3", "S4", "S6",
                 "SEC1", "SEC2", "SEC3", "SEC4", "SEC6"]}
  ],
  "composite": {"mode": "top3_weighted", "weights": [0.60, 0.25, 0.15],
                "cap": 9.05},
  "filters": {"candidate_min_model": 3, "environment_threshold": 3},
  "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                 "max_hold_days": 5}
}
```

- [ ] **Step 2: 跑验收测试转绿**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests/test_factor_migration.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt123`
Expected: 全 passed（含 test_full_factor_v1_loads）

- [ ] **Step 3: 指针切换（运行时动作，经引擎 API 不手工改文件）**

```bash
python -c "import sys; sys.path.insert(0, r'D:\cc-joesph'); from prism.engine import set_active_strategy; ok, err = set_active_strategy('full_factor_v1'); print(ok, err)"
```
Expected: `True None`（或该函数实际返回签名——实现者先 `grep -n "def set_active_strategy" prism/engine.py` 核对签名与返回值，按实际调整调用；若返回布尔直接打印即可）

验证指针：`Get-Content prism/strategies/.active.json` → `{"id": "full_factor_v1", ...}`；守护日志（job_output pwsh-9 尾部）出现策略 id 变化或下次访问生效（热重载无报错）。

- [ ] **Step 4: MEMORY.md 更新**

`MEMORY.md` 三处（精确编辑）：
1. 「项目现状」的模拟盘行替换为：`**模拟盘**：100 万 paper trading；守护 python -m prism.paper_daemon（后台作业 pwsh-9）；当前激活策略 = **full_factor_v1**（2026-09-03 起主力；此前 default 已删除——净值归因切换点 09-03，账本不清零）`
2. 「项目现状」新增一行：`**全因子四层策略**（09-03）：36 因子（门控 N1-N8 / 首板 F1-F9 w0.60 / 妖股 Y1-Y8 w0.25 / 势能板块 M6·M7·S2-S6·SEC1-4·SEC6 w0.15）；M1-M5 与 S1/S5/S7 已删；策略库仅存 v04/v03/full_factor_v1`
3. 新增小节（放在「关键决策史」前）：

```markdown
## 因子管理惯例（2026-09-03 起）

- **新增因子默认组合进 full_factor_v1**：按四层归位——环境/情绪类→market_gate（N 系）；首板确认类→first_board 模型（F 系）；妖股类→monster 模型（Y 系）；形态/板块类→momentum 模型（M/S/SEC 系）。加入策略 JSON 对应层 factors 数组并**重算 composite.cap**（Σ 模型权重×该模型因子数）。特殊要求（如仅供实验/仅供回测）才不入，需用户明说
```

- [ ] **Step 5: Commit**

```bash
git add prism/strategies/full_factor_v1.json MEMORY.md
git commit -m "feat(prism): 全因子四层策略full_factor_v1上默认 + 新因子入策略惯例"
```

---

### Task F3: 全量回归 + 守护实测 + 收尾

**Files:**
- Modify: `docs/superpowers/plans/2026-09-03-full-factor-strategy.md`（勾选）
- Test: 全量回归

- [ ] **Step 1: 全量回归**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt123`
Expected: 全绿（数量随删除死测试变化，以实际为准）

- [ ] **Step 2: 死引用清零验证**

Run: `git grep -n "five_factor\|sector_momentum" -- prism prism_web` 与 `git grep -n "\"M1\"\|\"S1\"\|\"S5\"\|\"S7\"\|\"M2\"\|\"M3\"\|\"M4\"\|\"M5\"" -- prism prism_web`
Expected: 仅剩测试内联模型 id（"m1" 小写与 A1/AS1 夹具组合）与历史文档；因子引用清零。残留即回 Task F1 Step 3 补清

- [ ] **Step 3: 守护实测（模拟盘换引擎第一现场）**

1. `job_output pwsh-9` 尾部：守护无报错；下次策略访问后日志显示新策略 id（或用 `python -m prism.paper --summary` 旁证账本健康）
2. 网页 5000 端口 F5：策略列表见 full_factor_v1 带 ★；模拟盘 tab 正常
3. `python -m prism.paper --summary` → exists true、无异常

- [ ] **Step 4: 台账 + 勾选 + Commit**

```bash
git add docs/superpowers/plans/2026-09-03-full-factor-strategy.md
git commit -m "docs(plan): 全因子四层策略实施完成(3任务/全量回归/守护实测)"
```
台账 `.superpowers/sdd/progress.md` 追加全因子策略项目收尾段。
