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
        nav = float(st.get("live_nav") or st.get("initial_capital"))
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
