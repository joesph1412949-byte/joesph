# -*- coding: utf-8 -*-
"""市场数据层 — 行业板块/美股指数/资金流的采集与本地缓存。

为 8.23 文档的板块强度/资金流向/美股映射因子提供数据地基:
  * 板块综合评分(行情/量能/资金/风险)、连阳天数、近10日涨幅 → 需行业板块历史K线
  * 主力资金净流入、ETF趋势            → 需板块资金流历史
  * 美股映射(纳指/标普/道指/美元指数)  → 需全球指数历史K线

设计对齐 prism/zt_history.py:
  * 采集结果落盘 pickle(.market_data_cache.pkl), 增量续传(已缓存板块跳过);
  * 按日索引(INDEX_PATH)供回测 O(1) 查询, 与 _pick(asof=) 防未来函数同机制;
  * 网络实现可注入(http_get), 离线测试用罐装 resp.

数据源(东财公开接口, 免费, 实测可用):
  * clist/get        行业板块列表 + 当日行情/资金(f3涨跌幅 f6成交额 f8换手 f62主力净流入)
  * stock/kline/get  板块历史K线(push2his, 可回溯任意日期)
  * fflow/daykline   板块资金流历史(push2his)
  * ulist.np/get     全球指数(纳指/标普/道指/美元指数) 当日快照
  * stock/kline/get(secid=100.NDX 等) 全球指数历史K线(push2his)
"""
import logging
import pickle
import sys
import time
from datetime import date, timedelta
from pathlib import Path

try:
    import requests
except Exception:  # pragma: no cover - 极少数环境无 requests
    requests = None

logger = logging.getLogger(__name__)

# 缓存路径(与 .zt_history_cache.pkl 同级, 已 gitignore)
CACHE_PATH = Path(__file__).parent.parent / ".market_data_cache.pkl"
INDEX_PATH = Path(__file__).parent.parent / ".market_data_index.pkl"

# 回填起点(用户选定: 2026年初至今)
BACKFILL_BEG = "20260101"

# 行业板块代码表: m:90 板块市场, t:2 行业(非概念 t:3), !50 剔除ST板块
SECTOR_FS = "m:90+t:2+f:!50"
# 全球指数 secid: 100.NDX 纳指 / 100.SPX 标普 / 100.DJIA 道指 / 100.UDI 美元指数
GLOBAL_INDICES = [
    ("100.NDX", "NDX", "纳斯达克"),
    ("100.SPX", "SPX", "标普500"),
    ("100.DJIA", "DJIA", "道琼斯"),
    ("100.UDI", "UDI", "美元指数"),
]
# 资金流字段(f51日期 f52主力净流入 f54超大单 f53大单 f55中单 f56小单)
FFLOW_FIELDS2 = "f51,f52,f53,f54,f55,f56"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 "
                   "Safari/537.36"),
    "Referer": "http://quote.eastmoney.com/",
}

# 单次请求重试次数与间隔(东财 flakiness: RemoteDisconnected 偶发)
RETRIES = 3
RETRY_SLEEP = 2.0


class MarketDataError(Exception):
    """市场数据获取失败(网络/解析/超时)。"""
    pass


def _default_http_get(url, params=None, headers=None, timeout=None):
    """默认真实 HTTP 实现。返回 Response-like(有 .json())。"""
    if requests is None:
        raise MarketDataError("requests 未安装, 无法访问东财接口")
    return requests.get(url, params=params, headers=headers, timeout=timeout)


