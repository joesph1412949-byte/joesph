# -*- coding: utf-8 -*-
"""通达信(pytdx)数据源 —— QMT 的补充与降级备用。

定位: 补 QMT 拿不到或已停更的数据(大盘指数K线/板块指数K线/全市场快照兜底),
并在 QMT miniQMT 未启动时降级顶上。不替代 QMT 的板块成分股与证券列表。

### 实测约束(2026-09-05 逐服务器验证, 改动前务必先读)

1. **服务器 123.125.108.14 是残废的**: 只能取财务/统计, K线与快照全报
   `calling function error`。已剔除出 _HOSTS。
2. **板块/概念指数 K 线必须用 get_index_bars**。用 get_security_bars 取
   880/881 代码不报错, 但返回的是垃圾内存(日期解析出 "92735-92-44",
   close 出现 8.4e+80), 静默污染因子。
3. **连接会被污染**: 取过 block_gn.dat 之后, 同一连接上的 K线/快照
   全部返回 0 条。→ 每次调用前做健康检查, 失败立即重连。
4. get_security_quotes 每批上限 80 只, 超出报错 → 自动分批。
5. get_security_list(market=1) 实测失败(沪市列表取不到); market=0 时通时不通
   → 证券列表一律走 QMT。
6. pytdx 无美股/宏观: NDX/US10Y/VIX 继续走 akshare/FRED。
7. 复权: pytdx 返回不复权, 与 QMT get_market_data_ex 默认一致, 口径对齐。

所有对外接口 fail-open: 异常一律返回 None/{}, 绝不抛出阻塞选股。
"""
import threading
import time

import pandas as pd

try:
    from pytdx.hq import TdxHq_API
except Exception:  # pytdx 未安装时本模块可被安全导入, 只是不可用
    TdxHq_API = None

# 实测可用(剔除 123.125.108.14 —— 只能取财务, K线全报错)
_HOSTS = [
    ("180.153.18.170", 7709),
    ("60.12.136.250", 7709),
    ("115.238.56.198", 7709),
    ("218.75.126.9", 7709),
]

_CAT_DAILY = 9      # 日K
_QUOTE_BATCH = 80   # get_security_quotes 每批上限

_MKT_SH = 1
_MKT_SZ = 0
_MKT_BJ = 2

_lock = threading.Lock()
_api = None
_host_idx = 0
_last_err = None

# 全局开关。生产默认开启: QMT 取不到时由通达信顶上。
# 测试/离线环境必须 set_enabled(False) —— 否则单测会真的联网取到真实 K 线,
# 破坏 "数据缺失 → None" 的 fail-open 语义(实测踩过: 注入的假 DS 抛异常,
# 兜底却返回了 260 根真实日K, 3 个断网用例因此失败)。
_enabled = True


def set_enabled(v):
    """开启/关闭通达信取数。False 时所有接口直接返回 None/{}。"""
    global _enabled
    _enabled = bool(v)


def is_enabled():
    return _enabled


# ---------------------------------------------------------------- 代码/市场
def _split_code(code):
    """'600519.SH' / '600519' -> ('600519', market)。"""
    c = (code or "").strip().upper()
    if "." in c:
        c = c.split(".", 1)[0]
    if not c or not c[0].isdigit():
        return c, None
    if c.startswith(("88", "99")):        # 通达信板块指数
        return c, _MKT_SH
    if c.startswith("6"):
        return c, _MKT_SH
    if c.startswith(("0", "3")):
        return c, _MKT_SZ
    if c.startswith(("8", "4")):          # 北交所
        return c, _MKT_BJ
    return c, _MKT_SH


def _index_market(code):
    """指数/板块专用市场判定(与普通股票规则不同)。

    000001 在上证是"上证指数"(market=1), 在深市是"平安银行"(market=0)。
    指数走 get_index_bars 通道, 与个股不冲突, 按指数系列判定即可。
    """
    c = (code or "").strip().upper().split(".")[0]
    if c.startswith("88"):                # 880/881 通达信板块
        return _MKT_SH
    if c.startswith("399"):               # 深证系列
        return _MKT_SZ
    return _MKT_SH                        # 000xxx 上证/中证系列


