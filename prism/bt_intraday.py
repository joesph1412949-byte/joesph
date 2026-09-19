# -*- coding: utf-8 -*-
"""1 分钟特征层(规格 2026-09-16 §5): 回测 F2(早封板) / F3(封单强度·分钟级代理)。

实盘 F2/F3 依赖实时 tick(封板时间 timetag / 买一队列 bidVol), 回测拿不到分笔。
本模块用 **当日 1 分钟 K 线** 还原封板盘面(探针 .superpowers/sdd/probe-1m-seal.py
已验证口径), 离线落盘供回测按 (code, ISO 日期) 读取:

  download_features(codes_by_day)   显式采集(QMT download_history_data2 批量,
                                    按周分片 + 速率礼貌) → 按月 JSON 原子落盘
  features_for(code, day)           读缓存; 缺失 → None(因子 fail-open 0)
  compute_features(rows, limit)     纯函数: 首封/开板次数/一字板/板上量额

**网页请求/回测路径绝不下载**: build_day_feed(use_intraday=True) 只读已缓存
特征, 缓存缺失的日期静默降级并在报告 data_notes 说明; 下载只发生在显式采集
命令(python -m backtest.cli --build-intraday)。

边界: QMT 1m 最早可回溯 2025-09-15, 但**实测缓存覆盖从 2025-09-16 起**
(09-15 当天也是 0 条) → 之前的日期特征缺失属预期, 因子 fail-open 0 并在报告
标注覆盖范围, 不造假。2026-06-15~18 那 4 天曾是采集缺口, 已于 2026-09-19 补采
(现覆盖 16345 股日 / 244 天, 2025-09-16 ~ 2026-09-18) —— 报告文案只动态给覆盖
数, 不再写死"某几天缺口"(静态文案会过期)。补采入口:
`python -m backtest.cli --build-intraday`。
"""
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from shared.common import (CACHE_DIR, atomic_write, limit_ratio_for_code,
                           with_market_suffix)

FEATURE_DIR = CACHE_DIR / "bt_intraday"

# QMT 1m 历史最早可回溯日(2026-09-16 实测): 之前的 download 必空, 直接跳过。
EARLIEST_1M_DAY = date(2025, 9, 15)

# 首封/开板判定容差: 价格进入涨停价 1.1 分内都算"在板上"(探针实测口径,
# 防浮点毛刺; 1m bar 的 high=涨停价精度两位)。
_SEAL_TOL = 0.011

# 板上量额累加的批量下载参数(速率礼貌: 分批 + 批间停顿; 只在采集命令路径跑)。
_BATCH_CODES = 100        # download_history_data2 单批最多股票数(40 只×1周≈2.4s)
_PAUSE_SECS = 0.5         # 批间停顿, 不打满 QMT 数据服务

# 按月读盘 memo: 回测逐日逐股调 features_for, 同月只读一次盘。
# key = (str(FEATURE_DIR), "YYYY-MM") —— FEATURE_DIR 可被测试注入, 必须入 key。
_MONTHS = {}


def _iso_day(raw):
    """date/ISO/"YYYYMMDD" → ISO 字符串; 非法 → None。

    与 backtest/cli.py 的 _iso_day 同一归一口径(xtdata 收窄 field_list 后
    time 列会变格式/缺失, 任何日期都必须先归一再做字符串比较)。
    """
    if isinstance(raw, datetime):
        return raw.strftime("%Y-%m-%d")
    if isinstance(raw, date):
        return raw.strftime("%Y-%m-%d")
    s = str(raw or "").strip()
    if not s:
        return None
    if len(s) == 8 and s.isdigit():
        return "%s-%s-%s" % (s[:4], s[4:6], s[6:8])
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def _month_path(ym):
    """"YYYY-MM" → 按月分片文件路径。"""
    return Path(FEATURE_DIR) / ("%s.json" % ym)


def _month_rec(ym):
    """读一个月的分片(带 memo); 文件缺失/脏 → {}(缓存损坏不炸回测)。"""
    key = (str(FEATURE_DIR), ym)
    if key in _MONTHS:
        return _MONTHS[key]
    p = _month_path(ym)
    try:
        rec = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        rec = {}
    _MONTHS[key] = rec if isinstance(rec, dict) else {}
    return _MONTHS[key]


