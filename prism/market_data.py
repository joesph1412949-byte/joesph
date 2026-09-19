# -*- coding: utf-8 -*-
"""市场数据层 — 行业板块/美股指数/资金流的采集与本地缓存。

为 8.23 文档的板块强度/资金流向/美股映射因子提供数据地基:
  * 板块综合评分(行情/量能/资金/风险)、连阳天数、近10日涨幅 → 需行业板块历史K线
  * 主力资金净流入、ETF趋势            → 需板块资金流历史
  * 美股映射(纳指/标普/道指/美元指数)  → 需全球指数历史K线

设计对齐 prism/zt_history.py:
  * 采集结果落盘 pickle(.market_data_cache.pkl), 增量续传(已追平的板块跳过,
    尾部过期的重采——09-07 修复日期冻结);
  * 网络实现可注入(http_get), 离线测试用罐装 resp.

数据源(东财公开接口, 免费, 实测可用):
  * clist/get        行业板块列表(f12代码 f14名称)
  * stock/kline/get  板块历史K线(push2his, 可回溯任意日期)
  * fflow/daykline   板块资金流历史(push2his)
  * stock/kline/get(secid=100.NDX 等) 全球指数历史K线(push2his)

CLI 退出码(6 个 --build-* 开关统一; 与 `--help` epilog 同步, 改一处必改另一处):
  * 0 = 所有点名的段本次都真的取到/推进了数据;
  * 1 = 有点名的段真失败 —— 抛异常 / `build_sector_cache` 本次尝试的板块全失败 /
        `--build-benchmark` 三路全灭(kept_old, 有意保留的 fail-open 点名) /
        拿不到数据且**无旧缓存可留**;
  * 3 = 无失败但有段本次没推进(空数据; 旧缓存原样保留)—— 警告级, **绝不与 0 同码**。
  多个 --build-* 可同时给出: 全部按参数声明顺序执行(不再静默短路); 一段失败
  不连累其余段(全部照跑), 最后统一报码。
  ⚠️ 多条目段的"本次一条都没取到"在**有旧缓存可留**且采集函数不自报失败计数时
  判 3 而非 1 —— 它在返回值层面与"非交易日没有新数据"不可区分(global/futures
  没有新鲜度门, 每次都会尝试全部条目)。把"周末没新数据"判成失败会让盘后任务
  每周报灾难; 只在证据明确时判 1(09-19 A 批次口径)。
"""
import logging
import os
import pickle
import sys
import threading
import time
from datetime import date
from pathlib import Path

from shared.common import CACHE_DIR, replace_with_retry

try:
    import requests
except Exception:  # pragma: no cover - 极少数环境无 requests
    requests = None

logger = logging.getLogger(__name__)

