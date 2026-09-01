# -*- coding: utf-8 -*-
"""模拟实盘账户引擎(100万/first_board_v04) — 设计规格 2026-09-01-paper-trading。

安全边界(设计 §8): 本模块绝不写 D:\\QMT_SIGNALS、绝不调用下单接口;
账本 .paper_account.json 原子写, 保存成功才算交易发生; 现金恒>=0、持仓<=max_positions。
数据(行情/K线)只读。"""
import copy
import json
import os
from datetime import datetime
from pathlib import Path

from prism.engine import load_strategy

STATE_FILENAME = ".paper_account.json"
_DEFAULT_STRATEGY = Path(__file__).parent / "strategies" / "first_board_v04.json"
_REQUIRED_KEYS = ("version", "created", "initial_capital", "cash", "holdings",
                  "trades", "nav_history", "live_nav", "screens_done",
                  "settled_dates")


class PaperAccount:
    """模拟账户: 账本 + 买入/卖出执行 + 结算 + 查询。

    执行临界段纪律: 先在内存改, 校验不变量, 再 save(); save 失败 →
    _restore_state 回滚 —— 保存成功才视为交易发生(设计 §5)。"""

    def __init__(self, strategy_path=None, initial_capital=1000000.0,
                 position_ratio=0.3, max_positions=5, fee_rate=0.00025,
                 slippage=0.001, stamp_duty=0.0005, transfer_fee=0.00001,
                 state_path=None):
        self.strategy_path = Path(strategy_path) if strategy_path \
            else _DEFAULT_STRATEGY
        self.initial_capital = float(initial_capital)
        self.position_ratio = float(position_ratio)
        self.max_positions = int(max_positions)
        self.fee_rate = float(fee_rate)
        self.slippage = float(slippage)
        self.stamp_duty = float(stamp_duty)
        self.transfer_fee = float(transfer_fee)
        if state_path is not None:
            self.state_path = Path(state_path)
        else:
            self.state_path = Path(__file__).parent.parent / STATE_FILENAME
        self.state = None
        self._strategy = None

    # ---------- 策略(惰性加载, 与实盘同一份 JSON) ----------
    @property
    def strategy(self):
        if self._strategy is None:
            from prism import registry as reg
            reg.scan_factors(force=True)   # 幂等重扫注册因子库(load_strategy 校验依赖)
            self._strategy = load_strategy(self.strategy_path)
        return self._strategy

    # ---------- 账本 ----------
    def init_account(self, created=None):
        """初始化账本(幂等: 已存在返回现有, 不覆盖)。"""
        if self.load():
            return self.state
        now = datetime.now()
        self.state = {
            "version": 1,
            "created": created or now.strftime("%Y-%m-%d"),
            "initial_capital": self.initial_capital,
            "cash": self.initial_capital,
            "holdings": [],
            "trades": [],
            "nav_history": [],
            "live_nav": self.initial_capital,
            "screens_done": [],
            "settled_dates": [],
        }
        self.save()
        return self.state

    def load(self):
        """读账本; 不存在/损坏/结构不符 → False(绝不覆盖原文件)。"""
        if not self.state_path.exists():
            return False
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(raw, dict) or any(k not in raw
                                            for k in _REQUIRED_KEYS):
            return False
        if raw.get("version") != 1:
            return False
        self.state = raw
        return True

    def save(self):
        """原子写: 先写 .tmp 再 os.replace; 任何异常向上抛(调用方回滚)。"""
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ---------- 执行临界段 ----------
    def _snapshot_state(self):
        return copy.deepcopy(self.state)

    def _restore_state(self, snap):
        self.state = snap

    # ---------- 查询 ----------
    def summary(self):
        if self.state is None and not self.load():
            return {"exists": False}
        st = self.state
        nav = float(st["live_nav"])
        return {
            "exists": True,
            "created": st["created"],
            "cash": round(float(st["cash"]), 2),
            "nav": round(nav, 2),
            "total_return_pct": round((nav / st["initial_capital"] - 1) * 100, 2),
            "holdings_count": len(st["holdings"]),
            "updated_at": (st["trades"][-1]["ts"]
                           if st["trades"] else st["created"]),
        }

    def detail(self, trade_limit=50):
        if self.state is None and not self.load():
            return {"exists": False}
        st = self.state
        return {
            "exists": True,
            "holdings": st["holdings"],
            "trades": st["trades"][-trade_limit:][::-1],
            "nav_history": st["nav_history"],
        }

    # ---------- 净值估算 ----------
    def _nav_estimate(self, ticks=None):
        """现金 + Σ持仓市值(ticks 有则按实时价, 无则按成本)。"""
        st = self.state
        if ticks:
            nav = st["cash"] + sum(
                h["shares"] * float((ticks.get(h["code"]) or {}).get(
                    "lastPrice") or h["cost"])
                for h in st["holdings"])
        else:
            nav = st["cash"] + sum(h["shares"] * h["cost"]
                                   for h in st["holdings"])
        return max(float(nav), 0.0)

    # ---------- 买入 ----------
    def buy_from_screen(self, provider, now=None):
        """时点选股(first_board_v04, 同一引擎) + 买入执行。

        幂等: 时点键 YYYY-MM-DDTHH:MM 已在 screens_done → already_done。
        选股失败/保存失败 fail-closed(不记账), 报告 error。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return {"error": "未初始化"}
        d = now.strftime("%Y-%m-%d")
        ts_key = "%sT%s" % (d, now.strftime("%H:%M"))
        if ts_key in self.state["screens_done"]:
            return {"candidates": 0, "bought": [], "skipped": [],
                    "env_ok": False, "already_done": True}
        from prism.engine import run_screen
        from prism import registry as reg
        market_ctx = provider.build_market_context()
        gate_fids = (self.strategy.get("market_gate") or {}).get("factors", [])
        gate_factors = {}
        for fid in gate_fids:
            try:
                res = reg.get_factor(fid)["func"](market_ctx)
                if isinstance(res, dict):
                    gate_factors[fid] = 1 if res.get("score") else 0
                else:
                    gate_factors[fid] = 1 if res else 0
            except Exception:
                gate_factors[fid] = 0
        limit_ups = provider.get_limit_ups()
        stock_contexts = {}
        for lu in limit_ups:
            try:
                stock_contexts[lu["code"]] = provider.build_stock_context(
                    lu["code"])
            except Exception:
                pass
        try:
            result = run_screen(self.strategy, market_ctx,
                                gate_factors=gate_factors,
                                stock_contexts=stock_contexts)
        except Exception as e:
            snap = self._snapshot_state()
            self.state["screens_done"].append(ts_key)
            try:
                self.save()
            except Exception:
                self._restore_state(snap)
            return {"error": "选股失败: %r" % e}
        env_ok = bool(result.get("environment_ok"))
        bought, skipped = [], []
        if env_ok:
            nav = self._nav_estimate()
            for c in result.get("candidates", []):
                code = c.get("code")
                up = float(c.get("up_stop_price") or 0)
                if not code or up <= 0:
                    skipped.append({"code": code, "reason": "缺涨停价"})
                    continue
                reason = self._buyable(code, nav)
                if reason:
                    skipped.append({"code": code, "reason": reason})
                    continue
                if self._one_word_board(code, up, provider):
                    skipped.append({"code": code, "reason": "一字板买不到"})
                    continue
                done = self._execute_buy(code, up, now=now)
                if done:
                    bought.append(done)
                    nav = self._nav_estimate()   # 买入后更新可用净值
                else:
                    skipped.append({"code": code, "reason": "执行失败"})
        snap = self._snapshot_state()
        self.state["screens_done"].append(ts_key)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return {"candidates": len(result.get("candidates", [])),
                    "bought": bought, "skipped": skipped, "env_ok": env_ok,
                    "error": "状态保存失败"}
        return {"candidates": len(result.get("candidates", [])),
                "bought": bought, "skipped": skipped, "env_ok": env_ok}

    def _buyable(self, code, nav):
        """买入前置判定; None=可买。"""
        st = self.state
        today = datetime.now().strftime("%Y-%m-%d")
        if any(h["code"] == code for h in st["holdings"]):
            return "已持仓"
        if any(t.get("side") == "buy" and t.get("code") == code
               and t.get("date") == today for t in st["trades"]):
            return "今日已交易"
        if len(st["holdings"]) >= self.max_positions:
            return "仓位已满"
        if st["cash"] < nav * self.position_ratio:
            return "现金不足"
        return None

    def _one_word_board(self, code, up_price, provider):
        """一字板判定: 当日K线 low >= up_price-0.01(全天未开板) → True。"""
        try:
            df = provider.ds.get_kline(code, days=1)
        except Exception:
            return True              # 拿不到K线 → 保守视为买不到
        if df is None or len(df) == 0 or "low" not in getattr(df, "columns", []):
            return True
        low = float(df["low"].iloc[-1])
        return low >= up_price - 0.01

    def _execute_buy(self, code, up_price, now=None):
        """买入执行(临界段: 内存改→不变量校验→save, 失败回滚)。"""
        now = now or datetime.now()
        snap = self._snapshot_state()
        nav = self._nav_estimate()
        target = nav * self.position_ratio
        buy_price = round(up_price * (1 + self.slippage), 4)
        shares = int(target / buy_price / 100) * 100
        if shares <= 0:
            return None
        amount = round(shares * buy_price, 2)
        fee = round(amount * (self.fee_rate + self.transfer_fee), 2)
        cash_after = round(self.state["cash"] - amount - fee, 2)
        if cash_after < 0:
            return None
        d = now.strftime("%Y-%m-%d")
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        self.state["cash"] = cash_after
        self.state["holdings"].append({
            "code": code, "shares": shares, "cost": buy_price,
            "buy_date": d, "buy_price": buy_price, "entry_nav": nav})
        self.state["trades"].append({
            "ts": ts, "date": d, "side": "buy", "code": code,
            "price": buy_price, "shares": shares, "amount": amount,
            "fee": fee, "reason": "screen", "cash_after": cash_after})
        self.state["live_nav"] = round(self._nav_estimate(), 2)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return None
        return {"code": code, "shares": shares, "price": buy_price,
                "amount": amount, "fee": fee}

    # ---------- 卖出 ----------
    def sell_check(self, ticks, now=None):
        """tick 监控: 止盈/止损触发卖出; T+1(当日买入不卖); 同步 live_nav。

        ticks: {code: tick_dict}; 缺该股 tick 或价格<=0 → 跳过。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return []
        d = now.strftime("%Y-%m-%d")
        rules = self.strategy.get("sell_rules") or {}
        # 注: 策略/回测中 sell_rules 以小数存储(0.08/0.05, 同 backtest.py),
        # 直接读小数; 缺省 8%/5% —— 简报蓝图误除以 100, 按测试意图(8%/5%)修正。
        tp = float(rules.get("take_profit_pct") or 0.08)
        sl = float(rules.get("stop_loss_pct") or 0.05)
        out = []
        for h in list(self.state["holdings"]):
            if h["buy_date"] >= d:          # T+1: 当日买入不卖
                continue
            t = (ticks or {}).get(h["code"]) or {}
            price = float(t.get("lastPrice") or 0)
            if price <= 0:
                continue
            cost = float(h["cost"])
            if price >= cost * (1 + tp):
                reason = "take_profit"
            elif price <= cost * (1 - sl):
                reason = "stop_loss"
            else:
                continue
            done = self._execute_sell(h["code"], price, reason, now=now)
            if done:
                out.append(done)
        snap = self._snapshot_state()
        self.state["live_nav"] = round(self._nav_estimate(ticks), 2)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
        return out

    def _execute_sell(self, code, price, reason, now=None):
        """卖出执行(临界段): 成交价=price×(1-slippage), 印花税仅卖出侧。"""
        now = now or datetime.now()
        snap = self._snapshot_state()
        idx = next((i for i, h in enumerate(self.state["holdings"])
                    if h["code"] == code), None)
        if idx is None:
            return None
        h = self.state["holdings"][idx]
        sell_price = round(price * (1 - self.slippage), 4)
        amount = round(h["shares"] * sell_price, 2)
        fee = round(amount * (self.fee_rate + self.stamp_duty
                              + self.transfer_fee), 2)
        cash_after = round(self.state["cash"] + amount - fee, 2)
        d = now.strftime("%Y-%m-%d")
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        self.state["cash"] = cash_after
        self.state["holdings"].pop(idx)
        self.state["trades"].append({
            "ts": ts, "date": d, "side": "sell", "code": code,
            "price": sell_price, "shares": h["shares"], "amount": amount,
            "fee": fee, "reason": reason, "cash_after": cash_after})
        self.state["live_nav"] = round(self._nav_estimate(), 2)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return None
        return {"code": code, "price": sell_price, "shares": h["shares"],
                "amount": amount, "fee": fee, "reason": reason}
