# prism 私享分享实现计划（Tailscale + 远程只读护栏）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 朋友经 Tailscale 只读访问 prism 看板；远程一切写方法 403，本机全功能不变。

**Architecture:** app.py 加一个 before_request 钩子（唯一代码改动）；Tailscale 安装/ACL 为无代码配置步骤。Spec: `docs/superpowers/specs/2026-09-09-tailscale-share-design.md`。

**Tech Stack:** Flask before_request；pytest（test_app.py 既有 client fixture）；Tailscale 免费档（外部配置）。

**基线:** 518 绿（pt_bt172 起）；基线 commit 38c312e。

## Global Constraints

- **纯护栏改动**：不碰任何既有路由/因子打分/模拟盘/采集代码路径（spec 拍板）
- 白名单 = **空**：非本机来源 `POST/PUT/DELETE/PATCH` 一律 403，`GET/HEAD/OPTIONS` 全放行
- loopback 定义 = `127.0.0.1` / `::1`（无反代无 ProxyFix，remote_addr 即真实来源）
- 测试离线（不连 QMT/不读真实缓存，沿用 test_app.py 既有 client fixture 模式）
- ponytail 阶梯：最懒可行方案；绝不简化掉护栏本身/测试确定性
- 本地 commit 随意，**绝不 push**（push 前必须问用户）

---

### Task T1: 远程只读护栏（before_request 钩子 + 3 个测试）

**Files:**
- Modify: `prism_web/app.py`（`@app.route("/")` 之前，约 283 行前插入钩子；顶部无需新 import——`request`/`jsonify` 已用）
- Test: `prism_web/tests/test_app.py`（末尾追加）

**Interfaces:**
- Consumes: 既有 Flask app 对象（`app_module.app`）、既有 client fixture（`app_module.app.config["TESTING"] = True; test_client()`）
- Produces: `GET /api/health` 在伪造远程 IP 下仍 200（朋友可用的探活语义）；一切远程写方法 → 403 JSON `{"error": "远程访问为只读模式, 写操作仅限本机"}`。后续白名单扩展（如影子选股）只改 `_READONLY_EXEMPT` 集合

- [ ] **Step 1: 写失败测试**（test_app.py 末尾追加）

```python
# ---------------- 远程只读护栏(私享分享 spec 2026-09-09) ----------------

def test_remote_write_blocked(client):
    """伪造 Tailscale 来源的 POST → 403, 且不走到路由内部(钩子在前)。"""
    r = client.post("/api/screen", json={"strategy": "x"},
                    environ_base={"REMOTE_ADDR": "100.64.1.2"})
    assert r.status_code == 403
    assert "只读" in r.get_json()["error"]


def test_remote_get_allowed(client):
    """伪造远程 IP 的 GET 放行(看板语义), 走到路由内部。"""
    r = client.get("/api/health", environ_base={"REMOTE_ADDR": "100.64.1.2"})
    assert r.status_code == 200


def test_local_write_untouched(client):
    """本机 loopback 写方法不受影响(放行到路由既有逻辑)。"""
    # /api/screen 本机 POST 会先撞 QMT 检查(400)而非 403——证明护栏未拦本机
    r = client.post("/api/screen", json={"strategy": "x"},
                    environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code != 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q -k "remote" --import-mode=importlib`
Expected: FAIL（`test_remote_write_blocked` 得到 400/500 而非 403——钩子不存在；另两条约已绿）

- [ ] **Step 3: 实现钩子**（app.py，插在 `@app.route("/")` 之前）

```python
# ================= 私享分享(2026-09-09): 远程只读护栏 =================
# spec: docs/superpowers/specs/2026-09-09-tailscale-share-design.md
# 白名单=空(spec 拍板): 写方法仅限本机 loopback; Tailscale(100.64.0.0/10)
# 与局域网 IP 一律视为远程。将来白名单路由只改 _READONLY_EXEMPT。
_READONLY_EXEMPT = frozenset()      # 路由函数名集合, 现为空
_WRITE_METHODS = frozenset(("POST", "PUT", "DELETE", "PATCH"))


@app.before_request
def _remote_readonly_guard():
    if request.method not in _WRITE_METHODS:
        return None
    if (request.remote_addr or "") in ("127.0.0.1", "::1"):
        return None
    logger.warning("远程只读拦截: %s %s from %s",
                   request.method, request.path, request.remote_addr)
    return jsonify({"error": "远程访问为只读模式, 写操作仅限本机"}), 403
```

- [ ] **Step 4: 跑测试确认通过**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism_web/tests/test_app.py -q --import-mode=importlib`
Expected: 全 PASS（含既有全部 POST 测试——本机 client 默认 REMOTE_ADDR=127.0.0.1）

- [ ] **Step 5: 全量回归**

Run: `$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_bt172`
Expected: 521 passed（518 + 3）

- [ ] **Step 6: 提交**

```bash
git add prism_web/app.py prism_web/tests/test_app.py
git commit -m "feat: 远程只读护栏(私享分享: 非本机写方法403, GET全放, 白名单空)"
```

### Task T2: Tailscale 配置指引（无代码，收尾文档）

**Files:**
- Modify: `MEMORY.md`（环境备忘追加 Tailscale 段；守护重启待办合并更新）

**Interfaces:**
- Consumes: Task T1 的护栏已上线（重启 PRISM.bat 后生效）
- Produces: 朋友可用的访问路径 + ACL 配置记录（运维知识入 MEMORY，防换机/重装丢配置）

- [ ] **Step 1: 用户手动安装**（agent 不代装 GUI 软件，口头指引）
  1. `tailscale.com/download` → Windows 客户端 → 安装 → 微软/Google 账号登录
  2. 登录后托盘常驻；管理台（login.tailscale.com）记下本机 Tailscale IP/MagicDNS 名
- [ ] **Step 2: 邀请朋友 + ACL**（用户在管理台操作）
  1. 管理台 Users → Invite users → 发邀请链接给朋友
  2. Access Controls（ACL）粘贴如下模板（IP 换成管理台 Machines 页看到的实际值：朋友设备 IP 与用户电脑 IP）：

```json
{
  "acls": [
    {"action": "accept", "src": ["<朋友设备IP>"], "dst": ["<用户电脑IP>:5000"]}
  ]
}
```
（Tailscale 新策略编辑器是 HuJSON 语法，以上结构同样成立；保存后管理台会提示应用。）
- [ ] **Step 3: 验收**
  1. 用户本机 `http://127.0.0.1:5000` → 全功能
  2. 朋友（或用户手机装 Tailscale 模拟远程）浏览器 `http://<本机Tailscale IP>:5000` → 看板可浏览；点"选股"按钮 → 报"仅本机"错误
- [ ] **Step 4: 更新 MEMORY.md 环境备忘**

```markdown
- **Tailscale 私享分享**（09-09）：朋友经 tailnet 只读访问 5000 看板；远程只读护栏=app.py before_request（非 loopback 写方法 403，白名单空）；你关机=朋友不可用（已接受）；ACL 限朋友→本机 5000；免费档 3 用户/100 设备
```

- [ ] **Step 5: 提交**

```bash
git add MEMORY.md
git commit -m "docs: Tailscale 私享分享配置备忘(ACL/验收/护栏说明)"
```
