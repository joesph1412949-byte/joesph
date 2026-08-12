# -*- coding: utf-8 -*-
"""东方财富个股因子: 自动计算原手填因子 Y1/Y5/F7/Y7/S5/Y6/Y2。
复用 eastmoney.py 的 http_get 注入模式(测试注入假实现, 绝不发真实请求)。
每个因子一方法; 任何失败 → 该因子跳过(fail-open), 调用方回落手动输入。"""
import json
import logging
from datetime import date, datetime, timedelta

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

logger = logging.getLogger(__name__)

# ---- 可调参数 ----
Y1_MAX_FLOAT_MV = 80e8      # 小市值: 流通市值 < 80亿
Y5_MIN_CONCEPTS = 3         # 多概念: 概念标签 >= 3
RECENT_DAYS = 5             # "近 N 日" 窗口(题材新颖/事件/龙虎榜)

# 利好关键词(公告 → Y6 事件催化)
EVENT_KEYWORDS = ["预增", "中标", "重组", "增持", "回购", "签约", "获批"]


def _default_http_get(url, params=None, headers=None, timeout=None):
    if requests is None:
        raise RuntimeError("requests 未安装")
    return requests.get(url, params=params, headers=headers, timeout=timeout)


def _today_key():
    return date.today().strftime("%Y%m%d")


def _code6(code):
    """取 6 位证券代码: '000001.SZ' → '000001'; 裸 '000001' → '000001'。"""
    return str(code).strip().split(".")[0][:6]


def _secid(code):
    """东财 secid: 深/北(00/30/8/4)→'0.code', 沪(6)→'1.code'。按后缀或首位推断。"""
    s = str(code).strip().upper()
    if s.endswith(".SH"):
        mkt = "1"
    elif s.endswith((".SZ", ".BJ")):
        mkt = "0"
    else:
        mkt = "1" if _code6(s).startswith("6") else "0"
    return "%s.%s" % (mkt, _code6(s))


