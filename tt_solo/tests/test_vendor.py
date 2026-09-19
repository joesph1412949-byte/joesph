# -*- coding: utf-8 -*-
"""_vendor 底座测试: 路径根 + 原子写 + 涨跌停比例 + 分级护栏。"""
import os
import threading
from pathlib import Path

from ttcore import _vendor


def test_project_root_points_at_tt_solo():
    assert _vendor.PROJECT_ROOT.name == "tt_solo"


def test_state_dir_under_project_by_default():
    assert _vendor.STATE_DIR == _vendor.PROJECT_ROOT / "runtime" / "state"


def test_runtime_dir_env_override(monkeypatch, tmp_path):
    """TT_RUNTIME_DIR 覆盖后需重新加载模块才生效 —— 用 reload 验证。"""
    import importlib
    monkeypatch.setenv("TT_RUNTIME_DIR", str(tmp_path))
    mod = importlib.reload(_vendor)
    try:
        assert mod.RUNTIME_DIR == tmp_path
        assert mod.STATE_DIR == tmp_path / "state"
    finally:
        monkeypatch.delenv("TT_RUNTIME_DIR", raising=False)
        importlib.reload(_vendor)


def test_atomic_write_creates_parent_and_content(tmp_path):
    p = tmp_path / "a" / "b" / "x.json"
    _vendor.atomic_write(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"


def test_atomic_write_leaves_no_tmp(tmp_path):
    p = tmp_path / "x.json"
    _vendor.atomic_write(p, "v1")
    _vendor.atomic_write(p, "v2")
    assert p.read_text(encoding="utf-8") == "v2"
    # tmp 名是 `x.json.tmp.<pid>.<tid>`(唯一名), 旧字面量 `x.json.tmp` 的断言
    # 恒真 = 没守卫; 改成真守卫: 目录里除目标文件外没有任何残留
    assert [q.name for q in tmp_path.iterdir() if q.name != "x.json"] == []


def test_atomic_write_concurrent_same_path_keeps_own_tmp(tmp_path, monkeypatch):
    """M13(tt 侧): 同进程两个写者并发写**同一路径** → 零异常 + 落地必是完整内容。

    Event 握手把致命交织钉死(A 写完自己的 tmp 后卡在 replace 前, 让 B 走完整个
    流程, 再放 A):tmp 名若还是固定的 `path + ".tmp"`, B 的 `open(tmp,"w")` 会把
    A 刚写的 tmp 截断成 B 的内容 → A 的 replace 要么发布**别人的**内容, 要么
    FileNotFoundError(实测旧实现 200 轮 94 轮抛错)。tmp 名带 pid+线程 id 后两条
    各写各的 tmp, 都成功, 且不留残留。
    """
    p = tmp_path / "x.json"
    _vendor.atomic_write(p, "seed")

    a_at_replace = threading.Event()
    release_a = threading.Event()
    real_replace = _vendor.os.replace

    def watched_replace(src, dst):
        if threading.current_thread().name == "A":
            a_at_replace.set()
            assert release_a.wait(10), "编排失败: A 没被放行"
        return real_replace(src, dst)

    monkeypatch.setattr(_vendor.os, "replace", watched_replace)

    errs = []

    def writer(name, text):
        try:
            _vendor.atomic_write(p, text)
        except Exception as exc:          # tmp 被对方截断 -> FileNotFoundError/…
            errs.append((name, repr(exc)))

    ta = threading.Thread(target=writer, args=("A", "A" * 200), name="A")
    ta.start()
    assert a_at_replace.wait(10), "编排失败: A 没走到 replace 前"
    tb = threading.Thread(target=writer, args=("B", "B" * 200), name="B")
    tb.start()
    tb.join(10)
    release_a.set()
    ta.join(10)

    assert errs == [], "同路径并发写不许抛错(tmp 互踩): %r" % errs
    got = p.read_text(encoding="utf-8")
    assert got in ("A" * 200, "B" * 200), "落地必须是某一次**完整**写入"
    assert [q.name for q in tmp_path.iterdir() if q.name != "x.json"] == [], \
        "不留任何残留"


def test_limit_ratio_by_board():
    # 权威规格 = shared/exit_rules.limit_ratio; tt_solo 刻意自包含(不许 import
    # shared), 等式由 datasource/tests/test_common.py 的守卫逐码钉住。
    assert _vendor.limit_ratio_for_code("600900.SH") == 0.10
    assert _vendor.limit_ratio_for_code("300750.SZ") == 0.20
    assert _vendor.limit_ratio_for_code("688981.SH") == 0.20
    assert _vendor.limit_ratio_for_code("689009.SH") == 0.20   # 科创CDR
    assert _vendor.limit_ratio_for_code("830799.BJ") == 0.30
    assert _vendor.limit_ratio_for_code("920001.BJ") == 0.30   # 北交所 92 段
    assert _vendor.limit_ratio_for_code("400001.BJ") == 0.05   # 老三板
    assert _vendor.limit_ratio_for_code("420001.BJ") == 0.05


def test_is_local_request_tiers():
    assert _vendor.is_local_request(None, "127.0.0.1") is True
    assert _vendor.is_local_request(None, "::1") is True
    assert _vendor.is_local_request(None, "192.168.1.5") is True
    assert _vendor.is_local_request(None, "172.16.0.1") is True
    assert _vendor.is_local_request(None, "172.32.0.1") is False
    assert _vendor.is_local_request(None, "8.8.8.8") is False
    # 有 CF 头 = 经隧道 = 远程, 即使 remote_addr 是回环
    assert _vendor.is_local_request("1.2.3.4", "127.0.0.1") is False