# ---------------------------------------------------------------- 连接管理
def _connect():
    """建立连接, 失败轮转下一台服务器。返回 TdxHq_API 或 None。"""
    global _api, _host_idx, _last_err
    if TdxHq_API is None:
        _last_err = "pytdx 未安装"
        return None
    for i in range(len(_HOSTS)):
        host, port = _HOSTS[(_host_idx + i) % len(_HOSTS)]
        try:
            api = TdxHq_API(raise_exception=True, auto_retry=True)
            if api.connect(host, port):
                _host_idx = (_host_idx + i) % len(_HOSTS)
                _api = api
                _last_err = None
                return api
        except Exception as e:
            _last_err = "%s:%s %r" % (host, port, e)
    _api = None
    return None


def _get_api():
    """取可用连接; 无连接则新建。调用方持有 _lock。"""
    global _api
    if _api is None:
        return _connect()
    return _api


def _reconnect():
    """丢弃当前连接并换服务器重连。"""
    global _api
    try:
        if _api is not None:
            _api.disconnect()
    except Exception:
        pass
    _api = None
    return _connect()


def _call(fn, *args, **kwargs):
    """带一次重连重试的调用包装。失败或已禁用时返回 None。"""
    if not _enabled:
        return None
    with _lock:
        api = _get_api()
        if api is None:
            return None
        try:
            return getattr(api, fn)(*args, **kwargs)
        except Exception:
            api = _reconnect()
            if api is None:
                return None
            try:
                return getattr(api, fn)(*args, **kwargs)
            except Exception as e:
                global _last_err
                _last_err = "%s %r" % (fn, e)
                return None


def connected():
    """数据源是否可用(供健康检查/日志用)。开关关闭时恒 False。"""
    if not _enabled:
        return False
    with _lock:
        return _get_api() is not None


def last_error():
    return _last_err


def close():
    """显式关闭连接(测试/进程退出用)。"""
    global _api
    with _lock:
        try:
            if _api is not None:
                _api.disconnect()
        except Exception:
            pass
        _api = None


