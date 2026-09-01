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
import time
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# 缓存路径(与 common.LOG_DIR 同级)
CACHE_PATH = Path(__file__).parent.parent / ".zt_history_cache.pkl"
# 按日索引路径: date → [{code, boards}], 查询 O(1)
INDEX_PATH = Path(__file__).parent.parent / ".zt_history_index.pkl"

# 拉多少根日K(约1.5年交易日)
KLINE_COUNT = 400
# 单批 xtdata 调用看门狗超时(秒)。QMT 下载/读取偶发永久挂起(实测同批次
# 重复卡死), 超时抛 TimeoutError 让 build_cache 跳过该批(增量续建不丢进度)。
DOWNLOAD_TIMEOUT = 90.0


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


def build_cache(progress=None):
    """下载全市场日K线并构建涨停判断缓存(增量保存, 中断不丢进度)。

    返回 {"codes": n, "cache_size": bytes}。耗时较长(首次30-60分)。
    cache 结构: {code: {"dates": [YYYY-MM-DD...], "close": [...], "pre": [...]}}
    (pre = 前日收盘, 用于涨停判断; 升序, 最后=最新)

    增量策略: 每处理 5 批(1000只)写一次缓存; 若中断, 已处理部分保留。
    """
    from xtquant import xtdata
    t0 = time.time()
    codes = xtdata.get_stock_list_in_sector("沪深A股")
    # 已有缓存(增量续建)
    cache = _load_cache()
    todo = [c for c in codes if c not in cache]
    logger.info("全市场 %d 只, 已缓存 %d, 待下载 %d...",
                len(codes), len(cache), len(todo))
    batch = 200
    processed = len(cache)
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        try:
            _run_with_timeout(xtdata.download_history_data2,
                              (chunk, "1d"),
                              {"start_time": "", "end_time": ""})
        except Exception as e:
            logger.warning("第 %d 批下载失败/超时: %r (跳过该批)", i // batch, e)
            processed += len(chunk)
            if progress:
                progress(processed, len(codes))
            continue
        try:
            data = _run_with_timeout(
                xtdata.get_market_data_ex,
                ([], chunk),
                {"period": "1d", "start_time": "", "end_time": "",
                 "count": KLINE_COUNT})
        except Exception as e:
            logger.warning("第 %d 批读取失败/超时: %r (跳过该批)", i // batch, e)
            processed += len(chunk)
            if progress:
                progress(processed, len(codes))
            continue
        for code, df in (data or {}).items():
            if df is None or len(df) < 2:
                continue
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
            cache[code] = {"dates": dates, "close": closes, "pre": pre}
        processed += len(chunk)
        # 每 5 批增量保存一次
        if (i // batch) % 5 == 4:
            CACHE_PATH.write_bytes(pickle.dumps(cache, protocol=4))
            logger.info("增量保存: %d 只 (%.0f秒)", len(cache),
                        time.time() - t0)
        if progress:
            progress(processed, len(codes))
    CACHE_PATH.write_bytes(pickle.dumps(cache, protocol=4))
    logger.info("缓存构建完成: %d 只, %.1f MB, 耗时 %.0f 秒",
                len(cache), CACHE_PATH.stat().st_size / 1e6,
                time.time() - t0)
    return {"codes": len(cache), "cache_size": CACHE_PATH.stat().st_size}


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


def qmt_zt_feed(date_yyyymmdd, cache=None):
    """按日期返回涨停池 [{code, boards, theme}]。无缓存/非交易日 → []。

    判断: close >= round(pre × (1+幅度), 2)(四舍五入到分的真实涨停价)。
    boards(连板数): 往前数连续涨停几天。
    """
    cache = cache if cache is not None else _load_cache()
    if not cache:
        return []
    day = date_yyyymmdd  # "YYYYMMDD"
    # 归一化成 YYYY-MM-DD 比对
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
            # 连板数: 往前数连续涨停几天
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
    INDEX_PATH.write_bytes(pickle.dumps(by_day, protocol=4))
    return by_day


def _load_index():
    if INDEX_PATH.exists():
        try:
            return pickle.loads(INDEX_PATH.read_bytes())
        except Exception:
            return {}
    return {}


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
