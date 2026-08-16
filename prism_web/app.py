# -*- coding: utf-8 -*-
"""Prism Web — 可视化网页 + JSON API(strategy_web 改造)。

保留旧 strategy_web 全部 API(选股/行情/绩效/手工因子), 新增:
  GET /api/factors            因子库列表(可带 ?category=)
  GET /api/strategies         策略列表
  GET /api/strategy/<id>      策略详情
  GET /api/backtest           东财数据源回测(?strategy=&start=&end=YYYYMMDD)
  GET/POST /api/automation    自动化暂停开关(读/写 PAUSE_FILE)
/api/screen 改用 prism 引擎(load_strategy + DataProvider + run_screen),
支持可选 strategy 参数(默认 default)。
启动：python prism_web/app.py，浏览器访问 http://localhost:5000
"""
import sys
import re
from pathlib import Path

# 路径注入(单源真相, 审查 I2): strategy_web(旧数据源模块唯一出处) → 项目根
# (common/prism/backtest_cli)。prism_web 不再保留数据模块副本, app 与
# prism.data.DataProvider 按名 import 时经 sys.modules 缓存解析到同一份
# strategy_web 模块, 杜绝"双份模块"类定义分叉。
_WEB = str(Path(__file__).parent)                 # prism_web(仅模板/静态资源)
_ROOT = str(Path(__file__).parent.parent)         # 项目根
_OLD_WEB = str(Path(__file__).parent.parent / "strategy_web")
for _p in (_OLD_WEB, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import os
import json as _json
import datetime as _dt
import logging
import threading as _threading

from flask import Flask, jsonify, render_template, request

from common import setup_logging

# 旧数据源模块(单源: strategy_web, 经上方路径注入解析; prism.data.DataProvider
# 内部同名导入复用同一份类定义, 见 prism/data.py)
from data_source import DataSource, DataSourceError
from manual_store import ManualStore
from perf_store import PerfStore
from eastmoney import EastMoneyFeed  # noqa: F401
from fundamental import FundamentalFeed  # noqa: F401

from prism import registry as reg
from prism.engine import load_strategy, run_screen
from prism.data import DataProvider
from prism import trader
from prism.strategies import STRATEGIES_DIR

# 因子库注册(装饰器触发): /api/factors、策略校验、选股都要用。幂等。
reg.scan_factors()

logger = setup_logging("prism_web")

app = Flask(__name__)

# 回测数据源(东财, backtest_cli): 导入失败 → /api/backtest 返回 500
try:
    from backtest_cli import zt_feed, kline_feed  # noqa: F401
    _BACKTEST_FEEDS_OK = True
except Exception:
    zt_feed = None
    kline_feed = None
    _BACKTEST_FEEDS_OK = False


def _kline_close_source(code, kdays):
    """绩效回填的行情源: 从 QMT 拉 K线。返回含 close 列的 DataFrame。"""
    return ds_obj.get_kline(code, days=kdays)


# 全局单例（测试时可用 monkeypatch 替换）
ds_obj = DataSource()
manual_store_obj = ManualStore()
perf_store_obj = PerfStore(kline_source=_kline_close_source)

# 选股单飞锁: /api/screen 同时只允许一个选股流程(耗时 1~2 分钟)
_screen_lock = _threading.Lock()

# 选股结果快照路径(供 /api/screen/latest 秒读; 测试可 monkeypatch)
SNAPSHOT_PATH = Path(__file__).parent / "screen_result.json"
LIMITUP_SNAPSHOT_PATH = Path(__file__).parent / "limitup_result.json"


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


def _save_limitup_snapshot(limit_ups):
    """涨停股列表落盘快照(原子写), 供 GET 秒读。"""
    snapshot = {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": limit_ups,
    }
    tmp = LIMITUP_SNAPSHOT_PATH.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, LIMITUP_SNAPSHOT_PATH)
    return snapshot


