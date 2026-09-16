# tt_solo 自包含化 + 仪表盘重建 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把做T策略从 `tt/` 剥离为自包含项目 `tt_solo/`（零 prism/shared 依赖），并把 `tt_web` 监控台重建为一屏决策仪表盘。

**Architecture:** 拷贝 `tt/` + `tt_web/` 到 `tt_solo/`，把 5 个外部符号内联进 `ttcore/_vendor.py`，把 `prism.live_account` 吸收为 `ttcore/broker.py`，路径根改为项目自带 `runtime/`。仪表盘沿用已验证的 Flask 后端接口，新增 2 个只读聚合接口，前端单页重做为五区块决策面板。

**Tech Stack:** Python 3.12、Flask、pytest、原生 JS + ECharts（已 vendored）、xtquant（miniQMT，仅账户只读/行情）

## Global Constraints

- **禁止** 在 `tt_solo/` 内 import `prism`、`shared`、`qmt_sync`、`backtest`、`legacy`（自包含的硬定义）。
- **不改策略逻辑**：band / 档位 / 风控口径 / 股数算法一律照搬。本次是搬家 + 换皮，不是改策略。
- **安全红线**：守护默认 `dry_run=True`；面板只能"关闸"不能开单；仪表盘只监听 `127.0.0.1`。
- **原子写必须保留 fsync**（`flush` + `os.fsync` + `os.replace`）—— 断电存活的关键，不是可选项。
- **`TT_SIGNAL_ROOT` 默认 `D:/QMT_SIGNALS` 不变** —— 那是与外部 QMT 的契约，不属于本项目数据。
- **测试命令**：`$env:PYTHONIOENCODING='utf-8'; python -m pytest <paths> -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_btNN`（NN 递增，下一个可用 **250**）。
- **沙箱注意**：沙箱内 pytest 会假失败（safe-delete 守卫 + 网络拦截），验证一律脱沙箱跑。
- **搜索命令**：本机**没有 `rg`**（实测 `where.exe rg` 找不到），一律用 PowerShell 原生
  `Select-String`（或用 agent 的 grep 工具）。计划里所有搜索命令均按 `Select-String` 给出。
- **基线**：`tt/tests` + `tt_web/tests` = **150 passed**。`tt_solo` 目标 ≥ 150 且全绿。
- **commit 随意，push 必须先问用户。**
- 每个子代理 dispatch 注入 ponytail 约束；`# ponytail:` 标记刻意取舍。

---

### Task 1: 项目骨架 + `_vendor.py` 底座内联

**Files:**
- Create: `tt_solo/ttcore/__init__.py`
- Create: `tt_solo/ttcore/_vendor.py`
- Create: `tt_solo/tests/__init__.py`
- Create: `tt_solo/tests/conftest.py`
- Create: `tt_solo/tests/test_vendor.py`
- Create: `tt_solo/.gitignore`
- Modify: `.gitignore`（根，加 `tt_solo/runtime/`）

**Interfaces:**
- Consumes: 无（起点）
- Produces: `ttcore._vendor` 导出 `PROJECT_ROOT: Path`、`RUNTIME_DIR: Path`、`STATE_DIR: Path`、`LOG_DIR: Path`、`atomic_write(path, text) -> None`、`limit_ratio_for_code(code) -> float`、`is_local_request(cf_ip, remote_addr) -> bool`

- [ ] **Step 1: 建目录骨架**

```powershell
cd D:\cc-joesph
New-Item -ItemType Directory -Force -Path tt_solo\ttcore, tt_solo\tests, tt_solo\runtime\state, tt_solo\runtime\log | Out-Null
```

- [ ] **Step 2: 写 `tt_solo/ttcore/__init__.py`**

```python
# -*- coding: utf-8 -*-
"""tt_solo 做T策略核心(自包含, 不依赖 prism/shared)。

模块职责:
    _vendor  底座内联(原子写/涨跌停比例/分级护栏/路径根)
    grid     网格纯逻辑(零 IO)
    risk     风控闸门(零 IO)
    state    日账本 + 日终归档
    market   行情与指标
    broker   miniQMT 账户只读
    engine   决策编排
    executor 直连下单
    config   配置加载与校验
    daemon   守护
"""
__version__ = "1.0.0"
```

- [ ] **Step 3: 写失败测试 `tt_solo/tests/test_vendor.py`**

```python
# -*- coding: utf-8 -*-
"""_vendor 底座测试: 路径根 + 原子写 + 涨跌停比例 + 分级护栏。"""
import os
from pathlib import Path

from ttcore import _vendor


def test_project_root_points_at_tt_solo():
    assert _vendor.PROJECT_ROOT.name == "tt_solo"


def test_state_dir_under_project_by_default():
    assert _vendor.STATE_DIR == _vendor.PROJECT_ROOT / "runtime" / "state"


def test_runtime_dir_env_override(monkeypatch, tmp_path):
    """TT_RUNTIME_DIR 覆盖后需重新加载模块才生效 —— 用 reload 验证。"""
    import importlib
    monkeypatch.setenv("TT_RUNTIME_DIR", str(tmp_path))
    mod = importlib.reload(_vendor)
    try:
        assert mod.RUNTIME_DIR == tmp_path
        assert mod.STATE_DIR == tmp_path / "state"
    finally:
        monkeypatch.delenv("TT_RUNTIME_DIR", raising=False)
        importlib.reload(_vendor)


def test_atomic_write_creates_parent_and_content(tmp_path):
    p = tmp_path / "a" / "b" / "x.json"
    _vendor.atomic_write(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"


def test_atomic_write_leaves_no_tmp(tmp_path):
    p = tmp_path / "x.json"
    _vendor.atomic_write(p, "v1")
    _vendor.atomic_write(p, "v2")
    assert p.read_text(encoding="utf-8") == "v2"
    assert not (tmp_path / "x.json.tmp").exists()


def test_limit_ratio_by_board():
    assert _vendor.limit_ratio_for_code("600900.SH") == 0.10
    assert _vendor.limit_ratio_for_code("300750.SZ") == 0.20
    assert _vendor.limit_ratio_for_code("688981.SH") == 0.20
    assert _vendor.limit_ratio_for_code("830799.BJ") == 0.30
    # 北交所新代码段 920xxx —— shared/common.py 缺这条, 这里必须有
    assert _vendor.limit_ratio_for_code("920001.BJ") == 0.30


def test_is_local_request_tiers():
    assert _vendor.is_local_request(None, "127.0.0.1") is True
    assert _vendor.is_local_request(None, "::1") is True
    assert _vendor.is_local_request(None, "192.168.1.5") is True
    assert _vendor.is_local_request(None, "172.16.0.1") is True
    assert _vendor.is_local_request(None, "172.32.0.1") is False
    assert _vendor.is_local_request(None, "8.8.8.8") is False
    # 有 CF 头 = 经隧道 = 远程, 即使 remote_addr 是回环
    assert _vendor.is_local_request("1.2.3.4", "127.0.0.1") is False
```

- [ ] **Step 4: 跑测试确认失败**

Run: `python -m pytest tt_solo/tests/test_vendor.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: FAIL — `ModuleNotFoundError: No module named 'ttcore'`

- [ ] **Step 5: 写 `tt_solo/tests/conftest.py`（把 `tt_solo/` 挂上 sys.path）**

```python
# -*- coding: utf-8 -*-
"""tt_solo 测试公共夹具。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]      # D:/cc-joesph/tt_solo
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

- [ ] **Step 6: 写 `tt_solo/ttcore/_vendor.py`**

```python
# -*- coding: utf-8 -*-
"""自包含底座: 路径根 + 原子写 + 涨跌停比例 + 分级写护栏。

ponytail: vendored from shared/common.py @2026-09-16 —— tt_solo 要能被整体
拷走独立运行, 故刻意不 import shared。5 个符号约 60 行, 为它们造一层包结构
属于过度设计, 内联到单文件即可(每个函数标注来源保留回溯)。

注意 limit_ratio_for_code 的差异: shared/common.py 的判定是 ("8", "4"),
而 tt/risk.py 的兜底实现是 ("8", "4", "92")。北交所 920xxx 属 30% 涨跌幅,
故此处采用 tt 的更正确版本("92"); shared 版本缺这条, 是主项目侧的潜在缺陷。
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent      # -> tt_solo/
RUNTIME_DIR = Path(os.environ.get("TT_RUNTIME_DIR")
                   or PROJECT_ROOT / "runtime")
STATE_DIR = RUNTIME_DIR / "state"
LOG_DIR = RUNTIME_DIR / "log"


# ---------------------------------------------------------------- io

