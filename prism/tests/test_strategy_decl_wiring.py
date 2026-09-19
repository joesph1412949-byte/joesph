# -*- coding: utf-8 -*-
"""策略"声明了却不生效"的接线测试(2026-09-19 批)。

四处键, 每处都注明判别力(这条能抓到什么错):
  A. execution.pick_slot / execution.open_window —— 改前 prism/schedule.py:39-40
     是**硬编码**常量: 改 JSON 不生效, 也不报错(配置看着被尊重, 实际被忽略)。
     现在 schedule 在 import 时走 engine.resolve_strategy 解析(与守护/引擎同一 loader)。
  B. market_gate.model —— 改前**全仓零读取点**: 手改成未知取值不生效也不报错,
     守护照样按 node 语义跑。现在未知取值 → fail-closed 拒绝加载(守卫在
     engine.load_strategy, 所有读取方共用, 含绕开 run_daily 的模拟盘/网页/回测)。
  C. filters.environment_threshold —— 与 market_gate.threshold 是同一个数
     (engine.validate_strategy_payload 由同一个 gt 写出两份), 全仓零读取点 ⇒
     停止声明; 历史文件仍带该键时必须能读(忽略而非报错)。
  D. execution.one_word_fallback —— 桥(qmt/bridge/signal_bridge_real.py)的
     ACTION_TO_OP 只有 BUY/SELL(**没有撤单通道**), paper 的一字板腿是**无条件**
     排队(create_pending_buy/check_pending_buys 不读该键, 也没有"跳过"实现), 且
     语义不同(实盘计划价 = **前一日**涨停价, 次日真一字板时那张低位限价单根本不
     成交) ⇒ 它是**描述无条件行为的死声明**, 停止声明(留着会误导"实盘也能排队打板");
     历史文件仍带该键(含垃圾值)时必须能读、行为不变。

离线: 篡改用例在 tmp 目录造策略文件 + 改 engine.STRATEGIES_DIR, **不碰**真实
策略文件; 不联网、不连 QMT、不写 D:/QMT_SIGNALS。
"""
import importlib
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism import backtest, engine, registry as reg, schedule, trader

REAL = engine.STRATEGIES_DIR


def _doc(p):
    return json.loads(Path(p).read_text(encoding="utf-8"))


