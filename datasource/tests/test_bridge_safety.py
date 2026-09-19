# -*- coding: utf-8 -*-
"""qmt_signal_bridge_real 安全闸门单元测试 — 授权文件机制 + 当日去重。
桥脚本顶层无副作用(只定义常量/函数, 无 __main__ 自测), 可安全 import。
测试只动 tmp_path 下的文件, 绝不触碰真实 D:/QMT_SIGNALS。

第二批(桥安全闸门修复)新增: paused 闸门 / armed 每单重读 / 去重账原子写与
fail-closed / passorder 异常记账 / 同轮卖出未受理禁买单 / demo 桥与 live_check。
所有 drain_queue 用例都带 DRY_RUN=True 或 stub 掉 passorder —— 绝不真下单。"""
import gc
import importlib
import json
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 项目根

from qmt.bridge import signal_bridge_real as bridge


# ---------- 授权文件机制 ----------

def test_armed_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "ARMED_FILE", str(tmp_path / "armed.txt"))
    ok, msg = bridge._is_armed()
    assert ok is False
    assert "missing" in msg


def test_armed_dated_today(monkeypatch, tmp_path):
    f = tmp_path / "armed.txt"
    f.write_text(datetime.now().strftime("%Y%m%d"), encoding="utf-8")
    monkeypatch.setattr(bridge, "ARMED_FILE", str(f))
    ok, msg = bridge._is_armed()
    assert ok is True
    assert "armed" in msg


def test_armed_stale_date(monkeypatch, tmp_path):
    f = tmp_path / "armed.txt"
    f.write_text("20200101", encoding="utf-8")   # 过期日期
    monkeypatch.setattr(bridge, "ARMED_FILE", str(f))
    ok, msg = bridge._is_armed()
    assert ok is False
    assert "not dated today" in msg


def test_armed_empty_content(monkeypatch, tmp_path):
    f = tmp_path / "armed.txt"
    f.write_text("", encoding="utf-8")
    monkeypatch.setattr(bridge, "ARMED_FILE", str(f))
    ok, _ = bridge._is_armed()
    assert ok is False


# ---------- 当日去重 ----------

def test_duplicate_detection_after_mark(monkeypatch, tmp_path):
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    sig = {"stock_code": "600000.SH", "order_id": "BUY_1"}
    placed = bridge._load_placed()
    assert bridge._already_placed_today(sig, placed) is False
    bridge._mark_placed_today(sig, placed)
    placed2 = bridge._load_placed()
    assert bridge._already_placed_today(sig, placed2) is True


def test_duplicate_distinct_codes_not_blocked(monkeypatch, tmp_path):
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    sig_a = {"stock_code": "600000.SH"}
    sig_b = {"stock_code": "000001.SZ"}
    placed = bridge._load_placed()
    bridge._mark_placed_today(sig_a, placed)
    placed2 = bridge._load_placed()
    assert bridge._already_placed_today(sig_a, placed2) is True
    assert bridge._already_placed_today(sig_b, placed2) is False


def test_duplicate_bare_code_suffix_normalized(monkeypatch, tmp_path):
    # 同一股票裸代码/带后缀写法 → 视作同一只 → 去重命中
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    sig = {"stock_code": "600000"}          # 裸代码
    sig2 = {"stock_code": "600000.SH"}      # 带后缀
    placed = bridge._load_placed()
    bridge._mark_placed_today(sig, placed)
    placed2 = bridge._load_placed()
    assert bridge._already_placed_today(sig2, placed2) is True


def test_duplicate_rolls_over_new_day(monkeypatch, tmp_path):
    # 去重记录带日期: 昨天的记录不应拦截今天的下单(按当天日期取)
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    sig = {"stock_code": "600000.SH"}
    placed = bridge._load_placed()
    bridge._mark_placed_today(sig, placed)
    # 手动把记录日期改到昨天 → 不再算"今日已下单"
    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now().date().toordinal() - 1)
    import datetime as _dt
    yesterday_key = _dt.date.fromordinal(yesterday).strftime("%Y%m%d")
    placed2 = bridge._load_placed()
    assert today in placed2
    # 把今天的记录"挪"到昨天
    placed2[yesterday_key] = placed2.pop(today)
    assert bridge._already_placed_today(sig, placed2) is False


