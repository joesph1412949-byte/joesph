# 分级写护栏实现计划（2026-09-15）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** prism_web 与 tt_web 各加一个 before_request 钩子——远程（经 CF 隧道/公网）可选股/新建策略/刷新涨停池/浏览全部看板，但交易闸门类写操作（设默认策略/手填因子/模拟盘暂停恢复/绩效回填/做T急停/做T放行）仅限本机，命中返回 403 + 明确文案。

**Architecture:** 判据收口在 `is_local_request()` 一个函数（CF-Connecting-IP 头存在 → 远程；无头且 remote_addr 为 loopback/RFC1918 → 本机）；两个 app 各一个 before_request 钩子按 `request.endpoint`（函数名）匹配模块级 `_LOCAL_ONLY` 冻结集合；前端零改动（被拦弹后端文案）。Spec: `docs/superpowers/specs/2026-09-15-tiered-guard-design.md`。

**Tech Stack:** Flask before_request；pytest（prism_web 既有 client fixture；tt_web 新建 client fixture）；零新依赖。

## Global Constraints

- **ponytail 阶梯**：最懒可行方案；绝不简化掉**护栏判据本身**（CF-Connecting-IP 优先、loopback/RFC1918 判定）、测试离线确定性、原子写语义；`# ponytail:` 标记刻意取舍
- **判据唯一真相**：`is_local_request()`——有 `CF-Connecting-IP` 头 → 远程；无头 + `remote_addr` ∈ {127.0.0.1, ::1} 或 RFC1918（10.x/172.16-31.x/192.168.x）→ 本机；其余 → 远程
- **默认全放行**：仅 spec 权限矩阵列出的 4+2 个函数名收本机（prism_web: `api_strategy_activate`/`manual`/`api_automation`/`perf_backfill`；tt_web: `api_pause`/`api_arm`）；将来调整只改集合
- **前端零改动**；被拦响应 = 403 JSON `{"ok": false, "error": "此操作仅限本机执行(交易闸门类)"}`（tt_web 文案同）
- 不采信 `X-Forwarded-For`（可伪造）；threat model = 误触/越权点按，非对抗攻击
- 本地 commit 随意，**绝不 push**（push 前问用户）
- 全量测试命令：`python -m pytest prism/tests prism_web/tests datasource/tests tt/tests qmt_sync/tests tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_btNN`（基线 **837 绿**，NN 用 240 起）

---

### Task G1: prism_web 分级护栏（is_local_request + before_request + 5 组测试）

**Files:**
- Modify: `prism_web/app.py`（钩子插在 `@app.route("/")` 之前；模块级常量放其上方）
- Test: `prism_web/tests/test_app.py`（末尾追加）

**Interfaces:**
- Consumes: 既有 `app`/`logger`/`request`/`jsonify`；既有 client fixture（`app_module.app.config["TESTING"] = True; test_client()`）
- Produces: `is_local_request() -> bool`（将来切 CF Access 邮箱白名单只改此函数）；`_LOCAL_ONLY` frozenset（函数名集合）；被拦响应契约 = 403 JSON `{"ok": false, "error": "此操作仅限本机执行(交易闸门类)"}`（**前端会读取 error 字段弹窗，文案与键名不可改**）

- [ ] **Step 1: 写失败测试**（test_app.py 末尾追加；判定函数按 `(cf_ip, remote_addr)` 两参实现便于直测）