def _tmp_active(tmp_path, monkeypatch, execution):
    """造 tmp 策略目录: 拷贝真实 full_factor_v1(保留真实因子 id 以过因子校验),
    只替换 execution 块, 并把默认指针指向它(= 标准加载路径, 无测试专用入口)。"""
    doc = _doc(REAL / "full_factor_v1.json")
    doc["execution"] = execution
    (tmp_path / "full_factor_v1.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (tmp_path / ".active.json").write_text('{"id": "full_factor_v1"}',
                                           encoding="utf-8")
    monkeypatch.setattr(engine, "STRATEGIES_DIR", tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _real_factors():
    """本文件直接走 engine.load_strategy(含因子存在性校验) → 真实因子库在场。"""
    reg.scan_factors("prism.factors")
    yield


# ---------------- A. 零行为变更: 现值与改前硬编码逐字符相等 ----------------

def test_shipped_values_match_previous_hardcoded_constants():
    """判别力: 接线后三个键的现值必须与改前硬编码**逐字符相等**(零行为变更的硬目标)。

    改前: PICK_SLOT == "15:05" / OPEN_WINDOW == ("09:26", "09:35") /
    SETTLE_AFTER == "15:00"; 前两个现在由策略 JSON 解析而来 —— 值与来源都要对。
    """
    assert schedule.PICK_SLOT == "15:05"
    assert schedule.OPEN_WINDOW == ("09:26", "09:35")
    assert schedule.SETTLE_AFTER == "15:00"
    ex = _doc(REAL / "full_factor_v1.json")["execution"]
    assert schedule.PICK_SLOT == ex["pick_slot"]
    assert schedule.OPEN_WINDOW == tuple(ex["open_window"].split("-"))


def test_both_daemons_snapshot_schedule_constants_not_a_second_copy():
    """判别力(暗坑防线): paper/live 两个守护都在 import 时各自快照 schedule 的
    常量(两文件都读 schedule.X) —— 钉住"守护看到的 = schedule 解析出的策略值",
    没有哪个守护落在另一份旧常量上。"""
    from prism import live_daemon, paper_daemon
    for mod in (live_daemon, paper_daemon):
        assert mod.PICK_SLOT == schedule.PICK_SLOT
        assert mod.OPEN_WINDOW == schedule.OPEN_WINDOW
        assert mod.SETTLE_AFTER == schedule.SETTLE_AFTER


# ---------------- A. 篡改 JSON 即生效(本任务关键判别力) ----------------

def test_tampered_execution_times_take_effect(tmp_path, monkeypatch):
    """判别力(关键): 把 execution 改成别的时点 → 解析结果跟着变。
    改前 schedule 是硬编码常量 ⇒ 这条必红(JSON 怎么写都不动)。"""
    _tmp_active(tmp_path, monkeypatch,
                {"pick_slot": "14:30", "open_window": "09:20-09:25"})
    assert schedule.execution_slots() == ("14:30", ("09:20", "09:25"))


def test_import_time_constants_follow_the_strategy_json(tmp_path, monkeypatch):
    """判别力: 模块常量(reload 即重走 import 路径)就是"策略 JSON → 解析"的结果 ——
    证明守护拿到的是 JSON 驱动的时点, 而不是另一份硬编码。测完 reload 回真实目录。"""
    _tmp_active(tmp_path, monkeypatch,
                {"pick_slot": "14:30", "open_window": "09:20-09:25"})
    try:
        m = importlib.reload(schedule)
        assert (m.PICK_SLOT, m.OPEN_WINDOW) == ("14:30", ("09:20", "09:25"))
    finally:
        monkeypatch.setattr(engine, "STRATEGIES_DIR", REAL)
        importlib.reload(schedule)
    assert (schedule.PICK_SLOT, schedule.OPEN_WINDOW) == ("15:05",
                                                          ("09:26", "09:35"))


# ---------------- A. 失败方向: fail-closed + 告警, 不放宽 ----------------

@pytest.mark.parametrize("bad", [
    {},                                              # 整个 execution 缺键
    {"pick_slot": "25:00"},                          # 越界小时
    {"pick_slot": "15:60"},                          # 越界分钟
    {"pick_slot": 1505},                             # 非字符串
    {"pick_slot": "9:05"},                           # 非严格 HH:MM(字符串比较会错)
    {"pick_slot": ""},                               # 空串
])
def test_bad_pick_slot_falls_back_with_warning(tmp_path, monkeypatch, caplog,
                                               bad):
    """判别力: 缺键/垃圾值 → 回落硬编码默认值 + WARNING(绝不静默), 且不抛异常
    (抛了就是守护启动崩)。"""
    _tmp_active(tmp_path, monkeypatch, dict(bad, **{"open_window": "09:26-09:35"}))
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.execution_slots()[0] == "15:05"
    assert "pick_slot" in caplog.text


@pytest.mark.parametrize("win", [
    None,                       # 缺键
    "09:26",                    # 缺 "-"
    "09:26-",                   # 尾端缺
    "-09:35",                   # 首端缺
    "09:26-09:70",              # 越界分钟
    "09:26-09:35-10:00",        # 多段
    "09:35-09:26",              # 逆序 → 守护 a<=hm<=b 恒 False(窗口静默失效)
    "9:26-9:35",                # 非严格
    "",
])
def test_bad_open_window_falls_back_and_never_widens(tmp_path, monkeypatch,
                                                     caplog, win):
    """判别力(硬要求): 坏窗口 → 回落 ("09:26","09:35") + WARNING, **绝不放宽**。
    回落目标是改前的硬编码窗口(不是无限宽、不是空窗), 断言逐字符相等即钉死方向。
    """
    _tmp_active(tmp_path, monkeypatch,
                {"pick_slot": "15:05", "open_window": win})
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.execution_slots()[1] == ("09:26", "09:35")
    assert "open_window" in caplog.text


def test_valid_but_different_window_is_honored_not_clamped(tmp_path, monkeypatch):
    """判别力(反面): 合法但更宽的声明值("08:00-16:00")是策略作者的**显式意图**,
    照读不误 —— 解析层只挡格式/逆序, 不自作主张收窄或放宽。"""
    _tmp_active(tmp_path, monkeypatch,
                {"pick_slot": "15:05", "open_window": "08:00-16:00"})
    assert schedule.execution_slots() == ("15:05", ("08:00", "16:00"))


def test_unreadable_strategy_falls_back_instead_of_raising(tmp_path,
                                                           monkeypatch, caplog):
    """判别力: 策略文件坏/指针坏(冷机器、半截写盘) → 回落默认值 + WARNING,
    绝不抛 —— 守护启动不能被一份坏 JSON 拦住。"""
    (tmp_path / ".active.json").write_text('{"id": "full_factor_v1"}',
                                           encoding="utf-8")
    (tmp_path / "full_factor_v1.json").write_text("{不是 json", encoding="utf-8")
    monkeypatch.setattr(engine, "STRATEGIES_DIR", tmp_path)
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.execution_slots() == ("15:05", ("09:26", "09:35"))
    assert "不可读" in caplog.text


# ---------------- B. market_gate.model: 未知取值 fail-closed ----------------
# 守卫落点 = engine.load_strategy(所有读取方共用), 不再是 trader.run_daily 独占。

def test_unknown_gate_model_refuses_to_load():
    """判别力: model 改成未知取值 → engine.load_strategy 抛错(fail-closed)。
    改前该键在 load_strategy 里是**零读取点**(唯一守卫在 trader._check_gate_model,
    只覆盖 run_daily)⇒ 这条必红。"""
    strat = _doc(REAL / "full_factor_v1.json")
    strat["market_gate"]["model"] = "node_v2"
    with pytest.raises(ValueError, match="node_v2"):
        engine.load_strategy(strat)


def test_guard_covers_the_entries_that_bypass_run_daily(tmp_path, monkeypatch):
    """判别力(本批全部价值): 守卫搬进 load_strategy 后, **绕开 run_daily 的入口**
    也守住了 —— 改前它们全照 node 语义跑, 一声不响:
      · 网页选股 prism_web.app._load_strategy_for_screen
      · 回测     prism.backtest(直接 load_strategy)
      · 模拟盘   paper.PaperAccount.strategy(→ engine.resolve_strategy)
    """
    doc = _doc(REAL / "full_factor_v1.json")
    doc["market_gate"]["model"] = "node_v2"
    (tmp_path / "full_factor_v1.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (tmp_path / ".active.json").write_text('{"id": "full_factor_v1"}',
                                           encoding="utf-8")
    monkeypatch.setattr(engine, "STRATEGIES_DIR", tmp_path)
    import prism_web.app as webapp
    monkeypatch.setattr(webapp, "STRATEGIES_DIR", tmp_path)
    from prism import paper
    with pytest.raises(ValueError, match="node_v2"):        # 网页选股
        webapp._load_strategy_for_screen("full_factor_v1")
    with pytest.raises(ValueError, match="node_v2"):        # 回测
        backtest.load_strategy(tmp_path / "full_factor_v1.json")
    acct = paper.PaperAccount(state_path=tmp_path / "state.json")
    with pytest.raises(ValueError, match="node_v2"):        # 模拟盘(首载无退路)
        acct.strategy


def test_run_daily_still_refuses_unknown_gate_model(monkeypatch):
    """判别力(回归防线): 删掉 trader._check_gate_model 后 run_daily 入口**仍**拒绝
    (经 resolve_strategy → load_strategy) —— 证明是搬走了守卫, 不是删没了。"""
    monkeypatch.setattr(trader, "check_paused", lambda: False)
    strat = _doc(REAL / "full_factor_v1.json")
    strat["market_gate"]["model"] = "node_v2"
    with pytest.raises(ValueError, match="node_v2"):
        trader.run_daily(strat, None)


def test_known_and_absent_gate_model_are_allowed():
    """判别力(反面): 唯一实现了的取值 node / 缺 model 键 / 整个 market_gate 缺失
    都放行 —— 不许把正常策略挡死(缺键 = 写盘侧的唯一取值)。"""
    strat = _doc(REAL / "full_factor_v1.json")
    assert engine.load_strategy(strat)["market_gate"]["model"] == "node"
    del strat["market_gate"]["model"]
    assert engine.load_strategy(strat)["id"] == strat["id"]
    strat.pop("market_gate")
    assert engine.load_strategy(strat)["id"] == strat["id"]


# ---------------- C. filters.environment_threshold: 停止声明 ----------------

def test_shipped_strategies_stop_declaring_the_duplicate_key():
    """判别力: 三份随仓策略都不再声明 filters.environment_threshold ——
    它与 market_gate.threshold 由同一个值写出两份, 全仓零读取点。"""
    files = [p for p in sorted(REAL.glob("*.json"))
             if not p.name.startswith(".")]
    assert files, "策略目录里没有策略?"
    for p in files:
        assert "environment_threshold" not in (_doc(p).get("filters") or {}), \
            p.name


def test_legacy_json_still_carrying_the_key_loads_fine(tmp_path):
    """兼容旧 JSON(硬要求): 历史文件仍带该键时必须能读 —— 忽略而非报错。
    取一个与门槛**刻意不一致**的值(99), 证明它确实不参与任何判定。"""
    doc = _doc(REAL / "full_factor_v1.json")
    doc["id"] = "legacy_dup_key"
    doc["filters"]["environment_threshold"] = 99
    p = tmp_path / "legacy_dup_key.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    assert engine.load_strategy(p)["filters"]["environment_threshold"] == 99


# ---------------- D. execution.one_word_fallback: 停止声明 ----------------

def test_shipped_strategies_stop_declaring_one_word_fallback():
    """判别力: 随仓策略都不再声明 execution.one_word_fallback —— 见文件头 D 段:
    桥没有撤单通道 + paper 的一字板腿无条件排队(不读该键) + 实盘计划价是前一日
    涨停价 ⇒ 它只是描述无条件行为的死声明, 留着会误导"实盘也能排队打板"。"""
    files = [p for p in sorted(REAL.glob("*.json"))
             if not p.name.startswith(".")]
    assert files, "策略目录里没有策略?"
    for p in files:
        assert "one_word_fallback" not in (_doc(p).get("execution") or {}), p.name


@pytest.mark.parametrize("bogus", ["cancel", "skip", 0, None, {"x": 1}])
def test_legacy_one_word_fallback_is_ignored_not_validated(tmp_path, bogus):
    """兼容旧 JSON(硬要求): 历史文件仍带该键(**含垃圾值**)必须能正常加载、行为不变。
    全仓零读取点 ⇒ 不校验、不报错、不参与任何判定。
    判别力: 加载结果与"不带该键"的**同一份策略**逐字段相等(只多出这一个不被读的
    键), 取值原样透传不被改写 —— 这条钉的是"静默忽略"而不是"悄悄纠正"。"""
    clean = _doc(REAL / "full_factor_v1.json")
    clean["execution"].pop("one_word_fallback", None)
    doc = json.loads(json.dumps(clean))          # 兼容性只取决于 loader, 不依赖随仓值
    doc["execution"]["one_word_fallback"] = bogus
    p = tmp_path / "legacy_one_word.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    loaded = engine.load_strategy(p)
    assert loaded["execution"].pop("one_word_fallback") == bogus
    assert loaded == clean


def test_shipped_strategies_pass_the_declared_key_lint():
    """判别力(复生防线): 随仓策略声明的**每个**键都必须有生产读取点 —— lint 一报出
    未消费键(又有人声明"只写不读"的键)这条就红。本批删掉 one_word_fallback 的收口,
    不改 lint 的判定逻辑, 只钉它的结论。"""
    from prism import strategy_lint as sl
    bad = [(r["strategy"], [e["key"] for e in r["unconsumed"]])
           for r in sl.lint_all() if r["unconsumed"]]
    assert bad == [], bad