# ---------- 价格合理性校验(fat-finger 防护) ----------

def test_price_absurd_rejected(monkeypatch):
    # 手误价格(如 999999.99) → 拒单, 不进 passorder
    monkeypatch.setattr(bridge, "DRY_RUN", True)   # 即便 dry-run 也先做 sanity 校验
    ok, msg = bridge._call_passorder(
        {"stock_code": "600000.SH", "action": "BUY", "price": 999999.99, "volume": 100})
    assert ok is False
    assert "out of range" in msg


def test_price_negative_rejected(monkeypatch):
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    ok, msg = bridge._call_passorder(
        {"stock_code": "600000.SH", "action": "BUY", "price": -5.0, "volume": 100})
    assert ok is False
    assert "out of range" in msg


def test_price_sane_accepted_under_dry_run(monkeypatch):
    # 正常涨停价(如 20.50)在范围内 → dry-run 通过(返回 DRY_RUN 提示)
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    ok, msg = bridge._call_passorder(
        {"stock_code": "600000.SH", "action": "BUY", "price": 20.50, "volume": 100})
    assert ok is True
    assert "DRY_RUN" in msg


# ================================================================
# 以下为第二批(桥安全闸门修复)新增用例。
# 把桥的所有真实路径指向 tmp —— 绝不触碰 D:/QMT_SIGNALS。
# ================================================================

def _tmp_dirs(monkeypatch, tmp_path):
    """重定向桥的全部文件路径到 tmp_path; DRY_RUN=True 兜底防真发单。"""
    monkeypatch.setattr(bridge, "PAUSE_FILE", str(tmp_path / "paused"))
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(tmp_path / "placed_today.json"))
    monkeypatch.setattr(bridge, "PENDING_DIR", str(tmp_path / "pending"))
    monkeypatch.setattr(bridge, "DONE_DIR", str(tmp_path / "done"))
    monkeypatch.setattr(bridge, "FAILED_DIR", str(tmp_path / "failed"))
    monkeypatch.setattr(bridge, "TRADES_DIR", str(tmp_path / "trades"))
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    for name in ("PENDING_DIR", "DONE_DIR", "FAILED_DIR", "TRADES_DIR"):
        os.makedirs(getattr(bridge, name), exist_ok=True)
    _reset_bridge_state()
    return tmp_path


def _reset_bridge_state():
    with bridge._queue_lock:
        bridge._signal_queue[:] = []
        bridge._queued_files.clear()
    reported = getattr(bridge, "_kept_files", None)
    if reported is not None:
        reported.clear()


def _sig(order_id, code="600000.SH", action="BUY", price=10.0, volume=100):
    return {"order_id": order_id, "stock_code": code, "action": action,
            "price": price, "volume": volume}


def _enqueue(root, sigs, prefix="s"):
    """把信号写成 pending/*.json 并塞进桥的队列(绕开 QMT handlebar)。"""
    files = []
    for i, sig in enumerate(sigs):
        f = root / "pending" / ("%s%d.json" % (prefix, i))
        f.write_text(json.dumps(sig, ensure_ascii=False), encoding="utf-8")
        files.append(f)
    with bridge._queue_lock:
        for f, sig in zip(files, sigs):
            bridge._signal_queue.append((sig, str(f)))
            bridge._queued_files.add(str(f))
    return files


# ---------- C1: paused 闸门 ----------

def test_is_paused_reads_signal_root_paused_file(monkeypatch, tmp_path):
    """paused 与 prism/trader.check_paused、ttcore.daemon.is_paused 同口径:
    信号根下的 paused 文件。"""
    monkeypatch.setattr(bridge, "PAUSE_FILE", str(tmp_path / "paused"))
    assert bridge._is_paused() is False
    (tmp_path / "paused").write_text("stop", encoding="utf-8")
    assert bridge._is_paused() is True


