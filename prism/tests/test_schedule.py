# -*- coding: utf-8 -*-
"""schedule.in_session 的交易日闸门(R2/R3/R5, 2026-09-19)。

为什么需要它(现场实测): 2026-09-19(周六) QMT 仍在推 tick, 600900 lastPrice 31.10
而 09-18 真收盘 28.27(假价 +10%), bid/ask 齐全 ⇒ paused/armed 一放行, 守护就
拿假价真报单。此前的时段判据只看 HH:MM + weekday, 对**工作日节假日**无感。

R3 三档各一条用例, 每条注明判别力(它能抓到什么错)。索引判据一律 monkeypatch
zt_history._load_index(离线, 不读真实 pkl); 真实索引另有一条 skip 对照用例。

⚠ 已知上限(判据物理约束, 见 zt_history.is_trading_day): 当日日K收盘后才落地,
索引对"今天"通常还没覆盖 ⇒ 盘中由 weekday 回落说了算, **工作日节假日的盘中
仍会放行**。要盘中挡住必须另找同日可用的交易日历(需拍板)。
"""
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism import schedule, zt_history


@pytest.fixture(autouse=True)
def _reset_note_memo(monkeypatch):
    """"一天一次"告警备忘是模块级状态 → 每个用例前清零, 用例之间互不污染。"""
    monkeypatch.setattr(schedule, "_last_note", None, raising=False)


def _index(monkeypatch, days):
    """把 _load_index 换成固定索引 {日期串: [条目]}(离线, 不碰真实 pkl)。"""
    monkeypatch.setattr(zt_history, "_load_index",
                        lambda: {d: [{"code": "600000.SH", "boards": 1}]
                                 for d in days})


