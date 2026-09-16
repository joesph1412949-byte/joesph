# -*- coding: utf-8 -*-
"""仪表盘接口测试: 只读接口形状 + 新聚合接口 + 写闸门确认。"""
import json

import pytest

from dashboard import app as dash


@pytest.fixture
def client():
    dash.app.config["TESTING"] = True
    return dash.app.test_client()


def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200


def test_config_is_masked(client):
    """脱敏: 不得回传 account_id 等敏感字段。"""
    r = client.get("/api/config")
    body = r.get_json()
    assert body["ok"] is True
    assert "account_id" not in json.dumps(body)


def test_rejections_empty_when_no_runtime(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "RUNTIME_PATH", tmp_path / "missing.json")
    r = client.get("/api/rejections")
    body = r.get_json()
    assert r.status_code == 200
    assert body["ok"] is True
    assert body["data"]["total"] == 0
    assert body["data"]["groups"] == []


def test_rejections_aggregates_by_code(client, monkeypatch, tmp_path):
    rt = tmp_path / "tt_runtime.json"
    rt.write_text(json.dumps({
        "rejected": [
            {"reject_code": "NO_BASE_POSITION", "reject_msg": "无底仓",
             "code": "600900.SH", "name": "长江电力", "side": "SELL",
             "price": 28.5, "volume": 300, "reason": "档位1"},
            {"reject_code": "NO_BASE_POSITION", "reject_msg": "无底仓",
             "code": "601088.SH", "name": "中国神华", "side": "SELL",
             "price": 47.2, "volume": 100, "reason": "档位1"},
            {"reject_code": "SIZE_ZERO", "reject_msg": "不足一手",
             "code": "600938.SH", "name": "中国海油", "side": "BUY",
             "price": 33.9, "volume": 0, "reason": "档位2"},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(dash, "RUNTIME_PATH", rt)
    body = client.get("/api/rejections").get_json()
    assert body["ok"] is True
    assert body["data"]["total"] == 3
    g = body["data"]["groups"]
    assert g[0]["code"] == "NO_BASE_POSITION" and g[0]["count"] == 2
    assert g[1]["code"] == "SIZE_ZERO" and g[1]["count"] == 1
    assert len(g[0]["samples"]) == 2


def test_ledger_history_empty(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "STATE_PATH", tmp_path / "tt_state.json")
    body = client.get("/api/ledger/history").get_json()
    assert body["ok"] is True
    assert body["data"]["rows"] == []


def test_ledger_history_returns_rows(client, monkeypatch, tmp_path):
    st = tmp_path / "tt_state.json"
    st.write_text("{}", encoding="utf-8")
    (tmp_path / "tt_history.jsonl").write_text(
        json.dumps({"date": "2026-09-14", "sold_total": 100,
                    "bought_total": 100, "trips": 1,
                    "realized_pnl": 33.5, "trades": 2, "symbols": {}}) + "\n"
        + json.dumps({"date": "2026-09-15", "sold_total": 0,
                      "bought_total": 0, "trips": 0,
                      "realized_pnl": 0.0, "trades": 0, "symbols": {}}) + "\n",
        encoding="utf-8")
    monkeypatch.setattr(dash, "STATE_PATH", st)
    body = client.get("/api/ledger/history?days=10").get_json()
    assert body["ok"] is True
    rows = body["data"]["rows"]
    assert [r["date"] for r in rows] == ["2026-09-14", "2026-09-15"]
    assert rows[0]["realized_pnl"] == 33.5


def test_pause_requires_confirm(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "SIGNAL_ROOT", tmp_path)
    r = client.post("/api/pause", json={})
    assert r.status_code == 400
    assert (tmp_path / "paused").exists() is False


def test_pause_with_confirm_writes_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(dash, "SIGNAL_ROOT", tmp_path)
    r = client.post("/api/pause", json={"confirm": True, "paused": True})
    assert r.status_code == 200
    assert (tmp_path / "paused").exists() is True