def test_should_place_paused_wins_over_armed_and_dedup(monkeypatch):
    """串联顺序: paused 在最前 —— 即便已武装、已在去重账里, 急停也必须拦。"""
    today = datetime.now().strftime("%Y%m%d")
    sig = _sig("OID-1")
    placed = {today: [bridge._dedup_key(sig)]}
    decision, _ = bridge._should_place(sig, True, True, placed, set())
    assert decision == "paused"


def test_should_place_chain_not_armed_then_duplicate_then_place(monkeypatch):
    monkeypatch.setattr(bridge, "DEDUP_ENABLED", True)
    today = datetime.now().strftime("%Y%m%d")
    sig = _sig("OID-2")
    assert bridge._should_place(sig, False, False, {}, set())[0] == "not_armed"
    placed = {today: [bridge._dedup_key(sig)]}
    assert bridge._should_place(sig, True, False, placed, set())[0] == "duplicate"
    assert bridge._should_place(_sig("OID-3"), True, False, placed, set())[0] == "place"


def test_paused_keeps_pending_file_and_writes_no_result(monkeypatch, tmp_path):
    """急停时: pending 文件必须保留(不许消费), 也不写 done/failed 结果。"""
    root = _tmp_dirs(monkeypatch, tmp_path)
    (root / "paused").write_text("stop", encoding="utf-8")
    monkeypatch.setattr(bridge, "_is_armed", lambda: (True, "armed"))
    files = _enqueue(root, [_sig("OID-P1")])
    try:
        bridge.drain_queue()
        assert files[0].exists() is True
        assert list((root / "done").iterdir()) == []
        assert list((root / "failed").iterdir()) == []
        assert (root / "placed_today.json").exists() is False
    finally:
        _reset_bridge_state()


# ---------- C2: armed 每单重读 ----------

def test_armed_reread_per_order_midbatch_stop_effective(monkeypatch, tmp_path):
    """批次中途撤防: 只允许第 1 单, 余下 2 单必须判 NOT ARMED。

    RED 现象: armed 在 for 之前只读一次 → 3 单全下(旧行为)。"""
    root = _tmp_dirs(monkeypatch, tmp_path)
    calls = []

    def fake_armed():
        calls.append(1)
        if len(calls) == 1:
            return True, "armed"
        return False, "armed file missing: (deleted mid-batch)"

    monkeypatch.setattr(bridge, "_is_armed", fake_armed)
    files = _enqueue(root, [_sig("OID-A0"), _sig("OID-A1"), _sig("OID-A2")])
    try:
        bridge.drain_queue()
        assert len(calls) == 3, "armed 必须每单重读一次"
        assert len(list((root / "done").iterdir())) == 1
        assert len(list((root / "failed").iterdir())) == 2
        assert files[0].exists() is False
        assert files[1].exists() is False and files[2].exists() is False
    finally:
        _reset_bridge_state()


# ---------- C3: 去重账 ----------

def test_load_placed_missing_file_is_empty(monkeypatch, tmp_path):
    """文件根本不存在 = 今天还没下过单 → {} (合法空账)。"""
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(tmp_path / "placed_today.json"))
    assert bridge._load_placed() == {}


def test_load_placed_corrupt_file_fails_closed(monkeypatch, tmp_path):
    """文件在但读不动/被截断 → 必须 fail-CLOSED(None), 不能当成空账放行。

    RED 现象: 旧实现一律 return {} → 去重闸门静默关闭。"""
    dedup = tmp_path / "placed_today.json"
    dedup.write_text('{"20260919": ["OID-1"', encoding="utf-8")   # 截断的 JSON
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    assert bridge._load_placed() is None


def test_should_place_dedup_unreadable_blocks(monkeypatch):
    """去重账不可读 → 拒单(宁可漏单不可重单)。"""
    monkeypatch.setattr(bridge, "DEDUP_ENABLED", True)
    decision, detail = bridge._should_place(_sig("OID-C1"), True, False, None, set())
    assert decision == "dedup_unreadable"
    assert "DEDUP" in detail


