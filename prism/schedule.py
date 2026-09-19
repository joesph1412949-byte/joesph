# -*- coding: utf-8 -*-
"""调度共用件: 模拟盘守护(paper_daemon)与实盘守护(live_daemon)逐行重复的
时段判定/数据源连接/退避睡眠 + 时点常量。

边界(刻意): 只共享"什么时候做什么"的调度骨架与常量。账本语义、
模拟盘-实盘隔离、买入/卖出/排队判定全部留在各守护与账户模块内, 此处不碰。
"""
import logging
import time

from prism import zt_history

LOG = logging.getLogger("prism.schedule")

PICK_SLOT = "15:05"               # 收盘选股时点(策略 execution.pick_slot)
OPEN_WINDOW = ("09:26", "09:35")  # 次日开盘买入窗口(execution.open_window)
SETTLE_AFTER = "15:00"

# "一天一次"告警备忘: (日期, 判据) → 已说过就不再落日志。守护 POLL_SECONDS=5
# 一轮调一次 in_session, 不加这道闸告警会刷屏把别的告警挤出日志。
_last_note = None


def _note(day, reason, level, msg, *args):
    """同一(日期, 判据)只在**状态变化时**落一次日志(R5 可观测性, 不刷屏)。"""
    global _last_note
    if _last_note == (day, reason):
        return
    _last_note = (day, reason)
    LOG.log(level, msg, *args)


def in_session(now):
    """交易日时段: 交易日 ∧ 周一~五 ∧ (09:30-11:30 ∨ 13:00-15:00)。

    交易日闸门(R2, 2026-09-19 加): 先用 zt 按日索引(zt_history.is_trading_day)
    判 now.date() 是不是交易日 —— 判定非交易日 → 一律不在时段(等价
    SESSION_CLOSED, fail-closed) + 一天一条 WARNING。

    索引判不了(缺失/为空/未覆盖该日) → **回落 weekday**(改造前的行为),
    绝不据此判"不能交易": 冷缓存/新机器锁死系统比漏判一个节假日更糟, 而且
    paused/armed 人工闸门仍在。

    ⚠ 已知上限: 当日日K收盘后才落地 ⇒ 索引对"今天"通常还没覆盖(返回 None)
    ⇒ **工作日节假日的盘中仍按 weekday 放行**, 只有索引已覆盖的日期(历史日期、
    收盘后)才由索引说了算。要盘中挡住需另找同日可用的交易日历, 见
    zt_history.is_trading_day 的说明(需拍板)。
    """
    if now.weekday() >= 5:                      # 周末: 先判, 省一次索引读
        _note(now.date(), "weekend", logging.WARNING,
              "非交易日(周末: %s 周%d) → 本日一律不在交易时段",
              now.date(), now.weekday() + 1)
        return False
    day = now.date()
    verdict = zt_history.is_trading_day(day)
    if verdict is False:
        _note(day, "index-holiday", logging.WARNING,
              "非交易日(zt 索引判据: %s 不在索引的交易日里) → "
              "本日一律不在交易时段", day)
        return False
    if verdict is None:
        _note(day, "index-unavailable", logging.WARNING,
              "交易日判据不可用(zt 索引未覆盖 %s: 当日K线收盘后才落地/"
              "索引缺失) → 回落 weekday 判据(宁可放行也不锁死系统)", day)
    else:
        _note(day, "index-trading", logging.INFO,
              "交易日(zt 索引判据: %s 在册) → 按时段判据", day)
    hm = now.strftime("%H:%M")
    return ("09:30" <= hm <= "11:30") or ("13:00" <= hm <= "15:00")


def sleep(sec, sleep_fn=None):
    """退避等待: 注入 sleep_fn 优先(离线测试即时返回), 否则真睡。"""
    (sleep_fn or time.sleep)(sec)


def connect_provider(daemon, max_retry=60, retry_wait=10):
    """连接 QMT 行情数据源(重试到上限); 已是连接态 → 直接 True。

    单次失败只重试不抛(行情不可用是常态), 拿不到 → False 由调用方决定退出。
    """
    if daemon.provider is not None:
        return True
    from prism.data import DataProvider
    for _ in range(max_retry):
        try:
            p = DataProvider()
            p.connect()
            if p.connected:
                daemon.provider = p
                return True
        except Exception:
            pass
        sleep(retry_wait, daemon.sleep_fn)
    return False
