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

from shared.common import limit_ratio_for_code, with_market_suffix
from prism import bt_intraday
from prism.backtest import Backtester, _parse_kline_date
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

def _df_to_kline6(df):
    """xtdata 日线 DataFrame → [(iso_date, open, high, low, close, volume), ...]。

    日期归一到 ISO(time 毫秒列 / YYYYMMDD 索引键两来源都兼容); 缺列以 close
    兜底(结构稳定, 量能列真缺失时不臆造); 升序; 无数据 → []。
    """
    if df is None or len(df) == 0:
        return []
    closes = df["close"].tolist()
    # 日期: 新版毫秒 epoch / 旧版 YYYYMMDD 索引
    if "time" in df.columns:
        raw_dates = [datetime.fromtimestamp(int(t) / 1000.0)
                     .strftime("%Y-%m-%d") for t in df["time"]]
    else:
        raw_dates = [str(t) for t in df.index]
    cols = {}
    for k in ("open", "high", "low", "volume"):
        cols[k] = (df[k].tolist() if k in df.columns else list(closes))
    out = []
    for raw, o, h, l, c, v in zip(raw_dates, cols["open"], cols["high"],
                                  cols["low"], closes, cols["volume"]):
        dt = _iso_day(raw)
        if dt is None:
            continue
        out.append((dt, float(o), float(h), float(l), float(c), float(v)))
    out.sort(key=lambda r: r[0])
    return out


def _qmt_daily(code, count=400):
    """QMT 本地日线 → 6 元组列表(全量 OHLCV, 真实成交量)。

    优先读本地(get_local_data, 实测单只 ~2ms); 本地没有该股数据时补一次
    download_history_data 再读。失败/无数据 → []。
    """
    try:
        from xtquant import xtdata
    except Exception:
        return []
    for attempt in (0, 1):
        try:
            if attempt:
                xtdata.download_history_data(code, "1d")
            data = xtdata.get_local_data(field_list=[], stock_list=[code],
                                         period="1d", start_time="",
                                         end_time="", count=count)
            df = (data or {}).get(code)
        except Exception:
            return []
        out = _df_to_kline6(df)
        if out:
            return out
    return []


def qmt_kline_feed(code):
    """QMT 日线 → [(date, open, high, low, close, volume), ...] 全量 OHLCV 契约。

    两档来源(都只读 QMT 本地, 不触网):
      1. get_local_data 本地日线 —— 含真实 open/high/low/close/**volume**
         (F5/Y3/S2/S3 量能因子与 M6/M7/S2 形态因子的数据源);
      2. zt_history 缓存兜底(全市场K线已落盘, QMT 停机时可用): 老缓存只存了
         close → 退回 **2 元组旧契约**。拿不到的量**不造假**: volume 由
         backtest 占位 1.0, 量能因子此时静默失效(不能用来评估它们)。
    失败 → []。
    """
    s = with_market_suffix(code)
    out = _qmt_daily(s)
    if out:
        return out
    _cache = _get_zt_cache()
    rec = (_cache or {}).get(s)
    if rec and rec.get("dates"):
        return list(zip(rec["dates"], rec["close"]))
    return []