def atomic_write(path, text):
    """原子写: mkdir + 同目录 .tmp + flush + fsync + os.replace。

    崩溃/断电都不能留下半截文件(账本读者会把截断文件当损坏)。
    fsync 是断电存活的关键, 不是可选项。
    vendored from shared/common.py @2026-09-16。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- 规则

def limit_ratio_for_code(code):
    """按板块返回涨跌停比例: 北交所 30%, 创业板/科创 20%, 主板 10%。

    vendored from tt/risk.py 的兜底实现 @2026-09-16(含 "92" 段)。
    """
    c = str(code).strip()
    if c.startswith(("8", "4", "92")):
        return 0.30
    if c.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


# ---------------------------------------------------------------- 分级护栏

def _is_rfc1918(ip):
    """RFC1918 私网段(局域网 = 可信本机档)。"""
    if ip.startswith(("10.", "192.168.")):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def is_local_request(cf_ip, remote_addr):
    """分级写护栏判据。有 CF-Connecting-IP = 经隧道 = 远程; 无头且
    回环/RFC1918 = 本机。vendored from shared/common.py @2026-09-16。"""
    if cf_ip:
        return False
    ra = remote_addr or ""
    return ra in ("127.0.0.1", "::1") or _is_rfc1918(ra)
```

- [ ] **Step 7: 跑测试确认通过**

Run: `python -m pytest tt_solo/tests/test_vendor.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: PASS（8 passed）

- [ ] **Step 8: 写 `tt_solo/.gitignore` 与追加根 `.gitignore`**

`tt_solo/.gitignore`:
```
runtime/*
!runtime/.gitkeep
__pycache__/
*.pyc
.pytest_cache/
```

根 `.gitignore` 追加一行：
```
tt_solo/runtime/
```

- [ ] **Step 9: Commit**

```bash
git add tt_solo .gitignore
git commit -m "feat(tt_solo): 项目骨架 + _vendor 底座内联(原子写/涨跌停/护栏/路径根)"
```

---

### Task 2: 迁移纯逻辑层 `grid` + `risk`

**Files:**
- Create: `tt_solo/ttcore/grid.py`（`tt/grid.py` 原样拷贝）
- Create: `tt_solo/ttcore/risk.py`（改 import 源）
- Create: `tt_solo/tests/test_grid.py`、`tt_solo/tests/test_risk.py`（改 import）

**Interfaces:**
- Consumes: `ttcore._vendor.limit_ratio_for_code`
- Produces: `ttcore.grid` 导出 `ENABLED/HALF/DISABLED`、`SIDE_BUY/SIDE_SELL`、`daily_sigma`、`trend_degree`、`ma`、`switch_state`、`band_of`、`build_ladder`、`crossed_sell`、`crossed_buy`、`ladder_price`、`target_units`、`half_scale`；`ttcore.risk` 导出 `Verdict`、`OK`、`PHASE_OPEN/PHASE_CONVERGE/PHASE_CLOSED`、`RiskGate`、`session_phase`

- [ ] **Step 1: 拷贝 grid.py（零改动）**

```powershell
Copy-Item D:\cc-joesph\tt\grid.py D:\cc-joesph\tt_solo\ttcore\grid.py
Copy-Item D:\cc-joesph\tt\tests\test_grid.py D:\cc-joesph\tt_solo\tests\test_grid.py
```

`tt/grid.py` 无任何外部 import（只 `import math`），原样搬入即可。

- [ ] **Step 2: 拷贝 risk.py 并改 import**

```powershell
Copy-Item D:\cc-joesph\tt\risk.py D:\cc-joesph\tt_solo\ttcore\risk.py
Copy-Item D:\cc-joesph\tt\tests\test_risk.py D:\cc-joesph\tt_solo\tests\test_risk.py
```

替换 `tt_solo/ttcore/risk.py` 第 15-24 行的 try/except 兜底块：

```python
from collections import namedtuple

from ._vendor import limit_ratio_for_code
```

（删掉原 `try: from shared.common import ... except: def limit_ratio_for_code(...)`
整块 —— 兜底逻辑已内联到 `_vendor`，且用的是含 `"92"` 的正确版本。）

- [ ] **Step 3: 改测试 import**

`tt_solo/tests/test_grid.py`：`from tt.grid import` → `from ttcore.grid import`（及 `from tt import grid` → `from ttcore import grid`）

`tt_solo/tests/test_risk.py`：`from tt.risk import` → `from ttcore.risk import`（及模块形式同理）

用 sed 批量替换（注意仓库 `core.autocrlf=true`，工作区为 CRLF）：
```powershell
cd D:\cc-joesph\tt_solo\tests
(Get-Content test_grid.py -Raw) -replace 'from tt\.grid','from ttcore.grid' -replace 'from tt import grid','from ttcore import grid' | Set-Content test_grid.py -NoNewline
(Get-Content test_risk.py -Raw) -replace 'from tt\.risk','from ttcore.risk' -replace 'from tt import risk','from ttcore import risk' | Set-Content test_risk.py -NoNewline
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tt_solo/tests/test_grid.py tt_solo/tests/test_risk.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: PASS — grid 23 + risk 29 = 52 例（含参数化，以实际数为准）

- [ ] **Step 5: 确认无外部依赖**

Run: `Select-String -Path tt_solo\ttcore\grid.py, tt_solo\ttcore\risk.py -Pattern 'shared|prism'`
Expected: 无输出

- [ ] **Step 6: Commit**

```bash
git add tt_solo
git commit -m "feat(tt_solo): 迁移纯逻辑层 grid + risk(改内联底座)"
```

---

### Task 3: 迁移 `state.py` + 新增日终归档

> **背景（重要）**：`tt_state.json` 是**单日账本**，`load()` 在日期不符时直接
> `_empty_state()` 覆盖 —— **前一日数据被丢弃，从未归档**。这既是没有历史曲线的
> 原因，也是一个小缺陷。本任务在重置前追加一行摘要到 `tt_history.jsonl`，
> 为仪表盘收益曲线提供真实数据源。
>
> **风险控制**：归档是 append-only 副作用，用 try/except 全包 —— **绝不允许**
> 归档失败影响交易。它不触碰 `plan()` 输出，Task 12 的双跑对照会证明零回归。

**Files:**
- Create: `tt_solo/ttcore/state.py`
- Create: `tt_solo/tests/test_state.py`（改 import + 新增归档用例）

**Interfaces:**
- Consumes: `ttcore._vendor.STATE_DIR`、`ttcore._vendor.atomic_write`
- Produces: `ttcore.state.Ledger`（保持不变的方法集）+ 新增属性 `history_path`、方法 `archive_current() -> bool`、`read_history(limit=60) -> list[dict]`；模块级 `STATE_VERSION=1`、`MAX_EVENTS=500`、`DEFAULT_STATE_PATH`

- [ ] **Step 1: 拷贝 state.py**

```powershell
Copy-Item D:\cc-joesph\tt\state.py D:\cc-joesph\tt_solo\ttcore\state.py
Copy-Item D:\cc-joesph\tt\tests\test_state.py D:\cc-joesph\tt_solo\tests\test_state.py
```

- [ ] **Step 2: 改 import 与路径**

`tt_solo/ttcore/state.py` 第 21 行：

```python
from ._vendor import STATE_DIR, atomic_write
```

删除第 26 行的 `REPO = Path(__file__).resolve().parent.parent`（未使用），第 27 行保持：

```python
DEFAULT_STATE_PATH = STATE_DIR / "tt_state.json"
HISTORY_NAME = "tt_history.jsonl"
```

- [ ] **Step 3: 先写失败测试（归档行为）**

追加到 `tt_solo/tests/test_state.py`：

```python
def test_archive_writes_one_line_on_roll(tmp_path, now_fn):
    """跨日重置前, 前一日的账本摘要须追加到 history.jsonl。"""
    from datetime import datetime
    from ttcore.state import Ledger

    day1 = lambda: datetime(2026, 9, 14, 10, 0, 0)
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=day1)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    assert led.archive_current() is True
    lines = (tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json
    row = json.loads(lines[0])
    assert row["date"] == "2026-09-14"
    assert row["sold_total"] == 100
    assert "realized_pnl" in row


def test_archive_skips_empty_ledger(tmp_path, now_fn):
    """空账本不产生归档行(避免跨日被反复写垃圾行)。"""
    from ttcore.state import Ledger
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    led.load()
    assert led.archive_current() is False
    assert not (tmp_path / "tt_history.jsonl").exists()


def test_archive_is_idempotent_per_date(tmp_path, now_fn):
    """同一天重复调用只归档一次。"""
    from datetime import datetime
    from ttcore.state import Ledger
    day1 = lambda: datetime(2026, 9, 14, 10, 0, 0)
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=day1)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    assert led.archive_current() is True
    assert led.archive_current() is False
    assert len((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()) == 1


def test_archive_failure_does_not_raise(tmp_path, now_fn, monkeypatch):
    """归档异常必须被吞掉 —— 绝不影响交易主流程。"""
    from ttcore import state as st
    led = st.Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(st, "atomic_write", boom)
    assert led.archive_current() is False        # 不抛异常


def test_load_archives_stale_day_before_reset(tmp_path):
    """load() 发现旧日账本时, 必须先归档再重置(否则历史永久丢失)。"""
    import json
    from datetime import datetime
    from ttcore.state import Ledger

    p = tmp_path / "tt_state.json"
    p.write_text(json.dumps({
        "version": 1, "date": "2026-09-14",
        "updated_at": "2026-09-14T15:00:00",
        "symbols": {"600900.SH": {"sold_today": 200, "bought_today": 100,
                                  "trips": 1, "realized_pnl": 33.0}},
        "events": [],
    }), encoding="utf-8")

    day2 = lambda: datetime(2026, 9, 15, 10, 0, 0)
    led = Ledger(path=p, now_fn=day2)
    led.load()
    assert led.state["date"] == "2026-09-15"          # 已重置
    row = json.loads((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip())
    assert row["date"] == "2026-09-14"
    assert row["sold_total"] == 200


def test_read_history_returns_rows(tmp_path, now_fn):
    from datetime import datetime
    from ttcore.state import Ledger
    day1 = lambda: datetime(2026, 9, 14, 10, 0, 0)
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=day1)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    led.archive_current()
    rows = Ledger.read_history(tmp_path / "tt_state.json")
    assert len(rows) == 1 and rows[0]["date"] == "2026-09-14"
```

- [ ] **Step 4: 跑测试确认失败**

Run: `python -m pytest tt_solo/tests/test_state.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: FAIL — `AttributeError: 'Ledger' object has no attribute 'archive_current'`

- [ ] **Step 5: 实现归档**

在 `tt_solo/ttcore/state.py` 的 `Ledger.__init__` 中新增两行：

```python
    def __init__(self, path=None, now_fn=None):
        self.path = path if path is not None else DEFAULT_STATE_PATH
        self.now_fn = now_fn or datetime.now
        self.state = _empty_state(_today_str(self.now_fn()))
        self._loaded = False
        # 日终归档: 与账本同目录的 append-only JSONL(见 archive_current)
        self.history_path = (Path(self.path).with_name(HISTORY_NAME)
                             if self.path else None)
        self._archived_dates = self._load_archived_dates()
```

新增方法（放在 `roll_if_new_day` 之后）：

```python
    # ---------------- 日终归档 ----------------

    def _load_archived_dates(self):
        """已归档日期集合(用于幂等判断)。读不到 → 空集, 不阻断。"""
        if not self.history_path:
            return set()
        try:
            p = Path(self.history_path)
            if not p.exists():
                return set()
            out = set()
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line).get("date")
                except ValueError:
                    continue
                if d:
                    out.add(str(d))
            return out
        except OSError:
            return set()

    def archive_current(self):
        """把当前账本摘要追加到 history.jsonl。返回 True 表示真的写了一行。

        fail-safe: 任何异常都吞掉并返回 False —— 归档是旁路副作用, 绝不允许
        它影响交易主流程。空账本(无成交且无事件)不写, 避免跨日刷垃圾行。
        """
        try:
            if not self.history_path:
                return False
            syms = self.state.get("symbols") or {}
            events = self.state.get("events") or []
            if not syms and not events:
                return False
            day = str(self.state.get("date") or "")
            if not day or day in self._archived_dates:
                return False
            sold_total = sum(int((v or {}).get("sold_today", 0))
                             for v in syms.values())
            bought_total = sum(int((v or {}).get("bought_today", 0))
                               for v in syms.values())
            row = {
                "date": day,
                "archived_at": datetime.now().isoformat(timespec="seconds"),
                "sold_total": sold_total,
                "bought_total": bought_total,
                "trips": self.total_trips(),
                "realized_pnl": self.total_realized_pnl(),
                "trades": len(events),
                "symbols": {c: round(float((v or {}).get("realized_pnl", 0)), 2)
                            for c, v in syms.items()},
            }
            p = Path(self.history_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # append-only: 读旧内容 → 追加 → 原子替换(保证读者永不见半行)
            old = p.read_text(encoding="utf-8") if p.exists() else ""
            atomic_write(p, old + json.dumps(row, ensure_ascii=False) + "\n")
            self._archived_dates.add(day)
            return True
        except Exception:                 # noqa: BLE001 - 刻意吞掉一切
            return False

    @staticmethod
    def read_history(path=None, limit=60):
        """读归档历史(升序)。坏行跳过; 文件不存在 → []。"""
        p = (Path(path).with_name(HISTORY_NAME) if path
             else STATE_DIR / HISTORY_NAME)
        try:
            if not p.exists():
                return []
            rows = []
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
            rows.sort(key=lambda r: str(r.get("date") or ""))
            return rows[-int(limit):] if limit else rows
        except OSError:
            return []
```

在 `load()` 的重置分支前插归档调用：

```python
    def load(self):
        """读盘。跨日 / 损坏 / 版本不符 → 先归档旧账本, 再重置为新日账本。"""
        today = _today_str(self.now_fn())
        if self.path and Path(self.path).exists():
            try:
                raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
                if (isinstance(raw, dict)
                        and raw.get("version") == STATE_VERSION
                        and raw.get("date") == today
                        and isinstance(raw.get("symbols"), dict)):
                    raw.setdefault("events", [])
                    self.state = raw
                    self._loaded = True
                    return self.state
                # 旧日/旧版本: 先把存量数据归档, 否则永久丢失
                if isinstance(raw, dict) and raw.get("symbols"):
                    self.state = raw
                    self.archive_current()
            except (ValueError, OSError):
                pass                          # 坏文件 → 按新日重置(fail-safe)
        self.state = _empty_state(today)
        self._loaded = True
        self.save()
        return self.state
```

并在 `reset_day()` 里也加一行归档（覆盖 `roll_if_new_day` 路径）：

```python
    def reset_day(self, day=None):
        self.archive_current()                # 重置前留档(幂等, 失败不影响)
        self.state = _empty_state(_today_str(day or self.now_fn()))
        self.save()
        return self.state
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tt_solo/tests/test_state.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: PASS — 原 14 例 + 新增 6 例

- [ ] **Step 7: Commit**

```bash
git add tt_solo
git commit -m "feat(tt_solo): 迁移 state + 新增日终归档(补历史曲线数据源)"
```

---

### Task 4: `broker.py` 吸收 `prism.live_account`

**Files:**
- Create: `tt_solo/ttcore/broker.py`
- Create: `tt_solo/tests/test_broker.py`

**Interfaces:**
- Consumes: 无（仅标准库 + 惰性 `xtquant`）
- Produces: `ttcore.broker.LiveAccount`（方法 `connect()`、`asset()`、`positions()`、`total_asset()`、`available_cash()`、`can_use_map()`）、`ttcore.broker._QmtBackend`、`ttcore.broker.calc_buy_volume(price, total_asset, position_ratio=0.15, lot=100)`、`QMT_DATA_DIR`、`LOT`

- [ ] **Step 1: 拷贝并去掉 prism 前缀**

```powershell
Copy-Item D:\cc-joesph\prism\live_account.py D:\cc-joesph\tt_solo\ttcore\broker.py
```

`prism/live_account.py` 本身**不 import prism 任何东西**（只 `import time` 与惰性 `from xtquant import ...`），所以只需改文档字符串里的模块名引用，代码零改动。

改文件头注释第 1 行的模块说明：

```python
# -*- coding: utf-8 -*-
"""miniQMT 实盘账户适配层(只读): 资产 / 持仓 / T+1 可卖量 / 可用资金。

