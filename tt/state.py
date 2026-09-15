# -*- coding: utf-8 -*-
"""做T状态机 + 持久化。

保存两类事实:
  1. **当日 T 账本** —— 每个标的当日卖出/买回了几股、配对盈亏多少。
     日内净敞口闸门(net exposure)与亏损熔断(daily loss)都读它。
  2. **档位成交水位** —— 当日卖出/买入各已成交到第几档, 避免同一档重复触发。

关键设计:
  - **跨日自动重置**: 每次 load 时比对日期, 不同日则清零账本(档位水位随
    ref 变化必须重置; 亏损/次数限额按自然日重算)。这是"日重置网格"的前提。
  - **配对盈亏走 FIFO**: 卖出先与已有买入配对(反T平仓), 剩余进卖出队列;
    买入先与已有卖出配对(正T平仓), 剩余进买入队列。两种方向都算得对。
  - **原子写**: 同目录 tmp + os.replace, 断电/崩溃不留半截文件。
  - **events 环形截断**: 只留最近 N 条, 防止状态文件无限膨胀(它是每秒读的)。
"""
import json
from datetime import datetime, date
from pathlib import Path

from shared.common import STATE_DIR, atomic_write

STATE_VERSION = 1
MAX_EVENTS = 500

REPO = Path(__file__).resolve().parent.parent
DEFAULT_STATE_PATH = STATE_DIR / "tt_state.json"


def _today_str(now=None):
    d = now or datetime.now()
    if isinstance(d, datetime):
        d = d.date()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)[:10]


def _empty_symbol():
    return {
        "sold_today": 0,
        "bought_today": 0,
        "filled_sell_units": 0,
        "filled_buy_units": 0,
        "trips": 0,
        "realized_pnl": 0.0,
        "sell_queue": [],      # [[price, qty], ...]
        "buy_queue": [],
        "ref_price": 0.0,
        "last_sell_price": 0.0,
        "last_buy_price": 0.0,
        "switch_state": "",
        "note": "",
    }


def _empty_state(day):
    return {
        "version": STATE_VERSION,
        "date": day,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "symbols": {},
        "events": [],
    }


def _atomic_write(path, text):
    """原子写: 复用 shared.common.atomic_write(mkdir+fsync+os.replace)。"""
    atomic_write(path, text)