# ---------------------------------------------------------------- K线
def _to_df(bars):
    """pytdx K线列表 -> DataFrame(列名对齐 QMT: open/high/low/close/volume)。"""
    if not bars:
        return None
    rows = []
    for b in bars:
        dt = b.get("datetime") or ""
        rows.append({
            "datetime": str(dt),
            "date": str(dt)[:10],
            "open": b.get("open"),
            "high": b.get("high"),
            "low": b.get("low"),
            "close": b.get("close"),
            "volume": b.get("vol"),
            "amount": b.get("amount"),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return None
    return df


def get_kline(code, days=260):
    """个股日K。返回 DataFrame(open/high/low/close/volume/amount) 或 None。

    days 默认 260 —— M6/M7 需要 251 根算 MA250, 只取 250 会差一根导致恒 0。
    """
    c, mkt = _split_code(code)
    if mkt is None:
        return None
    bars = _call("get_security_bars", _CAT_DAILY, mkt, c, 0, int(days))
    return _to_df(bars)


def get_index_kline(code, days=60):
    """指数日K(上证/深成/科创50/沪深300 等)。F6 大盘配合用这个。"""
    c = (code or "").strip().upper().split(".")[0]
    if not c:
        return None
    bars = _call("get_index_bars", _CAT_DAILY, _index_market(c), c, 0, int(days))
    return _to_df(bars)


# ---------------------------------------------------------------- 快照
def get_quotes(codes):
    """批量实时快照, 自动分批(每批 80)。

    返回 {code(带后缀): {price,last_close,open,high,low,vol,amount,
    bid1..bid5,bid_vol1..bid_vol5,ask1..ask5,ask_vol1..ask_vol5}}

    字段口径贴近 QMT: price~lastPrice, last_close~lastClose, vol~volume。
    """
    if not codes:
        return {}
    pairs, suffix_map = [], {}
    for raw in codes:
        c, mkt = _split_code(raw)
        if mkt is None:
            continue
        pairs.append((mkt, c))
        suffix_map[c] = (raw, mkt)
    out = {}
    for i in range(0, len(pairs), _QUOTE_BATCH):
        chunk = pairs[i:i + _QUOTE_BATCH]
        raw = _call("get_security_quotes", chunk)
        for d in (raw or []):
            c = str(d.get("code") or "")
            src, mkt = suffix_map.get(c, (c, None))
            out[src] = {
                "code": src,
                "lastPrice": d.get("price"),
                "lastClose": d.get("last_close"),
                "open": d.get("open"),
                "high": d.get("high"),
                "low": d.get("low"),
                "volume": d.get("vol"),
                "amount": d.get("amount"),
                "askPrice": [d.get("ask%d" % k) for k in range(1, 6)],
                "bidPrice": [d.get("bid%d" % k) for k in range(1, 6)],
                "askVol": [d.get("ask_vol%d" % k) for k in range(1, 6)],
                "bidVol": [d.get("bid_vol%d" % k) for k in range(1, 6)],
                # 通达信无 timetag, 首次封板时间改用分钟K推导
                "timetag": None,
            }
    return out


# ---------------------------------------------------------------- 基本面
def get_finance(code):
    """财务/股本。含 liutongguben(流通股本) -> 算流通市值, 供 Y1/Y8。"""
    c, mkt = _split_code(code)
    if mkt is None:
        return None
    return _call("get_finance_info", mkt, c)


def float_shares(code):
    """流通股本(股)。失败返回 None。

    Y1/Y8 的 float_mv 原先靠 QMT FloatVolume × 实时价, tick 缺失即 0;
    这里给出独立兜底来源。
    """
    f = get_finance(code)
    if not f:
        return None
    v = f.get("liutongguben")
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


# ---------------------------------------------------------------- 自检
def self_check(verbose=True):
    """连通性自检: 返回 {项: (是否通过, 说明)}。不抛异常。"""
    res = {}
    t0 = time.time()
    ok = connected()
    res["连接"] = (bool(ok), "正常" if ok else "失败: %s" % last_error())
    if not ok:
        return res

    k = get_kline("600519.SH", days=260)
    n = len(k) if k is not None else 0
    res["个股日K"] = (n >= 251, "%s 根(需>=251 供 M6/M7)" % n)

    idx = get_index_kline("000001.SH", days=30)
    n = len(idx) if idx is not None else 0
    res["大盘指数K"] = (n > 0, "%s 根(F6)" % n)

    sec = get_index_kline("880368", days=60)
    n = len(sec) if sec is not None else 0
    sane = False
    if sec is not None and n:
        d = str(sec.iloc[-1]["datetime"])
        sane = d.startswith("2026") or d.startswith("2025")
    res["板块指数K"] = (n > 0 and sane,
                    "%s 根 %s" % (n, "日期正常" if sane else "日期乱码!"))

    q = get_quotes(["600519.SH", "000001.SZ"])
    res["实时快照"] = (len(q) == 2, "%s 只" % len(q))

    fs = float_shares("600519.SH")
    res["流通股本"] = (bool(fs), "%.0f 股" % fs if fs else "取不到")

    res["__elapsed__"] = round(time.time() - t0, 2)
    if verbose:
        for kk, v in res.items():
            if kk == "__elapsed__":
                continue
            passed, note = v
            print("  [%s] %-8s %s" % ("OK" if passed else "NG", kk, note))
        print("  耗时 %.2fs" % res["__elapsed__"])
    return res


if __name__ == "__main__":
    print("通达信数据源自检:")
    self_check()