def _warns(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------- R3 ①索引可用 ∧ 在册 → 是交易日(照常放行) ----------------

def test_index_confirms_trading_day(monkeypatch, caplog):
    """判别力: 闸门不得把**在册交易日**判死 —— 判死就是每个交易日全时段锁死
    (R3: 比漏判一个节假日更糟)。

    反例: 把"索引缺失/读到别处/日期格式错位"错当非交易日, 或把 None 当 False,
    这条立刻红。
    """
    _index(monkeypatch, ["2026-09-17", "2026-09-18"])
    with caplog.at_level(logging.INFO, logger="prism.schedule"):
        assert schedule.in_session(datetime(2026, 9, 18, 10, 0)) is True
        assert zt_history.is_trading_day(date(2026, 9, 18)) is True
    assert not _warns(caplog), "确认是交易日时不该有 WARNING"
    assert "索引" in caplog.text, "R5: 判定依据(索引)要落在日志里"


# ---------------- R3 ②索引可用 ∧ 明确不在册 → fail-closed ----------------

def test_index_says_weekday_holiday_closes_session(monkeypatch, caplog):
    """判别力(本任务核心): 2026 春节 02-16~02-20 是**工作日**假期。

    索引里 02-13(节前最后交易日)与 02-24(节后首日)都在册, 02-17 不在册: 改造前
    按 weekday 判"周三盘中 → 可交易", 带闸门后必须 False。10:00 落在时段窗口
    正中 ⇒ 唯一能给出 False 的就是这道闸门, 只有真去读索引才会红。
    """
    _index(monkeypatch, ["2026-02-13", "2026-02-24"])
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.in_session(datetime(2026, 2, 17, 10, 0)) is False
    assert "非交易日" in caplog.text and "索引" in caplog.text
    assert "2026-02-17" in caplog.text


# ---------------- R3 ③索引不可用/未覆盖该日 → weekday 回落, 绝不锁死 ----------------

def test_missing_index_falls_back_to_weekday(monkeypatch, caplog):
    """判别力: 冷缓存/新机器(索引缺失/为空) → 行为**逐位等于改造前**(纯 weekday),
    并打 WARNING 说明回落。

    反面: 若把"索引缺失"当"非交易日", 全时段锁死系统 —— R3 明令这比漏判一个
    节假日更糟(paused/armed 人工闸门仍在)。
    """
    monkeypatch.setattr(zt_history, "_load_index", lambda: {})
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.in_session(datetime(2026, 9, 18, 10, 0)) is True   # 周五盘中
        assert schedule.in_session(datetime(2026, 9, 19, 10, 0)) is False  # 周六
    assert "回落" in caplog.text
    assert zt_history.is_trading_day(date(2026, 9, 18)) is None


def test_index_not_covering_day_falls_back_to_weekday(monkeypatch, caplog):
    """判别力: 索引末根远早于查询日(R3 的"明显陈旧": 2025-03-03 vs 2026-09-18)时,
    "该日不在册"**不是**判据 —— 索引只是还没覆盖它 → 必须回落 weekday。

    这条同时钉死最容易做错的方向: 不许用 "day > 末根 → 非交易日" 判死, 因为当日
    K线收盘后才落地, 盘中索引**永远**还没覆盖今天 ⇒ 那会把每一个交易日都判死。
    """
    _index(monkeypatch, ["2025-03-03", "2025-03-04"])
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.in_session(datetime(2026, 9, 18, 10, 0)) is True
    assert "回落" in caplog.text
    assert zt_history.is_trading_day(date(2026, 9, 18)) is None


# ---------------- R2/R5: 告警不刷屏 + 依据可辨 ----------------

def test_holiday_warning_at_most_once_per_day(monkeypatch, caplog):
    """判别力: 守护 POLL_SECONDS=5 一轮调一次 → 同一天重复调用只许打一条 WARNING
    (刷屏会把真正的告警挤出日志)。"""
    _index(monkeypatch, ["2026-02-13", "2026-02-24"])
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        for _ in range(5):
            assert schedule.in_session(datetime(2026, 2, 17, 10, 0)) is False
    assert len(_warns(caplog)) == 1, [r.message for r in _warns(caplog)]


def test_weekend_warning_is_labeled_as_weekend(monkeypatch, caplog):
    """R5: 周末自己也打一条"非交易日"说明(一天一次), 且与"索引判据"可区分。"""
    _index(monkeypatch, ["2026-09-18"])
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.in_session(datetime(2026, 9, 19, 10, 0)) is False   # 周六
        assert schedule.in_session(datetime(2026, 9, 19, 14, 0)) is False
    assert len(_warns(caplog)) == 1
    assert "周末" in caplog.text and "2026-09-19" in caplog.text


# ---------------- 零回归: 时段窗口/周末语义不变 ----------------

def test_time_window_and_weekend_unchanged(monkeypatch):
    """判别力: 闸门只新增"非交易日"一档, 不许动既有时段语义(09:30-11:30 ∨
    13:00-15:00 边界 + 周末), 与 test_paper_daemon.test_in_session 同口径。"""
    _index(monkeypatch, ["2026-09-02", "2026-09-17", "2026-09-18"])
    assert schedule.in_session(datetime(2026, 9, 2, 10, 0)) is True     # 周三盘中
    assert schedule.in_session(datetime(2026, 9, 2, 9, 25)) is False    # 未开盘
    assert schedule.in_session(datetime(2026, 9, 2, 9, 30)) is True     # 上午开边
    assert schedule.in_session(datetime(2026, 9, 2, 11, 30)) is True    # 上午收边
    assert schedule.in_session(datetime(2026, 9, 2, 11, 31)) is False   # 午休
    assert schedule.in_session(datetime(2026, 9, 2, 13, 0)) is True     # 下午开边
    assert schedule.in_session(datetime(2026, 9, 2, 15, 0)) is True     # 下午收边
    assert schedule.in_session(datetime(2026, 9, 2, 15, 1)) is False    # 收盘后
    assert schedule.in_session(datetime(2026, 9, 5, 10, 0)) is False    # 周六
    assert schedule.in_session(datetime(2026, 9, 6, 10, 0)) is False    # 周日


# ---------------- 真实索引对照(本机有 runtime/cache 索引时才跑) ----------------

def test_real_local_index_marks_spring_festival_weekdays(caplog):
    """真实数据对照: 读本机 runtime/cache 的 zt 按日索引(413 天, 末根 2026-09-18)。

    - 2026-02-17(春节工作日) 不在册且被更晚的 02-24 覆盖 → 闸门判非交易日;
    - 2026-09-18(周五) 在册 → 照常放行;
    - 2026-09-19(今天, 周六) → 非交易日。
    冷机器(索引缺失/范围不覆盖 2026-02) → skip, 不伪装成通过。
    """
    idx = zt_history.trading_days()
    if not idx or date(2026, 2, 24) not in idx or date(2026, 9, 18) not in idx:
        pytest.skip("本机 zt 索引缺失或未覆盖 2026-02/09 → 跳过真实数据对照")
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.in_session(datetime(2026, 2, 17, 10, 0)) is False
        assert "非交易日" in caplog.text
        assert schedule.in_session(datetime(2026, 9, 18, 10, 0)) is True
        assert schedule.in_session(datetime(2026, 9, 19, 10, 0)) is False
