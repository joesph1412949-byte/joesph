# -*- coding: utf-8 -*-
"""回测 CLI: 用东财真实数据(历史涨停池 + 历史K线)运行回测。

用法:
  python -m backtest.cli --start 20260701 --end 20260731
  python -m backtest.cli --start 20260701 --end 20260731 --compare
  python -m backtest.cli --start 20260701 --end 20260731 \
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
    """用 QMT 本地 K线(xtdata, 已连接的 miniQMT)拉历史日K
    → [(date, open, high, low, close, volume), ...] 全量 OHLCV 契约。

    优先从 zt_history 缓存读(构建后秒回, 无网络); 老缓存只存了 close →
    退回 2 元组旧契约(volume 由 backtest 占位, 量能因子失效属预期)。
    缓存无此股 → 单只实时拉(带完整 OHLCV)。失败 → []。
    """
    # 优先: zt_history 缓存(全市场K线已落盘)
    _cache = _get_zt_cache()
    if _cache:
        rec = _cache.get(str(code).strip().upper())
        if rec and rec.get("dates"):
            # 新缓存若带 volume 走全量契约; 老缓存只有 close 走旧契约
            if rec.get("volume"):
                return list(zip(rec["dates"],
                                rec.get("open") or rec["close"],
                                rec.get("high") or rec["close"],
                                rec.get("low") or rec["close"],
                                rec["close"], rec["volume"]))
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
        # 全量 OHLCV: 缺列时以 close 兜底, 保证 6 元组结构稳定
        cols = {}
        for k in ("open", "high", "low", "volume"):
            cols[k] = (df[k].tolist() if k in df.columns
                       else list(closes))
        return list(zip(dates, cols["open"], cols["high"], cols["low"],
                        closes, cols["volume"]))
    except Exception:
        return []


def kline_feed(code):
    """K线数据源(优先 QMT 本地, 网络源回退)。

    返回契约: 网络源/QMT 实时源 → [(date, open, high, low, close, volume), ...]
    全量 OHLCV; QMT 老缓存(只存 close) → 旧 2 元组, 由 backtest 占位。
    升序。

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
    """腾讯日K线 → [(date, open, high, low, close, volume), ...] 升序。
    全量 OHLCV 契约(带真实成交量), 量能/形态因子在回测中可生效。失败 → []。
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
        # 腾讯格式: [date, open, close, high, low, volume, ...]
        if isinstance(line, list) and len(line) >= 6:
            try:
                out.append((str(line[0]), float(line[1]), float(line[3]),
                            float(line[4]), float(line[2]), float(line[5])))
            except (TypeError, ValueError):
                continue
    return out


def _kline_from(url, code, extra=None):
    """东财K线端点 → [(date, open, high, low, close, volume), ...]。失败 → []。

    fields2 取全量 OHLCV: f51日期 f52开 f53收 f54高 f55低 f56量(手)。
    此前只请求 f51,f53(日期+收盘), 回测量能因子因此全失效(volume 恒 1.0)。
    """
    params = {"secid": _secid(code), "klt": 101, "fqt": 1,
              "fields1": "f1,f2,f3",
              "fields2": "f51,f52,f53,f54,f55,f56"}
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
        if len(parts) >= 6:
            try:
                # 东财顺序: date, open, close, high, low, volume
                out.append((parts[0], float(parts[1]), float(parts[3]),
                            float(parts[4]), float(parts[2]), float(parts[5])))
            except (TypeError, ValueError):
                continue
    return out

# ---------------------------------------------------------------- main

def _parse_date(s):
    return datetime.strptime(s, "%Y%m%d").date()


def load_market_data():
    """市场数据缓存装配(网页回测与 CLI 共用, 2026-09-04 修复):

    N6-N8/F8/F9/SEC1-4/SEC6 等 10 个因子依赖 mkt/sector_map; 不注入则恒 0
    ——网页回测端点曾漏注入, 导致三个策略全部"静默零交易"(v04 候选全 0 分
    被 candidate_min_model 过滤; full_factor_v1 门控 N6-N8 恒 0 永不达标)。

    返回 (mkt, sector_map, note): 缓存为空时 mkt/sector_map 为 None, note 说明原因。
    """
    from prism import market_data as _md
    cache = _md._load_cache()
    if not cache:
        return None, None, (
            "市场数据缓存为空 → N6-N8/F8/F9/SEC 因子失效, 回测结果不可用于"
            "评估这些因子(先采集: python -m prism.market_data "
            "--build-sectors / --build-global)")
    mkt = {"sector": cache.get("kline") or {},
           "global": cache.get("global") or {},
           "sector_flow": cache.get("flow") or {},
           "futures": _md.futures_snapshot()}
    smap = cache.get("sector_map") or {}
    sector_map = {}
    for c6, rec in smap.items():
        if rec and rec.get("sector"):
            suffix = ".SH" if c6.startswith("6") else ".SZ"
            sector_map[c6 + suffix] = rec["sector"]
    return mkt, sector_map, None


def main():
    ap = argparse.ArgumentParser(description="Prism 回测 CLI(QMT本地数据, 可回测约1.5年)")
    ap.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    ap.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    ap.add_argument("--strategy", default="default",
                    help="策略 id(prism/strategies/, 默认 default)")
    ap.add_argument("--sell-tp", type=float, default=None,
                    help="止盈百分比(覆盖策略配置, 如 0.15)")
    ap.add_argument("--sell-sl", type=float, default=None,
                    help="止损百分比(覆盖策略配置, 如 0.08)")
    ap.add_argument("--hold", type=int, default=None, help="持有天数(覆盖策略配置)")
    ap.add_argument("--oos", action="store_true",
                    help="运行样本外验证(前后半段对比, 防过拟合)")
    ap.add_argument("--no-market-data", dest="use_market_data",
                    action="store_false",
                    help="不注入市场数据缓存(mkt/sector_map)。默认注入 —— "
                         "否则 N6-N8/F8/F9/SEC1-4/SEC6 共 10 个因子静默失效, "
                         "回测结果不能用来评估它们")
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
        mkt, sector_map, md_note = load_market_data()
        if md_note:
            print("警告: %s" % md_note, file=sys.stderr)
        else:
            print("市场数据注入: 板块 %d, 全球指数 %d, 个股映射 %d" % (
                len(mkt["sector"]), len(mkt["global"]), len(sector_map)),
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
