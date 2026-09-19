# -*- coding: utf-8 -*-
"""data_refresh 调度层的判别力测试。

只测"调度 + 诚实汇报"这一层: 真正执行子进程的 `_run_cmd` 与只读磁盘的
`_load_cache` 被整体替换 —— 不联网、不跑真构建、不碰任何真实缓存。

判别力所在(必须存在): **第 3 段失败 → 整体非零退出且汇总点名该段**。
market_data 的 --build-sectors/--build-global/--build-sector-map/--build-futures
是"打印完裸 return"(永远 exit 0), 只靠退出码无法发现它们没刷新成功; 所以本入口
还要读盘报日期, 并把"空数据"单独分类, 绝不与成功混为一谈。
"""
import pytest

from prism import data_refresh as dr

# 一份"每段都有数据"的缓存快照(形状与 market_data 缓存一致)
CACHE_FULL = {
    "sectors": {"801010": {"name": "农林牧渔"}},
    "sector_map": {"000001": {"sector": "801780", "name": "平安银行"}},
    "kline": {"801010": {"dates": ["2026-09-11", "2026-09-18"], "close": [1, 2]}},
    "flow": {"801010": {"dates": ["2026-09-04"], "main_net_in": [1]}},
    "global": {"NDX": {"name": "纳斯达克", "dates": ["2026-09-11"]},
               "US10Y": {"name": "10年美债收益率", "dates": ["2026-09-10"]}},
    "flow_rank": {"dates": ["2026-09-18"], "rows": {"2026-09-18": [1]}},
    "benchmark": {"dates": ["2026-09-18"], "close": [1.0]},
    "futures": {"MA0": {"name": "甲醇", "dates": ["2026-09-18"]}},
}


class _CP:
    """CompletedProcess 的最小替身。"""

    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _nums(out):
    """退出码行 → int。"""
    line = [x for x in out.splitlines() if x.startswith("退出码:")][0]
    return int(line.split(":")[1].split()[0])


def _line(out, prefix):
    """汇总里的 "  成功: ..." 这一行。"""
    return [x for x in out.splitlines() if x.startswith(prefix)][0]


def _install(monkeypatch, fail_name=None, empty_name=None):
    """装假执行层。

    除点名段外都返回 rc=0(即使点名段失败, 后面的段照跑 —— 与 market_data
    自己的"一段被封不连累另一段"同口径)。empty_name 段复刻真实假成功路径:
    **执行 rc=0 但缓存里那段确实为空**。
    """
    calls = []

    def fake_run(cmd):
        calls.append(list(cmd))
        name = dr.SEGMENTS[len(calls) - 1]["name"]
        if name == fail_name:
            return _CP(1, "", "market_data: %s 采集失败(东财可能封禁)" % name)
        return _CP(0, "[%s] 采集完成" % name)

    def fake_cache():
        snap = dict(CACHE_FULL)
        if empty_name:
            idx = [s["name"] for s in dr.SEGMENTS].index(empty_name)
            if len(calls) > idx:               # 该段跑完之后, 它的段是空的
                for k in dr.SEGMENTS[idx]["keys"]:
                    snap.pop(k, None)
        return snap

    monkeypatch.setattr(dr, "_run_cmd", fake_run)
    monkeypatch.setattr(dr, "_load_cache", fake_cache)
    return calls


def test_all_success_exits_zero(monkeypatch, capsys):
    """全成功 → 退出 0, 且每段都真的跑了。"""
    calls = _install(monkeypatch)
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 0
    assert len(calls) == len(dr.SEGMENTS)
    assert "失败: (无)" in out
    assert "空数据: (无)" in out
    assert all(s["name"] in _line(out, "  成功:") for s in dr.SEGMENTS)


def test_third_segment_failure_names_it_and_exits_nonzero(monkeypatch, capsys):
    """核心判别力: 第 3 段失败 → 非零退出 + 汇总点名该段 + 后段不被连累。"""
    seg3 = dr.SEGMENTS[2]["name"]
    calls = _install(monkeypatch, fail_name=seg3)
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 1, "任一段失败必须非零退出"
    assert _nums(out) == 1
    assert seg3 in _line(out, "  失败:")
    assert seg3 not in _line(out, "  成功:")
    assert len(calls) == len(dr.SEGMENTS), "一段失败不该吃掉后续段"
    assert seg3 in out, "失败段必须出现在逐段输出里"


def test_empty_data_is_a_separate_class_not_success(monkeypatch, capsys):
    """空数据(执行 rc=0 但无数据)≠ 成功: 单独分类, 且整体非零。"""
    seg = dr.SEGMENTS[4]["name"]
    _install(monkeypatch, empty_name=seg)
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 2, "空数据必须非零退出(不得被当成功)"
    assert _nums(out) == 2
    assert seg in _line(out, "  空数据:")
    assert seg not in _line(out, "  成功:")
    assert "失败: (无)" in out, "空数据不该混进失败类别"


def test_failure_dominates_empty_in_exit_code(monkeypatch, capsys):
    """同时有失败与空数据 → 退出码取 1(失败优先), 两类各自点名。"""
    seg3, seg5 = dr.SEGMENTS[2]["name"], dr.SEGMENTS[4]["name"]
    _install(monkeypatch, fail_name=seg3, empty_name=seg5)
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert seg3 in _line(out, "  失败:") and seg5 in _line(out, "  空数据:")


def test_dry_run_executes_nothing(monkeypatch, capsys):
    """--dry-run 只打印计划: 一条命令都不执行, 退出 0。"""

    def boom(cmd):
        raise AssertionError("dry-run 不该执行任何命令: %r" % (cmd,))

    monkeypatch.setattr(dr, "_run_cmd", boom)
    monkeypatch.setattr(dr, "_load_cache", lambda: CACHE_FULL)
    code = dr.main(["--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    planned = [x for x in out.splitlines() if "prism.market_data" in x]
    assert len(planned) == len(dr.SEGMENTS), "每段都要打印将要执行的命令"
    assert "--build-benchmark" in out
    assert "dry-run" in out


def test_segments_table_shape_is_sane():
    """段表本身的自检: 名字唯一、都带 --build-* 参数、都声明要读的缓存段。"""
    names = [s["name"] for s in dr.SEGMENTS]
    assert len(names) == len(set(names))
    for s in dr.SEGMENTS:
        assert s["args"] and s["args"][0].startswith("--build-")
        assert s["keys"] and s["chain"]