def test_load_placed_regression_new_guard_still_reads_valid_file(monkeypatch, tmp_path):
    """回归守门: 正常文件仍照旧读出(别把 fail-closed 做成一刀切)。"""
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    sig = {"stock_code": "600000.SH", "order_id": "BUY_9"}
    placed = bridge._load_placed()
    bridge._mark_placed_today(sig, placed)
    got = bridge._load_placed()
    assert bridge._already_placed_today(sig, got) is True


def test_save_placed_failure_leaves_previous_account_intact(monkeypatch, tmp_path):
    """写账中途失败(内容不可序列化) → 旧账必须完好, 不许被截断成空文件。

    RED 现象: 旧实现 open(...,"w") 先截断 → json.dump 抛错后文件变空。"""
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    bridge._save_placed({"20260919": ["OID-OK"]})
    before = dedup.read_text(encoding="utf-8")
    bridge._save_placed({"20260919": {"bad": {1, 2}}})     # set 不可 JSON 序列化
    assert dedup.read_text(encoding="utf-8") == before
    assert bridge._load_placed() == {"20260919": ["OID-OK"]}
    assert not (tmp_path / "placed_today.json.tmp").exists()


def test_save_placed_retries_once_on_eacces(monkeypatch, tmp_path):
    """目标被读句柄占用(WinError 32 → PermissionError) → 重试一次即成功。"""
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    real_replace = os.replace
    seen = {"n": 0}

    def flaky_replace(src, dst):
        seen["n"] += 1
        if seen["n"] == 1:
            raise PermissionError(13, "file in use (WinError 32)")
        return real_replace(src, dst)

    monkeypatch.setattr(bridge.os, "replace", flaky_replace)
    monkeypatch.setattr(bridge.time, "sleep", lambda _s: None)
    bridge._save_placed({"20260919": ["OID-R"]})
    assert seen["n"] == 2, "必须重试恰好一次"
    assert bridge._load_placed() == {"20260919": ["OID-R"]}
    assert not (tmp_path / "placed_today.json.tmp").exists()


def test_placed_reloaded_per_order(monkeypatch, tmp_path):
    """去重账每单重读: 两个 QMT 终端并跑时把"读到旧账"的窗口收窄到单笔级别。"""
    root = _tmp_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "_is_armed", lambda: (True, "armed"))
    calls = []
    real_load = bridge._load_placed

    def counting_load():
        calls.append(1)
        return real_load()

    monkeypatch.setattr(bridge, "_load_placed", counting_load)
    _enqueue(root, [_sig("OID-D0"), _sig("OID-D1"), _sig("OID-D2")])
    try:
        bridge.drain_queue()
        assert len(calls) == 3
    finally:
        _reset_bridge_state()


def test_passorder_exception_still_marks_placed(monkeypatch, tmp_path):
    """passorder 抛异常时订单可能已到券商 → 必须记账, 防重发第二笔。

    RED 现象: 旧实现只在 success 时记账 → 异常后重发即双单。"""
    root = _tmp_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "DRY_RUN", False)
    monkeypatch.setattr(bridge, "_is_armed", lambda: (True, "armed"))
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")

    def boom(*_a, **_k):
        raise RuntimeError("submit timeout after reaching broker")

    monkeypatch.setattr(bridge, "passorder", boom, raising=False)
    sig = _sig("OID-E1")
    files = _enqueue(root, [sig])
    try:
        bridge.drain_queue()
        assert files[0].exists() is False
        assert len(list((root / "failed").iterdir())) == 1
        assert bridge._already_placed_today(sig, bridge._load_placed()) is True
    finally:
        _reset_bridge_state()


def test_validation_rejection_does_not_mark_placed(monkeypatch, tmp_path):
    """守门: 本地校验拒单(如无账号/量非法)根本没到券商 → 不记账, 保持原语义。"""
    root = _tmp_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "DRY_RUN", False)
    monkeypatch.setattr(bridge, "_is_armed", lambda: (True, "armed"))
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "")
    monkeypatch.setattr(bridge, "ContextInfo", None, raising=False)

    def boom(*_a, **_k):
        raise AssertionError("passorder 不该被调用")

    monkeypatch.setattr(bridge, "passorder", boom, raising=False)
    sig = _sig("OID-E2")
    _enqueue(root, [sig])
    try:
        bridge.drain_queue()
        assert bridge._already_placed_today(sig, bridge._load_placed() or {}) is False
    finally:
        _reset_bridge_state()


