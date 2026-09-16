# 分级写护栏（选股/策略可用，交易闸门仅本机）设计 2026-09-15

需求：公网部署（CF Tunnel）后，通过验证的远程用户可以**选股、新建策略、刷新涨停池**与浏览全部看板；但**敏感写操作仅限本机**：设为默认策略、手填因子、模拟盘暂停/恢复、绩效回填、tt_web 的做T急停/每日放行（真实交易闸门）。被拦时返回明确文案（拦住并报错，不改前端）。

> 背景关联：2026-09-09 曾做过"远程全只读"护栏后撤销（用户拍板全功能共享）；本次是**分域护栏**——写操作放行选股/新建策略，仅交易闸门类收回到本机。

## 已拍板决策（2026-09-15 与用户逐项确认）

1. **部署形态**：本机跑引擎 + CF Tunnel + CF Access 邮箱白名单（域名待购，先立护栏后接线）
2. **身份判定 = 按来源分级**（不依赖 CF 配置，今天就能生效）：
   - 本机/局域网来源 → 全功能
   - 其余来源（公网经隧道）→ 受限档
   - 判定收口在一个函数 `is_local_request()`，将来切 CF Access 邮箱白名单只改这里
3. **敏感集 = 只禁交易闸门**（用户明确：选股/新建策略/刷新涨停池远程可用）——见权限矩阵
4. **体验 = 拦住并报错**：403 + 文案「此操作仅限本机执行」；前端不改

## 关键技术事实（护栏正确性的前提）

- Flask 看到的 `remote_addr` 是**直连对端**：cloudflared 在本机回环转发 → 经 CF 隧道来的请求 `remote_addr == 127.0.0.1`，**不能直接用 loopback 判定"本机"**（否则护栏形同虚设）
- Cloudflare 边缘会把真实客户端 IP 写进 **`CF-Connecting-IP`** 请求头，cloudflared 转发时保留该头；本机直连（localhost/局域网）访问时该头**不存在**
- **判定规则（唯一真相，写在 `is_local_request()`）**：
  1. 有 `CF-Connecting-IP` 头 → 一定是公网隧道来的 → **远程**（该头无法从浏览器伪造穿透隧道，只有 CF 边缘会设置）
  2. 无该头且 `remote_addr` ∈ loopback（127.0.0.1/::1）→ **本机**
  3. 无该头且 `remote_addr` ∈ RFC1918 私网（10/172.16-31/192.168）→ **本机档**（局域网设备）
  4. 其余 → **远程**
- 诚实边界：局域网内出现恶意设备、或将来有人在本机跑代理注入头，护栏不设防——本方案威胁模型是"误触与越权点按"，不是对抗性攻击（对抗性场景应上 CF Access 服务令牌/证书，列为暂缓）

## 权限矩阵（最终）

| 路由（函数名） | 效果 | 本机 | 远程 |
|---|---|---|---|
| 全部 GET | 看板浏览 | ✓ | ✓ |
| `screen`（POST /api/screen） | 跑选股、覆盖最新快照、绩效存档 | ✓ | ✓ |
| `api_strategy_create`（POST /api/strategies/create） | 新建策略 JSON（不改既有策略/指针） | ✓ | ✓ |
| `market_limitup` POST 分支 | 重算涨停池快照（缓存类数据） | ✓ | ✓ |
| `api_strategy_activate`（POST /api/strategies/<sid>/activate） | **改写激活指针 → 影响真实选股** | ✓ | 403 |
| `manual`（POST /api/stock/<code>/manual） | 写手填因子 → 影响打分输入 | ✓ | 403 |
| `api_automation`（POST /api/automation） | 模拟盘暂停/恢复 | ✓ | 403 |
| `perf_backfill`（POST /api/perf/backfill） | 改绩效统计档案 | ✓ | 403 |
| tt_web `pause` / `arm`（POST） | **做T急停/每日放行 = 真实交易闸门** | ✓ | 403 |