def kline_feed(code):
    """K线数据源(优先 QMT 本地, 网络源回退)。

    返回契约: 网络源/QMT 本地源 → [(date, open, high, low, close, volume), ...]
    全量 OHLCV; QMT 停机且只有 zt 老缓存(只存 close) → 旧 2 元组, 由 backtest
    占位(量能因子失效属预期, 不造假成交量)。升序。

    数据源链(逐级回退, 全部失败 → []):
      1. QMT xtdata 本地K线(需 miniQMT 已连接 / 本地日线已下载, 最快最稳)
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

# ---------------------------------------------------------------- 按日上下文

# 指数: 上证(000001.SH) 供 F6 大盘配合 + 两市成交额的一半; 深证成指(399001.SZ)
# 另一半。两市成交额 = 两者 amount 之和(实测 8711亿 + 9680亿, 量级正确)。
_INDEX_CODES = ("000001.SH", "399001.SZ")
# 指数序列最多带多少根: F6 要 21 根算 MA20 → 30 根留余量, 不搬全历史。
_SH_INDEX_BARS = 30
# 区间前多取几天: "昨日池"(N3/N4)与近 5 日涨停家数(N1)要往前看。
_FEED_PAD_DAYS = 20


def _iso_day(raw):
    """日期串(YYYY-MM-DD / YYYYMMDD) → ISO 字符串; 非法 → None。"""
    d = _parse_kline_date(raw)
    return d.strftime("%Y-%m-%d") if d else None


def _calendar_days(extra=()):
    """交易日候选(ISO 字符串, 升序): zt 索引日期 ∪ 传入日期(指数日线)。

    交易日历必须来自真实数据: 用自然日回退一天会把周一误当"上一交易日"
    (周日无池 → yesterday_codes 空 → N3/N4 假阴性)。
    """
    days = set()
    for key in (_get_zt_index() or {}):
        iso = _iso_day(key)
        if iso:
            days.add(iso)
    for raw in extra or ():
        iso = _iso_day(raw)
        if iso:
            days.add(iso)
    return sorted(days)


def _df_to_index_rows(df):
    """指数日线 DataFrame → [(iso_date, close, volume, amount)] 升序。

    日期两来源都**归一成 ISO**: 'time' 毫秒列(取全字段时才有) / 索引键
    (get_local_data 收窄 field_list 后不带 time 列, 索引是 "YYYYMMDD")。
    必须归一 —— 逐日切片是字符串比较, "20260904" > "2026-09-04" 会被
    _upto 整段滤掉, sh_index_kline 与两市成交额双双静默为空(F6/N5 恒 0;
    2026-09-16 本地实跑抓到过一次)。无数据/脏行 → 空/丢弃。
    """
    if df is None or len(df) == 0:
        return []
    if "time" in df.columns:
        raw_dates = [datetime.fromtimestamp(int(t) / 1000.0)
                     .strftime("%Y-%m-%d") for t in df["time"]]
    else:
        raw_dates = [str(t) for t in df.index]
    closes = df["close"].tolist()
    vols = (df["volume"].tolist() if "volume" in df.columns
            else [None] * len(closes))
    amts = (df["amount"].tolist() if "amount" in df.columns
            else [None] * len(closes))
    out = []
    for raw, c, v, a in zip(raw_dates, closes, vols, amts):
        dt = _iso_day(raw)
        if dt is None:
            continue
        try:
            out.append((dt, float(c),
                        float(v) if v is not None else None,
                        float(a) if a is not None else None))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda r: r[0])
    return out


def _index_daily(code, start, end):
    """指数日线 → [(iso_date, close, volume, amount)] 升序; 失败/无数据 → []。

    amount 供 N5 两市成交额; close/volume 供 F6(MA20 + 连两日放量)。
    """
    try:
        from xtquant import xtdata
    except Exception:
        return []
    try:
        xtdata.download_history_data(code, "1d", start, end)
        data = xtdata.get_local_data(
            field_list=["close", "volume", "amount"], stock_list=[code],
            period="1d", start_time=start, end_time=end, count=-1)
        df = (data or {}).get(code)
    except Exception:
        return []
    return _df_to_index_rows(df)


def _iso_kline(rows):
    """kline_feed 元组列表 → [(iso_date, o, h, l, c, volume|None)] 升序。

    契约三档按长度识别(与 prism.backtest._stock_ctx 同一套规则); 日期归一到
    ISO(逐日切片靠字符串比较)。脏行丢弃。
    """
    out = []
    for row in rows or []:
        dt = _iso_day(row[0] if row else None)
        if dt is None:
            continue
        try:
            if len(row) >= 6:
                o, h, l, c = (float(row[1]), float(row[2]), float(row[3]),
                              float(row[4]))
                v = float(row[5])
            else:
                c = float(row[1])
                o = h = l = c
                v = None            # 旧契约没有成交量 → None(不占位 1.0)
        except (TypeError, ValueError, IndexError):
            continue
        out.append((dt, o, h, l, c, v))
    out.sort(key=lambda r: r[0])
    return out


def _upto(rows, d8):
    """升序日期序列 → 只留 date <= d8 的前缀(防未来函数)。无 → []。"""
    return [r for r in (rows or []) if r and r[0] <= d8]


def _batch_klines(codes):
    """池内股票日K {code: [(iso,o,h,l,c,v)...]}。

    逐只读 QMT 本地日线(实测 ~1ms/只; 本地没有才补一次单只 download)。
    **不做批量 download_history_data2**: 实测空窗口批量下载 1000+ 只要 100s+
    (等于全历史重拉), 收紧窗口也要 66s —— 本地日线本来就在(zt 缓存与
    守护刷新都已落盘), 白等。失败/无数据的股票不进结果(缺即缺, 不造假)。
    """
    out = {}
    for code in codes:
        # QMT 只认带后缀代码; 键保持池条目原样(东财兜底池给的是裸 6 位码),
        # 否则 _day_payload 按池 code 取 klines 会全空(F1 又变恒 0)。
        qmt_code = with_market_suffix(code)
        rows = _iso_kline(_qmt_daily(qmt_code))
        # 本地没有 → 退回 zt 缓存(可能只有 close) → 至少 last/last_close 可用
        if not rows:
            rec = (_get_zt_cache() or {}).get(qmt_code)
            if rec and rec.get("dates"):
                rows = _iso_kline(list(zip(rec["dates"], rec["close"])))
        if rows:
            out[code] = rows
    return out


def _float_volumes(codes):
    """QMT 流通股本 {code: 股}(缺/失败 → 不放键)。口径同实盘 data.py。"""
    try:
        from xtquant import xtdata
    except Exception:
        return {}
    out = {}
    for code in codes:
        try:
            det = xtdata.get_instrument_detail(code) or {}
            fv = det.get("FloatVolume")
            if fv:
                out[code] = float(fv)
        except Exception:
            continue
    return out


def _day_payload(d, pools, cal, klines, floats, indices):
    """单日市场/个股上下文(纯函数, 无 IO) → 规格 §4 的 day_feed 载荷。

    d: 决策日(date); pools: {date: 涨停池 [{code, boards}]}(含区间前几日,
    用来算"昨日池"); cal: 交易日(date, 升序); klines: {code: [(iso,...)...]};
    floats: {code: 流通股本}; indices: {指数代码: [(iso, close, vol, amount)]}。
    只读 <= d 的数据(指数/个股序列都按日切片), 无未来函数。
    ticks 的个股条 = 今日池 ∪ 昨日池(只 lastPrice/lastClose, 供 N3 首板溢价);
    stock 只装今日池(供 F1 首板确认: 必须今日涨停)。
    """
    d8 = d.strftime("%Y-%m-%d")
    pool = pools.get(d) or []
    pos = cal.index(d) if d in cal else None
    prev_pool = (pools.get(cal[pos - 1]) or []) if pos else []
    # 逐日涨停家数(池子合成, 与 N1 的 daily_counts 同源): 含当日, 取近 11 天
    hist = cal[max(0, pos + 1 - 11):pos + 1] if pos is not None else [d]
    counts = [(x.strftime("%Y-%m-%d"), len(pools.get(x) or [])) for x in hist]
    prev_counts = [c for _x, c in counts[:-1]][-5:][::-1]   # 近→远, 与东财同序

    # ---- 个股: 当日收盘 / 昨收 / 涨停价 / 流通股本(只回看 <= d 的K线) ----
    # 遍历**今日池 ∪ 昨日池**: 今日池供 F1(stock)/N3; 昨日池独有股只进 ticks ——
    # N3(首板溢价 = 昨日涨停股今日表现)必须拿到"昨涨停、今未涨停"的样本, 否则
    # 只剩"昨涨停且今仍涨停"的连板样本, 均涨幅恒正 → N3 近乎恒 1(门控形同虚设)。
    today_codes = {s.get("code") for s in pool}
    stock = {}
    quotes = {}          # code → (今收, 昨收): ticks 的 N3 样本
    for s in list(pool) + list(prev_pool):
        code = s.get("code")
        if not code or code in quotes:
            continue
        rows = _upto(klines.get(code), d8)
        if len(rows) < 2:
            continue        # 无昨收 → 算不出涨停价, 宁缺勿假
        close, prev_close = rows[-1][4], rows[-2][4]
        if not close or not prev_close or close <= 0 or prev_close <= 0:
            continue
        if code not in today_codes and rows[-1][0] != d8:
            # 昨池独有股今日无行情(停牌/缺数据) → 不拿旧K线充"今日溢价"
            # (与实盘 ticks 同口径: 没有今日报价的股票不进 N3 样本)
            continue
        quotes[code] = (close, prev_close)
        if code in today_codes:
            item = {"last": close, "last_close": prev_close,
                    # 涨停价 = 昨收 ×(1+档位), 四舍五入两位(同 zt_history 口径)
                    "up_price": round(prev_close
                                      * (1 + limit_ratio_for_code(code)), 2)}
            fv = (floats or {}).get(code)
            if fv:
                item["float_vol"] = float(fv)
                # 同实盘口径: 股本 × 现价。缺股本 → 显式 None(不造假, 与实盘
                # data.py "两者都为正才算否则 None" 同口径); Y1/Y8 拿到 None
                # → 因子缺键 → fail-open 0(Task 3 池条目 float_mv 契约)。
                item["float_mv"] = float(fv) * close
            else:
                item["float_mv"] = None
            stock[code] = item

    # ---- 门控 em: 今日最高连板 + 昨日池(N3/N4 口径同东财 get_market_stats) ----
    em = {"max_boards": max([int(s.get("boards") or 0) for s in pool],
                            default=0),
          "yesterday_codes": [s.get("code") for s in prev_pool
                              if s.get("code")],
          "yesterday_boards": [s.get("code") for s in prev_pool
                               if s.get("code")
                               and int(s.get("boards") or 0) >= 2]}
    if len(prev_counts) >= 3:      # 不足 3 天视为不可用(与东财 stats 同口径)
        em["daily_counts"] = prev_counts

    # ---- ticks: 指数条只带 amount(N5 求和), 个股只带 lastPrice/lastClose(N3)
    #      —— 个股**不带 amount**: 个股成交额已含在两市成交额里, 再叠加会重复计算。
    ticks = {}
    for code in _INDEX_CODES:
        rows = _upto((indices or {}).get(code), d8)
        if rows and rows[-1][3]:
            ticks[code] = {"amount": rows[-1][3]}
    for code, (close, prev_close) in quotes.items():
        ticks[code] = {"lastPrice": close, "lastClose": prev_close}

    # ---- 指数序列: 上证 30 根(≤d) 供 F6; 涨停家数序列供 N1 兜底 ----
    sh = _upto((indices or {}).get("000001.SH"), d8)[-_SH_INDEX_BARS:]
    return {"em": em, "ticks": ticks,
            "index_kline": [(x, c) for x, c in counts],
            "sh_index_kline": [(x, c, v) for x, c, v, _a in sh],
            "stock": stock}


def _apply_intraday(payload, iso, stats):
    """把 1m 特征缓存里的封板信息合进 payload["stock"](规格 §5, Task 2)。

    **只读已落盘缓存, 绝不下载** —— 下载只发生在显式采集命令
    (python -m backtest.cli --build-intraday); 缓存缺失的日期静默降级
    (stock 不带 sealed/tick/bt_seal_ratio → F2/F3 fail-open 0)。
    合成的字段(与实盘同型):
      sealed        = 特征 sealed_close(收盘是否仍封)
      tick          = {timetag: 首封时刻毫秒(与实盘同解析路径), lastPrice=收盘,
                       lastClose=昨收, amount=板上成交额}
      bt_seal_ratio = on_board_amt / float_mv(float_mv 缺 → 不给, F3 走实盘口径)
      one_word      = 一字板(规格 §6 成交约束: 买入侧判定"买不到")
    stats: {"asked": n, "got": n} 累计覆盖计数(供 data_notes 汇总)。
    """
    stock = payload.get("stock") or {}
    for code, item in stock.items():
        feat = bt_intraday.features_for(code, iso)
        stats["asked"] += 1
        if not feat:
            continue            # 缓存缺失 → 静默降级(该股该日 F2/F3 得 0)
        stats["got"] += 1
        item["sealed"] = bool(feat.get("sealed_close"))
        # 一字板: 缓存没采到该日的日期**不设键** —— 未知 ≠ False(不造假),
        # Backtester.run 按"未知"处理(不拦买入, 但计入 filter_stats)。
        item["one_word"] = bool(feat.get("one_word"))
        on_board_amt = feat.get("on_board_amt")
        float_mv = item.get("float_mv")
        if float_mv:
            item["bt_seal_ratio"] = float(on_board_amt or 0.0) / float_mv
        tick = {"lastPrice": item.get("last"),
                "lastClose": item.get("last_close"),
                "amount": float(on_board_amt or 0.0)}
        timetag = bt_intraday.seal_timetag(iso, feat.get("first_seal_hm"))
        if timetag is not None:
            tick["timetag"] = timetag
        item["tick"] = tick
    return payload


# 基本面因子的两类口径(I1 审查修复): 网络类只在"该日已采集"才有值(按天计覆盖);
# Y1/Y8 由当日流通市值纯计算, 与缓存覆盖无关 —— 两类必须分开统计, 否则
# "一天都没采过"的报告会写成满覆盖。
_FUND_NET_KEYS = frozenset(("F7", "Y6", "Y7"))
_FUND_CALC_KEYS = frozenset(("Y1", "Y8"))


def _apply_fund(payload, day, fund_feed, stats):
    """把基本面因子快照合进 payload["stock"](规格 §7, Task 3)。

    stock[code]["fund"] 来自 compute_for_stock(code, float_mv=当日流通市值,
    asof=决策日) —— asof 严格(防未来: F7 涨停池/Y6 公告/Y7 龙虎榜窗口不越
    当日; feed 缓存键 code:YYYYMMDD 天然分日, 同 (code, 日) 只算一次)。
    Y5(概念)/Y2(股东户数)是"当前快照"类接口, 对回测自带未来性 → 注入侧与
    Backtester._fund_for 同口径剔除(保持现状, 宁缺勿假)。
    网络失败 fail-open 不阻塞: feed 整体异常 → 该股不给 fund(F7/Y6/Y7 等得 0),
    单日其余个股照常。fund 结果为空 dict → 同样不给(与 _pick 的
    stock.get("fund") or _fund_for 短路语义一致)。
    stats: 覆盖计数(供 data_notes 汇总), 两类**分开**记(I1 审查修复):
      asked/days  —— 问过的股票日 / 问过的天数(分母)
      net_got/net_days —— 拿到 F7/Y6/Y7(网络类)的股票日 / 天数
      calc_got     —— 拿到 Y1/Y8(纯计算)的股票日
    网络类只有"该日已采集(缓存命中)"才有值, 而 Y1/Y8 由当日 float_mv 纯计算、
    与缓存覆盖无关 —— 旧口径把"只有 Y1/Y8"也算成已覆盖, 于是"一天都没采过"
    的报告会写成满覆盖, 并把 0 误归因于网络失败。
    """
    stock = payload.get("stock") or {}
    stats["days"] = stats.get("days", 0) + 1     # 本日已问(分母: 天)
    net_hit_today = False
    for code, item in stock.items():
        stats["asked"] += 1
        try:
            fund = dict(fund_feed.compute_for_stock(
                code, float_mv=item.get("float_mv"), asof=day) or {})
        except Exception as exc:
            print("警告: 基本面快照 %s@%s 计算失败(fail-open 0): %r"
                  % (code, day, exc), file=sys.stderr)
            continue
        for k in ("Y5", "Y2"):   # 快照类: 回测剔除(防未来), 保持现状
            fund.pop(k, None)
        if not fund:
            continue
        if _FUND_NET_KEYS & set(fund):
            stats["net_got"] = stats.get("net_got", 0) + 1
            net_hit_today = True
        if _FUND_CALC_KEYS & set(fund):
            stats["calc_got"] = stats.get("calc_got", 0) + 1
        item["fund"] = fund
    if net_hit_today:
        stats["net_days"] = stats.get("net_days", 0) + 1
    return payload


def _refresh_fund_note(notes, stats):
    """基本面覆盖说明(单条, 随回放滚动自我覆盖) → run() 收进报告 data_notes。

    I1(审查修复): 覆盖**按类拆开** —— F7/Y6/Y7 是网络类, 只有"该日已采集"
    (缓存命中)才有值, 故按**天**计; Y1/Y8 由当日流通市值纯计算, 与缓存覆盖
    无关, 单独说明。旧口径把"只有 Y1/Y8"的股票日也算成已覆盖 → "一天都没
    采过"的报告写"个股日覆盖 181/181", 且把 0 归因于"网络失败/缺 float_mv",
    真因其实是**该日未采集**。
    回测默认**只读缓存不联网**(2026-09-17 用户拍板): 东财单股取数实测 40s+
    (Y6 公告端点 35.5s), 全窗口 254 日 ≈100 小时不可行 → 未缓存的日子直接
    fail-open 0, 历史用 fund_snapshot 逐日回填。
    """
    mode = ("本次已联网取数(慢)" if stats.get("fetch")
            else "回测默认只读基本面缓存不联网(联网取数需显式 --fetch-fund)")
    note = ("基本面: F7/Y6/Y7 覆盖 %d/%d 天(网络类只在\"已采集日\"有值 —— "
            "未采集的日子为 0; %s; 补历史: "
            "python -m prism.fund_snapshot --date YYYYMMDD); "
            "Y1/Y8 由当日流通市值纯计算, 不受缓存覆盖影响(个股日 %d/%d); "
            "快照类 Y5/Y2 回测剔除防未来"
            % (stats.get("net_days", 0), stats.get("days", 0), mode,
               stats.get("calc_got", 0), stats["asked"]))
    for i, x in enumerate(notes):
        if x.startswith("基本面: "):
            notes[i] = note
            return
    notes.append(note)


def _refresh_intra_note(notes, stats):
    """覆盖说明(单条, 随回放滚动更新) → run() 收进报告 data_notes。"""
    note = ("1m特征: 个股日覆盖 %d/%d (缓存缺失静默降级 → F2/F3 fail-open 0; "
            "早于 2025-09-15 无 1m 数据。补采集: "
            "python -m backtest.cli --build-intraday)"
            % (stats["got"], stats["asked"]))
    if notes:
        notes[0] = note
    else:
        notes.append(note)


def build_day_feed(start, end, *, use_intraday=False, progress=None,
                   fund_feed=None):
    """装配回测的按日上下文 → day_feed(d) -> dict|None(规格 2026-09-16 §4)。

    **惰性构造**: 返回的 callable 首次被问到某日 d 时才取该日(及"昨日池"/近 5 日
    家数的回看窗口)的涨停池与日线, 之后命中缓存 —— 大窗口不再预先跑满全程
    (25 天预计算 8.5s → 254 天要 90s+, 网页请求会长时间阻塞), 也不预占内存。

    每字段都只含 <= 当日的本地数据(QMT 日线 + zt 涨停池历史), 防未来函数由
    _day_payload 的逐日切片保证。区间外的日子返回 None → run() 退回静态参数。

    use_intraday: 1 分钟特征层(规格 §5, Task 2): True 时每日把**已缓存**的
        1m 特征合进 stock[code](sealed/tick/bt_seal_ratio, 复活 F2/F3 代理);
        只读缓存绝不下载 —— 缺失日期静默降级, 覆盖情况进报告 data_notes
        (补采集: python -m backtest.cli --build-intraday)。
    fund_feed(Task 3, 规格 §7): FundamentalFeed 实例(测试可注入假实现)。
        传入时每日把基本面因子快照合进 stock[code]["fund"](Y1/Y8 纯计算 +
        F7/Y6/Y7 asof 窗口; Y5/Y2 快照类剔除防未来; 网络失败 fail-open 0,
        覆盖情况进报告 data_notes)。**回测默认传 offline=True 的 feed: 只读
        缓存不联网**(2026-09-17 拍板, 见 `_fund_feed`); offline feed 未命中的
        日子不给 fund → 因子得 0, 且绝不写缓存。None(缺省) → 不注入, 行为与
        旧版一致(零网络零注入; 仅池条目 float_mv 由"键缺省"改为显式 None ——
        消费方 .get() 语义不变)。
    progress(done, total): 已构造的区间交易日数 / 区间交易日总数(可选)。
    QMT 离线且无 zt 索引(无交易日历) → 返回恒 None 的 callable(等于不注入,
    静态参数照常生效), 并打印一行原因(不静默)。
    """
    lo = (start - timedelta(days=_FEED_PAD_DAYS)).strftime("%Y%m%d")
    hi = end.strftime("%Y%m%d")
    indices = {c: _index_daily(c, lo, hi) for c in _INDEX_CODES}
    lo_iso = (start - timedelta(days=_FEED_PAD_DAYS)).strftime("%Y-%m-%d")
    hi_iso = end.strftime("%Y-%m-%d")
    cal = [x for x in _calendar_days(
        [r[0] for rows in indices.values() for r in rows])
        if lo_iso <= x <= hi_iso]
    if not cal:
        print("警告: 按日上下文无法装配(无涨停池索引也无指数日线) → "
              "N3/N4/N5/F1/F6 仍按静态参数空转", file=sys.stderr)
        return lambda d: None
    cal_dates = [_parse_kline_date(x) for x in cal]
    pos_of = {x: i for i, x in enumerate(cal)}
    start_iso = start.strftime("%Y-%m-%d")
    replay_total = sum(1 for x in cal if x >= start_iso)
    # 惰性缓存的全部状态: 池/日线/股本都按需取, 取过就记(空池、无数据也不重取)
    cache = {"pools": {}, "klines": {}, "floats": {}, "ctx": {},
             "tried_k": set(), "tried_f": set(), "done": 0,
             "intra": {"asked": 0, "got": 0},
             "fund": {"asked": 0, "days": 0, "net_days": 0, "net_got": 0,
                      "calc_got": 0,
                      "fetch": not getattr(fund_feed, "offline", False)}}
    # use_intraday 的覆盖说明(单条) → run() 收进报告 data_notes(规格 §5)
    data_notes = []

    def _pools_at(i):
        """cal[i] 当日的涨停池(按需取 + 缓存)。"""
        iso = cal[i]
        if iso not in cache["pools"]:
            try:
                cache["pools"][iso] = zt_feed(iso.replace("-", "")) or []
            except Exception:
                cache["pools"][iso] = []
        return cache["pools"][iso]

    def _need_klines(codes):
        todo = [c for c in codes if c and c not in cache["tried_k"]]
        if todo:
            cache["klines"].update(_batch_klines(todo))
            cache["tried_k"].update(todo)   # 取过(含"确实没数据")才记账
        return cache["klines"]

    def _need_floats(codes):
        todo = [c for c in codes if c and c not in cache["tried_f"]]
        if todo:
            cache["floats"].update(_float_volumes(todo))
            cache["tried_f"].update(todo)
        return cache["floats"]

    def feed(d):
        """按需装配 d 当日的上下文; 区间外/非交易日 → None(退回静态参数)。

        单日取数失败也不炸整段回测: 打印原因 + 该日返回 None(退回静态参数),
        失败不入缓存(下次请求可重试)。
        """
        iso = d.strftime("%Y-%m-%d")
        i = pos_of.get(iso)
        if i is None or iso < start_iso:
            return None
        if iso not in cache["ctx"]:
            try:
                # 该日 + 回看窗口(近 11 天家数 / 昨日池): 区间前的日子只作上下文
                win = list(range(max(0, i - 10), i + 1))
                day_pools = {cal_dates[j]: _pools_at(j) for j in win}
                pool = day_pools[cal_dates[i]]
                prev = (day_pools.get(cal_dates[i - 1]) or []) if i else []
                cache["ctx"][iso] = _day_payload(
                    cal_dates[i], day_pools, cal_dates,
                    _need_klines([s.get("code")
                                  for s in list(pool) + list(prev)]),
                    _need_floats([s.get("code") for s in pool]), indices)
                if use_intraday:
                    # 1m 特征只读缓存(绝不下载), 缺失静默降级(规格 §5)
                    _apply_intraday(cache["ctx"][iso], iso, cache["intra"])
                    _refresh_intra_note(data_notes, cache["intra"])
                if fund_feed is not None:
                    # 基本面快照(规格 §7, Task 3): 在 1m 注入之后刷新覆盖说明,
                    # _refresh_fund_note 自我覆盖自己的槽位(互不挤占)
                    _apply_fund(cache["ctx"][iso], cal_dates[i], fund_feed,
                                cache["fund"])
                    _refresh_fund_note(data_notes, cache["fund"])
            except Exception as exc:
                print("警告: 按日上下文装配失败 %s: %r → 该日退回静态参数"
                      % (iso, exc), file=sys.stderr)
                return None
            cache["done"] += 1
            if progress:
                progress(cache["done"], replay_total)
        return cache["ctx"][iso]

    n_idx = sum(1 for c in _INDEX_CODES if indices.get(c))
    print("按日上下文: 惰性构造 %d 个交易日(首次请求某日才取该日池/日线, 之后命中"
          "缓存) / 指数 %d/2 / 1m特征%s" % (
              replay_total, n_idx,
              "启用(只读已缓存特征, 缺失日期降级; 采集: python -m backtest.cli "
              "--build-intraday)" if use_intraday else "未启用"), file=sys.stderr)
    feed.data_notes = data_notes       # run() 收进报告 data_notes
    return feed


def _pool_codes_by_day(start, end):
    """[start, end] 逐日涨停池 → {ISO 日期: [codes]}(1m 特征采集的输入)。

    交易日历与 build_day_feed 同源(zt 索引 ∪ 指数日线, 真实交易日);
    只取区间内的日子(区间前只作上下文, 不用采特征)。
    """
    lo = (start - timedelta(days=_FEED_PAD_DAYS)).strftime("%Y%m%d")
    hi = end.strftime("%Y%m%d")
    indices = {c: _index_daily(c, lo, hi) for c in _INDEX_CODES}
    cal = [x for x in _calendar_days(
        [r[0] for rows in indices.values() for r in rows])
        if start.strftime("%Y-%m-%d") <= x <= end.strftime("%Y-%m-%d")]
    out = {}
    for iso in cal:
        try:
            pool = zt_feed(iso.replace("-", "")) or []
        except Exception:
            pool = []
        codes = [s.get("code") for s in pool if s.get("code")]
        if codes:
            out[iso] = codes
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


def _fund_feed(fetch=False):
    """真实基本面 feed(Task 3, 规格 §7): 与实盘 prism.data 同一实现与同一份缓存。

    **默认 offline=True: 回测只读缓存、绝不联网**(2026-09-17 用户拍板)。
    东财单股取数实测 40s+(`np-anotice-stock` 公告端点 35.5s, 标量 timeout 只管
    connect+每次 socket read, 不覆盖 DNS 解析也不限总时长 → 服务端慢速分片返回
    可远超 5s), 全窗口 254 日 ≈100 小时不可行 → 回测只消费已采集缓存, 未缓存的
    日子 fail-open 0; 历史用 `python -m prism.fund_snapshot --date YYYYMMDD` 逐日
    回填。`fetch=True`(--fetch-fund, 显式选择)才联网取数。

    导入/构造失败 → None(不注入, Y1/Y8/F7/Y6/Y7 恒 0, 不阻塞回测, 打一行原因
    不静默)。
    """
    try:
        from datasource.fundamental import FundamentalFeed
        return FundamentalFeed(offline=not fetch)
    except Exception as exc:
        print("警告: 基本面 feed 不可用(%r) → Y1/Y8/F7/Y6/Y7 因子按 0 处理"
              % exc, file=sys.stderr)
        return None


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
                    help="不注入市场数据缓存(mkt/sector_map)与按日上下文"
                         "(day_feed)。默认注入 —— 否则 N6-N8/F8/F9/SEC1-4/SEC6 "
                         "共 10 个因子静默失效, 且 N3/N4/N5/F1/F6 拿不到逐日"
                         "快照(回测结果不能用来评估它们)")
    ap.add_argument("--build-intraday", dest="build_intraday",
                    action="store_true",
                    help="显式采集 1 分钟特征(逐日涨停池 → QMT 批量下载 → "
                         "特征落盘 runtime/cache/bt_intraday)。**只有本命令"
                         "会触发下载**; 普通回测/网页请求只读已落盘缓存")
    ap.add_argument("--fetch-fund", dest="fetch_fund", action="store_true",
                    help="基本面**联网取数**(默认关: 只读 runtime/cache/"
                         "fundamental_cache.json, 未缓存的日子 Y1/Y8/F7/Y6/Y7 "
                         "按 0)。东财单股实测 40s+(公告端点 35.5s), 长窗口会"
                         "慢到不可用; 历史请用 python -m prism.fund_snapshot "
                         "--date YYYYMMDD 逐日回填后再跑回测")
    args = ap.parse_args()

    start = _parse_date(args.start)
    end = _parse_date(args.end)
    if args.build_intraday:
        codes_by_day = _pool_codes_by_day(start, end)
        n_days = len(codes_by_day)
        print("1m 特征采集: %d 个交易日 / %d 只(股,日)... "
              % (n_days, sum(len(v) for v in codes_by_day.values())),
              file=sys.stderr)

        def _dl_progress(done, total):
            if total and done % 100 == 0:
                print("  1m 特征 %d/%d (股,日)" % (done, total),
                      file=sys.stderr)

        res = bt_intraday.download_features(codes_by_day,
                                            progress=_dl_progress)
        print(json.dumps({"days": n_days, **res}, ensure_ascii=False))
        return 0
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
    day_feed = None
    if args.use_market_data:
        mkt, sector_map, md_note = load_market_data()
        if md_note:
            print("警告: %s" % md_note, file=sys.stderr)
        else:
            print("市场数据注入: 板块 %d, 全球指数 %d, 个股映射 %d" % (
                len(mkt["sector"]), len(mkt["global"]), len(sector_map)),
                file=sys.stderr)
        # 按日上下文(N3/N4/N5/F1/F6 需要逐日快照; 静态参数只有一份)
        # use_intraday=True: 1m 特征只读已落盘缓存(F2/F3), 缺失静默降级
        # fund_feed(Task 3): 基本面快照注入(Y1/Y8/F7/Y6/Y7); 默认只读缓存不联网,
        # --fetch-fund 才联网(2026-09-17 拍板, 见 _fund_feed docstring)
        def _feed_progress(done, total):
            if done % 50 == 0:
                print("  装配按日上下文 %d/%d" % (done, total), file=sys.stderr)

        day_feed = build_day_feed(start, end, use_intraday=True,
                                  progress=_feed_progress,
                                  fund_feed=_fund_feed(args.fetch_fund))

    if args.oos:
        res = bt.run_oos(start, end, sell_rules=sell or None, progress=progress,
                         mkt=mkt, sector_map=sector_map, day_feed=day_feed)
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    else:
        rep = bt.run(start, end, sell_rules=sell or None, progress=progress,
                     mkt=mkt, sector_map=sector_map, day_feed=day_feed)
        print(json.dumps(rep, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
