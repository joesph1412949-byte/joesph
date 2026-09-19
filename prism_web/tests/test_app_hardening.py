# -*- coding: utf-8 -*-
"""2026-09-19 启动链/护栏加固批次 — 全部为离线判据守卫。

红线(本文件自我约束):
- 不碰 5000 上正在跑的生产实例(只 import app 模块, 不 app.run)。
- 不写 `prism/strategies/.active.json`(paper_daemon 每轮热读的默认策略指针):
  所有 activate 用例都把路由体依赖(`_strategy_path` / `engine.set_active_strategy`)
  打桩, 只断言"路由体有没有被执行"。
- 不走真实选股(/api/screen 会真打 QMT 全市场 1~2 分钟)。

覆盖:
- C1 端口归属自检 `_port_owned_by_other` / `_port_guard_or_exit`
  (含 reloader 子进程放行 + TIME_WAIT/裸绑定不得误判)
- A1 跨站否决: 本机档写端点拒绝 `Sec-Fetch-Site` 非 same-origin/none 的浏览器请求
- A2 debug 默认值翻转(显式 APP_DEBUG=1 才开)
- A3 `/api/strategies/create` 并发不静默丢写
- A4 写/读端点异常 → JSON(不吐 text/html), 文案不带 %r
"""
import datetime
import socket
import threading
import time

import pytest

import prism_web.app as app_module


# ---------------------------------------------------------------- 工具

def _listener():
    """起一个临时监听 socket(临时端口, 绝不用 5000)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(5)
    return s, s.getsockname()[1]


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def _ctx(method, path, cf_ip, remote="203.0.113.7", sec_fetch_site=None):
    """护栏单元测试用请求上下文(真实路径 → 真实 endpoint)。"""
    headers = {} if cf_ip is None else {"CF-Connecting-IP": cf_ip}
    if sec_fetch_site is not None:
        headers["Sec-Fetch-Site"] = sec_fetch_site
    return app_module.app.test_request_context(
        path, method=method,
        environ_base={"REMOTE_ADDR": remote or ""}, headers=headers)


# ================= C1: 端口归属自检 =================

def test_port_owned_true_when_listening():
    """有监听者 → True(这是唯一会 fail-closed 退出的判据)。"""
    s, port = _listener()
    try:
        assert app_module._port_owned_by_other("127.0.0.1", port) is True
    finally:
        s.close()


def test_port_owned_false_when_free():
    assert app_module._port_owned_by_other("127.0.0.1", _free_port()) is False


def test_port_owned_false_when_bound_but_not_listening():
    """TIME_WAIT 形状(裸 bind 失败但没有监听者)不得误判为"有人占用"。

    实测(本机 2026-09-19, 临时端口): 服务端先关进入 TIME_WAIT 后, 裸 bind
    反而会成功 —— 但**不能**依赖这一点: 该分支必须靠 connect 复核兜住。
    这里用"已绑定未 listen"稳定构造同一分支: 裸 bind 失败 + connect 失败。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))          # 绑定但不 listen
    port = s.getsockname()[1]
    try:
        probe = socket.socket()
        try:
            with pytest.raises(OSError):        # 前置条件: 裸 bind 确实会失败
                probe.bind(("127.0.0.1", port))
        finally:
            probe.close()
        assert app_module._port_owned_by_other("127.0.0.1", port) is False
    finally:
        s.close()


def test_port_owned_false_after_server_close_time_wait():
    """真实 TIME_WAIT: 服务端先关(主动关闭方) → 端口占位不算"有监听者"。"""
    s, port = _listener()
    cli = socket.create_connection(("127.0.0.1", port), timeout=2.0)
    conn, _ = s.accept()
    conn.close()                      # 服务端侧先发 FIN → 该连接进 TIME_WAIT
    cli.close()
    s.close()
    assert app_module._port_owned_by_other("127.0.0.1", port) is False


def test_port_guard_exits_when_owned(monkeypatch, capsys):
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    monkeypatch.setattr(app_module, "_port_owned_by_other",
                        lambda *a, **k: True)
    with pytest.raises(SystemExit) as ei:
        app_module._port_guard_or_exit("127.0.0.1", 5000)
    assert ei.value.code == 1
    assert "端口 5000 已被占用(检测有效)" in capsys.readouterr().out


