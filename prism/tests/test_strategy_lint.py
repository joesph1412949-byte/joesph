# -*- coding: utf-8 -*-
"""strategy_lint 测试 — 双向负控(全消费→0 / 假键→点名) + 判定方法各条。

全部自造临时树, **不依赖** prism/strategies/ 里现有策略的键清单
(W8 正在改 full_factor_v1.json, 依赖清单内容的测试会被别人的提交搞红)。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism import strategy_lint as sl


def _tree(tmp_path, strategy, producer="X = 1\n", name="s.json"):
    """造一棵最小树(仿仓库布局): prism/strategies/<name> + prism/producer.py。"""
    d = tmp_path / "prism" / "strategies"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(json.dumps(strategy, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "prism" / "producer.py").write_text(producer, encoding="utf-8")
    return p


def _run(tmp_path, strategy, producer="X = 1\n"):
    p = _tree(tmp_path, strategy, producer)
    return sl.lint_file(p, root=tmp_path)


# ---------------------------------------------------------------- 负控双向
def test_all_keys_consumed_reports_zero(tmp_path):
    """方向一: 所有键都有读取点 → 0 个未消费, 退出码 0。"""
    p = _tree(tmp_path, {"id": "s1", "composite": {"mode": "weighted_sum",
                                                  "cap": 9.0}},
              'def f(cfg):\n'
              '    return cfg.get("id"), cfg.get("mode"), cfg["cap"]\n')
    rep = sl.lint_file(p, root=tmp_path)
    assert rep["unconsumed"] == [], rep["unconsumed"]
    assert sorted(e["key"] for e in rep["consumed"]) == ["cap", "id", "mode"]
    assert sl.main(["--strategy", str(p), "--root", str(tmp_path)]) == 0


def test_fake_key_is_named_and_exit_nonzero(tmp_path):
    """方向二: 假键 zzz_never_read → 被点名, 退出码非 0。"""
    p = _tree(tmp_path, {"zzz_never_read": 1})
    rep = sl.lint_file(p, root=tmp_path)
    assert [e["key"] for e in rep["unconsumed"]] == ["zzz_never_read"]
    assert rep["unconsumed"][0]["paths"] == ["zzz_never_read"]
    assert rep["unconsumed"][0]["hits"] == []
    assert sl.main(["--strategy", str(p), "--root", str(tmp_path)]) == 1


def test_json_flag_is_parseable(tmp_path, capsys):
    p = _tree(tmp_path, {"zzz_never_read": 1})
    sl.main(["--strategy", str(p), "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert [e["key"] for e in out[0]["unconsumed"]] == ["zzz_never_read"]


# ---------------------------------------------------------------- 判定方法
def test_comment_docstring_and_bare_string_do_not_count(tmp_path):
    """散文/docstring/裸字符串里的同名词不是读取点(本仓血泪)。"""
    rep = _run(tmp_path, {"id": "s4", "cap": 9.0},
               'def f(cfg):\n'
               '    """cap 的语义见文档, 这里只是散文。"""\n'
               '    # cap 在注释里出现\n'
               '    DOC = "cap"\n'
               '    return cfg.get("id")\n')
    assert [e["key"] for e in rep["unconsumed"]] == ["cap"]
    assert [e["key"] for e in rep["consumed"]] == ["id"]


def test_weight_and_weights_are_distinct(tmp_path):
    """weight / weights 精确相等比较, 不做前缀包含。"""
    rep = _run(tmp_path, {"id": "s5", "weight": 0.6, "weights": [1.0]},
               'def f(cfg):\n'
               '    return cfg.get("id"), cfg.get("weights")\n')
    assert [e["key"] for e in rep["unconsumed"]] == ["weight"]
    assert sorted(e["key"] for e in rep["consumed"]) == ["id", "weights"]


def test_write_positions_and_dict_literals_are_not_reads(tmp_path):
    """x["cap"] = ... 与 {"cap": ...} 是写/构造, 不是消费。"""
    rep = _run(tmp_path, {"id": "s6", "cap": 9.0},
               'def f(cfg):\n'
               '    cfg["cap"] = 1\n'
               '    d = {"cap": 2}\n'
               '    return cfg.get("id"), d\n')
    assert [e["key"] for e in rep["unconsumed"]] == ["cap"]


def test_get_pop_setdefault_and_in_are_reads(tmp_path):
    for expr in ('cfg.get("cap")', 'cfg.pop("cap", None)',
                 'cfg.setdefault("cap", 1)', '"cap" in cfg'):
        rep = _run(tmp_path, {"cap": 9.0},
                   'def f(cfg):\n    return %s\n' % expr)
        assert rep["unconsumed"] == [], (expr, rep["unconsumed"])


def test_test_files_are_not_consumers(tmp_path):
    """*/tests/* 与 test_*.py 的命中不算消费。"""
    p = _tree(tmp_path, {"cap": 9.0})
    d = tmp_path / "prism" / "tests"
    d.mkdir()
    (d / "test_only.py").write_text('cfg.get("cap")\n', encoding="utf-8")
    rep = sl.lint_file(p, root=tmp_path)
    assert [e["key"] for e in rep["unconsumed"]] == ["cap"]


def test_evidence_carries_file_line_and_expression(tmp_path):
    """判定依据可复核: file:line + 读法原文。"""
    rep = _run(tmp_path, {"cap": 9.0},
               'def f(cfg):\n    return cfg.get("cap")\n')
    hit = rep["consumed"][0]["hits"][0]
    assert hit.startswith("prism/producer.py:2 ")
    assert "cap" in hit and "get(" in hit, hit


def test_active_pointer_is_not_a_strategy(tmp_path):
    """默认扫 strategies/*.json 时排除 .active.json 本地指针。"""
    p = _tree(tmp_path, {"id": "s10"})
    (p.parent / ".active.json").write_text('{"id": "s10"}', encoding="utf-8")
    assert [r["strategy"] for r in sl.lint_all(root=tmp_path)] == ["s10"]


def test_whitelist_mechanism_suppresses_a_key(tmp_path, monkeypatch):
    """白名单(键 → 理由)可显式豁免静态搜不到的动态消费者。"""
    monkeypatch.setitem(sl.DYNAMIC_CONSUMERS, "zzz_never_read",
                        "理由: 由网页按结构遍历; 证据: prism_web/app.py:753")
    rep = _run(tmp_path, {"zzz_never_read": 1})
    assert rep["unconsumed"] == []
    assert rep["consumed"][0]["whitelist"].startswith("理由:")


def test_container_keys_are_not_judged_on_their_own(tmp_path):
    """容器键(如 composite)不单独判定 —— 报的是它里面的叶子键。"""
    rep = _run(tmp_path, {"composite": {"zzz_never_read": 9.0}})
    assert [e["key"] for e in rep["unconsumed"]] == ["zzz_never_read"]
    assert rep["unconsumed"][0]["paths"] == ["composite.zzz_never_read"]


def test_declared_keys_walks_nested_lists(tmp_path):
    """路径含列表下标, 便于人工复核。"""
    keys = sl.declared_keys({"scoring_models": [{"weight": 0.6}, {"weight": 1}],
                             "composite": {"cap": 9.0}})
    assert ("scoring_models[0].weight", "weight") in keys
    assert ("composite.cap", "cap") in keys
    assert ("composite", "composite") not in keys
