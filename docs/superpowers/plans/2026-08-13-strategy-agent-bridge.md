# 选股策略 ↔ Vibe-Trading agent 桥接 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通 strategy_web(选股)与 Vibe-Trading agent:agent 读选股快照 + 主动重跑选股,一键启动脚本拉起全链路,不依赖 Claude Code。

**Architecture:** strategy_web(Flask, Py3.7, :5000)选股后落盘 JSON 快照;Vibe-Trading 新增 `strategy_screen` 只读工具(标准库 urllib)通过 HTTP 读快照/触发重跑/查状态;`start_all.py` + `start_all.bat` 一键拉起四进程。

**Tech Stack:** Flask / urllib(标准库) / subprocess / Python 3.7(strategy_web 运行) + 3.12(测试) + venv 3.12(Vibe-Trading)

## 全局约束(每个任务都隐含遵守)

- 只读、本地、无下单、无外发;所有服务只监听 127.0.0.1。
- agent 工具任何异常 fail-open:返回 `{"ok": false, ...}` JSON,绝不抛异常。
- agent 工具遵循 qmt_account_tool 既有模式:BaseTool 子类 + `execute(**kwargs)` + `is_readonly=True`。
- 不 push 到任何 remote。
- Python 版本严格按 spec 环境约束表(strategy_web 运行用 Py3.7 + xtdata;测试用 Py3.12 mock)。
- 数据契约见 spec(`docs/superpowers/specs/2026-08-13-strategy-agent-bridge-design.md`),字段名不得偏离。

## 测试运行环境(计划内命令统一用这些)

| 目标 | 解释器 | 命令 |
|------|--------|------|
| strategy_web 测试 | Python 3.12 | `cd d:/cc-joesph/strategy_web && C:/Users/28037/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q` |
| Vibe-Trading 工具测试 | venv 3.12 | `cd D:/Vibe-Trading && .venv/Scripts/python.exe -m pytest tests/test_strategy_screen_tool.py -q` |

---

### Task 0: 给 Python 3.7 装 strategy_web 运行依赖

**Files:**
- 无代码改动(纯环境准备)

**说明:** strategy_web 运行时需 `xtdata`(Py3.7 已有)+ flask/requests/numpy/pandas。Python 3.7 已 EOL,numpy/pandas 必须 pin 到兼容旧版本,否则装不上或运行崩。

- [ ] **Step 1: 安装依赖(兼容 Python 3.7 的版本)**

```bash
/c/Users/28037/AppData/Local/Programs/Python/Python37/python.exe -m pip install \
  "numpy==1.21.6" "pandas==1.3.5" "flask==2.2.5" "requests==2.31.0" \
  -i https://pypi.tuna.tsinghua.edu.cn/simple
```

- [ ] **Step 2: 验证导入**

```bash
PYTHONPATH=/d/QMT/bin.x64/Lib/site-packages /c/Users/28037/AppData/Local/Programs/Python/Python37/python.exe -c "import xtquant.xtdata, flask, numpy, pandas, requests; print('all OK')"
```

Expected: 打印 `all OK`。

- [ ] **Step 3: 验证 strategy_web 可 import(离线, 不连 QMT)**

```bash
cd /d/cc-joesph/strategy_web && PYTHONPATH=/d/QMT/bin.x64/Lib/site-packages /c/Users/28037/AppData/Local/Programs/Python/Python37/python.exe -c "import app; print('app imports OK')"
```

Expected: 打印 `app imports OK`(xtdata 在 try/except 内, 连接与否不影响 import)。

> 若 Step 1 报 numpy/pandas 无兼容 wheel(3.7),改用 `numpy==1.20.3 pandas==1.2.5`;若 factors.py/fundamental.py 用到 pandas 2.x 新 API,需在 Task 1/2 实施时按 1.3.5 兼容改写(记录为 deviation)。

---

### Task 1: strategy_web 选股快照落盘

**Files:**
- Modify: `d:/cc-joesph/strategy_web/app.py`
- Test: `d:/cc-joesph/strategy_web/tests/test_app.py`

**Interfaces:**
- Produces: 模块级 `SNAPSHOT_PATH`(Path, 可被测试 monkeypatch)+ `_save_snapshot(result) -> dict`(落盘并返回快照)。供 Task 2 的 `/api/screen/latest` 读取。

- [ ] **Step 1: 写失败测试**

在 `tests/test_app.py` 追加(沿用已有 FakeScreen + client fixture):

