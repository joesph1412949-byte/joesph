# -*- coding: utf-8 -*-
"""板块感知层(纯计算): 孕育期候选 / 五阶段定位 / 资金惯性。

设计: docs/superpowers/specs/2026-09-06-sector-perception-design.md
- 只依赖 mkt 切片(dict), 不碰网络/缓存/registry(sector_score 同风格)。
- 观察模式: 输出仅供复盘面板, 不进因子打分(用户 2026-09-06 拍板)。
- 缺数据降级: 信号不可判不计数, 阶段标"数据不足", 惯性表返回 []。
- 防未来: 调用方必须传 asof 切片(GUI 用 mkt_snapshot 全量, 天然只见过去)。
"""
# 阈值全部模块级常量(复盘观察用, 口径见设计 spec §2)
GEST_MAX_R5 = 8.0         # 孕育期"未启动"上限: 近5日涨幅 < 8%
REL_DAYS = 3              # 跑赢信号观察窗: 近3日
REL_MIN = 2               # 其中≥2日板块日涨幅>上证
SHARE_MIN_DAYS = 20       # 占比信号: 至少20日K线才可比 MA5/MA20
STAGE_R5_MAIN = 5.0       # 主升期: 近5日涨幅 > 5%
STAGE_R3_START = 4.0      # 启动期: 近3日涨幅 > 4%
STAGE_R10_START_MAX = 5.0  # 启动期: 且近10日涨幅 ≤ 5%(跳升刚脱离盘整)
STAGE_R5_PEAK = 10.0      # 高潮期: 近5日涨幅 > 10%
STAGE_SHARE_PCT = 0.90    # 高潮期: 最新日占比处自身历史≥90分位
FLOW_TOP_N = 3            # 惯性表: 每日净流入前3
FLOW_STREAK_MIN = 5       # 系统性增配: 连续上榜≥5天

# 纸面仓位上限(板块周度跟踪 W1, spec 2026-09-07 §3 六阶段值照抄)。
# **纸面参考, 无回测背书, 未接入资金管理** — 只读展示, 严禁进买入/仓位路径。
POS_CAP = {"孕育期": 30, "启动期": 50, "主升期": 75,
           "高潮期": 50, "退潮期": 10, "休整期": 30}


def _ret_series(dates, closes):
    """日涨幅(%)序列 → {date: ret%}。前收缺失/非正的日期不计。"""
    out = {}
    for i in range(1, len(dates)):
        prev = closes[i - 1]
        if prev and prev > 0:
            out[dates[i]] = (closes[i] / prev - 1.0) * 100.0
    return out


def _share_series(mkt):
    """全板块日成交额占比 → {code: [share_t...]}(日期骨架取全板块当日有数据的
    日子, 某板块当日无数据 → None)。总量 = 当日有数据板块之和(同源, 无需基准)。"""
    secs = mkt.get("sector") or {}
    amounts = {}
    all_dates = set()
    for code, rec in secs.items():
        rec = rec or {}
        m = {}
        for d, a in zip(rec.get("dates") or [], rec.get("amount") or []):
            if a and a > 0:
                m[d] = float(a)
        if m:
            amounts[code] = m
            all_dates.update(m)
    dates = sorted(all_dates)
    totals = [sum(m.get(d, 0.0) for m in amounts.values()) for d in dates]
    shares = {}
    for code, m in amounts.items():
        col = []
        for i, d in enumerate(dates):
            v = m.get(d)
            col.append(v / totals[i] if v and totals[i] > 0 else None)
        shares[code] = col
    return shares


def _mean(vals):
    vs = [v for v in vals if v is not None]
    return sum(vs) / len(vs) if vs else None


def _signals(rec, bench_ret, share_col):
    """孕育期三信号(命中名列表): rel(跑赢上证) / share(占比MA5>MA20) /
    struct(站上5日线且近5日收阳≥3)。不可判的信号不计数(fail-open)。"""
    # 日期与收盘按对同步过滤(脏数据下保持索引对齐, 避免 _ret_series 错位/越界)
    pairs = [(d, c) for d, c in zip(rec.get("dates") or [],
                                    rec.get("close") or []) if c]
    dates = [d for d, _ in pairs]
    closes = [c for _, c in pairs]
    out = []
    # rel: 近 REL_DAYS 日中≥REL_MIN 日板块日涨幅 > 上证日涨幅(交易日对齐)
    if bench_ret:
        sret = _ret_series(dates, closes)
        start = max(1, len(dates) - REL_DAYS)
        wins = total = 0
        for i in range(start, len(dates)):
            d = dates[i]
            if d in bench_ret and d in sret:
                total += 1
                if sret[d] > bench_ret[d]:
                    wins += 1
        if total > 0 and wins >= REL_MIN:
            out.append("rel")
    # share: 占比 MA5 > MA20
    share5 = _mean(share_col[-5:]) if share_col else None
    share20 = _mean(share_col[-20:]) if share_col else None
    if share5 is not None and share20 is not None and share5 > share20:
        out.append("share")
    # struct: 站上5日线 且 近5日收阳≥3
    if len(closes) >= 6:
        ma5 = sum(closes[-5:]) / 5.0
        up_days = sum(1 for i in range(len(closes) - 5, len(closes))
                      if closes[i] > closes[i - 1])
        if closes[-1] > ma5 and up_days >= 3:
            out.append("struct")
    return out


