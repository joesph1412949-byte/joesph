# -*- coding: utf-8 -*-
"""Flask 后端：提供可视化网页 + JSON API。
启动：python app.py，浏览器访问 http://localhost:5000"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import os
import json as _json

import datetime as _dt

from flask import Flask, jsonify, render_template, request

from data_source import DataSource, DataSourceError
from manual_store import ManualStore
from screen import ScreenRunner

app = Flask(__name__)

# 全局单例（测试时可用 monkeypatch 替换）
ds_obj = DataSource()
manual_store_obj = ManualStore()

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


def _get_screen_runner():
    return ScreenRunner(ds=ds_obj, store=manual_store_obj)


def _fmt_date(t):
    """K线 time 字段转日期字符串。xtdata 返回毫秒级 epoch int；测试 mock 可能是 datetime。"""
    if hasattr(t, "date"):
        return str(t.date())
    return _dt.datetime.fromtimestamp(int(t) / 1000.0).strftime("%Y-%m-%d")


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
    try:
        result = _get_screen_runner().run()
        _save_snapshot(result)
        return jsonify(result)
    except DataSourceError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "选股失败: %r" % e}), 500


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


@app.route("/api/stock/<code>/kline")
def stock_kline(code):
    try:
        df = ds_obj.get_kline(code, days=120)
        ma60 = df["close"].rolling(60).mean().tolist()
        detail = ds_obj.get_instrument(code)
        # OHLC 用于前端 ECharts 蜡烛图 (spec §4③)
        return jsonify({
            "dates": [_fmt_date(t) for t in df["time"]],
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
    app.run(host="127.0.0.1", port=5000, debug=debug)