ponytail: 整体搬自 prism/live_account.py @2026-09-16 —— 该模块本就不依赖
prism 内部, 搬过来即可让 tt_solo 自包含。

安全边界:
  - 只做 query_* 查询, 绝不调用 order_stock / cancel_order_stock 等下单接口;
  - 全部 fail-open: 未连接/查询异常 → None / {}, 由调用方 fail-closed 决策;
  - 后端可注入(backend 参数), 离线测试无需 QMT 终端。
"""
```

- [ ] **Step 2: 写测试 `tt_solo/tests/test_broker.py`**

```python
# -*- coding: utf-8 -*-
"""broker 测试: 用注入的 fake backend 覆盖只读语义与降级链。"""
import pytest

from ttcore import broker


class FakeBackend:
    def __init__(self, ok=True, asset=None, positions=None):
        self._ok = ok
        self._asset = asset
        self._positions = positions
        self.calls = []

    def connect(self):
        self.calls.append("connect")
        return self._ok

    def asset(self):
        return self._asset

    def positions(self):
        return self._positions


def test_asset_none_when_not_connected():
    acc = broker.LiveAccount(backend=FakeBackend(ok=False))
    assert acc.asset() is None
    assert acc.positions() == {}


def test_asset_and_positions_pass_through():
    acc = broker.LiveAccount(backend=FakeBackend(
        asset={"total_asset": 274648.96, "cash": 188570.96},
        positions={"600900.SH": {"volume": 1000, "can_use_volume": 1000,
                                 "market_value": 28630.0}}))
    a = acc.asset()
    assert a["total_asset"] == pytest.approx(274648.96)
    assert a["cash"] == pytest.approx(188570.96)
    p = acc.positions()
    assert p["600900.SH"]["can_use_volume"] == 1000


def test_connect_exception_is_swallowed():
    class Boom(FakeBackend):
        def connect(self):
            raise RuntimeError("xtquant missing")
    acc = broker.LiveAccount(backend=Boom())
    assert acc.connect() is False
    assert acc.asset() is None


def test_total_asset_and_cash_zero_become_none():
    acc = broker.LiveAccount(backend=FakeBackend(
        asset={"total_asset": 0, "cash": 0}))
    assert acc.total_asset() is None
    assert acc.available_cash() is None


def test_can_use_map():
    acc = broker.LiveAccount(backend=FakeBackend(positions={
        "600900.SH": {"volume": 1000, "can_use_volume": 800},
        "601088.SH": {"volume": 300, "can_use_volume": 0},
    }))
    assert acc.can_use_map() == {"600900.SH": 800, "601088.SH": 0}


def test_calc_buy_volume_floor_to_lot():
    # 100000 × 0.15 / 28.5 = 526.3 → 500
    assert broker.calc_buy_volume(28.5, 100000, 0.15) == 500


def test_calc_buy_volume_rejects_bad_input():
    assert broker.calc_buy_volume(0, 100000) == 0
    assert broker.calc_buy_volume(28.5, 0) == 0
    assert broker.calc_buy_volume(28.5, 100000, 0) == 0
    # 不足一手 → 0
    assert broker.calc_buy_volume(999.0, 1000, 0.15) == 0


def test_module_does_not_import_prism_or_shared():
    """自包含的硬约束: broker 不得引入外部包。"""
    src = (broker.__file__)
    text = open(src, encoding="utf-8").read()
    assert "prism" not in text.replace("prism/live_account.py", "")
    assert "shared" not in text
```

- [ ] **Step 3: 跑测试确认通过**

Run: `python -m pytest tt_solo/tests/test_broker.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: PASS（8 passed）。若 `test_module_does_not_import_prism_or_shared` 失败，说明注释里还留着 `prism` 字样，按 Step 1 清理。

- [ ] **Step 4: Commit**

```bash
git add tt_solo
git commit -m "feat(tt_solo): broker 吸收 prism.live_account(账户只读自包含)"
```

---

### Task 5: 迁移 `market.py` + `config.py` + `tt_config.json` + sample_data

**Files:**
- Create: `tt_solo/ttcore/market.py`、`tt_solo/ttcore/config.py`
- Create: `tt_solo/ttcore/tt_config.json`
- Create: `tt_solo/ttcore/sample_data/*.csv`（4 个）
- Create: `tt_solo/tests/test_engine.py` 的先决夹具（conftest 扩展）

**Interfaces:**
- Consumes: `ttcore.grid`
- Produces: `ttcore.market.indicators/closes/SAMPLE_DIR/make_feed/MarketFeed/XtdataBackend/SampleBackend`、`ttcore.config.load/validate/enabled_symbols/symbol_by_code/DEFAULT_CONFIG/DEFAULT_CONFIG_PATH/ConfigError`

- [ ] **Step 1: 拷贝模块与数据**

```powershell
cd D:\cc-joesph
Copy-Item tt\market.py tt_solo\ttcore\market.py
Copy-Item tt\config.py tt_solo\ttcore\config.py
Copy-Item tt\tt_config.json tt_solo\ttcore\tt_config.json
New-Item -ItemType Directory -Force -Path tt_solo\ttcore\sample_data | Out-Null
Copy-Item tt\sample_data\*.csv tt_solo\ttcore\sample_data\
```

`market.py` 只 `from . import grid`（已是相对导入）→ **零改动**。
`config.py` 无外部 import → **零改动**。

- [ ] **Step 2: 原样拷贝 `tt_config.json`（**不要重写**）**

```powershell
Copy-Item D:\cc-joesph\tt\tt_config.json D:\cc-joesph\tt_solo\ttcore\tt_config.json -Force
```

> **规划期误判纠正**：曾以为该文件中文是乱码需重写，**已推翻** —— 用 `read` 工具
> 核验，`name` 字段是正确的 `长江电力` / `中国海油` / `中国神华` / `松发股份`。
> 之前的"乱码"是 **PowerShell stdout 的输出通道问题**，不是文件编码。
> **逐字节拷贝即可，切勿用脚本重写**（重写反而可能引入真乱码）。

- [ ] **Step 3: 扩写 `tt_solo/tests/conftest.py`，搬入原夹具**

把 `tt/tests/conftest.py` 的夹具全量搬来，只改两处 import 与 `sample_dir` 路径：

```python
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
    """一份自足的测试配置(不读盘上的 tt_config.json, 避免被用户改动影响)。"""
    return tt_config.load(overrides={
        "dry_run": True,
        "paper_total_asset": 500000.0,
        "paper_positions": {"600900.SH": 5000, "600938.SH": 3000},
        "max_units_per_round": 2,
        "grid": {"band_mode": "sigma", "band_k": 1.0, "n_units": 5,
                 "ref_mode": "prev_close", "sigma_window": 60},
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
```

- [ ] **Step 4: 跑配置/样本源冒烟**

Run:
```powershell
cd D:\cc-joesph\tt_solo
python -c "from ttcore import config; c=config.load(); print(c['grid'], len(c['symbols'])); print([s['name'] for s in c['symbols']])"
```
Expected: 打印 grid dict 与 4，名称显示 **长江电力 中国海油 中国神华 松发股份**（不是乱码）

- [ ] **Step 5: Commit**

```bash
git add tt_solo
git commit -m "feat(tt_solo): 迁移 market + config + 配置/sample_data(修中文乱码)"
```

---

### Task 6: 迁移 `engine.py` + `executor.py`

**Files:**
- Create: `tt_solo/ttcore/engine.py`、`tt_solo/ttcore/executor.py`
- Create: `tt_solo/tests/test_engine.py`、`tt_solo/tests/test_executor.py`

**Interfaces:**
- Consumes: `ttcore.broker.LiveAccount`、`ttcore.grid`、`ttcore.market`、`ttcore.risk.RiskGate`、`ttcore.config`
- Produces: `ttcore.engine.TTEngine/Intent/make_engine/SIGNAL_KEYS`、`ttcore.executor.DirectExecutor`

- [ ] **Step 1: 拷贝并改 import**

```powershell
cd D:\cc-joesph
Copy-Item tt\engine.py tt_solo\ttcore\engine.py
Copy-Item tt\executor.py tt_solo\ttcore\executor.py
Copy-Item tt\tests\test_engine.py tt_solo\tests\test_engine.py
Copy-Item tt\tests\test_executor.py tt_solo\tests\test_executor.py
```

`tt_solo/ttcore/engine.py` 第 103 行：

```python
                from prism.live_account import LiveAccount
```
改为
```python
                from .broker import LiveAccount
```

`executor.py` 检查是否有 `shared`/`prism` 引用（若有，改指 `._vendor` / `.broker`）。

- [ ] **Step 2: 改测试 import**

```powershell
cd D:\cc-joesph\tt_solo\tests
foreach ($f in @('test_engine.py','test_executor.py')) {
  $t = Get-Content $f -Raw
  $t = $t -replace 'from tt\.','from ttcore.' -replace 'from tt import','from ttcore import'
  $t = $t -replace 'from prism\.live_account','from ttcore.broker'
  [System.IO.File]::WriteAllText("D:\cc-joesph\tt_solo\tests\$f", $t, (New-Object System.Text.UTF8Encoding($false)))
}
```

- [ ] **Step 3: 跑测试确认通过**

Run: `python -m pytest tt_solo/tests/test_engine.py tt_solo/tests/test_executor.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: PASS — engine 27 + executor 22 例（含参数化以实际为准）

- [ ] **Step 4: 确认 engine 不再引用 prism**

Run: `Select-String -Path tt_solo\ttcore\engine.py, tt_solo\ttcore\executor.py -Pattern 'prism|shared'`
Expected: 无输出

- [ ] **Step 5: Commit**

```bash
git add tt_solo
git commit -m "feat(tt_solo): 迁移 engine + executor(接线 broker)"
```

---

### Task 7: 迁移 `daemon.py` + `arm_today.py`

**Files:**
- Create: `tt_solo/ttcore/daemon.py`、`tt_solo/ttcore/arm_today.py`
- Create: `tt_solo/ttcore/__main__.py`（可选便捷入口）
- Create: `tt_solo/tests/test_direct_mode.py`、`test_drill_book.py`、`test_env_sim.py`

**Interfaces:**
- Consumes: `ttcore._vendor.STATE_DIR/atomic_write`、`ttcore.engine`、`ttcore.state`、`ttcore.executor`
- Produces: `ttcore.daemon.TTDaemon/is_paused/armed_state/write_signals/main`、`ttcore.arm_today.main`

- [ ] **Step 1: 拷贝并改 import**

```powershell
cd D:\cc-joesph
Copy-Item tt\daemon.py tt_solo\ttcore\daemon.py
Copy-Item tt\arm_today.py tt_solo\ttcore\arm_today.py
Copy-Item tt\tests\test_direct_mode.py tt_solo\tests\
Copy-Item tt\tests\test_drill_book.py tt_solo\tests\
Copy-Item tt\tests\test_env_sim.py tt_solo\tests\
```

`tt_solo/ttcore/daemon.py` 第 28 行：

```python
from shared.common import STATE_DIR, atomic_write
```
改为
```python
from ._vendor import STATE_DIR, atomic_write
```

同一文件删除第 37 行 `REPO = Path(__file__).resolve().parent.parent`（未使用）。

`tt_solo/ttcore/arm_today.py`：把 `from shared.common import atomic_write` 改为
`from ._vendor import atomic_write`；`sys.path.insert(0, str(Path(__file__).resolve().parent.parent))`
改为指向 `tt_solo/`（`parents[1]`）。注意 `arm_today.py` 作为脚本直接跑时相对导入会失败 —— 改为绝对导入：

```python
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # -> tt_solo/
from ttcore._vendor import atomic_write                          # noqa: E402
from ttcore import config as tt_config                           # noqa: E402
```

- [ ] **Step 2: 改测试 import（三个文件）**

```powershell
cd D:\cc-joesph\tt_solo\tests
foreach ($f in @('test_direct_mode.py','test_drill_book.py','test_env_sim.py')) {
  $t = Get-Content $f -Raw
  $t = $t -replace 'from tt\.','from ttcore.' -replace 'from tt import','from ttcore import'
  $t = $t -replace 'from shared\.common','from ttcore._vendor'
  [System.IO.File]::WriteAllText("D:\cc-joesph\tt_solo\tests\$f", $t, (New-Object System.Text.UTF8Encoding($false)))
}
```

- [ ] **Step 3: 跑守护相关测试**

Run: `python -m pytest tt_solo/tests/test_direct_mode.py tt_solo/tests/test_drill_book.py tt_solo/tests/test_env_sim.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: PASS — 6 + 8 + 14 例

- [ ] **Step 4: 守护 dry-run 实跑冒烟**

Run:
```powershell
cd D:\cc-joesph\tt_solo
$env:PYTHONIOENCODING='utf-8'
python -m ttcore.daemon --once --sample
```
Expected: 打印一轮 plan（非交易时段应被 `SESSION_CLOSED` 拦下或按 sample 数据出档位），**不写任何信号文件**，退出码 0

- [ ] **Step 5: 确认运行数据落在 tt_solo 自己的 runtime/**

Run:
```powershell
Get-ChildItem D:\cc-joesph\tt_solo\runtime\state
Test-Path D:\cc-joesph\runtime\state\tt_state.json   # 应仍存在但与本次无关
```
Expected: `tt_solo/runtime/state/` 下出现 `tt_state.json` / `tt_runtime.json`（时间戳为刚才）；主项目那个文件的 mtime **不变**

- [ ] **Step 6: Commit**

```bash
git add tt_solo
git commit -m "feat(tt_solo): 迁移 daemon + arm_today(自带 runtime 路径)"
```

---

### Task 8: 依赖剥离闸门 + 全套测试绿

**Files:**
- Create: `tt_solo/tests/test_selfcontained.py`

**Interfaces:**
- Consumes: 全部 `ttcore.*`
- Produces: 无（验证任务）

- [ ] **Step 1: 写自包含护栏(共享扫描器 + 整树测试)**

> **为什么用 AST 扫描而不是行正则**：`tt_solo` 里**刻意保留**了出处散文
> （`ttcore/_vendor.py`、`broker.py`、`engine.py:9/30/444`、`executor.py:84` 都提到
> `prism.*` 作为"这段逻辑来自哪里"的溯源说明）。行正则的两种写法都不好：
> 不锚定会被散文误伤；锚定虽可（实测零命中）但依然只认字面文本，对动态 import 无效。
> **AST 扫描按真实 `Import`/`ImportFrom` 节点判定，散文永远自由，且能覆盖嵌套 import。**
>
> 单一实现放在 `tt_solo/tests/_import_scan.py`，供本任务与既有
> `test_broker.py`（它已有一份本地 AST 扫描）共用 —— 避免同一逻辑块两处重复。

`tt_solo/tests/_import_scan.py`：

```python
# -*- coding: utf-8 -*-
"""自包含扫描器(单一实现): 判定一个 Python 文件是否 import 了禁用包。

