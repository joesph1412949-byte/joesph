# -*- coding: utf-8 -*-
"""prism_web app 测试 — 新 API(因子库/策略/回测/自动化) + 旧路由冒烟保留。

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
    monkeypatch.setattr(app_module.ds_obj, "_connected", False)
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
    """旧路由 /api/stock/<code>/manual 保留冒烟(临时文件避免污染)。"""
    from manual_store import ManualStore
    s = ManualStore(str(tmp_path / "m.json"))
    monkeypatch.setattr(app_module, "manual_store_obj", s)
    r = client.post("/api/stock/002859.SZ/manual", json={"S1": 1})
    assert r.status_code == 200
    assert r.get_json()["factors"]["S1"] == 1
    r2 = client.get("/api/stock/002859.SZ/manual")
    assert r2.get_json()["S1"] == 1