# 策略 id 白名单: 仅 [\w-]+(审查 Minor 3)。防止 sid 含 %2F 解码出的 "/" 等
# 路径分隔符越出 STRATEGIES_DIR 读取站外 .json 文件。
_SID_RE = re.compile(r"[\w-]+\Z")


def _valid_sid(sid):
    r"""sid 是否合规(非空且仅 [\w-])。"""
    return bool(sid) and _SID_RE.match(sid) is not None


def _load_strategy_for_screen(sid):
    """按 id 加载策略配置(校验因子存在性)。失败抛 ValueError。"""
    if not _valid_sid(sid):
        raise ValueError("策略不存在: %s" % sid)
    p = STRATEGIES_DIR / ("%s.json" % sid)
    if not p.is_file():
        raise ValueError("策略不存在: %s" % sid)
    return load_strategy(p)


# 东财个股因子输出集(fundamental_feed.compute_for_stock)与手填因子集(前端 MANUAL_ALL),
# 用于候选 auto_manual 来源推断(审查 I1/M6)。
_FUNDAMENTAL_FIDS = {"Y1", "Y5", "F7", "Y7", "Y2", "Y6"}
_MANUAL_FIDS = {"S1", "S5", "S7"}


def _infer_auto_manual(factors):
    """候选因子来源推断(简化版, 与旧 screen.py auto_manual 语义尽量一致):
    东财个股因子 → "fundamental"; 手填因子 → "manual"; 其余 → "auto"。
    前端 renderFactors 按 {fid: 来源} 渲染来源徽标。"""
    out = {}
    for fid in factors:
        if fid in _FUNDAMENTAL_FIDS:
            out[fid] = "fundamental"
        elif fid in _MANUAL_FIDS:
            out[fid] = "manual"
        else:
            out[fid] = "auto"
    return out


def _merge_candidate_fields(candidates, limit_ups):
    """把涨停池字段合并进候选(与旧 screen.py:2168-2173 同构, 审查 I1):
    name/sealed/float_mv 补齐, last/up_stop_price 缺时兜底, auto_manual 来源推断。"""
    lu_map = {lu.get("code"): lu for lu in limit_ups}
    for c in candidates:
        lu = lu_map.get(c["code"]) or {}
        c["name"] = lu.get("name") or c["code"]
        c["sealed"] = lu.get("sealed")
        c["float_mv"] = (lu.get("float_volume") or 0) * (lu.get("last") or 0)
        if c.get("last") is None:
            c["last"] = lu.get("last")
        if c.get("up_stop_price") is None:
            c["up_stop_price"] = lu.get("up_stop_price")
        c["auto_manual"] = _infer_auto_manual(c.get("factors") or {})


def _build_market_payload(market_ctx, market_factors, gate_score, limit_ups):
    """market 载荷(与旧 screen.py market 同构, 前端 app.js renderMarket 依赖, 审查 C1):
    node_score/stage/factors/total_amount/limit_up_count/top_themes。"""
    from prism.market import classify_market
    ticks = market_ctx.get("ticks") or {}
    total_amount = 0
    for t in ticks.values():
        try:
            total_amount += (t.get("amount") or 0)
        except AttributeError:
            pass
    em = market_ctx.get("em") or {}
    top_themes = em.get("top_themes") or []
    return {
        "node_score": gate_score,
        "stage": classify_market(gate_score),
        "factors": market_factors,
        "total_amount": total_amount,
        "limit_up_count": len(limit_ups),
        "top_themes": top_themes[:10],
    }