def _r(closes, n):
    """近 n 日涨幅(%); 数据不足 → None。"""
    if len(closes) <= n:
        return None
    prev = closes[-n - 1]
    if not prev or prev <= 0:
        return None
    return (closes[-1] / prev - 1.0) * 100.0


def _stage_of(closes, share_col, r3, r5, r10, signals, hits):
    """阶段判定(先到先得): 退潮>高潮>主升>启动>孕育>休整。"""
    have_share = [v for v in share_col if v is not None]
    if len(closes) < 11 or len(have_share) < SHARE_MIN_DAYS:
        return "数据不足", "K线或占比不足"
    share5 = _mean(share_col[-5:])
    share20 = _mean(share_col[-20:])
    # 退潮: 占比回落且下跌
    if share5 < share20 and r5 < 0:
        return "退潮期", "占比回落(%.4f<%.4f)且5日跌%.1f%%" % (
            share5, share20, r5)
    # 高潮: 最新日占比处自身历史≥90分位 且 大涨
    last_share = have_share[-1]
    rank = sum(1 for v in have_share if v <= last_share) / len(have_share)
    if r5 > STAGE_R5_PEAK and rank >= STAGE_SHARE_PCT:
        return "高潮期", "占比分位%.0f%%且5日涨%.1f%%" % (rank * 100, r5)
    # 主升: 涨且占比升
    if r5 > STAGE_R5_MAIN and share5 > share20:
        return "主升期", "5日涨%.1f%%且占比升(%.4f>%.4f)" % (
            r5, share5, share20)
    # 启动: 3日跳升且10日涨幅仍小(先于孕育判, 启动非孕育)
    if r3 > STAGE_R3_START and r10 <= STAGE_R10_START_MAX:
        return "启动期", "3日涨%.1f%%且10日涨%.1f%%" % (r3, r10)
    # 孕育: 未启动且信号≥2项
    if r5 < GEST_MAX_R5 and hits >= 2:
        return "孕育期", "信号%d项(%s)" % (hits, "+".join(signals))
    return "休整", "无阶段特征"