def features_for(code, day):
    """读 (code, ISO 日期) 的 1m 特征; 缓存缺失 → None(调用方 fail-open 0)。

    day 接受 date / ISO / "YYYYMMDD"。返回拷贝, 调用方原地改不污染缓存。
    只读已落盘缓存, 无任何下载(网页请求安全)。
    """
    iso = _iso_day(day)
    if not iso or not code:
        return None
    rec = _month_rec(iso[:7])
    feat = (rec.get(str(code)) or {}).get(iso)
    return dict(feat) if isinstance(feat, dict) else None


def seal_timetag(iso, hm):
    """首封 "HH:MM" → 当日该分钟的毫秒 epoch(F2 的 tick.timetag 同型)。

    回测因子走与实盘相同的 _parse_timetag_hhmm 解析路径, 所以这里必须回填
    当日本地时区的毫秒 epoch 而不是直接塞字符串。无法解析 → None。
    """
    if not hm:
        return None
    import re
    m = re.search(r"(\d{1,2}):(\d{2})", str(hm))
    if not m:
        return None
    try:
        base = datetime.strptime(iso, "%Y-%m-%d")
        return int(base.replace(hour=int(m.group(1)),
                                minute=int(m.group(2))).timestamp() * 1000)
    except (ValueError, OSError, OverflowError):
        return None


# ---------------------------------------------------------------- 特征算法

def _bar_hm(raw):
    """1m bar 时间 → "HH:MM"; 兼容 毫秒/秒 epoch、"YYYYMMDDHHMMSS"、
    ISO/Timestamp 字符串(xtdata 各版本取法不一)。解析不了 → None。"""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.strftime("%H:%M")
    if isinstance(raw, (int, float)):
        try:
            sec = raw / 1000.0 if raw > 1e11 else raw
            return datetime.fromtimestamp(sec).strftime("%H:%M")
        except (OSError, ValueError, OverflowError):
            return None
    s = str(raw).strip()
    if len(s) >= 12 and s[:14].isdigit():        # "20260904093200"
        return "%s:%s" % (s[8:10], s[10:12])
    import re
    m = re.search(r"(\d{1,2}):(\d{2})", s)
    return "%02d:%02d" % (int(m.group(1)), int(m.group(2))) if m else None


def compute_features(rows, limit_price):
    """当日 1m bars → 特征 dict(纯函数, 离线可测)。

    rows: [(time_raw, high, low, close, volume, amount), ...] 当日升序
    (volume 单位=股, xtdata 1m 实测; amount 单位=元)。
    口径(与探针逐条一致, 2026-09-04 真数据核对过):
      首封  = 首个 high ≥ limit-0.011 的分钟
      开板  = 首封之后 low < limit-0.011(连续多根算一次, open_times 计数)
      板上量额 = 首封至收盘累加(含开板时段 —— 探针/样例数字的口径)
      一字板 = 首封在第 1 根且未开板
      收盘封 = 末根 high/close ≥ limit-0.011
    bars<1 或涨停价非法 → None(宁缺勿假)。
    """
    if not rows:
        return None
    try:
        limit = float(limit_price)
    except (TypeError, ValueError):
        return None
    if limit <= 0:
        return None
    ups = limit - _SEAL_TOL
    n = len(rows)
    first_i = None
    for i, row in enumerate(rows):
        try:
            if float(row[1]) >= ups:
                first_i = i
                break
        except (TypeError, ValueError, IndexError):
            continue
    first_hm = None
    opened = False
    open_times = 0
    on_board_amt = 0.0
    on_board_vol = 0.0
    if first_i is not None:
        first_hm = _bar_hm(rows[first_i][0])
        on_board = True        # 首封视为"在板上"的起点
        for i in range(first_i, n):
            row = rows[i]
            try:
                low = float(row[2])
                amt = float(row[5])
                vol = float(row[4])
            except (TypeError, ValueError, IndexError):
                continue
            if i == first_i:
                # 首封 bar: 量额计入, 但"在板上"状态由此锚定, 其 low 不计开板
                # (探针 opened 口径: 从首封的下一根才开始找 low < limit-0.011)。
                on_board_amt += amt
                on_board_vol += vol
                continue
            off = low < ups
            if off and on_board:
                open_times += 1          # "下板 run"数: 同一次开板的 N 根只记一次
            on_board = not off
            on_board_amt += amt
            on_board_vol += vol
        opened = open_times > 0
    # 收盘封: 末根 high/close 都在板上(与首封无关, 从未触板时按 last bar 判)
    sealed_close = False
    last = rows[-1]
    try:
        sealed_close = float(last[1]) >= ups and float(last[3]) >= ups
    except (TypeError, ValueError, IndexError):
        sealed_close = False
    one_word = (first_i == 0 and not opened
                and _first_low_on_board(rows, ups))
    return {"limit_price": round(limit, 2),
            "first_seal_hm": first_hm,
            "opened": bool(opened),
            "open_times": int(open_times),
            "sealed_close": bool(sealed_close),
            "one_word": bool(one_word),
            "on_board_amt": round(on_board_amt, 2),
            "on_board_vol": round(on_board_vol / 100.0, 1),   # 股 → 手
            "bars": n}


