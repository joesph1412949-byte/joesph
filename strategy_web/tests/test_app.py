# -*- coding: utf-8 -*-
"""app 单元测试 — Flask test_client, 用假 ScreenRunner"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# 在 import app 前注入假模块（避免 app 里真实 import xtquant）
import app as app_module


class FakeScreen:
    def __init__(self, *a, **k):
        pass

    def run(self):
        return {"market": {"node_score": 4, "stage": "回暖期",
                           "factors": {}, "total_amount": 2.5e12,
                           "limit_up_count": 50},
                "environment_ok": True,
                "candidates": [{"code": "002859.SZ", "name": "洁美科技",
                                "scores": {"grade": "A", "composite": 5.0,
                                           "strength": "强", "position": "50%"},
                                "factors": {"F1": 1}}],
                "summary": {"candidate_count": 1, "a_count": 1}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "ScreenRunner", FakeScreen)
    monkeypatch.setattr(app_module.ds_obj, "_connected", True)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_screen_returns_candidates(client):
    r = client.post("/api/screen")
    assert r.status_code == 200
    data = r.get_json()
    assert data["environment_ok"] is True
    assert len(data["candidates"]) == 1


def test_manual_set_and_get(client, tmp_path, monkeypatch):
    # 用临时文件避免污染真实 manual_factors.json
    from manual_store import ManualStore
    s = ManualStore(str(tmp_path / "m.json"))
    monkeypatch.setattr(app_module, "manual_store_obj", s)
    r = client.post("/api/stock/002859.SZ/manual", json={"S1": 1})
    assert r.status_code == 200
    assert r.get_json()["factors"]["S1"] == 1
    r2 = client.get("/api/stock/002859.SZ/manual")
    assert r2.get_json()["S1"] == 1


def test_kline_endpoint(client, monkeypatch):
    import pandas as pd
    import numpy as np
    import datetime as dt
    n = 120
    ts = (pd.date_range("2026-01-01", periods=n, freq="B").astype("int64") // 10**6).tolist()
    class FakeDS:
        def get_kline(self, code, days=120):
            return pd.DataFrame({"time": ts,
                                 "open": np.linspace(9.5, 19.5, n),
                                 "high": np.linspace(10.5, 20.5, n),
                                 "low": np.linspace(9.0, 19.0, n),
                                 "close": np.linspace(10, 20, n),
                                 "volume": np.full(n, 100000)})
        def get_instrument(self, code):
            return {"UpStopPrice": 22.0}
    monkeypatch.setattr(app_module, "ds_obj", FakeDS())
    r = client.get("/api/stock/002859.SZ/kline")
    assert r.status_code == 200
    data = r.get_json()
    # 期望日期用与 _fmt_date 相同的 int64-ms 换算(fromtimestamp)推导 → 任何时区都一致,
    # 但仍能拦截 _fmt_date 的 int 分支被删的回归(日期会变回毫秒整数)。
    expected0 = dt.datetime.fromtimestamp(ts[0] / 1000.0).strftime("%Y-%m-%d")
    expected1 = dt.datetime.fromtimestamp(ts[1] / 1000.0).strftime("%Y-%m-%d")
    assert len(data["dates"]) == 120
    assert data["dates"][0] == expected0
    assert data["dates"][1] == expected1
    assert len(data["closes"]) == 120
    # OHLC 蜡烛图所需字段 (spec §4③)
    for key in ("opens", "highs", "lows"):
        assert key in data
        assert len(data[key]) == 120
        assert isinstance(data[key][0], float)
    assert "ma60" in data
    assert len(data["volumes"]) == 120
    assert data["up_stop"] == 22.0


def test_screen_disconnected_returns_400(client, monkeypatch):
    monkeypatch.setattr(app_module.ds_obj, "_connected", False)
    r = client.post("/api/screen")
    assert r.status_code == 400
    assert "QMT未连接" in r.get_json()["error"]
