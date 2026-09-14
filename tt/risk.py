# -*- coding: utf-8 -*-
"""风控引擎(纯逻辑, 零 IO)。

每一道检查都独立、可单测, 聚合在 RiskGate 里串行执行。设计原则:

  - **fail-closed**: 任何输入缺失/异常 → 拒绝该笔, 而不是放行。
    风控失效的代价是真实资金损失, 远比"少做一次T"严重。
  - **先硬后软**: 时段/涨跌停/金额/次数/亏损 是硬闸门; 偏离/滑点 是软闸门。
  - **单一出口**: 上层只消费 Verdict, 不自己拼装判定, 避免规则漂移。

Verdict.ok=False 时, code 标明是哪一道拦下的, 便于网页告警与日志归因。
"""
from collections import namedtuple

try:                                    # 与 exit_rules.py 同款: 保持可独立导入
    from shared.common import limit_ratio_for_code
except Exception:                       # pragma: no cover - 兜底
    def limit_ratio_for_code(code):
        c = str(code).strip()
        if c.startswith(("8", "4", "92")):
            return 0.30
        if c.startswith(("300", "301", "688")):
            return 0.20
        return 0.10

Verdict = namedtuple("Verdict", ["ok", "code", "msg"])

OK = Verdict(True, "OK", "")

# 时段阶段
PHASE_OPEN = "OPEN"          # 正常做T
PHASE_CONVERGE = "CONVERGE"  # 只允许归位(把卖掉的买回来)
PHASE_CLOSED = "CLOSED"      # 全停

PRICE_SANITY_MAX = 100000.0  # 防乌龙指(与桥端 MAX_ORDER_PRICE 同口径)


def _reject(code, msg):
    return Verdict(False, code, msg)


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 时段

def session_phase(hhmm, session_cfg):
    """当前时刻 → 交易阶段。参数非法 → CLOSED(fail-closed)。"""
    s = str(hhmm or "").strip()
    cfg = session_cfg or {}
    start = str(cfg.get("open_start") or "")
    end = str(cfg.get("open_end") or "")
    converge = str(cfg.get("converge_after") or "")
    hard = str(cfg.get("hard_stop_after") or "")
    if not (s and start and end):
        return PHASE_CLOSED
    if len(s) == 5 and ":" in s:
        s = s[:5]
    else:
        return PHASE_CLOSED
    if s < start or s >= hard or s >= end:
        return PHASE_CLOSED
    if converge and s >= converge:
        return PHASE_CONVERGE
    return PHASE_OPEN


def check_session(hhmm, session_cfg, side, sold_today=0, bought_today=0):
    """时段闸门。CONVERGE 阶段只放行"归位买入"。"""
    phase = session_phase(hhmm, session_cfg)
    if phase == PHASE_CLOSED:
        return _reject("SESSION_CLOSED",
                       "非交易时段/已过硬停时点 %s" % hhmm)
    if phase == PHASE_CONVERGE:
        if side == "BUY" and int(bought_today) < int(sold_today):
            return OK                      # 归位买入放行
        return _reject("SESSION_CONVERGE",
                       "收敛时段(%s)只允许归位买入, 当前 %s" % (hhmm, side))
    return OK


# ---------------------------------------------------------------- 价格

def check_price_sanity(price, max_price=PRICE_SANITY_MAX):
    p = _f(price)
    if p is None or p <= 0:
        return _reject("PRICE_INVALID", "委托价非法: %r" % price)
    if p > max_price:
        return _reject("PRICE_TOO_HIGH", "委托价 %.2f 超过上限 %.0f" % (p, max_price))
    return OK


def check_price_deviation(price, ref, max_dev_pct):
    """委托价相对参考价的偏离(防"挂错一档"级别的乌龙)。"""
    p, r = _f(price), _f(ref)
    m = _f(max_dev_pct)
    if p is None or r is None or r <= 0 or m is None:
        return _reject("DEVIATION_UNKNOWN", "偏离校验参数缺失")
    dev = abs(p / r - 1)
    if dev > m:
        return _reject("DEVIATION_TOO_BIG",
                       "委托价 %.3f 偏离参考价 %.3f 达 %.2f%% (> %.2f%%)"
                       % (p, r, dev * 100, m * 100))
    return OK


