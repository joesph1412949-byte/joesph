# -*- coding: utf-8 -*-
"""Flask 后端：提供可视化网页 + JSON API。
启动：python app.py，浏览器访问 http://localhost:5000"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import os
import json as _json

import datetime as _dt
import logging
import threading as _threading

from flask import Flask, jsonify, render_template, request

sys.path.insert(0, str(Path(__file__).parent.parent))  # 项目根(common.py)
from common import setup_logging

from data_source import DataSource, DataSourceError
from manual_store import ManualStore
from perf_store import PerfStore
from screen import ScreenRunner

logger = setup_logging("strategy_web")

app = Flask(__name__)


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


def _get_screen_runner():
    return ScreenRunner(ds=ds_obj, store=manual_store_obj)


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
        result = _get_screen_runner().run()
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
    import os
    debug = os.environ.get("APP_DEBUG", "1") != "0"
    # 本机主机名含非 UTF-8 字节(GBK), socket.getfqdn 会 UnicodeDecodeError 崩掉 werkzeug 绑定端口
    import socket as _socket
    _socket.getfqdn = lambda name: name
    app.run(host="127.0.0.1", port=5000, debug=debug)
