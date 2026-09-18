# -*- coding: utf-8 -*-
"""行情与指标层。

两个后端, 同一接口:
  - XtdataBackend : 真实 miniQMT 行情(xtdata)。缺历史数据时自动补下载。
  - SampleBackend : 读 ttcore/sample_data/*.csv 的离线样本(非交易时段演示/测试)。

所有方法 fail-open(取不到 → None / {}), 由上层 fail-closed 决策:
拿不到行情就不做T, 而不是拿旧价硬做。

指标口径统一在 indicators() 里算, 避免网格/开关/网页三处各算一遍导致漂移。
"""
import csv
from datetime import datetime
from pathlib import Path

from . import grid

SAMPLE_DIR = Path(__file__).parent / "sample_data"


# ---------------------------------------------------------------- 指标

def indicators(closes, sigma_window=60):
    """由收盘价序列(升序)算出做T所需的全部指标。

    返回 {'last','ma20','ma20_prev','r20_pct','sigma','trend_degree','n'}。
    数据不足的字段为 None(上层 switch/band 会 fail-closed)。
    """
    out = {"last": None, "ma20": None, "ma20_prev": None,
           "r20_pct": None, "sigma": None, "trend_degree": None, "n": 0}
    if not closes:
        return out
    seq = [float(c) for c in closes if c is not None]
    if not seq:
        return out
    out["n"] = len(seq)
    out["last"] = seq[-1]
    out["ma20"] = grid.ma(seq, 20)
    # MA20 的斜率基准: 取 T-5 时点的 MA20(用截至那时的 20 根)
    if len(seq) >= 25:
        out["ma20_prev"] = grid.ma(seq[:-5], 20)
    elif len(seq) >= 21:
        out["ma20_prev"] = grid.ma(seq[:-1], 20)
    if len(seq) >= 21:
        out["r20_pct"] = (seq[-1] / seq[-21] - 1) * 100.0
    out["sigma"] = grid.daily_sigma(seq, window=sigma_window)
    out["trend_degree"] = grid.trend_degree(seq, 20)
    return out


def now_hhmm(now=None):
    return (now or datetime.now()).strftime("%H:%M")


def prev_close(tick, daily, fallback=None):
    """做T中枢 ref 的"前收": 优先**最后一根已完成日K的收盘**。

    tick 只在其交易日**新于**日K末根时才采信 —— 盘前 QMT 的 tick 快照还停在上一
    交易日盘中那一份, 其 lastClose 指向更早一天, 直接采信会把 ref 钉错一个交易日
    (账本当轮就缓存 ref → 全天档位全错)。日K拿不到 → 回落 tick(旧行为, 不抛)。

    返回 (价格, 来源): 'daily' 日K末根 | 'tick_newer' tick 更新于日K |
    'tick_fallback' 日K缺失回落 tick。
    """
    t, d = tick or {}, daily or {}
    tclose = _num(t.get("last_close")) or _num(fallback)
    dclose = _num(d.get("close"))
    if dclose > 0:
        tdate, ddate = t.get("date"), d.get("date")
        if tclose > 0 and tdate and ddate and tdate > ddate:
            return tclose, "tick_newer"
        return dclose, "daily"
    if tclose > 0:
        return tclose, "tick_fallback"
    return None, ""


def _tick_date(t):
    """tick 所属交易日(YYYYMMDD, 本地时区); 取不到 → None。

    xtdata.get_full_tick 实测只给 'time'(毫秒时间戳), 没有 'timetag'。
    """
    ts = _num(t.get("time"))
    if ts > 1e11:                       # 13 位毫秒 → 秒
        ts /= 1000.0
    if ts <= 1e8:
        return None
    try:
        return datetime.fromtimestamp(ts).strftime("%Y%m%d")
    except (OSError, OverflowError, ValueError):
        return None


# ---------------------------------------------------------------- 后端

class XtdataBackend:
    """真实 miniQMT 行情后端(懒加载 xtquant)。"""

    def __init__(self, download_missing=True):
        self._xtdata = None
        self.download_missing = download_missing
        self._downloaded = set()

    def _mod(self):
        if self._xtdata is None:
            from xtquant import xtdata
            self._xtdata = xtdata
        return self._xtdata

    def ticks(self, codes):
        try:
            xtdata = self._mod()
            raw = xtdata.get_full_tick(list(codes)) or {}
        except Exception:
            return {}
        out = {}
        for code, t in raw.items():
            if not isinstance(t, dict):
                continue
            out[code] = {
                "last": _num(t.get("lastPrice")),
                "open": _num(t.get("open")),
                "high": _num(t.get("high")),
                "low": _num(t.get("low")),
                "last_close": _num(t.get("lastClose")),
                "date": _tick_date(t),          # 该 tick 所属交易日(定 ref 比新旧用)
                "volume": _num(t.get("volume")),
                "amount": _num(t.get("amount")),
            }
        return out

    def daily_last(self, code):
        """最后一根日K → {"date": "YYYYMMDD", "close": float}; 取不到 → None。

        定 ref 用(count=1 只取末根, 不多拉历史)。
        """
        try:
            data = self._mod().get_market_data_ex(
                ["close"], [code], period="1d", count=1)
        except Exception:
            return None
        return _df_last_bar((data or {}).get(code))

    def closes(self, code, count=80):
        xtdata = None
        try:
            xtdata = self._mod()
            data = xtdata.get_market_data_ex(
                ["close"], [code], period="1d", count=int(count))
            df = (data or {}).get(code)
            seq = _df_closes(df)
            if len(seq) >= 21:
                return seq
            # 本地不够 → 尝试补下载一次(每个 code 只试一次, 避免每轮都下载)
            if self.download_missing and code not in self._downloaded:
                self._downloaded.add(code)
                try:
                    xtdata.download_history_data(code, "1d", "", "")
                    data = xtdata.get_market_data_ex(
                        ["close"], [code], period="1d", count=int(count))
                    seq = _df_closes((data or {}).get(code))
                except Exception:
                    return None
            return seq if len(seq) >= 3 else None
        except Exception:
            return None