# ---------- C6: 同轮卖出未受理 → 禁同标的买单 ----------

def test_should_place_blocks_buy_when_same_code_sell_not_accepted(monkeypatch):
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    rejected = {bridge._code_key(_sig("S-1"))}
    buy_same = _sig("B-1", code="600000")          # 裸代码, 需归一化后能命中
    buy_other = _sig("B-2", code="000001.SZ")
    assert bridge._should_place(buy_same, True, False, {}, rejected)[0] == "sell_not_accepted"
    assert bridge._should_place(buy_other, True, False, {}, rejected)[0] == "place"
    sell_same = _sig("S-2", code="600000", action="SELL")
    assert bridge._should_place(sell_same, True, False, {}, rejected)[0] == "place"


def test_drain_blocks_same_code_buy_after_rejected_sell(monkeypatch, tmp_path):
    """同轮内某标的 SELL 被柜台拒 → 该标的 BUY 不下单(写 failed 并消费文件);
    其它标的的买单不受影响。"""
    root = _tmp_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(bridge, "DRY_RUN", False)
    monkeypatch.setattr(bridge, "_is_armed", lambda: (True, "armed"))
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")
    attempted = []

    def fake_passorder(op_type, order_type, account, code, pr_type, price, volume, ctx):
        attempted.append(code)
        return 99            # 非 0 = 柜台未受理

    monkeypatch.setattr(bridge, "passorder", fake_passorder, raising=False)
    sigs = [_sig("OID-S1", code="600000.SH", action="SELL"),
            _sig("OID-B1", code="600000", action="BUY"),
            _sig("OID-B2", code="000001.SZ", action="BUY")]
    files = _enqueue(root, sigs)
    try:
        bridge.drain_queue()
        assert attempted == ["600000.SH", "000001.SZ"], "同标的买单不得尝试报单"
        assert files[1].exists() is False, "被拦的买单要消费掉, 否则 2 秒后照发(护栏失效)"
        details = [json.loads(p.read_text(encoding="utf-8"))["detail"]
                   for p in (root / "failed").iterdir()]
        assert any("SELL_NOT_ACCEPTED" in d for d in details)
    finally:
        _reset_bridge_state()


# ---------- C4: demo 桥 ----------

def test_demo_armed_stale_date_rejected(monkeypatch, tmp_path):
    """demo 桥 real 分支: 陈旧 .REAL_ARMED 不得永久放行(与 real 桥同口径)。"""
    demo = importlib.import_module("qmt.bridge.signal_bridge_demo")
    arm = tmp_path / ".REAL_ARMED"
    arm.write_text("20200101", encoding="utf-8")
    monkeypatch.setattr(demo, "ARM_FILE", str(arm))
    monkeypatch.setattr(demo, "ENVIRONMENT", "real")
    monkeypatch.setattr(demo, "DRY_RUN", False)
    ok, msg = demo._check_safety()
    assert ok is False
    assert "not dated today" in msg


def test_demo_armed_today_accepted(monkeypatch, tmp_path):
    """控制者裁决 #5: demo 桥 real 分支从"文档约束"升级为**执行约束** ——
    即便 arm 文件是今天的也拒绝启动(该桥无去重/无 paused), 并指向 real 桥。
    这条断言由本批翻转: 旧行为是"今天的 arm 即放行"。"""
    demo = importlib.import_module("qmt.bridge.signal_bridge_demo")
    arm = tmp_path / ".REAL_ARMED"
    arm.write_text(datetime.now().strftime("%Y%m%d"), encoding="utf-8")
    monkeypatch.setattr(demo, "ARM_FILE", str(arm))
    monkeypatch.setattr(demo, "ENVIRONMENT", "real")
    monkeypatch.setattr(demo, "DRY_RUN", False)
    ok, msg = demo._check_safety()
    assert ok is False
    assert "signal_bridge_real" in msg