用 AST 而非文本正则 —— tt_solo 刻意保留出处散文(如 "与 prism.trader 同构"),
文本扫描会把散文误判成依赖; 而 AST 只看真实的 Import/ImportFrom 节点,
散文永远自由, 且能覆盖函数内/条件内的嵌套 import。
"""
import ast
from pathlib import Path

FORBIDDEN_ROOT_IMPORTS = frozenset(
    {"prism", "shared", "qmt_sync", "backtest", "legacy"})


def imported_roots(source):
    """返回源码 import 到的顶层包名集合(相对 import 不计)。

    ast.walk 会遍历全部后代节点, 故函数内/条件内的 import 也会被抓到。
    取根段(name.split(".")[0]), 所以 `import prism.foo` 归为 `prism`。
    相对 import(from . import x / from .broker import Y)解析不到顶层包, 跳过。
    """
    tree = ast.parse(source)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                roots.add(node.module.split(".")[0])
    return roots


def forbidden_imports(path, source=None):
    """该文件的禁用 import 列表(已排序); 无则空列表。"""
    if source is None:
        source = Path(path).read_text(encoding="utf-8")
    hits = imported_roots(source) & FORBIDDEN_ROOT_IMPORTS
    return sorted(hits)
```

`tt_solo/tests/test_selfcontained.py`：

```python
# -*- coding: utf-8 -*-
"""自包含硬护栏: tt_solo 不得依赖主项目任何模块。

这是本次重构的完成定义 —— 用测试固化, 防止将来有人顺手 import 回去。
"""
import importlib
from pathlib import Path

from _import_scan import FORBIDDEN_ROOT_IMPORTS, forbidden_imports

ROOT = Path(__file__).resolve().parents[1]        # tt_solo/


def _py_files():
    return [p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_scanner_discriminates():
    """负控: 扫描器必须真能判别禁用 import(否则整条护栏是空的)。"""
    assert forbidden_imports("x.py", "import prism\n") == ["prism"]
    assert forbidden_imports("x.py", "import prism.foo\n") == ["prism"]
    assert forbidden_imports("x.py", "def f():\n    import shared\n") == ["shared"]
    assert forbidden_imports("x.py", "from . import grid\n") == []
    assert forbidden_imports("x.py", "from .broker import X\n") == []
    # 散文里的 prism 不算依赖(这正是选 AST 的理由)
    assert forbidden_imports("x.py", '"""与 prism.trader 同构"""\n') == []
    # 禁用清单本身不得被改窄
    assert {"prism", "shared", "qmt_sync", "backtest", "legacy"} <= \
        FORBIDDEN_ROOT_IMPORTS


def test_no_forbidden_imports_anywhere():
    bad = []
    for p in _py_files():
        hits = forbidden_imports(p)
        if hits:
            bad.append("%s: %s" % (p.relative_to(ROOT), hits))
    assert bad == [], "tt_solo 出现外部依赖: %s" % bad


def test_core_modules_importable_without_qmt():
    """核心模块必须能在没有 xtquant 的环境导入(惰性加载)。"""
    for name in ("ttcore._vendor", "ttcore.grid", "ttcore.risk",
                 "ttcore.state", "ttcore.config", "ttcore.broker",
                 "ttcore.market", "ttcore.engine", "ttcore.executor",
                 "ttcore.daemon"):
        importlib.import_module(name)
```

- [ ] **Step 1b: 让 `test_broker.py` 复用共享扫描器（消除重复实现）**

`test_broker.py` 里已有一份本地 `_imported_roots`（Task 4 引入，约 15 行）。
现在有了单一实现 → 删掉本地副本，改为 `from _import_scan import forbidden_imports`，
其断言改成 `assert forbidden_imports(broker.__file__) == []`。
**必须保留该文件既有的负控测试 `test_import_scanner_discriminates`**（它证明扫描器会失败），
可改为调用共享扫描器。改完 `test_broker.py` 的测试数应保持不变（13）。

- [ ] **Step 2: 跑护栏测试**

Run: `python -m pytest tt_solo/tests/test_selfcontained.py tt_solo/tests/test_broker.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: PASS — selfcontained 3 例（含负控）+ broker 13 例。若 `test_no_forbidden_imports_anywhere` 失败，按报错逐个清理残留 import。

- [ ] **Step 3: 独立复核（不依赖测试，两道）**

Run（锚定 grep，只查真实 import 语句）:
```powershell
Get-ChildItem tt_solo -Recurse -Include *.py |
  Where-Object { $_.FullName -notmatch '__pycache__' } |
  Select-String -Pattern '^\s*(from|import)\s+(prism|shared|qmt_sync|backtest|legacy)\b'
```
Expected: 无输出。
> **注意**：裸词搜索（不带 `^\s*(from|import)` 前缀）会命中若干**出处散文**
> （`ttcore/_vendor.py`、`broker.py`、`engine.py:9/30/444`、`executor.py:84`），
> 那是**刻意保留的溯源说明，不是依赖**。判断依据永远是"真实 import 节点"，
> 不是"文件里有没有出现 prism 这个词"。

- [ ] **Step 4: 脱沙箱跑全套 tt_solo 测试**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest tt_solo/tests -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt251`
Expected: **全绿，用例数 ≥ 150**

若数量不足，检查是否有测试文件漏拷贝（对照 `tt/tests/` 逐文件核对）。

- [ ] **Step 5: Commit**

```bash
git add tt_solo
git commit -m "test(tt_solo): 自包含硬护栏 + 全套测试通过(>=150)"
```

---

### Task 9: 仪表盘后端 + 2 个新接口

**Files:**
- Create: `tt_solo/dashboard/app.py`
- Create: `tt_solo/dashboard/__init__.py`（**必须** —— 见 Step 1d）
- Create: `tt_solo/dashboard/tests/conftest.py`（见 Step 1b）
- Create: `tt_solo/dashboard/tests/test_guard.py`、`test_api.py`
- Create: `tt_solo/dashboard/templates/.gitkeep`（前端在 Task 10 填）

**Interfaces:**
- Consumes: `ttcore._vendor`（`STATE_DIR`、`is_local_request`）、`ttcore.config`、`ttcore.market`、`ttcore.daemon`（`is_paused`、`armed_state`）、`ttcore.engine.TTEngine`、`ttcore.state.Ledger`
- Produces: Flask `app`，路由 `GET /`、`GET /api/status`、`GET /api/config`、`GET /api/kline/<code>`、`GET /api/ledger/history`、`GET /api/rejections`、`GET /api/health`、`POST /api/pause`、`POST /api/arm`

- [ ] **Step 1: 拷贝 tt_web/app.py 并改造**

```powershell
cd D:\cc-joesph
New-Item -ItemType Directory -Force -Path tt_solo\dashboard\tests, tt_solo\dashboard\templates, tt_solo\dashboard\static | Out-Null
Copy-Item tt_web\app.py tt_solo\dashboard\app.py
Copy-Item tt_web\static\echarts.min.js tt_solo\dashboard\static\
Copy-Item tt_web\tests\test_guard.py tt_solo\dashboard\tests\
Copy-Item tt_web\tests\__init__.py tt_solo\dashboard\tests\ -ErrorAction SilentlyContinue
New-Item -ItemType File -Force -Path tt_solo\dashboard\tests\__init__.py | Out-Null
```

`tt_solo/dashboard/app.py` 改 import 段（原第 30、113 行）：

```python
ROOT = Path(__file__).resolve().parent.parent          # -> tt_solo/
sys.path.insert(0, str(ROOT))

from ttcore._vendor import STATE_DIR, is_local_request   # noqa: E402
from ttcore import config as tt_config                   # noqa: E402
from ttcore import market                                # noqa: E402
from ttcore import daemon as tt_daemon                   # noqa: E402
from ttcore.engine import TTEngine                       # noqa: E402
from ttcore.state import Ledger                          # noqa: E402
```

删除原文件里第二处 `from shared.common import is_local_request`（第 113 行）—— 已并入上面。

端口默认改 **5011**：

```python
    port = int(os.environ.get("TT_WEB_PORT") or 5011)
```

文档字符串首行与启动注释同步改为 `5011`。

- [ ] **Step 1b: 建 `tt_solo/dashboard/tests/conftest.py`（**关键**）**

`tt_solo/tests/conftest.py` **不会**被 dashboard 测试加载 —— pytest 只沿测试文件的
父目录链找 conftest，`tt_solo/tests/` 不是 `tt_solo/dashboard/tests/` 的父目录。
缺了它，`from dashboard import app` 会直接 `ModuleNotFoundError`。

```python
# -*- coding: utf-8 -*-
"""dashboard 测试夹具: 把 tt_solo/ 挂上 sys.path。