def _run_prism_screen(strategy, provider):
    """用 prism 引擎跑选股(与 trader.run_daily 同一编排: 门槛因子 → 涨停池 → 逐股上下文)。"""
    market_ctx = provider.build_market_context()
    gate_fids = (strategy.get("market_gate") or {}).get("factors", [])
    gate_factors = {}
    market_factors = {}
    for fid in gate_fids:
        try:
            res = reg.get_factor(fid)["func"](market_ctx)
            if isinstance(res, dict):
                gate_factors[fid] = 1 if res.get("score") else 0
                market_factors[fid] = {"score": res.get("score", 0),
                                       "note": res.get("note", "")}
            else:
                gate_factors[fid] = 1 if res else 0
                market_factors[fid] = {"score": res, "note": ""}
        except Exception:
            gate_factors[fid] = 0
            market_factors[fid] = {"score": 0, "note": "异常"}
    limit_ups = provider.get_limit_ups()
    stock_contexts = {}
    for lu in limit_ups:
        code = lu["code"]
        stock_contexts[code] = provider.build_stock_context(code)
    result = run_screen(strategy, market_ctx, gate_factors=gate_factors,
                        stock_contexts=stock_contexts)
    result["market"] = _build_market_payload(
        market_ctx, market_factors, result.get("gate_score", 0), limit_ups)
    _merge_candidate_fields(result.get("candidates", []), limit_ups)
    return result


def _fmt_date(t):
    """K线时间 → 'YYYY-MM-DD'。xtdata 新版 time 列是毫秒级 epoch int；
    旧版(QMT 当前自带)无 time 列, 日期在 int64 索引(YYYYMMDD)上；测试 mock 可能是 datetime。"""
    if hasattr(t, "date"):
        return str(t.date())
    if isinstance(t, int) and 19000000 < t < 21000000:  # YYYYMMDD 整数索引
        s = str(t)
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    return _dt.datetime.fromtimestamp(int(t) / 1000.0).strftime("%Y-%m-%d")


def _kline_dates(df):
    """K线日期序列, 兼容新旧 xtquant 两种 schema: 新版有 'time' 列；旧版无该列, 日期在索引上。"""
    if "time" in df.columns:
        return [_fmt_date(t) for t in df["time"]]
    return [_fmt_date(t) for t in df.index]


# ================= 旧路由(保留) =================

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "qmt_connected": ds_obj._connected})


@app.route("/api/screen", methods=["POST"])
def screen():
    if not ds_obj._connected:
        return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
    # 单飞锁: 选股耗时 1~2 分钟, 浏览器连点会并发跑多个选股同时打爆东财接口。
    # 进行中再请求 → 409 提示稍候(不排队)。
    if not _screen_lock.acquire(blocking=False):
        return jsonify({"error": "选股进行中, 请稍候(上次选股尚未完成)"}), 409
    logger.info("选股开始")
    try:
        payload = request.get_json(silent=True) or {}
        sid = (payload.get("strategy") or request.form.get("strategy")
               or request.args.get("strategy") or "default")
        try:
            strategy = _load_strategy_for_screen(sid)
        except Exception as e:
            return jsonify({"ok": False,
                            "error": "策略加载失败: %s (%s)" % (sid, e)}), 400
        provider = DataProvider(ds=ds_obj, manual=manual_store_obj)
        result = _run_prism_screen(strategy, provider)
        _save_snapshot(result)
        # 绩效追踪: 候选清单自动存档(按日期), 供后续回填涨跌/统计胜率
        if result.get("candidates"):
            try:
                perf_store_obj.archive_daily(result["candidates"])
            except Exception as e:
                logger.warning("绩效存档失败(不影响选股): %r", e)
        logger.info("选股完成: 环境=%s 候选=%d (A%d/B%d/C%d/D%d)",
                    result.get("environment_ok"),
                    result.get("summary", {}).get("candidate_count", 0),
                    result.get("summary", {}).get("a_count", 0),
                    result.get("summary", {}).get("b_count", 0),
                    result.get("summary", {}).get("c_count", 0),
                    result.get("summary", {}).get("d_count", 0))
        return jsonify(result)
    except DataSourceError as e:
        logger.error("选股失败(DataSourceError): %r", e)
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        logger.error("选股失败: %r", e, exc_info=True)
        return jsonify({"error": "选股失败: %r" % e}), 500
    finally:
        _screen_lock.release()


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


