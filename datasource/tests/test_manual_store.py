# -*- coding: utf-8 -*-
"""manual_store 单元测试"""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from manual_store import ManualStore


@pytest.fixture
def store(tmp_path):
    return ManualStore(str(tmp_path / "manual.json"))


def test_empty_manual(store):
    assert store.get_manual("000001.SZ") == {}
    assert store.all() == {}
    # 手填集合已空(2026-08: S1/S5/S7 被 K线因子取代) → 无可用手填因子
    assert store.MANUAL_FACTORS == []


def test_set_any_factor_rejected(store):
    # 手填集合为空: 写入任何因子都被拒(ValueError)
    with pytest.raises(ValueError):
        store.set_manual("000001.SZ", {"S1": 1})
    with pytest.raises(ValueError):
        store.set_manual("000001.SZ", {"S7": 0})


def test_set_persists_to_disk(store, tmp_path):
    # 空集合下无有效因子可写; 验证读取历史文件仍兼容
    store.set_manual("000001.SZ", {})   # 空写入合法(幂等)
    reloaded = ManualStore(str(tmp_path / "manual.json"))
    assert reloaded.get_manual("000001.SZ") == {}


def test_set_rejects_invalid_values(store):
    with pytest.raises(ValueError):
        store.set_manual("000001.SZ", {"S1": 2})   # 未知因子(空集合)即拒
    with pytest.raises(ValueError):
        store.set_manual("000001.SZ", {"S1": "x"})


def test_merge_combines_auto_and_manual(store):
    auto = {"F1": 1, "F3": 0, "S5": 0}
    # 空手填集合: merge 只保留自动因子
    merged = store.merge(auto, "000001.SZ")
    assert merged["F1"] == 1
    assert merged["F3"] == 0
    assert merged["S5"] == 0       # 无手填覆盖, 保持自动值


def test_merge_unknown_code(store):
    auto = {"F1": 1}
    merged = store.merge(auto, "999999.SZ")
    assert merged == {"F1": 1}     # 无手填则保持原样


def test_corrupt_json_is_preserved_and_recoverable(tmp_path, caplog):
    p = tmp_path / "manual.json"
    # 有效 UTF-8 文本但非法 JSON → json.loads 抛 JSONDecodeError（触发备份分支）
    garbage = b"{ this is not valid json {{ "
    p.write_bytes(garbage)
    s = ManualStore(str(p))
    assert s.all() == {}                    # 损坏时不抛异常, 返回空
    siblings = list(tmp_path.glob("manual.json.corrupt-*"))
    assert len(siblings) == 1               # 损坏文件被重命名为 .corrupt-* 保留
    assert siblings[0].read_bytes() == garbage  # 原始字节保留
    assert not p.exists()                   # 原路径已被移走
    s.set_manual("000001.SZ", {})           # 空写入生成全新有效文件
    reloaded = ManualStore(str(p))
    assert reloaded.get_manual("000001.SZ") == {}
    assert any("损坏" in rec.message for rec in caplog.records)


def test_manual_rejects_automated_factor():
    from manual_store import ManualStore
    store = ManualStore(path="__nonexistent__.json")
    try:
        with pytest.raises(ValueError):
            store.set_manual("000001", {"Y1": 1})   # Y1 已自动化, 不再手填
    finally:
        store._data = {}   # 清理内存态(不落盘)
