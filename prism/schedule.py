# -*- coding: utf-8 -*-
"""调度共用件: 模拟盘守护(paper_daemon)与实盘守护(live_daemon)逐行重复的
时段判定/数据源连接/退避睡眠 + 时点常量。

边界(刻意): 只共享"什么时候做什么"的调度骨架与常量。账本语义、
模拟盘-实盘隔离、买入/卖出/排队判定全部留在各守护与账户模块内, 此处不碰。

交易日闸门(2026-09-19 加, 两档):
  1) **周末**(主判据, 零数据依赖): now.weekday() >= 5 → 非交易时段。
     现场实况: 2026-09-19(周六) QMT 照样推 tick —— 600900 lastPrice 31.10,
     而 09-18 真收盘 28.27(假价 +10%, 且 bid/ask 齐全)。
  2) **工作日节假日**: 查本地 prism/trading_calendar.json 的休市日清单
     (只列休市日, 由交易所次年休市安排生成, 一年更新一次)。

为什么不用"历史交易日索引"(zt 索引/xtdata.get_trading_dates)判今天:
  那类索引只有**已经产生过数据的日子**。今天的数据还不存在 ⇒ 索引对"今天"
  必然答"不是交易日" ⇒ 闸门会把守护**永久** CLOSED(比不做危险得多)。
  实测: xtdata.get_trading_dates('SH') 8729 条、末根 2026-09-18;
  查未来区间(20260919~20261012)→ [] —— 下周一 09-21 也只因"还没生成"而缺席。
  zt 索引另有第二处不可靠: 它只在有涨停时写日、且跳过每只股的首根K线
  (本机实测 413 天 vs K线并集 414 天, 缺 2025-01-07), 连"过去某天"都不完全
  可靠。故本判据**只做周末 + 本地休市日清单**。

失败方向(硬要求): 清单缺失/过期(年份不符)/坏 JSON/解析异常 → **只保留周末
闸门** + 一天一条 WARNING, **绝不**据此判 CLOSED。宁可漏判一个节假日
(paused/armed 人工闸门仍在兜底), 也不能因为一个 JSON 没更新就把系统锁死。
"""
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path

LOG = logging.getLogger("prism.schedule")

# 本地休市日清单(随仓发布; 测试/部署可整份替换或改指别处)
CALENDAR_PATH = Path(__file__).with_name("trading_calendar.json")

# 时点默认值 = 接线前的硬编码值, 现在只作**解析失败兜底**(见 execution_slots)。
# 为什么保持"模块级常量"而不是改成显式注入: paper_daemon/live_daemon 都在 import
# 时各自快照 schedule.PICK_SLOT / schedule.OPEN_WINDOW, 且两个守护文件本批不可改
# —— 常量语义是**一次覆盖全部调用点**的唯一形式, 不会留下"某个守护还读旧值"的暗坑。
# 代价(如实记录): 进程启动时定值, 运行中改 JSON 不生效(与改前的硬编码同款语义);
# 显式 --strategy 注入的实例策略不改本进程时点(改前也不会 —— 时点从来不是按实例的,
# 时点跟随**默认策略指针**)。
_DEFAULT_PICK_SLOT = "15:05"
_DEFAULT_OPEN_WINDOW = ("09:26", "09:35")


def _hhmm(s):
    """严格 "HH:MM" → 原串; "9:05"/"25:00"/非字符串 → None。

    严格是有意的: 交给守护的是**字符串比较**(hm >= PICK_SLOT),
    "9:05" 这种写法会让比较结果全错, 必须挡在解析层。
    """
    try:
        t = datetime.strptime(s, "%H:%M")
    except (TypeError, ValueError):
        return None
    return s if t.strftime("%H:%M") == s else None


def _open_window(s):
    """"09:26-09:35" → ("09:26", "09:35"); 缺 "-"/任一端非法/逆序 → None。

    逆序(如 "10:00-09:00")会让守护的 a <= hm <= b 恒 False(窗口静默失效),
    与缺键一样归为非法。多段("a-b-c")经末段非法自然挡下。
    """
    if not isinstance(s, str):
        return None
    a, _, b = s.partition("-")
    a, b = _hhmm(a), _hhmm(b)
    return (a, b) if a and b and a <= b else None


