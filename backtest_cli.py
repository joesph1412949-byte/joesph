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

from prism.backtest import Backtester
from prism.engine import load_strategy
from prism.strategies import STRATEGIES_DIR
import prism.factors  # noqa: F401  触发因子库扫描注册
import prism.registry as _reg

_reg.reset()
_reg.scan_factors("prism.factors", force=True)

HEADERS = {"User-Agent": "Mozilla/5.0",
           "Referer": "http://quote.eastmoney.com/"}

# 复用连接的会话: 腾讯/东财接口每个请求新建连接约 3 秒, session 复用
# keep-alive 后降到 ~0.2 秒, 回测几百次请求提速 5-10 倍。
_session = requests.Session() if requests is not None else None

# zt_history 缓存一次性加载(64MB pickle 每只股票重读太慢, 只读一次)
_zt_cache = None
_zt_index = None


def _get_zt_cache():
    global _zt_cache
    if _zt_cache is None:
        try:
            from prism.zt_history import _load_cache
            _zt_cache = _load_cache()
        except Exception:
            _zt_cache = {}
    return _zt_cache


def _get_zt_index():
    global _zt_index
    if _zt_index is None:
        try:
            from prism.zt_history import _load_index
            _zt_index = _load_index()
        except Exception:
            _zt_index = {}
    return _zt_index


# ---------------------------------------------------------------- QMT K线源

def qmt_kline_feed(code):
    """用 QMT 本地 K线(xtdata, 已连接的 miniQMT)拉历史日K → [(date, close), ...]。

    优先从 zt_history 缓存读(构建后秒回, 无网络); 缓存无此股 → 单只拉。
    缓存一次性加载(模块级 _zt_cache), 避免每只股票重读 64MB pickle。
    失败 → []。
    """
    # 优先: zt_history 缓存(全市场K线已落盘)
    _cache = _get_zt_cache()
    if _cache:
        rec = _cache.get(str(code).strip().upper())
        if rec and rec.get("dates"):
            return list(zip(rec["dates"], rec["close"]))
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
    # QMT 本地历史池(优先, 用一次性加载的缓存+索引)
    _cache = _get_zt_cache()
    if _cache:
        from prism.zt_history import qmt_zt_feed
        pool = qmt_zt_feed(date_yyyymmdd, _cache, index=_get_zt_index())
        if pool:
            return pool
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
    ap = argparse.ArgumentParser(description="Prism 回测 CLI(QMT本地数据, 可回测约1.5年)")
    ap.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    ap.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    ap.add_argument("--strategy", default="default",
                    help="策略 id(prism/strategies/, 默认 default)")
    ap.add_argument("--sell-tp", type=float, default=None, help="止盈%(覆盖策略配置)")
    ap.add_argument("--sell-sl", type=float, default=None, help="止损%(覆盖策略配置)")
    ap.add_argument("--hold", type=int, default=None, help="持有天数(覆盖策略配置)")
    ap.add_argument("--oos", action="store_true",
                    help="运行样本外验证(前后半段对比, 防过拟合)")
    ap.add_argument("--use-market-data", action="store_true",
                    help="注入市场数据层(申万板块K线/全球指数/个股行业映射), 供SEC等板块因子使用")
    args = ap.parse_args()

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    sp = STRATEGIES_DIR / ("%s.json" % args.strategy)
    if not sp.exists():
        print("策略不存在: %s (可用: %s)" % (
            args.strategy,
            ", ".join(p.stem for p in STRATEGIES_DIR.glob("*.json"))))
        return 1
    strategy = load_strategy(sp)
    sell = {}
    if args.sell_tp is not None:
        sell["take_profit_pct"] = args.sell_tp
    if args.sell_sl is not None:
        sell["stop_loss_pct"] = args.sell_sl
    if args.hold is not None:
        sell["max_hold_days"] = args.hold
    bt = Backtester(strategy, zt_feed=zt_feed, kline_feed=kline_feed)

    def progress(d):
        if d.day % 5 == 1:
            print("  回放中... %s" % d, file=sys.stderr)

    mkt = None
    sector_map = None
    if args.use_market_data:
        from prism import market_data as _md
        cache = _md._load_cache()
        if cache:
            # 组装 market_data 结构: sector(板块K线) + global(全球指数)
            mkt = {"sector": cache.get("kline") or {},
                   "global": cache.get("global") or {}}
            smap = cache.get("sector_map") or {}
            # sector_map 期望 {code: 行业代码}, stock_sector 返回行业代码
            sector_map = {}
            for c6, rec in smap.items():
                if rec and rec.get("sector"):
                    suffix = ".SH" if c6.startswith("6") else ".SZ"
                    sector_map[c6 + suffix] = rec["sector"]
            print("市场数据注入: 板块 %d, 全球指数 %d, 个股映射 %d" % (
                len(mkt["sector"]), len(mkt["global"]), len(sector_map)),
                file=sys.stderr)
        else:
            print("警告: 市场数据缓存为空(--build-sectors/--build-global 先采集)",
                  file=sys.stderr)

    if args.oos:
        res = bt.run_oos(start, end, sell_rules=sell or None, progress=progress,
                         mkt=mkt, sector_map=sector_map)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        rep = bt.run(start, end, sell_rules=sell or None, progress=progress,
                     mkt=mkt, sector_map=sector_map)
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
