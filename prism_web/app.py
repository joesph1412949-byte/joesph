# -*- coding: utf-8 -*-
"""Prism Web — 可视化网页 + JSON API(v04 网页的继任者)。

保留 v04 网页全部 API(选股/行情/绩效/手工因子), 新增:
  GET /api/factors            因子库列表(可带 ?category=)
  GET /api/strategies         策略列表
  GET /api/strategy/<id>      策略详情
  GET /api/backtest           东财数据源回测(?strategy=&start=&end=YYYYMMDD)
  GET/POST /api/automation    自动化暂停开关(读/写 PAUSE_FILE)
/api/screen 改用 prism 引擎(load_strategy + DataProvider + run_screen),
支持可选 strategy 参数(默认指针激活策略; 回测缺省 first_board_v04)。
启动：python prism_web/app.py，浏览器访问 http://localhost:5000
"""
import sys
import re
from pathlib import Path

# 路径注入(单源真相, 审查 I2): datasource(旧数据源模块唯一出处) → 项目根
# (common/prism/backtest_cli)。prism_web 不再保留数据模块副本, app 与
# prism.data.DataProvider 按名 import 时经 sys.modules 缓存解析到同一份
# datasource 模块, 杜绝"双份模块"类定义分叉。
# 注意: 必须 resolve() 再取 parent —— 'python app.py' 相对启动时 __file__
# 是相对路径, Path('app.py').parent 得到 '.', 会导致 sys.path 注入相对路径,
# Py3.7 下后续 import common 失败(ModuleNotFoundError)。
_WEB = str(Path(__file__).resolve().parent)       # prism_web(仅模板/静态资源)
_ROOT = str(Path(__file__).resolve().parent.parent)  # 项目根
_OLD_WEB = str(Path(__file__).resolve().parent.parent / "datasource")
for _p in (_OLD_WEB, _ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import os
import json as _json
import datetime as _dt
import logging
import socket
import threading as _threading

from flask import Flask, jsonify, render_template, request

from shared.common import atomic_write, setup_logging

# 旧数据源模块(单源: datasource, 经上方路径注入解析; prism.data.DataProvider
# 内部同名导入复用同一份类定义, 见 prism/data.py)
from data_source import DataSource, DataSourceError
from perf_store import PerfStore

from prism import registry as reg
from prism.engine import load_strategy, run_screen
from prism.data import DataProvider
from prism import trader
from prism.strategies import STRATEGIES_DIR
from prism import market_data as _md
from prism import sector_stage as _ss
from prism import sector_etf_map as _etfmap
from prism import zt_history as _zt
from prism import first_board_review as _fbr

# 因子库注册(装饰器触发): /api/factors、策略校验、选股都要用。幂等。
reg.scan_factors()

logger = setup_logging("prism_web")

app = Flask(__name__)

# 回测数据源(东财, backtest_cli): 导入失败 → /api/backtest 返回 500
try:
    from backtest.cli import (zt_feed, kline_feed, load_market_data,   # noqa: F401
                              build_day_feed, _fund_feed)
    _BACKTEST_FEEDS_OK = True
except Exception:
    zt_feed = None
    kline_feed = None
    load_market_data = None
    build_day_feed = None
    _fund_feed = None
    _BACKTEST_FEEDS_OK = False


def _kline_close_source(code, kdays):
    """绩效回填的行情源: 从 QMT 拉 K线。返回含 close 列的 DataFrame。"""
    return ds_obj.get_kline(code, days=kdays)


# 全局单例（测试时可用 monkeypatch 替换）
ds_obj = DataSource()
perf_store_obj = PerfStore(kline_source=_kline_close_source)

# 选股单飞锁: /api/screen 同时只允许一个选股流程(耗时 1~2 分钟)
_screen_lock = _threading.Lock()

# 新建策略锁(A3 2026-09-19): "分配 id ∧ 落盘"必须同锁 —— 两个并发 create 都先
# 看到 base id 不存在, 于是写同一个文件名, 后者静默覆盖前者而两边都回 ok:true。
_strategy_create_lock = _threading.Lock()

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
    # 原子写统一走 shared.common.atomic_write: 唯一 tmp(pid+线程) + fsync +
    # 有界退避 replace + 失败清理。守护/网页/编辑器可能同时刷新同一份快照,
    # 固定 tmp 名会互踩。
    atomic_write(SNAPSHOT_PATH, _json.dumps(snapshot, ensure_ascii=False))
    return snapshot


def _save_limitup_snapshot(limit_ups):
    """涨停股列表落盘快照(原子写), 供 GET 秒读。"""
    snapshot = {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": limit_ups,
    }
    atomic_write(LIMITUP_SNAPSHOT_PATH,
                 _json.dumps(snapshot, ensure_ascii=False))
    return snapshot


# 策略 id 白名单: 仅 [\w-]+(审查 Minor 3)。防止 sid 含 %2F 解码出的 "/" 等
# 路径分隔符越出 STRATEGIES_DIR 读取站外 .json 文件。
_SID_RE = re.compile(r"[\w-]+\Z")


def _valid_sid(sid):
    r"""sid 是否合规(非空且仅 [\w-])。"""
    return bool(sid) and _SID_RE.match(sid) is not None


def _strategy_path(sid):
    """sid → 策略文件路径。白名单(防路径穿越) ∧ 文件存在, 任一不过 → None。"""
    if not _valid_sid(sid):
        return None
    p = STRATEGIES_DIR / ("%s.json" % sid)
    return p if p.is_file() else None


def _load_strategy_for_screen(sid):
    """按 id 加载策略配置(校验因子存在性)。失败抛 ValueError。"""
    p = _strategy_path(sid)
    if p is None:
        raise ValueError("策略不存在: %s" % sid)
    return load_strategy(p)


# 东财个股因子输出集(fundamental_feed.compute_for_stock), 用于候选 auto_manual
# 来源推断(审查 I1/M6)。手填因子已全部被 K线自动因子取代(2026-08):
# 原 S1/S5/S7 已移除, 故无 "manual" 来源。
_FUNDAMENTAL_FIDS = {"Y1", "Y5", "F7", "Y7", "Y2", "Y6"}


def _infer_auto_manual(factors):
    """候选因子来源推断: 东财个股因子 → "fundamental", 其余 → "auto"。
    前端 renderFactors 按 {fid: 来源} 渲染来源徽标。"""
    return {fid: ("fundamental" if fid in _FUNDAMENTAL_FIDS else "auto")
            for fid in factors}


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
        # 门控标准总数(策略 market_gate.factors 数): 前端分母动态化,
        # 不再写死 /5(full_factor_v1 为 8 因子门槛)
        "gate_total": len(market_factors),
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

# QMT 自动重连: xtdata 连接可能中途失效(QMT 重启/休眠/超时),
# _connected 标志会残留 False。_ensure_qmt() 在每次需要 QMT 的调用前
# 尝试重新连接(轻量探针), 成功则恢复 _connected=True。
_qmt_reconnect_lock = _threading.Lock()


def _ensure_qmt():
    """确保 QMT 连接可用。返回 True/False; False 时日志记录原因。
    带锁防并发重连(多个请求同时触发只会重连一次)。"""
    if ds_obj._connected:
        return True
    if not _qmt_reconnect_lock.acquire(blocking=False):
        return False   # 另一个请求正在重连, 稍后再试
    try:
        ds_obj.connect()
        if ds_obj._connected:
            logger.info("QMT 自动重连成功")
        return ds_obj._connected
    except Exception as e:
        logger.warning("QMT 自动重连失败: %r", e)
        return False
    finally:
        _qmt_reconnect_lock.release()


# ================= 分级写护栏(2026-09-15, spec tiered-guard) =================
# 远程(经 CF 隧道/公网)可选股/新建策略/刷新涨停池; 交易闸门类写操作仅本机。
# 判据唯一真相在 shared.common.is_local_request(有 CF-Connecting-IP 头=经
# 隧道=远程; 无头且 loopback/RFC1918=本机)。将来切 CF Access 邮箱白名单只改
# 那个函数。白名单方向: 默认全放行, 仅下列函数名收本机(新增写路由默认可用)。
from shared.common import is_local_request  # noqa: E402

_LOCAL_ONLY = frozenset({"api_strategy_activate",
                         "api_automation", "perf_backfill"})


@app.before_request
def _local_only_guard():
    if request.method not in ("POST", "PUT", "DELETE", "PATCH"):
        return None
    if request.endpoint not in _LOCAL_ONLY:
        return None
    if is_local_request(request.headers.get("CF-Connecting-IP"),
                        request.remote_addr):
        # A1(2026-09-19): 本机档再加一道跨站否决。只看 remote_addr 时, 浏览器
        # 发起的**跨站简单表单 POST**(form-urlencoded, 无需 CORS 预检)也满足
        # "本机" → 任意本机页面(另一个本机 web 应用/file:// /被 XSS 的本机页)
        # 一条表单就能改掉 paper_daemon 每轮热读的默认策略 —— 实测
        # api_strategy_activate 连请求体都不读, 表单里什么都不用带。
        # Sec-Fetch-Site 由浏览器强制写入、页面无法伪造: 同源前端=same-origin,
        # 地址栏直跳=none, 其余(cross-site / same-site 跨端口 / file://)一律否决。
        # 非浏览器(curl/守护/测试)不带该头 → 不受影响。
        site = request.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            logger.warning("跨站拦截本机档写: %s %s (Sec-Fetch-Site=%s)",
                           request.method, request.path, site)
            return jsonify({"ok": False,
                            "error": "此操作仅限本机同源页面执行"}), 403
        return None
    logger.warning("远程拦截敏感写: %s %s (CF-IP=%s)", request.method,
                   request.path, request.headers.get("CF-Connecting-IP"))
    return jsonify({"ok": False,
                    "error": "此操作仅限本机执行(交易闸门类)"}), 403


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    # 未连接时先尝试自动重连, 让页面状态自愈
    _ensure_qmt()
    return jsonify({"ok": True, "qmt_connected": ds_obj._connected})


@app.route("/api/screen", methods=["POST"])
def screen():
    if not _ensure_qmt():
        return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
    # 单飞锁: 选股耗时 1~2 分钟, 浏览器连点会并发跑多个选股同时打爆东财接口。
    # 进行中再请求 → 409 提示稍候(不排队)。
    if not _screen_lock.acquire(blocking=False):
        return jsonify({"error": "选股进行中, 请稍候(上次选股尚未完成)"}), 409
    logger.info("选股开始")
    try:
        payload = request.get_json(silent=True) or {}
        from prism.engine import active_strategy_id
        sid = (payload.get("strategy") or request.form.get("strategy")
               or request.args.get("strategy") or active_strategy_id())
        try:
            strategy = _load_strategy_for_screen(sid)
        except Exception as e:
            return jsonify({"ok": False,
                            "error": "策略加载失败: %s (%s)" % (sid, e)}), 400
        provider = DataProvider(ds=ds_obj)
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
        return jsonify({"error": "选股失败"}), 500
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
        logger.warning("快照读取失败(%s): %r", SNAPSHOT_PATH, e)
        return jsonify({"ok": False, "error": "快照读取失败"}), 500


@app.route("/api/perf", methods=["GET"])
def perf():
    """绩效统计: 按等级(A-E)返回 候选数/已结算数/胜率/平均收益。"""
    return jsonify({"ok": True, "days": perf_store_obj.list_days(),
                    "summary": perf_store_obj.summary()})


@app.route("/api/perf/backfill", methods=["POST"])
def perf_backfill():
    """用最新行情回填未结算存档的 N 日收益(N 默认 5)。需 QMT 已连接。"""
    if not _ensure_qmt():
        return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
    try:
        days = int(request.args.get("days", 5))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "days 参数非法"}), 400
    # A4(2026-09-19): 旧代码没有 try —— 落盘 OSError 逃逸 → 500 + text/html,
    # 前端 res.json() 直接抛。这里收敛成 JSON, 文案不带 %r(不泄露内部路径)。
    try:
        result = perf_store_obj.backfill(days=days)
    except Exception as e:
        logger.error("绩效回填失败: %r", e, exc_info=True)
        return jsonify({"ok": False, "error": "绩效回填失败"}), 500
    return jsonify({"ok": True, "result": result,
                    "summary": perf_store_obj.summary()})