def test_demo_real_refused_without_arm_file(monkeypatch, tmp_path):
    """未武装时同样拒绝启动(不因缺文件或日期新鲜而放行)。"""
    demo = importlib.import_module("qmt.bridge.signal_bridge_demo")
    monkeypatch.setattr(demo, "ARM_FILE", str(tmp_path / "nope.txt"))
    monkeypatch.setattr(demo, "ENVIRONMENT", "real")
    monkeypatch.setattr(demo, "DRY_RUN", False)
    ok, msg = demo._check_safety()
    assert ok is False
    assert "signal_bridge_real" in msg


def test_demo_declares_no_production_dedup(monkeypatch):
    """demo 桥明示不具生产去重能力(不得用于 real), 且 real 分支如实告警。"""
    demo = importlib.import_module("qmt.bridge.signal_bridge_demo")
    assert demo.HAS_DAILY_DEDUP is False


def test_demo_market_suffix_matches_real_bridge():
    """北交所口径必须与 real 桥/shared.common 一致(92/8/4 → .BJ)。"""
    demo = importlib.import_module("qmt.bridge.signal_bridge_demo")
    cases = {"920001": ".BJ", "830799": ".BJ", "430047": ".BJ",
             "600000": ".SH", "510300": ".SH", "900901": ".SH",
             "000001": ".SZ", "300750": ".SZ", "002594": ".SZ",
             "600000.SH": None}
    for code, suffix in cases.items():
        got = demo._with_market_suffix(code)
        want = code if suffix is None else code + suffix
        assert got == want, "%s → %s (期望 %s)" % (code, got, want)
        assert got == bridge._with_market_suffix(code), "与 real 桥口径不一致: %s" % code


# ---------- C5: live_check ----------

def _live_check_config_env(monkeypatch, tmp_path):
    lc = importlib.import_module("qmt.tools.live_check")
    monkeypatch.setattr(lc, "SIGNAL_ROOT", str(tmp_path))
    monkeypatch.setattr(lc, "PAUSE_FILE", str(tmp_path / "paused"))
    monkeypatch.setattr(lc, "ARMED_FILE", str(tmp_path / "real" / "armed.txt"))
    monkeypatch.setattr(lc, "DEDUP_FILE", str(tmp_path / "real" / "placed_today.json"))
    monkeypatch.setattr(lc, "STRATEGY_POINTER", str(tmp_path / "strategies" / ".active.json"))
    monkeypatch.setattr(lc, "BRIDGE", str(tmp_path / "signal_bridge_real.py"))
    (tmp_path / "signal_bridge_real.py").write_text(
        'DRY_RUN = False\nFIXED_ACCOUNT = ""\n', encoding="utf-8")
    lc._rows.clear()
    return lc


def test_live_check_dedup_row_counts_orders_not_stocks(monkeypatch, tmp_path):
    """去重键是 order_id(一笔一档) → 文案必须说"笔", 不能说"只"。"""
    lc = _live_check_config_env(monkeypatch, tmp_path)
    today = datetime.now().strftime("%Y%m%d")
    dedup = tmp_path / "real" / "placed_today.json"
    dedup.parent.mkdir(parents=True, exist_ok=True)
    dedup.write_text(json.dumps({today: ["OID-1", "OID-2", "OID-3"]}), encoding="utf-8")
    lc.check_config()
    rows = [r for r in lc._rows if r[2] == "当日去重账"]
    assert rows, "必须报告当日去重账"
    detail = rows[0][3]
    assert "3 笔" in detail and "order_id" in detail
    assert "只" not in detail


