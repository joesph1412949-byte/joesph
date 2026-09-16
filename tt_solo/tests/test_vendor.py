# -*- coding: utf-8 -*-
"""_vendor 底座测试: 路径根 + 原子写 + 涨跌停比例 + 分级护栏。"""
import os
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
    assert not (tmp_path / "x.json.tmp").exists()


def test_limit_ratio_by_board():
    assert _vendor.limit_ratio_for_code("600900.SH") == 0.10
    assert _vendor.limit_ratio_for_code("300750.SZ") == 0.20
    assert _vendor.limit_ratio_for_code("688981.SH") == 0.20
    assert _vendor.limit_ratio_for_code("830799.BJ") == 0.30
    # 北交所新代码段 920xxx —— shared/common.py 缺这条, 这里必须有
    assert _vendor.limit_ratio_for_code("920001.BJ") == 0.30


def test_is_local_request_tiers():
    assert _vendor.is_local_request(None, "127.0.0.1") is True
    assert _vendor.is_local_request(None, "::1") is True
    assert _vendor.is_local_request(None, "192.168.1.5") is True
    assert _vendor.is_local_request(None, "172.16.0.1") is True
    assert _vendor.is_local_request(None, "172.32.0.1") is False
    assert _vendor.is_local_request(None, "8.8.8.8") is False
    # 有 CF 头 = 经隧道 = 远程, 即使 remote_addr 是回环
    assert _vendor.is_local_request("1.2.3.4", "127.0.0.1") is False
