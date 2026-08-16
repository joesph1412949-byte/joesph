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

# 拉多少根日K(约1.5年交易日)
KLINE_COUNT = 400


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
    """下载全市场日K线并构建涨停判断缓存。

    返回 {"codes": n, "days": n, "cache_size": bytes}。耗时较长(首次30-60分)。
    cache 结构: {code: {"dates": [YYYY-MM-DD...], "close": [...], "pre": [...]}}
    (pre = 前日收盘, 用于涨停判断; 升序, 最后=最新)
    """
    from xtquant import xtdata
    t0 = time.time()
    codes = xtdata.get_stock_list_in_sector("沪深A股")
    logger.info("全市场 %d 只, 开始批量下载日K线...", len(codes))
    # 分批下载, 每批 200 只(避免单次请求过大)
    batch = 200
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        xtdata.download_history_data2(chunk, "1d", start_time="", end_time="")
        if progress:
            progress(i + len(chunk), len(codes))
    # 读取全部
    cache = {}
    all_days = set()
    for i in range(0, len(codes), batch):
        chunk = codes[i:i + batch]
        data = xtdata.get_market_data_ex([], chunk, period="1d",
                                         start_time="", end_time="",
                                         count=KLINE_COUNT)
        for code, df in (data or {}).items():
            if df is None or len(df) < 2:
                continue
            closes = df["close"].tolist()
            # preClose 字段(QMT 提供)最准; 缺失则用前一日收盘
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
            all_days.update(dates)
        if progress:
            progress(i + len(chunk), len(codes))
    CACHE_PATH.write_bytes(pickle.dumps(cache, protocol=4))
    logger.info("缓存构建完成: %d 只, %d 天, %.1f MB, 耗时 %.0f 秒",
                len(cache), len(all_days),
                CACHE_PATH.stat().st_size / 1e6, time.time() - t0)
    return {"codes": len(cache), "days": len(all_days),
            "cache_size": CACHE_PATH.stat().st_size}


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


def build_cli():
    """CLI: python -m prism.zt_history [--limit N] [--date YYYYMMDD]"""
    import argparse
    ap = argparse.ArgumentParser(description="历史涨停池缓存构建")
    ap.add_argument("--build", action="store_true", help="构建/重建缓存")
    ap.add_argument("--date", default="", help="查询某日涨停池 YYYYMMDD")
    ap.add_argument("--stats", action="store_true", help="显示缓存统计")
    args = ap.parse_args()
    if args.build:
        def prog(done, total):
            sys.stdout.write("\r下载 %d/%d" % (done, total))
            sys.stdout.flush()
        r = build_cache(progress=prog)
        print("\n构建完成:", r)
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