def test_live_check_does_not_leak_read_handles(monkeypatch, tmp_path):
    """live_check 的裸 open(...) 不 close → ResourceWarning(句柄靠 GC 兜底关闭,
    异常路径/非 CPython 下会占住 DEDUP_FILE, 让桥端原子写 os.replace 撞
    WinError 32)。这里逐条要求用 with 关闭。"""
    lc = _live_check_config_env(monkeypatch, tmp_path)
    today = datetime.now().strftime("%Y%m%d")
    dedup = tmp_path / "real" / "placed_today.json"
    dedup.parent.mkdir(parents=True, exist_ok=True)
    dedup.write_text(json.dumps({today: ["OID-1"]}), encoding="utf-8")
    (tmp_path / "real" / "armed.txt").write_text(today, encoding="utf-8")
    strategies = tmp_path / "strategies"
    strategies.mkdir(exist_ok=True)
    (strategies / ".active.json").write_text(json.dumps({"id": "s1"}), encoding="utf-8")
    (strategies / "s1.json").write_text(
        json.dumps({"execution": {"pct": 0.5, "top_n": 1, "open_window": 1,
                                  "pick_slot": 1}}), encoding="utf-8")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lc.check_config()
        gc.collect()
    leaks = [str(w.message) for w in caught
             if issubclass(w.category, ResourceWarning)]
    assert leaks == [], "存在未关闭的读句柄: %s" % leaks
    os.replace(str(dedup), str(tmp_path / "moved.json"))   # 句柄必须已释放


def test_live_check_pause_row_states_bridge_honors_it(monkeypatch, tmp_path):
    """paused 闸门文案必须说明桥端也读它(第一版只有发送侧读 → 文档失真)。"""
    lc = _live_check_config_env(monkeypatch, tmp_path)
    (tmp_path / "paused").write_text("stop", encoding="utf-8")
    lc.check_config()
    rows = [r for r in lc._rows if r[2] == "一键暂停开关"]
    assert rows
    assert "桥" in rows[0][3], "文案要点明桥端同样受 paused 约束"


# ---------- 控制者裁决 #1/#2: opt-in 加固(默认不改变现行为) ----------

def _guard_sig(**kw):
    sig = {"stock_code": "600000.SH", "action": "BUY", "price": 10.0, "volume": 100}
    sig.update(kw)
    return sig


def test_allowed_accounts_empty_allows_any(monkeypatch):
    """默认空名单 = 允许任意(QMT 当前登录账号) —— 不得改变今天的语义。"""
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    monkeypatch.setattr(bridge, "ALLOWED_ACCOUNTS", ())
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")
    ok, msg = bridge._call_passorder(_guard_sig())
    assert ok is True, msg
    assert "DRY_RUN" in msg


def test_allowed_accounts_mismatch_rejected(monkeypatch):
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    monkeypatch.setattr(bridge, "ALLOWED_ACCOUNTS", {"11111111"})
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")
    ok, msg = bridge._call_passorder(_guard_sig())
    assert ok is False
    assert "88869979" in msg and "11111111" in msg, "必须打印当前账号与名单: %s" % msg


def test_allowed_accounts_match_allowed(monkeypatch):
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    monkeypatch.setattr(bridge, "ALLOWED_ACCOUNTS", {"88869979"})
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")
    ok, msg = bridge._call_passorder(_guard_sig())
    assert ok is True, msg


def test_max_order_volume_default_unlimited(monkeypatch):
    """默认 0 = 不限制。"""
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    monkeypatch.setattr(bridge, "MAX_ORDER_VOLUME", 0)
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")
    ok, msg = bridge._call_passorder(_guard_sig(volume=99999999))
    assert ok is True, msg


def test_max_order_volume_rejected_when_exceeded(monkeypatch):
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    monkeypatch.setattr(bridge, "MAX_ORDER_VOLUME", 1000)
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")
    ok, msg = bridge._call_passorder(_guard_sig(volume=1100))
    assert ok is False
    assert "1100" in msg and "1000" in msg, "必须打印实际 volume 与上限: %s" % msg


def test_max_order_volume_allows_at_limit(monkeypatch):
    """边界: == 上限放行, 只有严格超限才拒。"""
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    monkeypatch.setattr(bridge, "MAX_ORDER_VOLUME", 1000)
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")
    ok, msg = bridge._call_passorder(_guard_sig(volume=1000))
    assert ok is True, msg
