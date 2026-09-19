# -*- coding: utf-8 -*-
"""交付级冒烟自检 — 以「用户拿到这个系统」的视角逐项验证。

用法: python ops/smoke_check.py [--with-cli]

覆盖:
  1. 关键模块可导入(无语法/循环依赖)
  2. 缓存文件可读且结构完整(缺段/空段不崩)
  3. prism_web 全部 GET 路由返回 200 且 JSON 可解析(真实缓存, 不打桩)
  4. 纯计算层用真实缓存出数(板块观察/选股上下文)
  5. CLI 入口 --help 可用(可选 --with-cli)
  6. 运行期目录与状态文件健康(无测试残留污染)

只读自检: 不发起真实下单、不写业务状态(仅日志)。
退出码: 0 全通过; 1 有失败项(逐条打印)。
"""
import argparse
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# GBK 控制台兜底: 报告含非 GBK 字符(如 ✓)时不再 UnicodeEncodeError 崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS = []


def check(name, fn):
    try:
        detail = fn()
        RESULTS.append((True, name, detail or ""))
    except Exception as e:
        RESULTS.append((False, name, "%s: %s" % (type(e).__name__, e)))
        if "--trace" in sys.argv:
            traceback.print_exc()


# ---------------------------------------------------------------- 1. 导入

def _imports():
    import prism.market_data      # noqa: F401
    import prism.sector_stage     # noqa: F401
    import prism.engine           # noqa: F401
    import prism.paper            # noqa: F401
    import prism.backtest         # noqa: F401
    import prism.registry         # noqa: F401
    import prism.factors          # noqa: F401
    import shared.common          # noqa: F401
    import prism_web.app          # noqa: F401
    n = len(prism.registry.FACTORS)
    return "核心模块可导入, 注册因子 %d 个" % n


# ---------------------------------------------------------------- 2. 缓存

def _caches():
    from prism import market_data as md
    c = md._load_cache()
    seg = {k: (len(v) if hasattr(v, "__len__") else 0) for k, v in c.items()}
    missing = [k for k in ("sectors",) if k not in c]
    if missing:
        raise AssertionError("缺关键段: %s" % missing)
    snap = md.mkt_snapshot()
    return "缓存段 %s; 快照键 %s" % (seg, sorted(snap.keys()))


# ---------------------------------------------------------------- 3. 路由

GET_ROUTES = [
    "/", "/api/health", "/api/factors", "/api/strategies",
    "/api/screen/latest", "/api/perf", "/api/paper/summary",
    "/api/paper/detail", "/api/automation", "/api/sector_stage",
    "/api/market/limitup", "/api/market/tick",
    "/api/stock/600519/kline", "/api/stock/600519/manual",
]


def _routes():
    import prism_web.app as appmod
    client = appmod.app.test_client()
    bad, degraded = [], []
    for r in GET_ROUTES:
        resp = client.get(r)
        if resp.status_code == 503:
            degraded.append(r)          # 上游不可用(可接受降级, 非缺陷)
        elif resp.status_code >= 500:
            bad.append("%s→%d" % (r, resp.status_code))
        elif r.startswith("/api/") and resp.status_code == 200:
            try:
                resp.get_json()
            except Exception:
                bad.append("%s→JSON 不可解析" % r)
    if bad:
        raise AssertionError("异常路由: %s" % ", ".join(bad))
    note = "%d 条 GET 路由无 5xx" % len(GET_ROUTES)
    if degraded:
        note += " (%d 条降级 503=上游不可用: %s)" % (len(degraded),
                                                ", ".join(degraded))
    return note


# ---------------------------------------------------------------- 4. 计算层

def _compute():
    from prism import market_data as md
    from prism import sector_stage as ss
    snap = md.mkt_snapshot()
    table = ss.sector_table(snap)
    if not table:
        raise AssertionError("板块表为空(缓存可能损坏)")
    stages = {}
    for row in table:
        stages[row["stage"]] = stages.get(row["stage"], 0) + 1
    inertia = ss.flow_inertia(snap.get("flow_rank"))
    return "板块 %d 行, 阶段分布 %s, 惯性表 %d 行" % (
        len(table), stages, len(inertia))


# ---------------------------------------------------------------- 5. 状态

def _state():
    from shared.common import STATE_DIR, LOG_DIR, CACHE_DIR
    for d in (STATE_DIR, LOG_DIR, CACHE_DIR):
        if not Path(d).exists():
            raise AssertionError("运行期目录缺失: %s" % d)
    junk = [p.name for p in Path(STATE_DIR).glob("*")
            if ".bak" in p.name or "fake" in p.name.lower()
            or "dirty" in p.name.lower()]
    acct = Path(STATE_DIR) / ".paper_account.json"
    info = "账本存在" if acct.exists() else "账本未创建(首次运行)"
    if junk:
        raise AssertionError("状态目录有测试残留: %s" % junk)
    return "状态/日志/缓存目录正常, %s" % info


# ---------------------------------------------------------------- 6. CLI

# 交付 CLI 入口, --help 必须 rc=0。
# 历史缺陷(2026-09-19): 第二项写的是 `scripts/make_prism_summary_pdf.py`,
# 该路径**不存在**(真身是 ops/make_summary_pdf.py), 但守卫条件
# `"--help" not in args[-1]` 对 `--help` 恒为 False ⇒ 任何 rc 都被吞掉,
# 本项**无条件**打印"CLI --help 可用"(实测: 子进程 rc=2 时依旧返回通过)。
# 现: 真入口 + 失败即 raise(返回一句话不算失败, 判别力在 raise 上)。
# 不列 ops/make_summary_pdf.py: 它不解析 argv, 传 --help 会真的去生成 PDF
# (实测 rc=0 + 写出 docs/reports/*.pdf), 与"只读自检"冲突。
CLI_HELP_CMDS = (
    ["-m", "prism.market_data", "--help"],
    ["ops/watchdog.py", "--help"],
)