class SampleBackend:
    """离线样本后端: 读 ttcore/sample_data/<code>.csv (date,close,high,low)。"""

    def __init__(self, sample_dir=None):
        self.dir = Path(sample_dir or SAMPLE_DIR)

    def _rows(self, code):
        f = self.dir / ("%s.csv" % code)
        if not f.exists():
            return []
        out = []
        try:
            with open(f, "r", encoding="utf-8") as fp:
                for r in csv.DictReader(fp):
                    try:
                        out.append({
                            "date": r.get("date", ""),
                            "close": float(r["close"]),
                            "high": float(r.get("high") or r["close"]),
                            "low": float(r.get("low") or r["close"]),
                        })
                    except (KeyError, ValueError, TypeError):
                        continue
        except OSError:
            return []
        return out

    def closes(self, code, count=80):
        rows = self._rows(code)
        if not rows:
            return None
        return [r["close"] for r in rows[-int(count):]]

    def ticks(self, codes):
        out = {}
        for code in codes:
            rows = self._rows(code)
            if not rows:
                continue
            last = rows[-1]
            prev = rows[-2] if len(rows) > 1 else rows[-1]
            out[code] = {
                "last": last["close"], "open": prev["close"],
                "high": last["high"], "low": last["low"],
                "last_close": prev["close"], "volume": 0.0, "amount": 0.0,
            }
        return out

    def daily_last(self, code):
        """样本模式不提供日K末根 → None(上层回落 tick, 维持原行为)。

        样本的 tick 已经把 CSV 末根当"当日"用, 再暴露末根会与它同一天 → 两个口径
        打架, 样本/离线演示的 ref 会从"前收"漂成"末根收盘"。
        """
        return None


def _num(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if f == f else default          # 过滤 NaN


def _df_closes(df):
    if df is None:
        return []
    try:
        if len(df) == 0:
            return []
        col = df["close"].tolist() if "close" in df.columns else list(df.iloc[:, 0])
    except Exception:
        return []
    return [float(x) for x in col if x is not None and x == x]


def _df_last_bar(df):
    """日K末根 → {"date": "YYYYMMDD", "close": float}; 空/垃圾 → None(fail-open)。"""
    try:
        if df is None or len(df) == 0:
            return None
        close = float(df["close"].iloc[-1])
        # period='1d' 的 index 实测是 'YYYYMMDD'; DatetimeIndex 也归一化到同一形状
        date = str(df.index[-1]).replace("-", "").split(" ")[0][:8]
    except Exception:
        return None
    # NaN / 0 价 / 日期不成形 → 这根不可用
    if close != close or close <= 0 or not date.isdigit():
        return None
    return {"date": date, "close": close}


# ---------------------------------------------------------------- 门面

class MarketFeed:
    """行情门面。backend 可注入(默认先用真实 QMT, 失败回落样本)。"""

    def __init__(self, backend=None, fallback=None, now_fn=None):
        self.backend = backend or XtdataBackend()
        self.fallback = fallback if fallback is not None else SampleBackend()
        self.now_fn = now_fn
        self.last_source = ""

    def ticks(self, codes):
        codes = list(codes)
        t = self.backend.ticks(codes) if self.backend else {}
        missing = [c for c in codes if c not in t]
        if missing and self.fallback is not None:
            fb = self.fallback.ticks(missing) or {}
            if fb:
                t.update(fb)
                self.last_source = "sample"
            elif t:
                self.last_source = "qmt"
        elif t:
            self.last_source = "qmt"
        return t

    def closes(self, code, count=80):
        c = self.backend.closes(code, count) if self.backend else None
        if c and len(c) >= 3:
            return c
        if self.fallback is not None:
            return self.fallback.closes(code, count)
        return None

    def daily_last(self, code):
        """最后一根已完成日K的 {"date","close"}; backend 没这能力/取不到 → None。"""
        f = getattr(self.backend, "daily_last", None)
        if f is None:
            return None
        try:
            return f(code)
        except Exception:
            return None

    def snapshot(self, code, count=80, sigma_window=60):
        """单标的完整上下文: tick + 指标 + 日K末根。行情缺失 → None。"""
        t = (self.ticks([code]) or {}).get(code)
        closes = self.closes(code, count)       # 先取日K: 本地不够时会补下载一次
        if not t and not closes:
            return None
        daily = self.daily_last(code)           # 定 ref 的权威前收(拿不到 → None)
        ind = indicators(closes, sigma_window) if closes else indicators([])
        if t:
            # 用真实 tick 的现价/最高/最低覆盖样本推出来的值
            ind["last"] = t.get("last") or ind["last"]
        return {"code": code, "tick": t or {}, "ind": ind, "daily": daily,
                "source": self.last_source or "unknown"}

    def hhmm(self):
        return now_hhmm(self.now_fn() if self.now_fn else None)


def make_feed(prefer_sample=False, now_fn=None):
    """构造 Feed。prefer_sample=True 时只用样本(离线演示)。"""
    if prefer_sample:
        return MarketFeed(backend=SampleBackend(), fallback=None, now_fn=now_fn)
    return MarketFeed(backend=XtdataBackend(), fallback=SampleBackend(),
                      now_fn=now_fn)
