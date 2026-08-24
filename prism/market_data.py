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


# ---------------------------------------------------------------- 申万指数通道

class SWIndexFeed:
    """申万行业指数数据源(akshare, 非东财) — 东财封禁时的板块K线备用。

    用官方申万行业指数替代东财板块指数:
      * sw_index_first_info()    31个一级行业 [{code, name, count}]
      * sw_index_second_info()   131个二级行业
      * index_hist_sw(code)      指数日K(1999年至今, 含开高低收/成交额)

    ak = 注入的 akshare 模块(默认真实, 测试注入假实现免网络)。
    注意: 申万指数无资金流 → flow 段留空(fail-open)。"""

    def __init__(self, ak=None):
        self.ak = ak

    def _ak(self):
        if self.ak is None:
            import akshare  # 延迟导入, 未安装时仅 SW 通道不可用
            self.ak = akshare
        return self.ak

    def fetch_sector_list(self):
        """申万一级行业 → [{code, name}]。code 如 '801010'(去 .SI 后缀)。失败 → 抛。"""
        try:
            df = self._ak().sw_index_first_info()
        except Exception as e:
            raise MarketDataError("申万行业列表失败: %r" % e)
        out = []
        if df is None or len(df) == 0:
            return out
        # 列: 行业代码 / 行业名称 / 成分个数
        code_col = df.columns[0]
        name_col = df.columns[1]
        for _, row in df.iterrows():
            code = str(row[code_col]).split(".")[0]
            if code.isdigit():
                out.append({"code": code, "name": str(row[name_col])})
        return out

    def fetch_sector_kline(self, code, beg=None, end=None):
        """申万指数日K → [{date, close, amount}]。升序。失败 → 抛。

        beg/end 支持 YYYYMMDD 或 YYYY-MM-DD(内部统一为 YYYY-MM-DD 比较)。"""
        beg_fmt = _norm_day(beg)
        end_fmt = _norm_day(end)
        try:
            df = self._ak().index_hist_sw(symbol=code, period="day")
        except Exception as e:
            raise MarketDataError("申万指数 %s 日K失败: %r" % (code, e))
        out = []
        if df is None or len(df) == 0:
            return out
        # 列: 指数代码 / 日期(date) / 开盘 / 收盘 / 最高 / 最低 / 成交量 / 成交额
        import datetime as _dt
        date_col = df.columns[1]
        close_col = df.columns[3]
        amount_col = df.columns[7]
        for _, row in df.iterrows():
            d = row[date_col]
            try:
                d_str = d.strftime("%Y-%m-%d") if isinstance(
                    d, (_dt.date, _dt.datetime)) else str(d)[:10]
            except Exception:
                continue
            if len(d_str) != 10:
                continue
            if beg_fmt and d_str < beg_fmt:
                continue
            if end_fmt and d_str > end_fmt:
                continue
            out.append({"date": d_str, "close": _f(row[close_col]),
                        "amount": _f(row[amount_col])})
        return out

    def fetch_sector_cons(self, code):
        """申万指数成分股 → [{code, name, sector}]。code 需带后缀如 '801010.SI'。

        返回该指数的成分股列表(个股→所属申万行业的映射来源)。失败 → 抛。"""
        try:
            df = self._ak().sw_index_third_cons(symbol=code)
        except Exception as e:
            raise MarketDataError("申万指数 %s 成分股失败: %r" % (code, e))
        out = []
        if df is None or len(df) == 0:
            return out
        # 列: 序号 / 股票代码 / 股票名称 / 纳入时间 / 所属行业
        code_col = df.columns[1]
        name_col = df.columns[2]
        sector_col = df.columns[4]
        for _, row in df.iterrows():
            c = str(row[code_col]).strip()
            if not c:
                continue
            out.append({"code": c, "name": str(row[name_col]),
                        "sector": str(row[sector_col])})
        return out


# ---------------------------------------------------------------- 新浪美股通道

