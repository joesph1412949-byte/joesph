# -*- coding: utf-8 -*-
"""东方财富涨停池数据源：为 N1/N3/N4 提供真实市场数据（东财公开涨停池 API）。
网络模块：真实使用 requests；测试注入假 http_get 返回罐装 JSON 或抛异常，绝不发真实请求。
N1(今日涨停 vs 近5日均值) / N3(昨日涨停股今日平均涨幅) / N4(最高连板数)。

交易日启发式（文档化）：不调用单独的日历接口，而是从"昨天"起逐日历日回溯，
响应的 data.pool != null 即视为一个交易日（空池计 0 家）；data.pool == null（非交易日）
或请求失败 → 跳过该日历日继续往前，直到凑齐 5 个交易日或超过回溯上限。"""
import logging
from datetime import date, timedelta

try:
    import requests
except Exception:  # pragma: no cover - 极少数环境无 requests
    requests = None

logger = logging.getLogger(__name__)


class EastMoneyError(Exception):
    """东财数据获取失败（网络/解析/超时等）。"""
    pass


def _default_http_get(url, params=None, headers=None, timeout=None):
    """默认真实 HTTP 实现：requests.get。返回 Response-like（有 .json()）。"""
    if requests is None:
        raise EastMoneyError("requests 未安装，无法访问东财涨停池")
    return requests.get(url, params=params, headers=headers, timeout=timeout)


