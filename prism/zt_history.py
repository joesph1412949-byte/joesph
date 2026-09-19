# -*- coding: utf-8 -*-
"""历史涨停池生成器 — 用 QMT 本地日K线判断"哪些股票哪天涨停"。

突破东财涨停池仅保留约20天的限制: 只要 QMT 本地有K线(通常1.5年+),
就能生成任意历史区间的涨停池, 用于拉长回测。

判断逻辑(与真实涨跌停规则一致):
  当日收盘 >= 前日收盘 × (1 + 涨停幅度) - 0.01 容差 → 当日涨停
  涨停幅度: 北交所(8/4开头) 30% / 创业板科创(300/301/688) 20% / 主板 10%
  ST股5%需名称判断, 本地K线无名称 → 主板ST会被误判为10%涨停(保守可接受:
  ST涨停本来就少, 且策略主要选科技主板)。

首次构建: 下载全市场K线(5209只, 约30-60分钟, 后台跑) + 本地缓存;
之后秒回。缓存文件: <项目根>/.zt_history_cache.pkl
"""
import logging
import os
import pickle
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from shared.common import CACHE_DIR, replace_with_retry

logger = logging.getLogger(__name__)

# 缓存路径: 统一收在 runtime/cache/ (见 shared/common.py)
CACHE_PATH = CACHE_DIR / ".zt_history_cache.pkl"
# 按日索引路径: date → [{code, boards}], 查询 O(1)
INDEX_PATH = CACHE_DIR / ".zt_history_index.pkl"

# 拉多少根日K(约1.5年交易日)
KLINE_COUNT = 400
# 单批 xtdata 调用看门狗超时(秒)。QMT 下载/读取偶发永久挂起(实测同批次
# 重复卡死), 超时抛 TimeoutError 让 build_cache 跳过该批(增量续建不丢进度)。
DOWNLOAD_TIMEOUT = 90.0
# 索引陈旧阈值(自然日): prev_day_pool 里索引最新日期距今超过该天数 →
# 停供空契约(fail-closed)。10 天覆盖国庆/春节 8 天长假。
STALE_DAYS = 10