```python
# ---------------- 分级写护栏(spec 2026-09-15-tiered-guard) ----------------

def test_guard_is_local_request():
    """判定函数单元测试: CF头→远程; loopback/RFC1918→本机; 公网→远程。"""
    assert app_module.is_local_request(None, "127.0.0.1") is True
    assert app_module.is_local_request(None, "::1") is True
    assert app_module.is_local_request(None, "192.168.1.50") is True
    assert app_module.is_local_request(None, "10.0.0.3") is True
    assert app_module.is_local_request(None, "172.20.1.5") is True
    assert app_module.is_local_request(None, "100.64.1.2") is False
    assert app_module.is_local_request(None, "203.0.113.7") is False
    assert app_module.is_local_request("1.2.3.4", "127.0.0.1") is False


def test_guard_blocks_sensitive_for_remote(client):
    """远程(带CF头)点敏感路由 → 403 + 文案。"""
    env = {"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "203.0.113.7"}
    for url, body in (("/api/strategies/full_factor_v1/activate", {}),
                      ("/api/stock/600519/manual", {"X": 1}),
                      ("/api/automation", {"paused": True}),
                      ("/api/perf/backfill", {})):
        r = client.post(url, json=body, environ_base=env)
        assert r.status_code == 403, (url, r.status_code)
        assert "仅限本机" in r.get_json()["error"]


def test_guard_allows_screen_create_limitup_for_remote(client, monkeypatch):
    """远程可用: 选股/新建策略/涨停池(护栏不拦, 走到路由既有逻辑)。"""
    env = {"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "203.0.113.7"}
    # 选股: 放行到 QMT 检查(测试环境 QMT 未连接 → 400, 证明未在护栏层被拦)
    r = client.post("/api/screen", json={}, environ_base=env)
    assert r.status_code == 400            # 400=QMT检查, 而非护栏 403
    # 新建策略: 放行到参数校验(空 payload → 400 校验失败, 而非护栏 403)
    r = client.post("/api/strategies/create", json={}, environ_base=env)
    assert r.status_code == 400
    # 涨停池 POST: 放行到 QMT 检查(400)
    r = client.post("/api/market/limitup", environ_base=env)
    assert r.status_code == 400


def test_guard_local_full_access(client):
    """本机 loopback 无 CF 头: 敏感路由放行到既有逻辑(非 403)。"""
    r = client.post("/api/automation", json={"paused": True},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code != 403            # 走到 automation 既有逻辑
    # 清理: automation 写的 paused 文件删掉, 不影响后续用例/守护
    import prism.trader as trader
    p = getattr(trader, "PAUSE_FILE", None)
    if p and Path(p).exists():
        Path(p).unlink()


def test_guard_lan_ip_full_access(client):
    """局域网来源(无 CF 头) = 本机档: 敏感路由放行。"""
    env = {"REMOTE_ADDR": "192.168.1.50"}
    r = client.post("/api/automation", json={"paused": True}, environ_base=env)
    assert r.status_code != 403
    import prism.trader as trader
    p = getattr(trader, "PAUSE_FILE", None)
    if p and Path(p).exists():
        Path(p).unlink()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q -k guard --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt240`
Expected: FAIL（`AttributeError: no attribute 'is_local_request'` + 敏感路由得 200/400 而非 403）

- [ ] **Step 3: 实现**（app.py，插在 `@app.route("/")` 之前）

```python
# ================= 分级写护栏(2026-09-15, spec tiered-guard) =================
# 远程(经 CF 隧道/公网)可选股/新建策略/刷新涨停池; 交易闸门类写操作仅本机。
# 判据唯一真相(spec §判定): 有 CF-Connecting-IP 头=经隧道=远程; 无头且
# remote_addr 为 loopback/RFC1918=本机。不采信 X-Forwarded-For(可伪造)。
# 将来切 CF Access 邮箱白名单: 只改 is_local_request()。
def _is_rfc1918(ip):
    return ip.startswith(("10.", "192.168.")) or \
        ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31


def is_local_request(cf_ip, remote_addr):
    """True=本机/局域网(全功能); False=公网/隧道来的(交易闸门收本机)。"""
    if cf_ip:
        return False                      # 经 CF 边缘转发 = 公网来源
    ra = remote_addr or ""
    return ra in ("127.0.0.1", "::1") or _is_rfc1918(ra)


_LOCAL_ONLY = frozenset({"api_strategy_activate", "manual",
                         "api_automation", "perf_backfill"})


@app.before_request
def _local_only_guard():
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None
    if is_local_request(request.headers.get("CF-Connecting-IP"),
                        request.remote_addr):
        return None
    if request.endpoint in _LOCAL_ONLY:
        logger.warning("分级护栏拦截: %s %s (CF-IP=%s)", request.method,
                       request.path, request.headers.get("CF-Connecting-IP"))
        return jsonify({"ok": False,
                        "error": "此操作仅限本机执行(交易闸门类)"}), 403
    return None
```