def execution_slots():
    """默认策略声明的 (收盘选股时点, 次日开盘买入窗口)。

    读取走**同一个 loader**(engine.resolve_strategy: 默认指针 → load_strategy,
    与守护/引擎/模拟盘同一份 JSON), 不另造 json.load 的第二真相源。

    失败方向(fail-closed, 硬要求): 策略不可读/缺键/格式不符 → **各键独立**回落
    硬编码默认值 + WARNING(绝不静默), 绝不解析出一个比声明更宽的窗口。
    """
    try:
        from prism import engine
        ex = (engine.resolve_strategy() or {}).get("execution") or {}
    except Exception as e:
        LOG.warning("策略 execution 块不可读(%r) → 时点用硬编码默认值 %s / %s-%s",
                    e, _DEFAULT_PICK_SLOT, *_DEFAULT_OPEN_WINDOW)
        ex = {}
    slot = _hhmm(ex.get("pick_slot"))
    if slot is None:
        LOG.warning("execution.pick_slot=%r 缺失/非法(需 HH:MM) → 用默认 %s",
                    ex.get("pick_slot"), _DEFAULT_PICK_SLOT)
        slot = _DEFAULT_PICK_SLOT
    win = _open_window(ex.get("open_window"))
    if win is None:
        LOG.warning("execution.open_window=%r 缺失/非法(需 HH:MM-HH:MM 且不逆序) "
                    "→ 用默认 %s-%s", ex.get("open_window"), *_DEFAULT_OPEN_WINDOW)
        win = _DEFAULT_OPEN_WINDOW
    return slot, win


# 模块常量在 import 时定值: live_daemon/paper_daemon 的 `PICK_SLOT = schedule.PICK_SLOT`
# 快照因此自动拿到策略声明值(改 JSON 生效), 兜底值与前完全一致。
PICK_SLOT, OPEN_WINDOW = execution_slots()
SETTLE_AFTER = "15:00"

# "一天一次"告警备忘: (日期, 判据) → 已说过就不再落日志。守护 POLL_SECONDS=5
# 一轮调一次 in_session, 不加这道闸告警会刷屏把别的告警挤出日志。
_last_note = None


def _note(day, reason, level, msg, *args):
    """同一(日期, 判据)只在**状态变化时**落一次日志(可观测性 + 不刷屏)。"""
    global _last_note
    if _last_note == (day, reason):
        return
    _last_note = (day, reason)
    LOG.log(level, msg, *args)


def _holidays(today, path=None):
    """休市日清单 → set[date]; 缺失/过期/坏文件 → **None**(未知, 调用方忽略)。

    只读本地 JSON(无网络/无实时依赖): {"generated_for": 2026,
    "holidays": ["2026-10-01", ...]}(只列工作日休市日)。
    失败方向一律 None + WARNING —— 由 in_session 退化成"仅周末闸门",
    绝不因为清单不可用而判"不能交易"。
    """
    p = Path(path) if path is not None else CALENDAR_PATH
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
        generated_for = int(doc["generated_for"])
        days = {date.fromisoformat(str(s)) for s in doc["holidays"]}
    except Exception as e:
        _note(today, "calendar-unusable", logging.WARNING,
              "休市日清单不可用(%s: %r) → 本次只用周末闸门(绝不锁死系统); "
              "更新办法: 按交易所次年休市安排重写该 JSON", p, e)
        return None
    if generated_for != today.year:
        _note(today, "calendar-stale", logging.WARNING,
              "休市日清单已过期(generated_for=%s, 今天 %s) → 本次只用周末闸门; "
              "请按交易所休市安排更新 %s", generated_for, today.year, p)
        return None
    return days


def in_session(now):
    """交易日时段: 交易日 ∧ 周一~五 ∧ (09:30-11:30 ∨ 13:00-15:00)。

    交易日判定两档(见模块 docstring): 周末 → 一律不在时段; 工作日 → 查本地
    休市日清单, 命中则不在时段(fail-closed, 一天一条 WARNING)。

    清单不可用(缺失/过期/坏) → **回落 weekday**(=改造前行为) + WARNING:
    冷缓存/新机器/忘更新 JSON 都不许把系统锁死。
    """
    day = now.date()
    if now.weekday() >= 5:                      # 周末: 先判, 省一次清单读
        _note(day, "weekend", logging.WARNING,
              "非交易日(周末: %s 周%d) → 本日一律不在交易时段",
              day, now.weekday() + 1)
        return False
    holidays = _holidays(day)
    if holidays and day in holidays:
        _note(day, "holiday", logging.WARNING,
              "非交易日(本地休市日清单 %s 列明 %s 休市) → "
              "本日一律不在交易时段", CALENDAR_PATH.name, day)
        return False
    if holidays:
        _note(day, "calendar-ok", logging.INFO,
              "交易日(休市日清单 %s: %d 条, %s 不在其中) → 按时段判据",
              CALENDAR_PATH.name, len(holidays), day)
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