@app.route("/api/market/kline", methods=["GET"])
def market_kline():
    if not _ensure_qmt():
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
        if not _ensure_qmt():
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
        logger.warning("快照读取失败(%s): %r", LIMITUP_SNAPSHOT_PATH, e)
        return jsonify({"ok": False, "error": "快照读取失败"}), 500


@app.route("/api/market/tick", methods=["GET"])
def market_tick():
    if not _ensure_qmt():
        return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
    codes = [c.strip() for c in (request.args.get("codes") or "").split(",") if c.strip()]
    if not codes:
        return jsonify({"error": "缺少 codes 参数"}), 400
    try:
        ticks = ds_obj.get_full_market_ticks(codes)
    except DataSourceError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "data": ticks})


def _stock_kline_df(code, days=120):
    """个股K线 DataFrame: QMT 优先, 空/异常降级通达信(与因子链路同一降级策略)。

    二者返回同结构(open/high/low/close/volume/amount), 下游无需分叉。
    全失败 → None(调用方给友好错误, 不是 500 裸异常)。"""
    df = None
    try:
        df = ds_obj.get_kline(code, days=days)
    except Exception as e:
        logger.warning("K线(QMT)失败(%s), 降级通达信: %r", code, e)
    if df is None or len(df) == 0:
        try:
            from prism import tdx_source
            df = tdx_source.get_kline(code, days=days)
        except Exception as e:
            logger.warning("K线(通达信)失败(%s): %r", code, e)
            df = None
    return df


