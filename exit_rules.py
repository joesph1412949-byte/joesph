# -*- coding: utf-8 -*-
"""卖出策略: 止盈 / 止损 / T+N 强制平仓 规则引擎(纯逻辑, 无 IO)。

与 strategy_close_pick 配合: 买入时把成交记录写入持仓档案(positions.json),
每日(或盘中)运行 exit 命令拉最新行情, 按规则判定哪些持仓要卖,
生成 SELL 信号走同一信号文件通道(桥已支持 SELL)。

规则(全部可配置):
  - 止盈: 现价 >= 买入价 * (1 + take_profit_pct)  → SELL
  - 止损: 现价 <= 买入价 * (1 - stop_loss_pct)    → SELL
  - 持有期: 买入日起满 max_hold_days 个自然日     → SELL(强制平仓)
  - 优先级: 止损 > 止盈 > 持有期(先触发哪个卖哪个, 同一天多规则命中取止损)

价格语义: A股按"现价"判断; 委托价默认市价(price=0), 桥端按对手价成交。
"""
from datetime import date, datetime


class ExitRule:
    """单只持仓的卖出判定。"""

    def __init__(self, code, name, buy_price, buy_date,
                 take_profit_pct=0.08, stop_loss_pct=0.05, max_hold_days=5,
                 today=None):
        self.code = code
        self.name = name or code
        self.buy_price = float(buy_price or 0)
        self.buy_date = buy_date
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        self.max_hold_days = max_hold_days
        self.today = today or date.today()

    # ---------------- 判定 ----------------
    def evaluate(self, last_price):
        """按最新价判定卖出动作。返回 (action, reason) 或 (None, None)。
        action: "SELL"; reason 含规则名供日志/信号。"""
        if not last_price or last_price <= 0 or self.buy_price <= 0:
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
    """持仓档案: {code: {code, name, buy_price, buy_date, volume}}。

    序列化为 JSON 存盘(调用方负责读写), 本类只做增删改查与判定。"""

    def __init__(self, positions=None):
        self.positions = positions or {}

    def add(self, code, name, buy_price, buy_date, volume=0):
        self.positions[code] = {
            "code": code, "name": name or code,
            "buy_price": float(buy_price), "buy_date": str(buy_date),
            "volume": int(volume or 0),
        }

    def remove(self, code):
        self.positions.pop(code, None)

    def get(self, code):
        return self.positions.get(code)

    def all(self):
        return dict(self.positions)

    def evaluate_all(self, last_prices, **rule_kwargs):
        """对全部持仓跑卖出规则。
        last_prices: {code: 最新价}。返回 [(code, position, action, reason), ...]"""
        out = []
        for code, pos in self.positions.items():
            last = (last_prices or {}).get(code)
            if not last:
                continue
            try:
                buy_date = datetime.strptime(str(pos["buy_date"]), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                buy_date = date.today()
            rule = ExitRule(code, pos.get("name"), pos["buy_price"], buy_date,
                            **rule_kwargs)
            action, reason = rule.evaluate(last)
            if action:
                out.append((code, pos, action, reason))
        return out

    @classmethod
    def from_json(cls, data):
        if not isinstance(data, dict):
            return cls()
        return cls({k: dict(v) for k, v in data.items()})