def sector_table(mkt, new_high=None, etf_quotes=None, etf_map=None):
    """mkt 切片 → 全板块感知表(孕育/阶段, 观察模式)。

    行: {code, name, stage, note, r3, r5, r10, share5, share20,
         share_chg(share5-share20), hits, signals,
         new_high, etf, pos_cap, week_rank}。mkt 空 → []。

    可选注入(板块周度跟踪 W1, 缺省 None → 对应列 None, 纯函数零破坏):
    - new_high: new_high_counts() 产物 {行业名: {"nh","base"}}, 按行名称对齐。
    - etf_map:  sector_etf_map.SECTOR_ETF_MAP {行业名: {"code","name"}/{}}。
    - etf_quotes: market_data.fetch_etf_quotes() 产物
      {etf_code: {"amount","pct_chg"}}; 映射有锚点但行情缺 → 数值 None
      (fail-open); 映射留空 {} → etf 列 None。
    - week_rank: 恒输出 — 按 r5 降序名次(1 起), r5 缺失 → None 不参与。
    - pos_cap: POS_CAP 纸面上限; 阶段"休整"(无"期"字)归一到"休整期"取值,
      "数据不足" → None。"""
    if not isinstance(mkt, dict) or not mkt:
        return []
    bench = mkt.get("benchmark") or {}
    bench_ret = _ret_series(bench.get("dates") or [], bench.get("close") or [])
    shares = _share_series(mkt)
    out = []
    for code, rec in sorted((mkt.get("sector") or {}).items()):
        rec = rec or {}
        closes = [c for c in (rec.get("close") or []) if c]
        share_col = shares.get(code) or []
        r3, r5, r10 = _r(closes, 3), _r(closes, 5), _r(closes, 10)
        signals = _signals(rec, bench_ret, share_col)
        hits = len(signals)
        share5 = _mean(share_col[-5:]) if share_col else None
        share20 = _mean(share_col[-20:]) if share_col else None
        if r5 is None or share5 is None or share20 is None:
            stage, note = "数据不足", "K线或占比不足"
        else:
            stage, note = _stage_of(closes, share_col, r3, r5, r10,
                                    signals, hits)
        name = rec.get("name") or ""
        if etf_map is None:
            etf = None
        else:
            anchor = etf_map.get(name) or None
            if not anchor:
                etf = None
            else:
                q = (etf_quotes or {}).get(anchor.get("code")) or {}
                etf = {"code": anchor.get("code"), "name": anchor.get("name"),
                       "amount": q.get("amount"), "pct_chg": q.get("pct_chg")}
        pos_cap = POS_CAP.get(stage)
        if pos_cap is None:
            pos_cap = POS_CAP.get(stage + "期")   # "休整" → "休整期"
        out.append({"code": str(code), "name": name,
                    "stage": stage, "note": note,
                    "r3": r3, "r5": r5, "r10": r10,
                    "share5": share5, "share20": share20,
                    "share_chg": (share5 - share20
                                  if share5 is not None
                                  and share20 is not None else None),
                    "hits": hits, "signals": signals,
                    "new_high": (new_high.get(name)
                                 if new_high is not None else None),
                    "etf": etf, "pos_cap": pos_cap, "week_rank": None})
    # 周排名: 按 r5 降序(1 起); r5 缺失的行保持 None 不参与
    ranked = sorted((r for r in out if r["r5"] is not None),
                    key=lambda r: -r["r5"])
    for i, r in enumerate(ranked, 1):
        r["week_rank"] = i
    return out


def new_high_counts(sector_map, zt_cache, window=60):
    """60日新高家数(纯计算): {行业名: {"nh": 创新高家数, "base": 有效基数}}。

    sector_map: {带后缀代码: 行业名} — 与 F9 同款带后缀契约
    (data.py 注入的 sector_map 键即 '600051.SH' 格式), 值作为分组标签原样聚合。
    zt_cache: {code: {"dates": [...], "close": [...]}}(zt_history 缓存形态)。
    口径: 个股 len(close) < window 不计入 base; 最新收盘 ≥ 近 window 日
    (含当日)最高 → 计入 nh。全离线纯函数, 不碰网络/缓存。"""
    out = {}
    for code, label in (sector_map or {}).items():
        rec = (zt_cache or {}).get(code)
        g = out.setdefault(label, {"nh": 0, "base": 0})
        if not rec:
            continue
        closes = [c for c in (rec.get("close") or []) if c]
        if len(closes) < window:
            continue
        g["base"] += 1
        if closes[-1] >= max(closes[-window:]):
            g["nh"] += 1
    return out


def flow_inertia(flow_rank, top_n=FLOW_TOP_N, need=FLOW_STREAK_MIN):
    """资金惯性表: 每日净流入前 top_n 的板块连续上榜天数(streak)。

    flow_rank: {"dates": [...], "rows": {date: [{code,name,net_in}...]}}
    → [{code, name, streak, last_net_in, systematic(streak≥need)}],
    streak≥1 才输出, 按 streak 降序再净流入降序。输入空/缺 → []。"""
    if not isinstance(flow_rank, dict):
        return []
    dates = sorted(flow_rank.get("dates") or [])
    rows = flow_rank.get("rows") or {}
    if not dates:
        return []
    top_sets = []
    for d in dates:
        day = sorted((r for r in (rows.get(d) or []) if isinstance(r, dict)),
                     key=lambda r: r.get("net_in") or 0, reverse=True)
        top_sets.append({r.get("code") for r in day[:top_n]})
    names, lasts = {}, {}
    for d in dates:      # 升序遍历, 最近一天的名称/净流入生效
        for r in rows.get(d) or []:
            if isinstance(r, dict) and r.get("code"):
                names[r["code"]] = r.get("name") or ""
                lasts[r["code"]] = r.get("net_in")
    out = []
    for code in names:
        streak = 0
        for top in reversed(top_sets):
            if code in top:
                streak += 1
            else:
                break
        if streak >= 1:
            out.append({"code": code, "name": names.get(code, ""),
                        "streak": streak, "last_net_in": lasts.get(code),
                        "systematic": streak >= need})
    out.sort(key=lambda r: (-r["streak"], -(r["last_net_in"] or 0)))
    return out