def _run_with_timeout(fn, args=(), kwargs=None, timeout=DOWNLOAD_TIMEOUT):
    """带看门狗执行 fn(*args, **kwargs)。

    超时 → raise TimeoutError(执行线程无法终止, 作为 daemon 泄漏,
    但主流程可继续处理后续批次); fn 内部异常 → 原样转交; 正常 → 返回值。
    """
    import threading
    kwargs = kwargs or {}
    box = {}

    def _target():
        try:
            box["ret"] = fn(*args, **kwargs)
        except BaseException as e:   # 原样转交主线程(含 KeyboardInterrupt)
            box["err"] = e

    th = threading.Thread(target=_target, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError("call timed out after %ss" % timeout)
    if "err" in box:
        raise box["err"]
    return box.get("ret")


def _limit_ratio(code):
    """涨停幅度: 北交所30% / 创业科创20% / 主板10%。"""
    c = str(code).strip()
    if c.startswith(("8", "4")):
        return 0.30
    if c.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


def _with_suffix(code):
    s = str(code).strip().upper()
    if "." in s:
        return s
    if s.startswith("6"):
        return s + ".SH"
    if s.startswith(("0", "3")):
        return s + ".SZ"
    return s


def _df_to_records(df):
    """xtdata 日K DataFrame → 缓存记录 {dates, close, pre}; 不足2根 → None。

    提取逻辑与原 build_cache 内联段一致(time 毫秒列优先, index 兜底;
    preClose 列优先, 缺列用 close 前移), build_cache/refresh_cache 共用。
    """
    if df is None or len(df) < 2:
        return None
    closes = df["close"].tolist()
    if "preClose" in df.columns:
        pre = df["preClose"].tolist()
    else:
        pre = [closes[0]] + closes[:-1]
    if "time" in df.columns:
        dates = [datetime.fromtimestamp(int(t) / 1000.0).strftime("%Y-%m-%d")
                 for t in df["time"]]
    else:
        dates = [str(t) for t in df.index]
    return {"dates": dates, "close": closes, "pre": pre}


# 同进程写者串行锁(A, 2026-09-18): 唯一 tmp 名只解决"互踩同一个 tmp"; 两个线程
# 同时 os.replace 到**同一个目标**在 Windows 上仍会 PermissionError(实测: 只改
# tmp 名 200 轮仍炸 26 轮, 加锁后 0 轮)。跨进程靠唯一 tmp 名, 进程内靠这把锁。
# 更正(2026-09-18 晚, M12): 上面那组数字只在**无并发读者**时成立 —— 有线程
# 正在读这个 pkl 时, 单个写者照样 EACCES(实测 2 reader 线程 291/300 轮),
# 那把锁对此**无效**(它只串行写者)。读者场景见 replace_with_retry。
# ponytail: 全局一把锁 —— 写盘是低频(采集/刷新)操作, 不构成瓶颈; 真变重再按路径分锁。
_WRITE_LOCK = threading.Lock()


def _atomic_pickle(path, obj):
    """原子写 pickle: 先写 tmp 再 os.replace(中断不留半个文件)。

    tmp 名带 pid+线程 id(A, 2026-09-18; 与 shared/common.atomic_write 同一手法):
    固定的 `path+'.tmp'` 在**同进程多写者**下会互踩 —— 两个线程同时写同一个 tmp,
    前者的 os.replace 撞上后者的 write_bytes → PermissionError/FileNotFoundError
    (实测 200 轮 94 轮抛错), 或发布半截 pkl; 而 `_load_cache`/`_load_index` 把
    坏文件**静默当 {}** ⇒ 90 天 zt 缓存被无声清零。现实触发: 守护的"强制刷新+
    重试"在首个刷新线程 join 超时后照样起第二个刷新线程(冷缓存 90 天×全 A 股
    可达 >120s)。

    os.replace 走 shared.common.replace_with_retry(M12, 2026-09-18): 唯一 tmp 名
    与写者锁都挡不住**读者**占着目标句柄(Windows 的 open 不带
    FILE_SHARE_DELETE); 刷新线程写的时候 `_load_cache`/`qmt_zt_feed` 正在读是
    常态, 有界退避等读者关句柄再落地, 预算耗尽则原样抛错(不静默当成功)。

    落地失败连 tmp 一起收掉(M13, 2026-09-18): 唯一名没人会复用/清理它。
    """
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(),
                                           threading.get_ident()))
    with _WRITE_LOCK:
        tmp.write_bytes(pickle.dumps(obj, protocol=4))
        try:
            replace_with_retry(str(tmp), str(path))
        except BaseException:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise


def _merge_tail(old, rec, start_fmt):
    """尾部合并: 旧缓存 date<START 的头部保留, 新记录整体替换 >=START 尾巴,
    按日期升序接上(排序稳定, 头部本就升序)。新股 old=None → 纯新尾部。"""
    if not old:
        triples = list(zip(rec["dates"], rec["close"], rec["pre"]))
    else:
        triples = [(d, c, p) for d, c, p in zip(old["dates"], old["close"],
                                                old["pre"]) if d < start_fmt]
        triples += list(zip(rec["dates"], rec["close"], rec["pre"]))
    triples.sort(key=lambda x: x[0])
    return {"dates": [t[0] for t in triples],
            "close": [t[1] for t in triples],
            "pre": [t[2] for t in triples]}


