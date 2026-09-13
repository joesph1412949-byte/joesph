# -*- coding: utf-8 -*-
"""卖出策略: 止盈 / 止损 / T+N 强制平仓 规则引擎(纯逻辑, 无 IO)。

与 strategy_close_pick / prism.live_daemon 配合: 买入时把成交记录写入持仓
档案(positions.json), 每日(或盘中)拉最新行情, 按规则判定哪些持仓要卖,
生成 SELL 信号走同一信号文件通道(桥已支持 SELL)。

规则(全部可配置):
  - 止盈: 现价 >= 买入价 * (1 + take_profit_pct)  → SELL
  - 止损: 现价 <= 买入价 * (1 - stop_loss_pct)    → SELL
  - 持有期: 买入日起满 max_hold_days 个自然日     → SELL(强制平仓)
  - 优先级: 止损 > 止盈 > 持有期(先触发哪个卖哪个, 同一天多规则命中取止损)
  - T+1(实盘启用, 默认关闭): 当日买入当日不可卖
  - 跌停顺延(见 is_limit_down): 跌停价上卖不出, 调用方跳过当日判定

价格语义: A股按"现价"判断; 委托价默认市价(price=0), 桥端按对手价成交。
"""
from datetime import date, datetime


def _as_date(v):
    """宽松日期归一化(date/datetime/YYYY-MM-DD 字符串) → date; 坏值 → None。"""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def limit_ratio(code):
    """涨跌停幅度: 北交所 30% / 创业板·科创板 20% / 其余 10%。

    与 common.limit_ratio_for_code 同口径; 此处内联以保持 exit_rules
    零依赖(它被 backtest 与根目录 tests 直接 import, 不能假定 common 在
    sys.path 上)。"""
    c = str(code).strip()
    if c.startswith(("92", "8", "4")):
        return 0.30
    if c.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


def is_limit_down(code, last_price, last_close):
    """跌停判定(卖出顺延): 现价 <= 昨收 × (1 - 幅度) + 0.001。

    缺昨收/现价 → False(fail-open, 照常判定卖出; 宁可挂单被拒也不错失止损)。
    与 prism/paper.py sell_check 的判定同款(paper 侧只分 20/10 两档)。"""
    try:
        last_price = float(last_price or 0)
        last_close = float(last_close or 0)
    except (TypeError, ValueError):
        return False
    if last_price <= 0 or last_close <= 0:
        return False
    return last_price <= last_close * (1 - limit_ratio(code)) + 0.001


class ExitRule:
    """单只持仓的卖出判定。"""

    def __init__(self, code, name, buy_price, buy_date,
                 take_profit_pct=0.08, stop_loss_pct=0.05, max_hold_days=5,
                 today=None, enforce_t1=False):
        self.code = code
        self.name = name or code
        self.buy_price = float(buy_price or 0)
        self.buy_date = _as_date(buy_date) or (today or date.today())
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold_days = max_hold_days
        self.today = _as_date(today) or date.today()
        # T+1: 实盘路径开启(当日买入不可卖); 回测/旧路径默认关闭保持原语义
        self.enforce_t1 = enforce_t1

    # ---------------- 判定 ----------------
    def evaluate(self, last_price):
        """按最新价判定卖出动作。返回 (action, reason) 或 (None, None)。
        action: "SELL"; reason 含规则名供日志/信号。"""
        if not last_price or last_price <= 0 or self.buy_price <= 0:
            return None, None
        # T+1: 当日买入的仓位今日不可卖(A股规则)
        if self.enforce_t1 and self.today <= self.buy_date:
            return None, None
        # 止损优先
        if last_price <= self.buy_price * (1 - self.stop_loss_pct):
            return "SELL", ("止损: 现价%.2f <= 买入价%.2f×(1-%.0f%%)"
                            % (last_price, self.buy_price,
                               self.stop_loss_pct * 100))
        # 止盈
        if last_price >= self.buy_price * (1 + self.take_profit_pct):
            return "SELL", ("止盈: 现价%.2f >= 买入价%.2f×(1+%.0f%%)"
                            % (last_price, self.buy_price,
                               self.take_profit_pct * 100))
        # 持有期强制
        hold_days = (self.today - self.buy_date).days
        if hold_days >= self.max_hold_days:
            return "SELL", ("持有期满: %d天 >= %d天" % (hold_days,
                                                       self.max_hold_days))
        return None, None

    def exit_reason_preview(self, last_price):
        """无动作时返回距触发还差多少, 供界面/日志提示。"""
        action, reason = self.evaluate(last_price)
        if action:
            return reason
        if self.buy_price <= 0 or not last_price:
            return "数据不足"
        up_gap = (self.buy_price * (1 + self.take_profit_pct) - last_price) / \
                 (self.buy_price * self.take_profit_pct) if self.take_profit_pct else 0
        dn_gap = (last_price - self.buy_price * (1 - self.stop_loss_pct)) / \
                 (self.buy_price * self.stop_loss_pct) if self.stop_loss_pct else 0
        return "距止盈还需+%.1f%% / 距止损还有%.1f%%缓冲" % (
            max(up_gap, 0) * 100, max(dn_gap, 0) * 100)


