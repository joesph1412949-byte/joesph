# -*- coding: utf-8 -*-
"""做T策略可视化面板(Flask)。

安全设计:
  - **只监听 127.0.0.1**: 面板能触发急停/ARM, 绝不对外网暴露;
  - **只读优先**: 绝大多数接口是只读快照; 仅 pause/arm 会写文件, 且必须
    显式带 confirm=true —— 避免误点或爬虫误触改变真实交易状态;
  - **不碰交易**: 本服务不调用任何下单接口, 只能"关闸"(paused), 不能开单;
  - **脱敏**: /api/config 不回传账号等敏感字段。

数据来源优先级:
  1. tt_runtime.json —— 守护每轮写的快照(最新, 且含真实 arm/paused 判定);
  2. 若快照缺失或过期, 现场跑一次**只读 plan**(断掉写盘句柄), 不产生副作用。

启动: python tt_web/app.py           # http://127.0.0.1:5010
"""
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from shared.common import STATE_DIR               # noqa: E402
from tt import config as tt_config                # noqa: E402
from tt import market                             # noqa: E402
from tt import daemon as tt_daemon                # noqa: E402
from tt.engine import TTEngine                    # noqa: E402
from tt.state import Ledger                       # noqa: E402

app = Flask(__name__)
LOG = logging.getLogger("tt_web")

STATE_PATH = STATE_DIR / "tt_state.json"
RUNTIME_PATH = STATE_DIR / "tt_runtime.json"
# 信号根目录可用环境变量覆盖, 便于演练/测试时与真实 QMT 目录隔离
SIGNAL_ROOT = Path(os.environ.get("TT_SIGNAL_ROOT") or r"D:/QMT_SIGNALS")
RUNTIME_MAX_AGE = 20.0          # 秒: 超过则视为过期, 现场重算

_cache = {"at": 0.0, "data": None}


def _read_json(path):
    try:
        p = Path(path)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def _runtime_age():
    p = Path(RUNTIME_PATH)
    if not p.exists():
        return None
    try:
        return time.time() - p.stat().st_mtime
    except OSError:
        return None


def readonly_plan():
    """现场跑一次 plan, 但断掉账本的写盘句柄 —— 零副作用。

    结果缓存 5 秒, 避免面板自动刷新时反复拉行情。
    """
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < 5.0:
        return _cache["data"]
    cfg = tt_config.load()
    led = Ledger(path=STATE_PATH)
    led.load()
    led.path = None                      # 关键: 后续 save() 直接 return
    eng = TTEngine(cfg, led, feed=market.make_feed(), now_fn=None,
                   force_paper=False)
    plan = eng.plan()
    plan["runtime_at"] = datetime.now().isoformat(timespec="seconds")
    plan["ledger"] = led.snapshot()
    plan["signals_written"] = 0
    plan["paused"] = tt_daemon.is_paused(SIGNAL_ROOT)
    armed, msg = tt_daemon.armed_state(SIGNAL_ROOT, cfg.get("env", "real"))
    plan["armed"] = armed
    plan["armed_msg"] = msg
    plan["blocked"] = "面板只读重算(未落信号)"
    plan["_source"] = "live_recompute"
    _cache["at"], _cache["data"] = now, plan
    return plan


def current_status():
    """优先用守护快照; 过期/缺失则现场重算。"""
    rt = _read_json(RUNTIME_PATH)
    age = _runtime_age()
    if rt and age is not None and age <= RUNTIME_MAX_AGE:
        rt["_source"] = "daemon"
        rt["_age"] = round(age, 1)
        return rt
    plan = readonly_plan()
    plan["_age"] = None
    return plan


# 分级写护栏(2026-09-15, spec tiered-guard): 做T急停/每日放行是真实交易
# 闸门, 仅限本机; 判据唯一真相 = shared.common.is_local_request(有
# CF-Connecting-IP 头=经隧道=远程; 无头且 loopback/RFC1918=本机)。
from shared.common import is_local_request            # noqa: E402

_LOCAL_ONLY = frozenset({"api_pause", "api_arm"})


@app.before_request
def _local_only_guard():
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None
    if is_local_request(request.headers.get("CF-Connecting-IP"),
                        request.remote_addr):
        return None
    if request.endpoint in _LOCAL_ONLY:
        LOG.warning("远程拦截交易闸门: %s %s (CF-IP=%s)", request.method,
                    request.path, request.headers.get("CF-Connecting-IP"))
        return jsonify({"ok": False,
                        "error": "此操作仅限本机执行(交易闸门类)"}), 403
    return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/status")
