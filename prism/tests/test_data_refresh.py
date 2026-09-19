# -*- coding: utf-8 -*-
"""data_refresh 调度层的判别力测试。

只测"调度 + 诚实汇报"这一层: 真正执行子进程的 `_run_cmd` 与只读磁盘的
`_load_cache` 被整体替换 —— 不联网、不跑真构建、不碰任何真实缓存。

判别力所在(必须存在): **第 3 段失败 → 整体非零退出且汇总点名该段**。
09-19 A 批次起 market_data 的 6 个 --build-* 有了统一退出码(0/1/3), 本入口
改用它, 但**不放弃读盘核实**: 退出码与"缓存是否推进"矛盾时两条证据都报出来,
空数据/未推进仍单独分类, 绝不与成功混为一谈。
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


def _seg_of(cmd):
    """命令 → 段表项(尾部参数与 SEGMENTS 的 args(+降级后缀)精确匹配)。

    **不能用"第几次调用"推断段**: sectors 有了降级重试后, 调用序号与段表不再
    一一对应(sectors 失败会多出一次 --source sw 的调用)。
    """
    tail = list(cmd[3:])
    for s in dr.SEGMENTS:
        if tail in (list(s["args"]),
                    list(s["args"]) + list(s.get("fallback") or ())):
            return s
    raise AssertionError("未知命令: %r" % (cmd,))


def _is_fallback(cmd):
    """这条命令是不是降级重跑(尾部参数 = 段 args + 段 fallback)。"""
    s = _seg_of(cmd)
    return bool(s.get("fallback")) and \
        list(cmd[3:]) == list(s["args"]) + list(s["fallback"])


def _install(monkeypatch, fail_name=None, empty_name=None, rc_map=None,
             grown_keys=(), sw_rc=None):
    """装假执行层。

    除点名段外都返回 rc=0(即使点名段失败, 后面的段照跑 —— 与 market_data
    自己的"一段被封不连累另一段"同口径)。empty_name 段复刻真实假成功路径:
    **执行 rc=0 但缓存里那段确实为空**; grown_keys 里的缓存键在"跑完后"变长
    (复现"退出码说失败/未推进, 缓存却推进了"的矛盾场景)。
    sw_rc: **仅**覆盖降级重跑(--source sw)那次的退出码; None ⇒ 降级重跑沿用
    与主源相同的规则(fail_name/rc_map 照样命中) —— 这样"两次都失败"用例不必
    额外声明, 而"降级成功"用例显式给 0。
    """
    calls = []
    scans = []          # _load_cache 被调次数: 每段 = 跑前(before) + 跑后(after)

    def _rc_for(cmd):
        name = _seg_of(cmd)["name"]
        if _is_fallback(cmd) and sw_rc is not None:
            return sw_rc, "[%s/sw] 采集完成" % name, ""
        if rc_map and name in rc_map:
            return rc_map[name], "", "market_data: %s" % name
        if name == fail_name:
            return 1, "", "market_data: %s 采集失败(东财可能封禁)" % name
        return 0, "[%s] 采集完成" % name, ""

    def fake_run(cmd):
        calls.append(list(cmd))
        rc, out, err = _rc_for(cmd)
        return _CP(rc, out, err)

    def fake_cache():
        # 第 1 次读 = 第 1 段的 before, 第 2 次 = 第 1 段的 after, ...
        nth = len(scans)
        scans.append(nth)
        is_after = nth % 2 == 1
        snap = dict(CACHE_FULL)
        if empty_name and any(_seg_of(c)["name"] == empty_name for c in calls):
            idx = [s["name"] for s in dr.SEGMENTS].index(empty_name)
            for k in dr.SEGMENTS[idx]["keys"]:
                snap.pop(k, None)
        if grown_keys and is_after:
            for k in grown_keys:
                snap[k] = {"XX": {"dates": ["2026-01-01", "2026-12-31"]}}
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


# ---------------- 09-19 A 批次: 改用 market_data 的统一退出码(0/1/3) ----------------

def test_rc3_is_classified_empty(monkeypatch, capsys):
    """退出码 3 = 未推进/空数据(不是失败也不是成功) → EMPTY, 聚合码 2。

    判别力: 若把 3 当成功(或当失败), 本用例红。
    """
    seg = dr.SEGMENTS[4]["name"]
    _install(monkeypatch, empty_name=seg, rc_map={seg: 3})
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 2
    assert seg in _line(out, "  空数据:")
    assert "失败: (无)" in out


def test_unknown_rc_is_failure(monkeypatch, capsys):
    """看不懂的退出码(如 7)一律按失败 —— 保守, 绝不当成功。"""
    seg = dr.SEGMENTS[3]["name"]
    _install(monkeypatch, rc_map={seg: 7})
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert seg in _line(out, "  失败:")


def test_rc1_but_cache_grew_reports_both(monkeypatch, capsys):
    """矛盾(退出码 1 但缓存推进了)→ 判 FAIL(严), 但两条证据都打印出来。"""
    seg = dr.SEGMENTS[0]["name"]                      # sectors: keys 含 kline
    _install(monkeypatch, rc_map={seg: 1}, grown_keys=("kline",))
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 1
    assert "矛盾" in out and "缓存本次推进了" in out
    assert "退出码 1" in out


def test_rc0_but_cache_empty_reports_both(monkeypatch, capsys):
    """矛盾(退出码 0 自称成功但该段为空)→ EMPTY, 且两条证据都打印。"""
    seg = dr.SEGMENTS[4]["name"]
    _install(monkeypatch, empty_name=seg, rc_map={seg: 0})
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 2
    assert "矛盾" in out and "该段缓存为空" in out


def test_rc0_without_progress_is_reported_as_note_not_mismatch(monkeypatch,
                                                               capsys):
    """退出码 0 但末日未推进 → 只作注记(同日重采/非交易日都这样), 不判死。"""
    monkeypatch.setattr(dr, "_run_cmd", lambda cmd: _CP(0, "ok"))
    monkeypatch.setattr(dr, "_load_cache", lambda: dict(CACHE_FULL))
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 0, "未推进不得被自动判死(那需要交易日历)"
    assert "末日未推进" in out
    assert "矛盾" not in out


# ---------------- 09-19: sectors 段的 东财 → 申万 降级链 ----------------
#
# 为什么需要(问题): sectors 段用默认源(eastmoney)。本机东财 push2/push2his 连接
# 被 reset, 该段**每个交易日**必判 rc=1 ⇒ 计划任务天天落 data_refresh_FAILED.txt
# 告警旗, 而缓存其实本可以用 --source sw 刷新(kline/flow 本来就是申万 801)。
# 这就是"功能名义存在、实际不通"。
#
# 判定边界(有意选择, 不是默认值): 降级成功 ⇒ 该段判 **OK 并标注降级**, 聚合里
# 只作"注记"(成功行里带 (降级→--source sw)), **不**升级为 EMPTY/FAIL ——
# 源降级是有意设计的容错, 不是故障; 判 EMPTY(2) 会让计划任务把"数据其实刷到了"
# 报成告警。但"降级后拿不到的段"必须逐段如实列出, 绝不冒充全绿。
# 两次都失败 ⇒ 老实判 FAIL(降级不是洗白)。

def test_sectors_primary_fail_falls_back_to_sw(monkeypatch, capsys):
    """① 主源(东财)失败 → 用 --source sw 重试一次; 成功则判成功且**标注降级**。

    判别力(变异取证): 去掉降级重试 ⇒ 该段判 FAIL、调用数少一条 ⇒ 本用例红。
    """
    calls = _install(monkeypatch, fail_name="sectors", sw_rc=0)
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 0, "降级成功不该判失败(源降级是有意的容错, 不是故障)"
    assert len(calls) == len(dr.SEGMENTS) + 1, "sectors 必须被重试恰好一次"
    assert calls[1][3:] == ["--build-sectors", "--source", "sw"], \
        "重试必须换成申万源(不是原样再跑一次东财)"
    assert "sectors" in _line(out, "  成功:")
    assert "失败: (无)" in out
    assert "降级" in _line(out, "  成功:"), "降级不许被冒充成全绿"


def test_sectors_both_sources_fail_is_still_fail(monkeypatch, capsys):
    """② 主源与降级源都失败 → 仍判 FAIL(降级不许把失败洗白)。"""
    calls = _install(monkeypatch, fail_name="sectors")
    code = dr.main([])
    out = capsys.readouterr().out
    assert code == 1, "两次都失败必须非零退出"
    assert len(calls) == len(dr.SEGMENTS) + 1, "失败也要走完降级重试再判死"
    assert "sectors" in _line(out, "  失败:")
    assert "sectors" not in _line(out, "  成功:")
    assert "--source sw" in _line(out, "  降级:"), \
        "两路皆败也要说清试过哪个降级源"


def test_degraded_run_names_segments_still_unavailable(monkeypatch, capsys):
    """③ 降级后**拿不到**的段必须如实列出(申万无 flow 段)。

    注意 flow 在缓存里**有数据**(CACHE_FULL): 这里要报的是"本次拿不到",
    与"缓存为空"是两回事 —— 少了这句等于把降级说成"全都刷到了"。
    判别力(变异取证): 抹掉"仍拿不到"这一句 ⇒ 本用例红。
    """
    _install(monkeypatch, fail_name="sectors", sw_rc=0)
    dr.main([])
    out = capsys.readouterr().out
    ln = _line(out, "  降级:")
    assert "拿不到" in ln and "flow" in ln, \
        "降级后拿不到的段必须逐段点名: %r" % ln
    assert "--source sw" in ln


def test_child_process_has_timeout(monkeypatch):
    """不许长时间阻塞: 单段子进程必须带超时(降级 = 至多 2 次 × 超时)。

    判别力(变异取证): 去掉 timeout 参数 ⇒ 东财连接挂死会挂住整个计划任务 ⇒ 红。
    """
    seen = {}

    class _P:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        seen.update(kw)
        return _P()

    monkeypatch.setattr(dr.subprocess, "run", fake_run)
    dr._run_cmd(["python", "-c", "pass"])
    assert seen.get("timeout"), "子进程必须带有限超时, 否则会无限阻塞"

