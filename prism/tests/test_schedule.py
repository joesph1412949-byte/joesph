# -*- coding: utf-8 -*-
"""schedule.in_session 的交易日闸门(2026-09-19 改版)。

需求与两条铁律(派单 + 控制者复核):
  R1 周末闸门(主判据, 零数据依赖): weekday >= 5 → 非交易时段, 离线可回归。
  R2 工作日节假日: 查本地 prism/trading_calendar.json 的**休市日**清单。
  R3 失败方向(硬要求): 清单缺失/过期/坏 JSON/年份不符 → **只保留周末闸门** +
     WARNING, **绝不**判 CLOSED —— 宁可漏判一个节假日(paused/armed 人工闸门兜底),
    也不能因为一个 JSON 没更新就把系统锁死。
  ❌ 不用 zt 索引 / xtdata.get_trading_dates / tick 交易日判"今天": 见
     tests 末条与本文件顶部说明 —— 历史索引只有"已产生数据的日子", 对今天
     必然答"非交易日", 会把守护**永久** CLOSED。

每条用例都注明判别力(它能抓到什么错)。清单类用例用 tmp JSON(离线), 不依赖
真实文件的年份; 另有一条用例钉住随仓发布的真实清单本身。
"""
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

from prism import schedule


@pytest.fixture(autouse=True)
def _reset_note_memo(monkeypatch):
    """"一天一次"告警备忘是模块级状态 → 每个用例前清零, 用例之间互不污染。"""
    monkeypatch.setattr(schedule, "_last_note", None, raising=False)


def _use_cal(monkeypatch, tmp_path, doc):
    """把清单路径指到 tmp JSON。doc 为 dict(序列化)或裸字符串(坏文件用)。"""
    p = tmp_path / "trading_calendar.json"
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc),
                 encoding="utf-8")
    monkeypatch.setattr(schedule, "CALENDAR_PATH", p, raising=False)
    return p


def _cal(holidays, generated_for=2026):
    return {"generated_for": generated_for, "holidays": list(holidays),
            "source": "test"}


def _warns(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING]


# ---------------- R1 周末闸门(主判据) ----------------

def test_weekend_closed_including_real_today():
    """判别力: 现场实况日 2026-09-19(周六) 与 09-20(周日) 的任何时刻都必须
    False —— 这条**不依赖任何数据文件**, 是闸门最稳的一档(离线也成立)。

    (周六 QMT 照样推 tick: 600900 lastPrice 31.10 vs 09-18 真收盘 28.27。)
    """
    for t in (datetime(2026, 9, 19, 9, 30), datetime(2026, 9, 19, 10, 0),
              datetime(2026, 9, 19, 14, 59), datetime(2026, 9, 20, 10, 0)):
        assert schedule.in_session(t) is False, t


def test_weekend_warning_says_weekend(caplog):
    """R2 可观测性: 非交易日的 WARNING 要说清是哪一档(周末), 且一天一次不刷屏。"""
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        for _ in range(5):
            assert schedule.in_session(datetime(2026, 9, 19, 10, 0)) is False
    assert len(_warns(caplog)) == 1
    assert "周末" in caplog.text and "2026-09-19" in caplog.text


# ---------------- R2 工作日节假日(清单) ----------------

def test_calendar_holiday_closes_session(monkeypatch, tmp_path, caplog):
    """判别力(本任务核心): 工作日节假日(2026-10-01 周四, 国庆) 10:00 必须 False。

    改造前只看 weekday ⇒ 判"周四盘中可交易" ⇒ 守护拿假价真报单。这条只有真读
    清单才会绿。
    """
    _use_cal(monkeypatch, tmp_path, _cal(["2026-10-01", "2026-09-25"]))
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.in_session(datetime(2026, 10, 1, 10, 0)) is False
    assert "非交易日" in caplog.text and "2026-10-01" in caplog.text


def test_calendar_trading_day_unaffected(monkeypatch, tmp_path):
    """判别力: 清单里没有的日子照常走时段判据(不许把正常交易日判死)。"""
    _use_cal(monkeypatch, tmp_path, _cal(["2026-10-01"]))
    assert schedule.in_session(datetime(2026, 9, 18, 10, 0)) is True    # 周五盘中
    assert schedule.in_session(datetime(2026, 9, 18, 9, 25)) is False   # 未开盘
    assert schedule.in_session(datetime(2026, 9, 18, 12, 0)) is False   # 午休
    assert schedule.in_session(datetime(2026, 9, 18, 15, 1)) is False   # 收盘后