def _batch_download(codes, start, count, cache, apply_fn, progress=None,
                    processed0=0, total=None, t0=None):
    """批200 下载→看门狗读取→写入的共用骨架(build_cache/refresh_cache 复用)。

    语义与原两个函数逐行一致: 批下载/批读取失败或超时 → 跳过该批(增量不丢),
    每 5 批原子落盘(中断不留半个文件), 每批回调 progress。

    codes: 待处理代码; start: download/read 的 start_time(8位串, ""=全量);
    count: 单码读取 K 线根数(0=按 start_time 决定);
    cache: 落盘用的缓存 dict(_atomic_pickle 直接写它);
    apply_fn(code, df) 决定写入方式(全量赋值 / _merge_tail 尾部合并);
    processed0/total: 进度起点与分母(build_cache 续建时起点=已有缓存数)。
    """
    from xtquant import xtdata
    t0 = time.time() if t0 is None else t0
    total = len(codes) if total is None else total
    batch = 200
    processed = processed0
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        try:
            _run_with_timeout(xtdata.download_history_data2,
                              (chunk, "1d"),
                              {"start_time": start, "end_time": ""})
        except Exception as e:
            logger.warning("第 %d 批下载失败/超时: %r (跳过该批)", i // batch, e)
            processed += len(chunk)
            if progress:
                progress(processed, total)
            continue
        try:
            data = _run_with_timeout(
                xtdata.get_market_data_ex,
                ([], chunk),
                {"period": "1d", "start_time": start, "end_time": "",
                 "count": count})
        except Exception as e:
            logger.warning("第 %d 批读取失败/超时: %r (跳过该批)", i // batch, e)
            processed += len(chunk)
            if progress:
                progress(processed, total)
            continue
        for code, df in (data or {}).items():
            apply_fn(code, df)
        processed += len(chunk)
        # 每 5 批增量保存一次
        if (i // batch) % 5 == 4:
            _atomic_pickle(CACHE_PATH, cache)
            logger.info("增量保存: %d 只 (%.0f秒)", len(cache),
                        time.time() - t0)
        if progress:
            progress(processed, total)


def build_cache(progress=None):
    """下载全市场日K线并构建涨停判断缓存(增量保存, 中断不丢进度)。

    返回 {"codes": n, "cache_size": bytes}。耗时较长(首次30-60分)。
    cache 结构: {code: {"dates": [YYYY-MM-DD...], "close": [...], "pre": [...]}}
    (pre = 前日收盘, 用于涨停判断; 升序, 最后=最新)

    增量策略: 每处理 5 批(1000只)写一次缓存; 若中断, 已处理部分保留。
    注意: 只补缓存里没有的代码, 存量K线尾部更新走 refresh_cache()。
    """
    from xtquant import xtdata
    t0 = time.time()
    codes = xtdata.get_stock_list_in_sector("沪深A股")
    # 已有缓存(增量续建)
    cache = _load_cache()
    todo = [c for c in codes if c not in cache]
    logger.info("全市场 %d 只, 已缓存 %d, 待下载 %d...",
                len(codes), len(cache), len(todo))

    def _apply(code, df):
        rec = _df_to_records(df)
        if rec is not None:
            cache[code] = rec

    _batch_download(todo, "", KLINE_COUNT, cache, _apply, progress=progress,
                    processed0=len(cache), total=len(codes), t0=t0)
    _atomic_pickle(CACHE_PATH, cache)
    logger.info("缓存构建完成: %d 只, %.1f MB, 耗时 %.0f 秒",
                len(cache), CACHE_PATH.stat().st_size / 1e6,
                time.time() - t0)
    return {"codes": len(cache), "cache_size": CACHE_PATH.stat().st_size}


def refresh_cache(progress=None, days=90):
    """增量刷新存量K线尾部: 重拉最近 days 天, 整体替换旧缓存 >=START 的尾巴。

    与 build_cache 的分工: build_cache 只补缓存里没有的代码(存量永不更新,
    缓存会冻在首次构建日), 本函数反着干 — 复用 _batch_download 批处理骨架
    (批200/看门狗/批失败跳过/每5批增量保存), 对所有沪深A股从
    START=今天-days 重拉, 旧记录 date<START 原样保留, >=START 用新拉替换。

    限制: 新股(缓存没有的代码)只有 START 以来的尾部, 缺更早历史 —
    要全量 KLINE_COUNT 根仍走 build_cache()。

    返回 {"codes": len(cache), "last_date": 全缓存最大日期 or None}。
    """
    from xtquant import xtdata
    t0 = time.time()
    codes = xtdata.get_stock_list_in_sector("沪深A股")
    cache = _load_cache()
    start = (date.today() - timedelta(days=days)).strftime("%Y%m%d")
    start_fmt = "%s-%s-%s" % (start[:4], start[4:6], start[6:8])
    logger.info("刷新 %d 只(>= %s 尾部重拉, 已缓存 %d)...",
                len(codes), start_fmt, len(cache))

    def _apply(code, df):
        rec = _df_to_records(df)
        if rec is None:
            return
        # ponytail: 新股也进但只有尾部(缺更早历史), 全量走 build_cache
        cache[code] = _merge_tail(cache.get(code), rec, start_fmt)

    _batch_download(codes, start, 0, cache, _apply, progress=progress, t0=t0)
    _atomic_pickle(CACHE_PATH, cache)
    last_date = max((d for rec in cache.values() for d in rec["dates"]),
                    default=None)
    logger.info("缓存刷新完成: %d 只, 最新 %s, 耗时 %.0f 秒",
                len(cache), last_date, time.time() - t0)
    return {"codes": len(cache), "last_date": last_date}


def _load_cache():
    if CACHE_PATH.exists():
        try:
            return pickle.loads(CACHE_PATH.read_bytes())
        except Exception:
            return {}
    return {}


def _limit_up_price(pre, ratio):
    """A股涨停价 = 前收 × (1+幅度), 四舍五入到分(0.01)。"""
    return round(pre * (1 + ratio), 2)


def build_index(cache=None):
    """从K线缓存构建按日索引: {date: [{code, boards}]}, 查询 O(1)。

    回测逐日查询涨停池时, 避免每次遍历全市场 5209 只。构建约 10-30 秒。
    """
    cache = cache if cache is not None else _load_cache()
    if not cache:
        return {}
    by_day = {}
    for code, rec in cache.items():
        dates, closes, pre = rec["dates"], rec["close"], rec["pre"]
        ratio = _limit_ratio(code)
        n = len(dates)
        for i in range(n):
            if i == 0:
                continue   # 第一天无前收, 不判
            c = closes[i]
            p = pre[i]
            if not p or c <= 0:
                continue
            if c >= _limit_up_price(p, ratio) - 0.001:
                # 连板数
                boards = 1
                j = i - 1
                while j >= 0:
                    c2 = closes[j]
                    p2 = pre[j]
                    if p2 and c2 >= _limit_up_price(p2, ratio) - 0.001:
                        boards += 1
                        j -= 1
                    else:
                        break
                by_day.setdefault(dates[i], []).append(
                    {"code": code, "boards": boards})
    _atomic_pickle(INDEX_PATH, by_day)
    return by_day


def _load_index():
    if INDEX_PATH.exists():
        try:
            return pickle.loads(INDEX_PATH.read_bytes())
        except Exception:
            return {}
    return {}


def _day_key(s):
    """索引键/入参 → date; 认得 date/datetime 与 "YYYY-MM-DD"/"YYYYMMDD",
    其余(None/空串/垃圾/非法日期) → None。

    与 prev_day_pool 的 _n8 同口径(索引键实测是 "YYYY-MM-DD", 兼容 8 位)。
    """
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    try:
        return datetime.strptime(str(s).replace("-", ""), "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def trading_days(index=None):
    """按日索引里的交易日集合(set[date]); 索引缺失/为空/键全非法 → None(未知)。

    只读: 复用 _load_index()(与 qmt_zt_feed/prev_day_pool 同一份解析与同一个
    文件), 绝不重建/刷新缓存, 也不在索引缺失时凭空造一个空索引文件。

    判据来源: .zt_history_index.pkl 的键就是交易日 —— 索引由各股日K日期生成,
    只有交易日才可能出现在涨停池里。本机实测(2026-09-19): 413 天、每天 ≥32 家;
    2026 春节 02-16~02-20 五个工作日 0 天, 02-13/02-24 都在册。
    """
    if index is None:
        index = _load_index()
    if not index:
        return None
    days = {d for d in (_day_key(k) for k in index) if d is not None}
    return days or None


def is_trading_day(day, index=None):
    """day 是不是交易日: True / False / **None(未知)**。

    - 索引缺失/为空/键全非法 → None(未知; **绝不**据此判"不能交易" ——
      冷缓存/新机器锁死系统比漏判一个节假日更糟, paused/armed 人工闸门仍在)
    - day 在索引里 → True(只有交易日才可能出现在按日涨停池索引里)
    - day 早于索引最大日 → False(fail-closed: 索引已覆盖这天**之后**,
      所以"缺席"是结论性的; 本机实测 2026 春节 02-16~02-20 五个工作日都缺席)
    - day 不早于索引最大日(含"今天") → None(索引**还没覆盖**这天)

    ⚠ 已知上限(2026-09-19 实测): 当日日K收盘后才落地 ⇒ 盘中索引里没有今天,
    于是 is_trading_day(今天) 恒为 None, **工作日节假日的盘中判不出来**。
    这不是保守取值而是信息上限 —— "今天缺席"既可能是节假日, 也可能只是今天的
    数据还没到, 两者在收盘前不可区分。要盘中挡住它必须另找同日可用的交易日历
    (需要拍板: 另做假日表 / 用 xtdata.get_trading_dates / 东财当日空池探针)。
    反向的错更危险: 若用"不在册 → 非交易日"判今天, 每一个交易日都会被判死
    (交易日盘中必然"今天不在册"), 等于全时段锁死交易。

    只读、不抛: 坏输入返回 None 交给调用方(prism/schedule.in_session)回落 weekday。
    """
    days = trading_days(index)
    if not days:
        return None
    d = _day_key(day)
    if d is None:
        return None
    if d in days:
        return True
    if d < max(days):
        return False
    return None


def qmt_zt_feed(date_yyyymmdd, cache=None, index=None):
    """按日期返回涨停池 [{code, boards, theme}]。无缓存/非交易日 → []。

    优先用按日索引(INDEX_PATH, O(1)); 索引不存在时退化为全表扫描。
    """
    if index is None:
        index = _load_index()
    if index:
        if len(date_yyyymmdd) == 8:
            day_fmt = "%s-%s-%s" % (date_yyyymmdd[:4],
                                    date_yyyymmdd[4:6],
                                    date_yyyymmdd[6:8])
        else:
            day_fmt = date_yyyymmdd
        items = index.get(day_fmt) or []
        return [{"code": it["code"], "boards": it["boards"], "theme": ""}
                for it in items]
    # 退化: 全表扫描
    cache = cache if cache is not None else _load_cache()
    if not cache:
        return []
    day = date_yyyymmdd  # "YYYYMMDD"
    if len(day) == 8:
        day_fmt = "%s-%s-%s" % (day[:4], day[4:6], day[6:8])
    else:
        day_fmt = day
    out = []
    for code, rec in cache.items():
        dates = rec["dates"]
        try:
            idx = dates.index(day_fmt)
        except ValueError:
            continue
        close = rec["close"][idx]
        pre = rec["pre"][idx]
        if not pre or close <= 0:
            continue
        ratio = _limit_ratio(code)
        limit_px = _limit_up_price(pre, ratio)
        if close >= limit_px - 0.001:
            boards = 1
            j = idx - 1
            while j >= 0:
                c2 = rec["close"][j]
                p2 = rec["pre"][j]
                if p2 and c2 >= _limit_up_price(p2, ratio) - 0.001:
                    boards += 1
                    j -= 1
                else:
                    break
            out.append({"code": code, "boards": boards, "theme": ""})
    return out


def prev_day_pool(today=None):
    """上一交易日涨停代码表(F9 昨日基准)。

    today: "YYYY-MM-DD" 或 date; 默认今天。取索引中 < today 的最大日期,
    池条目取其 code 列表。无更早数据/空索引 → {"date": None, "codes": []}。
    只读本地索引, 不触网。

    键格式兼容(实测): .zt_history_index.pkl 的键是 "YYYY-MM-DD"
    (build_index 以K线日期写入, qmt_zt_feed 查询也归一成该格式),
    比较前两侧统一归一为 YYYYMMDD, 同时兼容 "YYYYMMDD" 键。
    条目 code 直接透传(zt_history 缓存本就是 QMT 带后缀格式, 如
    "600051.SH") — 与 sector_map 键(带 .SH/.SZ 后缀)同格式契约, 见
    factor_f9_sector_expansion docstring。

    陈旧保护: 索引最大日期距 today 超过 STALE_DAYS(10) 自然日 →
    返回空契约 + warning(索引停更时 F9 会拿旧池当"昨日"——静默错数据;
    10 天口径覆盖国庆/春节长假)。修复: python -m prism.zt_history --refresh。
    """
    index = _load_index() or {}
    if not index:
        return {"date": None, "codes": []}
    if today is None:
        import datetime as _dt
        today = _dt.date.today()
    t = today.strftime("%Y%m%d") if hasattr(today, "strftime") \
        else str(today).replace("-", "")

    def _n8(s):
        return str(s).replace("-", "")

    try:
        today_d = datetime.strptime(t, "%Y%m%d").date()
    except ValueError:
        return {"date": None, "codes": []}

    def _day8(s):
        """索引键 → date; 非法键 → None(跳过)。"""
        try:
            return datetime.strptime(_n8(s), "%Y%m%d").date()
        except ValueError:
            return None

    parsed = [d for d in map(_day8, index) if d]
    latest = max(parsed) if parsed else None
    if latest is None or (today_d - latest).days > STALE_DAYS:
        logger.warning("涨停池索引陈旧(最新 %s, 距今>%d 自然日) → "
                       "F9 昨日池停供(fail-closed); "
                       "跑 python -m prism.zt_history --refresh 修复",
                       latest, STALE_DAYS)
        return {"date": None, "codes": []}
    days = sorted((d for d in index if _n8(d) < t), key=_n8)
    if not days:
        return {"date": None, "codes": []}
    day8 = _n8(days[-1])
    items = qmt_zt_feed(day8, index=index) or []
    return {"date": "%s-%s-%s" % (day8[:4], day8[4:6], day8[6:8]),
            "codes": [it.get("code") for it in items if it.get("code")]}


def build_cli():
    """CLI: python -m prism.zt_history [--limit N] [--date YYYYMMDD]"""
    import argparse
    ap = argparse.ArgumentParser(description="历史涨停池缓存构建")
    ap.add_argument("--build", action="store_true", help="构建/重建K线缓存")
    ap.add_argument("--refresh", action="store_true",
                    help="增量刷新存量K线尾部(近90天)并重建索引")
    ap.add_argument("--build-index", action="store_true",
                    help="从K线缓存构建按日索引(查询加速)")
    ap.add_argument("--date", default="", help="查询某日涨停池 YYYYMMDD")
    ap.add_argument("--stats", action="store_true", help="显示缓存统计")
    args = ap.parse_args()
    if args.build:
        def prog(done, total):
            sys.stdout.write("\r下载 %d/%d" % (done, total))
            sys.stdout.flush()
        r = build_cache(progress=prog)
        print("\n构建完成:", r)
        idx = build_index()
        print("按日索引: %d 天" % len(idx))
        return
    if args.refresh:
        def prog(done, total):
            sys.stdout.write("\r刷新 %d/%d" % (done, total))
            sys.stdout.flush()
        r = refresh_cache(progress=prog)
        print("\n刷新完成:", r)
        idx = build_index()
        print("按日索引: %d 天" % len(idx))
        return
    if args.build_index:
        idx = build_index()
        print("按日索引: %d 天" % len(idx))
        return
        return
    cache = _load_cache()
    if args.stats or not args.date:
        print("缓存股票数:", len(cache))
        if cache:
            first = next(iter(cache.values()))
            print("K线天数:", len(first["dates"]),
                  "范围:", first["dates"][0], "→", first["dates"][-1])
        return
    pool = qmt_zt_feed(args.date, cache)
    print("%s 涨停 %d 只:" % (args.date, len(pool)))
    for s in pool[:20]:
        print("  ", s["code"], "连板", s["boards"])
    if len(pool) > 20:
        print("  ... 共 %d 只" % len(pool))


if __name__ == "__main__":
    build_cli()