class FundamentalFeed:
    """东财个股因子。http_get 可注入(测试);cache_path 为 None 时禁用缓存。"""

    HEADERS = {"User-Agent": "Mozilla/5.0",
               "Referer": "http://quote.eastmoney.com/"}

    def __init__(self, http_get=None, cache_path="fundamental_cache.json",
                 timeout=5.0):
        self.http_get = http_get or _default_http_get
        self.cache_path = cache_path
        self.timeout = timeout
        self._cache = self._load_cache() if cache_path else {}

    # ---------- 缓存 ----------
    def _load_cache(self):
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cache(self):
        if not self.cache_path:
            return
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, ensure_ascii=False)

    def _cache_key(self, code):
        return "%s:%s" % (_today_key(), code)

    def _datacenter(self, report_name, filter_str):
        """datacenter-web 通用请求 → result.data 列表; 失败抛异常。"""
        resp = self.http_get(
            "https://datacenter-web.eastmoney.com/api/data/v1/get",
            params={"reportName": report_name, "columns": "ALL",
                    "filter": filter_str, "pageSize": 50, "pageNumber": 1},
            headers=self.HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        d = resp.json()
        return (d.get("result") or {}).get("data") or []

    def _ztpool(self, date_yyyymmdd):
        """getTopicZTPool → data.pool 列表; 非交易日(data.pool==null)/空 → None。
        按日缓存键 'ztpool:YYYYMMDD' 与各股票无关, 一天只请求一次(全股票共用);
        结果无论 list 还是 None(非交易日)都缓存, 避免每只股重复请求同日池。
        请求失败 / 解析失败 → 抛异常(不写缓存, 由 compute_for_stock 捕获, 该因子 fail-open)。"""
        cache_key = "ztpool:%s" % date_yyyymmdd
        if cache_key in self._cache:
            cached = self._cache[cache_key]
            return list(cached) if isinstance(cached, list) else None
        resp = self.http_get(
            "https://push2ex.eastmoney.com/getTopicZTPool",
            params={"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                    "Pageindex": 0, "pagesize": 1000, "sort": "fbt:asc",
                    "date": date_yyyymmdd},
            headers=self.HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        d = resp.json()
        data = d.get("data") if isinstance(d, dict) else None
        if data is None:
            self._cache[cache_key] = None
            return None
        pool = data.get("pool")
        if pool is None:
            self._cache[cache_key] = None
            return None
        result = pool if isinstance(pool, list) else []
        self._cache[cache_key] = result
        if self.cache_path:
            self._save_cache()
        return result

    def _hybk_history(self):
        """近 RECENT_DAYS-1 个交易日的全部涨停池 hybk 集合(按日缓存, 全股票共用)。
        从昨天起逐日历日回溯, data.pool==null(非交易日)→跳过该日历日继续, 上限 20 日;
        端点失败(HTTP/超时/解析)→ 直接抛异常(F7 fail-open, 不写缓存)。
        键 'hybk_history:YYYYMMDD' 与各股票无关, 一天只算一次, 避免每只股各回溯 N 次。"""
        today_key = _today_key()
        cache_key = "hybk_history:%s" % today_key
        if cache_key in self._cache:
            return set(self._cache[cache_key])
        hist = set()
        days = 0
        day = date.today() - timedelta(days=1)
        for _ in range(20):
            if days >= RECENT_DAYS - 1:  # 已凑够 N-1 个交易日
                break
            try:
                pool = self._ztpool(day.strftime("%Y%m%d"))
            except Exception as e:
                # 端点失败(HTTP/超时/解析) ≠ 非交易日(data.pool==null): 失败日不能当
                # "无该题材", 否则该日题材会被误判"新颖"(F7 假阳性)。上抛给
                # compute_for_stock → F7 fail-open(不出现在输出); 此处不写缓存,
                # 绝不让部分/空历史集合被缓存。
                logger.warning("F7 历史涨停池 %s 获取失败, F7 跳过(fail-open): %r",
                               day.strftime("%Y%m%d"), e)
                raise
            if pool is None:  # 非交易日(data.pool==null) → 跳过
                day -= timedelta(days=1)
                continue
            for item in pool:
                if isinstance(item, dict) and item.get("hybk"):
                    hist.add(item["hybk"])
            days += 1
            day -= timedelta(days=1)
        self._cache[cache_key] = sorted(hist)
        if self.cache_path:
            self._save_cache()
        return hist

    # ---------- 各因子 ----------
    def _small_cap(self, float_mv):
        """Y1 小市值: 流通市值 < Y1_MAX_FLOAT_MV (纯计算, 无网络)。"""
        if not float_mv:
            return None
        hit = float_mv < Y1_MAX_FLOAT_MV
        return {"score": 1 if hit else 0,
                "note": "流通市值 %.0f亿 < 80亿" % (float_mv / 1e8) if hit
                else "流通市值 %.0f亿 >= 80亿" % (float_mv / 1e8)}

    def _concepts(self, code):
        """Y5 多概念: 概念标签数 >= Y5_MIN_CONCEPTS。返回 {"score","note"} 或 None。"""
        resp = self.http_get(
            "https://push2.eastmoney.com/api/qt/slist/get",
            params={"spt": "3", "pi": 0, "po": 1, "np": 1, "fltt": 2,
                    "invt": 2, "fid": "f3", "secid": _secid(code),
                    "fields": "f12,f13,f14"},
            headers=self.HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        d = resp.json()
        data = d.get("data") or {}
        diff = data.get("diff") or []
        if isinstance(diff, dict):  # 单元素接口可能返回 dict 而非 list
            count = 1
        else:
            count = len(diff) if isinstance(diff, list) else 0
        # po=1 时 diff 可能是分页抽样, data.total 才是板块总数 → 取更大者
        total = data.get("total")
        if total is not None:
            count = max(count, int(total))
        hit = count >= Y5_MIN_CONCEPTS
        return {"score": 1 if hit else 0,
                "note": "概念标签 %d 个 %s %d" % (count, ">=" if hit else "<",
                                              Y5_MIN_CONCEPTS)}

    def _novel_concept(self, code):
        """F7 题材新颖: 今日涨停池该股 hybk 不在近 RECENT_DAYS-1 个交易日的 hybk 集合 → 1。
        今日池无该股 / 无 hybk → None(fail-open); 历史集合按日缓存(hybk_history:YYYYMMDD)。"""
        code6 = _code6(code)
        pool = self._ztpool(_today_key())   # 今日涨停池
        if pool is None:
            return None
        hybk = None
        for item in pool:
            if not isinstance(item, dict):
                continue
            if str(item.get("c") or item.get("code")) == code6:
                hybk = item.get("hybk") or item.get("hyb")
                break
        if not hybk:
            return None
        hist = self._hybk_history()
        novel = hybk not in hist
        return {"score": 1 if novel else 0,
                "note": ("今日题材 %s 为近 %d 日首次出现(新颖)" % (hybk, RECENT_DAYS))
                if novel else "今日题材 %s 近 %d 日已出现过" % (hybk, RECENT_DAYS)}

    def _dragon_tiger(self, code):
        """Y7 游资现身: 近 RECENT_DAYS 日龙虎榜有该股且净买入 >0 → 1。返回 {"score","note"}。
        filter 用 SECURITY_CODE(6 位无后缀, 非 SECUCODE); 日期在 Python 里过滤(更稳)。"""
        rows = self._datacenter("RPT_DAILYBILLBOARD_DETAILSNEW",
                                '(SECURITY_CODE="%s")' % _code6(code))
        cutoff = date.today() - timedelta(days=RECENT_DAYS - 1)
        for row in rows:
            if not isinstance(row, dict):
                continue
            amt = row.get("BILLBOARD_NET_AMT")
            try:
                if amt is None or float(amt) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            td = row.get("TRADE_DATE") or ""
            try:
                row_date = datetime.strptime(str(td)[:10], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue
            if row_date >= cutoff:
                return {"score": 1,
                        "note": "近 %d 日龙虎榜净买入 %.0f 元" % (RECENT_DAYS, float(amt))}
        return {"score": 0, "note": "近 %d 日无龙虎榜净买入" % RECENT_DAYS}

    def _financing(self, code):
        """S5 机构流入: 融资余额近 RECENT_DAYS 日增长。
        探针实测拿不到可用的融资接口 → 始终返回 None(fail-open), 保持手填。"""
        return None

    def _shareholders(self, code):
        """Y2 筹码干净: 任一股东户数记录 HOLDER_NUM_CHANGE < 0(户数环比下降) → 1。"""
        rows = self._datacenter("RPT_HOLDERNUMLATEST",
                                '(SECURITY_CODE="%s")' % _code6(code))
        for row in rows:
            if not isinstance(row, dict):
                continue
            chg = row.get("HOLDER_NUM_CHANGE")
            try:
                if chg is None or float(chg) >= 0:
                    continue
            except (TypeError, ValueError):
                continue
            return {"score": 1,
                    "note": "股东户数环比减少 %.0f 户" % abs(float(chg))}
        return {"score": 0, "note": "股东户数环比未下降"}

    def _event(self, code):
        """Y6 事件催化: 近 RECENT_DAYS 日公告 title 命中 EVENT_KEYWORDS → 1。
        日期(notice_date)在 Python 里过滤; 关键词命中即判定。"""
        resp = self.http_get(
            "https://np-anotice-stock.eastmoney.com/api/security/ann",
            params={"sr": "-1", "page_size": "20", "page_index": "1",
                    "ann_type": "A", "client_source": "web", "page_number": "1",
                    "f_node": "0", "s_node": "0", "stock_list": _code6(code)},
            headers=self.HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        d = resp.json()
        items = (d.get("data") or {}).get("list") or []
        cutoff = date.today() - timedelta(days=RECENT_DAYS - 1)
        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("title") or ""
            if not any(k in title for k in EVENT_KEYWORDS):
                continue
            nd = item.get("notice_date") or ""
            try:
                nd_date = datetime.strptime(str(nd)[:10], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                continue
            if nd_date >= cutoff:
                return {"score": 1,
                        "note": "近 %d 日公告命中事件: %s" % (RECENT_DAYS, title[:20])}
        return {"score": 0, "note": "近 %d 日无事件催化公告" % RECENT_DAYS}

    # ---------- 公开入口 ----------
    def compute_for_stock(self, code, float_mv=None):
        """返回 {因子名: {"score":0/1, "note":str}}。单因子失败→跳过(不抛)。
        Y1 纯计算; Y5/F7/Y7/S5/Y6/Y2 逐个调用, 任一异常被捕获并 log。"""
        key = self._cache_key(code)
        if key in self._cache:
            return dict(self._cache[key])
        out = {}
        try:
            y1 = self._small_cap(float_mv)
            if y1:
                out["Y1"] = y1
        except Exception as e:
            logger.warning("东财因子 %s(%s) 计算失败, 回落手填: %r", "Y1", code, e)
        for name, fn in [("Y5", self._concepts), ("F7", self._novel_concept),
                         ("Y7", self._dragon_tiger), ("S5", self._financing),
                         ("Y2", self._shareholders), ("Y6", self._event)]:
            try:
                res = fn(code)
                if res is not None:
                    out[name] = res
            except Exception as e:
                logger.warning("东财因子 %s(%s) 计算失败, 回落手填: %r", name, code, e)
        self._cache[key] = out
        self._save_cache()
        return out