# 缓存路径: 统一收在 runtime/cache/ (已 gitignore, 见 shared/common.py)
CACHE_PATH = CACHE_DIR / ".market_data_cache.pkl"

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
# 新浪美股符号(与东财 secid 不同名, 且无美元指数): secid → 新浪 symbol
_SINA_SYMBOLS = {"100.NDX": ".IXIC", "100.SPX": ".INX", "100.DJIA": ".DJI"}
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

    # ---------------- 行业板块列表 ----------------
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

    # ---------------- 历史K线 ----------------
    def fetch_kline(self, secid, beg, end, fields2="f51,f53,f57", lmt=None):
        """历史日K → [{date, close, amount?}]。升序。失败 → 抛。
        lmt: 只取最近 N 根(全球指数通道用, 不传则按 beg/end 区间)。"""
        params = {"secid": secid, "klt": 101, "fqt": 1,
                  "beg": beg, "end": end,
                  "fields1": "f1,f2,f3", "fields2": fields2}
        if lmt:
            params["lmt"] = lmt
        data = self._get_json(self.KLINE_URL, params)
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

    def fetch_global_kline(self, secid, beg, end, lmt=600):
        """全球指数历史K线 → [{date, close}]。升序(只取收盘, 无 amount)。"""
        return self.fetch_kline(secid, beg, end, fields2="f51,f53", lmt=lmt)

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

    # ---------------- 板块资金惯性(当日快照, 前向累积) ----------------
    def fetch_flow_rank(self, page_size=100):
        """全行业板块当日主力净流入(f62) → [{code, name, net_in}] 降序。
        f62 非数值(如'-')的行跳过。失败 → 抛出。"""
        items = []
        seen = 0
        pn = 1
        while True:
            data = self._get_json(self.CLIST_URL, {
                "pn": pn, "pz": page_size, "po": 1, "np": 1,
                "fltt": 2, "invt": 2, "fid": "f62",
                "fs": SECTOR_FS, "fields": "f12,f14,f62"})
            diff = ((data or {}).get("data") or {}).get("diff") or []
            seen += len(diff)
            for it in diff:
                if not isinstance(it, dict):
                    continue
                try:
                    net = float(it.get("f62"))
                except (TypeError, ValueError):
                    continue    # '-' 等无数据行
                items.append({"code": str(it.get("f12") or ""),
                              "name": str(it.get("f14") or ""),
                              "net_in": net})
            total = ((data or {}).get("data") or {}).get("total") or 0
            # ponytail: 断路按 diff 行数(seen)而非 items 数——'-'行被过滤后
            # items < total, 按 items 断路会多翻页把同一批行重复拉一遍
            if not diff or seen >= int(total):
                break
            pn += 1
        items.sort(key=lambda r: r["net_in"], reverse=True)
        return items

    def fetch_benchmark_kline(self, beg, end):
        """上证指数日K(secid=1.000001) → [{date, close, amount}]。失败 → 抛出。"""
        return self.fetch_kline("1.000001", beg, end, fields2="f51,f53,f57")


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

        取全量(增量门在 build_sector_cache 的 _tail_stale 缓存层做, 这里不过滤
        日期); beg/end 仅为签名兼容保留。"""
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

def _load_cache(strict=False):
    """读盘缓存。`strict=True` → 读失败原样抛出, 让调用方自己决定。

    "文件不存在/空" → 起点 {}, 覆盖无害(没有键可丢); 但**读失败**(坏 pkl / 外部
    截断 / IO 错)不等于空 —— 盘上明明有内容却被当空的, `_save_cache` 的读-改-写
    合并就退化成整文件覆盖, 磁盘上别的写者的段被整批抹掉。
    非 strict 保持 fail-open(坏文件当空), 但**留痕**: 5.9MB 缓存曾被无声清零(D2)。
    口径与 datasource/fundamental.py 的 I2b、prism/zt_history.py 一致。
    """
    if not CACHE_PATH.exists():
        return {}
    try:
        data = CACHE_PATH.read_bytes()
        if not data:
            return {}                    # 空文件: 没有键可丢, 覆盖无害
        return pickle.loads(data)
    except FileNotFoundError:
        return {}                        # 竞态: 刚判存在又被删 → 无键可丢
    except Exception as e:
        if strict:
            raise
        logger.warning("市场数据缓存读取失败, 按空缓存继续: %s: %r",
                       CACHE_PATH, e)
        return {}


def _atomic_pickle(path, obj):
    """原子写 pickle: 同目录唯一 tmp(pid+线程 id) → fsync → replace_with_retry。

    不能走 shared.common.atomic_write: 它收的是 str + utf-8 + 平台换行转换
    (Windows 上 '\\n'→'\\r\\n'), pickle 二进制经它必然损坏 —— 实测 protocol=0
    的 ASCII pickle 也会因 CRLF 变成 "could not convert string to float"。
    故这里用同一库的 replace_with_retry(发布撞车有界退避重试, 预算耗尽原样抛:
    写失败绝不当成功), 与 prism/zt_history.py::_atomic_pickle 同一手法。

    ponytail: 不加写者锁 —— 唯一 tmp 名防互踩, M12 的 replace 重试已覆盖同路径
    写者/读者撞车; 真出现预算耗尽再按路径补锁。"""
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(),
                                           threading.get_ident()))
    with open(tmp, "wb") as fp:
        fp.write(pickle.dumps(obj, protocol=4))
        fp.flush()
        os.fsync(fp.fileno())
    try:
        replace_with_retry(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


def _save_cache(cache):
    """整文件原子保存。保留文件里已有的、本次未更新的段(如 global)。

    cache 只传本次真正拥有的段: 调用方启动时读到的整份快照里带着别的段的陈旧
    副本, 整份回写会把并发写者刚落的新值按旧快照盖回去(09-19 D3 实测:
    build_global_cache/fetch_futures 整份交 → 期间的 flow_rank 当日行 LOST)。

    I2b 同口径(09-19, 与 datasource/fundamental.py / prism/zt_history.py 一致):
    读盘失败 → **跳过本次落盘**(盘上原文件一动不动) + WARNING 点名, 绝不退化成
    "只有本次新段"的整文件覆盖 —— 原子写只降低自发损坏概率, 挡不住外部截断,
    而"读不出"不等于"空": 读不出的内容里可能有别的写者的段。
    """
    try:
        merged = dict(_load_cache(strict=True))
    except Exception as e:
        logger.warning("缓存读盘失败(%r) → 跳过本次落盘, 不覆盖 %s "
                       "(读不出的内容里可能有别的写者的段)", e, CACHE_PATH)
        return
    merged.update(cache)
    _atomic_pickle(CACHE_PATH, merged)


# ---------------------------------------------------------------- 采集

def _tail_stale(rec, end):
    """存量段最后日期早于目标日 → 过期(需重采)。空段视为过期。"""
    ds = (rec or {}).get("dates") or []
    last = str(ds[-1]) if ds else ""
    tgt = _norm_day(end) or ""
    return last < tgt


def _shrink_suspicious(old_rec, new_items):
    """重采结果比旧段短且末日期未回退 → 可疑截断(M-1 守卫, True=保留旧段)。"""
    old_dates = (old_rec or {}).get("dates") or []
    if not old_dates or not new_items:
        return False
    new_dates = [r["date"] for r in new_items]
    return len(new_dates) < len(old_dates) and new_dates[-1] >= old_dates[-1]


def build_sector_cache(probe=None, beg=BACKFILL_BEG, end=None,
                       progress=None, source="eastmoney", rebuild=False):
    """采集行业板块列表 + 历史K线 + 资金流历史, 增量落盘。

    source: "eastmoney"(默认, 东财板块+资金流) / "sw"(申万行业指数,
    东财封禁时的备用; 申万无资金流, flow 段留空)。
    rebuild: True 时清空已有 sectors/kline/flow 段后全量重采(用于切换数据源:
    申万体系 → 东财体系, 避免两套板块代码混在同一缓存)。

    增量口径: 无缓存的板块新采; 已有缓存但最后日期早于 end(尾部过期)
    的重采替换(09-07 修复: 原先"已有即跳过"导致日期永久冻结, 与
    zt_history ④ 同类病); 已追平的不重复请求。

    返回 {"sectors": n, "kline_codes": n, "flow_codes": n,
          "attempted": n, "failed": n}。attempted/failed = 本次**尝试采集**的
    板块数与其中失败的板块数 —— CLI 用它区分"全失败"(退出码 1)与"本次无可采"
    (退出码 3): 没有这个自报计数时二者不可区分(K线/资金流段无返回值)。
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
        # 切源重建: sectors/kline/flow **三段一起清**, 再按 source 全新采集。
        # 理由是 kline/flow 由同一个采集循环写出, 混族(申万 801xxx 与东财 BKxxxx)
        # 会让按前缀过滤的下游看见半套; sectors 单独留旧码则每次 --source sw 都对
        # 东财 BK 码发注定失败的申万请求(实测 496 个, 见
        # test_rebuild_clears_mixed_source_sectors)。清空后 fetch_sector_list 失败
        # 会走下面的"无已有缓存"分支抛错(fail-closed, 且此时尚未落盘)。
        #
        # 边界(09-19 B 复核, 别过度解读这句): 上述只是 **rebuild 的语义**,
        # **不是**"缓存必须单源"的不变量。不带 --rebuild 的 `--build-sectors`
        # (默认 source=eastmoney)会把东财 BK 码追加进 sectors, 于是 sectors
        # 长期同时装着两族(真实缓存实测 527 = 31 个 801 + 496 个 BK) —— 这是
        # **已知且被容忍**的正常状态: prism_web/app.py、prism/first_board_review.py
        # 都只取 801 前缀, qmt/tools/live_check.py 只取 "80" 前缀, flow_rank 段自带
        # code/name 不读 sectors, F8 只按 sector_map 目标(全 801)反查。真正被依赖
        # 的不变量见 tests 的 _assert_source_invariants: kline/flow 同源族, 且
        # kline/flow ⊆ sectors ⊆ sector_map 的映射目标集合。**无证据不要"顺手清理"。**
        sectors = {}
        kline = {}
        flow = {}
        logger.info("rebuild=True: 清空 sectors/kline/flow, 按 %s 全新采集", source)
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
    attempted = 0       # 本次真去采集的板块数(尾部过期的)
    failed = 0          # 其中失败的 —— CLI 据此区分"全失败"(1)与"无可采"(3)
    for code, info in sectors.items():
        need_kline = _tail_stale(kline.get(code), end)
        need_flow = flow_enabled and _tail_stale(flow.get(code), end)
        if need_kline or need_flow:
            attempted += 1
            try:
                if source == "sw":
                    kl = feed.fetch_sector_kline(code)
                else:
                    kl = feed.fetch_kline("90.%s" % code, beg, end,
                                          fields2="f51,f53,f57")
                fl = feed.fetch_sector_flow("90.%s" % code) if flow_enabled \
                    else None
            except MarketDataError as e:
                failed += 1
                logger.warning("板块 %s 采集失败: %r (跳过)", code, e)
                done += 1
                if progress:
                    progress(done, total)
                continue
            if kl and need_kline:
                if _shrink_suspicious(kline.get(code), kl):
                    logger.warning("板块 %s K线重采变短且末日期未回退, "
                                   "保留旧段(收窄回补窗口请用 --rebuild)",
                                   code)
                else:
                    kline[code] = {"dates": [r["date"] for r in kl],
                                   "close": [r["close"] for r in kl],
                                   "amount": [r.get("amount") for r in kl]}
            if fl and need_flow:
                if _shrink_suspicious(flow.get(code), fl):
                    logger.warning("板块 %s 资金流重采变短且末日期未回退, "
                                   "保留旧段", code)
                else:
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
            "flow_codes": len(flow), "attempted": attempted, "failed": failed}