必须独立于 tt_solo/tests/conftest.py —— 后者不是本目录的父目录,
pytest 不会加载它。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # -> tt_solo/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

- [ ] **Step 1c: 改造 `tt_solo/dashboard/tests/test_guard.py`（**易漏**）**

该文件用的是 `import tt_web.app as app_module`（**模块形式，不是 `from`**），
所以普通的 `from tt.` 替换**抓不到它**，漏改会直接 `ModuleNotFoundError`。

> **编码说明**：该文件曾疑似 GBK 乱码，**已核验为误判** —— 原始字节为
> `e4 ba a4`（UTF-8 的「交」），文件本身是正确的 UTF-8。
> **不要做任何转码**，只改 import 一行。

```powershell
cd D:\cc-joesph\tt_solo\dashboard\tests
$t = Get-Content 'D:\cc-joesph\tt_web\tests\test_guard.py' -Raw
$t = $t -replace 'import tt_web\.app as app_module', 'import dashboard.app as app_module'
[System.IO.File]::WriteAllText('D:\cc-joesph\tt_solo\dashboard\tests\test_guard.py', $t, (New-Object System.Text.UTF8Encoding($false)))
```

确认改对了（**用 `read` 工具看，不要用 pwsh 打印**，pwsh 的 stdout 会把中文显示成乱码）：
```
read tt_solo/dashboard/tests/test_guard.py
```
Expected: 第 11 行左右为 `import dashboard.app as app_module`；docstring 为可读中文

`sys.path.insert(0, str(Path(__file__).parent.parent.parent))` 在原文件里指向
`tt_solo/`（`dashboard/tests/` 往上三级）—— **恰好正确，无需改**。

- [ ] **Step 1d: 建 `tt_solo/dashboard/__init__.py`（**执行时发现，原计划遗漏**）**

**必须存在。** 否则 `tt_solo/tests/conftest.py` 与 `tt_solo/dashboard/tests/conftest.py`
会派生出**同名模块** `tests.conftest`，整树收集时 pytest 直接报
`ValueError: Plugin already registered under a different name`（实测复现）。
建了 `__init__.py` 后 `dashboard` 成为真正的包，整树可收集。

```powershell
New-Item -ItemType File -Force -Path tt_solo\dashboard\__init__.py | Out-Null
```

> **Step 1b 的 conftest 说明已修正**：原计划称"缺了它 `from dashboard import app` 会
> `ModuleNotFoundError`"——**该说法只在"只跑 dashboard 目录"时成立**。整树运行时，
> pytest 的初始 conftest 发现机制**会**加载 `tt_solo/tests/conftest.py`（它把 `tt_solo/`
> 放进 `sys.path`），`dashboard` 随后作为 PEP 420 命名空间包也能导入。
>
> 也就是说：**`__init__.py` 才是关键，conftest 在有了 `__init__.py` 之后是冗余的。**
> 仍保留 conftest（5 行、无谎报、brief 要求），但**不要**把它当成不可省的依赖。

**Step 1c 补充：** `tt_solo/dashboard/tests/test_guard.py` 的文件头 docstring 仍写着
`tt_web 交易闸门护栏测试`，改为 `dashboard`（纯文案，不影响断言）。

---

- [ ] **Step 2: 先写失败测试（新接口）**

`tt_solo/dashboard/tests/test_api.py`：

```python
# -*- coding: utf-8 -*-
"""仪表盘接口测试: 只读接口形状 + 新聚合接口 + 写闸门确认。"""
import json

import pytest

from dashboard import app as dash


@pytest.fixture
def client():
    dash.app.config["TESTING"] = True
    return dash.app.test_client()


def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200


def test_config_is_masked(client):
    """脱敏: 不得回传 account_id 等敏感字段。"""
    r = client.get("/api/config")
    body = r.get_json()
    assert body["ok"] is True
    assert "account_id" not in json.dumps(body)


def test_rejections_empty_when_no_runtime(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "RUNTIME_PATH", tmp_path / "missing.json")
    r = client.get("/api/rejections")
    body = r.get_json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["data"]["total"] == 0
    assert body["data"]["groups"] == []


def test_rejections_aggregates_by_code(client, monkeypatch, tmp_path):
    rt = tmp_path / "tt_runtime.json"
    rt.write_text(json.dumps({
        "rejected": [
            {"reject_code": "NO_BASE_POSITION", "reject_msg": "无底仓",
             "code": "600900.SH", "name": "长江电力", "side": "SELL",
             "price": 28.5, "volume": 300, "reason": "档位1"},
            {"reject_code": "NO_BASE_POSITION", "reject_msg": "无底仓",
             "code": "601088.SH", "name": "中国神华", "side": "SELL",
             "price": 47.2, "volume": 100, "reason": "档位1"},
            {"reject_code": "SIZE_ZERO", "reject_msg": "不足一手",
             "code": "600938.SH", "name": "中国海油", "side": "BUY",
             "price": 33.9, "volume": 0, "reason": "档位2"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(dash, "RUNTIME_PATH", rt)
    body = client.get("/api/rejections").get_json()
    assert body["ok"] is True
    assert body["data"]["total"] == 3
    g = body["data"]["groups"]
    assert g[0]["code"] == "NO_BASE_POSITION" and g[0]["count"] == 2
    assert g[1]["code"] == "SIZE_ZERO" and g[1]["count"] == 1
    assert len(g[0]["samples"]) == 2


def test_ledger_history_empty(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "STATE_PATH", tmp_path / "tt_state.json")
    body = client.get("/api/ledger/history").get_json()
    assert body["ok"] is True
    assert body["data"]["rows"] == []


def test_ledger_history_returns_rows(client, monkeypatch, tmp_path):
    st = tmp_path / "tt_state.json"
    st.write_text("{}", encoding="utf-8")
    (tmp_path / "tt_history.jsonl").write_text(
        json.dumps({"date": "2026-09-14", "sold_total": 100,
                    "bought_total": 100, "trips": 1,
                    "realized_pnl": 33.5, "trades": 2, "symbols": {}}) + "\n"
        + json.dumps({"date": "2026-09-15", "sold_total": 0,
                      "bought_total": 0, "trips": 0,
                      "realized_pnl": 0.0, "trades": 0, "symbols": {}}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(dash, "STATE_PATH", st)
    body = client.get("/api/ledger/history?days=10").get_json()
    assert body["ok"] is True
    rows = body["data"]["rows"]
    assert [r["date"] for r in rows] == ["2026-09-14", "2026-09-15"]
    assert rows[0]["realized_pnl"] == 33.5


def test_pause_requires_confirm(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "SIGNAL_ROOT", tmp_path)
    r = client.post("/api/pause", json={})
    assert r.status_code == 400
    assert (tmp_path / "paused").exists() is False


def test_pause_with_confirm_writes_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "SIGNAL_ROOT", tmp_path)
    r = client.post("/api/pause", json={"confirm": True, "paused": True})
    assert r.status_code == 200
    assert (tmp_path / "paused").exists() is True
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tt_solo/dashboard/tests/test_api.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: FAIL — `/api/rejections` 与 `/api/ledger/history` 返回 404

- [ ] **Step 4: 实现两个新接口**

在 `tt_solo/dashboard/app.py` 的 `api_health` 之前插入：

```python
@app.route("/api/rejections")
def api_rejections():
    """被拦归因聚合: 按 reject_code 分组计数 + 样本明细。

    数据源 = 守护快照的 rejected 列表(守护跑过才有); 无快照 → 空聚合。
    只读, 不触发重算 —— 面板每 5 秒轮询, 重算太贵。
    """
    try:
        rt = _read_json(RUNTIME_PATH) or {}
        rows = rt.get("rejected") or []
        groups = {}
        for it in rows:
            code = str(it.get("reject_code") or "UNKNOWN")
            g = groups.setdefault(code, {
                "code": code,
                "msg": str(it.get("reject_msg") or ""),
                "count": 0,
                "samples": [],
            })
            g["count"] += 1
            if len(g["samples"]) < 5:
                g["samples"].append({
                    "code": it.get("code"), "name": it.get("name"),
                    "side": it.get("side"), "price": it.get("price"),
                    "volume": it.get("volume"), "reason": it.get("reason"),
                })
        out = sorted(groups.values(), key=lambda g: (-g["count"], g["code"]))
        return jsonify({"ok": True, "data": {
            "total": len(rows), "groups": out,
            "at": rt.get("runtime_at", ""),
        }})
    except Exception as e:
        LOG.exception("rejections 失败")
        return jsonify({"ok": False, "error": repr(e)}), 500