class SinaUSIndexFeed:
    """新浪美股指数数据源(akshare, 非东财) — 东财封禁时的全球指数备用。

    用新浪美股官方指数替代东财全球指数:
      * .IXIC 纳斯达克 / .INX 标普500 / .DJI 道琼斯
      * index_us_stock_sina(symbol) → 日K(2004年至今, 含开高低收/量/额)

    ak = 注入的 akshare 模块(默认真实, 测试注入假实现免网络)。
    注意: 美债收益率(TNX)/美元指数(UDI)新浪源不可用, 这两个字段留空。"""

    def __init__(self, ak=None):
        self.ak = ak

    def _ak(self):
        if self.ak is None:
            import akshare  # 延迟导入
            self.ak = akshare
        return self.ak

    def fetch_global_kline(self, sina_symbol, beg=None, end=None):
        """新浪美股指数日K → [{date, close}]。升序。失败 → 抛。

        beg/end 支持 YYYYMMDD 或 YYYY-MM-DD(内部统一为 YYYY-MM-DD 比较)。"""
        beg_fmt = _norm_day(beg)
        end_fmt = _norm_day(end)
        try:
            df = self._ak().index_us_stock_sina(symbol=sina_symbol)
        except Exception as e:
            raise MarketDataError("新浪指数 %s 日K失败: %r" % (sina_symbol, e))
        out = []
        if df is None or len(df) == 0:
            return out
        import datetime as _dt
        for _, row in df.iterrows():
            d = row["date"]
            try:
                d_str = d.strftime("%Y-%m-%d") if isinstance(
                    d, (_dt.date, _dt.datetime)) else str(d)[:10]
            except Exception:
                continue
            if len(d_str) != 10:
                continue
            if beg_fmt and d_str < beg_fmt:
                continue
            if end_fmt and d_str > end_fmt:
                continue
            out.append({"date": d_str, "close": _f(row["close"])})
        return out


# ---------------------------------------------------------------- FRED 通道