class EastMoneyProbe:
    """东财公开接口封装。http_get 可注入(测试用假实现)。"""

    CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
    KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    FFLOW_URL = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    ULIST_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"

    def __init__(self, http_get=None, timeout=10.0):
        self.http_get = http_get or _default_http_get
        self.timeout = timeout

    def _get_json(self, url, params):
        """带重试的 GET → dict。全部失败 → 抛 MarketDataError。"""
        last = None
        for _ in range(RETRIES):
            try:
                resp = self.http_get(url, params=params, headers=HEADERS,
                                     timeout=self.timeout)
                raise_for_status = getattr(resp, "raise_for_status", None)
                if callable(raise_for_status):
                    raise_for_status()
                return resp.json()
            except Exception as e:  # noqa: BLE001 - 重试吞掉所有网络异常
                last = e
                time.sleep(RETRY_SLEEP)
        raise MarketDataError("东财请求失败 %s: %r" % (url, last))

    # ---------------- 行业板块列表 + 当日行情 ----------------
    def fetch_sector_list(self, page_size=300):
        """行业板块列表 → [{code, name}]。失败 → 抛 MarketDataError。"""
        items = []
        pn = 1
        while True:
            data = self._get_json(self.CLIST_URL, {
                "pn": pn, "pz": page_size, "po": 1, "np": 1,
                "fltt": 2, "invt": 2, "fid": "f12",
                "fs": SECTOR_FS,
                "fields": "f12,f14"})
            diff = ((data or {}).get("data") or {}).get("diff") or []
            if not diff:
                break
            for it in diff:
                if not isinstance(it, dict):
                    continue
                code = it.get("f12")
                if code:
                    items.append({"code": str(code),
                                  "name": str(it.get("f14") or "")})
            total = ((data or {}).get("data") or {}).get("total") or 0
            if len(items) >= int(total):
                break
            pn += 1
        return items

    def fetch_sector_quotes(self, codes, page_size=100):
        """当日板块行情(涨跌幅/成交额/换手/主力净流入) → {code: {...}}。
        f3涨跌幅 f6成交额 f8换手 f12代码 f14名称 f62主力净流入。"""
        out = {}
        for i in range(0, len(codes), page_size):
            chunk = codes[i:i + page_size]
            data = self._get_json(self.CLIST_URL, {
                "pn": 1, "pz": len(chunk), "po": 1, "np": 1,
                "fltt": 2, "invt": 2, "fid": "f3",
                "fs": ",".join("b:%s" % c for c in chunk),
                "fields": "f12,f3,f6,f8,f62"})
            diff = ((data or {}).get("data") or {}).get("diff") or []
            for it in diff:
                if not isinstance(it, dict):
                    continue
                code = it.get("f12")
                if not code:
                    continue
                out[str(code)] = {
                    "pct": it.get("f3"),
                    "amount": it.get("f6"),
                    "turnover": it.get("f8"),
                    "main_net_in": it.get("f62"),
                }
        return out

    # ---------------- 历史K线 ----------------
    def fetch_kline(self, secid, beg, end, fields2="f51,f53,f57"):
        """历史日K → [{date, close, amount?}]。升序。失败 → 抛。"""
        data = self._get_json(self.KLINE_URL, {
            "secid": secid, "klt": 101, "fqt": 1,
            "beg": beg, "end": end,
            "fields1": "f1,f2,f3", "fields2": fields2})
        klines = ((data or {}).get("data") or {}).get("klines") or []
        out = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 2:
                continue
            rec = {"date": parts[0], "close": _f(parts[1])}
            if len(parts) >= 3:
                rec["amount"] = _f(parts[2])
            out.append(rec)
        return out

    # ---------------- 板块资金流历史 ----------------
    def fetch_sector_flow(self, secid, lmt=600):
        """板块资金流历史日线 → [{date, main_net_in, ...}]。升序。失败 → 抛。"""
        data = self._get_json(self.FFLOW_URL, {
            "lmt": lmt, "klt": 101, "secid": secid,
            "fields1": "f1,f2,f3,f7", "fields2": FFLOW_FIELDS2})
        klines = ((data or {}).get("data") or {}).get("klines") or []
        out = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 2:
                continue
            rec = {"date": parts[0], "main_net_in": _f(parts[1])}
            if len(parts) >= 6:
                rec.update({"big_net_in": _f(parts[2]),
                            "xl_net_in": _f(parts[3]),
                            "mid_net_in": _f(parts[4]),
                            "small_net_in": _f(parts[5])})
            out.append(rec)
        return out

    # ---------------- 全球指数 ----------------
    def fetch_global_indices(self):
        """全球指数当日快照 → {code: {name, close, pct}}。失败 → 抛。"""
        secids = ",".join(s for s, _, _ in GLOBAL_INDICES)
        data = self._get_json(self.ULIST_URL, {
            "fltt": 2, "invt": 2, "fields": "f2,f3,f4,f12,f14",
            "secids": secids})
        diff = ((data or {}).get("data") or {}).get("diff") or []
        out = {}
        for it in diff:
            if not isinstance(it, dict):
                continue
            code = it.get("f12")
            if code:
                out[str(code)] = {"name": str(it.get("f14") or ""),
                                  "close": it.get("f2"),
                                  "pct": it.get("f3")}
        return out

    def fetch_global_kline(self, secid, beg, end, lmt=600):
        """全球指数历史K线 → [{date, close}]。升序(与 fetch_kline 同语义)。"""
        data = self._get_json(self.KLINE_URL, {
            "secid": secid, "klt": 101, "fqt": 1,
            "beg": beg, "end": end, "lmt": lmt,
            "fields1": "f1,f2,f3", "fields2": "f51,f53"})
        klines = ((data or {}).get("data") or {}).get("klines") or []
        out = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) >= 2:
                out.append({"date": parts[0], "close": _f(parts[1])})
        return out