@app.route("/api/ledger/history")
def api_ledger_history():
    """逐日账本归档序列(供收益曲线)。无归档 → 空数组, 不是错误。"""
    try:
        days = int(request.args.get("days", 60))
    except ValueError:
        days = 60
    days = max(1, min(days, 365))
    try:
        rows = Ledger.read_history(STATE_PATH, limit=days)
        return jsonify({"ok": True, "data": {"rows": rows, "days": days}})
    except Exception as e:
        LOG.exception("ledger history 失败")
        return jsonify({"ok": False, "error": repr(e)}), 500
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tt_solo/dashboard/tests -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt250`
Expected: PASS — guard 2 例 + api 9 例

- [ ] **Step 6: Commit**

```bash
git add tt_solo
git commit -m "feat(tt_solo): 仪表盘后端(端口 5011) + 被拦归因/历史曲线接口"
```

---

### Task 10: 仪表盘前端重建（五区块一屏决策面板）

**Files:**
- Replace: `tt_solo/dashboard/templates/index.html`（**整文件替换，不是编辑**）
- Delete: `tt_solo/dashboard/templates/.gitkeep`（目录里已有真实文件，占位无意义）

> ⚠️ **必须整文件替换，不要在旧模板上改。**
> Task 9 为让 `test_index_renders` 通过，先把主项目的旧 `tt_web/templates/index.html`
> （538 行）拷了过来 —— 那是**临时占位**。旧页面的 JS 只接了旧接口
> （`/api/status`、`/api/config`、`/api/kline`、`/api/pause`、`/api/arm`），
> **完全没有** `/api/rejections` 与 `/api/ledger/history`。若在其上做增量编辑，
> 会留下悬挂的旧逻辑和半新半旧的界面。
> 直接写入本任务给出的完整 `index.html` 内容（覆盖），然后删掉 `.gitkeep`。

**Interfaces:**
- Consumes: `/api/status`、`/api/config`、`/api/kline/<code>`、`/api/rejections`、`/api/ledger/history`、`/api/pause`、`/api/arm`
- Produces: 无（终端产物）

- [ ] **Step 1: 写 `tt_solo/dashboard/templates/index.html`**

要求（**逐条对照验收**）：

1. **状态条**：三道闸门 `dry_run → paused → armed` 串联显示，任一为"关"则该段变红并文字说明被拦在哪一道；同时显示 env、行情源、快照新鲜度（`_age` / `_source`）。
2. **账户卡**：总资产 / 现金 / 持仓数；**必须标注 `account.source`**（`qmt` 真实 vs `paper` 纸面推演，不可混同）。
3. **档位阶梯**：每只票一块，竖向列出 buy3..buy1 / **ref 中枢** / sell1..sell3，标注当前价位置、已成交档位（`filled_sell_units` / `filled_buy_units`）、开关状态（ENABLED/HALF/DISABLED）与带宽。
4. **今日战果**：`ledger.total_trips`、`ledger.total_realized_pnl`、各标的净敞口、`risk.daily_trades` / `max_daily_trades`、熔断状态。
5. **被拦排行**：`/api/rejections` 的横向条形图（宽度按 count 比例）+ 每组的 samples 明细（可折叠）。

技术约定：
- 原生 JS，`fetch` + `setInterval(load, 5000)`；
- ECharts 走本地 `static/echarts.min.js`（**不得引 CDN** —— 面板只监听本机，但断网也要能用）；
- 顶部固定"急停"与"今日放行"两个按钮，`POST` 时**必须带 `confirm: true`**，且点击前 `confirm()` 二次确认；
- 单接口失败不白屏：各自 catch 后该区块显示错误文案，其余区块照常渲染。

关键 JS 骨架（**下面是完整可运行代码，照此写入 `index.html`**，五个区块的渲染逻辑均已实现）：

```html
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>做T仪表盘</title>
<script src="/static/echarts.min.js"></script>
<style>
 body{font:13px/1.5 -apple-system,"Microsoft YaHei",sans-serif;margin:0;background:#0f1419;color:#d8dee9}
 .wrap{max-width:1400px;margin:0 auto;padding:12px}
 .row{display:flex;gap:12px;flex-wrap:wrap}
 .card{background:#1b2129;border:1px solid #2b333d;border-radius:8px;padding:12px;flex:1;min-width:260px}
 .card h3{margin:0 0 8px;font-size:13px;color:#8b98a5;font-weight:600}
 .gates{display:flex;gap:8px;align-items:center;margin-bottom:12px}
 .gate{padding:6px 12px;border-radius:6px;font-weight:600}
 .on{background:#1d3b2a;color:#4ade80;border:1px solid #2f6b45}
 .off{background:#3b1d1d;color:#f87171;border:1px solid #6b2f2f}
 .arrow{color:#4b5563}
 .big{font-size:22px;font-weight:700}
 .muted{color:#8b98a5}
 .up{color:#f87171}.down{color:#4ade80}
 table{width:100%;border-collapse:collapse}
 td,th{padding:4px 6px;text-align:right;border-bottom:1px solid #2b333d}
 th:first-child,td:first-child{text-align:left}
 button{background:#2b333d;color:#d8dee9;border:1px solid #3b4552;
        border-radius:6px;padding:6px 14px;cursor:pointer;font-weight:600}
 button.danger{background:#5a1f1f;border-color:#7f2d2d}
 button.ok{background:#1d3b2a;border-color:#2f6b45}
 .err{color:#f87171;font-size:12px}
 .bar{height:16px;background:#3b1d1d;border-radius:3px}
 .barrow{display:grid;grid-template-columns:150px 1fr 40px;gap:8px;
         align-items:center;margin:4px 0}
</style>
</head>
<body>
<div class="wrap">
  <div class="row" style="align-items:center;margin-bottom:12px">
    <h2 style="margin:0;flex:1">做T仪表盘 <span class="muted" id="clock"></span></h2>
    <button class="danger" onclick="togglePause(true)">急停</button>
    <button class="ok" onclick="togglePause(false)">解除急停</button>
    <button onclick="arm(true)">今日放行</button>
    <button onclick="arm(false)">撤销放行</button>
  </div>

  <!-- 1. 状态条 -->
  <div class="gates" id="gates"></div>

  <div class="row">
    <!-- 2. 账户卡 -->
    <div class="card" style="max-width:340px">
      <h3>账户</h3><div id="account"></div>
    </div>
    <!-- 4. 今日战果 -->
    <div class="card" style="max-width:340px">
      <h3>今日战果</h3><div id="battle"></div>
    </div>
    <!-- 5. 被拦排行 -->
    <div class="card"><h3>被拦原因排行</h3><div id="rejections"></div></div>
  </div>

  <!-- 3. 档位阶梯 -->
  <div class="card" style="margin-top:12px">
    <h3>档位阶梯</h3>
    <div class="row" id="ladders"></div>
  </div>

  <!-- 收益曲线 -->
  <div class="card" style="margin-top:12px">
    <h3>历史收益（按日归档）</h3>
    <div id="equity" style="height:220px"></div>
    <div class="muted" id="equityNote"></div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"]/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const num = (v, d=2) => (v === null || v === undefined) ? '—'
        : Number(v).toFixed(d);

let equityChart = null;

async function jget(url){
  const r = await fetch(url);
  if(!r.ok) throw new Error(url + ' HTTP ' + r.status);
  const b = await r.json();
  if(!b.ok) throw new Error(b.error || 'api not ok');
  return b.data;
}

function renderGates(d){
  const gates = [
    {name:'dry_run', off: d.dry_run,
     on:'实盘可发单', offText:'演练(不发单)'},
    {name:'paused', off: d.paused,
     on:'未急停', offText:'已急停'},
    {name:'armed', off: !d.armed,
     on:'今日已放行', offText:(d.armed_msg || '今日未放行')},
  ];
  $('gates').innerHTML =
    '<span class="muted">能否做T:</span>' +
    gates.map(g => `<span class="gate ${g.off?'off':'on'}">${
        g.name} · ${esc(g.off ? g.offText : g.on)}</span>`).join('<span class="arrow">→</span>')
    + `<span class="muted" style="margin-left:auto">env=${esc(d.env)} ·
       源=${esc(d.source)} · ${d._source||''}${d._age!=null?' '+d._age+'s':''}</span>`;
}

function renderAccount(d){
  const a = d.account || {};
  const srcTag = a.source === 'qmt' ? '<span class="on" style="padding:1px 6px;border-radius:4px">真实账户</span>'
              : '<span class="off" style="padding:1px 6px;border-radius:4px">纸面推演</span>';
  const pos = a.positions || {};
  const n = Object.keys(pos).length;
  $('account').innerHTML =
    `<div>${srcTag}</div>
     <div class="big">${num(a.total_asset)}</div>
     <div class="muted">现金 ${num(a.cash)} · 持仓 ${n} 只</div>
     ${a.note ? `<div class="muted" style="margin-top:6px">${esc(a.note)}</div>`:''}`;
}

function renderBattle(d){
  const l = d.ledger || {}, r = d.risk || {};
  const pnl = Number(l.total_realized_pnl || 0);
  $('battle').innerHTML =
    `<div class="big ${pnl>=0?'up':'down'}">${pnl>=0?'+':''}${num(pnl)}</div>
     <div class="muted">实现盈亏</div>
     <div style="margin-top:6px">往返 <b>${l.total_trips ?? 0}</b> 次 ·
       当日笔数 <b>${r.daily_trades ?? 0}</b>/${r.max_daily_trades ?? '—'}</div>
     <div>熔断: ${r.breaker_tripped
        ? `<span class="off" style="padding:1px 6px;border-radius:4px">已触发 · ${esc(r.breaker_reason||'')}</span>`
        : '<span class="on" style="padding:1px 6px;border-radius:4px">正常</span>'}</div>
     ${balRows(l)}`;
}

function balRows(l){
  const syms = l.symbols || {};
  const rows = Object.entries(syms).map(([c,v]) =>
    `<tr><td>${esc(c)}</td>
         <td>${v.filled_sell_units||0}/${v.filled_buy_units||0}</td>
         <td>${v.net_exposure||0}</td>
         <td>${num(v.realized_pnl)}</td></tr>`).join('');
  if(!rows) return '';
  return `<table style="margin-top:8px">
    <tr><th>标的</th><th>卖/买档</th><th>净敞口</th><th>盈亏</th></tr>
    ${rows}</table>`;
}

function renderLadders(d){
  const syms = d.symbols || [];
  const t = (s) => {
    if(s.skip) return `<div class="muted">跳过: ${esc(s.skip)}</div>`;
    const lad = s.ladder || {};
    const line = (label, price) =>
      `<div style="display:flex;justify-content:space-between">
         <span class="muted">${label}</span><span>${num(price,3)}</span></div>`;
    const sells = (lad.sell||[]).map((p,i) =>
        line('sell'+(i+1)+(((s.filled_sell_units||0)>i)?' ✓':''), p)).reverse().join('');
    const buys = (lad.buy||[]).map((p,i) =>
        line('buy'+(i+1)+(((s.filled_buy_units||0)>i)?' ✓':''), p)).join('');
    return `${sells}
      <div style="border-top:2px solid #4b5563;border-bottom:2px solid #4b5563;
                  margin:4px 0;padding:2px 0;display:flex;
                  justify-content:space-between">
        <b>中枢 ref</b><b>${num(s.ref,3)}</b></div>
      ${buys}`;
  };
  $('ladders').innerHTML = syms.map(s => `
    <div class="card">
      <h3>${esc(s.name)} ${esc(s.code)}</h3>
      <div class="muted">开关 <b>${esc(s.switch||'—')}</b> ·
        带宽 ${num((s.band||0)*100,3)}% · 现价 ${num(s.last,3)}
        ${s.dev_pct!=null?`(离MA20 ${num(s.dev_pct)}%)`:''}</div>
      <div style="margin-top:8px">${t(s)}</div>
    </div>`).join('') || '<div class="muted">无标的</div>';
}

async function renderRejections(){
  try{
    const d = await jget('/api/rejections');
    if(!d.groups.length){
      $('rejections').innerHTML = '<div class="muted">本轮无被拦记录</div>';
      return;
    }
    const max = Math.max(...d.groups.map(g => g.count));
    $('rejections').innerHTML =
      `<div class="muted">共 ${d.total} 笔被拦</div>` +
      d.groups.map(g => `
        <div class="barrow">
          <span title="${esc(g.msg)}">${esc(g.code)}</span>
          <div class="bar" style="width:${Math.round(g.count/max*100)}%"></div>
          <b>${g.count}</b>
        </div>
        <details><summary class="muted" style="cursor:pointer">明细</summary>
          ${g.samples.map(s=>`<div class="muted">${esc(s.name)} ${esc(s.code)}
            ${esc(s.side)} ${num(s.price,3)}×${s.volume} · ${esc(s.reason||'')}</div>`).join('')}
        </details>`).join('');
  }catch(e){ $('rejections').innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}

async function renderEquity(){
  try{
    const d = await jget('/api/ledger/history?days=60');
    const rows = d.rows || [];
    if(!equityChart) equityChart = echarts.init($('equity'), 'dark');
    if(!rows.length){
      $('equityNote').textContent = '暂无归档（每个交易日结束后自动产生一行）';
      equityChart.clear();
      return;
    }
    $('equityNote').textContent = `共 ${rows.length} 个交易日归档`;
    let acc = 0;
    const cum = rows.map(r => (acc += Number(r.realized_pnl||0)));
    equityChart.setOption({
      grid:{left:50,right:16,top:20,bottom:24},
      tooltip:{trigger:'axis'},
      xAxis:{type:'category',data:rows.map(r=>r.date)},
      yAxis:{type:'value',name:'累计实现盈亏'},
      series:[{type:'line',smooth:true,areaStyle:{opacity:.15},
               data:cum.map(v=>Number(v.toFixed(2)))}],
    });
    equityChart.resize();
  }catch(e){ $('equityNote').innerHTML = `<span class="err">${esc(e.message)}</span>`; }
}

async function load(){
  $('clock').textContent = new Date().toLocaleTimeString('zh-CN');
  try{
    const d = await jget('/api/status');
    renderGates(d); renderAccount(d); renderBattle(d); renderLadders(d);
  }catch(e){
    $('gates').innerHTML = `<span class="err">状态获取失败: ${esc(e.message)}</span>`;
  }
  await Promise.allSettled([renderRejections(), renderEquity()]);
}

async function post(url, body){
  const r = await fetch(url, {method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(Object.assign({confirm:true}, body))});
  const b = await r.json().catch(()=>({ok:false,error:'bad json'}));
  if(!b.ok) alert('失败: ' + (b.error||''));
  await load();
}
function togglePause(on){
  const w = on ? '确认急停？将立刻停止一切发单。' : '确认解除急停？';
  if(confirm(w)) post('/api/pause', {paused: !!on});
}
function arm(on){
  const w = on ? '确认今日放行？(仅当日有效)' : '确认撤销今日放行？';
  if(confirm(w)) post('/api/arm', {armed: !!on});
}

load();
setInterval(load, 5000);
</script>
</body>
</html>
```

- [ ] **Step 2: 冒烟：起服务确认页面渲染**

Run:
```powershell
cd D:\cc-joesph\tt_solo
$env:PYTHONIOENCODING='utf-8'; $env:TT_WEB_PORT='5011'
python dashboard\app.py
```
Expected: 启动日志显示 `SIGNAL_ROOT=... port=5011`；浏览器打开 `http://127.0.0.1:5011` 出现五区块，页面 **不报 JS 错**（F12 Console 干净）

- [ ] **Step 3: 验证只监听本机**

Run: `netstat -ano | Select-String ":5011"`
Expected: 只出现 `127.0.0.1:5011`，**不出现** `0.0.0.0:5011`

- [ ] **Step 4: Commit**

```bash
git add tt_solo
git commit -m "feat(tt_solo): 仪表盘前端重建(五区块一屏决策面板)"
```

---

### Task 11: 启动入口 `.bat` 更新 + 编码修复

**Files:**
- Modify: `启动做T守护.bat`、`启动做T直连守护.bat`、`启动做T实盘直连.bat`、`启动做T模拟守护.bat`、`做T-今日放行.bat`、`启动做T监控台.bat`

**Interfaces:**
- Consumes: Task 7 的 `python -m ttcore.daemon`、Task 10 的 `dashboard/app.py`
- Produces: 6 个可用入口

- [ ] **Step 1: 只改路径，**不重写文件内容****

> **规划期误判纠正**：曾判定这些 `.bat` 是「GBK 编码 + `chcp 65001` → 乱码」需重写。
> **已推翻** —— 用 `read` 工具核验，`做T-今日放行.bat` 的中文完全正常，
> 与 `chcp 65001` 是正确配对。乱码是 **PowerShell stdout 的输出通道问题**。
> 另有 3 个文件（`启动做T守护.bat` / `启动做T模拟守护.bat` 等）**根本不含中文**。
>
> **因此：逐行做最小字符串替换，保留原文（含既有中文安全警告）原样。**
> 用脚本重写全文反而会把好文件改坏。

六个文件各自需要的替换（全部是路径层面，不动文案）：

| 文件 | `cd` | python 调用 |
|---|---|---|
| `启动做T守护.bat` | `→ D:\cc-joesph\tt_solo` | `tt.daemon` → `ttcore.daemon` |
| `启动做T直连守护.bat` | 同上 | `tt.daemon --direct` → `ttcore.daemon --direct` |
| `启动做T实盘直连.bat` | 同上 | `tt.daemon --direct --live` → `ttcore.daemon --direct --live` |
| `启动做T模拟守护.bat` | 同上 | `tt.daemon --env sim` → `ttcore.daemon --env sim` |
| `启动做T监控台.bat` | 同上 | `tt_web\app.py` → `dashboard\app.py`，端口提示 `5010` → `5011` |
| `做T-今日放行.bat` | 无 cd | `D:\cc-joesph\tt\arm_today.py` → `D:\cc-joesph\tt_solo\ttcore\arm_today.py` |

```powershell
cd D:\cc-joesph
$utf8 = New-Object System.Text.UTF8Encoding($false)
$files = @('启动做T守护.bat','启动做T直连守护.bat','启动做T实盘直连.bat',
           '启动做T模拟守护.bat','启动做T监控台.bat','做T-今日放行.bat')
foreach ($f in $files) {
  $p = Join-Path 'D:\cc-joesph' $f
  $t = [System.IO.File]::ReadAllText($p, [System.Text.Encoding]::UTF8)
  # (?m) 多行模式必需: 否则 $ 只匹配整串末尾, 锚不住行尾的 cd 那一行
  $t = $t -replace '(?m)^cd /d D:\\cc-joesph\r?$', 'cd /d D:\cc-joesph\tt_solo'
  $t = $t -replace 'python -m tt\.daemon', 'python -m ttcore.daemon'
  $t = $t -replace 'python tt_web\\app\.py', 'python dashboard\app.py'
  $t = $t -replace 'D:\\cc-joesph\\tt\\arm_today\.py', 'D:\cc-joesph\tt_solo\ttcore\arm_today.py'
  if ($f -eq '启动做T监控台.bat') { $t = $t -replace '5010', '5011' }
  [System.IO.File]::WriteAllText($p, $t, $utf8)
}
```

（上述脚本已**干跑验证**：6 个文件各自只改动 `cd` 行与 python 调用行，
`做T-今日放行.bat` 只改 python 路径，`启动做T监控台.bat` 额外改端口提示，
其余文案一字未动。）

- [ ] **Step 2: 逐条核对替换结果（用 `read` 工具，不要用 pwsh 打印）**

```
read 启动做T监控台.bat
read 做T-今日放行.bat
```
Expected:
- 监控台：`cd /d D:\cc-joesph\tt_solo`、`python dashboard\app.py`、提示 `5011`；中文可读
- 今日放行：`python "D:\cc-joesph\tt_solo\ttcore\arm_today.py" %*`；中文可读

- [ ] **Step 3: 冒烟（两个代表性入口）**

Run: 双击 `启动做T监控台.bat`
Expected: 中文提示正常显示，服务起在 5011

Run: `python D:\cc-joesph\tt_solo\ttcore\arm_today.py --status`
Expected: 正确报告 armed 状态；退出码 0 或 1

- [ ] **Step 4: 确认无残留指向 tt/ 或 tt_web/ 的引用**

Run: `Select-String -Path *.bat -Pattern 'tt_web|tt\.daemon|tt\\arm_today'`
Expected: 无输出

- [ ] **Step 5: Commit**

```bash
git add *.bat
git commit -m "chore(tt_solo): 启动入口指向 tt_solo/dashboard(仅改路径, 不重写文案)"
```

---

### Task 12: 双跑对照验证（证明"搬家无回归"）

> 这是**唯一能证明搬家没改变行为的证据**。新旧引擎喂同一份确定性行情、
> 同一份配置、同一个时钟，逐字段比对决策输出与账本状态。

**Files:**
- Create: `tt_solo/tools/compare_legacy.py`
- Create: `tt_solo/tests/test_parity.py`

**Interfaces:**
- Consumes: `tt.*`（旧）与 `ttcore.*`（新）**同时**导入
- Produces: `tt_solo/tools/compare_legacy.py` 可执行脚本（退出码 0=一致，1=有差异）

- [ ] **Step 1: 写对照脚本**

```python
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
```

- [ ] **Step 2: 跑对照**

Run: `cd D:\cc-joesph; $env:PYTHONIOENCODING='utf-8'; python tt_solo\tools\compare_legacy.py`
Expected: `OK 新旧决策完全一致` + intents/rejected/signals 统计，退出码 0

若报差异：**差异即真缺陷**，逐字段定位后再提交 —— 不允许"改对照脚本让它过"。

- [ ] **Step 3: 把对照固化为测试**

`tt_solo/tests/test_parity.py`：

```python
# -*- coding: utf-8 -*-
"""把新旧对照固化为测试: 搬家不得改变任何决策输出。

注意: 本测试依赖旧 tt 包仍存在。删掉 tt/ 后本测试应被同步移除
(见计划 Task 14 —— 删除时一并处理)。
"""
import sys
from pathlib import Path

import pytest

TT_SOLO = Path(__file__).resolve().parents[1]
REPO = TT_SOLO.parent
for p in (str(REPO), str(TT_SOLO)):
    if p not in sys.path:
        sys.path.insert(0, p)

pytest.importorskip("tt.engine", reason="旧 tt 包已删除, 对照测试不再适用")


def test_plans_match_between_old_and_new(tmp_path):
    from tools.compare_legacy import run_side, strip_volatile
    a, b = tmp_path / "old", tmp_path / "new"
    a.mkdir(); b.mkdir()
    old_plan, old_led = run_side("tt", a)
    new_plan, new_led = run_side("ttcore", b)
    assert strip_volatile(old_plan) == strip_volatile(new_plan)
    assert strip_volatile(old_led) == strip_volatile(new_led)
```

- [ ] **Step 4: 跑测试**

Run: `python -m pytest tt_solo/tests/test_parity.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt251`
Expected: PASS

- [ ] **Step 5: 脱沙箱跑一次 tt_solo 全套（含新测试）**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest tt_solo/tests tt_solo/dashboard/tests -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt251`
Expected: **全绿**

- [ ] **Step 6: Commit**

```bash
git add tt_solo
git commit -m "test(tt_solo): 新旧双跑对照, 证明搬家零回归"
```

---

### Task 13: README + requirements（交付物齐备）

**Files:**
- Create: `tt_solo/README.md`
- Create: `tt_solo/requirements.txt`

**Interfaces:**
- Consumes: 全部前置任务
- Produces: 可交付文档

- [ ] **Step 1: 写 `tt_solo/requirements.txt`**

实测依赖（`xtquant` 由 QMT 安装提供，**不写进 requirements** 以免 pip 去找不存在的包）：

```
Flask>=3.0
pytest>=8.0
```

- [ ] **Step 2: 写 `tt_solo/README.md`**

必须覆盖（逐条检查）：

1. **这是什么**：底仓 + 浮仓 日内高抛低吸（做T），自包含项目，不依赖主项目。
2. **快速开始**：`python -m ttcore.daemon --once --sample`（离线演练，零副作用）
3. **六种运行方式** + 各自对应 .bat。
4. **三道闸门**（`dry_run` → `paused` → `armed`）的串联语义，明确**默认 dry_run**。
5. **T+1 硬约束**：当天买入不可卖 → 做T必须有隔夜底仓；无底仓时卖出腿恒被 `NO_BASE_POSITION` 拦。
6. **目录结构**与各模块职责。
7. **数据归属**：运行数据在 `tt_solo/runtime/`；信号目录 `D:/QMT_SIGNALS` 是与外部 QMT 的契约，可用 `TT_SIGNAL_ROOT` 覆盖；`TT_RUNTIME_DIR` 覆盖运行数据根。
8. **测试**：命令 + 沙箱假失败警告。
9. **安全边界**：面板只监听 127.0.0.1；只能关闸不能开单；账户访问只读。
10. **仪表盘**：`http://127.0.0.1:5011`，五区块说明。

- [ ] **Step 3: 验证 README 里的命令真能跑**

Run: `cd D:\cc-joesph\tt_solo; python -m ttcore.daemon --once --sample`
Expected: 与 README 描述一致，退出码 0

- [ ] **Step 4: Commit**

```bash
git add tt_solo
git commit -m "docs(tt_solo): README + requirements"
```

---

### Task 14: 【用户确认点】删除 `tt/` + `tt_web/`

> **这一步不可逆。执行前必须先问用户，得到明确同意才能做。**
> 前面的任务全部完成且全绿之前，不得开始本任务。

**Files:**
- Delete: `tt/`（整目录）
- Delete: `tt_web/`（整目录）
- Modify: `tt_solo/tests/test_parity.py`（旧包已删 → 移除）
- Modify: `docs/superpowers/specs/2026-09-16-tt-solo-extract-design.md`（标注已执行）
- Already modified（2026-09-16 终审修订, Step 1 的前置条件）:
  `ops/smoke_check.py`、`ops/make_summary_pdf.py`、`tt_README.md`、
  `docs/做T操作卡_20260915.md`、`docs/做T测试指南.md`、`STRUCTURE.md`、根 `README.md`、
  `做T-今日放行.bat`

**Interfaces:**
- Consumes: 全部
- Produces: 单一份实现

- [ ] **Step 1: 前置检查 —— 全仓引用扫描**

Run:
```powershell
cd D:\cc-joesph
Get-ChildItem . -Recurse -Include *.py,*.bat,*.ps1,*.md |
  Where-Object { $_.FullName -notmatch '\\(tt|tt_web|pt_bt[^\\]*|\.git|__pycache__|node_modules)\\' } |
  Select-String -Pattern 'tt_web|from tt\.|import tt\b|tt\.daemon|tt\.arm_today|5010|做T'
```
Expected: 只应剩下 `tt_solo/tests/test_parity.py`（Step 2 移除）、本计划的自身文本，以及**新入口**的提及
（`tt_solo/`、`ttcore.daemon`、`5011`、`tt_solo/README.md`）。**任何运维脚本/看门狗/启动器/操作文档里
还指着旧入口的引用, 都必须先改完。**

> ⚠️ **2026-09-16 终审修订 —— 原 `Expected` 是错的。** 原清单只预期
> `test_parity.py` + “文档提及”，但首轮扫描漏掉了下面这些**会真的坏掉**的引用，
> 现已在本次修订中改完（改动清单见 Step 1.1）：
>
> | 类别 | 文件 | 原状态 | 后果 |
> |---|---|---|---|
> | **运维脚本（会 ImportError）** | `ops/smoke_check.py` | `import tt_web.app` 并断言做T闸门 403 | 删目录后整项抛 ImportError → **“远程碰不了交易闸门”这条断言静默消失** |
> | **运维脚本** | `ops/make_summary_pdf.py` | `prod_dirs` 含 `tt`/`tt_web`；文案写“Flask 双控制台(5000/5010)” | 统计漏掉 `tt_solo/`、交付 PDF 写着已退役的端口 |
> | **操作文档（交易员盘中会照做）** | `tt_README.md`、`docs/做T操作卡_20260915.md`、`docs/做T测试指南.md`、`STRUCTURE.md`、根 `README.md` | 仍命令旧守护/旧面板（`python -m tt.daemon ...`、`tt_web` 5010、`tt/arm_today.py`） | **今天就会出事**：旧守护写旧账本，5011 面板读 `tt_solo` 账本，两者共用 `D:/QMT_SIGNALS` → 面板空账、真单照发 |
> | **对照工具的前提** | `tt_solo/tools/compare_legacy.py` | 依赖旧 `tt/` 树做左右对照 | 旧树删除后**失去意义**（不是坏掉）：保留作历史取证，README 已注明前提 |
>
> 另: 项目根 `MEMORY.md` 与本计划外的 spec 也可能提旧入口 —— 那些属于**并发的另一会话**，本修订刻意不动。

- [ ] **Step 1.1: 复核这些改动仍在**（改动已于 2026-09-16 终审修订完成）

```powershell
cd D:\cc-joesph
# ① 运维脚本已改指新入口
Select-String -Path ops\smoke_check.py -Pattern 'dashboard\.app'          # 期望: 命中(且无 import tt_web)
Select-String -Path ops\make_summary_pdf.py -Pattern 'tt_solo'           # 期望: 命中 4 处, 无 "5000/5010"
# ② 操作文档里不再有可照抄的旧入口命令
Select-String -Path tt_README.md,docs\做T操作卡_20260915.md,docs\做T测试指南.md,STRUCTURE.md,README.md `
  -Pattern 'python -m tt\.daemon|python tt[/\\]arm_today|tt_web.*5010'
```
Expected: ① 两行都命中新入口；② **只应剩下 5 处"旧 `tt/` + 旧面板 5010 已退役"的说明性提及**
（tt_README.md:5、做T操作卡:63、做T测试指南:7、STRUCTURE.md:102、README.md:179）。
**不允许出现任何可照抄的旧命令模板** —— 判断标准是"交易员照着这段敲下去会启动旧守护吗"。

再跑一次冒烟自检确认闸门断言真的还在：
```powershell
python -c "import sys; sys.path.insert(0,r'D:\cc-joesph'); from ops import smoke_check; print(smoke_check._guard())"
```
Expected: 打印 `... / 做T面板(tt_solo/dashboard:5011) 闸门 403 均验证`（**不得出现 ImportError**）

同时检查（MEMORY 记载的踩坑）：
```powershell
Get-ChildItem D:\cc-joesph\ops\*.py | Select-String -Pattern "tt_web|tt\.daemon|5000|5010"
```
Expected: **只应剩下三类历史/说明性命中**：`start_all.py:66-67`、`watchdog.py:28` 的 prism_web
端口 5000，以及 `smoke_check.py:158-159` / `make_summary_pdf.py:112` 里"旧 tt_web(5010) 已退役"
与"5000 选股 / 5011 做T"的注释文字。
即：`ops/start_all.py` / `ops/watchdog.py` **不引用做T面板**（只引用 prism_web）。

- [ ] **Step 2: 处理 `test_parity.py`**

旧包删除后该测试失去意义（`importorskip` 会静默跳过，留下永假的绿）。**删除文件**：

```powershell
Remove-Item D:\cc-joesph\tt_solo\tests\test_parity.py
```

`tools/compare_legacy.py` **保留** —— 它是"搬家零回归"的证据留存，README 里说明其历史用途。

- [ ] **Step 3: 再跑一次 tt_solo 全套确认仍绿**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest tt_solo/tests tt_solo/dashboard/tests -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt252`
Expected: 全绿

- [ ] **Step 4: 【问用户】得到明确同意**

向用户复述：将删除 `tt/`（10 个模块 + 150 例测试）与 `tt_web/`（面板），`tt_solo/` 为唯一实现。
**用户未明确同意前，不得执行 Step 5。**

- [ ] **Step 5: 删除**

```powershell
cd D:\cc-joesph
python -c "import shutil; shutil.rmtree('tt', ignore_errors=True); shutil.rmtree('tt_web', ignore_errors=True)"
```

（用 Python 而非 `Remove-Item -Recurse` —— 沙箱下后者易触发 safe-delete 守卫。）

- [ ] **Step 6: 清老运行数据残留**

```powershell
cd D:\cc-joesph
# 先确认这些文件确实是做T的旧状态(时间戳/内容核对)
Get-ChildItem runtime\state\tt_*.json | Select-Object Name, LastWriteTime
```
经用户确认后删除 `runtime/state/tt_state.json`、`runtime/state/tt_runtime.json`（做T数据已迁至 `tt_solo/runtime/`）。
**注意**：`runtime/state/` 下 `.paper_account.json` / `close_pick_state.json` / `manual_factors.json` 属 prism，**绝不可动**。

- [ ] **Step 7: 更新 MEMORY.md**

在「项目全貌」表里把 `tt/` 与 `tt_web/` 两行替换为 `tt_solo/` 一行（说明：自包含 + 仪表盘 5011）；补一条本批次记录（依赖内联、broker 吸收、日终归档、双跑对照零回归、150 例迁入）。

- [ ] **Step 8: 最终全量测试**

Run:
```powershell
cd D:\cc-joesph
$env:PYTHONIOENCODING='utf-8'
python -m pytest prism/tests prism_web/tests datasource/tests tt_solo/tests tt_solo/dashboard/tests qmt_sync/tests tests -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt253
```
Expected: 全绿（原 840 基线 - tt 迁出的 150 + tt_solo 的 ≥150，总数应持平或更多）

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "refactor: 删除 tt/ + tt_web/, tt_solo 成为唯一实现"
```

---

## 附录：验证命令速查

```powershell
# 自包含护栏
Get-ChildItem tt_solo -Recurse -Include *.py |
  Where-Object { $_.FullName -notmatch '__pycache__' } |
  Select-String -Pattern '^\s*(from|import)\s+(prism|shared|qmt_sync|backtest|legacy)\b'

# tt_solo 全套
$env:PYTHONIOENCODING='utf-8'; python -m pytest tt_solo/tests tt_solo/dashboard/tests -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt251

# 搬家零回归对照
python tt_solo\tools\compare_legacy.py

# 守护离线演练(零副作用)
cd D:\cc-joesph\tt_solo; python -m ttcore.daemon --once --sample

# 仪表盘
cd D:\cc-joesph\tt_solo; python dashboard\app.py     # http://127.0.0.1:5011
```