「改内核代码」在网页无入口（代码不在暴露面）；服务器磁盘的暴露面 = 上述写路由的落盘范围。

## 实现

两个 app 各一个 `before_request` 钩子（互不共享模块——prism_web 与 tt_web 是独立应用，各自 10 行内）：

```python
# prism_web/app.py
_LOCAL_ONLY = frozenset({"api_strategy_activate", "manual",
                         "api_automation", "perf_backfill"})


def _is_local_request():
    """本机/局域网=True；带 CF-Connecting-IP 头(经 CF 隧道)=远程。"""
    if request.headers.get("CF-Connecting-IP"):
        return False
    ra = request.remote_addr or ""
    return ra in ("127.0.0.1", "::1") or ra.startswith(
        ("10.", "192.168.", "172.16.", "172.17.", "172.18.", "172.19.",
         "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.",
         "172.26.", "172.27.", "172.28.", "172.29.", "172.30.", "172.31."))


@app.before_request
def _local_only_guard():
    if _is_local_request():
        return None
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None
    fn = request.endpoint or ""
    if fn in _LOCAL_ONLY:
        logger.warning("远程拦截敏感写: %s %s (CF-IP=%s)",
                       request.method, request.path,
                       request.headers.get("CF-Connecting-IP"))
        return jsonify({"ok": False, "error": "此操作仅限本机执行(交易闸门类)"}), 403
    return None
```

`tt_web/app.py`：同构钩子，`_LOCAL_ONLY = frozenset({"pause", "arm"})`；文案同上（做T交易闸门）。

要点：
- 按函数名（`request.endpoint`）匹配而非路径字符串——路由规则改动时护栏自动跟随
- 白名单方向：**默认全放行，仅列出的函数收本机**（将来新增写路由默认可用，敏感才手动加）
- `CF-Connecting-IP` 是多值头时的取值：只信 CF 边缘注入，取第一个值即可（`request.headers.get` 默认行为）；不做进一步校验（伪造成本=需要控制 CF 边缘，超出威胁模型）

## 测试（prism_web/tests/test_app.py + tt_web 对应测试）

1. 带 `CF-Connecting-IP` 头 POST 敏感路由（四条各一）→ 403 + 「仅限本机」
2. 带 `CF-Connecting-IP` POST 选股/新建策略/涨停池 → 放行（选股走到 QMT 检查 400 ≠ 403，即证明未拦）
3. 无头 + remote_addr=127.0.0.1 → 全放行（既有全部 POST 测试绿即回归证明）
4. 无头 + remote_addr=192.168.1.50（局域网）→ 敏感路由放行
5. tt_web：无头 + remote_addr=100.64.1.2（公网形态）POST /api/pause、/api/arm → 403；本机 → 放行
6. `is_local_request` 直测：CF 头存在→False；127.0.0.1→True；192.168.1.5→True；100.64.1.2→False

## 失败模式

| 情形 | 结果 |
|---|---|
| 未部署 CF 时远程朋友经 Tailscale 访问 | remote_addr=100.x（非 loopback、无 CF 头）→ 受限档 ✓ |
| 经 CF 隧道访问 | 带 CF-Connecting-IP → 受限档 ✓ |
| 本机 localhost / 局域网 | 全功能 ✓ |
| 前端点了被拦按钮 | 弹出「此操作仅限本机执行」文案 |
| 误判导致自己被拦 | 本机访问不带 CF 头，不受影响；管理可随时删钩子回退 |

## 暂缓项（YAGNI）

- **CF Access 邮箱白名单接线**（部署完成后如需"区分你和朋友"再切，只改 `is_local_request()`）
- **对抗性防御**（请求签名/双向 TLS）：当前威胁模型为"误触/越权点按"，非对抗
- **前端按钮隐藏**：用户拍板"拦住并报错"
- **影子选股**（远程选股不污染快照）：用户明确允许远程选股，暂无需求