def build_global_cache(probe=None, beg=BACKFILL_BEG, end=None,
                       source="eastmoney"):
    """采集全球指数历史K线, 落盘到同一缓存文件的 "global" 段。

    source: "eastmoney"(默认, 东财全球指数) / "sina"(新浪美股指数,
    东财封禁时的备用; 美债/美元指数新浪源不可用, 只采纳指/标普/道指)。

    返回 {code: {"dates": [...], "close": [...]}}。
    """
    if source == "sina":
        feed = probe or SinaUSIndexFeed()
        # 新浪可用的三个(无 UDI 美元指数): secid → 新浪符号
        index_map = [(_SINA_SYMBOLS[s], c, n) for s, c, n in GLOBAL_INDICES
                     if s in _SINA_SYMBOLS]
    elif source == "fred":
        feed = probe or FREDFeed()
        # FRED series_id → (缓存key, 名称): 美债收益率/VIX
        index_map = [("DGS10", "US10Y", "10年美债收益率"),
                     ("VIXCLS", "VIX", "VIX恐慌指数")]
    else:
        feed = probe or EastMoneyProbe()
        index_map = GLOBAL_INDICES
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
        # 只交 global 段: 整份 cache 会带着启动时的陈旧段按旧值盖回并发写者的新值
        _save_cache({"global": globald})
    return globald


# ------------------------------------------------- 商品期货(首板v04 F8 用)

