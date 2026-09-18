# -*- coding: utf-8 -*-
"""东方财富个股因子: 自动计算原手填因子 Y1/Y5/F7/Y7/S5/Y6/Y2。
复用 eastmoney.py 的 http_get 注入模式(测试注入假实现, 绝不发真实请求)。
每个因子一方法; 任何失败 → 该因子跳过(fail-open), 调用方回落手动输入。"""
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from shared.common import atomic_write

# 缓存落盘位置: 统一收在 runtime/cache/ (本文件上两级 = 项目根)
_DEFAULT_CACHE_PATH = (Path(__file__).resolve().parent.parent
                       / "runtime" / "cache" / "fundamental_cache.json")

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

logger = logging.getLogger(__name__)

# ---- 可调参数 ----
Y1_MAX_FLOAT_MV = 80e8      # 小市值: 流通市值 < 80亿
Y8_MID_MIN = 30e8           # 中市值启动: 30亿 ≤ 流通市值 < 100亿(覆盖艾艾精工类48亿)
Y8_MID_MAX = 100e8
Y5_MIN_CONCEPTS = 3         # 多概念: 概念标签 >= 3
RECENT_DAYS = 5             # "近 N 日" 窗口(题材新颖/事件/龙虎榜)
# 网络类因子(联网路径"真的取到数了"的证据): 一个都没成功 = 本次取数整体失败,
# 结果不许落盘(I3, 见 compute_for_stock)。含 Y5/Y2(快照类) —— 它们同样来自
# 网络, 且**无历史可回补**: 若因 F7/Y6/Y7 全挂就把它们一起丢掉, 等于把"当天
# 快照永久缺失"换个位置再挖一遍(与守护 I1 同一类数据丢失)。
NET_FACTORS = ("Y5", "Y2", "F7", "Y7", "Y6")

# 利好关键词(公告 → Y6 事件催化)
EVENT_KEYWORDS = ["预增", "中标", "重组", "增持", "回购", "签约", "获批"]


def _default_http_get(url, params=None, headers=None, timeout=None):
    if requests is None:
        raise RuntimeError("requests 未安装")
    return requests.get(url, params=params, headers=headers, timeout=timeout)


def _today_key():
    return date.today().strftime("%Y%m%d")