def test_port_guard_skips_reloader_child(monkeypatch, capsys):
    """debug 子进程(WERKZEUG_RUN_MAIN 非空)必须放行。

    werkzeug 3.1.8: run_simple 里**父进程**建 server 并占住端口(把 fd 经
    WERKZEUG_SERVER_FD 交给子进程)。子进程若也自检, 会被自己的父进程挡住 →
    debug 模式正常启动直接失败。
    """
    monkeypatch.setenv("WERKZEUG_RUN_MAIN", "true")
    monkeypatch.setattr(app_module, "_port_owned_by_other", lambda *a, **k: True)
    assert app_module._port_guard_or_exit("127.0.0.1", 5000) is None
    assert "已被占用" not in capsys.readouterr().out


def test_port_guard_passes_when_free(monkeypatch):
    monkeypatch.delenv("WERKZEUG_RUN_MAIN", raising=False)
    monkeypatch.setattr(app_module, "_port_owned_by_other", lambda *a, **k: False)
    assert app_module._port_guard_or_exit("127.0.0.1", 5000) is None


# ================= A2: debug 默认值 =================

def test_debug_default_off(monkeypatch):
    """默认关(fail-closed); 只有显式 APP_DEBUG=1 才开。"""
    monkeypatch.delenv("APP_DEBUG", raising=False)
    assert app_module._debug_enabled() is False
    monkeypatch.setenv("APP_DEBUG", "0")
    assert app_module._debug_enabled() is False
    monkeypatch.setenv("APP_DEBUG", "true")
    assert app_module._debug_enabled() is False      # 旧语义会开; 新语义只认 "1"
    monkeypatch.setenv("APP_DEBUG", "1")
    assert app_module._debug_enabled() is True


# ================= A1: 跨站否决 =================

def test_guard_unit_blocks_cross_site_local_write():
    """本机档写端点: Sec-Fetch-Site 非 same-origin/none → 403(钩子拦, 不走路由)。

    跨站简单表单 POST 的 remote_addr 也是 127.0.0.1 → is_local_request 判"本机"
    → 旧代码放行。Sec-Fetch-Site 由浏览器强制写入, 页面无法伪造。
    """
    for site in ("cross-site", "same-site"):
        for path in ("/api/strategies/full_factor_v1/activate", "/api/automation"):
            with _ctx("POST", path, None, "127.0.0.1", site):
                assert app_module._local_only_guard() is not None, (path, site)


def test_guard_unit_allows_same_origin_and_nonbrowser():
    """同源前端(same-origin)与非浏览器(curl/守护/测试, 无该头)不得被误伤。"""
    for site in (None, "same-origin", "none"):
        for path in ("/api/strategies/full_factor_v1/activate", "/api/automation"):
            with _ctx("POST", path, None, "127.0.0.1", site):
                assert app_module._local_only_guard() is None, (path, site)


def test_guard_unit_cross_site_still_local_only():
    """跨站否决只加在本机档: 远程可写的路由不因该头改变行为。"""
    with _ctx("POST", "/api/screen", None, "127.0.0.1", "cross-site"):
        assert app_module._local_only_guard() is None