@app.route("/api/perf", methods=["GET"])
def perf():
    """绩效统计: 按等级(A-E)返回 候选数/已结算数/胜率/平均收益。"""
    return jsonify({"ok": True, "days": perf_store_obj.list_days(),
                    "summary": perf_store_obj.summary()})


@app.route("/api/perf/backfill", methods=["POST"])
def perf_backfill():
    """用最新行情回填未结算存档的 N 日收益(N 默认 5)。需 QMT 已连接。"""
    if not ds_obj._connected:
        return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
    try:
        days = int(request.args.get("days", 5))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "days 参数非法"}), 400
    result = perf_store_obj.backfill(days=days)
    return jsonify({"ok": True, "result": result,
                    "summary": perf_store_obj.summary()})


@app.route("/api/market/kline", methods=["GET"])
def market_kline():
    if not ds_obj._connected:
        return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
    codes = [c.strip() for c in (request.args.get("codes") or "").split(",") if c.strip()]
    if not codes:
        return jsonify({"error": "缺少 codes 参数"}), 400
    try:
        days = int(request.args.get("days", 120))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "days 参数非法"}), 400
    period = request.args.get("period", "1d")
    try:
        kline_map = ds_obj.get_kline_bulk(codes, days=days, period=period)
    except DataSourceError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    data = {}
    for code, df in kline_map.items():
        item = {
            "dates": _kline_dates(df),
            "open": [float(x) for x in df["open"]],
            "high": [float(x) for x in df["high"]],
            "low": [float(x) for x in df["low"]],
            "close": [float(x) for x in df["close"]],
            "volume": [int(v) for v in df["volume"]],
        }
        if "amount" in df.columns:
            item["amount"] = [float(a) for a in df["amount"]]
        data[code] = item
    if not data:
        return jsonify({"ok": False, "error": "无法获取任何股票的K线"}), 500
    return jsonify({"ok": True, "data": data})


@app.route("/api/market/limitup", methods=["GET", "POST"])
def market_limitup():
    if request.method == "POST":
        if not ds_obj._connected:
            return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
        try:
            ticks = ds_obj.get_full_market_ticks()
            limit_ups = ds_obj.get_limit_up_stocks(ticks)
        except DataSourceError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        snap = _save_limitup_snapshot(limit_ups)
        snap["ok"] = True
        return jsonify(snap)
    if not LIMITUP_SNAPSHOT_PATH.is_file():
        return jsonify({"ok": False, "error": "尚未刷新, 请先 POST /api/market/limitup"}), 404
    try:
        data = _json.loads(LIMITUP_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        data["ok"] = True
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": "快照读取失败: %r" % e}), 500


@app.route("/api/market/tick", methods=["GET"])
def market_tick():
    if not ds_obj._connected:
        return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
    codes = [c.strip() for c in (request.args.get("codes") or "").split(",") if c.strip()]
    if not codes:
        return jsonify({"error": "缺少 codes 参数"}), 400
    try:
        ticks = ds_obj.get_full_market_ticks(codes)
    except DataSourceError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "data": ticks})


@app.route("/api/stock/<code>/kline")
def stock_kline(code):
    try:
        df = ds_obj.get_kline(code, days=120)
        ma60 = df["close"].rolling(60).mean().tolist()
        detail = ds_obj.get_instrument(code)
        # OHLC 用于前端 ECharts 蜡烛图 (spec §4③)
        return jsonify({
            "dates": _kline_dates(df),
            "opens": [float(x) for x in df["open"]],
            "closes": [float(x) for x in df["close"]],
            "highs": [float(x) for x in df["high"]],
            "lows": [float(x) for x in df["low"]],
            "volumes": [int(v) for v in df["volume"]],
            "ma60": [None if x != x else round(x, 2) for x in ma60],  # NaN→None
            "up_stop": detail.get("UpStopPrice") or 0,
        })
    except Exception as e:
        return jsonify({"error": "K线获取失败: %r" % e}), 500