def _first_low_on_board(rows, ups):
    """一字板判定: 第 1 根的 low 也在板上(整根都封着)。"""
    try:
        return float(rows[0][2]) >= ups
    except (TypeError, ValueError, IndexError):
        return False


# ---------------------------------------------------------------- 采集

def _sleep(secs):
    """批间停顿(测试可桩掉)。"""
    time.sleep(secs)


def _download_batch(codes, start_d8, end_d8):
    """批量下载一周的 1m 数据(真 QMT 调用, 只在采集命令路径)。

    download_history_data2 批量实测可用(40 只×1周=2.4s); 失败不抛 —— 缺
    数据的 (code, day) 按特征缺失处理(fail-open), 不让单批网络问题炸采集。
    """
    from xtquant import xtdata
    try:
        xtdata.download_history_data2(list(codes), period="1m",
                                      start_time=start_d8, end_time=end_d8)
    except Exception as exc:
        print("警告: 1m 批量下载失败 %s~%s(%d 只): %r"
              % (start_d8, end_d8, len(codes), exc))
    _sleep(_PAUSE_SECS)          # 批间礼貌(实测 40 只×1周=2.4s, 不打满服务)


def _load_1m(code, iso):
    """QMT 本地 1m 当日 bars → [(time_raw, high, low, close, volume, amount)]。

    只读本地(下载由 download_features 先做); 无数据 → []。
    脏行丢弃, 不让单条坏 bar 炸整段。
    """
    from xtquant import xtdata
    d8 = iso.replace("-", "")
    try:
        data = xtdata.get_local_data(field_list=[], stock_list=[code],
                                     period="1m", start_time=d8, end_time=d8,
                                     count=-1)
        df = (data or {}).get(code)
    except Exception:
        return []
    if df is None or len(df) == 0:
        return []
    # time 列(毫秒)优先; 收窄字段后缺失 → 索引键。统一归一(Task 1 踩过的坑)。
    if "time" in df.columns:
        times = list(df["time"])
    else:
        times = list(df.index)
    highs = df["high"].tolist() if "high" in df.columns else []
    rows = []
    for i in range(min(len(times), len(highs))):
        try:
            low = float(df["low"].iloc[i])
            close = float(df["close"].iloc[i])
            vol = float(df["volume"].iloc[i])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        try:
            amt = float(df["amount"].iloc[i]) if "amount" in df.columns else 0.0
        except (TypeError, ValueError, IndexError, KeyError):
            amt = 0.0
        try:
            rows.append((times[i], float(highs[i]), low, close, vol, amt))
        except (TypeError, ValueError):
            continue
    return rows