def _ref_key(d):
    """基准日 → 缓存键日期段(YYYYMMDD)。"""
    return d.strftime("%Y%m%d")


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
    """东财个股因子。http_get 可注入(测试);cache_path 为 None 时禁用缓存。

    asof: 数据基准日(date, 默认 None=今天)。防未来函数的关键:
      龙虎榜(Y7)/公告(Y6)/涨停池(F7)都按 asof 切窗口, 缓存键也按 asof 分日。
      实盘(计算今天)不传即用今天, 行为不变; 回测若接入本 feed, 必须传
      asof=回测选股日, 否则会读到回测日之后的数据(未来函数, 回测虚高)。
    offline(2026-09-17 用户拍板): True = **回测侧只读缓存, 绝不联网**。
      跳过全部网络因子(Y5/Y2/F7/Y7/S5/Y6), Y1/Y8 纯计算照算(用传入 float_mv);
      命中既有缓存照常返回缓存值(含联网因子)。动机: 东财单股取数实测 40s+
      (其中 Y6 公告端点 35.5s), 全窗口 254 日 ≈100 小时不可行 → 历史靠
      `python -m prism.fund_snapshot --date YYYYMMDD` 逐日回填, 回测只消费。
      **offline 绝不写缓存**(含未命中与已命中两种情形): 否则"离线缺失 → 只有
      Y1/Y8 的空/半截结果"会被钉进历史 —— `compute_for_stock` 开头
      `if key in self._cache: return` 会短路, 之后联网运行与每日快照再也补不上
      该日的真实值(缓存是 append-only 的事实记录, 不该被降级结果污染)。"""

    HEADERS = {"User-Agent": "Mozilla/5.0",
               "Referer": "http://quote.eastmoney.com/"}

    def __init__(self, http_get=None, cache_path=_DEFAULT_CACHE_PATH,
                 timeout=5.0, asof=None, offline=False):
        self.http_get = http_get or _default_http_get
        self.cache_path = cache_path
        self.timeout = timeout
        self.asof = asof
        self.offline = offline
        self._cache = self._load_cache() if cache_path else {}

    def _ref(self, asof=None):
        """本次取数的"今天": 调用级 asof > 实例级 asof > date.today()。"""
        return asof or self.asof or date.today()

    # ---------- 缓存 ----------
    def _load_cache(self, strict=False):
        """读盘缓存。`strict=True` → 读失败原样抛出, 让调用方自己决定。

        为什么要区分: `_save_cache` 的读-改-写合并以读到的盘上内容为起点。
        "文件不存在/空" → 起点 {} , 覆盖无害(没有键可丢); 但**读失败**(坏
        JSON / 权限 / IO 错)若也当 {} , 合并就退化成整文件覆盖 ⇒ 磁盘上别的
        写者的键被整批抹掉, 正是 I2 修过的病复发(且更隐蔽: 盘上明明有内容)。
        非 strict(构造时载入)保持旧语义: 坏文件当空, 不因读失败炸掉采集。"""
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                text = f.read()
            if not text.strip():
                return {}                    # 空文件: 没有键可丢, 覆盖无害
            return json.loads(text)
        except FileNotFoundError:
            return {}
        except Exception:
            if strict:
                raise
            return {}

    def _save_cache(self):
        if not self.cache_path:
            return
        # 原子写(mkdir+tmp+fsync+os.replace, shared 底座): 回测逐日采集/快照/
        # 实盘会并发写同一份缓存, 半截 JSON 会被 _load_cache 静默当空 → 历史
        # 全丢, 必须整文件原子落地(Task 3, 规格 §7)。
        # I2: 整文件回写前**先读回磁盘并合并** —— self._cache 是 __init__ 时载入
        # 的那一份快照, 长寿命实例(prism.data.DataProvider.fund_feed 实盘/盯盘
        # 只建一次)回写时会把"启动之后由别的写者(prism.fund_snapshot CLI / 守护
        # 快照线程)新增的键"整批删掉, 两边还都报成功 —— 丢的正是本批次要积累的
        # Y2/Y5 日快照与 --date 回填历史。磁盘独有的键必须保留; 同键以本进程
        # 内存值为准(那是刚算出来的更新)。
        # I2b(2026-09-18): 读盘失败**不许**退化成整文件覆盖 —— 那等于把 I2 的
        # 病复发(把读不出的盘当空的)。fail-safe: 跳过本次落盘 + WARNING,
        # 下次读得通时再合并; 内存里的结果还在, 不会因此丢。
        # ponytail: 读-改-写不是原子的(无跨进程锁): 两个写者若在同一瞬间各读到
        # 旧盘再各自落盘, 仍可能互相盖掉刚落的新键 —— 窗口已从"整个进程生命周期"
        # 缩到"一次写盘", 实盘两个写者(守护快照线程 / fund_snapshot CLI)频率极低,
        # 够用; 真观测到丢键再加文件锁。
        try:
            disk = self._load_cache(strict=True)
        except Exception as exc:
            logger.warning("缓存读盘失败(%r) → 跳过本次落盘, 不覆盖 %s "
                           "(读不出的内容里可能有别的写者的键)",
                           exc, self.cache_path)
            return
        disk.update(self._cache)
        atomic_write(self.cache_path,
                     json.dumps(disk, ensure_ascii=False))

    def _cache_key(self, code, asof=None):
        """按基准日分日缓存: 同一只股在不同 asof 下是不同的数据快照。"""
        return "%s:%s" % (_ref_key(self._ref(asof)), code)

    def _complete_calc_factors(self, key, entry, float_mv):
        """缓存命中时补齐纯计算因子 Y1/Y8(C1 修复, 2026-09-18)。

        机制: 快照采集(`prism.fund_snapshot --date` / 守护每日钩子)调
        `compute_for_stock(code[, asof=…])` **不传 float_mv**(快照的目标是
        Y2/Y5 这类无历史可回补的接口值 + F7/Y6/Y7 的 asof 当日值), 而 Y1/Y8
        是 float_mv 的**纯函数** → 落下的 `YYYYMMDD:code` 条目不含 Y1/Y8;
        之后回测带当日 float_mv 来问同一天, 被开头 `if key in self._cache:
        return` 短路 → **Y1/Y8 永远补不回来**(该 (股,日) 被钉成 0, 而数据说明
        却宣称"Y1/Y8 由当日流通市值纯计算, 不受缓存覆盖影响" —— 假话)。

        命中时若 `float_mv` 为真且条目缺 Y1/Y8 → 就地算出并合并进**返回副本**:
          - `offline=True`: 只返回, **绝不落盘**(离线不写缓存的铁律不变);
          - `offline=False`: 补齐结果原子写回(下次命中即自足, 不必重算)。
        已有该因子时**不动它**(缓存是 append-only 的事实记录, 不许被重算覆盖);
        `float_mv` 为假(缺股本)时什么都不做 —— 无从算起就不猜。
        """
        out = dict(entry)
        if not float_mv:
            return out
        added = False
        for name, fn in (("Y1", self._small_cap), ("Y8", self._mid_cap)):
            if name in out:
                continue
            try:
                res = fn(float_mv)
            except Exception as e:   # 非数值 float_mv 等 → 保持缺省, 不崩
                logger.warning("东财因子 %s(%s) 命中缓存时补齐失败: %r",
                               name, key, e)
                continue
            if res is not None:
                out[name] = res
                added = True
        if added and not self.offline:
            self._cache[key] = dict(out)
            self._save_cache()
        return out

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

    def _hybk_history(self, asof=None):
        """近 RECENT_DAYS-1 个交易日的全部涨停池 hybk 集合(按日缓存, 全股票共用)。
        从 asof 前一天起逐日历日回溯, data.pool==null(非交易日)→跳过该日历日继续,
        上限 20 日; 端点失败(HTTP/超时/解析)→ 直接抛异常(F7 fail-open, 不写缓存)。
        键 'hybk_history:YYYYMMDD' 与各股票无关, 一天只算一次, 避免每只股各回溯 N 次。"""
        ref = self._ref(asof)
        today_key = _ref_key(ref)
        cache_key = "hybk_history:%s" % today_key
        if cache_key in self._cache:
            return set(self._cache[cache_key])
        hist = set()
        days = 0
        day = ref - timedelta(days=1)
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

    def _mid_cap(self, float_mv):
        """Y8 中市值启动: 30亿 ≤ 流通市值 < 100亿(纯计算, 无网络)。

        文档艾艾精工案例: 启动时市值约48亿, 妖股模型原小市值条件(≤80亿)
        其实能覆盖, 但30亿以下更优的票更稀缺; Y8 把中市值启动票单独标记,
        供策略配置决定是否纳入(默认不干扰 Y1 小市值语义)。"""
        if not float_mv:
            return None
        hit = Y8_MID_MIN <= float_mv < Y8_MID_MAX
        return {"score": 1 if hit else 0,
                "note": "流通市值 %.0f亿 %s" % (
                    float_mv / 1e8,
                    "∈[30,100)亿 中市值启动" if hit else "非中市值区间")}

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

    def _novel_concept(self, code, asof=None):
        """F7 题材新颖: asof 当日涨停池该股 hybk 不在近 RECENT_DAYS-1 个交易日的
        hybk 集合 → 1。当日池无该股 / 无 hybk → None(fail-open);
        历史集合按日缓存(hybk_history:YYYYMMDD)。"""
        code6 = _code6(code)
        pool = self._ztpool(_ref_key(self._ref(asof)))   # 基准日涨停池
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
        hist = self._hybk_history(asof)
        novel = hybk not in hist
        return {"score": 1 if novel else 0,
                "note": ("今日题材 %s 为近 %d 日首次出现(新颖)" % (hybk, RECENT_DAYS))
                if novel else "今日题材 %s 近 %d 日已出现过" % (hybk, RECENT_DAYS)}

    def _dragon_tiger(self, code, asof=None):
        """Y7 游资现身: 近 RECENT_DAYS 日龙虎榜有该股且净买入 >0 → 1。返回 {"score","note"}。
        filter 用 SECURITY_CODE(6 位无后缀, 非 SECUCODE); 日期在 Python 里过滤(更稳)。
        窗口以 asof 为基准(防未来函数: 回测传入选股日, 不看 asof 之后的榜)。"""
        rows = self._datacenter("RPT_DAILYBILLBOARD_DETAILSNEW",
                                '(SECURITY_CODE="%s")' % _code6(code))
        ref = self._ref(asof)
        cutoff = ref - timedelta(days=RECENT_DAYS - 1)
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
            # 双向窗口: 榜单日必须在 [cutoff, ref] 内——ref 之后的记录
            # 在回测场景就是未来数据, 绝不能计入
            if cutoff <= row_date <= ref:
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

    def _event(self, code, asof=None):
        """Y6 事件催化: 近 RECENT_DAYS 日公告 title 命中 EVENT_KEYWORDS → 1。
        日期(notice_date)在 Python 里过滤; 关键词命中即判定。
        窗口以 asof 为基准(防未来函数); 公告接口只给最近 20 条, asof 距今
        太远时取不到当日窗口数据(该股得 0, 属数据覆盖问题, 不引入未来数据)。"""
        resp = self.http_get(
            "https://np-anotice-stock.eastmoney.com/api/security/ann",
            params={"sr": "-1", "page_size": "20", "page_index": "1",
                    "ann_type": "A", "client_source": "web", "page_number": "1",
                    "f_node": "0", "s_node": "0", "stock_list": _code6(code)},
            headers=self.HEADERS, timeout=self.timeout)
        resp.raise_for_status()
        d = resp.json()
        items = (d.get("data") or {}).get("list") or []
        ref = self._ref(asof)
        cutoff = ref - timedelta(days=RECENT_DAYS - 1)
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
            # 双向窗口: 公告日必须在 [cutoff, ref] 内——超过 ref 的公告
            # 在回测场景就是未来数据, 绝不能计入
            if cutoff <= nd_date <= ref:
                return {"score": 1,
                        "note": "近 %d 日公告命中事件: %s" % (RECENT_DAYS, title[:20])}
        return {"score": 0, "note": "近 %d 日无事件催化公告" % RECENT_DAYS}

    # ---------- 公开入口 ----------
    def compute_for_stock(self, code, float_mv=None, asof=None):
        """返回 {因子名: {"score":0/1, "note":str}}。单因子失败→跳过(不抛)。
        Y1 纯计算; Y5/F7/Y7/S5/Y6/Y2 逐个调用, 任一异常被捕获并 log。
        asof: 数据基准日(防未来函数), 实盘不传=今天。
        注: Y2(股东户数, RPT_HOLDERNUMLATEST)与 Y5(概念标签)是"当前快照"类
        数据, 接口本身不提供历史时点; 回测接入时这两类天然带未来性, 由
        调用方决定是否启用(见 Backtester fund_feed 文档)。基准日 < 今天时
        这两类**硬跳过**(不请求不落缓存) —— 硬算只能拿"今天"的值, 对该日是
        未来数据, 且会把今天的值错记成那天的历史(污染 Y2/Y5 每日快照积累,
        规格 §7); 窗口类(Y6/Y7/F7)按 asof 正常计算。
        offline=True(回测侧, 2026-09-17 拍板): 只读缓存 + 只算 Y1/Y8,
        **零网络、零落盘**(见类 docstring)。
        命中缓存时按当日 float_mv 补齐缺的 Y1/Y8(C1): 快照条目不带 float_mv,
        不补就会被短路钉成 0(见 `_complete_calc_factors`)。
        I3: offline=False 且网络类因子(NET_FACTORS)一个都没成功 → 视为取数失败,
        半截条目**不落盘**(留待重试), 否则会被开头短路永久钉死。"""
        key = self._cache_key(code, asof)
        if key in self._cache:
            return self._complete_calc_factors(key, self._cache[key], float_mv)
        if self.offline:
            # 只算纯计算因子, 不请求、不写缓存(离线结果不许钉进历史)
            out = {}
            for name, fn in (("Y1", self._small_cap), ("Y8", self._mid_cap)):
                try:
                    res = fn(float_mv)
                    if res is not None:
                        out[name] = res
                except Exception as e:
                    logger.warning("离线因子 %s(%s) 计算失败: %r", name, code, e)
            return out
        out = {}
        try:
            y1 = self._small_cap(float_mv)
            if y1:
                out["Y1"] = y1
        except Exception as e:
            logger.warning("东财因子 %s(%s) 计算失败, 回落手填: %r", "Y1", code, e)
        try:
            y8 = self._mid_cap(float_mv)
            if y8:
                out["Y8"] = y8
        except Exception as e:
            logger.warning("东财因子 %s(%s) 计算失败, 回落手填: %r", "Y8", code, e)
        # 快照类(Y5/Y2): 接口无历史时点, 基准日在过去 → 计算即造假, 跳过
        snapshot_ok = self._ref(asof) >= date.today()
        for name, fn, kw in ([(x, f, {}) for x, f in
                              (("Y5", self._concepts), ("Y2", self._shareholders))
                              if snapshot_ok] +
                             [("F7", self._novel_concept, {"asof": asof}),
                              ("Y7", self._dragon_tiger, {"asof": asof}),
                              ("S5", self._financing, {}),
                              ("Y6", self._event, {"asof": asof})]):
            try:
                res = fn(code, **kw)
                if res is not None:
                    out[name] = res
            except Exception as e:
                logger.warning("东财因子 %s(%s) 计算失败, 回落手填: %r", name, code, e)
        if not any(n in out for n in NET_FACTORS):
            # I3: 网络类因子一个都没成功(结果只剩 Y1/Y8 这类纯计算值) → 视为
            # "取数失败", **不落盘**, 留待下次重试。落盘就会被开头 `if key in
            # self._cache: return` 永久短路 —— 该 (股,日) 的 F7/Y6/Y7 永远是 0,
            # 之后 fund_snapshot --date 补采/重试命中同键被短路, `_complete_calc_
            # factors` 只补 Y1/Y8 ⇒ 永久缺失; 而 backtest 的"已覆盖"判定是"该日
            # 任一网络类有值" ⇒ 这些天被统计成"已采集", 覆盖数偏乐观。
            # 部分成功(任一网络类有值)照常落盘: 尤其 Y5/Y2 无历史可回补。
            logger.warning("东财因子 %s(%s) 网络类全部失败 → 不落盘(留待重试)",
                           "/".join(NET_FACTORS), code)
            return out
        self._cache[key] = out
        self._save_cache()
        return out