# 行业名 → 商品期货品种(名称驱动, 规避申万代码在 SW2014/SW2021 间的版本差异;
# 板块代码在 futures_snapshot 组装时从缓存 sectors 段按名称反查)。
# 品种代码 = akshare sina 主力连续符号, 可用性以 pt_futures_probe 实测为准,
# 采集失败的品种静默跳过(fail-open)。
COMMODITY_BY_NAME = {
    "基础化工": ["MA0", "TA0"],
    "煤炭": ["JM0"],
    "钢铁": ["RB0", "HC0"],
    "有色金属": ["CU0", "AL0", "SI0"],
    "石油石化": ["SC0"],
    "农林牧渔": ["LH0", "M0"],
    "食品饮料": ["SR0", "Y0"],
    "建筑材料": ["FG0"],
    "电力设备": ["LC0"],
}
# 品种可用性为 Task 1 探针实测(2026-08-31, 16/16 可用): 动力煤 ZC0 停更于
# 2022-12-30(死数据会让 F8 用旧涨幅误判, 删除); 工业硅符号为 SI0(PS0 实为
# 多晶硅)。详证 .superpowers/sdd/task-v04-1-report.md。

_FUTURES_NAMES = {
    "MA0": "甲醇", "TA0": "PTA", "JM0": "焦煤",
    "RB0": "螺纹钢", "HC0": "热卷", "CU0": "铜", "AL0": "铝",
    "SC0": "原油", "LH0": "生猪", "M0": "豆粕", "Y0": "豆油",
    "SR0": "白糖", "FG0": "玻璃", "LC0": "碳酸锂", "SI0": "工业硅",
}


def _fetch_futures_daily(sym):
    """单品种商品期货日线(akshare sina 主力连续)。返回 DataFrame(date, close)。"""
    import akshare as ak
    return ak.futures_zh_daily_sina(symbol=sym)


def fetch_futures(force=False):
    """采集全部映射品种日线 → 缓存 futures 段。**会联网并回写缓存**。

    只允许从显式采集入口调用: CLI `--build-futures` /
    `futures_snapshot(refresh=True)`。回测跑批与实盘快照走
    `futures_snapshot()` 的默认纯读路径 —— 跑批中途不得改写缓存
    (09-19 实测: 同一回测连跑两轮因中途回写拿到不同期货数据, F8 命中在
    666↔719 间漂移, A/B 对照不再单一变量)。

    返回 {品种代码: {"name", "dates", "close"}}; 单品种失败跳过(fail-open)。
    force=True 忽略缓存全量重采。

    新鲜度(09-19 修 D4): 与板块K线同一门 `_tail_stale` —— 尾部早于今日的品种
    重采, 不再是"只采缓存没有的"("已有即跳过"曾让 15 个品种全部冻在
    2026-08-31, 而 F8 的 20 日涨幅就吃这份死数据; 与 ETF 首下载冻结同类病)。"""
    cache = _load_cache()
    fut = {} if force else dict(cache.get("futures") or {})
    today = date.today().strftime("%Y%m%d")
    if force:
        todo = list(_FUTURES_NAMES)
    else:
        todo = [sym for sym in _FUTURES_NAMES
                if sym not in fut or _tail_stale(fut[sym], today)]
    for sym in todo:
        try:
            df = _fetch_futures_daily(sym)
            if df is None or len(df) < 60:
                continue
            dates = [str(d) for d in df["date"].tolist()]
            closes = [float(x) for x in df["close"].tolist()]
            fut[sym] = {"name": _FUTURES_NAMES[sym],
                        "dates": dates, "close": closes}
        except Exception as e:
            logger.warning("期货 %s 采集失败: %r", sym, e)
        time.sleep(0.5)
    if todo:   # 本次确有采集才落盘; 无新采集时不重写文件
        # 只交 futures 段: 整份 cache 会带着启动时的陈旧段按旧值盖回并发写者的新值
        _save_cache({"futures": fut})
    return fut


def futures_snapshot(refresh=False):
    """组装 mkt["futures"] 快照: {板块代码: {"name", "commodities": {品种: K线}}}。
    板块代码/名称来自缓存 sectors 段(与 sector_map / mkt["sector"] 同源);
    行业 → 品种按名称映射(COMMODITY_BY_NAME), 无映射行业不出现。

    **默认纯读**(09-19 硬要求): 只读缓存 futures 段, 绝不联网、绝不回写 ——
    回测跑批(backtest/cli.py::load_market_data)与实盘/网页快照都走这条。
    原先默认调 fetch_futures() 会在跑批中途采集并回写真实缓存。
    refresh=True 才允许"采集 + 落盘"(给 --build-futures 这类显式入口用)。"""
    cache = _load_cache()
    fut = fetch_futures() if refresh else dict(cache.get("futures") or {})
    out = {}
    for scode, rec in (cache.get("sectors") or {}).items():
        name = (rec or {}).get("name") or ""
        syms = COMMODITY_BY_NAME.get(name)
        if not syms:
            continue
        comms = {sym: fut[sym] for sym in syms if sym in fut}
        out[scode] = {"name": name, "commodities": comms}
    return out