def _cli():
    import subprocess
    outs = []
    for args in CLI_HELP_CMDS:
        if args[0] != "-m" and not (ROOT / args[0]).is_file():
            outs.append("%s 不存在" % args[0])
            continue
        r = subprocess.run([sys.executable] + args, cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90)
        if r.returncode != 0:
            outs.append("%s rc=%d" % (args[1], r.returncode))
    if outs:
        raise AssertionError("CLI --help 失败: %s" % "; ".join(outs))
    return "CLI --help 可用 (%d 个入口)" % len(CLI_HELP_CMDS)


def _guard():
    """分级写护栏(2026-09-15): 远程敏感写 403, 远程选股放行, 做T闸门 403。

    做T面板自 2026-09-16 起是 `tt_solo/dashboard`(端口 5011), 旧 `tt_web`
    (5010) 已退役: 这里若继续 import tt_web, 删目录后整个自检项抛 ImportError,
    「远程调用者碰不了交易闸门」这条断言会**静默消失**(只剩一条 FAIL, 很容易被
    当成环境问题忽略)。所以按新路径 import —— 断言本身一字不改。
    """
    import prism_web.app as appmod
    # 追加而非插 0: tt_solo/tests、tt_solo/tools 会与根级同名目录抢名
    if str(ROOT / "tt_solo") not in sys.path:
        sys.path.append(str(ROOT / "tt_solo"))
    import dashboard.app as tt_app
    env = {"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "203.0.113.7"}
    # prism_web: 敏感路由必须 403; 远程选股必须放行(非 403)
    c = appmod.app.test_client()
    r = c.post("/api/strategies/full_factor_v1/activate", json={},
               environ_base=env)
    if r.status_code != 403:
        raise AssertionError("prism_web 敏感路由未被拦: %d" % r.status_code)
    r = c.post("/api/screen", json={"strategy": "x"}, environ_base=env)
    if r.status_code == 403:
        raise AssertionError("远程选股被误拦")
    # 做T面板(tt_solo/dashboard): 闸门必须 403(真下单闸门绝不能远程碰)
    ct = tt_app.app.test_client()
    for url in ("/api/pause", "/api/arm"):
        r = ct.post(url, json={"confirm": "true"}, environ_base=env)
        if r.status_code != 403:
            raise AssertionError("做T面板闸门 %s 未被拦: %d"
                                 % (url, r.status_code))
    return "prism_web 敏感 403 / 远程选股放行 / 做T面板(tt_solo/dashboard:5011) 闸门 403 均验证"


def _public_tunnel():
    """公网隧道可达性(可选, --with-tunnel 时跑): 期望 CF Access 拦到登录页。

    判定: 未登录时 CF Access 会 302 到 *.cloudflareaccess.com 登录页
    (或 200 直接给登录页)。隧道断/服务停 → 连接失败或 502/530。"""
    import urllib.error
    import urllib.request
    url = "https://prism.prism1121.icu/"
    req = urllib.request.Request(url, headers={"User-Agent": "prism-smoke"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            code, body = resp.status, resp.read(400).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        code, body = e.code, ""
    except Exception as e:                      # 连接失败 = 隧道/服务断
        raise AssertionError("公网不可达(隧道或 PRISM 未运行): %r" % e)
    if code >= 500:
        raise AssertionError("隧道通但源站故障(HTTP %d)" % code)
    if code in (301, 302, 303, 307, 308):
        loc = ""
        try:                                     # 跟随一次拿 Location 判 Access
            with urllib.request.urlopen(req, timeout=15) as r2:
                loc = r2.geturl()
        except urllib.error.HTTPError as e2:
            loc = e2.headers.get("Location", "") if e2.headers else ""
        except Exception:
            loc = ""
        if "cloudflareaccess.com" in loc:
            return "公网可达 + CF Access 登录页(白名单生效)"
        return "公网可达(HTTP %d → %s)" % (code, loc[:60] or "重定向")
    if "cloudflareaccess.com" in body or "Sign in" in body:
        return "公网可达 + CF Access 登录页(白名单生效)"
    return "公网可达(HTTP %d, 未见 Access 登录页——检查 Access 应用是否启用)" % code


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-cli", action="store_true", help="附带 CLI 自检")
    ap.add_argument("--with-tunnel", action="store_true",
                    help="附带公网隧道可达性(需联网; 断网时跳过)")
    ap.add_argument("--trace", action="store_true", help="失败时打印堆栈")
    ap.parse_args()

    check("1. 模块导入与因子注册", _imports)
    check("2. 缓存结构与快照", _caches)
    check("3. Web 路由(真实缓存)", _routes)
    check("4. 纯计算层出数", _compute)
    check("5. 运行期状态健康", _state)
    check("6. 分级护栏", _guard)
    if "--with-tunnel" in sys.argv:
        check("7. 公网隧道可达性", _public_tunnel)
    if "--with-cli" in sys.argv:
        check("8. CLI 入口", _cli)

    print("=" * 62)
    print("prism 交付级冒烟自检")
    print("=" * 62)
    failed = 0
    for ok, name, detail in RESULTS:
        print("[%s] %s" % ("PASS" if ok else "FAIL", name))
        if detail:
            print("       %s" % detail)
        failed += 0 if ok else 1
    print("-" * 62)
    print("%d/%d 通过" % (len(RESULTS) - failed, len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