def _prev_daily_close(code, iso):
    """昨收 = 目标日**前一交易日**日线收盘(单位=元)。

    注意不能用 get_instrument_detail 的 PreClose —— 那是"当天快照"的昨收,
    涨停价必须按"前一交易日"的收盘推(本日 bar 的口径)。失败/无数据 → None。
    """
    from xtquant import xtdata
    dt = datetime.strptime(iso, "%Y-%m-%d").date()
    start = (dt - timedelta(days=20)).strftime("%Y%m%d")
    end = (dt - timedelta(days=1)).strftime("%Y%m%d")
    try:
        data = xtdata.get_local_data(field_list=["close"], stock_list=[code],
                                     period="1d", start_time=start,
                                     end_time=end, count=-1)
        df = (data or {}).get(code)
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    if "time" in df.columns:
        keys = [_iso_day(datetime.fromtimestamp(int(t) / 1000.0))
                for t in df["time"]]
    else:
        keys = [_iso_day(t) for t in df.index]
    closes = []
    for k, c in zip(keys, df["close"].tolist()):
        if k and k < iso:
            try:
                closes.append((k, float(c)))
            except (TypeError, ValueError):
                continue
    closes.sort(key=lambda r: r[0])
    return closes[-1][1] if closes else None


def _merge_write(ym, data):
    """把新特征并进该月的分片文件(读改写, 原子落盘)。返回写入记录数。"""
    p = _month_path(ym)
    try:
        rec = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        rec = {}
    if not isinstance(rec, dict):
        rec = {}
    n = 0
    for code, days in data.items():
        bucket = rec.setdefault(code, {})
        for iso, feat in days.items():
            bucket[iso] = feat
            n += 1
    atomic_write(p, json.dumps(rec, ensure_ascii=False, sort_keys=True))
    return n


def download_features(codes_by_day, progress=None):
    """批量采集 1m 特征并落盘(**只在显式采集命令里调用; 网页/回测绝不调**)。

    codes_by_day: {date(ISO/"YYYYMMDD"/date): [code, ...]}(回测侧 = 逐日涨停池)。
    步骤: 过滤早于 EARLIEST_1M_DAY 的日期与已缓存条目 → 按 ISO 周分片, 每片
    合并一次 download_history_data2(单批 ≤_BATCH_CODES 只, 批间 _sleep) →
    逐 (code, day) 读本地 1m 算特征 → 按月分片原子落盘(每月至多写一次)。

    幂等: 已在缓存里的 (code, day) 跳过(不重复下载/重算)。
    返回 {"stock_days": 本次算出特征的 (code,day) 数,
          "written": 本次新落盘的记录数}。
    progress(done, total): 本次要处理的 (code,day) 计数(可选)。
    """
    pairs = []
    for day, codes in (codes_by_day or {}).items():
        iso = _iso_day(day)
        if not iso:
            continue
        if datetime.strptime(iso, "%Y-%m-%d").date() < EARLIEST_1M_DAY:
            continue                       # 1m 不可回溯, 连下载都不发起
        for code in codes or []:
            if code and not features_for(code, iso):
                pairs.append((iso, str(code)))
    pairs.sort()
    total = len(pairs)
    # 按 ISO 周分片: 同周的 (code,day) 合并一批下载(一周一个窗口)
    shards = {}
    for iso, code in pairs:
        wk = datetime.strptime(iso, "%Y-%m-%d").date().isocalendar()[:2]
        shards.setdefault(wk, []).append((iso, code))
    done = 0
    computed = 0
    out = {}
    for wk in sorted(shards):
        items = shards[wk]
        codes_sfx = sorted({with_market_suffix(c) for _i, c in items})
        days = sorted({i for i, _c in items})
        for chunk_start in range(0, len(codes_sfx), _BATCH_CODES):
            chunk = codes_sfx[chunk_start:chunk_start + _BATCH_CODES]
            _download_batch(chunk, days[0].replace("-", ""),
                            days[-1].replace("-", ""))
            if progress:
                progress(done, total)
        for iso, code in items:
            done += 1
            if progress:
                progress(done, total)
            sfx = with_market_suffix(code)
            prev = _prev_daily_close(sfx, iso)
            if not prev:
                continue                 # 无昨收 → 算不出涨停价, 宁缺勿假
            rows = _load_1m(sfx, iso)
            feat = compute_features(
                rows, round(prev * (1 + limit_ratio_for_code(code)), 2))
            if feat is None:
                continue                 # 无 1m 数据 → 特征缺失, 不造假
            out.setdefault(iso[:7], {}).setdefault(code, {})[iso] = feat
            computed += 1
    written = 0
    for ym in sorted(out):
        written += _merge_write(ym, out[ym])
    _MONTHS.clear()                      # 落盘后失效读 memo(本进程立刻可见)
    return {"stock_days": computed, "written": written}
