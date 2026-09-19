# -*- coding: utf-8 -*-
r"""prism_web app 测试 — 新 API(因子库/策略/回测/自动化) + 旧路由冒烟保留。

运行: cd D:\cc-joesph && python -m pytest prism_web/tests/ -v
"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))  # 项目根(import prism_web)

import pytest

import prism_web.app as app_module


@pytest.fixture
def client(monkeypatch):
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# ---------------- 新 API: 因子库 ----------------

def test_factors_list(client):
    r = client.get("/api/factors")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    ids = [f["id"] for f in data["factors"]]
    assert "F1" in ids and "Y3" in ids
    # jsonify 可序列化: 不泄漏内部 func 对象
    assert all("func" not in f for f in data["factors"])


def test_factors_category_filter(client):
    r = client.get("/api/factors?category=first_board")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    ids = [f["id"] for f in data["factors"]]
    assert "F1" in ids
    assert "Y3" not in ids


# ---------------- 新 API: 策略 ----------------

def test_strategies_list(client):
    r = client.get("/api/strategies")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert any(s["id"] == "first_board_v04" for s in data["strategies"])


def test_strategy_detail(client):
    r = client.get("/api/strategy/first_board_v04")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["strategy"]["id"] == "first_board_v04"
    assert data["strategy"]["scoring_models"]


def test_screen_default_strategy_is_v04(client, tmp_path, monkeypatch):
    """/api/screen 无 strategy 参数 → 默认 first_board_v04(实盘切换,
    2026-09-01 用户决策; 本测试锁定新默认, 历史默认策略文件已删)。"""
    import prism.engine as engine
    # I-F1: 指针隔离 — 空 tmp 目录 → 指针缺失回落 v04, 不再依赖仓库真实指针
    monkeypatch.setattr(engine, "STRATEGIES_DIR", tmp_path)
    monkeypatch.setattr(app_module.ds_obj, "_connected", True)
    monkeypatch.setattr(app_module, "SNAPSHOT_PATH",
                        tmp_path / "screen_result.json")
    monkeypatch.setattr(app_module, "perf_store_obj",
                        type("FakePerf", (), {
                            "archive_daily": lambda self, c, d=None: None})())

    seen = {}

    def fake_run_screen(strategy, market_ctx, gate_factors=None,
                        stock_contexts=None):
        seen["id"] = strategy["id"]
        return {"environment_ok": True, "gate_score": 4, "candidates": [],
                "summary": {"candidate_count": 0, "a_count": 0, "b_count": 0,
                            "c_count": 0, "d_count": 0}}
    monkeypatch.setattr(app_module, "run_screen", fake_run_screen)
    monkeypatch.setattr(app_module, "DataProvider",
                        lambda ds=None, manual=None: _stub_provider())

    r = client.post("/api/screen")          # 不传任何策略参数
    assert r.status_code == 200
    assert seen["id"] == "first_board_v04"


def test_screen_default_follows_pointer(client, tmp_path, monkeypatch):
    """指针指向 v03 → 无参数选股用 v03。"""
    import prism.engine as engine
    monkeypatch.setattr(app_module.ds_obj, "_connected", True)
    monkeypatch.setattr(app_module, "SNAPSHOT_PATH", tmp_path / "s.json")
    monkeypatch.setattr(app_module, "perf_store_obj",
                        type("FakePerf", (), {"archive_daily": lambda s, c, d=None: None})())
    seen = {}
    def fake_screen(strategy, market_ctx, gate_factors=None, stock_contexts=None):
        seen["id"] = strategy["id"]
        return {"environment_ok": True, "gate_score": 1, "candidates": [],
                "summary": {"candidate_count": 0}}
    monkeypatch.setattr(app_module, "run_screen", fake_screen)
    monkeypatch.setattr(app_module, "DataProvider",
                        lambda ds=None, manual=None: _stub_provider())
    # 把真实指针临时指向 v03(测试后恢复)
    real_ptr = engine.STRATEGIES_DIR / engine.ACTIVE_FILENAME
    old = real_ptr.read_text(encoding="utf-8") if real_ptr.exists() else None
    try:
        engine.set_active_strategy("first_board_v03")
        r = client.post("/api/screen")
        assert r.status_code == 200 and seen["id"] == "first_board_v03"
    finally:
        if old is not None:
            real_ptr.write_text(old, encoding="utf-8")
        else:
            real_ptr.unlink(missing_ok=True)


def test_strategy_detail_unknown_404(client):
    r = client.get("/api/strategy/no_such")
    assert r.status_code == 404
    data = r.get_json()
    assert data["ok"] is False
    assert "策略不存在" in data["error"]


# ---------------- 新 API: 自动化开关(读/写 PAUSE_FILE) ----------------

def test_automation_pause_roundtrip(client, tmp_path, monkeypatch):
    import prism.trader as trader
    pf = tmp_path / "paused"
    monkeypatch.setattr(trader, "PAUSE_FILE", str(pf))
    monkeypatch.setattr(app_module, "trader", trader)
    r = client.post("/api/automation", json={"paused": True})
    assert r.status_code == 200
    assert pf.exists()
    r2 = client.get("/api/automation")
    assert r2.get_json()["paused"] is True
    client.post("/api/automation", json={"paused": False})
    assert not pf.exists()


# ---------------- 新 API: 回测(参数校验/策略缺失/数据源不可用) ----------------

def test_backtest_missing_params_400(client):
    r = client.get("/api/backtest")
    assert r.status_code == 400
    assert r.get_json()["ok"] is False
    assert "start/end" in r.get_json()["error"]


def test_backtest_bad_date_400(client):
    r = client.get("/api/backtest?start=2026-01-01&end=20260105")
    assert r.status_code == 400
    assert "日期格式应为 YYYYMMDD" in r.get_json()["error"]


def test_backtest_unknown_strategy_404(client):
    r = client.get("/api/backtest?strategy=no_such&start=20260101&end=20260105")
    assert r.status_code == 404
    assert "策略不存在" in r.get_json()["error"]


def test_backtest_feeds_unavailable_500(client, monkeypatch):
    monkeypatch.setattr(app_module, "_BACKTEST_FEEDS_OK", False)
    r = client.get("/api/backtest?strategy=first_board_v04&start=20260101&end=20260105")
    assert r.status_code == 500
    assert r.get_json()["ok"] is False
    assert "回测数据源不可用" in r.get_json()["error"]


def test_backtest_injects_market_data(client, monkeypatch):
    """网页回测必须注入市场数据(与 CLI 一致)——缺注入曾致三策略静默零交易。"""
    captured = {}

    class FakeBT:
        def __init__(self, strategy, zt_feed=None, kline_feed=None):
            pass

        def run(self, s, e, sell_rules=None, progress=None, **kw):
            captured.update(kw)
            return {"trades": 1, "trading_days": 1, "gate_notes": [],
                    "filter_stats": {}}

    monkeypatch.setattr("prism.backtest.Backtester", FakeBT)
    monkeypatch.setattr(app_module, "load_market_data",
                        lambda: ({"sector": {"880368": 1}},
                                 {"600000.SH": "S1"}, None))
    r = client.get(
        "/api/backtest?strategy=first_board_v04&start=20260101&end=20260105")
    assert r.status_code == 200
    assert captured["mkt"] == {"sector": {"880368": 1}}
    assert captured["sector_map"] == {"600000.SH": "S1"}
    assert r.get_json()["report"]["market_data"] is True


def test_backtest_market_data_missing_flagged(client, monkeypatch):
    """缓存空 → 报告带警示 note + market_data=False(不再静默零交易)。"""

    class FakeBT:
        def __init__(self, *a, **kw):
            pass

        def run(self, s, e, **kw):
            return {"trades": 0, "trading_days": 1, "gate_notes": [],
                    "filter_stats": {}}

    monkeypatch.setattr("prism.backtest.Backtester", FakeBT)
    monkeypatch.setattr(app_module, "load_market_data",
                        lambda: (None, None, "市场数据缓存为空 → 因子失效"))
    r = client.get(
        "/api/backtest?strategy=first_board_v04&start=20260101&end=20260105")
    rep = r.get_json()["report"]
    assert rep["market_data"] is False
    assert any("市场数据缓存为空" in n for n in rep["gate_notes"])


def test_backtest_injects_day_feed(client, monkeypatch):
    """网页回测必须接按日上下文(与 CLI 同一装配 build_day_feed):
    不接则 N3/N4/N5/F1/F6 恒 0 —— 网页与 CLI 口径不一致。"""
    from datetime import date
    captured = {}
    sentinel = object()

    class FakeBT:
        def __init__(self, strategy, zt_feed=None, kline_feed=None):
            pass

        def run(self, s, e, sell_rules=None, progress=None, **kw):
            captured.update(kw)
            return {"trades": 1, "trading_days": 1, "gate_notes": [],
                    "filter_stats": {}}

    def fake_build(start, end, **kw):
        captured["feed_range"] = (start, end)
        captured["feed_kw"] = kw
        return sentinel

    monkeypatch.setattr("prism.backtest.Backtester", FakeBT)
    monkeypatch.setattr(app_module, "build_day_feed", fake_build)
    monkeypatch.setattr(app_module, "load_market_data",
                        lambda: (None, None, None))
    r = client.get(
        "/api/backtest?strategy=first_board_v04&start=20260101&end=20260105")
    assert r.status_code == 200
    assert captured["feed_range"] == (date(2026, 1, 1), date(2026, 1, 5))
    assert captured["day_feed"] is sentinel, "day_feed 必须传给 run()"
    assert captured["feed_kw"].get("use_intraday") is True, \
        "1m 特征只读缓存(绝不下载), 网页与 CLI 同口径启用"
    assert r.get_json()["report"]["day_feed"] is True


def test_backtest_skips_validation_payload(client, monkeypatch):
    """网页回测传 validate=False: validation 自带 sharpe_samples(2000 个数)
    + equity_paths(≤30×400), 响应体会膨胀到 MB 级, 而网页不展示该字段。
    (默认仍跑 —— 规格"可选、默认跑"; CLI 要精简用 --no-validate。)"""
    captured = {}

    class FakeBT:
        def __init__(self, strategy, zt_feed=None, kline_feed=None):
            pass

        def run(self, s, e, sell_rules=None, progress=None, **kw):
            captured.update(kw)
            return {"trades": 1, "trading_days": 1, "gate_notes": [],
                    "filter_stats": {}}

    monkeypatch.setattr("prism.backtest.Backtester", FakeBT)
    monkeypatch.setattr(app_module, "build_day_feed", None)
    monkeypatch.setattr(app_module, "load_market_data",
                        lambda: (None, None, None))
    r = client.get(
        "/api/backtest?strategy=first_board_v04&start=20260101&end=20260105")
    assert r.status_code == 200
    assert captured["validate"] is False, "网页不传验证载荷(体积)"


def test_backtest_day_feed_failure_degrades_with_note(client, monkeypatch):
    """按日上下文装配失败 → 不阻塞回测(退化为无 day_feed) + 报告带 note。"""
    captured = {}

    class FakeBT:
        def __init__(self, strategy, zt_feed=None, kline_feed=None):
            pass

        def run(self, s, e, sell_rules=None, progress=None, **kw):
            captured.update(kw)
            return {"trades": 0, "trading_days": 1, "gate_notes": [],
                    "filter_stats": {}}

    def boom(start, end, **kw):
        raise RuntimeError("QMT 挂了")

    monkeypatch.setattr("prism.backtest.Backtester", FakeBT)
    monkeypatch.setattr(app_module, "build_day_feed", boom)
    monkeypatch.setattr(app_module, "load_market_data",
                        lambda: (None, None, None))
    r = client.get(
        "/api/backtest?strategy=first_board_v04&start=20260101&end=20260105")
    assert r.status_code == 200, "装配失败不该把回测打成 500"
    assert captured["day_feed"] is None
    rep = r.get_json()["report"]
    assert rep["day_feed"] is False
    assert any("按日上下文" in n for n in rep["gate_notes"])


def test_backtest_report_carries_factor_hits_and_data_notes(client, monkeypatch):
    """报告里的因子存活率与数据说明必须原样到网页(Task 5 §8)。

    存活率表/数据说明区的数据源就是这两个字段 —— 端点若把它们过滤掉,
    前端只会渲染出空表(静默), 所以这里钉住透传契约(只增不减)。
    """
    fh = {"F3": {"hits": 1, "evals": 4, "rate": 0.25, "kind": "scoring",
                 "status": "代理"},
          "N1": {"hits": 2, "evals": 2, "rate": 1.0, "kind": "gate",
                 "status": "实算"}}
    notes = ["1m特征: 个股日覆盖 1/2 (缓存缺失静默降级 → F2/F3 fail-open 0)",
             "F3 封单强度: 回测无盘口队列(bidVol 不可得) → 走分钟级代理"]

    class FakeBT:
        def __init__(self, strategy, zt_feed=None, kline_feed=None):
            pass

        def run(self, s, e, sell_rules=None, progress=None, **kw):
            return {"trades": 0, "trading_days": 1, "gate_notes": [],
                    "filter_stats": {}, "factor_hits": fh, "data_notes": notes}

    monkeypatch.setattr("prism.backtest.Backtester", FakeBT)
    monkeypatch.setattr(app_module, "build_day_feed", None)
    monkeypatch.setattr(app_module, "load_market_data",
                        lambda: (None, None, None))
    r = client.get(
        "/api/backtest?strategy=first_board_v04&start=20260101&end=20260105")
    assert r.status_code == 200
    rep = r.get_json()["report"]
    assert rep["factor_hits"] == fh, "存活率字段必须透传到网页"
    assert rep["data_notes"] == notes, "数据说明必须透传到网页"


def test_backtest_js_renders_factor_survival_and_data_notes():
    """前端契约(结构性): 交易结果下方的因子存活率表 + 数据说明区(Task 5 §8)。

    存活率表存在的意义就是"一眼看出哪些因子在空转", 所以钉住:
      - 表读 `factor_hits`(因子/类型/命中率/状态), 四个状态词都在前端可读;
      - `data_notes` 逐条公示(不可得的数据不许静默);
      - 成本列用 Task 4 的 `floor_cost_pct`(¥5 下限差额)且兼容旧报告;
      - **有交易/零交易两个分支都要渲染**(零交易正是最需要看存活率的时候)。
    """
    js = (Path(__file__).parent.parent / "static" / "app.js").read_text(
        encoding="utf-8")
    assert "function renderFactorHits" in js
    assert "factor_hits" in js
    assert "function renderDataNotes" in js and "data_notes" in js
    for mark in ("实算", "代理", "恒0", "未评估"):
        assert mark in js, "存活率状态缺 %s(前端必须能区分空转因子)" % mark
    assert "常数" in js, "rate==1.0 的常数因子要在命中率列标注"
    assert "floor_cost_pct" in js and "cost_pct" in js, "新成本字段 + 向后兼容"
    assert "escHtml(" in js                                  # XSS 契约
    assert js.count("renderFactorHits(r)") >= 2, \
        "有交易与零交易两个分支都要展示存活率"


# ---------------- 旧路由保留: 冒烟 ----------------

def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_screen_disconnected_returns_400(client, monkeypatch):
    # 自动重连后: 若 QMT 真不可达(connect 失败) → 仍 400 未连接
    class BrokenDS:
        _connected = False
        def connect(self):
            raise RuntimeError("QMT down")
    monkeypatch.setattr(app_module, "ds_obj", BrokenDS())
    r = client.post("/api/screen")
    assert r.status_code == 400
    assert "QMT未连接" in r.get_json()["error"]


def test_screen_unknown_strategy_400(client, monkeypatch):
    monkeypatch.setattr(app_module.ds_obj, "_connected", True)
    r = client.post("/api/screen", json={"strategy": "no_such"})
    assert r.status_code == 400
    assert "策略加载失败" in r.get_json()["error"]


def test_screen_runs_prism_engine(client, tmp_path, monkeypatch):
    """/api/screen 走 prism 引擎: 默认策略加载 + run_screen 编排 + 快照落盘。"""
    monkeypatch.setattr(app_module.ds_obj, "_connected", True)
    monkeypatch.setattr(app_module, "SNAPSHOT_PATH", tmp_path / "screen_result.json")

    class FakePerf:
        def archive_daily(self, candidates, d=None):
            return None
    monkeypatch.setattr(app_module, "perf_store_obj", FakePerf())

    canned = {
        "environment_ok": True, "gate_score": 3,
        "candidates": [{"code": "002859.SZ", "name": "洁美科技",
                        "scores": {"grade": "A", "composite": 5.0,
                                   "strength": "强", "position": "50%"},
                        "factors": {"F1": 1}, "up_stop_price": 22.0,
                        "last": 20.0}],
        "summary": {"candidate_count": 1, "a_count": 1, "b_count": 0,
                    "c_count": 0, "d_count": 0},
        "market": {"limit_up_count": 1},
    }
    monkeypatch.setattr(app_module, "_run_prism_screen",
                        lambda strategy, provider: dict(canned))
    r = client.post("/api/screen", json={"strategy": "first_board_v04"})
    assert r.status_code == 200
    data = r.get_json()
    assert data["environment_ok"] is True
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["code"] == "002859.SZ"
    # 快照落盘
    import json as _json
    snap = _json.loads((tmp_path / "screen_result.json").read_text(encoding="utf-8"))
    assert snap["environment_ok"] is True
    assert snap["candidates"][0]["code"] == "002859.SZ"


def test_manual_roundtrip(client, tmp_path, monkeypatch):
    """旧路由 /api/stock/<code>/manual 保留冒烟(临时文件避免污染)。

    手填因子已全部被 K线自动因子取代(2026-08; 原 S1/S5/S7 因子已于
    09-03 删除) → 写入任何非手填因子都被拒(400), 读取返回空 {}。"""
    from manual_store import ManualStore
    s = ManualStore(str(tmp_path / "m.json"))
    monkeypatch.setattr(app_module, "manual_store_obj", s)
    r = client.post("/api/stock/002859.SZ/manual", json={"F1": 1})
    assert r.status_code == 400          # F1 非手填因子 → 拒
    r2 = client.get("/api/stock/002859.SZ/manual")
    assert r2.get_json() == {}           # 无手填因子 → 空


# ---------------- C1: /api/screen market 载荷前端契约(stage/node_score/factors/...) ----------------

def _stub_provider(limit_ups=None, ticks=None, em=None):
    """契约测试用 stub provider: 受控 market 上下文 + 受控涨停池, 无网络/无 QMT。"""
    from prism.context import FactorContext
    limit_ups = limit_ups or []

    class FakeProvider:
        def build_market_context(self):
            return FactorContext(code="__MARKET__", limit_ups=limit_ups,
                                 ticks=ticks or {}, em=em or {})

        def get_limit_ups(self):
            return limit_ups

        def build_stock_context(self, code, **kw):
            return FactorContext(code=code)

    return FakeProvider()


def test_screen_market_payload_contract(client, tmp_path, monkeypatch):
    """/api/screen market 载荷契约(前端 app.js renderMarket 依赖, 审查 C1):
    stage/node_score/factors/total_amount/limit_up_count/top_themes 必须存在,
    缺任一字段前端都会渲染中断(Object.entries(undefined) TypeError)。"""
    from prism.context import FactorContext  # noqa: F401  (类型提示用)
    monkeypatch.setattr(app_module.ds_obj, "_connected", True)
    monkeypatch.setattr(app_module, "SNAPSHOT_PATH", tmp_path / "screen_result.json")
    monkeypatch.setattr(app_module, "perf_store_obj",
                        type("FakePerf", (), {"archive_daily": lambda self, c, d=None: None})())

    # stub 引擎: run_screen 固定输出 gate_score=4 → stage=回暖期
    def fake_run_screen(strategy, market_ctx, gate_factors=None, stock_contexts=None):
        return {"environment_ok": True, "gate_score": 4, "candidates": [],
                "summary": {"candidate_count": 0, "a_count": 0, "b_count": 0,
                            "c_count": 0, "d_count": 0}}
    monkeypatch.setattr(app_module, "run_screen", fake_run_screen)

    provider = _stub_provider(ticks={"000001.SZ": {"amount": 1.2e12}},
                              em={"top_themes": [{"name": "AI"}, {"name": "机器人"}]})
    monkeypatch.setattr(app_module, "DataProvider",
                        lambda ds=None, manual=None: provider)

    r = client.post("/api/screen", json={"strategy": "first_board_v04"})
    assert r.status_code == 200
    m = r.get_json()["market"]
    assert m["stage"] == "回暖期"          # gate_score=4 → classify_market
    assert m["node_score"] == 4
    assert m["gate_total"] == 1            # 门控标准总数=策略 gate 因子数(v04 仅 N1)
    assert m["total_amount"] == 1.2e12     # 从 market_ctx.ticks 求和
    assert m["limit_up_count"] == 0
    assert m["top_themes"] == [{"name": "AI"}, {"name": "机器人"}]
    # 门槛因子逐个收集为 {fid: {"score":…, "note":…}}
    # (v04 门槛仅 N1; 原 default 为 N1-N5 已删, full_factor_v1 落盘后可扩回多因子)
    for fid in ("N1",):
        assert fid in m["factors"], "market.factors 缺 %s" % fid
        assert "score" in m["factors"][fid] and "note" in m["factors"][fid]


# ---------------- I1: 候选合并涨停池字段(name/sealed/float_mv/auto_manual) ----------------

def test_screen_candidates_merged_fields(client, tmp_path, monkeypatch):
    """候选合并涨停池字段(审查 I1): name/sealed/float_mv/up_stop_price/last 补齐,
    auto_manual 来源推断供前端徽标, 不再显示 undefined/QMT 误导。"""
    monkeypatch.setattr(app_module.ds_obj, "_connected", True)
    monkeypatch.setattr(app_module, "SNAPSHOT_PATH", tmp_path / "screen_result.json")
    monkeypatch.setattr(app_module, "perf_store_obj",
                        type("FakePerf", (), {"archive_daily": lambda self, c, d=None: None})())

    def fake_run_screen(strategy, market_ctx, gate_factors=None, stock_contexts=None):
        return {"environment_ok": True, "gate_score": 4,
                "candidates": [{"code": "002859.SZ",
                                "scores": {"grade": "A", "composite": 5.0,
                                           "strength": "强", "position": "50%",
                                           "first_board": 4, "monster": 3,
                                           "momentum": 2},
                                "factors": {"F1": 1, "Y1": 1, "S2": 1, "F2": 0},
                                "up_stop_price": None, "last": None}],
                "summary": {"candidate_count": 1, "a_count": 1, "b_count": 0,
                            "c_count": 0, "d_count": 0}}
    monkeypatch.setattr(app_module, "run_screen", fake_run_screen)

    provider = _stub_provider(limit_ups=[{"code": "002859.SZ", "name": "洁美科技",
                                          "sealed": True, "float_volume": 5.0e8,
                                          "last": 20.0, "up_stop_price": 22.0}])
    monkeypatch.setattr(app_module, "DataProvider",
                        lambda ds=None, manual=None: provider)

    r = client.post("/api/screen", json={"strategy": "first_board_v04"})
    assert r.status_code == 200
    c = r.get_json()["candidates"][0]
    assert c["name"] == "洁美科技"
    assert c["sealed"] is True
    assert c["float_mv"] == 5.0e8 * 20.0
    assert c["up_stop_price"] == 22.0     # engine 透出 None → lu 兜底
    assert c["last"] == 20.0
    am = c["auto_manual"]
    assert am["F1"] == "auto"
    assert am["Y1"] == "fundamental"      # 东财个股因子
    assert am["S2"] == "auto"             # S2 现存非手填因子 → 归为 auto
    assert am["F2"] == "auto"


# ---------------- Minor: M7 单飞锁 409 / M1 start>end / M2 字符串false / M3 sid 白名单 ----------------

def test_screen_lock_conflict_409(client, monkeypatch):
    """单飞锁(审查 Minor 7): 选股进行中再请求 → 409。"""
    import threading as _t
    monkeypatch.setattr(app_module.ds_obj, "_connected", True)
    busy = _t.Lock()
    busy.acquire()
    monkeypatch.setattr(app_module, "_screen_lock", busy)
    try:
        r = client.post("/api/screen")
        assert r.status_code == 409
        assert "选股进行中" in r.get_json()["error"]
    finally:
        busy.release()


def test_backtest_start_after_end_400(client):
    """M1: start > end → 400(不再返回 200 空报告)。"""
    r = client.get("/api/backtest?strategy=first_board_v04&start=20260110&end=20260105")
    assert r.status_code == 400
    assert "不能晚于" in r.get_json()["error"]


def test_automation_paused_string_false_no_create(client, tmp_path, monkeypatch):
    """M2: {"paused": "false"}(字符串真值)不得误建暂停文件, 仅 is True 生效。"""
    import prism.trader as trader
    pf = tmp_path / "paused"
    monkeypatch.setattr(trader, "PAUSE_FILE", str(pf))
    monkeypatch.setattr(app_module, "trader", trader)
    r = client.post("/api/automation", json={"paused": "false"})
    assert r.status_code == 200
    assert not pf.exists(), "字符串 'false' 不应建暂停文件"
    r2 = client.post("/api/automation", json={"paused": True})
    assert r2.status_code == 200
    assert pf.exists()


def test_strategy_detail_bad_sid_404(client):
    r"""M3: sid 白名单 [\w-]+, 含路径穿越/非法字符 → 404。"""
    r = client.get("/api/strategy/..%2Ffoo")     # %2F 解码为 /, 防站外读取
    assert r.status_code == 404
    r2 = client.get("/api/strategy/foo.bar")
    assert r2.status_code == 404
    r3 = client.get("/api/backtest?strategy=foo.bar&start=20260101&end=20260105")
    assert r3.status_code == 404


# ---------- QMT 自动重连 ----------

class FakeReconnectDS:
    """模拟连接状态: 初始 _connected=False, connect() 后恢复 True。
    用实例属性(测试间不污染)。"""
    def __init__(self):
        self._connected = False
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        self._connected = True


def test_health_auto_reconnects_when_disconnected(client, monkeypatch):
    r"""QMT 连接失效后, /api/health 应自动重连并恢复状态。"""
    fake = FakeReconnectDS()
    monkeypatch.setattr(app_module, "ds_obj", fake)
    # 初始未连接 → health 触发重连 → 返回已连接
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.get_json()
    assert data["qmt_connected"] is True
    assert fake.connect_calls >= 1


def test_screen_auto_reconnects_before_qmt_call(client, monkeypatch):
    r"""/api/screen 在 QMT 未连接时自动重连, 而非直接 400。"""
    fake = FakeReconnectDS()
    monkeypatch.setattr(app_module, "ds_obj", fake)
    # 用假 provider 替代真实选股(连接成功后进入选股流程)
    class FakeProvider:
        def build_market_context(self):
            from prism.context import FactorContext
            return FactorContext(code="__MKT__")
        def get_limit_ups(self):
            return []
    monkeypatch.setattr(app_module, "DataProvider",
                        lambda ds=None, manual=None: FakeProvider())
    r = client.post("/api/screen")
    # 重连成功 → 不再 400"未连接"(进入选股流程, 涨停池空 → 环境不达标也返回 200)
    assert r.status_code != 400
    assert fake.connect_calls >= 1


def test_screen_still_400_when_reconnect_fails(client, monkeypatch):
    r"""重连失败(connect 抛异常) → 仍返回 400 未连接。"""
    class BrokenDS(FakeReconnectDS):
        def connect(self):
            raise RuntimeError("QMT down")

    fake = BrokenDS()
    monkeypatch.setattr(app_module, "ds_obj", fake)
    r = client.post("/api/screen")
    assert r.status_code == 400
    assert "QMT未连接" in r.get_json()["error"]


# ---------------- 模拟盘面板 ----------------

def test_paper_endpoints_not_initialized(client, tmp_path, monkeypatch):
    """未初始化账本 → exists=false, 200 不报错。"""
    import prism.paper as paper_mod

    class FakeAcc:
        def __init__(self, **kw):
            self.state_path = tmp_path / "p.json"
            self.state = None
            self._strategy = None
            self.initial_capital = 1000000.0
        def summary(self):
            return {"exists": False}
        def detail(self, trade_limit=50):
            return {"exists": False}

    monkeypatch.setattr(paper_mod, "PaperAccount", FakeAcc)
    r = client.get("/api/paper/summary")
    assert r.status_code == 200
    assert r.get_json()["exists"] is False
    r2 = client.get("/api/paper/detail")
    assert r2.status_code == 200 and r2.get_json()["exists"] is False


def test_paper_endpoints_with_ledger(client, tmp_path, monkeypatch):
    """有账本 → summary 字段契约 + detail 持仓/流水透出(子类注入 tmp 账本)。"""
    import prism.paper as paper_mod

    class RealTmpAcc(paper_mod.PaperAccount):
        def __init__(self, **kw):
            super().__init__(state_path=tmp_path / "p.json", **kw)
    monkeypatch.setattr(paper_mod, "PaperAccount", RealTmpAcc)
    acc = RealTmpAcc()
    acc.init_account(created="2026-09-01")
    acc.state["holdings"].append({
        "code": "600000.SH", "shares": 29900, "cost": 10.01,
        "buy_date": "2026-09-01", "buy_price": 10.01, "entry_nav": 1e6})
    acc.save()
    r = client.get("/api/paper/summary")
    s = r.get_json()
    assert s["exists"] is True and s["holdings_count"] == 1
    assert "total_return_pct" in s and "nav_points" in s
    d = client.get("/api/paper/detail").get_json()
    assert d["holdings"][0]["code"] == "600000.SH"


def test_paper_pending_dom(client):
    """模拟盘 tab 含排队区骨架(排队中 + paper-pending)。"""
    r = client.get("/")
    html = r.get_data(as_text=True)
    assert "排队中" in html and "paper-pending" in html


def test_paper_detail_pending_field(client, tmp_path, monkeypatch):
    """detail 透出 pending 排队区(含委托字段); summary 带 pending_count。"""
    import prism.paper as paper_mod

    class RealTmpAcc(paper_mod.PaperAccount):
        def __init__(self, **kw):
            super().__init__(state_path=tmp_path / "p.json", **kw)
    monkeypatch.setattr(paper_mod, "PaperAccount", RealTmpAcc)
    acc = RealTmpAcc()
    acc.init_account(created="2026-09-01")
    acc.state["pending_buys"].append({
        "code": "600000.SH", "shares": 30000, "price": 10.0,
        "amount": 300000.0, "frozen": 300000.0,
        "queued_shares": 2000000, "base_volume": 1000000,
        "created": "2026-09-02T10:00:05", "slot": "T10:00"})
    acc.save()
    s = client.get("/api/paper/summary").get_json()
    assert s["pending_count"] == 1
    d = client.get("/api/paper/detail").get_json()
    assert d["pending"][0]["code"] == "600000.SH"
    assert d["pending"][0]["queued_shares"] == 2000000


# ---------------- 策略编辑器端点(Task 4: create/activate/active 字段) ----------------

_EDITOR_PAYLOAD = {
    "name": "测试组合",
    "models": [{"id": "first_board", "name": "首板", "weight": 1.0,
                "factors": ["F1", "F8"], "weights": [1, 1]}],
    "gate_factors": ["N1"], "gate_threshold": 1,
    "candidate_min_model": 3,
    "sell": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
             "max_hold_days": 5}}


def test_strategy_create_ok(client, tmp_path, monkeypatch):
    import prism.engine as engine
    monkeypatch.setattr(app_module, "STRATEGIES_DIR", tmp_path)
    r = client.post("/api/strategies/create", json=_EDITOR_PAYLOAD)
    assert r.status_code == 200
    sid = r.get_json()["id"]
    assert sid.startswith("custom_")
    p = tmp_path / ("%s.json" % sid)
    assert p.is_file()
    s = engine.load_strategy(p)          # 试载即可用
    assert s["id"] == sid and s["composite"]["cap"] == 2.0


def test_strategy_create_reject_and_no_write(client, tmp_path, monkeypatch):
    bad = dict(_EDITOR_PAYLOAD, models=[{"id": "m", "name": "m",
                                        "weight": 1.0,
                                        "factors": ["F1", "ZZ9"],
                                        "weights": []}])
    r = client.post("/api/strategies/create", json=bad)
    assert r.status_code == 400
    assert r.get_json()["ok"] is False and r.get_json()["errors"]
    assert not any(tmp_path.glob("custom_*.json"))


def test_strategy_activate(client, tmp_path, monkeypatch):
    import prism.engine as engine
    monkeypatch.setattr(app_module, "STRATEGIES_DIR", tmp_path)
    monkeypatch.setattr(engine, "STRATEGIES_DIR", tmp_path)
    (tmp_path / "my.json").write_text(
        engine.json.dumps({"id": "my", "name": "x",       # engine 用 import json(蓝图 _json 笔误修正)
                           "scoring_models": []}), encoding="utf-8")
    r404 = client.post("/api/strategies/no_such/activate")
    assert r404.status_code == 404
    r = client.post("/api/strategies/my/activate")
    assert r.status_code == 200 and r.get_json()["active"] == "my"
    assert engine.active_strategy_id() == "my"     # 指针已写(tmp 注入)


def test_strategies_list_has_active(client):
    import prism.engine as engine
    r = client.get("/api/strategies")
    assert r.status_code == 200
    assert r.get_json()["active"] == engine.active_strategy_id()


def test_strategies_list_skips_pointer_file(client, tmp_path, monkeypatch):
    """I-1(审查): .active.json 指针被 *.json glob 误收 → 列表混入
    {id:…, name:None} 伪条目。锁定: 列表仅含真策略, active 照常透出。"""
    import json as _json
    import prism.engine as engine
    monkeypatch.setattr(app_module, "STRATEGIES_DIR", tmp_path)
    monkeypatch.setattr(engine, "STRATEGIES_DIR", tmp_path)
    (tmp_path / "my.json").write_text(
        _json.dumps({"id": "my", "name": "我的策略", "description": "d"}),
        encoding="utf-8")
    engine.set_active_strategy("my")     # 生成 .active.json(伪条目源头)
    r = client.get("/api/strategies")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert [s["id"] for s in data["strategies"]] == ["my"]   # 无 .active 伪条目
    assert data["active"] == "my"


def test_strategy_create_trial_load_fail_no_write(client, tmp_path, monkeypatch):
    """试载硬条件(Task 3 复审): 校验已过但 load_strategy 抛错 → 400 + 零写盘。"""
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(app_module, "STRATEGIES_DIR", tmp_path)
    monkeypatch.setattr(app_module, "load_strategy", _boom)
    r = client.post("/api/strategies/create", json=_EDITOR_PAYLOAD)
    assert r.status_code == 400
    assert "试载失败" in r.get_json()["errors"][0]
    assert not any(tmp_path.iterdir())   # 零写盘: 正式文件与 .tmp 都不留


def test_strategy_create_never_overwrites_same_second(client, tmp_path, monkeypatch):
    """spec §7-1: 同秒 id 冲突 → 加 _2 序号, 绝不覆盖已存在文件。"""
    import datetime
    monkeypatch.setattr(app_module, "STRATEGIES_DIR", tmp_path)

    class _FixedDT:                      # 固定时钟 → 冲突路径可复现
        class datetime:                  # 与 app 的 _dt(datetime 模块) 同形
            @staticmethod
            def now():
                return datetime.datetime(2026, 9, 1, 12, 0, 0)

    monkeypatch.setattr(app_module, "_dt", _FixedDT)
    clash = tmp_path / "custom_20260901_120000.json"
    clash.write_text('{"id": "sentinel"}', encoding="utf-8")
    r = client.post("/api/strategies/create", json=_EDITOR_PAYLOAD)
    assert r.status_code == 200
    sid = r.get_json()["id"]
    assert sid == "custom_20260901_120000_2"
    assert clash.read_text(encoding="utf-8") == '{"id": "sentinel"}'  # 未被覆盖
    assert (tmp_path / (sid + ".json")).is_file()


# ---------------- Task 5: 策略编辑面板 DOM 冒烟 ----------------

def test_strategy_editor_dom(client):
    """编辑面板骨架全部渲染(模板完整性)。"""
    r = client.get("/")
    html = r.get_data(as_text=True)
    for mark in ("btn-new-strategy", "strategy-editor", "ed-name",
                 "ed-models", "ed-gate", "ed-tp", "ed-sl", "ed-hold"):
        assert mark in html, "模板缺 %s" % mark


# ---------------- 板块观察 ----------------

def _sector_stage_snap():
    """罐头 mkt 快照: A 平淡(休整) + 惯性 1 条。"""
    from datetime import date, timedelta
    ds = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(30)]
    return {
        "sector": {"801010": {"name": "农林牧渔", "dates": ds,
                              "close": [100.0] * 30,
                              "amount": [1e8] * 30}},
        "benchmark": {"dates": ds, "close": [1000.0] * 30,
                      "amount": [1e8] * 30},
        "flow_rank": {"dates": ds[-2:],
                      "rows": {ds[-2]: [{"code": "BK0433",
                                         "name": "农林牧渔",
                                         "net_in": 1e9}],
                               ds[-1]: [{"code": "BK0433",
                                         "name": "农林牧渔",
                                         "net_in": 2e9}]}},
    }


@pytest.mark.usefixtures("_weekly_stub")
def test_sector_stage_endpoint(client, monkeypatch):
    import prism.market_data as md
    monkeypatch.setattr(md, "mkt_snapshot", _sector_stage_snap)
    r = client.get("/api/sector_stage")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["date"] == "2026-09-30"
    assert data["flow_days"] == 2
    assert data["sectors"][0]["code"] == "801010"
    assert data["sectors"][0]["name"] == "农林牧渔"
    assert data["inertia"][0]["streak"] == 2


@pytest.mark.usefixtures("_weekly_stub")
def test_sector_stage_endpoint_empty(client, monkeypatch):
    import prism.market_data as md
    monkeypatch.setattr(md, "mkt_snapshot", lambda: {})
    r = client.get("/api/sector_stage")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["sectors"] == [] and data["inertia"] == []


@pytest.mark.usefixtures("_weekly_stub")
def test_sector_stage_endpoint_error_failopen(client, monkeypatch):
    import prism.market_data as md
    def boom():
        raise RuntimeError("x")
    monkeypatch.setattr(md, "mkt_snapshot", boom)
    r = client.get("/api/sector_stage")
    assert r.status_code == 200
    assert r.get_json()["ok"] is False


# ---------------- 板块周度跟踪(W2): API 组装 + DOM ----------------

@pytest.fixture()
def _weekly_stub(monkeypatch):
    """周度组装件离线桩: 端点测试不触碰真实缓存/xtquant(离线确定性)。

    raising=False: 组装件尚未实现时本桩不炸(存量端点测试保持原语义)。"""
    monkeypatch.setattr(app_module, "_sector_new_high", lambda: None,
                        raising=False)
    monkeypatch.setattr(app_module, "_sector_etf_quotes", lambda: {},
                        raising=False)


def _sector_weekly_snap():
    """罐头快照: 申万 801780"银行" 与东财 BK1283"银行" 同名并存(W1 交接坑)。"""
    from datetime import date, timedelta
    ds = [(date(2026, 9, 1) + timedelta(days=i)).isoformat() for i in range(30)]
    rec = {"dates": ds, "close": [100.0] * 30, "amount": [1e8] * 30}
    return {
        "sector": {"801780": dict(rec, name="银行"),
                   "BK1283": dict(rec, name="银行")},
        "benchmark": {"dates": ds, "close": [1000.0] * 30,
                      "amount": [1e8] * 30},
        "flow_rank": {"dates": [], "rows": {}},
    }


@pytest.mark.usefixtures("_weekly_stub")
def test_sector_stage_weekly_columns_injected(client, monkeypatch):
    """注入 new_high/etf_quotes 后四列透出, 且 BK 同名行被过滤(只留 801)。"""
    import prism.market_data as md
    from prism.sector_etf_map import SECTOR_ETF_MAP
    monkeypatch.setattr(md, "mkt_snapshot", _sector_weekly_snap)
    monkeypatch.setattr(app_module, "_sector_new_high",
                        lambda: {"银行": {"nh": 3, "base": 100}})
    monkeypatch.setattr(app_module, "_sector_etf_quotes",
                        lambda: {"512800.SH": {"amount": 2.5e8,
                                               "pct_chg": 1.2}})
    r = client.get("/api/sector_stage")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    rows = data["sectors"]
    assert [x["code"] for x in rows] == ["801780"]     # BK1283 同名行被滤掉
    row = rows[0]
    assert row["new_high"] == {"nh": 3, "base": 100}
    assert row["etf"]["code"] == SECTOR_ETF_MAP["银行"]["code"]
    assert row["etf"]["amount"] == 2.5e8 and row["etf"]["pct_chg"] == 1.2
    assert row["pos_cap"] == 30                        # 休整 → 纸面上限 30
    assert row["week_rank"] == 1


@pytest.mark.usefixtures("_weekly_stub")
def test_sector_stage_endpoint_bk_only_universe_warns(client, monkeypatch,
                                                     caplog):
    """缓存全是 BK 码 → 801 过滤后 0 行: ok 不炸 + warning 提示(M-4)。"""
    import logging
    import prism.market_data as md
    snap = _sector_weekly_snap()
    snap["sector"] = {"BK0486": snap["sector"]["801780"]}   # 全 BK, 无 801
    monkeypatch.setattr(md, "mkt_snapshot", lambda: snap)
    with caplog.at_level(logging.WARNING, logger="prism_web"):
        r = client.get("/api/sector_stage")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["sectors"] == []
    assert any("801" in rec.message for rec in caplog.records)


@pytest.mark.usefixtures("_weekly_stub")
def test_sector_stage_weekly_missing_degrades_none(client, monkeypatch):
    """组装件缺供(None/{}) → new_high/etf 列退化 None, 其余列不受牵连。"""
    import prism.market_data as md
    called = []
    monkeypatch.setattr(md, "mkt_snapshot", _sector_weekly_snap)

    def nh():
        called.append("nh")
        return None

    def quotes():
        called.append("q")
        return {}

    monkeypatch.setattr(app_module, "_sector_new_high", nh)
    monkeypatch.setattr(app_module, "_sector_etf_quotes", quotes)
    r = client.get("/api/sector_stage")
    data = r.get_json()
    assert data["ok"] is True
    assert called == ["nh", "q"]                       # 组装件确被接线
    row = data["sectors"][0]
    assert row["new_high"] is None
    # etf_map 注入下行情缺供 → 锚点仍在、数值退化 None(W1 sector_table 契约)
    from prism.sector_etf_map import SECTOR_ETF_MAP
    assert row["etf"]["code"] == SECTOR_ETF_MAP["银行"]["code"]
    assert row["etf"]["amount"] is None and row["etf"]["pct_chg"] is None
    assert row["pos_cap"] == 30 and row["week_rank"] == 1


def _fake_caches(monkeypatch, tmp_path, md_cache, zt_cache, calls):
    """注入假缓存加载器(离线): calls 复盘 _load_cache 真实调用次数。"""
    import prism.market_data as md
    import prism.zt_history as zt
    zt_file = tmp_path / "zt.pkl"
    md_file = tmp_path / "md.pkl"
    zt_file.write_bytes(b"x")
    md_file.write_bytes(b"x")
    monkeypatch.setattr(zt, "CACHE_PATH", zt_file)
    monkeypatch.setattr(md, "CACHE_PATH", md_file)

    def zt_load():
        calls.append("zt")
        return zt_cache

    def md_load():
        calls.append("md")
        return md_cache

    monkeypatch.setattr(zt, "_load_cache", zt_load)
    monkeypatch.setattr(md, "_load_cache", md_load)


def test_sector_new_high_translation_and_memo(monkeypatch, tmp_path):
    """双重翻译(6位码→带后缀→801→行业名) + 按 mtime+当日 memo 只算一次。"""
    import os
    import time
    md_cache = {"sectors": {"801010": {"name": "农林牧渔"},
                            "BK1283": {"name": "银行"}},
                "sector_map": {"600051": {"sector": "801010"},
                               "300750": {"sector": "801010"},
                               "000002": {"sector": "801010"}}}
    zt_cache = {"600051.SH": {"close": [10.0] * 59 + [11.0]},   # 创新高
                "300750.SZ": {"close": [20.0] * 60},            # 持平=新高
                "000002.SZ": {"close": [5.0] * 10}}             # 历史不足
    calls = []
    _fake_caches(monkeypatch, tmp_path, md_cache, zt_cache, calls)
    monkeypatch.setattr(app_module, "_WEEKLY_NH_MEMO",
                        {"key": None, "val": None})
    out = app_module._sector_new_high()
    assert out == {"农林牧渔": {"nh": 2, "base": 2}}
    app_module._sector_new_high()
    assert calls == ["md", "zt"]                       # memo 命中, 不重算
    os.utime(tmp_path / "zt.pkl", (time.time() + 10,) * 2)      # 缓存变了
    app_module._sector_new_high()
    assert calls.count("zt") == 2


def test_sector_new_high_failopen(monkeypatch, tmp_path):
    """zt 缓存炸/缓存文件缺失 → 返回 None 不抛(fail-open)。"""
    import prism.zt_history as zt
    monkeypatch.setattr(app_module, "_WEEKLY_NH_MEMO",
                        {"key": None, "val": None})
    _fake_caches(monkeypatch, tmp_path, {"sectors": {}, "sector_map": {}},
                 {}, [])
    monkeypatch.setattr(zt, "_load_cache",
                        lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert app_module._sector_new_high() is None
    monkeypatch.setattr(zt, "CACHE_PATH", tmp_path / "missing.pkl")
    assert app_module._sector_new_high() is None


def test_sector_etf_quotes_assembly(monkeypatch):
    """codes = SECTOR_ETF_MAP 全部非空锚点; fetch 炸 → {} (fail-open)。"""
    import prism.market_data as md
    from prism.sector_etf_map import SECTOR_ETF_MAP
    seen = {}

    def fake_fetch(codes):
        seen["codes"] = codes
        return {"512800.SH": {"amount": 1.0, "pct_chg": 0.5}}

    monkeypatch.setattr(md, "fetch_etf_quotes", fake_fetch)
    out = app_module._sector_etf_quotes()
    assert out["512800.SH"] == {"amount": 1.0, "pct_chg": 0.5}
    want = sorted({a["code"] for a in SECTOR_ETF_MAP.values() if a})
    assert seen["codes"] == want
    monkeypatch.setattr(md, "fetch_etf_quotes",
                        lambda codes: (_ for _ in ()).throw(RuntimeError("x")))
    assert app_module._sector_etf_quotes() == {}


def test_sector_tab_weekly_dom(client):
    """板块观察 tab: 新列表头 + 纸面参考/锚点口径注明。"""
    html = client.get("/").get_data(as_text=True)
    for mark in ("周排名", "ETF锚点", "60日新高", "建议上限",
                 "建议上限为纸面参考，未接入交易", "流动性最好的代表品种"):
        assert mark in html, "板块观察模板缺 %s" % mark


def test_sector_tab_weekly_js_contract():
    """前端契约(结构性): 默认按 week_rank 升序 + 新列走既有契约。"""
    js = (Path(__file__).parent.parent / "static" / "app.js").read_text(
        encoding="utf-8")
    assert "week_rank" in js                                # 排序键
    assert "new_high" in js and "pos_cap" in js             # 数值列
    assert "escHtml(etf" in js                              # XSS 契约
    assert "1e8" in js                                      # 成交额折亿
    assert "<td>—</td>" in js                               # 留空锚点显示—


# ---------------- 个股K线降级(交付视角 2026-09-15) ----------------

def _fake_kdf(n=70):
    """同结构假 K线 DataFrame(open/high/low/close/volume, 日期在索引)。"""
    import pandas as pd
    idx = pd.date_range("2026-06-01", periods=n, freq="D")
    return pd.DataFrame({
        "open": [10.0 + i * 0.1 for i in range(n)],
        "high": [10.5 + i * 0.1 for i in range(n)],
        "low": [9.5 + i * 0.1 for i in range(n)],
        "close": [10.2 + i * 0.1 for i in range(n)],
        "volume": [1000 + i for i in range(n)],
    }, index=idx)


def test_stock_kline_falls_back_to_tdx(client, monkeypatch):
    """QMT 取数失败 → 降级通达信, 个股K线不因 QMT 停机不可用。"""
    import prism.tdx_source as tdx
    monkeypatch.setattr(app_module.ds_obj, "get_kline",
                        lambda code, days=120:
                        (_ for _ in ()).throw(RuntimeError("QMT down")))
    monkeypatch.setattr(app_module.ds_obj, "get_instrument",
                        lambda code:
                        (_ for _ in ()).throw(RuntimeError("QMT down")))
    monkeypatch.setattr(tdx, "get_kline", lambda code, days=120: _fake_kdf())
    r = client.get("/api/stock/600519/kline")
    assert r.status_code == 200
    d = r.get_json()
    assert len(d["closes"]) == 70 and len(d["dates"]) == 70
    assert d["up_stop"] == 0                     # 涨停价缺失不牵连K线


def test_stock_kline_both_sources_down(client, monkeypatch):
    """两个源都挂 → 503 + 友好错误(交付视角: 不返回 500 裸异常)。"""
    import prism.tdx_source as tdx
    monkeypatch.setattr(app_module.ds_obj, "get_kline",
                        lambda code, days=120:
                        (_ for _ in ()).throw(RuntimeError("QMT down")))
    monkeypatch.setattr(tdx, "get_kline", lambda code, days=120: None)
    r = client.get("/api/stock/600519/kline")
    assert r.status_code == 503
    body = r.get_json()
    assert body["ok"] is False and "行情源不可用" in body["error"]


# ---------------- 分级写护栏(spec 2026-09-15-tiered-guard) ----------------

_REMOTE_ENV = {"REMOTE_ADDR": "127.0.0.1", "HTTP_CF_CONNECTING_IP": "203.0.113.7"}


def test_guard_is_local_request():
    """判据唯一真相: CF头→远程; loopback/RFC1918→本机; 其他公网→远程。"""
    assert app_module.is_local_request(None, "127.0.0.1") is True
    assert app_module.is_local_request(None, "::1") is True
    assert app_module.is_local_request(None, "192.168.1.50") is True
    assert app_module.is_local_request(None, "10.0.0.3") is True
    assert app_module.is_local_request(None, "172.20.1.5") is True
    assert app_module.is_local_request(None, "172.32.1.5") is False   # 边界
    assert app_module.is_local_request(None, "100.64.1.2") is False
    assert app_module.is_local_request(None, "203.0.113.7") is False
    assert app_module.is_local_request("1.2.3.4", "127.0.0.1") is False


def test_guard_unit_matrix():
    """钩子判定矩阵(纯单元, 不发真实请求——test_request_context 只定
    endpoint 不执行路由): 敏感四路径×远程→403; 放行清单/GET/本机/局域网→None。"""
    sensitive = {  # 真实路径 → 函数名(endpoint)
        "/api/strategies/full_factor_v1/activate": "api_strategy_activate",
        "/api/stock/600519/manual": "manual",
        "/api/automation": "api_automation",
        "/api/perf/backfill": "perf_backfill",
    }
    allowed = ("/api/screen", "/api/strategies/create", "/api/market/limitup")
    for path, fn in sensitive.items():
        with appmod_ctx("POST", path, "203.0.113.7"):
            assert app_module.request.endpoint == fn       # 路径→函数名核对
            assert app_module._local_only_guard() is not None, fn
        with appmod_ctx("POST", path, None, "127.0.0.1"):
            assert app_module._local_only_guard() is None, fn
        with appmod_ctx("POST", path, None, "192.168.1.50"):
            assert app_module._local_only_guard() is None, fn
    for path in allowed:
        with appmod_ctx("POST", path, "203.0.113.7"):
            assert app_module._local_only_guard() is None, path  # 远程放行
        with appmod_ctx("POST", path, None, "127.0.0.1"):
            assert app_module._local_only_guard() is None, path  # 本机放行
    # GET 永远放行(即使敏感路径+远程)
    with appmod_ctx("GET", "/api/automation", "203.0.113.7"):
        assert app_module._local_only_guard() is None


def appmod_ctx(method, path, cf_ip, remote="203.0.113.7"):
    """护栏单元测试用请求上下文(真实路径 → 解析出真实 endpoint)。"""
    return app_module.app.test_request_context(
        path, method=method,
        environ_base={"REMOTE_ADDR": remote or ""},
        headers={} if cf_ip is None else {"CF-Connecting-IP": cf_ip})


def test_guard_integration_remote_sensitive_403(client):
    """集成抽查: 远程 POST 敏感路由经 client → 403 + 文案(钩子直拦, 不走路由)。"""
    r = client.post("/api/strategies/full_factor_v1/activate", json={},
                    environ_base=_REMOTE_ENV)
    assert r.status_code == 403
    assert "仅限本机" in r.get_json()["error"]
    r = client.post("/api/automation", json={"paused": True},
                    environ_base=_REMOTE_ENV)
    assert r.status_code == 403


# ================= 首板盘后拆解(观察层, 2026-09-19) =================
# 约束: 网页路径绝不下载 1m 特征 —— 这里断言读落盘/只读采集两条路径,
# 且采集异常必须 fail-open(200 + ok:false), 不 500。

def _fb_item(code="600001.SH", total=80, conf="高"):
    dims = {k: {"score": 80, "available": True, "evidence": ["依据"]}
            for k in ("seal", "sector", "volume", "fund", "industry")}
    return {"record": {"code": code, "name": "某股", "seal_time": "09:35",
                       "sector_zt_count": 5,
                       "sources": {"seal_amount": "unknown"}},
            "analysis": {"total": total, "confidence": conf, "dims": dims}}


def test_first_board_reads_snapshot(client, tmp_path, monkeypatch):
    """有落盘 → 直接读落盘, 不触发采集。"""
    monkeypatch.setattr(app_module, "_FBR_STATE_DIR", tmp_path)
    (tmp_path / "first_board_review_20260918.json").write_text(
        json.dumps({"date": "20260918", "count": 1, "items": [_fb_item()]}),
        encoding="utf-8")
    called = {"n": 0}

    def _boom(d, timeout=90):
        called["n"] += 1
        raise AssertionError("有落盘时不应触发采集")

    monkeypatch.setattr(app_module, "_fbr_collect", _boom)
    d = client.get("/api/first_board?date=20260918").get_json()
    assert d["ok"] is True and d["count"] == 1
    assert d["items"][0]["record"]["code"] == "600001.SH"
    assert called["n"] == 0        # 有落盘就不该采集


def test_first_board_collect_when_no_snapshot(client, tmp_path, monkeypatch):
    """无落盘 → 只读采集一次; date 归一成 8 位。"""
    monkeypatch.setattr(app_module, "_FBR_STATE_DIR", tmp_path)
    seen = {}

    def fake_collect(day8, timeout=90):
        seen["day"] = day8
        return [{"code": "600002.SH", "name": "另一只"}], None

    monkeypatch.setattr(app_module, "_fbr_collect", fake_collect)
    d = client.get("/api/first_board?date=2026-09-18").get_json()
    assert d["ok"] is True
    assert seen["day"] == "20260918"
    assert d["items"][0]["record"]["code"] == "600002.SH"
    # 内存兜底路径也要带 analysis(前端依赖五维渲染)
    assert "analysis" in d["items"][0]


def test_first_board_collect_error_fail_open(client, tmp_path, monkeypatch):
    """采集失败 → 200 + ok:false(不 500)。"""
    monkeypatch.setattr(app_module, "_FBR_STATE_DIR", tmp_path)
    monkeypatch.setattr(app_module, "_fbr_collect",
                        lambda d, timeout=90: (None, "QMT 离线"))
    r = client.get("/api/first_board?date=20260918")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] is False and "QMT" in d["error"]
    assert d["items"] == []


def test_first_board_bad_date(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_FBR_STATE_DIR", tmp_path)
    r = client.get("/api/first_board?date=abc")
    assert r.status_code == 200
    assert r.get_json()["ok"] is False


def test_first_board_refresh_forces_recollect(client, tmp_path, monkeypatch):
    """refresh=1 → 忽略落盘强制重算。"""
    monkeypatch.setattr(app_module, "_FBR_STATE_DIR", tmp_path)
    (tmp_path / "first_board_review_20260918.json").write_text(
        json.dumps({"date": "20260918", "count": 1,
                    "items": [_fb_item("600009.SH")]}), encoding="utf-8")
    seen = {"n": 0}

    def fake_collect(day8, timeout=90):
        seen["n"] += 1
        return [{"code": "600010.SH", "name": "重算"}], None

    monkeypatch.setattr(app_module, "_fbr_collect", fake_collect)
    d1 = client.get("/api/first_board?date=20260918").get_json()
    assert seen["n"] == 0
    assert d1["items"][0]["record"]["code"] == "600009.SH"
    d2 = client.get("/api/first_board?date=20260918&refresh=1").get_json()
    assert seen["n"] == 1
    assert d2["items"][0]["record"]["code"] == "600010.SH"


def test_first_board_dates_lists_snapshots(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_FBR_STATE_DIR", tmp_path)
    for d in ("20260917", "20260918"):
        (tmp_path / f"first_board_review_{d}.json").write_text(
            json.dumps({"date": d, "count": 0, "items": []}), encoding="utf-8")
    (tmp_path / "other.json").write_text("{}", encoding="utf-8")
    d = client.get("/api/first_board/dates").get_json()
    assert d["ok"] is True
    assert d["dates"] == ["20260918", "20260917"]   # 新→旧


def test_first_board_empty_dir_returns_empty(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_FBR_STATE_DIR", tmp_path / "nope")
    d = client.get("/api/first_board/dates").get_json()
    assert d["ok"] is True and d["dates"] == []


def test_index_has_firstboard_tab(client):
    """首页含首板拆解 tab 与面板(前端接线冒烟)。"""
    html = client.get("/").get_data(as_text=True)
    assert 'data-tab="firstboard"' in html
    assert 'id="tab-firstboard"' in html
    assert 'id="fb-table"' in html
