# -*- coding: utf-8 -*-
"""PaperAccount 账本核心测试 — 全离线, 账本一律注入 tmp_path。"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.paper import PaperAccount


def _acc(tmp_path, **kw):
    return PaperAccount(state_path=tmp_path / "paper.json", **kw)


def test_init_creates_ledger(tmp_path):
    acc = _acc(tmp_path)
    st = acc.init_account(created="2026-09-01")
    assert st["version"] == 1
    assert st["cash"] == 1000000.0
    assert st["initial_capital"] == 1000000.0
    assert st["holdings"] == [] and st["trades"] == []
    assert st["nav_history"] == []
    assert st["screens_done"] == [] and st["settled_dates"] == []
    assert st["live_nav"] == 1000000.0
    # 幂等: 二次 init 不覆盖
    st2 = acc.init_account(created="2026-09-02")
    assert st2["created"] == "2026-09-01"


def test_load_missing_returns_false(tmp_path):
    acc = _acc(tmp_path)
    assert acc.load() is False
    assert not (tmp_path / "paper.json").exists()   # 不误建文件


def test_load_corrupt_keeps_file(tmp_path):
    p = tmp_path / "paper.json"
    p.write_text("{broken json!!", encoding="utf-8")
    acc = _acc(tmp_path)
    assert acc.load() is False          # 拒绝加载
    assert p.read_text(encoding="utf-8") == "{broken json!!"  # 原文件保留
    # 结构校验: 缺关键键也拒绝
    p.write_text(json.dumps({"version": 1, "cash": 1.0}), encoding="utf-8")
    assert acc.load() is False


def test_save_atomic_and_roundtrip(tmp_path):
    acc = _acc(tmp_path)
    acc.init_account()
    acc.state["cash"] = 900000.0
    acc.save()
    acc2 = _acc(tmp_path)
    assert acc2.load() is True
    assert acc2.state["cash"] == 900000.0
    assert not (tmp_path / "paper.json.tmp").exists()   # 无残留临时文件


def test_summary_and_detail(tmp_path):
    acc = _acc(tmp_path)
    assert acc.summary() == {"exists": False}      # 未初始化
    acc.init_account(created="2026-09-01")
    acc.state["live_nav"] = 0.0            # 清零账户: 总收益应为 -100%
    s0 = acc.summary()
    assert s0["nav"] == 0.0 and s0["total_return_pct"] == -100.0
    acc.state["live_nav"] = 1000000.0      # 恢复正常后再断言原有行为
    s = acc.summary()
    assert s["exists"] is True
    assert s["cash"] == 1000000.0 and s["nav"] == 1000000.0
    assert s["total_return_pct"] == 0.0
    assert s["holdings_count"] == 0
    d = acc.detail()
    assert d["holdings"] == [] and d["trades"] == [] and d["nav_history"] == []
