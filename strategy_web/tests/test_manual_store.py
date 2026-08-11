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


def test_set_and_get(store):
    store.set_manual("000001.SZ", {"F7": 1, "Y5": 0})
    assert store.get_manual("000001.SZ") == {"F7": 1, "Y5": 0}


def test_set_persists_to_disk(store, tmp_path):
    store.set_manual("000001.SZ", {"F7": 1})
    reloaded = ManualStore(str(tmp_path / "manual.json"))
    assert reloaded.get_manual("000001.SZ") == {"F7": 1}


def test_set_rejects_invalid_values(store):
    with pytest.raises(ValueError):
        store.set_manual("000001.SZ", {"F7": 2})   # 非 0/1
    with pytest.raises(ValueError):
        store.set_manual("000001.SZ", {"F7": "x"})


def test_merge_combines_auto_and_manual(store):
    auto = {"F1": 1, "F3": 0, "Y1": 0}
    store.set_manual("000001.SZ", {"F7": 1, "Y1": 1})  # 手填覆盖 Y1
    merged = store.merge(auto, "000001.SZ")
    assert merged["F1"] == 1       # 自动
    assert merged["F3"] == 0       # 自动
    assert merged["F7"] == 1       # 手填
    assert merged["Y1"] == 1       # 手填覆盖自动


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
    s.set_manual("000001.SZ", {"F7": 1})    # 后续写入生成全新有效文件
    reloaded = ManualStore(str(p))
    assert reloaded.get_manual("000001.SZ") == {"F7": 1}
    assert any("损坏" in rec.message for rec in caplog.records)