（注意：`_is_rfc1918` 需在 `is_local_request` 里以 `_is_rfc1918(remote_addr)` 调用——实现时函数体按此意图写：先判 CF 头，再判 remote_addr；上面 `ra` 变量对应 `remote_addr` 形参。）

- [ ] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt240`
Expected: 全 PASS（既有全部 POST 测试 = 本机 client 默认 127.0.0.1 无 CF 头 → 不受影响）

- [ ] **Step 5: 全量回归**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests datasource/tests tt/tests qmt_sync/tests tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt241`
Expected: **840 passed**（837 + 5 新护栏用例；若 tt_web 钩子在 G2 才加则仍 840，数量以实际为准报告）

- [ ] **Step 5: 提交**

```bash
git add prism_web/app.py prism_web/tests/test_app.py
git commit -m "feat: prism_web 分级写护栏(远程可选股/建策略; 交易闸门仅本机; CF-Connecting-IP判据)"
```

### Task G2: tt_web 交易闸门护栏 + client fixture + 2 测试

**Files:**
- Modify: `tt_web/app.py`（同样在第一个 `@app.route` 前插钩子；复用同款判定——**直接从 prism_web import 还是复制？** 决策：tt_web 已 `from shared.common import STATE_DIR`，但判据函数与 prism_web 的是同一逻辑两份小代码——ponytail「one implementation」与「两 app 独立」权衡后：**放 `shared/common.py`**（`is_local_request(cf_ip, remote_addr)` + `_is_rfc1918(ip)`），prism_web 与 tt_web 都 import（B 已确立 shared 收敛先例；纯函数、ASCII 注释约束可满足）
- Modify: `shared/common.py`（新增 `is_local_request`——**注释保持 ASCII**（文件头规则））
- Test: `tt_web/tests/__init__.py`(空) + `tt_web/tests/test_guard.py`（新建, 自带 client fixture——tt_web 无既有测试基建）

**Interfaces:**
- Consumes: G1 的 `is_local_request` 逻辑（本任务迁到 shared，prism_web 改为 import）
- Produces: shared.common.is_local_request(cf_ip, remote_addr)；tt_web `_LOCAL_ONLY = frozenset({"api_pause", "api_arm"})`

- [ ] **Step 1: 判据函数迁到 shared**（shared/common.py 追加，ASCII 注释）

```python
# ---------------------------------------------------------------- guard
def _is_rfc1918(ip):
    """RFC1918 private ranges (LAN = trusted local tier)."""
    if ip.startswith(("10.", "192.168.")):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def is_local_request(cf_ip, remote_addr):
    """Tiered-write guard predicate (spec 2026-09-15-tiered-guard).

    CF-Connecting-IP present  -> public tunnel origin -> NOT local.
    No CF header and loopback/RFC1918 remote -> local machine.
    Future: switch to CF Access email allowlist by editing this only."""
    if cf_ip:
        return False
    ra = remote_addr or ""
    return ra in ("127.0.0.1", "::1") or _is_rfc1918(ra)
```

- [ ] **Step 2: prism_web 改为 import**（删 G1 里本地定义的 `_is_rfc1918`/`is_local_request`，`_local_only_guard` 改调 shared 版；**钩子其余逻辑/文案不动**；重跑 G1 测试应仍全绿——这是"实现搬位置、行为零漂移"的回归证明）

- [ ] **Step 3: tt_web 钩子**（tt_web/app.py，`@app.route("/")` 之前插入）

```python
# 分级写护栏(2026-09-15): 做T急停/每日放行是真实交易闸门, 仅限本机。
from shared.common import is_local_request as _is_local  # noqa: E402

_LOCAL_ONLY = frozenset({"api_pause", "api_arm"})


@app.before_request
def _local_only_guard():
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None
    if _is_local(request.headers.get("CF-Connecting-IP"),
                 request.remote_addr):
        return None
    if request.endpoint in _LOCAL_ONLY:
        logging.getLogger("tt_web").warning(
            "远程拦截交易闸门: %s %s", request.method, request.path)
        return jsonify({"ok": False,
                        "error": "此操作仅限本机执行(交易闸门类)"}), 403
    return None
```

