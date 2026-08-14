# -*- coding: utf-8 -*-
"""回测 CLI: 用东财真实数据(历史涨停池 + 历史K线)运行回测。

用法:
  python backtest_cli.py --start 20260701 --end 20260731
  python backtest_cli.py --start 20260701 --end 20260731 --compare
  python backtest_cli.py --start 20260701 --end 20260731 \
      --min-limit 30 --picks 5 --hold 3

数据源: 东财公开接口(getTopicZTPool 涨停池 / push2his K线), 无需 QMT。
注意: 东财接口有频率限制, 区间越长请求越多(每交易日 1 次池 + 每笔 N 次K线)。
"""
import argparse
import json
import sys
from datetime import date, datetime, timedelta

try:
    import requests
except Exception:
    requests = None

from backtest import BacktestEngine

HEADERS = {"User-Agent": "Mozilla/5.0",
           "Referer": "http://quote.eastmoney.com/"}

# ---------------------------------------------------------------- feeds

def zt_feed(date_yyyymmdd):
    """东财历史涨停池 → [{code, boards, theme}]。非交易日/失败 → []。"""
    if requests is None:
        return []
    try:
        resp = requests.get(
            "https://push2ex.eastmoney.com/getTopicZTPool",
            params={"ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
                    "Pageindex": 0, "pagesize": 1000, "sort": "fbt:asc",
                    "date": date_yyyymmdd},
            headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = (resp.json().get("data") or {}).get("pool") or []
    except Exception:
        return []
    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = item.get("c") or item.get("code")
        if not code:
            continue
        out.append({"code": str(code),
                    "boards": int(item.get("lbc") or 0),
                    "theme": str(item.get("hybk") or item.get("hyb") or "")})
    return out


def _secid(code):
    """东财 secid: 深/北 → '0.code', 沪 → '1.code'。"""
    s = str(code).strip().upper()
    if s.startswith("6"):
        mkt = "1"
    else:
        mkt = "0"
    return "%s.%s" % (mkt, s[:6])


def kline_feed(code):
    """东财日K线 → [(date_str, close), ...] 升序。失败 → []。"""
    if requests is None:
        return []
    try:
        resp = requests.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={"secid": _secid(code), "klt": 101, "fqt": 1,
                    "fields1": "f1,f2,f3", "fields2": "f51,f53",
                    "beg": "20200101", "end": "20500101", "lmt": 100000},
            headers=HEADERS, timeout=10)
        resp.raise_for_status()
        klines = (resp.json().get("data") or {}).get("klines") or []
    except Exception:
        return []
    out = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) >= 2:
            try:
                out.append((parts[0], float(parts[1])))
            except (TypeError, ValueError):
                continue
    return out

# ---------------------------------------------------------------- main

def _parse_date(s):
    return datetime.strptime(s, "%Y%m%d").date()


def main():
    ap = argparse.ArgumentParser(description="东财数据回测")
    ap.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    ap.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    ap.add_argument("--min-limit", type=int, default=20, help="环境门槛: 涨停家数≥N(默认20)")
    ap.add_argument("--picks", type=int, default=3, help="每天最多选几只(默认3)")
    ap.add_argument("--hold", type=int, default=5, help="持有交易日数(默认5)")
    ap.add_argument("--compare", action="store_true",
                    help="运行多组参数对比(环境门槛/选股数/持有天数)")
    args = ap.parse_args()

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    eng = BacktestEngine(zt_feed=zt_feed, kline_feed=kline_feed)

    def progress(d):
        if d.day % 5 == 1:
            print("  回放中... %s" % d, file=sys.stderr)

    if args.compare:
        grid = []
        for min_l in (10, 20, 30):
            for picks in (1, 3, 5):
                for hold in (1, 3, 5):
                    grid.append({"min_limit_count": min_l,
                                 "max_picks": picks, "hold_days": hold})
        rows = eng.compare_params(start, end, grid, progress=progress)
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        rep = eng.run(start, end,
                      params={"min_limit_count": args.min_limit,
                              "max_picks": args.picks, "hold_days": args.hold},
                      progress=progress)
        print(json.dumps(rep, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