@app.route("/api/stock/<code>/kline")
def stock_kline(code):
    try:
        df = _stock_kline_df(code)
        if df is None or len(df) == 0:
            # 上游全挂(交付视角: 明确告知, 不是 500 裸异常)
            return jsonify({"ok": False,
                            "error": "行情源不可用(QMT未连接且通达信取数失败)"}), 503
        ma60 = df["close"].rolling(60).mean().tolist()
        # OHLC 用于前端 ECharts 蜡烛图 (spec §4③)
        up_stop = 0
        try:
            up_stop = ds_obj.get_instrument(code).get("UpStopPrice") or 0
        except Exception:
            pass                      # 涨停价缺失 → 0(fail-open, 不牵连K线)
        return jsonify({
            "dates": _kline_dates(df),
            "opens": [float(x) for x in df["open"]],
            "closes": [float(x) for x in df["close"]],
            "highs": [float(x) for x in df["high"]],
            "lows": [float(x) for x in df["low"]],
            "volumes": [int(v) for v in df["volume"]],
            "ma60": [None if x != x else round(x, 2) for x in ma60],  # NaN→None
            "up_stop": up_stop,
        })
    except Exception:
        return jsonify({"error": "K线获取失败"}), 500


# ================= 新路由: 因子库/策略/回测/自动化 =================