class EastMoneyFeed:
    """东财涨停池接口封装。
    http_get: 可注入的 callable(url, params, headers, timeout) -> Response-like(.json())。
    测试注入假实现（罐装 JSON 或抛异常），真实使用默认 requests.get。
    """

    URL = "https://push2ex.eastmoney.com/getTopicZTPool"
    PARAMS = {
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": 0,
        "pagesize": 1000,
        "sort": "fbt:asc",
    }
    HEADERS = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
        "Referer": "http://quote.eastmoney.com/",
    }

    def __init__(self, http_get=None, timeout=5.0):
        self.http_get = http_get or _default_http_get
        self.timeout = timeout

    # ---------- 低层：原始响应 ----------
    def _get_pool_data(self, date_yyyymmdd):
        """返回 data.pool 列表；data 缺失 / pool==null（非交易日）→ None。
        请求失败 / 响应解析失败 → 抛 EastMoneyError。"""
        try:
            params = dict(self.PARAMS, date=date_yyyymmdd)
            resp = self.http_get(self.URL, params=params,
                                 headers=self.HEADERS, timeout=self.timeout)
        except Exception as e:
            raise EastMoneyError("东财涨停池请求失败(%s): %r" % (date_yyyymmdd, e))
        try:
            raise_for_status = getattr(resp, "raise_for_status", None)
            if callable(raise_for_status):
                raise_for_status()
            data = resp.json()
        except Exception as e:
            raise EastMoneyError("东财涨停池响应解析失败(%s): %r" % (date_yyyymmdd, e))
        data = data.get("data") if isinstance(data, dict) else None
        if data is None:
            return None
        pool = data.get("pool")
        if pool is None:
            return None
        return pool if isinstance(pool, list) else []

    @staticmethod
    def _pool_to_stocks(pool):
        """data.pool 条目 → [{code, name, boards, theme}]。
        boards = lbc(连板数, 东财真实字段); theme = hybk(题材/板块, 东财真实字段)。"""
        out = []
        for item in pool or []:
            if not isinstance(item, dict):
                continue
            code = item.get("c") or item.get("code")
            if not code:
                continue
            name = item.get("n") or item.get("name") or ""
            boards = item.get("lbc") or 0
            theme = item.get("hybk") or item.get("hyb") or ""
            out.append({"code": str(code), "name": str(name),
                        "boards": int(boards), "theme": str(theme)})
        return out

    @staticmethod
    def aggregate_by_theme(stocks):
        """按题材(hybk)聚合涨停池 → 识别主线题材。

        返回按 涨停家数降序、再按最高连板降序 排序的列表:
        [{theme, count, max_boards, codes}]。无题材(theme 为空)的股票聚合到
        "未知" 桶, 排在最后(主线识别只看有题材的)。"""
        buckets = {}
        for s in stocks or []:
            theme = (s.get("theme") or "").strip() or "未知"
            b = buckets.setdefault(theme, {"theme": theme, "count": 0,
                                           "max_boards": 0, "codes": []})
            b["count"] += 1
            b["max_boards"] = max(b["max_boards"], int(s.get("boards") or 0))
            b["codes"].append(s.get("code"))
        result = [b for t, b in buckets.items() if t != "未知"]
        result.sort(key=lambda b: (b["count"], b["max_boards"]), reverse=True)
        unknown = buckets.get("未知")
        if unknown:
            result.append(unknown)
        return result

    # ---------- 公开接口 ----------
    def fetch_limit_up_pool(self, date_yyyymmdd):
        """获取指定日期的涨停池。返回 [{code, name, boards}]。
        非交易日 / 空数据 → []；任何失败（HTTP/解析/超时/网络）→ 抛 EastMoneyError。"""
        pool = self._get_pool_data(date_yyyymmdd)
        if pool is None:
            return []
        return self._pool_to_stocks(pool)

    def get_market_stats(self):
        """尽力而为，绝不抛异常。返回 {"daily_counts","yesterday_codes","yesterday_boards","max_boards","today_theme_map","top_themes"} 或 None。
        - daily_counts: 今天之前最近 5 个交易日（回溯算法）的涨停家数，近→远。
          凑齐 <3 个交易日 → 返回 None（不可用）。
        - yesterday_codes: 最近一个交易日的涨停代码列表。
        - yesterday_boards: 最近一个交易日中连板数≥2 的代码列表（供 N4 晋级率计算）。
        - max_boards: 今日涨停池的最高连板数（今日非交易日/空池 → 0）。
        - today_theme_map: 今日涨停池 裸代码 → 题材(hybk), 供 F4/S6 题材共振。
        - top_themes: 今日主线题材列表(按涨停家数/连板高度排序), 供市场环境展示。
        任一步失败（EastMoneyError）→ 返回 None，调用方回退到代理算法。"""
        today = date.today()
        try:
            today_pool = self._get_pool_data(today.strftime("%Y%m%d"))
        except EastMoneyError:
            return None
        today_stocks = self._pool_to_stocks(today_pool or [])
        max_boards = max((s["boards"] for s in today_stocks), default=0)
        today_theme_map = {s["code"]: s["theme"] for s in today_stocks if s.get("theme")}
        top_themes = self.aggregate_by_theme(today_stocks)

        daily_counts = []
        yesterday_codes = []
        yesterday_boards = []
        first_day_found = False
        day = today - timedelta(days=1)
        # 回溯上限：覆盖春节/十一等长假，同时防止无限循环
        for _ in range(20):
            if len(daily_counts) >= 5:
                break
            try:
                pool = self._get_pool_data(day.strftime("%Y%m%d"))
            except EastMoneyError:
                day -= timedelta(days=1)
                continue
            if pool is None:  # 非交易日（data.pool==null）→ 跳过
                day -= timedelta(days=1)
                continue
            stocks = self._pool_to_stocks(pool)
            daily_counts.append(len(stocks))
            # 最近一个交易日（即昨天往前的第一个交易日）的代码。用布尔标志而非列表真值:
            # 若最近交易日空池(0家), 需让 yesterday_codes 保持 [] → N3 走兜底, 不能误取更早一天。
            if not first_day_found:
                yesterday_codes = [s["code"] for s in stocks]
                # 连板≥2 的代码列表 → N4 晋级率(昨日连板股今日继续涨停占比)
                yesterday_boards = [s["code"] for s in stocks if s["boards"] >= 2]
                first_day_found = True
            day -= timedelta(days=1)

        if len(daily_counts) < 3:
            return None
        return {
            "daily_counts": daily_counts,
            "yesterday_codes": yesterday_codes,
            "yesterday_boards": yesterday_boards,
            "max_boards": max_boards,
            "today_theme_map": today_theme_map,
            "top_themes": top_themes,
        }