def mkt_snapshot():
    """组装引擎/实盘用的市场数据快照(run_screen 下发 ctx._extra["mkt"])。

    结构与回测 CLI 注入一致: sector/global/sector_flow + benchmark/flow_rank
    (板块感知层) + futures(F8) + zt_prev(F9)。全部读本地缓存, 单段失败降级缺键
    (fail-open), 不抛。**纯读**: 不联网、不回写(futures 段走
    futures_snapshot() 默认路径, 见其 docstring)。
    """
    cache = _load_cache()
    snap = {}
    for key, ck in (("global", "global"), ("sector_flow", "flow"),
                    ("benchmark", "benchmark"), ("flow_rank", "flow_rank")):
        v = cache.get(ck)
        if v:
            snap[key] = v
    kline = cache.get("kline") or {}
    if kline:
        # sector 段附加板块名(GUI 阶段面板显示名称; 因子只读 close/amount 不受影响)
        names = {c: (i or {}).get("name") or ""
                 for c, i in (cache.get("sectors") or {}).items()}
        snap["sector"] = {c: dict((rec or {}), name=names.get(c, ""))
                          for c, rec in kline.items()}
    try:
        snap["futures"] = futures_snapshot()
    except Exception as e:
        logger.warning("futures 快照失败: %r", e)
    try:
        from prism.zt_history import prev_day_pool
        snap["zt_prev"] = prev_day_pool()
    except Exception as e:
        logger.warning("zt_prev 快照失败: %r", e)
    return snap


# ------------------------------------------------- 资金惯性/基准(板块感知层)

# 上证指数代码: 东财 secid=1.000001 / 通达信指数通道(取点号前6位) / QMT 000001.SH
BENCHMARK_INDEX_CODE = "000001.SH"

def build_flow_rank(probe=None):
    """当日行业板块主力净流入快照 → 前向累积落盘 cache["flow_rank"]。
    东财 BK 细分行业口径, 自洽使用(不回填 SEC3 申万 flow 段)。
    当日已在 dates → 覆盖当日行, 幂等, 盘后重跑取终值。
    返回 {"dates": n, "sectors_today": n}。失败 → 抛 MarketDataError。"""
    probe = probe or EastMoneyProbe()
    rows = probe.fetch_flow_rank()
    if not rows:
        # 全 '-'/空快照(盘前/非交易日): 不落盘, 避免空断点日截断 streak 链
        cache = _load_cache()
        return {"dates": len((cache.get("flow_rank") or {}).get("dates") or []),
                "sectors_today": 0}
    cache = _load_cache()
    fr = cache.get("flow_rank") or {}
    dates = list(fr.get("dates") or [])
    rmap = dict(fr.get("rows") or {})
    today = date.today().strftime("%Y-%m-%d")
    # ponytail: 只前向累积, 不回补历史(fflow 历史端点实测被封)
    if today not in dates:
        dates.append(today)
    rmap[today] = rows
    _save_cache({"flow_rank": {"dates": dates, "rows": rmap}})
    return {"dates": len(dates), "sectors_today": len(rows)}


def _tdx_benchmark_kline(beg=None, end=None):
    """通达信上证指数日K → [{date, close, amount}](与东财通道逐字段同构)。

    东财 push2his 被封(09-07 起逐段, 09-13 复查确认)后的降级通道; 指数必须走
    get_index_bars(见 prism/tdx_source.py 文档: get_security_bars 取指数返回垃圾
    内存)。单次上限 800 根(≈3 年)足够覆盖 BACKFILL_BEG 起全区间, 再按 beg/end
    过滤。取不到 → 返回 [](调用方据此走 fail-open 保留旧缓存)。"""
    from prism import tdx_source
    try:
        df = tdx_source.get_index_kline(BENCHMARK_INDEX_CODE, days=800)
    except Exception as e:      # 补充源 fail-open, 不能反过来炸掉基准任务
        logger.warning("通达信上证指数取数异常: %r", e)
        return []
    if df is None or len(df) == 0:
        return []
    beg_fmt, end_fmt = _norm_day(beg), _norm_day(end)
    out = []
    for _, row in df.iterrows():
        d = str(row["date"])[:10]
        if beg_fmt and d < beg_fmt:
            continue
        if end_fmt and d > end_fmt:
            continue
        out.append({"date": d, "close": _f(row["close"]),
                    "amount": _f(row.get("amount"))})
    return out


def _qmt_xtdata():
    """QMT xtdata 模块(延迟导入; 不可用 → None)。测试可整体替换。"""
    try:
        from xtquant import xtdata
        return xtdata
    except Exception as e:
        logger.warning("QMT(xtquant) 不可用: %r", e)
        return None


def _qmt_benchmark_kline(beg=None, end=None):
    """QMT 上证指数日K → [{date, close, amount}](与东财通道逐字段同构)。

    最后一路降级(09-19): 东财 push2his 已封, 通达信 K线通道实测全灭
    (4 台白名单服务器全报 calling function error, 只有财务接口活着) —— 本机
    QMT 在线且本地已有 000001.SH 日K。QMT 索引形如 '20260918' → 用 _norm_day
    归一成 '2026-09-18'(与东财段同构)。取不到 → [](fail-open)。"""
    xt = _qmt_xtdata()
    if xt is None:
        return []
    code = BENCHMARK_INDEX_CODE
    try:
        data = xt.get_market_data_ex([], [code], period="1d", count=800) or {}
    except Exception as e:
        logger.warning("QMT 上证指数取数异常: %r", e)
        return []
    df = data.get(code)
    if df is None or len(df) == 0:
        return []
    beg_fmt, end_fmt = _norm_day(beg), _norm_day(end)
    out = []
    for idx, row in df.iterrows():
        d = _norm_day(str(idx)[:8])
        if not d:
            continue
        if beg_fmt and d < beg_fmt:
            continue
        if end_fmt and d > end_fmt:
            continue
        out.append({"date": d, "close": _f(row.get("close")),
                    "amount": _f(row.get("amount"))})
    return out