# ---------------- R3 失败方向: 一律回落, 绝不 CLOSED ----------------

def test_missing_calendar_falls_back_to_weekday_only(monkeypatch, tmp_path,
                                                     caplog):
    """判别力(最容易做错的地方): 冷机器/没部署 JSON 时, **工作日节假日的 10:00
    必须仍然放行**(=改造前的 weekday 行为), 只留 WARNING。

    反面: 若"清单缺失 → 不能交易", 新机器上整个交易时段被锁死 —— 派单与控制者
    都明令禁止(锁死系统比漏判一个节假日更糟)。断言 in_session(...) is True 就是
    在钉死这个方向。
    """
    monkeypatch.setattr(schedule, "CALENDAR_PATH", tmp_path / "不存在.json",
                        raising=False)
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.in_session(datetime(2026, 10, 1, 10, 0)) is True   # 节假日也放行
    assert "不可用" in caplog.text and "周末" in caplog.text
    assert schedule.in_session(datetime(2026, 9, 19, 10, 0)) is False      # 周末照挡


def test_expired_calendar_falls_back_to_weekday_only(monkeypatch, tmp_path,
                                                     caplog):
    """判别力: 清单年份 != 今天年份(过期) ⇒ 视同不可用 → 回落 weekday + WARNING。
    过期清单里"今天不是休市日"这种结论**不可信**(新年度的假期根本没列)。"""
    _use_cal(monkeypatch, tmp_path, _cal(["2026-10-01"], generated_for=2025))
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.in_session(datetime(2026, 10, 1, 10, 0)) is True
    assert "过期" in caplog.text


def test_broken_calendar_falls_back_and_never_raises(monkeypatch, tmp_path,
                                                     caplog):
    """判别力: 坏 JSON / 缺键 / 日期格式错 都只能回落, 绝不抛异常
    (抛了就是 tick 崩溃)。"""
    for bad in ("{不是 json", "[]", '{"holidays": []}', '{"generated_for": 2026}',
                _cal(["2026-13-45"])):
        _use_cal(monkeypatch, tmp_path, bad)
        with caplog.at_level(logging.WARNING, logger="prism.schedule"):
            assert schedule.in_session(datetime(2026, 10, 1, 10, 0)) is True
        schedule._last_note = None
        assert "不可用" in caplog.text
        caplog.clear()


def test_unusable_calendar_warning_once_per_day(monkeypatch, tmp_path, caplog):
    """判别力: POLL_SECONDS=5 轮询下, 清单不可用的 WARNING 也只能一天一条。"""
    monkeypatch.setattr(schedule, "CALENDAR_PATH", tmp_path / "无.json",
                        raising=False)
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        for _ in range(5):
            assert schedule.in_session(datetime(2026, 9, 18, 10, 0)) is True
    assert len(_warns(caplog)) == 1


# ---------------- 随仓发布的真实清单(不许被删/改坏) ----------------

def test_shipped_calendar_lists_official_2026_holidays(caplog):
    """判别力: 真实 prism/trading_calendar.json 必须存在、可解析、年份对得上,
    且含 2026 中秋(09-25)与国庆(10-01..10-07)的工作日休市日; 全是工作日
    (周末由 weekday 闸门管, 清单里混周末说明生成口径错了)。

    同时钉两条真实日期的判定: 10-01(周四) False, 09-18(周五) True。
    """
    doc = json.loads(schedule.CALENDAR_PATH.read_text(encoding="utf-8"))
    assert doc["generated_for"] == 2026
    days = {date.fromisoformat(d) for d in doc["holidays"]}
    assert all(d.weekday() < 5 for d in days), "清单只该列工作日休市日"
    for d in ("2026-09-25", "2026-10-01", "2026-10-02", "2026-10-05",
              "2026-10-06", "2026-10-07", "2026-02-17", "2026-06-19"):
        assert date.fromisoformat(d) in days, d
    with caplog.at_level(logging.WARNING, logger="prism.schedule"):
        assert schedule.in_session(datetime(2026, 10, 1, 10, 0)) is False
        assert schedule.in_session(datetime(2026, 9, 18, 10, 0)) is True
        assert schedule.in_session(datetime(2026, 9, 19, 10, 0)) is False
    assert "2026-10-01" in caplog.text
