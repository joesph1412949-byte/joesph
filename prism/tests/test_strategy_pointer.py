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


def test_real_pointer_resolves_to_existing_strategy():
    """仓库真实指针(若存在)解析出的策略文件必须存在; 缺省回落 v04 亦然。
    (断言本意与具体 id 无关: 只保证指针所指不落空。)"""
    rid = engine.active_strategy_id()
    assert (engine.STRATEGIES_DIR / ("%s.json" % rid)).is_file()


def test_paper_hot_reload(tmp_path, monkeypatch):
    """指针变化 → PaperAccount.strategy 下次访问换策略(账本不变)。"""
    import shutil
    from prism.paper import PaperAccount
    # I-F1: 首次访问 .strategy 前注入 tmp 目录(指针/策略文件全隔离);
    # 需放真实 v03+v04(回落检查要求所指文件存在, 缺省首载也要能加载)
    for name in ("first_board_v03.json", "first_board_v04.json"):
        shutil.copy(Path(engine.__file__).parent / "strategies" / name,
                    tmp_path / name)
    monkeypatch.setattr(engine, "STRATEGIES_DIR", tmp_path)
    acc = PaperAccount(state_path=tmp_path / "paper.json")
    acc.init_account()
    assert acc.strategy["id"] == "first_board_v04"      # 缺省
    # 写指针指向 v03 → 热重载
    engine.set_active_strategy("first_board_v03", pointer_path=
                               tmp_path / engine.ACTIVE_FILENAME)
    assert acc.strategy["id"] == "first_board_v03"
    assert acc.state["cash"] == 1000000.0               # 账本不动


def test_paper_hot_reload_bad_file_keeps_old_and_logs(tmp_path, caplog,
                                                      monkeypatch):
    """坏策略文件热重载失败 → 保留旧策略并记 warning(spec §10)。"""
    import logging
    import shutil
    from prism.paper import PaperAccount
    # I-F1: 首次访问 .strategy 前注入 tmp 目录; v04 用真实文件(缺省回落可加载),
    # v03 复制后被坏 JSON 覆盖(热重载失败场景)
    for name in ("first_board_v03.json", "first_board_v04.json"):
        shutil.copy(Path(engine.__file__).parent / "strategies" / name,
                    tmp_path / name)
    (tmp_path / "first_board_v03.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(engine, "STRATEGIES_DIR", tmp_path)
    acc = PaperAccount(state_path=tmp_path / "paper.json")
    acc.init_account()
    assert acc.strategy["id"] == "first_board_v04"      # 首载 v04
    # 指针指向 v03, 但 tmp 下 v03.json 是坏 JSON → 热重载失败
    engine.set_active_strategy("first_board_v03", pointer_path=
                               tmp_path / engine.ACTIVE_FILENAME)
    with caplog.at_level(logging.WARNING, logger="prism.paper"):
        assert acc.strategy["id"] == "first_board_v04"   # 旧策略保留
    assert any("热重载失败" in r.getMessage() for r in caplog.records)