```python
def test_screen_writes_snapshot(client, tmp_path, monkeypatch):
    import json as _json
    snap = tmp_path / "screen_result.json"
    monkeypatch.setattr(app_module, "SNAPSHOT_PATH", snap)
    r = client.post("/api/screen")
    assert r.status_code == 200
    assert snap.is_file()
    data = _json.loads(snap.read_text(encoding="utf-8"))
    assert data["generated_at"]          # 非空时间戳
    assert data["environment_ok"] is True
    assert data["candidates"][0]["code"] == "002859.SZ"
    assert data["summary"]["a_count"] == 1
    # 原子写: 不残留 tmp 文件
    assert not (tmp_path / "screen_result.json.tmp").exists()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd d:/cc-joesph/strategy_web && C:/Users/28037/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/test_app.py::test_screen_writes_snapshot -q`
Expected: FAIL — `AttributeError: module 'app' has no attribute 'SNAPSHOT_PATH'`(或快照未生成)。

- [ ] **Step 3: 实现落盘**

在 `app.py` 顶部 import 区补充 `os`、`json`:

```python
import os
import json as _json
```

在 `manual_store_obj = ManualStore()` 之后新增:

```python
# 选股结果快照路径(供 /api/screen/latest 秒读; 测试可 monkeypatch)
SNAPSHOT_PATH = Path(__file__).parent / "screen_result.json"


def _save_snapshot(result: dict) -> dict:
    """选股成功后落盘快照(原子写), 供 latest 秒读。"""
    snapshot = {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "market": result.get("market"),
        "environment_ok": result.get("environment_ok"),
        "candidates": result.get("candidates"),
        "summary": result.get("summary"),
    }
    tmp = SNAPSHOT_PATH.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, SNAPSHOT_PATH)
    return snapshot
```

在 `screen()` 里 `result = _get_screen_runner().run()` 之后、`return jsonify(result)` 之前插入:

```python
        _save_snapshot(result)
```

- [ ] **Step 4: 运行测试确认通过**

Run 同上命令。Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add strategy_web/app.py strategy_web/tests/test_app.py
git commit -m "feat(strategy_web): snapshot screen result to screen_result.json"
```

---

### Task 2: strategy_web 新增 /api/screen/latest 端点

**Files:**
- Modify: `d:/cc-joesph/strategy_web/app.py`
- Test: `d:/cc-joesph/strategy_web/tests/test_app.py`

**Interfaces:**
- Consumes: Task 1 的 `SNAPSHOT_PATH`、`_save_snapshot`。
- Produces: `GET /api/screen/latest` → `{"ok": true, "generated_at", "market", "environment_ok", "summary", "candidates"}`(200) 或 `{"ok": false, "error": "..."}`(404/500)。

- [ ] **Step 1: 写失败测试**

```python
def test_screen_latest_returns_snapshot(client, tmp_path, monkeypatch):
    import json as _json
    snap = tmp_path / "screen_result.json"
    monkeypatch.setattr(app_module, "SNAPSHOT_PATH", snap)
    client.post("/api/screen")  # 先跑一次落盘
    r = client.get("/api/screen/latest")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["generated_at"]
    assert data["candidates"][0]["code"] == "002859.SZ"


def test_screen_latest_no_snapshot(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "SNAPSHOT_PATH", tmp_path / "nope.json")
    r = client.get("/api/screen/latest")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd d:/cc-joesph/strategy_web && C:/Users/28037/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/test_app.py::test_screen_latest_returns_snapshot tests/test_app.py::test_screen_latest_no_snapshot -q`
Expected: FAIL — 404(路由不存在)。

- [ ] **Step 3: 实现端点**

在 `app.py` 的 `screen()` 之后新增:

```python
@app.route("/api/screen/latest")
def screen_latest():
    if not SNAPSHOT_PATH.is_file():
        return jsonify({"ok": False, "error": "尚未选股, 请先调用 /api/screen"}), 404
    try:
        data = _json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        data["ok"] = True
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": "快照读取失败: %r" % e}), 500
```

- [ ] **Step 4: 运行测试确认通过 + 全量回归**

```bash
cd d:/cc-joesph/strategy_web && C:/Users/28037/AppData/Local/Programs/Python/Python312/python.exe -m pytest tests/ -q
```

Expected: PASS(95 + 3 新增 = 98)。

- [ ] **Step 5: 提交**

```bash
git add strategy_web/app.py strategy_web/tests/test_app.py
git commit -m "feat(strategy_web): GET /api/screen/latest returns latest snapshot"
```

---

### Task 3: Vibe-Trading 新增 strategy_screen 工具

**Files:**
- Create: `D:/Vibe-Trading/agent/src/tools/strategy_screen_tool.py`
- Test: `D:/Vibe-Trading/agent/tests/test_strategy_screen_tool.py`

**Interfaces:**
- Consumes: `src.agent.tools.BaseTool`(name/description/parameters/is_readonly + `execute(**kwargs)`)。HTTP 依赖 strategy_web 的 `/api/screen/latest`、`/api/screen`、`/api/health`。
- Produces: 工具 `name="strategy_screen"`,operations latest/run/health,返回 `{"ok": ...}` JSON(`ensure_ascii=False`)。

- [ ] **Step 1: 写失败测试**

新建 `test_strategy_screen_tool.py`:

```python
import json