class Ledger:
    """当日 T 账本。所有变更即时落盘(可注入 path=None 表示纯内存)。"""

    def __init__(self, path=None, now_fn=None):
        self.path = path if path is not None else DEFAULT_STATE_PATH
        self.now_fn = now_fn or datetime.now
        self.state = _empty_state(_today_str(self.now_fn()))
        self._loaded = False

    # ---------------- 载入 / 重置 ----------------

    def load(self):
        """读盘。跨日 / 损坏 / 版本不符 → 重置为新日账本。"""
        today = _today_str(self.now_fn())
        if self.path and Path(self.path).exists():
            try:
                raw = json.loads(Path(self.path).read_text(encoding="utf-8"))
                if (isinstance(raw, dict)
                        and raw.get("version") == STATE_VERSION
                        and raw.get("date") == today
                        and isinstance(raw.get("symbols"), dict)):
                    raw.setdefault("events", [])
                    self.state = raw
                    self._loaded = True
                    return self.state
            except (ValueError, OSError):
                pass                          # 坏文件 → 按新日重置(fail-safe)
        self.state = _empty_state(today)
        self._loaded = True
        self.save()
        return self.state

    def reset_day(self, day=None):
        self.state = _empty_state(_today_str(day or self.now_fn()))
        self.save()
        return self.state

    def roll_if_new_day(self):
        """跨日则自动重置。返回 True 表示发生了重置。"""
        today = _today_str(self.now_fn())
        if self.state.get("date") != today:
            self.reset_day(today)
            return True
        return False

    # ---------------- 读写 ----------------

    def save(self):
        if not self.path:
            return
        self.state["updated_at"] = datetime.now().isoformat(timespec="seconds")
        _atomic_write(self.path, json.dumps(self.state, ensure_ascii=False,
                                            indent=1))

    def sym(self, code, create=True):
        syms = self.state.setdefault("symbols", {})
        if code not in syms:
            if not create:
                return None
            syms[code] = _empty_symbol()
        # 补齐老版本可能缺的键(向前兼容)
        base = _empty_symbol()
        for k, v in base.items():
            syms[code].setdefault(k, v)
        return syms[code]

    def set_ref(self, code, ref):
        s = self.sym(code)
        s["ref_price"] = float(ref or 0)
        return s

    def get_ref(self, code):
        s = self.sym(code, create=False)
        return (s or {}).get("ref_price") or 0.0

    def set_switch(self, code, state_name):
        self.sym(code)["switch_state"] = str(state_name or "")
        self.save()

    def set_units(self, code, side, units):
        self.sym(code)["filled_%s_units" % side.lower()] = int(units)
        self.save()

    def get_units(self, code, side):
        s = self.sym(code, create=False)
        if not s:
            return 0
        return int(s.get("filled_%s_units" % side.lower(), 0))

    # ---------------- 成交记账 ----------------

    def record_fill(self, code, side, price, volume, hhmm="", reason="",
                    order_id=""):
        """记一笔成交, 返回配对明细 dict。

        配对(FIFO):
          SELL → 先与 buy_queue 对冲(反T平仓), 余额进 sell_queue
          BUY  → 先与 sell_queue 对冲(正T平仓), 余额进 buy_queue
        实现盈亏 = Σ (卖出价 - 买入价) × 配对股数(仅"卖出腿"记账, 避免重复)。
        """
        s = self.sym(code)
        side = str(side).upper()
        price = float(price or 0)
        volume = int(volume or 0)
        side_l = side.lower()
        matched_qty = 0
        pnl = 0.0

        if side == "SELL":
            s["sold_today"] += volume
            s["last_sell_price"] = price
            q = s.setdefault("buy_queue", [])
            remain = volume
            while remain > 0 and q:
                bp, bq = q[0]
                take = min(remain, bq)
                pnl += (price - float(bp)) * take
                matched_qty += take
                remain -= take
                if take >= bq:
                    q.pop(0)
                else:
                    q[0] = [bp, bq - take]
            if remain > 0:
                s.setdefault("sell_queue", []).append([price, remain])
        else:
            s["bought_today"] += volume
            s["last_buy_price"] = price
            q = s.setdefault("sell_queue", [])
            remain = volume
            while remain > 0 and q:
                sp, sq = q[0]
                take = min(remain, sq)
                pnl += (float(sp) - price) * take
                matched_qty += take
                remain -= take
                if take >= sq:
                    q.pop(0)
                else:
                    q[0] = [sp, sq - take]
            if remain > 0:
                s.setdefault("buy_queue", []).append([price, remain])

        s["realized_pnl"] = round(float(s.get("realized_pnl", 0)) + pnl, 4)
        s["trips"] = int(s.get("trips", 0)) + (1 if matched_qty > 0 else 0)

        ev = {
            "at": datetime.now().isoformat(timespec="seconds"),
            "hhmm": hhmm,
            "code": code,
            "side": side,
            "price": price,
            "volume": volume,
            "matched_qty": matched_qty,
            "pnl": round(pnl, 2),
            "reason": reason,
            "order_id": order_id,
        }
        evs = self.state.setdefault("events", [])
        evs.append(ev)
        if len(evs) > MAX_EVENTS:
            del evs[:len(evs) - MAX_EVENTS]
        self.save()
        return ev

    # ---------------- 汇总(供风控/网页) ----------------

    def daily_trades(self):
        """当日成交笔数(买+卖, 按事件条数计)。"""
        return len(self.state.get("events", []))

    def total_realized_pnl(self):
        syms = self.state.get("symbols", {})
        return round(sum(float((v or {}).get("realized_pnl", 0))
                         for v in syms.values()), 2)

    def total_trips(self):
        syms = self.state.get("symbols", {})
        return sum(int((v or {}).get("trips", 0)) for v in syms.values())

    def snapshot(self):
        """给网页用的安全快照(去掉内部队列细节)。"""
        syms = self.state.get("symbols", {})
        out = {}
        for code, v in syms.items():
            out[code] = {
                "sold_today": int(v.get("sold_today", 0)),
                "bought_today": int(v.get("bought_today", 0)),
                "filled_sell_units": int(v.get("filled_sell_units", 0)),
                "filled_buy_units": int(v.get("filled_buy_units", 0)),
                "trips": int(v.get("trips", 0)),
                "realized_pnl": round(float(v.get("realized_pnl", 0)), 2),
                "net_exposure": int(v.get("bought_today", 0))
                                - int(v.get("sold_today", 0)),
                "switch_state": v.get("switch_state", ""),
                "ref_price": float(v.get("ref_price", 0) or 0),
            }
        return {
            "date": self.state.get("date"),
            "updated_at": self.state.get("updated_at"),
            "symbols": out,
            "daily_trades": self.daily_trades(),
            "total_realized_pnl": self.total_realized_pnl(),
            "total_trips": self.total_trips(),
            "events": list(self.state.get("events", []))[-50:],
        }
