# -*- coding: utf-8 -*-
"""每日基本面快照采集(规格 2026-09-16 §7): Y2/Y5 快照类因子的真实历史积累。

收盘后对当日涨停池(或指定代码表)逐只调 FundamentalFeed.compute_for_stock
(**不传 asof = 当日快照**), 结果由 feed 自身落既有缓存
(runtime/cache/fundamental_cache.json, 键 YYYYMMDD:code 按基准日分日) ——
从今天起积累真实历史。守护 15:05 选股后挂钩子(paper_daemon), 也可手动跑:

    python -m prism.fund_snapshot [--date YYYYMMDD] [--codes 000001.SZ ...]

无未来约束: 快照只写"今天"或指定**历史**日; 未来日期一律拒绝(未来键会让
回测把今天的数据当成那天的事实)。测试注入假 feed, 绝不联网。
"""
import argparse
import json
import sys
from datetime import date, datetime

from datasource.fundamental import FundamentalFeed


def _today():
    return date.today()


def _parse_date(v):
    """'YYYYMMDD' / 'YYYY-MM-DD' / date → date; 非法 → ValueError。"""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError("日期格式应为 YYYYMMDD 或 YYYY-MM-DD: %r" % (v,))


def default_codes(day=None):
    """当日(默认)或指定历史日的涨停池代码表(本地 zt 索引, 只读不触网)。空 → []。

    索引异常 → [] (采集是锦上添花, 绝不因索引问题炸调用方)。
    """
    from prism import zt_history
    try:
        pool = zt_history.qmt_zt_feed(_parse_date(day or _today())
                                      .strftime("%Y%m%d")) or []
    except Exception:
        return []
    return [s.get("code") for s in pool if s.get("code")]


def snapshot_once(codes, date=None, feed=None):
    """对 codes 逐只采集当日(或指定历史日)基本面快照, 经 feed 落既有缓存。

    date=None → 今天(compute_for_stock 不传 asof, feed 内部基准日=今天;
    date 给定 → asof=该日(F7/Y6/Y7 按 asof 切窗口; 未来日期拒绝)。
    Y5/Y2(快照类目标因子)只在基准日=今天时采集 —— 接口无历史时点, feed 对
    过去基准日硬跳过(见 datasource 防未来测试), 因此 --date 回填的是
    F7/Y6/Y7 的窗口值, Y2/Y5 的真实历史只能从今天起逐日积累。
    float_mv 不传: Y1/Y8 是纯计算(随时可按历史 float_mv 重算), 快照的目标
    是 Y2/Y5 这类"无历史可回补"的接口值 + F7/Y6/Y7 的 asof 当日值。
    feed: 可注入(测试假实现); 缺省真实 FundamentalFeed(共享既有缓存文件,
    compute_for_stock 自带 _cache_key/_save_cache, 重复采集天然幂等)。
    返回 {"saved": n, "failed": n}: saved=采到≥1 个因子值; failed=抛异常或
    一无所获(全因子 fail-open 空 dict, 记失败不虚报)。单股失败绝不阻塞其余。
    """
    feed = feed or FundamentalFeed()
    asof = None
    if date is not None:
        asof = _parse_date(date)
        if asof > _today():
            raise ValueError(
                "快照不采集未来日期(会写未来键污染回测): %s" % asof)
    saved = failed = 0
    for code in codes:
        try:
            if asof is None:
                out = feed.compute_for_stock(code) or {}
            else:
                out = feed.compute_for_stock(code, asof=asof) or {}
        except Exception:
            failed += 1
            continue
        if out:
            saved += 1
        else:
            failed += 1
    return {"saved": saved, "failed": failed}


def main(argv=None):
    """CLI: [--date YYYYMMDD] [--codes c1 c2 ...] → 打印 {"saved": n, "failed": n}。

    codes 缺省 = **快照基准日**的涨停池(本地 zt 索引; 今天 → 当日池, 指定历史日
    → 该日池)。无数据 → 提示后 0 采集退出, 不算失败。
    """
    ap = argparse.ArgumentParser(
        description="每日基本面快照采集(Y2/Y5, 规格 §7): 对当日涨停池逐只调 "
                    "FundamentalFeed.compute_for_stock 并落既有缓存")
    ap.add_argument("--date", default=None,
                    help="快照基准日 YYYYMMDD(默认今天; 只允许今天或历史日)")
    ap.add_argument("--codes", nargs="+", default=None,
                    help="代码表(缺省=快照基准日的涨停池, 读本地 zt 索引)")
    args = ap.parse_args(argv)
    codes = args.codes or default_codes(args.date)
    if not codes:
        print("警告: 本地涨停池索引无该日数据 → 未采集(可用 --codes 指定)",
              file=sys.stderr)
        print(json.dumps({"saved": 0, "failed": 0}, ensure_ascii=False))
        return 0
    try:
        res = snapshot_once(codes, date=args.date)
    except ValueError as e:
        print("错误: %s" % e, file=sys.stderr)
        return 2
    print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