def build_benchmark(probe=None, beg=BACKFILL_BEG, end=None):
    """上证指数日K → cache["benchmark"] 全量替换(单指数成本低, 自愈)。

    取数降级链(09-19 D7): 东财 kline(原源) → 通达信 get_index_bars
    (prism/tdx_source) → QMT 本地日K(xtdata)。三路都拿不到 → 保留旧缓存
    (fail-open, kept_old=True; CLI 据此非零退出, 绝不把"没刷新成功"当成功)。
    返回 {"days": n, "kept_old": bool}。"""
    probe = probe or EastMoneyProbe()
    end = end or date.today().strftime("%Y%m%d")
    old = _load_cache().get("benchmark") or {}
    old_days = len(old.get("dates") or [])
    kl = []
    try:
        kl = probe.fetch_benchmark_kline(beg, end)
    except MarketDataError as e:
        logger.warning("上证基准东财取数失败(%r), 降级补充源", e)
    if not kl:
        kl = _tdx_benchmark_kline(beg, end)
        if kl:
            logger.warning("上证基准走通达信降级通道: %d 日", len(kl))
    if not kl:
        kl = _qmt_benchmark_kline(beg, end)
        if kl:
            logger.warning("上证基准走 QMT 本地降级通道: %d 日", len(kl))
    if not kl:
        logger.warning("上证基准三路都拿不到(东财封禁 + 通达信无数据 + QMT 无数据), "
                       "保留旧缓存 %d 日", old_days)
        return {"days": old_days, "kept_old": True}
    _save_cache({"benchmark": {"dates": [r["date"] for r in kl],
                               "close": [r["close"] for r in kl],
                               "amount": [r.get("amount") for r in kl]}})
    return {"days": len(kl), "kept_old": False}


# ------------------------------------------------- ETF 锚点行情(周度跟踪 W1)

# 下载 memo: {code: "YYYYMMDD"} — 每代码每日至多下载一次(测试可整体替换)
_ETF_DL_MEMO = {}


def _etf_df(xt, code):
    """单码本地日K(count=2) → df; 不足 2 根 → None。"""
    data = xt.get_market_data_ex([], [code], period="1d", count=2) or {}
    df = data.get(code)
    return df if df is not None and len(df) >= 2 else None


def _df_last_day(df):
    """df 末根K线日期('YYYYMMDD'; QMT 索引可能带时分秒, 截前 8 位)。"""
    try:
        return str(df.index[-1])[:8]
    except Exception:
        return ""


def _etf_quote_one(xt, code):
    """单码最新日K(count=2) → {"amount": 元, "pct_chg": %}; 数据不足 → None。"""
    df = _etf_df(xt, code)
    if df is None:
        return None
    closes = list(df["close"])
    amounts = list(df["amount"])
    prev, last = closes[-2], closes[-1]
    if not prev or prev <= 0 or not last:
        return None
    return {"amount": float(amounts[-1] or 0.0),
            "pct_chg": (float(last) / float(prev) - 1.0) * 100.0}


def fetch_etf_quotes(codes, xtdata_mod=None):
    """ETF 锚点行情: {code: {"amount": 元, "pct_chg": %}}(周度跟踪 W1)。

    xtdata get_market_data_ex count=2 日K → 最新成交额 + 涨跌幅。
    下载触发(09-08 修 I-1): 本地无数据**或末根日期早于今日** → 下载——
    否则首下载后本地永远有数据, 行情冻结在下载日被当"当日"展示。
    批量一次 download_history_data2(该 API 本就收列表), 每码每日至多一次
    (memo _ETF_DL_MEMO; **下载失败也占当日槽**——防 QMT 停机日每请求
    24 连打, 次日自愈); 批量炸 → 逐码兜底。单代码失败/始终无数据 →
    跳过(fail-open), 不抛。amount 单位与 data.py 既有口径一致(元)。
    xtdata_mod 供测试注入假模块; 缺省 import xtquant(不可用 → {})。"""
    xt = xtdata_mod
    if xt is None:
        try:
            from xtquant import xtdata as xt
        except Exception:
            return {}
    today = date.today().strftime("%Y%m%d")
    codes = list(codes or [])
    # 1. 逐码读本地, 标出缺数据/末根早于今日的码
    stale = []
    for code in codes:
        try:
            df = _etf_df(xt, code)
        except Exception as e:
            logger.warning("ETF %s 行情失败(跳过): %r", code, e)
            continue
        if df is None or _df_last_day(df) < today:
            stale.append(code)
    # 2. 批量下载(memo 日记帐在前: 失败也占当日槽)
    todo = [c for c in stale if _ETF_DL_MEMO.get(c) != today]
    for c in todo:
        _ETF_DL_MEMO[c] = today
    if todo:
        try:
            xt.download_history_data2(todo, "1d", start_time="", end_time="")
        except Exception as e:
            logger.warning("ETF 批量下载失败, 逐码兜底: %r", e)
            for c in todo:
                try:
                    xt.download_history_data2([c], "1d",
                                              start_time="", end_time="")
                except Exception as ee:
                    logger.warning("ETF %s 下载失败(跳过): %r", c, ee)
    # 3. 逐码出数(单码失败跳过, 不牵连)
    out = {}
    for code in codes:
        try:
            q = _etf_quote_one(xt, code)
        except Exception as e:
            logger.warning("ETF %s 行情失败(跳过): %r", code, e)
            q = None
        if q:
            out[code] = q
    return out


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
        # 尾部统一落盘(行业数 <10 时循环内的增量保存不会触发)
        _save_cache({"sector_map": smap})
    return {"stocks": len(smap)}