# ================= 板块周度跟踪(W2): 观察面板组装件(纯只读, 红线: 不进打分/交易) =================

# 60日新高 memo: {(zt缓存mtime, 行情缓存mtime, 当日): 结果} — 缓存未变且当日
# 内不重算(spec §3: zt 缓存 63MB 加载 ~0.3s, 避免每次 API 重复加载)
_WEEKLY_NH_MEMO = {"key": None, "val": None}


def _sector_new_high():
    """60日新高家数 {行业名: {"nh","base"}}(缓存驱动, fail-open → None)。

    双重翻译(W1 交接): sector_map 段(6位码→801码) 按 data.py 同款后缀规则
    (6开头→.SH 否则→.SZ) 翻成带后缀码(zt 缓存键格式), 再经 sectors 段
    (801码→行业名) 得 {带后缀码: 行业名}。任一环节失败/缺段 → None。"""
    try:
        key = (_zt.CACHE_PATH.stat().st_mtime,
               _md.CACHE_PATH.stat().st_mtime,
               _dt.date.today().strftime("%Y%m%d"))
    except OSError:
        return None
    if _WEEKLY_NH_MEMO["key"] == key:
        return _WEEKLY_NH_MEMO["val"]
    val = None
    try:
        cache = _md._load_cache()
        names = {c: (i or {}).get("name") or ""
                 for c, i in (cache.get("sectors") or {}).items()}
        smap = {}
        for c6, rec in (cache.get("sector_map") or {}).items():
            name = names.get((rec or {}).get("sector") or "")
            if not (c6 and name):
                continue
            smap[c6 + (".SH" if c6.startswith("6") else ".SZ")] = name
        if smap:
            zt = _zt._load_cache()
            if zt:
                val = _ss.new_high_counts(smap, zt)
    except Exception as e:  # noqa: BLE001 - 观察面板 fail-open
        logger.warning("60日新高计算失败(此列退化 None): %r", e)
        val = None
    _WEEKLY_NH_MEMO["key"] = key
    _WEEKLY_NH_MEMO["val"] = val
    return val


