# -*- coding: utf-8 -*-
"""调度共用件: 模拟盘守护(paper_daemon)与实盘守护(live_daemon)逐行重复的
时段判定/数据源连接/退避睡眠 + 时点常量。

边界(刻意): 只共享"什么时候做什么"的调度骨架与常量。账本语义、
模拟盘-实盘隔离、买入/卖出/排队判定全部留在各守护与账户模块内, 此处不碰。
"""
import time

PICK_SLOT = "15:05"               # 收盘选股时点(策略 execution.pick_slot)
OPEN_WINDOW = ("09:26", "09:35")  # 次日开盘买入窗口(execution.open_window)
SETTLE_AFTER = "15:00"


def in_session(now):
    """交易日时段: 周一~五 ∧ (09:30-11:30 ∨ 13:00-15:00)。"""
    if now.weekday() >= 5:
        return False
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
