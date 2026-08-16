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

# 复用连接的会话: 腾讯/东财接口每个请求新建连接约 3 秒, session 复用
# keep-alive 后降到 ~0.2 秒, 回测几百次请求提速 5-10 倍。
_session = requests.Session() if requests is not None else None


# ---------------------------------------------------------------- QMT K线源

def qmt_kline_feed(code):
    """用 QMT 本地 K线(xtdata, 已连接的 miniQMT)拉历史日K → [(date, close), ...]。

    优先从 zt_history 缓存读(构建后秒回, 无网络); 缓存无此股 → 单只拉。
    失败 → []。
    """
    # 优先: zt_history 缓存(全市场K线已落盘)
    try:
        from prism.zt_history import _load_cache
        _cache = _load_cache()
        if _cache:
            rec = _cache.get(str(code).strip().upper())
            if rec and rec.get("dates"):
                return list(zip(rec["dates"], rec["close"]))
    except Exception:
        pass
    # 回退: 单只实时拉
    try:
        from xtquant import xtdata
        s = str(code).strip().upper()
        if "." not in s:
            if s.startswith("6"):
                s += ".SH"
            else:
                s += ".SZ"
        xtdata.download_history_data(s, "1d")
        df = xtdata.get_market_data_ex([], [s], period="1d",
                                       start_time="", end_time="", count=400)
        df = (df or {}).get(s)
        if df is None or len(df) == 0:
            return []
        closes = df["close"].tolist()
        # 日期: 新版毫秒 epoch / 旧版 YYYYMMDD 索引
        if "time" in df.columns:
            from datetime import datetime as _dt
            dates = [_dt.fromtimestamp(int(t) / 1000.0).strftime("%Y-%m-%d")
                     for t in df["time"]]
        else:
            dates = [str(t) for t in df.index]
        return list(zip(dates, closes))
    except Exception:
        return []


def kline_feed(code):
    """K线数据源(优先 QMT 本地, 网络源回退): [(date_str, close), ...] 升序。

    数据源链(逐级回退, 全部失败 → []):
      1. QMT xtdata 本地K线(需 miniQMT 已连接, 最快最稳)
      2. 东财 push2his(历史接口)
      3. 东财 push2(行情接口)
      4. 腾讯 ifzq.gtimg.cn(与东财无关)
    """
    qmt = qmt_kline_feed(code)
    if qmt:
        return qmt
    if requests is None:
        return []
    out = _kline_from("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                      code, extra={"beg": "20200101", "end": "20500101", "lmt": 100000})
    if out:
        return out
    out = _kline_from("https://push2.eastmoney.com/api/qt/stock/kline/get",
                      code, extra={"lmt": 250})
    if out:
        return out
    return _kline_tencent(code)

# ---------------------------------------------------------------- feeds

def zt_feed(date_yyyymmdd):
    """东财历史涨停池 → [{code, boards, theme}]。非交易日/失败 → []。

    优先用 QMT 历史K线生成(zt_history 缓存, 可回溯约1.5年, 无网络依赖);
    缓存不存在时回退东财(仅最近约20天)。"""
    # QMT 本地历史池(优先)
    try:
        from prism.zt_history import qmt_zt_feed, _load_cache
        _cache = _load_cache()
        if _cache:
            pool = qmt_zt_feed(date_yyyymmdd, _cache)
            if pool:
                return pool
    except Exception:
        pass
    # 回退: 东财(最近约20天)
    if requests is None:
        return []
    try:
        resp = _session.get(
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


def _kline_tencent(code):
    """腾讯日K线 → [(date_str, close), ...] 升序。失败 → []。
    param 格式: 市场代码 + ',' + 6位代码, 如 sz000936 / sh600000。"""
    s = str(code).strip().upper()
    if s.startswith("6"):
        mkt = "sh"
    else:
        mkt = "sz"
    code6 = s.split(".")[0][-6:]
    symbol = "%s%s" % (mkt, code6)
    try:
        resp = _session.get(
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
            params={"param": "%s,day,,,400,qfq" % symbol},
            headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = (resp.json().get("data") or {}).get(symbol) or {}
        klines = data.get("qfqday") or data.get("day") or []
    except Exception:
        return []
    out = []
    for line in klines:
        # 腾讯格式: [date, open, close, high, low, volume, ...] — 索引1=开 2=收
        if isinstance(line, list) and len(line) >= 3:
            try:
                out.append((str(line[0]), float(line[2])))
            except (TypeError, ValueError):
                continue
    return out


def _kline_from(url, code, extra=None):
    """从指定东财K线端点拉数据。成功返回 [(date, close)], 失败 → []。"""
    params = {"secid": _secid(code), "klt": 101, "fqt": 1,
              "fields1": "f1,f2,f3", "fields2": "f51,f53"}
    if extra:
        params.update(extra)
    try:
        resp = _session.get(url, params=params, headers=HEADERS, timeout=10)
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