def _f(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 缓存

def _load_cache():
    if CACHE_PATH.exists():
        try:
            return pickle.loads(CACHE_PATH.read_bytes())
        except Exception:
            return {}
    return {}


def _save_cache(cache):
    """整文件保存。保留文件里已有的、本次未更新的段(如 global)。"""
    merged = dict(_load_cache())
    merged.update(cache)
    CACHE_PATH.write_bytes(pickle.dumps(merged, protocol=4))


def _load_index():
    if INDEX_PATH.exists():
        try:
            return pickle.loads(INDEX_PATH.read_bytes())
        except Exception:
            return {}
    return {}


def _save_index(index):
    INDEX_PATH.write_bytes(pickle.dumps(index, protocol=4))


# ---------------------------------------------------------------- 采集

def build_sector_cache(probe=None, beg=BACKFILL_BEG, end=None,
                       progress=None):
    """采集行业板块列表 + 历史K线 + 资金流历史, 增量落盘。

    返回 {"sectors": n, "kline_codes": n, "flow_codes": n}。
    缓存结构: {
      "sectors": {code: {"name": ...}},
      "kline":   {code: {"dates": [...], "close": [...], "amount": [...]}},
      "flow":    {code: {"dates": [...], "main_net_in": [...]}},
    }
    """
    probe = probe or EastMoneyProbe()
    end = end or date.today().strftime("%Y%m%d")
    cache = _load_cache()
    sectors = cache.get("sectors") or {}
    kline = cache.get("kline") or {}
    flow = cache.get("flow") or {}

    # 1. 板块列表(增量保留名称, 失败 → 抛错, 不能让任务"看起来成功")
    try:
        lst = probe.fetch_sector_list()
    except MarketDataError as e:
        if not sectors:
            raise MarketDataError("板块列表获取失败且无已有缓存: %r" % e)
        logger.warning("板块列表刷新失败(用已有缓存 %d 个): %r",
                       len(sectors), e)
        lst = []
    for s in lst:
        sectors.setdefault(s["code"], {"name": s["name"]})
        sectors[s["code"]]["name"] = s["name"]

    # 2. 板块历史K线 + 资金流(增量: 已有完整数据的跳过)
    total = len(sectors)
    done = 0
    for code, info in sectors.items():
        secid = "90.%s" % code
        if code not in kline or code not in flow:
            try:
                kl = probe.fetch_kline(secid, beg, end,
                                       fields2="f51,f53,f57")
                fl = probe.fetch_sector_flow(secid)
            except MarketDataError as e:
                logger.warning("板块 %s 采集失败: %r (跳过)", code, e)
                done += 1
                if progress:
                    progress(done, total)
                continue
            if kl:
                kline[code] = {"dates": [r["date"] for r in kl],
                               "close": [r["close"] for r in kl],
                               "amount": [r.get("amount") for r in kl]}
            if fl:
                flow[code] = {"dates": [r["date"] for r in fl],
                              "main_net_in": [r["main_net_in"] for r in fl]}
        done += 1
        # 每 20 个板块增量保存一次(中断不丢进度)
        if done % 20 == 0:
            _save_cache({"sectors": sectors, "kline": kline, "flow": flow})
            logger.info("增量保存: %d/%d 板块", done, total)
        if progress:
            progress(done, total)

    _save_cache({"sectors": sectors, "kline": kline, "flow": flow})
    return {"sectors": len(sectors), "kline_codes": len(kline),
            "flow_codes": len(flow)}


def build_global_cache(probe=None, beg=BACKFILL_BEG, end=None):
    """采集全球指数历史K线, 落盘到同一缓存文件的 "global" 段。

    返回 {code: {"dates": [...], "close": [...]}}。
    """
    probe = probe or EastMoneyProbe()
    end = end or date.today().strftime("%Y%m%d")
    cache = _load_cache()
    globald = cache.get("global") or {}
    for secid, code, name in GLOBAL_INDICES:
        try:
            kl = probe.fetch_global_kline(secid, beg, end)
        except MarketDataError as e:
            logger.warning("指数 %s 采集失败: %r (跳过)", code, e)
            continue
        if kl:
            globald[code] = {"name": name,
                             "dates": [r["date"] for r in kl],
                             "close": [r["close"] for r in kl]}
    if globald:
        cache["global"] = globald
        _save_cache(cache)
    return globald


# ---------------------------------------------------------------- 查询

def _by_date(rec, day):
    """rec(dates/values...) 中取某日索引; 无 → -1。"""
    try:
        return rec["dates"].index(day)
    except ValueError:
        return -1


def sector_close_on(sector_code, day, cache=None):
    """板块某日收盘。day: "YYYY-MM-DD"。无 → None。"""
    cache = cache if cache is not None else _load_cache()
    rec = (cache.get("kline") or {}).get(sector_code)
    if not rec:
        return None
    i = _by_date(rec, day)
    if i < 0:
        return None
    return rec["close"][i]


def sector_flow_on(sector_code, day, cache=None):
    """板块某日主力净流入。无 → None。"""
    cache = cache if cache is not None else _load_cache()
    rec = (cache.get("flow") or {}).get(sector_code)
    if not rec:
        return None
    i = _by_date(rec, day)
    if i < 0:
        return None
    return rec["main_net_in"][i]


def index_close_on(index_code, day, cache=None):
    """全球指数某日收盘(NDX/SPX/DJIA/UDI)。无 → None。"""
    cache = cache if cache is not None else _load_cache()
    rec = (cache.get("global") or {}).get(index_code)
    if not rec:
        return None
    i = _by_date(rec, day)
    if i < 0:
        return None
    return rec["close"][i]


def build_index():
    """从采集缓存构建按日索引(与 zt_history.build_index 同模式):
      {"YYYY-MM-DD": {"sector_close": {code: v}, "sector_flow": {code: v},
                      "global": {code: v}}}
    """
    cache = _load_cache()
    index = {}
    kline = cache.get("kline") or {}
    flow = cache.get("flow") or {}
    globald = cache.get("global") or {}
    for code, rec in kline.items():
        for i, d in enumerate(rec["dates"]):
            day = index.setdefault(d, {"sector_close": {},
                                       "sector_flow": {},
                                       "global": {}})
            day["sector_close"][code] = rec["close"][i]
    for code, rec in flow.items():
        for i, d in enumerate(rec["dates"]):
            day = index.setdefault(d, {"sector_close": {},
                                       "sector_flow": {},
                                       "global": {}})
            day["sector_flow"][code] = rec["main_net_in"][i]
    for code, rec in globald.items():
        for i, d in enumerate(rec["dates"]):
            day = index.setdefault(d, {"sector_close": {},
                                       "sector_flow": {},
                                       "global": {}})
            day["global"][code] = rec["close"][i]
    if index:
        _save_index(index)
    return index


def day_snapshot(day):
    """某日市场数据快照(回测/实盘共用, asof 语义)。
    day: "YYYY-MM-DD" / "YYYYMMDD"。无 → {"sector_close": {}, ...}。"""
    if len(day) == 8:
        day = "%s-%s-%s" % (day[:4], day[4:6], day[6:8])
    index = _load_index()
    snap = index.get(day)
    if snap:
        return snap
    # 退化: 直接查缓存
    return {"sector_close": {}, "sector_flow": {}, "global": {}}


# ---------------------------------------------------------------- CLI

def build_cli():
    import argparse
    ap = argparse.ArgumentParser(description="市场数据层采集(板块/K线/资金流/全球指数)")
    ap.add_argument("--build-sectors", action="store_true",
                    help="采集行业板块K线+资金流(增量)")
    ap.add_argument("--build-global", action="store_true",
                    help="采集全球指数历史K线(增量)")
    ap.add_argument("--build-index", action="store_true",
                    help="从缓存构建按日索引(查询加速)")
    ap.add_argument("--beg", default=BACKFILL_BEG,
                    help="回填起点 YYYYMMDD(默认 %s)" % BACKFILL_BEG)
    ap.add_argument("--stats", action="store_true", help="显示缓存统计")
    ap.add_argument("--day", default="", help="查询某日快照 YYYYMMDD")
    args = ap.parse_args()

    def prog(done, total):
        sys.stdout.write("\r  采集 %d/%d" % (done, total))
        sys.stdout.flush()

    if args.build_sectors:
        r = build_sector_cache(beg=args.beg, progress=prog)
        print("\n板块采集完成:", r)
        if args.build_index or True:
            idx = build_index()
            print("按日索引: %d 天" % len(idx))
        return
    if args.build_global:
        g = build_global_cache(beg=args.beg)
        print("全球指数采集完成: %d 个" % len(g))
        if args.build_index or True:
            idx = build_index()
            print("按日索引: %d 天" % len(idx))
        return
    if args.build_index:
        idx = build_index()
        print("按日索引: %d 天" % len(idx))
        return
    cache = _load_cache()
    if args.stats or not args.day:
        print("板块数:", len(cache.get("sectors") or {}))
        print("K线板块数:", len(cache.get("kline") or {}))
        print("资金流板块数:", len(cache.get("flow") or {}))
        print("全球指数:", {k: len(v.get("dates") or [])
                            for k, v in (cache.get("global") or {}).items()})
        return
    snap = day_snapshot(args.day)
    print("%s 快照: 板块收盘 %d, 板块资金 %d, 全球 %d" % (
        args.day, len(snap["sector_close"]), len(snap["sector_flow"]),
        len(snap["global"])))


if __name__ == "__main__":
    build_cli()