def api_status():
    try:
        return jsonify({"ok": True, "data": current_status()})
    except Exception as e:                      # 面板不能因单点异常白屏
        LOG.exception("status 失败")
        return jsonify({"ok": False, "error": repr(e)}), 500


@app.route("/api/config")
def api_config():
    """脱敏配置摘要(不回账号)。"""
    try:
        cfg = tt_config.load()
        return jsonify({"ok": True, "data": {
            "env": cfg.get("env"),
            "dry_run": cfg.get("dry_run"),
            "max_units_per_round": cfg.get("max_units_per_round"),
            "grid": cfg.get("grid"),
            "session": cfg.get("session"),
            "risk": cfg.get("risk"),
            "paper_total_asset": cfg.get("paper_total_asset"),
            "symbols": [{k: v for k, v in s.items() if k != "account_id"}
                        for s in cfg.get("symbols", [])],
        }})
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 500


@app.route("/api/kline/<code>")
def api_kline(code):
    """最近 N 日收盘/最高/最低, 供面板画走势与档位。"""
    try:
        n = int(request.args.get("n", 60))
    except ValueError:
        n = 60
    n = max(20, min(n, 250))
    try:
        feed = market.make_feed()
        closes = feed.closes(code, count=n)
        # 样本源带 high/low; QMT 源只有 close, 这里用 close 兜底保证图能画
        rows = []
        if closes:
            rows = [{"date": "", "close": c, "high": c, "low": c}
                    for c in closes]
        sb = getattr(feed, "fallback", None)
        if sb is not None:
            try:
                detail = sb._rows(code)[-n:]
                if detail:
                    rows = detail
            except Exception:
                pass
        return jsonify({"ok": True, "data": {"code": code, "rows": rows}})
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 500


@app.route("/api/pause", methods=["POST"])
def api_pause():
    """急停开关。confirm=true 才生效。只能"关闸", 不能开单。"""
    body = request.get_json(silent=True) or {}
    if str(body.get("confirm")).lower() not in ("true", "1", "yes"):
        return jsonify({"ok": False, "error": "需要 confirm=true"}), 400
    want = bool(body.get("paused", True))
    f = SIGNAL_ROOT / "paused"
    try:
        if want:
            SIGNAL_ROOT.mkdir(parents=True, exist_ok=True)
            f.write_text("paused by tt_web at %s\n"
                         % datetime.now().isoformat(timespec="seconds"),
                         encoding="utf-8")
        elif f.exists():
            f.unlink()
        LOG.warning("急停开关切换 paused=%s by %s", want, request.remote_addr)
        _cache["data"] = None
        return jsonify({"ok": True, "paused": f.exists()})
    except OSError as e:
        return jsonify({"ok": False, "error": repr(e)}), 500


@app.route("/api/arm", methods=["POST"])
def api_arm():
    """写桥端 ARM 文件(当日)。confirm=true 才生效。"""
    body = request.get_json(silent=True) or {}
    if str(body.get("confirm")).lower() not in ("true", "1", "yes"):
        return jsonify({"ok": False, "error": "需要 confirm=true"}), 400
    cfg = tt_config.load()
    env = cfg.get("env", "real")
    today = datetime.now().strftime("%Y%m%d")
    d = SIGNAL_ROOT / env
    try:
        d.mkdir(parents=True, exist_ok=True)
        if bool(body.get("armed", True)):
            (d / "armed.txt").write_text(
                "%s armed by tt_web at %s\n"
                % (today, datetime.now().isoformat(timespec="seconds")),
                encoding="utf-8")
        else:
            p = d / "armed.txt"
            if p.exists():
                p.unlink()
        LOG.warning("ARM 切换 armed=%s (%s) by %s",
                    body.get("armed", True), today, request.remote_addr)
        _cache["data"] = None
        armed, msg = tt_daemon.armed_state(SIGNAL_ROOT, env)
        return jsonify({"ok": True, "armed": armed, "msg": msg})
    except OSError as e:
        return jsonify({"ok": False, "error": repr(e)}), 500


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "at": datetime.now().isoformat(timespec="seconds"),
                    "runtime_age": _runtime_age()})


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")
    port = int(os.environ.get("TT_WEB_PORT") or 5010)
    LOG.info("SIGNAL_ROOT=%s port=%s", SIGNAL_ROOT, port)
    if SIGNAL_ROOT != Path(r"D:/QMT_SIGNALS"):
        LOG.warning("信号根目录已被 TT_SIGNAL_ROOT 覆盖 -> %s", SIGNAL_ROOT)
    # 只监听本机: 面板可急停, 绝不对外暴露
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