def _sector_etf_quotes():
    """ETF 锚点行情 {code: {"amount","pct_chg"}}(fail-open → {})。

    codes = SECTOR_ETF_MAP 全部非空锚点; fetch_etf_quotes 单码已 fail-open。"""
    try:
        codes = sorted({a["code"] for a in _etfmap.SECTOR_ETF_MAP.values()
                        if a})
        return _md.fetch_etf_quotes(codes) if codes else {}
    except Exception as e:  # noqa: BLE001 - 观察面板 fail-open
        logger.warning("ETF 锚点行情失败(退化): %r", e)
        return {}


@app.route("/api/sector_stage")
def api_sector_stage():
    """板块感知观察(孕育期/阶段定位/资金惯性/周度跟踪), 只读 fail-open 不 500。"""
    try:
        snap = _md.mkt_snapshot()
        # 面板只展示申万一级(801 前缀): 缓存 sectors 段 801 与东财 BK 同名行
        # 并存(如两个"银行"), 不过滤会重复命中同名 new_high/etf(W1 交接§6);
        # week_rank 同样只在申万一级内排名(周度排名视图口径)
        _all_sec = snap.get("sector") or {}
        snap["sector"] = {c: r for c, r in _all_sec.items()
                          if str(c).startswith("801")}
        if not snap["sector"] and _all_sec:
            logger.warning("板块观察: 801 过滤后 0 行(缓存疑似东财 BK 体系, "
                           "如需切换请 --rebuild --source sw)")
        table = _ss.sector_table(snap, new_high=_sector_new_high(),
                                 etf_quotes=_sector_etf_quotes(),
                                 etf_map=_etfmap.SECTOR_ETF_MAP)
        inertia = _ss.flow_inertia(snap.get("flow_rank"))
        last_date = ""
        for rec in (snap.get("sector") or {}).values():
            ds = (rec or {}).get("dates") or []
            if ds and str(ds[-1]) > last_date:
                last_date = str(ds[-1])
        flow_days = len(((snap.get("flow_rank") or {}).get("dates") or []))
        return jsonify({"ok": True, "date": last_date, "sectors": table,
                        "inertia": inertia, "flow_days": flow_days})
    except Exception as e:  # noqa: BLE001 - 观察面板 fail-open
        logger.warning("sector_stage 快照失败: %r", e)
        return jsonify({"ok": False, "date": "", "sectors": [],
                        "inertia": [], "flow_days": 0, "error": str(e)})


# ================= 首板盘后深度拆解(2026-09-19, 观察层) =================
# 用户分工: 盘中(09:15-10:00)用户自己执行; 盘后本面板对每个首板做五维拆解。
# **网页路径绝不下载 1m 特征**(bt_intraday 约定, 采集只在显式 CLI):
# 这里只读已落盘结果; 无落盘时做一次只读采集(不触发 1m 补采, 缺特征的日子
# 封板质量维如实标"未知")。缺数据不造假 —— 同 prism.first_board_review 口径。
_FBR_STATE_DIR = Path(_ROOT) / "runtime" / "state"


def _fbr_state_path(day8):
    return _FBR_STATE_DIR / ("first_board_review_%s.json" % day8)


def _load_fbr_state(day8):
    """读落盘分析结果; 缺失/损坏 → None(不抛)。"""
    p = _fbr_state_path(day8)
    if not p.exists():
        return None
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - 读失败按无落盘处理
        logger.warning("首板拆解落盘读取失败 %s: %r", p, e)
        return None


def _fbr_collect(day8, timeout=90):
    """只读采集 + 落盘, 带超时护栏(web 长驻进程不能被 QMT 卡死)。

    返回 (records|None, error|None)。超时按失败处理 —— 线程 daemon, 不阻塞。
    """
    box = {}

    def _work():
        try:
            # persist_when_empty=False: 查空日不落盘, 免污染日期列表
            recs, _paths = _fbr.run(day8, want_report=True,
                                    persist_when_empty=False)
            box["recs"] = recs
        except Exception as e:  # noqa: BLE001 - 面板 fail-open
            box["error"] = "%s: %s" % (type(e).__name__, e)

    t = _threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, "采集超时(>%ds), 请稍后重试或用 CLI 生成" % timeout
    if "error" in box:
        return None, box["error"]
    return box.get("recs"), None