def test_cross_site_activate_denied_and_body_not_run(client, tmp_path,
                                                     monkeypatch):
    """RED 复现(审计手法): 一条跨站表单就能改掉 paper_daemon 热读的默认策略。

    只打桩路由体依赖, 绝不真写 .active.json。
    """
    import prism.engine as engine
    called = []
    monkeypatch.setattr(app_module, "_strategy_path",
                        lambda sid: tmp_path / "x.json")
    monkeypatch.setattr(engine, "set_active_strategy",
                        lambda sid: called.append(sid))
    r = client.post("/api/strategies/first_board_v04/activate", json={},
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert called == []                      # 路由体没被执行
    assert r.get_json()["ok"] is False


def test_cross_site_perf_backfill_denied_and_body_not_run(client, monkeypatch):
    """同一钩子的第二个端点(审计证据里的无 body 表单 POST)。"""
    touched = []
    monkeypatch.setattr(app_module, "_ensure_qmt", lambda: True)
    monkeypatch.setattr(app_module, "perf_store_obj", type("P", (), {
        "backfill": lambda self, days: touched.append(days) or 0,
        "summary": lambda self: {}})())
    r = client.post("/api/perf/backfill?days=5",
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert touched == []


def test_same_origin_activate_still_works(client, tmp_path, monkeypatch):
    """同源前端(生产 app.js 用)必须照旧可用。"""
    import prism.engine as engine
    called = []
    monkeypatch.setattr(app_module, "_strategy_path",
                        lambda sid: tmp_path / "x.json")
    monkeypatch.setattr(engine, "set_active_strategy",
                        lambda sid: called.append(sid))
    r = client.post("/api/strategies/first_board_v04/activate", json={},
                    headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200
    assert called == ["first_board_v04"]


# ================= A3: create 并发 =================

_EDITOR_PAYLOAD = {
    "name": "并发测试",
    "models": [{"id": "first_board", "name": "首板", "weight": 1.0,
                "factors": ["F1", "F8"], "weights": [1, 1]}],
    "gate_factors": ["N1"], "gate_threshold": 1,
    "candidate_min_model": 3,
    "sell": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
             "max_hold_days": 5},
}


def test_strategy_create_concurrent_no_lost_write(tmp_path, monkeypatch):
    """两个并发 create 不得撞同一个 id(撞号 = 后写覆盖先写, 双方都报 ok)。

    固定时钟 + 拖慢 atomic_write, 稳定逼出"分配 id 与落盘之间"的交错。
    """
    real_atomic_write = app_module.atomic_write

    class _FixedDT:
        class datetime:
            @staticmethod
            def now():
                return datetime.datetime(2026, 9, 19, 14, 21, 32)

    def _slow_atomic_write(p, s):
        time.sleep(0.4)
        real_atomic_write(p, s)

    monkeypatch.setattr(app_module, "STRATEGIES_DIR", tmp_path)
    monkeypatch.setattr(app_module, "_dt", _FixedDT)
    monkeypatch.setattr(app_module, "atomic_write", _slow_atomic_write)

    ids, errors, start = [], [], threading.Barrier(2)

    def _post():
        c = app_module.app.test_client()
        start.wait(timeout=5)
        r = c.post("/api/strategies/create", json=_EDITOR_PAYLOAD)
        body = r.get_json()
        if r.status_code == 200 and body.get("ok"):
            ids.append(body["id"])
        else:
            errors.append((r.status_code, body))

    ts = [threading.Thread(target=_post) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)

    assert errors == [], errors
    assert len(ids) == 2, ids
    assert len(set(ids)) == 2, ids                       # 不撞号
    files = sorted(p.name for p in tmp_path.glob("custom_*.json"))
    assert len(files) == 2, files                        # 不丢写


# ================= A4: 异常 → JSON =================

def test_corrupt_strategy_json_returns_json_500(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "STRATEGIES_DIR", tmp_path)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    r = client.get("/api/strategy/broken")
    assert r.status_code == 500
    assert r.is_json and r.get_json()["ok"] is False


def test_perf_backfill_store_failure_returns_json_500(client, monkeypatch):
    monkeypatch.setattr(app_module, "_ensure_qmt", lambda: True)

    class _Boom:
        def backfill(self, days=5):
            raise OSError(13, "Permission denied",
                          r"D:\cc-joesph\runtime\state\perf.json")

        def summary(self):
            return {}

    monkeypatch.setattr(app_module, "perf_store_obj", _Boom())
    r = client.post("/api/perf/backfill?days=5")
    assert r.status_code == 500
    assert r.is_json and r.get_json()["ok"] is False
    assert "cc-joesph" not in r.get_data(as_text=True)   # 不泄露内部路径


def test_automation_bad_body_returns_json_400(client):
    """坏 JSON / 表单体: 旧代码 → 415/400 text/html(前端 res.json() 会抛)。"""
    r = client.post("/api/automation", data="not json",
                    content_type="application/json")
    assert r.status_code == 400
    assert r.is_json and r.get_json()["ok"] is False
    r2 = client.post("/api/automation", data={"paused": "true"})
    assert r2.status_code == 400
    assert r2.is_json and r2.get_json()["ok"] is False


def test_automation_json_body_still_works(client, tmp_path, monkeypatch):
    """不许把正常 JSON 路径一起打死。"""
    from prism import trader
    monkeypatch.setattr(trader, "PAUSE_FILE", str(tmp_path / "pause"))
    r = client.post("/api/automation", json={"paused": True})
    assert r.status_code == 200 and r.get_json()["paused"] is True
    r2 = client.post("/api/automation", json={"paused": False})
    assert r2.status_code == 200 and r2.get_json()["paused"] is False