def check_limit_band(price, last_close, code):
    """委托价须落在当日涨跌停价之内(超出必被柜台拒绝, 提前拦下更干净)。"""
    p, lc = _f(price), _f(last_close)
    if p is None or lc is None or lc <= 0:
        return _reject("BAND_UNKNOWN", "涨跌停校验缺昨收/委托价")
    ratio = limit_ratio_for_code(code)
    up, dn = lc * (1 + ratio), lc * (1 - ratio)
    if p > up + 1e-6 or p < dn - 1e-6:
        return _reject("BAND_OUT",
                       "委托价 %.3f 超出涨跌停 [%.3f, %.3f]" % (p, dn, up))
    return OK


def check_slippage(price, ref, max_slippage_pct):
    """软闸门: 委托价相对"应该挂的价"的滑点上限。"""
    p, r = _f(price), _f(ref)
    m = _f(max_slippage_pct)
    if m is None or m <= 0:
        return OK
    if p is None or r is None or r <= 0:
        return _reject("SLIPPAGE_UNKNOWN", "滑点校验参数缺失")
    if abs(p / r - 1) > m:
        return _reject("SLIPPAGE_TOO_BIG",
                       "滑点 %.2f%% 超上限 %.2f%%" % (abs(p / r - 1) * 100, m * 100))
    return OK


# ---------------------------------------------------------------- 规模

def check_lot(volume, lot=100):
    v = _f(volume)
    if v is None or v <= 0:
        return _reject("VOLUME_INVALID", "数量非法: %r" % volume)
    if int(v) % int(lot) != 0:
        return _reject("VOLUME_NOT_LOT", "%d 股不是 %d 的整数倍" % (v, lot))
    return OK


def check_order_amount(price, volume, max_amount):
    p, v, m = _f(price), _f(volume), _f(max_amount)
    if p is None or v is None:
        return _reject("AMOUNT_UNKNOWN", "金额校验参数缺失")
    amt = p * v
    if m is not None and amt > m + 1e-6:
        return _reject("AMOUNT_TOO_BIG",
                       "单笔金额 %.0f 元超过上限 %.0f 元" % (amt, m))
    return OK


def check_position_value(order_amount, held_value, total_asset, max_position_pct):
    """单标的市值上限(含本单)。任一输入缺失 → 放行(由上层其他闸门兜底)。"""
    t, m = _f(total_asset), _f(max_position_pct)
    if t is None or t <= 0 or m is None or m <= 0:
        return OK
    after = (_f(held_value, 0.0) or 0.0) + (_f(order_amount, 0.0) or 0.0)
    cap = t * m
    if after > cap + 1e-6:
        return _reject("POSITION_CAP",
                       "该标的市值 %.0f 将超上限 %.0f (总资产%.0f×%.0f%%)"
                       % (after, cap, t, m * 100))
    return OK


def check_sellable(side, volume, can_use_volume):
    """卖出量硬约束在券商可卖量之内 —— T+1 的最终合规闸门。"""
    if side != "SELL":
        return OK
    v, avail = _f(volume), _f(can_use_volume)
    if avail is None:
        return _reject("SELLABLE_UNKNOWN", "拿不到券商可卖量, 拒绝卖出")
    if v is None or v > avail:
        return _reject("SELLABLE_INSUFFICIENT",
                       "卖出 %s 股 > 可卖 %s 股(T+1/冻结)" % (volume, avail))
    return OK


# ---------------------------------------------------------------- 频次/亏损

def check_daily_trades(count, max_count):
    c, m = _f(count, 0), _f(max_count)
    if m is None:
        return OK
    if int(c) >= int(m):
        return _reject("DAILY_TRADES_CAP",
                       "当日已成交 %d 次, 达上限 %d" % (int(c), int(m)))
    return OK


def check_daily_loss(realized_pnl, max_loss):
    """已实现亏损熔断。realized_pnl 为负数表示亏损。"""
    p, m = _f(realized_pnl, 0.0), _f(max_loss)
    if m is None or m <= 0:
        return OK
    if p is not None and p <= -abs(m):
        return _reject("DAILY_LOSS_CAP",
                       "当日已实现 %.0f 元, 触及亏损上限 -%.0f 元" % (p, abs(m)))
    return OK