from src.tools.strategy_screen_tool import StrategyScreenTool


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body.encode("utf-8")


def _patch_urlopen(monkeypatch, handler):
    import src.tools.strategy_screen_tool as mod
    monkeypatch.setattr(mod.urllib.request, "urlopen", handler)


def test_latest_returns_snapshot(monkeypatch):
    body = json.dumps({"ok": True, "generated_at": "2026-08-13 10:00:00",
                       "environment_ok": True, "summary": {"a_count": 1},
                       "candidates": [{"code": "600000.SH", "scores": {"composite": 5.0}}]})
    def _handler(req, timeout=None):
        assert req.full_url == "http://127.0.0.1:5000/api/screen/latest"
        return _FakeResp(200, body)
    _patch_urlopen(monkeypatch, _handler)
    out = json.loads(StrategyScreenTool().execute(operation="latest"))
    assert out["ok"] is True and out["candidates"][0]["code"] == "600000.SH"


def test_latest_top_n_truncates(monkeypatch):
    cands = [{"code": "C%d.SH" % i, "scores": {"composite": float(10 - i)}} for i in range(3)]
    body = json.dumps({"ok": True, "candidates": cands})
    def _handler(req, timeout=None):
        return _FakeResp(200, body)
    _patch_urlopen(monkeypatch, _handler)
    out = json.loads(StrategyScreenTool().execute(operation="latest", top_n=2))
    assert len(out["candidates"]) == 2


def test_run_posts_and_truncates(monkeypatch):
    body = json.dumps({"environment_ok": True, "candidates": [{"code": "600000.SH"}]})
    def _handler(req, timeout=None):
        assert req.get_method() == "POST"
        assert timeout == 180
        return _FakeResp(200, body)
    _patch_urlopen(monkeypatch, _handler)
    out = json.loads(StrategyScreenTool().execute(operation="run"))
    assert out["environment_ok"] is True


def test_health(monkeypatch):
    def _handler(req, timeout=None):
        assert req.full_url == "http://127.0.0.1:5000/api/health"
        return _FakeResp(200, json.dumps({"ok": True, "qmt_connected": True}))
    _patch_urlopen(monkeypatch, _handler)
    out = json.loads(StrategyScreenTool().execute(operation="health"))
    assert out["ok"] is True and out["qmt_connected"] is True


def test_connection_error_fail_open(monkeypatch):
    def _handler(req, timeout=None):
        raise OSError("connection refused")
    _patch_urlopen(monkeypatch, _handler)
    out = json.loads(StrategyScreenTool().execute(operation="latest"))
    assert out["ok"] is False and "选股服务" in out["error"]


def test_run_qmt_not_connected_transparent(monkeypatch):
    def _handler(req, timeout=None):
        return _FakeResp(400, json.dumps({"error": "QMT未连接"}))
    _patch_urlopen(monkeypatch, _handler)
    out = json.loads(StrategyScreenTool().execute(operation="run"))
    assert out["ok"] is False and "QMT未连接" in out["error"]