@app.route("/api/first_board")
def api_first_board():
    """首板盘后拆解: 读落盘; 无则只读采集一次。fail-open 不 500。"""
    day8 = str(request.args.get("date") or "").replace("-", "").strip()
    if not day8:
        day8 = _dt.date.today().strftime("%Y%m%d")
    if len(day8) != 8 or not day8.isdigit():
        return jsonify({"ok": False, "date": day8, "count": 0, "items": [],
                        "error": "date 需为 YYYYMMDD"})
    try:
        state = None
        force = request.args.get("refresh") in ("1", "true", "yes")
        if not force:
            state = _load_fbr_state(day8)
        if state is None:
            recs, err = _fbr_collect(day8)
            if err:
                return jsonify({"ok": False, "date": day8, "count": 0,
                                "items": [], "error": err})
            # 直接用内存结果(不重读盘: 避免读到上一次的旧落盘)
            state = {"date": day8, "count": len(recs or []),
                     "items": [{"record": r, "analysis": _fbr.analyze(r)}
                               for r in (recs or [])]}
        return jsonify({"ok": True, "date": state.get("date") or day8,
                        "count": state.get("count", 0),
                        "items": state.get("items") or []})
    except Exception as e:  # noqa: BLE001 - 观察面板 fail-open
        logger.warning("首板拆解失败: %r", e)
        return jsonify({"ok": False, "date": day8, "count": 0, "items": [],
                        "error": str(e)})


@app.route("/api/first_board/dates")
def api_first_board_dates():
    """已生成拆解的日期列表(新→旧), 供前端下拉。"""
    try:
        out = []
        for p in _FBR_STATE_DIR.glob("first_board_review_*.json"):
            d = p.stem.replace("first_board_review_", "")
            if len(d) == 8 and d.isdigit():
                out.append(d)
        return jsonify({"ok": True, "dates": sorted(set(out), reverse=True)})
    except Exception as e:  # noqa: BLE001
        logger.warning("首板拆解日期列表失败: %r", e)
        return jsonify({"ok": False, "dates": [], "error": str(e)})


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
        if p.name.startswith("."):      # .active.json 指针不是策略(审查 I-1)
            continue
        try:
            data = _json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": data.get("id"), "name": data.get("name"),
                        "description": data.get("description", "")})
        except Exception:
            continue
    from prism.engine import active_strategy_id
    return jsonify({"ok": True, "strategies": out,
                    "active": active_strategy_id()})


@app.route("/api/strategy/<sid>")
def api_strategy(sid):
    p = _strategy_path(sid)
    if p is None:
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    # A4(2026-09-19): 旧代码无 try → 文件损坏时抛 JSONDecodeError → 500 + text/html
    # (同文件 /api/strategies 是 except: continue, 两条读路径口径不一致)。
    try:
        data = _json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("策略文件读取失败(%s): %r", sid, e)
        return jsonify({"ok": False,
                        "error": "策略文件损坏或不可读: %s" % sid}), 500
    return jsonify({"ok": True, "strategy": data})


@app.route("/api/strategies/create", methods=["POST"])
def api_strategy_create():
    """网页编辑器新建策略(spec §3): 校验→生成 id→试载→失败零写盘→原子写。
    # ponytail: MVP 只增不改不删(spec §7-4), 编辑/删除端点有需求再加。"""
    from prism.engine import validate_strategy_payload
    payload = request.get_json(silent=True) or {}
    ok, errors, strat = validate_strategy_payload(payload)
    if not ok:
        return jsonify({"ok": False, "errors": errors}), 400
    # A3: id 分配 + 试载 + 落盘 整段同锁(并发时后到者拿到 _2 而不是覆盖同一个文件)
    with _strategy_create_lock:
        base = "custom_" + _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        sid = base
        n = 2
        while (STRATEGIES_DIR / ("%s.json" % sid)).exists():
            sid = "%s_%d" % (base, n)
            n += 1
        strat["id"] = sid
        strat["description"] = ("网页编辑器生成 %s"
                                % _dt.datetime.now().isoformat(timespec="seconds"))
        try:
            load_strategy(strat)        # 终极校验: 试载(spec §4-7, Task3 复审硬条件)
        except Exception as e:
            return jsonify({"ok": False,
                            "errors": ["引擎试载失败: %r" % e]}), 400
        p = STRATEGIES_DIR / ("%s.json" % sid)
        atomic_write(p, _json.dumps(strat, ensure_ascii=False, indent=1))
    return jsonify({"ok": True, "id": sid})