- [ ] **Step 4: tt_web 测试**（新建 `tt_web/tests/__init__.py` 空文件 + `tt_web/tests/test_guard.py`）

```python
# -*- coding: utf-8 -*-
"""tt_web 交易闸门护栏: 远程禁 pause/arm, 本机放行(全离线)。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import tt_web.app as app_module


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_remote_blocked_pause_arm(client):
    """公网来源(无 CF 头, remote=100.x 形态) → 403。"""
    env = {"REMOTE_ADDR": "100.64.1.2"}
    for url in ("/api/pause", "/api/arm"):
        r = client.post(url, json={"confirm": "true"}, environ_base=env)
        assert r.status_code == 403
        assert "仅限本机" in r.get_json()["error"]


def test_local_pause_arm_ok(client, monkeypatch, tmp_path):
    """本机 → 放行到既有逻辑(404/200 均可, 唯非 403); 闸门文件落 tmp。"""
    monkeypatch.setattr(app_module, "PAUSE_FILE", tmp_path / "paused")
    monkeypatch.setattr(app_module, "ARM_FILE", tmp_path / "armed.txt")
    for url in ("/api/pause", "/api/arm"):
        r = client.post(url, json={"confirm": "true"},
                        environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert r.status_code != 403
```

（注: 若 tt_web/app.py 里闸门路径常量名不是 `PAUSE_FILE`/`ARM_FILE`，先 grep 实名替换——断言只要求"非 403"，路径打桩方式按实际调整。）

- [ ] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest tt_web/tests prism_web/tests/test_app.py -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt242`
Expected: 全 PASS

- [ ] **Step 5: 全量回归 + 提交**

```bash
python -m pytest prism/tests prism_web/tests datasource/tests tt/tests qmt_sync/tests tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt243
git add shared/common.py prism_web/app.py tt_web/app.py tt_web/tests/
git commit -m "feat: 分级写护栏(远程可选股/建策略; 设默认策略/手填/模拟盘/做T闸门仅本机; CF-Connecting-IP判据)"
```

### Task G3: 收尾（smoke + 文档）

**Files:**
- Modify: `ops/smoke_check.py`（新增第 6 关：护栏语义验证）
- Modify: `docs/reports/ponytail精简审计_20260915.md` 不动；新增一节到 spec 同目录即可（无）

- [ ] **Step 1: smoke_check 加护栏关**（_routes 后追加一节）

```python
def _guard():
    import prism_web.app as appmod
    client = appmod.app.test_client()
    env = {"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "203.0.113.7"}
    checks = []
    # 敏感路由必须 403
    for url in ("/api/strategies/full_factor_v1/activate",):
        checks.append((url, client.post(url, json={}, environ_base=env)))
    blocked = [u for u, r in checks if r.status_code == 403]
    if len(blocked) != len(checks):
        raise AssertionError("敏感路由未被拦: %s" % checks)
    # 远程选股必须放行(非 403)
    r = client.post("/api/screen", json={}, environ_base=env)
    if r.status_code == 403:
        raise AssertionError("远程选股被误拦")
    return "敏感路由 403 ✓, 远程选股放行 ✓"


check("6. 分级护栏", _guard)
```

- [ ] **Step 2: 跑冒烟确认 6/6**

Run: `$env:PYTHONIOENCODING='utf-8'; python ops/smoke_check.py`
Expected: 6/6 通过

- [ ] **Step 3: MEMORY.md 更新**（护栏条目加入「Tailscale 私享分享」条目之后：分级写护栏判据/权限矩阵/文件位置；测试基线数字更新）

- [ ] **Step 4: 提交**

```bash
git add ops/smoke_check.py MEMORY.md
git commit -m "docs+ops: 分级护栏冒烟关+MEMORY同步(权限矩阵/判据/基线)"
```