# ---------------- 持仓档案(纯内存模型, IO 由调用方负责) ----------------

class PositionBook:
    """持仓档案: {code: {code, name, buy_price, buy_date, volume, ...}}。

    序列化为 JSON 存盘(调用方负责读写), 本类只做增删改查与判定。
    实盘路径额外承载 can_use_volume(券商当日可卖量, 供 T+1 双保险)。"""

    def __init__(self, positions=None):
        self.positions = positions or {}

    def add(self, code, name, buy_price, buy_date, volume=0, **extra):
        pos = {
            "code": code, "name": name or code,
            "buy_price": float(buy_price), "buy_date": str(buy_date),
            "volume": int(volume or 0),
        }
        # 扩展字段(如 can_use_volume)按需并入; None 不落盘, 避免 JSON 噪声
        for k, v in extra.items():
            if v is not None:
                pos[k] = v
        self.positions[code] = pos

    def update(self, code, **fields):
        """就地更新已存在持仓的字段(如券商回写 can_use_volume)。"""
        pos = self.positions.get(code)
        if pos is None:
            return False
        for k, v in fields.items():
            if v is not None:
                pos[k] = v
        return True

    def remove(self, code):
        self.positions.pop(code, None)

    def get(self, code):
        return self.positions.get(code)

    def all(self):
        return dict(self.positions)

    def evaluate_all(self, last_prices, can_use=None, enforce_t1=False,
                     today=None, **rule_kwargs):
        """对全部持仓跑卖出规则。

        last_prices: {code: 最新价}。
        can_use: {code: 券商当日可卖量}; 传 None 时不按可卖量过滤(旧语义)。
                 提供时 ≤0 的持仓跳过 —— T+1/冻结/已挂单的实盘双保险。
        enforce_t1: 透传给 ExitRule(当日买入不卖)。
        today: 判定基准日(默认 date.today()); 实盘守护传入自己的时钟日,
               避免注入时钟与墙钟不一致时 T+1/持有期误判。
        返回 [(code, position, action, reason), ...]"""
        out = []
        for code, pos in self.positions.items():
            if can_use is not None:
                avail = can_use.get(code)
                if avail is None:
                    avail = pos.get("can_use_volume")
                if avail is not None and int(avail) <= 0:
                    continue
            last = (last_prices or {}).get(code)
            if not last:
                continue
            buy_date = _as_date(pos.get("buy_date")) or date.today()
            rule = ExitRule(code, pos.get("name"), pos["buy_price"], buy_date,
                            today=today, enforce_t1=enforce_t1, **rule_kwargs)
            action, reason = rule.evaluate(last)
            if action:
                out.append((code, pos, action, reason))
        return out

    @classmethod
    def from_json(cls, data):
        if not isinstance(data, dict):
            return cls()
        return cls({k: dict(v) for k, v in data.items()})
