# -*- coding: utf-8 -*-
"""tt_web 交易闸门护栏测试: 远程禁 pause/arm, 本机放行。全离线——
信号根目录用 TT_SIGNAL_ROOT 环境变量打到 tmp, 不碰真实 D:/QMT_SIGNALS。"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import tt_web.app as app_module


@pytest.fixture
def client(monkeypatch, tmp_path):
    # 闸门文件隔离: SIGNAL_ROOT 指向临时目录(必须在 import 后改实例常量,
    # 因为 api_pause/api_arm 直接闭包引用模块级 SIGNAL_ROOT)
    monkeypatch.setattr(app_module, "SIGNAL_ROOT", tmp_path)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_remote_blocked_pause_arm(client):
    """公网来源(remote=100.x, 无 CF 头) → 403, 闸门文件不被触碰。"""
    env = {"REMOTE_ADDR": "100.64.1.2"}
    for url in ("/api/pause", "/api/arm"):
        r = client.post(url, json={"confirm": "true"}, environ_base=env)
        assert r.status_code == 403
        assert "仅限本机" in r.get_json()["error"]


def test_local_pause_arm_ok(client, monkeypatch, tmp_path):
    """本机 → 放行到既有逻辑(闸门文件写进 tmp, 不碰真实信号根)。"""
    for url in ("/api/pause", "/api/arm"):
        r = client.post(url, json={"confirm": "true"},
                        environ_base={"REMOTE_ADDR": "127.0.0.1"})
        assert r.status_code != 403
    # 既有语义抽查: pause 的 confirm 校验仍在(护栏放行后走到路由本体)
    r = client.post("/api/pause", json={}, environ_base={"REMOTE_ADDR": "127.0.0.1"})
    assert r.status_code == 400          # 缺 confirm → 400 而非 403