def _code6(s):
    """'000019.SZ' / '000019' → '000019'。无法解析 → None。"""
    s = str(s).strip().upper()
    if "." in s:
        s = s.split(".")[0]
    if len(s) == 6 and s.isdigit():
        return s
    return None


# ---------------------------------------------------------------- CLI

# 统一退出码(6 个 --build-* 开关一致; 模块 docstring 与 --help epilog 同步):
EXIT_OK, EXIT_FAIL, EXIT_EMPTY = 0, 1, 3
EXIT_HELP = """退出码(6 个 --build-* 开关统一):
  0  所有点名的段本次都真的取到/推进了数据
  1  有点名的段真失败: 抛异常 / sectors 本次尝试的板块全失败 /
     --build-benchmark 三路全灭(kept_old) / 拿不到数据且无旧缓存可留
     (点名消息走 stderr, 进程退出码 = 1)
  3  无失败但有段本次未推进(空数据; 旧缓存原样保留) —— 警告级, 绝不与 0 同码
多个 --build-* 可同时给出: 全部按参数声明顺序执行(不再静默短路);
一段失败不连累其余段, 最后统一报码。不带任何 --build-* 只打印缓存统计(退出码 0)。"""

# 各段的缓存键(判"本次是否真推进"用; 与 prism/data_refresh.py 的 SEGMENTS 同口径;
# 段名直接用缓存键名, 报错点名与 data_refresh 的段名一一对应)
_SEG_KEYS = {"sectors": ("sectors", "kline", "flow"),
             "global": ("global",),
             "sector_map": ("sector_map",),
             "flow_rank": ("flow_rank",),
             "futures": ("futures",),
             "benchmark": ("benchmark",)}


def _seg_state(keys):
    """该段当前落盘状态 → {缓存键: (条目数, 末日期)}; 判"本次是否真推进"。"""
    cache = _load_cache()
    out = {}
    for k in keys:
        seg = cache.get(k)
        ds = seg.get("dates") if isinstance(seg, dict) else None
        if isinstance(ds, list):            # flow_rank/benchmark: 单序列段
            out[k] = (len(ds), str(ds[-1]) if ds else "")
            continue
        lasts = []
        for r in (seg or {}).values():      # 逐子键段(kline/global/futures...)
            rds = r.get("dates") if isinstance(r, dict) else None
            if rds:
                lasts.append(str(rds[-1]))
        out[k] = (len(seg or {}), max(lasts) if lasts else "")
    return out


def _advanced(before, after):
    """缓存是否真的推进了(条目数或末日期变大)。"""
    return any(after.get(k, (0, "")) > before.get(k, (0, "")) for k in after)


def _produced(name, r):
    """采集函数自报的"本次真的写出了新数据"。

    只有自报字段**确实表示"本次"**的段才用它 —— sectors 的 kline_codes/flow_codes
    是**累积**条数(len(kline)), 不是本次写入量, 故不在此列(它走 _advanced);
    拿不到"本次"信号的段返回 False, 调用方退回 _advanced。"""
    if name == "flow_rank":
        return bool(r.get("sectors_today"))   # 空快照(盘前/非交易日)不落盘
    if name == "benchmark":
        return not r.get("kept_old")          # kept_old 由调用方先点名失败
    return False