def _load_fred_api_key():
    """FRED API key 读取顺序: 环境变量 FRED_API_KEY > 项目根 .env 文件。

    .env 格式: FRED_API_KEY=xxxx (一行)。.env 已被 .gitignore, 不会泄露。"""
    import os
    key = os.environ.get("FRED_API_KEY")
    if key:
        return key.strip()
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("FRED_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return None


class FREDFeed:
    """美联储 FRED 官方数据源 — 美债收益率/VIX 等宏观序列。

    免费(需注册 api key, fred.stlouisfed.org/docs/api/api_key.html):
      * series_id=DGS10  10年期美债收益率(文档"宏观因子"的日频数据)
      * series_id=VIXCLS  VIX恐慌指数(文档"市场情绪因子")

    调用: FRED API /fred/series/observations?series_id=...&api_key=...
    注入: http_get 可注入(测试用假实现)。key 缺失 → MarketDataError。"""

    URL = "https://api.stlouisfed.org/fred/series/observations"

    # series_id → 缓存 key(与 global 段一致): DGS10→US10Y, VIXCLS→VIX
    SERIES_MAP = {"DGS10": "US10Y", "VIXCLS": "VIX"}

    def __init__(self, http_get=None, timeout=10.0, api_key=None):
        self.http_get = http_get or _default_http_get
        self.timeout = timeout
        self.api_key = api_key or _load_fred_api_key()

    def _get_observations(self, series_id, beg=None, end=None):
        """拉 FRED 序列观察值 → [{date, value}]。key 缺失/失败 → 抛。"""
        if not self.api_key:
            raise MarketDataError("FRED API key 缺失: 设置环境变量 "
                                  "FRED_API_KEY 或项目根 .env 文件")
        params = {"series_id": series_id, "api_key": self.api_key,
                  "file_type": "json", "observation_start": beg or "2026-01-01"}
        try:
            resp = self.http_get(self.URL, params=params,
                                 headers={"User-Agent": "Mozilla/5.0"},
                                 timeout=self.timeout)
            raise_for_status = getattr(resp, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            data = resp.json()
        except MarketDataError:
            raise
        except Exception as e:
            raise MarketDataError("FRED %s 请求失败: %r" % (series_id, e))
        obs = (data or {}).get("observations") or []
        out = []
        for o in obs:
            d = (o or {}).get("date") or ""
            v = (o or {}).get("value")
            if not d or v in (None, "", "."):
                continue
            if end and d > end:
                continue
            try:
                out.append({"date": d, "close": float(v)})
            except (TypeError, ValueError):
                continue
        return out

    def fetch_global_kline(self, series_id, beg=None, end=None):
        """FRED 序列日频 → [{date, close}]。升序。失败 → 抛。

        供 build_global_cache(source="fred") 使用; beg/end 支持两种格式。"""
        beg_fmt = _norm_day(beg)
        end_fmt = _norm_day(end)
        return self._get_observations(series_id, beg=beg_fmt, end=end_fmt)


def _norm_day(s):
    """'YYYYMMDD' → 'YYYY-MM-DD'; 已是 YYYY-MM-DD 原样; 其他 → None。"""
    if not s:
        return None
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
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
                       progress=None, source="eastmoney", rebuild=False):
    """采集行业板块列表 + 历史K线 + 资金流历史, 增量落盘。

    source: "eastmoney"(默认, 东财板块+资金流) / "sw"(申万行业指数,
    东财封禁时的备用; 申万无资金流, flow 段留空)。
    rebuild: True 时清空已有 kline/flow 段后全量重采(用于切换数据源:
    申万体系 → 东财体系, 避免两套板块代码混在同一缓存)。

    返回 {"sectors": n, "kline_codes": n, "flow_codes": n}。
    缓存结构: {
      "sectors": {code: {"name": ...}},
      "kline":   {code: {"dates": [...], "close": [...], "amount": [...]}},
      "flow":    {code: {"dates": [...], "main_net_in": [...]}},
    }
    """
    if source == "sw":
        feed = probe or SWIndexFeed()
        flow_enabled = False
    else:
        feed = probe or EastMoneyProbe()
        flow_enabled = True
    end = end or date.today().strftime("%Y%m%d")
    cache = _load_cache()
    sectors = cache.get("sectors") or {}
    if rebuild:
        # 切换数据源: 清空旧体系 K线/资金流(避免申万801xxx与东财BKxxx混杂)
        kline = {}
        flow = {}
        logger.info("rebuild=True: 清空 kline/flow, 按 %s 全新采集", source)
    else:
        kline = cache.get("kline") or {}
        flow = cache.get("flow") or {}

    # 1. 板块列表(增量保留名称, 失败 → 抛错, 不能让任务"看起来成功")
    try:
        lst = feed.fetch_sector_list()
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
    has_fetch_kline = hasattr(feed, "fetch_kline")
    has_fetch_sector_kline = hasattr(feed, "fetch_sector_kline")
    for code, info in sectors.items():
        need_kline = code not in kline
        need_flow = flow_enabled and code not in flow
        if need_kline or need_flow:
            try:
                if source == "sw" and has_fetch_sector_kline:
                    kl = feed.fetch_sector_kline(code)
                elif has_fetch_kline:
                    secid = "90.%s" % code
                    kl = feed.fetch_kline(secid, beg, end,
                                          fields2="f51,f53,f57")
                else:
                    kl = None
                if flow_enabled and hasattr(feed, "fetch_sector_flow"):
                    fl = feed.fetch_sector_flow("90.%s" % code)
                else:
                    fl = None
            except MarketDataError as e:
                logger.warning("板块 %s 采集失败: %r (跳过)", code, e)
                done += 1
                if progress:
                    progress(done, total)
                continue
            if kl and need_kline:
                kline[code] = {"dates": [r["date"] for r in kl],
                               "close": [r["close"] for r in kl],
                               "amount": [r.get("amount") for r in kl]}
            if fl and need_flow:
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


def build_global_cache(probe=None, beg=BACKFILL_BEG, end=None,
                       source="eastmoney"):
    """采集全球指数历史K线, 落盘到同一缓存文件的 "global" 段。

    source: "eastmoney"(默认, 东财全球指数) / "sina"(新浪美股指数,
    东财封禁时的备用; 美债/美元指数新浪源不可用, 只采纳指/标普/道指)。

    返回 {code: {"dates": [...], "close": [...]}}。
    """
    if source == "sina":
        feed = probe or SinaUSIndexFeed()
        # 新浪可用的: .IXIC 纳指 / .INX 标普 / .DJI 道指 (无 .UDI 美元指数)
        index_map = [(".IXIC", "NDX", "纳斯达克"),
                     (".INX", "SPX", "标普500"),
                     (".DJI", "DJIA", "道琼斯")]
    elif source == "fred":
        feed = probe or FREDFeed()
        # FRED series_id → (缓存key, 名称): 美债收益率/VIX
        index_map = [("DGS10", "US10Y", "10年美债收益率"),
                     ("VIXCLS", "VIX", "VIX恐慌指数")]
    else:
        feed = probe or EastMoneyProbe()
        index_map = [(s, c, n) for s, c, n in GLOBAL_INDICES]
    end = end or date.today().strftime("%Y%m%d")
    cache = _load_cache()
    globald = cache.get("global") or {}
    for secid, code, name in index_map:
        try:
            kl = feed.fetch_global_kline(secid, beg, end)
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

def build_sector_map(feed=None, progress=None):
    """采集全部申万一级行业成分股 → 个股→行业代码映射, 落盘缓存 "sector_map" 段。

    返回 {"stocks": n}。映射结构: {"sector_map": {code6: {"sector": "801010",
    "name": ...}}} (code6 如 '000019' 转 '000019.SZ' 统一后缀)。
    失败的单行业跳过(增量容错)。
    """
    feed = feed or SWIndexFeed()
    cache = _load_cache()
    smap = cache.get("sector_map") or {}
    # 行业列表(优先用已有缓存, 避免重复拉)
    if not smap:
        try:
            lst = feed.fetch_sector_list()
        except MarketDataError as e:
            raise MarketDataError("行业列表失败, 无法构建映射: %r" % e)
    else:
        lst = [{"code": c, "name": i.get("name") or ""}
               for c, i in (cache.get("sectors") or {}).items()]
    total = len(lst)
    done = 0
    for s in lst:
        secid = "%s.SI" % s["code"]
        try:
            cons = feed.fetch_sector_cons(secid)
        except MarketDataError as e:
            logger.warning("行业 %s 成分股失败: %r (跳过)", s["code"], e)
            done += 1
            if progress:
                progress(done, total)
            continue
        for st in cons:
            c6 = _code6(st["code"])
            if c6:
                smap[c6] = {"sector": s["code"], "name": st.get("name") or ""}
        done += 1
        if done % 10 == 0:
            _save_cache({"sector_map": smap})
        if progress:
            progress(done, total)
    if smap:
        cache = _load_cache()
        cache["sector_map"] = smap
        # sectores 段同时刷新名称(与板块K线对齐)
        _save_cache({"sector_map": smap})
    return {"stocks": len(smap)}


def stock_sector(code):
    """个股(带后缀/裸代码) → 所属申万一级行业代码。无 → None。"""
    cache = _load_cache()
    smap = cache.get("sector_map") or {}
    c6 = _code6(code)
    if not c6:
        return None
    rec = smap.get(c6)
    return (rec or {}).get("sector")


def _code6(s):
    """'000019.SZ' / '000019' → '000019'。无法解析 → None。"""
    s = str(s).strip().upper()
    if "." in s:
        s = s.split(".")[0]
    if len(s) == 6 and s.isdigit():
        return s
    return None


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
    ap.add_argument("--build-sector-map", action="store_true",
                    help="构建个股→申万行业映射(成分股采集)")
    ap.add_argument("--beg", default=BACKFILL_BEG,
                    help="回填起点 YYYYMMDD(默认 %s)" % BACKFILL_BEG)
    ap.add_argument("--source", default="eastmoney",
                    choices=["eastmoney", "sw", "sina", "fred"],
                    help="板块/指数数据源: eastmoney(默认) / sw(申万) / sina(新浪美股)")
    ap.add_argument("--rebuild", action="store_true",
                    help="清空 kline/flow 后全量重采(切换数据源时用, 避免混杂)")
    ap.add_argument("--stats", action="store_true", help="显示缓存统计")
    ap.add_argument("--day", default="", help="查询某日快照 YYYYMMDD")
    args = ap.parse_args()

    def prog(done, total):
        sys.stdout.write("\r  采集 %d/%d" % (done, total))
        sys.stdout.flush()

    if args.build_sectors:
        r = build_sector_cache(beg=args.beg, progress=prog,
                               source=args.source, rebuild=args.rebuild)
        print("\n板块采集完成(source=%s):" % args.source, r)
        if args.build_index or True:
            idx = build_index()
            print("按日索引: %d 天" % len(idx))
        return
    if args.build_global:
        g = build_global_cache(beg=args.beg, source=args.source)
        print("全球指数采集完成(source=%s): %d 个" % (args.source, len(g)))
        if args.build_index or True:
            idx = build_index()
            print("按日索引: %d 天" % len(idx))
        return
    if args.build_index:
        idx = build_index()
        print("按日索引: %d 天" % len(idx))
        return
    if args.build_sector_map:
        r = build_sector_map(progress=prog)
        print("\n个股→行业映射完成: %d 只" % r["stocks"])
        print("示例查询: 600519 →", stock_sector("600519"))
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