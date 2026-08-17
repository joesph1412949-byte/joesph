# -*- coding: utf-8 -*-
r"""prism_web app 测试 — 新 API(因子库/策略/回测/自动化) + 旧路由冒烟保留。

运行: cd D:\cc-joesph && python -m pytest prism_web/tests/ -v
"""
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
    assert any(s["id"] == "default" for s in data["strategies"])


def test_strategy_detail(client):
    r = client.get("/api/strategy/default")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert data["strategy"]["id"] == "default"
    assert data["strategy"]["scoring_models"]


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
    r = client.get("/api/backtest?strategy=default&start=20260101&end=20260105")
    assert r.status_code == 500
    assert r.get_json()["ok"] is False
    assert "回测数据源不可用" in r.get_json()["error"]


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
    r = client.post("/api/screen", json={"strategy": "default"})
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

    手填因子已全部被 K线自动因子取代(2026-08): S1/S5/S7 移出手填集合 →
    写入任何手填因子都被拒(400), 读取返回空 {}。"""
    from manual_store import ManualStore
    s = ManualStore(str(tmp_path / "m.json"))
    monkeypatch.setattr(app_module, "manual_store_obj", s)
    r = client.post("/api/stock/002859.SZ/manual", json={"S1": 1})
    assert r.status_code == 400          # S1 不再是手填因子 → 拒
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

    r = client.post("/api/screen", json={"strategy": "default"})
    assert r.status_code == 200
    m = r.get_json()["market"]
    assert m["stage"] == "回暖期"          # gate_score=4 → classify_market
    assert m["node_score"] == 4
    assert m["total_amount"] == 1.2e12     # 从 market_ctx.ticks 求和
    assert m["limit_up_count"] == 0
    assert m["top_themes"] == [{"name": "AI"}, {"name": "机器人"}]
    # 门槛因子逐个收集为 {fid: {"score":…, "note":…}}
    for fid in ("N1", "N2", "N3", "N4", "N5"):
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
                                "factors": {"F1": 1, "Y1": 1, "S1": 1, "F2": 0},
                                "up_stop_price": None, "last": None}],
                "summary": {"candidate_count": 1, "a_count": 1, "b_count": 0,
                            "c_count": 0, "d_count": 0}}
    monkeypatch.setattr(app_module, "run_screen", fake_run_screen)

    provider = _stub_provider(limit_ups=[{"code": "002859.SZ", "name": "洁美科技",
                                          "sealed": True, "float_volume": 5.0e8,
                                          "last": 20.0, "up_stop_price": 22.0}])
    monkeypatch.setattr(app_module, "DataProvider",
                        lambda ds=None, manual=None: provider)

    r = client.post("/api/screen", json={"strategy": "default"})
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
    assert am["S1"] == "auto"             # S1 已移出手填集合(2026-08) → 归为 auto
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
    r = client.get("/api/backtest?strategy=default&start=20260110&end=20260105")
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