def check_net_exposure(side, volume, sold_today, bought_today,
                       max_net_buy_qty=0):
    """日内净敞口闸门。

    定义: 净买入 = 当日买入 - 当日卖出。
    max_net_buy_qty=0 → 严格归位(买了多少必须卖掉多少), 日内净持仓只减不增。
    买入方向校验上限; 卖出方向天然减少净敞口, 直接放行。
    """
    if side != "BUY":
        return OK
    v = _f(volume, 0.0) or 0.0
    net_after = (float(bought_today) + v) - float(sold_today)
    if net_after > float(max_net_buy_qty) + 1e-9:
        return _reject("NET_EXPOSURE",
                       "买入后日内净敞口 %d 股 > 上限 %d 股"
                       % (net_after, max_net_buy_qty))
    return OK


# ---------------------------------------------------------------- 熔断

class CircuitBreaker:
    """连续失败熔断。任何一次成功即清零。

    用于行情/账户/落盘连续异常时自动停机, 避免"坏状态下的连续错误下单"。
    """

    def __init__(self, max_failures=3):
        self.max_failures = int(max_failures or 3)
        self.count = 0
        self.tripped = False
        self.reason = ""

    def record_success(self):
        self.count = 0
        self.tripped = False
        self.reason = ""

    def record_failure(self, reason=""):
        self.count += 1
        if self.count >= self.max_failures:
            self.tripped = True
            self.reason = "连续 %d 次失败: %s" % (self.count, reason)
        return self.tripped

    def check(self):
        if self.tripped:
            return _reject("CIRCUIT_BREAKER", self.reason or "熔断已触发")
        return OK

    def reset(self):
        self.record_success()


# ---------------------------------------------------------------- 聚合

class RiskGate:
    """把上面的单项检查串成一次完整预交易校验。

    运行时状态(账本数字/账户快照)由调用方通过 ctx 传入, 本类不持有 IO。
    """

    def __init__(self, risk_cfg, breaker=None):
        self.cfg = dict(risk_cfg or {})
        self.breaker = breaker or CircuitBreaker(
            self.cfg.get("max_consecutive_failures", 3))

    def check(self, side, code, price, volume, ref_price, ladder_price_ref,
              last_close, hhmm, session_cfg, lot=100,
              sold_today=0, bought_today=0, can_use_volume=None,
              held_value=0.0, total_asset=None, daily_trades=0,
              realized_pnl=0.0, max_net_buy_qty=None, enabled=True):
        """返回 Verdict。短路在第一个不通过项。"""
        cfg = self.cfg

        # 0) 熔断 / 标的启用
        v = self.breaker.check()
        if not v.ok:
            return v
        if not enabled:
            return _reject("SYMBOL_DISABLED", "该标的未启用做T")

        # 1) 时段
        v = check_session(hhmm, session_cfg, side, sold_today, bought_today)
        if not v.ok:
            return v

        # 2) 价格合法性(由松到紧)
        for chk in (
            lambda: check_price_sanity(price),
            lambda: check_lot(volume, lot),
            lambda: check_limit_band(price, last_close, code),
            lambda: check_price_deviation(price, ref_price,
                                          cfg.get("max_price_deviation_pct")),
            lambda: check_slippage(price, ladder_price_ref,
                                   cfg.get("max_slippage_pct")),
        ):
            v = chk()
            if not v.ok:
                return v

        # 3) 规模
        amount = (float(price) * float(volume))
        v = check_order_amount(price, volume,
                               cfg.get("max_single_order_amount"))
        if not v.ok:
            return v
        v = check_position_value(amount, held_value, total_asset,
                                 cfg.get("max_position_pct"))
        if not v.ok:
            return v

        # 4) 可卖量(T+1)
        v = check_sellable(side, volume, can_use_volume)
        if not v.ok:
            return v

        # 5) 频次与亏损
        v = check_daily_trades(daily_trades, cfg.get("max_daily_trades"))
        if not v.ok:
            return v
        v = check_daily_loss(realized_pnl, cfg.get("max_daily_loss"))
        if not v.ok:
            return v

        # 6) 净敞口(默认严格归位)
        if max_net_buy_qty is None:
            max_net_buy_qty = 0
        v = check_net_exposure(side, volume, sold_today, bought_today,
                               max_net_buy_qty)
        if not v.ok:
            return v

        return OK
