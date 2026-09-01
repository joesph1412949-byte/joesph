# -*- coding: utf-8 -*-
"""默认策略指针测试 — tmp 注入, 全离线。"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism import engine


def test_active_default_when_no_pointer(tmp_path):
    p = tmp_path / "active.json"
    assert engine.active_strategy_id(pointer_path=p) == "first_board_v04"
    assert not p.exists()          # 读不写


def test_active_reads_pointer(tmp_path):
    p = tmp_path / "active.json"
    p.write_text(json.dumps({"id": "first_board_v03"}), encoding="utf-8")
    assert engine.active_strategy_id(pointer_path=p) == "first_board_v03"


def test_active_falls_back_on_corrupt(tmp_path):
    p = tmp_path / "active.json"
    p.write_text("{broken", encoding="utf-8")
    assert engine.active_strategy_id(pointer_path=p) == "first_board_v04"
    assert p.read_text(encoding="utf-8") == "{broken"   # 不覆盖


def test_active_falls_back_when_strategy_missing(tmp_path):
    p = tmp_path / "active.json"
    p.write_text(json.dumps({"id": "no_such_strategy"}), encoding="utf-8")
    assert engine.active_strategy_id(pointer_path=p) == "first_board_v04"


def test_set_active_roundtrip(tmp_path):
    p = tmp_path / "active.json"
    engine.set_active_strategy("first_board_v03", pointer_path=p)
    assert json.loads(p.read_text(encoding="utf-8"))["id"] == "first_board_v03"
    assert engine.active_strategy_id(pointer_path=p) == "first_board_v03"
    assert not (tmp_path / "active.json.tmp").exists()   # 原子写无残留


def test_real_pointer_is_v04():
    """仓库真实指针(若存在)不得指向不存在的策略; 缺省回落 v04。"""
    rid = engine.active_strategy_id()
    assert (engine.STRATEGIES_DIR / ("%s.json" % rid)).is_file()