@app.route("/api/strategies/<sid>/activate", methods=["POST"])
def api_strategy_activate(sid):
    """设为默认策略(spec §5): sid 白名单 ∧ 文件存在 → 写指针。"""
    from prism.engine import set_active_strategy
    if _strategy_path(sid) is None:
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    set_active_strategy(sid)
    return jsonify({"ok": True, "active": sid})


@app.route("/api/backtest")
def api_backtest():
    from prism.backtest import Backtester
    sid = request.args.get("strategy", "first_board_v04")
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
    p = _strategy_path(sid)
    if p is None:
        return jsonify({"ok": False, "error": "策略不存在: %s" % sid}), 404
    if not _BACKTEST_FEEDS_OK:
        return jsonify({"ok": False, "error": "回测数据源不可用"}), 500
    try:
        strategy = load_strategy(p)
        # 市场数据注入(2026-09-04 修复): 与 CLI 一致。
        # 缺注入时 N6-N8/F8/F9/SEC 共 10 个因子恒 0 → v04 候选全 0 分被过滤、
        # full_factor_v1 门控(N6-N8)永不达标 → 三策略静默零交易。
        mkt, sector_map, md_note = (load_market_data()
                                    if load_market_data else (None, None, None))
        # 按日上下文(2026-09-16): 与 CLI **同一装配**(backtest.cli.build_day_feed)。
        # 不接则 N3/N4/N5/F1/F6 恒 0 —— 网页回测成了残废版, 与已复活的 CLI 口径
        # 不一致。惰性构造(首次请求某日才算那天), 装配失败不阻塞回测: 退化为无
        # day_feed(= 旧行为), 报告 gate_notes 带原因(不静默)。
        # use_intraday=True(Task 2): 1m 特征**只读已落盘缓存**(F2/F3 代理),
        # 缓存缺失静默降级进报告 data_notes —— 网页请求路径绝不触发 1m 下载。
        day_feed, feed_note = None, None
        if build_day_feed is not None:
            try:
                # fund_feed(Task 3): 与 CLI 同口径注入基本面快照(Y1/Y8/F7/Y6/Y7;
                # 网络/构造失败 → None = 不注入, 报告 data_notes 标覆盖)。
                # 网页与 CLI 共用同一装配, 保持数字一致(2026-09-04 修复的教训)。
                # **网页一律 offline(不传 fetch): 这是刻意的** —— 东财单股取数
                # 实测 40s+, 而标量 timeout 只管 connect + 每次 socket read,
                # 不限总时长, 联网取数会让 HTTP 请求"永不返回"(2026-09-17 实测
                # 卡死 27 分钟被强杀)。网页只读已采集缓存, 补数据走 CLI:
                # python -m prism.fund_snapshot --date YYYYMMDD
                day_feed = build_day_feed(
                    s, e, use_intraday=True,
                    fund_feed=_fund_feed() if _fund_feed else None)
            except Exception as exc:
                logger.warning("按日上下文装配失败: %r", exc, exc_info=True)
                feed_note = ("按日上下文装配失败 → N3/N4/N5/F1/F6 按静态参数"
                             "空转: %r" % exc)
        bt = Backtester(strategy, zt_feed=zt_feed, kline_feed=kline_feed)
        # validate=False(M8①): validation 自带 sharpe_samples(1000+1000 个数)
        # + equity_paths(≤30×400), 响应体会膨胀到 MB 级 —— 而网页不展示该字段
        # (只展示 sharpe_ratio 等净值口径指标)。默认跑的口径仍归 CLI。
        rep = bt.run(s, e, mkt=mkt, sector_map=sector_map, day_feed=day_feed,
                     validate=False)
        rep["market_data"] = mkt is not None
        rep["day_feed"] = day_feed is not None
        if md_note:
            rep.setdefault("gate_notes", []).append(md_note)
        if feed_note:
            rep.setdefault("gate_notes", []).append(feed_note)
    except Exception as e:
        logger.error("回测失败: %r", e, exc_info=True)
        return jsonify({"ok": False, "error": "回测失败"}), 500
    return jsonify({"ok": True, "report": rep})


# ---------------- 模拟盘(只读查询, 设计 §8-7: 无写端点) ----------------

