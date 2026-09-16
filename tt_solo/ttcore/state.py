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
import os
from datetime import datetime, date
from pathlib import Path

from ._vendor import STATE_DIR, atomic_write

STATE_VERSION = 1
MAX_EVENTS = 500

DEFAULT_STATE_PATH = STATE_DIR / "tt_state.json"
HISTORY_NAME = "tt_history.jsonl"


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
    """原子写: 复用 _vendor.atomic_write(mkdir+fsync+os.replace)。"""
    atomic_write(path, text)


class Ledger:
    """当日 T 账本。所有变更即时落盘(可注入 path=None 表示纯内存)。

    writable=False = **只读模式**: 一样读盘, 但 load/reset_day/archive_current/
    _preserve_unarchived/save 一个字节都不写。给监控面板用 —— 面板必须能读到
    真实账本, 又绝不能改变交易状态(包括"什么时候翻页")。
    """

    def __init__(self, path=None, now_fn=None, writable=True):
        self.path = path if path is not None else DEFAULT_STATE_PATH
        self.now_fn = now_fn or datetime.now
        self.writable = bool(writable)
        self.state = _empty_state(_today_str(self.now_fn()))
        self._loaded = False
        # 日终归档: 与账本同目录的 append-only JSONL(见 archive_current)
        self.history_path = (Path(self.path).with_name(HISTORY_NAME)
                             if self.path else None)
        self._archived_dates = self._load_archived_dates()

    # ---------------- 载入 / 重置 ----------------

    def load(self):
        """读盘。跨日 → 先归档旧账本; 同日的版本/结构不符 → 改名留档;
        损坏 → 直接重置。两者都不让存量数据凭空消失, 再重置为新日账本。

        只读模式: 只认"当日有效"的账本; 跨日 / 版本不符 / 损坏一律不归档、
        不改名、不落盘 —— 内存里留着的就是新一天的空账本(拿昨天的成交冒充
        今天会让重算出的档位与净敞口全错)。
        """
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
                if not self.writable:
                    self._loaded = True
                    return self.state
                # 存量数据两条出路, 一律不许"就地覆盖丢掉":
                #   1. 已完结的日(date != today) → 归档进 history;
                #   2. 同日但版本/结构不符 → 刻意不归档(否则给今天写半日行并占掉
                #      归档位, 日终真正的汇总行再也写不出来), 改为改名留档
                #      (见 _preserve_unarchived), 原始数据仍在盘上可人工恢复。
                # "非空"口径与 archive_current 对齐: 只有 events 也算有料。
                if isinstance(raw, dict):
                    has_data = bool(raw.get("symbols") or raw.get("events"))
                    archived = False
                    if has_data and raw.get("date") != today:
                        self.state = raw
                        archived = self.archive_current()
                    if has_data and not archived:
                        self._preserve_unarchived(raw)
            except (ValueError, OSError):
                pass                          # 坏文件 → 按新日重置(fail-safe)
        if not self.writable:
            self._loaded = True
            return self.state                 # 只读: 盘上没有可用的今日账本
        self.state = _empty_state(today)
        self._loaded = True
        self.save()
        return self.state

    def reset_day(self, day=None):
        target = _today_str(day or self.now_fn())
        if not self.writable:
            # 只读: 翻页只发生在内存里 —— 不归档、不落盘(见 load())
            self.state = _empty_state(target)
            return self.state
        # 只归档已完结的日: 同日手动 reset 不归档(否则半日行钉死今天, 见 load())
        if self.state.get("date") != target:
            self.archive_current()
        self.state = _empty_state(target)
        self.save()
        return self.state

    def roll_if_new_day(self):
        """跨日则自动重置。返回 True 表示发生了重置。"""
        today = _today_str(self.now_fn())
        if self.state.get("date") != today:
            self.reset_day(today)
            return True
        return False

    # ---------------- 日终归档 ----------------

    def _preserve_unarchived(self, raw):
        """未被归档的账本在重置前改名留档, 免得 save() 把它就地盖成空账本。

        进这里 = 同日仅版本不符(收盘后升版 / 手改过 state 文件)或归档没写成功。
        定名(name + 旧版本 + 日期)是关键: 重启循环反复覆盖同一个 .bak, 不堆积。
        名字带版本+日期, 也不会撞上正经的 state 文件。

        fail-safe: 与归档同款 —— 任何异常吞掉, 备份是旁路, 绝不阻断载入/交易。
        """
        try:
            if not self.writable:
                return                        # 只读模式: 改名也是写
            p = Path(self.path)
            if not p.exists():
                return
            bak = "%s.pre-v%s-%s.bak" % (p.name, raw.get("version"),
                                         raw.get("date") or "")
            p.replace(p.with_name(bak))       # os.replace 语义: 同盘原子改名
        except Exception:                 # noqa: BLE001 - 刻意吞掉一切
            pass

    def _load_archived_dates(self):
        """已归档日期集合(用于幂等判断)。读不到 → 空集, 不阻断。"""
        if not self.history_path:
            return set()
        try:
            p = Path(self.history_path)
            if not p.exists():
                return set()
            out = set()
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                # 标量行(如 5 / "x")解析得出来但不是行 —— 跳过它, 别让一行
                # 毒掉整份日期集合(那会导致已归档的日被写第二遍)。
                if not isinstance(obj, dict):
                    continue
                d = obj.get("date")
                if d:
                    out.add(str(d))
            return out
        # 刻意宽于 OSError: 归档文件损坏(如非 UTF-8)会抛 UnicodeDecodeError,
        # 而这一读发生在 __init__ = 交易路径上。归档只是旁路, 一律 fail-open。
        except Exception:                 # noqa: BLE001 - 见上
            return set()

    def archive_current(self):
        """把当前账本摘要追加到 history.jsonl。返回 True 表示真的写了一行。

        fail-safe: 任何异常都吞掉并返回 False —— 归档是旁路副作用, 绝不允许
        它影响交易主流程。空账本(无成交且无事件)不写, 避免跨日刷垃圾行。
        """
        try:
            if not self.writable:
                return False                  # 只读模式: 归档就是写
            if not self.history_path:
                return False
            syms = self.state.get("symbols") or {}
            events = self.state.get("events") or []
            if not syms and not events:
                return False
            day = str(self.state.get("date") or "")
            if not day or day in self._archived_dates:
                return False
            sold_total = sum(int((v or {}).get("sold_today", 0))
                             for v in syms.values())
            bought_total = sum(int((v or {}).get("bought_today", 0))
                               for v in syms.values())
            row = {
                "date": day,
                "archived_at": datetime.now().isoformat(timespec="seconds"),
                "sold_total": sold_total,
                "bought_total": bought_total,
                "trips": self.total_trips(),
                "realized_pnl": self.total_realized_pnl(),
                "trades": len(events),
                "symbols": {c: round(float((v or {}).get("realized_pnl", 0)), 2)
                            for c, v in syms.items()},
            }
            p = Path(self.history_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            # append-only = 真的 append。绝不做"读全文 → 拼接 → 原子替换":
            # 幂等判据来自 __init__ 读一次的日期集合, 两个写入者(守护首轮 +
            # 面板重算 / Flask 多线程)都认为"这天没归档"时会各写一行; 读-改-写
            # 除了重复写, 还会把对方刚追加的行整段盖掉。读者本来就按行容错
            # (坏行跳过), 所以 append 严格更安全。
            with open(p, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                fp.flush()
                os.fsync(fp.fileno())     # 与 atomic_write 同款: 掉电不丢这一行
            self._archived_dates.add(day)
            return True
        except Exception:                 # noqa: BLE001 - 刻意吞掉一切
            return False

    @staticmethod
    def read_history(path=None, limit=60):
        """读归档历史(升序, 每个 date 只留最后一行)。坏行跳过; 不存在 → []。

        去重是必须的: 同日可能被两个写入者各追加一次(幂等判据是内存里的集合),
        而前端是**累加**画收益曲线 —— 重复日会让当日盈亏静默翻倍, 正是归档
        要防的事。保留最后一行 = 保留后写的那个(并发时更完整的那次)。
        """
        p = (Path(path).with_name(HISTORY_NAME) if path
             else STATE_DIR / HISTORY_NAME)
        try:
            if not p.exists():
                return []
            rows = []
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):     # 标量行跳过(见 _load_archived_dates)
                    rows.append(obj)
            rows.sort(key=lambda r: str(r.get("date") or ""))
            uniq = {}                         # 同日只留最后一行, 顺序仍是 date 升序
            for r in rows:
                uniq[str(r.get("date") or "")] = r
            rows = list(uniq.values())
            return rows[-int(limit):] if limit else rows
        # 与 _load_archived_dates 同款 fail-safe: 归档文件损坏(如非 UTF-8)只让
        # 读者拿到 [], 不把 UnicodeDecodeError 抛给调用方(BaseException 仍上抛)。
        except Exception:                 # noqa: BLE001 - 见上
            return []

    # ---------------- 读写 ----------------

    def save(self):
        if not self.writable or not self.path:
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