@app.route("/api/stock/<code>/manual", methods=["GET", "POST"])
def manual(code):
    if request.method == "GET":
        return jsonify(manual_store_obj.get_manual(code))
    try:
        payload = request.get_json() or {}
        manual_store_obj.set_manual(code, payload)
        return jsonify({"factors": manual_store_obj.get_manual(code)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


# ================= 新路由: 因子库/策略/回测/自动化 =================

@app.route("/api/factors")
def api_factors():
    category = request.args.get("category") or None
    out = []
    for f in reg.list_factors(category=category):
        item = dict(f)
        item.pop("func", None)  # jsonify 不可序列化函数对象
        out.append(item)
    return jsonify({"ok": True, "factors": out})


@app.route("/api/strategies")
def api_strategies():
    out = []
    for p in sorted(STRATEGIES_DIR.glob("*.json")):
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": data.get("id"), "name": data.get("name"),
                        "description": data.get("description", "")})
        except Exception:
            continue
    return jsonify({"ok": True, "strategies": out})


@app.route("/api/strategy/<sid>")
def api_strategy(sid):
    if not _valid_sid(sid):
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    p = STRATEGIES_DIR / ("%s.json" % sid)
    if not p.is_file():
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    return jsonify({"ok": True, "strategy": _json.loads(p.read_text(encoding="utf-8"))})


@app.route("/api/backtest")
def api_backtest():
    from prism.backtest import Backtester
    sid = request.args.get("strategy", "default")
    start = request.args.get("start")
    end = request.args.get("end")
    if not start or not end:
        return jsonify({"ok": False, "error": "缺少 start/end (YYYYMMDD)"}), 400
    try:
        s = _dt.datetime.strptime(start, "%Y%m%d").date()
        e = _dt.datetime.strptime(end, "%Y%m%d").date()
    except ValueError:
        return jsonify({"ok": False, "error": "日期格式应为 YYYYMMDD"}), 400
    if s > e:
        return jsonify({"ok": False, "error": "start 不能晚于 end"}), 400
    if not _valid_sid(sid):
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    p = STRATEGIES_DIR / ("%s.json" % sid)
    if not p.is_file():
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    if not _BACKTEST_FEEDS_OK:
        return jsonify({"ok": False, "error": "回测数据源不可用"}), 500
    try:
        strategy = load_strategy(p)
        bt = Backtester(strategy, zt_feed=zt_feed, kline_feed=kline_feed)
        rep = bt.run(s, e)
    except Exception as e:
        logger.error("回测失败: %r", e, exc_info=True)
        return jsonify({"ok": False, "error": "回测失败: %r" % e}), 500
    return jsonify({"ok": True, "report": rep})


@app.route("/api/automation", methods=["GET", "POST"])
def api_automation():
    if request.method == "GET":
        return jsonify({"ok": True, "paused": trader.check_paused()})
    payload = request.get_json() or {}
    pf = Path(trader.PAUSE_FILE)  # 兼容 str / Path 两种配置
    if payload.get("paused") is True:  # 严格布尔判断: 字符串 "false" 不再误建(审查 Minor 2)
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text("", encoding="utf-8")
    else:
        try:
            pf.unlink()
        except FileNotFoundError:
            pass
    return jsonify({"ok": True, "paused": trader.check_paused()})


if __name__ == "__main__":
    print("=" * 50)
    print("连接 QMT miniQMT...")
    try:
        ds_obj.connect()
        print("已连接: 数据源就绪")
    except DataSourceError as e:
        print("警告: %s" % e)
        print("请先打开 QMT 并开启 miniQMT 模式")
    print("浏览器访问: http://localhost:5000")
    # debug 模式(带 reloader)后台运行不稳定, 生产/常驻用 APP_DEBUG=0 关闭
    debug = os.environ.get("APP_DEBUG", "1") != "0"
    # 本机主机名含非 UTF-8 字节(GBK), socket.getfqdn 会 UnicodeDecodeError 崩掉 werkzeug 绑定端口
    import socket as _socket
    _socket.getfqdn = lambda name: name
    app.run(host="127.0.0.1", port=5000, debug=debug)