def test_latest_no_snapshot_transparent(monkeypatch):
    def _handler(req, timeout=None):
        return _FakeResp(404, json.dumps({"ok": False, "error": "尚未选股, 请先调用 /api/screen"}))
    _patch_urlopen(monkeypatch, _handler)
    out = json.loads(StrategyScreenTool().execute(operation="latest"))
    assert out["ok"] is False and "尚未选股" in out["error"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd D:/Vibe-Trading && .venv/Scripts/python.exe -m pytest tests/test_strategy_screen_tool.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.tools.strategy_screen_tool'`。

- [ ] **Step 3: 实现工具**

新建 `strategy_screen_tool.py`:

```python
"""选股策略快照查询工具(只读)。

通过 HTTP 调 strategy_web(Flask, 127.0.0.1:5000)读取量化选股结果:
latest=读最近选股快照; run=重新选股(耗时); health=查选股服务/QMT 状态。
服务不可用时 fail-open, 返回带提示的 JSON, 不抛异常。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from src.agent.tools import BaseTool

DEFAULT_BASE_URL = "http://127.0.0.1:5000"

_PARAMS = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["latest", "run", "health"],
            "description": "latest=读最近选股快照(秒回); run=重新选股(耗时1-2分钟); health=查选股服务/QMT状态",
        },
        "top_n": {"type": "integer", "description": "只返回综合分前 N 个候选, 默认全部"},
    },
    "required": ["operation"],
}


class StrategyScreenTool(BaseTool):
    name = "strategy_screen"
    description = ("读取量化选股策略(strategy_web)的选股结果(只读)。"
                   "operation: latest / run / health。"
                   "latest 读最近一次选股快照; run 触发重新选股(耗时1-2分钟); health 查服务状态。"
                   "若返回服务不可用, 说明 strategy_web 尚未运行。")
    parameters = _PARAMS
    is_readonly = True

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or os.environ.get("STRATEGY_WEB_URL")
                          or DEFAULT_BASE_URL).rstrip("/")

    def execute(self, **kwargs: object) -> str:
        operation = str(kwargs.get("operation") or "latest")
        try:
            return self._dispatch(operation, kwargs)
        except Exception as exc:  # noqa: BLE001 — 工具必须 fail-open
            return json.dumps(
                {"ok": False, "error": "选股服务不可用(%r)。请先运行 d:\\cc-joesph\\strategy_web 下的 app.py。" % exc},
                ensure_ascii=False)

    # ---- HTTP ----
    def _request(self, path: str, method: str, timeout: int) -> tuple[int, str]:
        req = urllib.request.Request(self._base_url + path, data=b"" if method == "POST" else None,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # 非 2xx 也把 body 读出返回, 由各 operation 决定怎么透出
            return exc.code, exc.read().decode("utf-8")

    def _truncate(self, data: dict, kw: dict) -> dict:
        top_n = kw.get("top_n")
        if top_n and isinstance(data.get("candidates"), list):
            data["candidates"] = data["candidates"][:int(top_n)]
        return data

    # ---- operations ----
    def _dispatch(self, op: str, kw: dict) -> str:
        if op == "latest":
            return self._latest(kw)
        if op == "run":
            return self._run(kw)
        if op == "health":
            return self._health()
        return json.dumps({"ok": False, "error": "未知操作: %s" % op}, ensure_ascii=False)

    def _latest(self, kw: dict) -> str:
        status, body = self._request("/api/screen/latest", "GET", 5)
        data = json.loads(body)
        return json.dumps(self._truncate(data, kw), ensure_ascii=False)

    def _run(self, kw: dict) -> str:
        status, body = self._request("/api/screen", "POST", 180)
        data = json.loads(body)
        if status != 200:
            # strategy_web 明确返回的错误(QMT未连接/选股失败)透出, 不吞
            return json.dumps({"ok": False, "error": data.get("error", "选股失败")},
                              ensure_ascii=False)
        return json.dumps(self._truncate(data, kw), ensure_ascii=False)

    def _health(self) -> str:
        status, body = self._request("/api/health", "GET", 5)
        data = json.loads(body)
        data["ok"] = (status == 200)
        return json.dumps(data, ensure_ascii=False)
```

- [ ] **Step 4: 运行测试确认通过**

```bash
cd D:/Vibe-Trading && .venv/Scripts/python.exe -m pytest tests/test_strategy_screen_tool.py -q
```

Expected: PASS(7 tests)。

- [ ] **Step 5: 验证自动注册**

```bash
cd D:/Vibe-Trading && .venv/Scripts/python.exe -c "from src.tools import build_registry; print('strategy_screen' in build_registry().tool_names)"
```

Expected: 打印 `True`。

- [ ] **Step 6: 提交(D:\Vibe-Trading 仓库)**

```bash
cd D:/Vibe-Trading && git add agent/src/tools/strategy_screen_tool.py agent/tests/test_strategy_screen_tool.py
git commit -m "feat(tools): strategy_screen read-only tool (latest/run/health over HTTP)"
```

---

### Task 4: 一键启动脚本

**Files:**
- Create: `d:/cc-joesph/start_all.py`(Python 启动器, subprocess 拉起四进程)
- Create: `d:/cc-joesph/start_all.bat`(双击入口, 调 start_all.py)

**Interfaces:**
- Consumes: qmt_sync(Py3.7 `-m qmt_sync --account-id 88869979`)、strategy_web(Py3.7 `app.py`)、Vibe-Trading 后端(venv `serve --port 8899`)、前端(`npm run dev`)。
- Produces: 四进程后台运行, 日志落 `D:\QMT_SYNC\logs\<name>.log`, 打开浏览器 http://localhost:5899。

- [ ] **Step 1: 写 start_all.py**

```python
# -*- coding: utf-8 -*-
"""一键启动 qmt_sync + strategy_web + Vibe-Trading(后端+前端)。
用法: python start_all.py (start_all.bat 双击入口)
"""
import os
import subprocess
import sys
import webbrowser

PY37 = r"C:\Users\28037\AppData\Local\Programs\Python\Python37\python.exe"
QMT_LIB = r"D:\QMT\bin.x64\Lib\site-packages"
LOGS = r"D:\QMT_SYNC\logs"
ACCOUNT_ID = "88869979"

PROCS = [
    {
        "name": "qmt_sync",
        "cmd": [PY37, "-m", "qmt_sync", "--account-id", ACCOUNT_ID],
        "cwd": r"d:\cc-joesph",
        "env": {"PYTHONPATH": QMT_LIB},
    },
    {
        "name": "strategy_web",
        "cmd": [PY37, "app.py"],
        "cwd": r"d:\cc-joesph\strategy_web",
        "env": {"PYTHONPATH": QMT_LIB, "APP_DEBUG": "0"},
    },
    {
        "name": "vibe_backend",
        "cmd": [r"D:\Vibe-Trading\.venv\Scripts\vibe-trading.exe", "serve", "--port", "8899"],
        "cwd": r"D:\Vibe-Trading",
        "env": {},
    },
    {
        "name": "vibe_frontend",
        "cmd": ["npm", "run", "dev"],
        "cwd": r"D:\Vibe-Trading\frontend",
        "env": {},
    },
]


def main() -> int:
    os.makedirs(LOGS, exist_ok=True)
    for p in PROCS:
        env = dict(os.environ)
        env.update(p["env"])
        log = open(os.path.join(LOGS, p["name"] + ".log"), "ab")
        subprocess.Popen(p["cmd"], cwd=p["cwd"], env=env,
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        print("[start_all] launched %s (log=%s)" % (p["name"], p["name"] + ".log"))
    webbrowser.open("http://localhost:5899")
    print("[start_all] all processes launched. 浏览器已打开 http://localhost:5899")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 写 start_all.bat 入口**

```bat
@echo off
REM 一键启动: qmt_sync + strategy_web + Vibe-Trading 后端 + 前端
"C:\Users\28037\AppData\Local\Programs\Python\Python312\python.exe" "d:\cc-joesph\start_all.py"
pause
```

- [ ] **Step 3: 手动验证(冒烟, 非 pytest)**

先确保 QMT 已登录 miniQMT, 然后:

```bash
/c/Users/28037/AppData/Local/Programs/Python/Python312/python.exe /d/cc-joesph/start_all.py
```

验证:
- `netstat -ano | grep -E ':5000|:8899|:5899'` 显示三端口 LISTENING。
- `D:\QMT_SYNC\logs\` 下出现 4 个 log, `qmt_sync.log` 含 `connect result: 0`, `vibe_backend.log` 含 `Application startup complete`。

- [ ] **Step 4: 提交**

```bash
git add start_all.py start_all.bat
git commit -m "feat: one-click start script for qmt_sync + strategy_web + Vibe-Trading"
```

---

## Self-Review(写完计划后自查)

- [ ] Spec 覆盖:spec 三改动点(快照落盘 / latest 端点 / agent 工具)+ 启动脚本 + 环境准备,均有对应任务。
- [ ] Placeholder:无 TBD/TODO。
- [ ] 类型一致:Task 1 产 `SNAPSHOT_PATH`/`_save_snapshot`,Task 2 消费,名称一致;Task 3 工具 operation 名 latest/run/health 与 spec 一致。