@app.route("/api/paper/summary")
def api_paper_summary():
    from prism.paper import PaperAccount
    try:
        return jsonify(PaperAccount().summary())
    except Exception as e:
        logger.error("读取模拟盘账本失败: %r", e)
        return jsonify({"exists": False, "error": "读取模拟盘账本失败"})


@app.route("/api/paper/detail")
def api_paper_detail():
    from prism.paper import PaperAccount
    try:
        return jsonify(PaperAccount().detail())
    except Exception as e:
        logger.error("读取模拟盘账本失败: %r", e)
        return jsonify({"exists": False, "error": "读取模拟盘账本失败"})


@app.route("/api/automation", methods=["GET", "POST"])
def api_automation():
    if request.method == "GET":
        return jsonify({"ok": True, "paused": trader.check_paused()})
    # A4(2026-09-19): 旧代码用不带 silent 的 get_json() → 表单体/坏 JSON 抛 415,
    # 响应是 text/html, 前端 res.json() 直接崩。这里 fail-closed 成 400 + JSON,
    # 且绝不据此去动暂停开关。
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
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


# ================= 启动自检(2026-09-19) =================

def _port_owned_by_other(host="127.0.0.1", port=5000):
    """端口是否已被**别的进程**监听(且能区分 TIME_WAIT 误判)。

    为什么不能指望 werkzeug 报错: BaseWSGIServer.allow_reuse_address = True
    (werkzeug 3.1.8) → Windows 上 SO_REUSEADDR 等价 SO_REUSEPORT, 第二个实例
    bind 照样成功并 LISTENING(实测 127.0.0.1:5000 同时两行 LISTENING, PID 不同)。
    判据: 先**不带 SO_REUSEADDR**抢绑一次 —— 成功 = 没人监听; 失败可能是真有
    监听者, 也可能只是 TIME_WAIT/已绑定未监听。所以失败后再 connect 一次确认:
    连得上才算"真有监听者"(loopback 上要么立刻成功, 要么立刻被拒)。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((host, port))
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            try:
                probe.connect((host, port))
            except OSError:
                return False
            return True
    finally:
        s.close()
    return False


def _port_guard_or_exit(host="127.0.0.1", port=5000):
    """服务端 fail-closed 端口自检: 已有监听者 → 打印原因并 sys.exit(1)。

    放在服务端是唯一能兜住所有入口的一层 —— start_all / prism_launcher /
    watchdog 都只问"端口开着吗", 谁都不会发现双开。
    reloader 子进程必须放行: werkzeug 3.1.8 的 run_simple 里**父进程**建 server
    并占住端口(把 fd 经 WERKZEUG_SERVER_FD 交给子进程), 子进程若也自检会被自己
    的父进程挡死 → debug 模式正常启动直接失败。
    """
    if os.environ.get("WERKZEUG_RUN_MAIN"):
        return
    if not _port_owned_by_other(host, port):
        return
    print("端口 %d 已被占用(检测有效): 本机已有 prism_web 在监听, "
          "拒绝启动以免静默双开" % port)
    sys.exit(1)


def _debug_enabled():
    """debug 默认**关**(2026-09-19 翻转): 只有显式 APP_DEBUG=1 才开。

    旧默认(os.environ.get("APP_DEBUG","1") != "0")意味着直接
    `python prism_web\\app.py` 就开 debug, 后果(均实测): reloader 父+子两个
    进程(每实例多占一条 QMT 连接)、GET /console 暴露交互式调试器(200)、
    任一被 import 的 .py 保存就把生产面板热重启。
    """
    return os.environ.get("APP_DEBUG", "0") == "1"


if __name__ == "__main__":
    # 端口自检放最前(C1): 一旦要退出就不该再去连 QMT(每实例多占一条 QMT 连接)
    _port_guard_or_exit("127.0.0.1", 5000)
    print("=" * 50)
    print("连接 QMT miniQMT...")
    try:
        ds_obj.connect()
        print("已连接: 数据源就绪")
    except DataSourceError as e:
        print("警告: %s" % e)
        print("请先打开 QMT 并开启 miniQMT 模式")
    print("浏览器访问: http://localhost:5000")
    debug = _debug_enabled()
    # 本机主机名含非 UTF-8 字节(GBK), socket.getfqdn 会 UnicodeDecodeError 崩掉 werkzeug 绑定端口
    socket.getfqdn = lambda name: name
    app.run(host="127.0.0.1", port=5000, debug=debug)