def build_cli():
    import argparse
    ap = argparse.ArgumentParser(
        description="市场数据层采集(板块/K线/资金流/全球指数)",
        epilog=EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build-sectors", action="store_true",
                    help="采集行业板块K线+资金流(增量; 东财封禁时 --source sw 换"
                         "申万K线, 申万无资金流 ⇒ flow 段只能东财)")
    ap.add_argument("--build-global", action="store_true",
                    help="采集全球指数历史K线(增量; 东财封禁时 --source sina "
                         "或 --source fred)")
    ap.add_argument("--build-sector-map", action="store_true",
                    help="构建个股→申万行业映射(成分股采集; 走 akshare 申万, "
                         "不依赖东财)")
    ap.add_argument("--build-flow-rank", action="store_true",
                    help="当日板块主力净流入快照(前向累积, 幂等; 仅东财源, "
                         "东财封禁时无降级源)")
    ap.add_argument("--build-futures", action="store_true",
                    help="商品期货日线采集(尾部过期的品种重采; akshare 新浪源)")
    ap.add_argument("--build-benchmark", action="store_true",
                    help="上证指数日K基准(全量替换; 降级链 东财→通达信→QMT, "
                         "三路都拿不到则保留旧缓存并非零退出)")
    ap.add_argument("--beg", default=BACKFILL_BEG,
                    help="回填起点 YYYYMMDD(默认 %s)" % BACKFILL_BEG)
    ap.add_argument("--source", default="eastmoney",
                    choices=["eastmoney", "sw", "sina", "fred"],
                    help="板块/指数数据源: eastmoney(默认) / sw(申万) / sina(新浪美股)")
    ap.add_argument("--rebuild", action="store_true",
                    help="清空 sectors/kline/flow 后全量重采(切换数据源时用, 避免混杂)")
    args = ap.parse_args()

    def prog(done, total):
        sys.stdout.write("\r  采集 %d/%d" % (done, total))
        sys.stdout.flush()

    # 点名的段全部入列并按声明顺序执行 —— 改前前四个 flag 是"打印完裸 return",
    # 组合调用时后面的 flag 被静默丢弃(--build-sectors --build-flow-rank 里
    # flow-rank 根本不跑, 实测退出码 0)。
    # 选"组合执行"而不是"组合即报错": ① `--build-flow-rank --build-benchmark`
    # 的组合本来就是既有行为且有回归锁(test_cli_build_flags_run_both);
    # ② 文档与 data_refresh 已按多段组合写命令(如"东财解封后
    # `--build-sectors --build-flow-rank`"), 拒绝组合会把它们判成用法错误;
    # ③ 组合执行是对既有意图的兑现, 拒绝只是把静默忽略换成显式忽略。
    jobs = []
    if args.build_sectors:
        jobs.append(("sectors",
                     lambda: build_sector_cache(beg=args.beg, progress=prog,
                                                source=args.source,
                                                rebuild=args.rebuild),
                     lambda r: print("\n板块采集完成(source=%s):"
                                     % args.source, r)))
    if args.build_global:
        jobs.append(("global",
                     lambda: build_global_cache(beg=args.beg,
                                                source=args.source),
                     lambda r: print("全球指数采集完成(source=%s): %d 个"
                                     % (args.source, len(r)))))
    if args.build_sector_map:
        jobs.append(("sector_map",
                     lambda: build_sector_map(progress=prog),
                     lambda r: print("\n个股→行业映射完成: %d 只" % r["stocks"])))
    if args.build_flow_rank:
        jobs.append(("flow_rank", lambda: build_flow_rank(),
                     lambda r: print("资金惯性快照:", r)))
    if args.build_futures:
        jobs.append(("futures", lambda: fetch_futures(),
                     lambda r: print("\n商品期货采集完成: %d 个品种" % len(r))))
    if args.build_benchmark:
        jobs.append(("benchmark", lambda: build_benchmark(beg=args.beg),
                     lambda r: print("上证基准:", r)))

    if not jobs:
        cache = _load_cache()
        # 无采集动作 → 打印缓存统计
        print("板块数:", len(cache.get("sectors") or {}))
        print("K线板块数:", len(cache.get("kline") or {}))
        print("资金流板块数:", len(cache.get("flow") or {}))
        print("全球指数:", {k: len(v.get("dates") or [])
                            for k, v in (cache.get("global") or {}).items()})
        bench = cache.get("benchmark") or {}
        fr = cache.get("flow_rank") or {}
        print("上证基准:", len(bench.get("dates") or []), "日")
        print("资金惯性:", len(fr.get("dates") or []), "日快照")
        return EXIT_OK

    fails, empties, oks = [], [], []
    for name, fn, emit in jobs:
        keys = _SEG_KEYS[name]
        before = _seg_state(keys)
        try:
            r = fn()
        except MarketDataError as e:
            # 容错: 一段被封不连累其余段(push2/push2his 封禁常不同步)
            fails.append(name)
            print("\n%s 采集失败: %r" % (name, e))
            continue
        emit(r)
        after = _seg_state(keys)
        if name == "benchmark" and r.get("kept_old"):
            # build_benchmark 三路都拿不到时 fail-open 保留旧缓存(不抛异常) ——
            # 这里补记点名失败: 否则"没刷新成功"照样 exit 0 (09-19 实证: 缓存
            # 停更 9 个交易日而盘后任务判成功)。**有意保留的 fail-open 语义**。
            fails.append(name)
            print("  → 三路都拿不到, 保留旧缓存(fail-open), 退出码 %d" % EXIT_FAIL)
            continue
        if _produced(name, r) or _advanced(before, after):
            oks.append(name)
            continue
        if (name == "sectors" and r.get("attempted")
                and r.get("failed") == r.get("attempted")):
            fails.append(name)               # 全失败(自报计数, 证据明确)
            print("  → 本次 %d 个板块全部采集失败, 退出码 %d"
                  % (r["attempted"], EXIT_FAIL))
        elif any(n for n, _ in after.values()):
            empties.append(name)             # 旧缓存还在 → 未推进(警告级)
            print("  → 本次未推进(空数据, 旧缓存原样保留), 退出码 %d" % EXIT_EMPTY)
        else:
            fails.append(name)               # 拿不到数据且无旧缓存可留
            print("  → 拿不到数据且无旧缓存可留, 退出码 %d" % EXIT_FAIL)

    print("\n汇总: 成功 %d 段 / 未推进 %d 段 / 失败 %d 段 (共 %d 段)"
          % (len(oks), len(empties), len(fails), len(jobs)))
    if fails:
        # 点名的采集有失败 → 非零退出(自动化可感知), 不静默吞。
        # 部分成功也报 1: 其余段已照跑完(上面的循环不提前中断), 逐段结果都在 stdout。
        raise SystemExit("market_data: %s 采集失败(东财可能封禁), 稍后重试"
                         % "+".join(fails))
    if empties:
        raise SystemExit(EXIT_EMPTY)
    return EXIT_OK


if __name__ == "__main__":
    build_cli()