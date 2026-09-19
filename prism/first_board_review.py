# -*- coding: utf-8 -*-
"""首板盘后深度拆解(spec 2026-09-18)。

用户分工界面: 盘中(09:15-10:00)用户执行交易, 盘后由本模块对**每一个首板**
做彻底拆解(封板质量/板块共振/量能结构/资金行为/产业逻辑), 供用户拿结构化
证据对照手写因子。

定位: **观察层**。不进因子打分、不进买卖链路(同 sector_etf_map 口径)。

三层:
  collect(day, deps)       采集层: 从 prism 现有数据接口尽量自动抓全
  apply_manual(recs, m)    覆盖层: 手填兜底(非必需), 手填优先
  analyze(record)          分析层: 五维拆解 + 加权总评 + 置信度

不造假原则: 拿不到的字段一律 None + sources 标 "unknown", 绝不用推测值冒充;
总评时缺失维**从分母剔除**而非当 0 分(0 分会被误读为"已评估且很差")。
"""
import json
from datetime import date, datetime
from pathlib import Path

# 五维权重初值(spec §4)。可校准: 校准后只改这一处(同 F3 BT_SEAL_RATIO_T 做法)。
WEIGHTS = {"seal": 0.35, "sector": 0.25, "volume": 0.20,
           "fund": 0.15, "industry": 0.05}

DIM_LABELS = {"seal": "封板质量", "sector": "板块共振", "volume": "量能结构",
              "fund": "资金行为", "industry": "产业逻辑"}

# 产业逻辑维: 板块阶段(sector_stage) → 评分。常量集中, 校准只改这一处。
# ⚠️ 这是**首板客视角**, 与 sector_stage.POS_CAP(趋势跟踪视角)刻意相反:
#   趋势视角"主升期"最高(仓位给到 75); 首板视角"启动期"最高 —— 板块刚跳升时
#   首板是最强先锋, 到高潮/退潮期首板就是接盘。别把它当成 POS_CAP 抄错了。
# sector_stage 输出的休整期无"期"字("休整"), 两种写法都收。
INDUSTRY_STAGE_SCORE = {"启动期": 92, "主升期": 78, "孕育期": 58,
                        "休整": 50, "休整期": 50,
                        "高潮期": 40, "退潮期": 22}

# 输出目录
_STATE_DIR = Path(__file__).resolve().parent.parent / "runtime" / "state"
_REPORT_DIR = Path(__file__).resolve().parent.parent / "docs" / "reports"


def _iso8(day):
    """date/ISO/"YYYYMMDD" → "YYYYMMDD"; 非法 → 原样字符串。"""
    if isinstance(day, datetime):
        return day.strftime("%Y%m%d")
    if isinstance(day, date):
        return day.strftime("%Y%m%d")
    s = str(day or "").strip().replace("-", "")
    return s


def _hm_minutes(hm):
    """"HH:MM" → 当日分钟数; 非法 → None。"""
    if not hm or not isinstance(hm, str):
        return None
    try:
        h, m = hm.split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


# ================================================================ A 封板质量

def score_seal(r):
    """封板质量: 首封越早越高; 一字板满分; 每次开板扣分。

    依据 = 1m K线还原的盘面(bt_intraday), 可复现。
    """
    seal_time = r.get("seal_time")
    open_times = r.get("open_times")
    one_word = r.get("one_word")
    if seal_time is None and open_times is None and one_word is None:
        return {"score": None, "available": False,
                "evidence": ["1分钟盘面特征缺失(缓存未覆盖该日), 无法评估"]}

    ev = []
    if one_word:
        ev.append("一字板(首根即封且全天未开板)")
        return {"score": 100, "available": True, "evidence": ev}

    mins = _hm_minutes(seal_time)
    if mins is None:
        score = 50
        ev.append("首封时间未知, 按中性计")
    else:
        # 09:30=570min 基准; 09:35 前 = 90+, 之后每 5 分钟递减
        t9_30, t9_35, t10_00 = 570, 575, 600
        if mins <= t9_35:
            score = 95
        elif mins <= t10_00:
            score = 95 - int((mins - t9_35) / 5) * 5
        else:
            score = max(30, 70 - int((mins - t10_00) / 10) * 5)
        ev.append("首封 %s" % seal_time)

    if open_times:
        score -= open_times * 15
        ev.append("盘中开板 %d 次" % open_times)
    else:
        ev.append("全天未开板")

    on_board = r.get("on_board_amt")
    float_mv = r.get("float_mv")
    if on_board is not None and float_mv:
        ratio = on_board / float_mv * 100
        ev.append("板上成交额占流通市值 %.2f%%" % ratio)
        if ratio > 8:
            score -= 10
            ev.append("板上换手偏重(接力盘多), 略扣分")

    return {"score": max(0, min(100, int(score))),
            "available": True, "evidence": ev}


# ================================================================ B 板块共振

def score_sector(r):
    """板块共振: 板块内当日涨停家数分档(spec §4)。"""
    n = r.get("sector_zt_count")
    if n is None:
        return {"score": None, "available": False,
                "evidence": ["板块映射缺失, 无法统计板块内涨停家数"]}

    name = r.get("sector_name") or "未知板块"
    if n >= 6:
        score = 90 + min(10, n - 6)
    elif n >= 3:
        score = 70 + (n - 3) * 7
    elif n == 2:
        score = 55
    else:
        score = 30

    ev = ["所属「%s」当日 %d 只涨停" % (name, n)]
    if n <= 1:
        ev.append("板块内独苗(仅 1 只), 疑跟风脉冲")
    elif n >= 5:
        ev.append("板块内涨停密集, 共振强")
    return {"score": int(score), "available": True, "evidence": ev}


# ================================================================ C 量能结构

def score_volume(r):
    """量能结构: 量比 1.5-3 最佳; 缩量/过度放量都降分。"""
    vr = r.get("vol_ratio")
    amount = r.get("amount")
    prev5 = r.get("prev5_avg_amt")
    if vr is None and amount is None:
        return {"score": None, "available": False,
                "evidence": ["当日量额缺失, 无法评估量能结构"]}

    ev = []
    if prev5 and amount:
        mult = amount / prev5
        ev.append("当日成交额 %.2f 亿, 为前 5 日均量的 %.1f 倍"
                  % (amount / 1e8, mult))
        vr = vr if vr is not None else mult

    if vr is None:
        return {"score": 50, "available": True,
                "evidence": ev + ["量比未知, 按中性计"]}

    if 1.5 <= vr <= 3:
        score = 80 + int(min(15, (vr - 1.5) * 10))
    elif vr < 1.5:
        # 缩量上板: 承接弱, 越低越差(vr=1.0 → 40, vr=0.5 → 25)
        score = max(15, int(vr * 40))
    else:  # 过度放量
        score = max(40, 90 - int((vr - 3) * 8))
    ev.append("量比 %.2f" % vr)
    if vr > 5:
        ev.append("放量过猛, 接力盘重")
    elif vr < 1.0:
        ev.append("缩量上板, 承接弱")
    return {"score": max(0, min(100, int(score))),
            "available": True, "evidence": ev}


# ================================================================ D 资金行为

def score_fund(r):
    """资金行为: 有封单金额 → 按占流通市值比; 无 → 板上量额代理(标"代理")。"""
    seal_amount = r.get("seal_amount")
    float_mv = r.get("float_mv")
    on_board = r.get("on_board_amt")

    if seal_amount is not None and float_mv:
        ratio = seal_amount / float_mv * 100
        if ratio >= 1.0:
            score = 90
        elif ratio >= 0.5:
            score = 75
        elif ratio >= 0.2:
            score = 60
        else:
            score = 40
        return {"score": score, "available": True,
                "evidence": ["封单金额 %.2f 亿, 占流通市值 %.2f%%"
                             % (seal_amount / 1e8, ratio)]}

    if on_board is not None and float_mv:
        ratio = on_board / float_mv * 100
        score = 70 if ratio <= 3 else (55 if ratio <= 8 else 45)
        return {"score": score, "available": True,
                "evidence": ["封单金额不可得(盘后无法复现 buy1 队列), "
                             "用板上量额代理: %.2f 亿, 占流通市值 %.2f%%"
                             % (on_board / 1e8, ratio)]}

    return {"score": None, "available": False,
            "evidence": ["封单金额与板上量额均不可得, 无法评估资金行为"]}


# ================================================================ E 产业逻辑

def score_industry(r):
    """产业逻辑: 按所属板块所处阶段(sector_stage)评分, 口径见 INDUSTRY_STAGE_SCORE。

    阶段不可得 → **available=False**(从总评分母剔除), 不再给 60 分"中性"冒充
    已评估 —— 那正是 spec 铁律①要防的假覆盖率。
    """
    stage = r.get("sector_stage")
    if not stage:
        return {"score": None, "available": False,
                "evidence": ["板块阶段数据缺失(缓存未覆盖), 无法评估产业逻辑"]}
    score = INDUSTRY_STAGE_SCORE.get(stage)
    if score is None:
        return {"score": None, "available": False,
                "evidence": ["板块阶段「%s」不在评分口径内" % stage]}

    ev = ["所属「%s」板块处 %s" % (r.get("sector_name") or "该板块", stage)]
    r5 = r.get("sector_r5")
    if r5 is not None:
        ev.append("板块近 5 日 %+.1f%%" % r5)
    note = r.get("sector_stage_note")
    if note:
        ev.append(note)
    if stage == "启动期":
        ev.append("板块刚跳升, 首板为最强先锋")
    elif stage in ("高潮期", "退潮期"):
        ev.append("板块退潮风险, 首板易成接盘")
    return {"score": int(score), "available": True, "evidence": ev}


# ================================================================ 汇总

def total_score(dims):
    """五维加权总评。缺失维权重从分母剔除, 其余归一(不用 0 分冒充)。

    返回 (total:int|None, label:str)。
    """
    num = 0.0
    den = 0.0
    for k, w in WEIGHTS.items():
        d = dims.get(k) or {}
        if d.get("available") and d.get("score") is not None:
            num += d["score"] * w
            den += w
    if den <= 0:
        return None, "数据不足"
    total = int(round(num / den))

    # 跟风脉冲: 板块共振极弱时覆盖标签(spec §4)
    sec = dims.get("sector") or {}
    if sec.get("available") and (sec.get("score") or 100) <= 35:
        return total, "跟风脉冲"

    if total >= 75:
        label = "高"
    elif total >= 55:
        label = "中"
    elif total >= 35:
        label = "低"
    else:
        label = "低"
    return total, label


def analyze(record):
    """五维拆解 + 总评。返回 {dims, total, confidence}。"""
    dims = {
        "seal": score_seal(record),
        "sector": score_sector(record),
        "volume": score_volume(record),
        "fund": score_fund(record),
        "industry": score_industry(record),
    }
    total, label = total_score(dims)
    return {"dims": dims, "total": total, "confidence": label}


# ================================================================ 采集层

def _default_deps():
    """生产 deps: 接线 prism 现有接口。任一导入/调用失败 → 该路失败(不炸)。"""
    deps = {}

    def _zt_feed(day):
        from prism import zt_history
        return zt_history.qmt_zt_feed(day)

    def _intraday(code, day):
        from prism import bt_intraday
        return bt_intraday.features_for(code, day)

    def _sector_map():
        try:
            from prism import market_data as md
            cache = md._load_cache()
            out = {}
            for c6, rec in (cache.get("sector_map") or {}).items():
                if rec and rec.get("sector"):
                    suffix = ".SH" if c6.startswith("6") else ".SZ"
                    out[c6 + suffix] = rec["sector"]
            return out
        except Exception:
            return {}

    def _snapshot(code):
        """封单金额尝试: 实时快照存在才有; 盘后一般 None。"""
        try:
            from prism import live_account
            return live_account.seal_snapshot(code)
        except Exception:
            return None

    deps["zt_feed"] = _zt_feed
    deps["intraday"] = _intraday
    deps["sector_map"] = _sector_map()
    deps["sector_stage_map"] = _sector_stage_map()
    deps["kline_batch"] = _qmt_daily_batch
    deps["float_mv_batch"] = _float_mv_batch
    deps["snapshot"] = _snapshot
    return deps


def _sector_name_map():
    """行业代码 → 中文名(申万)。失败 → {}。"""
    try:
        from prism import market_data as md
        cache = md._load_cache()
        secs = cache.get("sectors") or {}
        return {str(k): (v.get("name") if isinstance(v, dict) else v)
                for k, v in secs.items()}
    except Exception:
        return {}


def _sector_stage_map():
    """行业代码 → sector_stage 行({code,name,stage,note,r5,...})。失败 → {}。

    走 mkt_snapshot(本地缓存, 实测 0.07s, 不触网) → sector_table 纯计算。
    只取 801 申万一级(app.py /api/sector_stage 同口径, 免 BK 同名行重复命中)。
    注: `cache["sectors"]` 只有 name 无 K 线, **不能**直接喂 sector_table。
    """
    try:
        from prism import market_data as md
        from prism import sector_stage as ss
        snap = md.mkt_snapshot()
        if not isinstance(snap, dict) or not snap:
            return {}
        snap["sector"] = {c: r for c, r in (snap.get("sector") or {}).items()
                          if str(c).startswith("801")}
        if not snap["sector"]:
            return {}
        return {str(r["code"]): r for r in ss.sector_table(snap)}
    except Exception:
        return {}


def _qmt_daily_batch(codes, day8, xt=None):
    """QMT 日K 批量取: {code: {amount, prev5_avg_amt}}。失败 → {}。

    一次 download_history_data2(批量) + 一次 get_market_data_ex(批量),
    避免逐码调用把 xtquant 拖崩(实测逐码 68 只会硬崩)。
    """
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}
    if xt is None:
        try:
            from xtquant import xtdata as xt
        except Exception:
            return {}
    try:
        xt.download_history_data2(codes, "1d", start_time="", end_time="")
    except Exception:
        pass
    try:
        data = xt.get_market_data_ex([], codes, period="1d", count=10) or {}
    except Exception:
        return {}
    out = {}
    for code in codes:
        df = data.get(code)
        if df is None or len(df) == 0:
            continue
        try:
            idxs = [str(i)[:8] for i in df.index]
            amounts = list(df["amount"])
        except Exception:
            continue
        if day8 not in idxs:
            continue
        i = idxs.index(day8)
        rec = {}
        amt = amounts[i]
        if amt is not None:
            rec["amount"] = float(amt)
        prev = [float(a) for a in amounts[max(0, i - 5):i] if a is not None]
        if prev:
            rec["prev5_avg_amt"] = sum(prev) / len(prev)
        if rec:
            out[code] = rec
    return out


def _qmt_daily(code, day8, xt=None):
    """单码 QMT 日K(兼容旧调用路径; 内部走批量)。"""
    return _qmt_daily_batch([code], day8, xt=xt).get(code) or {}


def _float_mv_batch(codes, xt=None):
    """批量流通市值(元): {code: 市值}。拿不到 → {}(不造假)。

    优先 QMT InstrumentDetail(流通股本) × 最新价; 失败回退 tdx float_shares。
    """
    codes = [c for c in (codes or []) if c]
    if not codes:
        return {}
    out = {}
    # 价: QMT 批量日K末根收盘
    px = {}
    if xt is None:
        try:
            from xtquant import xtdata as xt
        except Exception:
            xt = None
    if xt is not None:
        try:
            data = xt.get_market_data_ex([], codes, period="1d", count=1) or {}
            for c in codes:
                df = data.get(c)
                if df is not None and len(df):
                    px[c] = float(df["close"].iloc[-1])
        except Exception:
            pass
    # 流通股本: QMT instrument detail
    shares = {}
    if xt is not None:
        try:
            for c in codes:
                det = xt.get_instrument_detail(c) or {}
                s = det.get("FloatVolume") or det.get("FloatVol")
                if s:
                    shares[c] = float(s)
        except Exception:
            pass
    # 回退 tdx
    for c in codes:
        if c in px and c in shares:
            out[c] = px[c] * shares[c]
            continue
        try:
            from prism import tdx_source
            s = tdx_source.float_shares(c)
            p = px.get(c)
            if p is None:
                df = tdx_source.get_kline(c)
                if df is not None and len(df):
                    p = float(df["close"].iloc[-1])
            if s and p:
                out[c] = float(s) * float(p)
        except Exception:
            continue
    return out


def _float_mv(code, up_price=None, xt=None):
    """单码流通市值(元)。兼容旧调用; 内部走批量。"""
    return _float_mv_batch([code], xt=xt).get(code)


def backfill_intraday(day, codes=None, progress=None):
    """显式补采 1m 特征(封板质量维的数据源)。

    **只在用户显式调 `--backfill` 时执行** —— 沿用 bt_intraday 的约定:
    网页/报告路径绝不下载。QMT 离线 → 返回 {"error": ...}, 不改任何缓存。
    """
    codes = codes or []
    if not codes:
        return {"skipped": "无代码可采"}
    try:
        from prism import bt_intraday
        res = bt_intraday.download_features({day: codes}, progress=progress)
        return res
    except Exception as e:
        return {"error": "%s: %s" % (type(e).__name__, e)}


def collect(day, deps=None):
    """采集当日首板盘面事实。deps 注入便于测试。

    返回 List[BoardRecord] (plain dict, 每字段带 sources 标注)。失败 fail-open,
    缺字段标 unknown, 绝不造假。
    """
    deps = deps if deps is not None else _default_deps()
    day8 = _iso8(day)

    try:
        pool = deps["zt_feed"](day8) or []
    except Exception:
        return []

    smap = deps.get("sector_map") or {}
    sec_names = _sector_name_map()
    # 板块阶段表(产业逻辑维): 一次批量取, 失败 → {} 该维不可用(不给中性分冒充)
    stage_map = deps.get("sector_stage_map") or {}

    # 板块内涨停家数(含连板: 同板块当日所有涨停)
    sec_counts = {}
    for it in pool:
        sec = smap.get(it.get("code"))
        if sec:
            sec_counts[sec] = sec_counts.get(sec, 0) + 1

    # 首板代码(只对首板取量能/市值, 减少调用面)
    fb_codes = [it["code"] for it in pool if it.get("boards") == 1]

    # 量能: QMT 批量一次取全(逐码调用会拖崩 xtquant)
    kmap = {}
    try:
        kmap = deps["kline_batch"](fb_codes, day8) or {}
    except Exception:
        kmap = {}

    # 流通市值: 批量一次取
    mvmap = {}
    try:
        mvmap = deps["float_mv_batch"](fb_codes) or {}
    except Exception:
        mvmap = {}

    out = []
    for it in pool:
        if it.get("boards") != 1:      # 只要首板
            continue
        code = it.get("code")
        sources = {}

        # 1m 盘面特征
        feat = None
        try:
            feat = deps["intraday"](code, day8)
        except Exception:
            feat = None
        feat = feat or {}

        seal_time = feat.get("first_seal_hm")
        sources["seal_time"] = "auto" if seal_time else "unknown"
        open_times = feat.get("open_times")
        sources["open_times"] = "auto" if open_times is not None else "unknown"
        one_word = feat.get("one_word")
        sources["one_word"] = "auto" if one_word is not None else "unknown"
        on_board_amt = feat.get("on_board_amt")
        sources["on_board_amt"] = "auto" if on_board_amt is not None else "unknown"

        # 板块
        sec = smap.get(code)
        srow = ((stage_map.get(str(sec)) if sec else None) or {})
        # 名称: 申万名表优先, 板块阶段行兜底(两份缓存覆盖面不同)
        sec_name = (sec_names.get(str(sec)) if sec else None) or srow.get("name")
        sources["sector_name"] = "auto" if sec_name else "unknown"
        zt_count = sec_counts.get(sec) if sec else None
        sources["sector_zt_count"] = "auto" if zt_count is not None else "unknown"

        # 板块阶段(sector_stage): "数据不足" 视同不可得, 不参与产业逻辑评分
        stage = srow.get("stage")
        if stage == "数据不足":
            stage = None
        sources["sector_stage"] = "auto" if stage else "unknown"

        # 日K量能(批量表查)
        km = kmap.get(code) or {}
        amount = it.get("amount") or km.get("amount")
        prev5 = it.get("prev5_avg_amt") or km.get("prev5_avg_amt")
        sources["amount"] = "auto" if amount is not None else "unknown"
        sources["prev5_avg_amt"] = "auto" if prev5 is not None else "unknown"

        # 流通市值(批量表查)
        float_mv = it.get("float_mv") or mvmap.get(code)
        sources["float_mv"] = "auto" if float_mv is not None else "unknown"

        # 封单金额(常为 None)
        seal_amount = None
        try:
            seal_amount = deps["snapshot"](code)
        except Exception:
            seal_amount = None
        sources["seal_amount"] = "auto" if seal_amount is not None else "unknown"

        rec = {
            "code": code,
            "name": it.get("name") or "",
            "seal_time": seal_time,
            "open_times": open_times,
            "one_word": one_word,
            "on_board_amt": on_board_amt,
            "float_mv": float_mv,
            "amount": amount,
            "turnover": it.get("turnover"),
            "vol_ratio": it.get("vol_ratio"),
            "prev5_avg_amt": prev5,
            "sector_name": sec_name,
            "sector_zt_count": zt_count,
            "sector_stage": stage,
            "sector_stage_note": (srow.get("note") if stage else None),
            "sector_r5": (srow.get("r5") if stage else None),
            "seal_amount": seal_amount,
            "sources": sources,
        }
        out.append(rec)
    return out


# ================================================================ 覆盖层

def apply_manual(records, manual):
    """手填兜底: 手填优先覆写, source 标 manual。manual 为 None → 原样返回。"""
    if not manual:
        return records
    for r in records:
        patch = manual.get(r.get("code"))
        if not patch:
            continue
        for k, v in patch.items():
            if v is not None:
                r[k] = v
                r.setdefault("sources", {})[k] = "manual"
    return records


# ================================================================ 报告

def render_report(day, records):
    """人读版 Markdown 报告。缺失字段如实标"未知(原因)"。"""
    day8 = _iso8(day)
    lines = ["# 首板盘后拆解 %s-%s-%s" % (day8[:4], day8[4:6], day8[6:8]), ""]
    if not records:
        lines.append("当日无首板(涨停池为空或索引未覆盖)。")
        lines.append("")
        lines.append("> 若确认当日有涨停, 请检查 `python -m prism.zt_history --refresh`。")
        return "\n".join(lines)

    lines.append("共 %d 只首板。" % len(records))
    lines.append("")

    ranked = sorted(records, key=lambda r: -(analyze(r)["total"] or -1))
    for r in ranked:
        a = analyze(r)
        total = a["total"] if a["total"] is not None else "—"
        lines.append("## %s %s ｜ 总评 %s (%s)"
                     % (r.get("code"), r.get("name") or "", total,
                        a["confidence"]))
        lines.append("")
        lines.append("| 维度 | 评分 | 关键依据 |")
        lines.append("|---|---|---|")
        for k in ("seal", "sector", "volume", "fund", "industry"):
            d = a["dims"][k]
            sc = d["score"] if d["score"] is not None else "—"
            if not d["available"]:
                sc = "未知"
            ev = "；".join(d["evidence"]) or "—"
            lines.append("| %s | %s | %s |" % (DIM_LABELS[k], sc, ev))
        lines.append("")

        src = r.get("sources") or {}
        unknown = [k for k, v in src.items() if v == "unknown"]
        if unknown:
            lines.append("**数据缺失**：%s（盘后不可得或缓存未覆盖，未推测）"
                         % "、".join(unknown))
            lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


# ================================================================ 落盘 + CLI

def _atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(path)


def run(day, manual=None, want_report=True, out_dir=None,
        persist_when_empty=True):
    """端到端: 采集 → 覆盖 → 落 JSON(+报告)。返回 (records, paths)。

    persist_when_empty=False: 当日无首板时不落盘(web 查历史空日不该生成
    空文件污染日期列表)。
    """
    day8 = _iso8(day)
    recs = collect(day8)
    recs = apply_manual(recs, manual)
    paths = {}
    if not recs and not persist_when_empty:
        return recs, paths
    analyzed = [{"record": r, "analysis": analyze(r)} for r in recs]

    state_dir = Path(out_dir) if out_dir else _STATE_DIR
    json_path = state_dir / ("first_board_review_%s.json" % day8)
    _atomic_json(json_path, {"date": day8, "count": len(recs),
                             "items": analyzed})
    paths = {"json": str(json_path)}

    if want_report:
        md = render_report(day8, recs)
        rp = _REPORT_DIR / ("首板拆解_%s.md" % day8)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(md, encoding="utf-8")
        paths["report"] = str(rp)
    return recs, paths


def build_cli():
    import argparse
    ap = argparse.ArgumentParser(description="首板盘后深度拆解")
    ap.add_argument("--date", required=True, help="YYYYMMDD 或 YYYY-MM-DD")
    ap.add_argument("--report", action="store_true", help="额外生成 md 报告")
    ap.add_argument("--manual", help="手填覆盖 JSON 路径")
    ap.add_argument("--backfill", action="store_true",
                    help="显式补采当日首板的 1m 特征(需 QMT 在线; 只在显式指定时下载)")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="打印报告到 stdout")
    return ap


def main(argv=None):
    args = build_cli().parse_args(argv)
    manual = None
    if args.manual:
        manual = json.loads(Path(args.manual).read_text(encoding="utf-8"))

    if args.backfill:
        day8 = _iso8(args.date)
        recs0 = collect(day8)
        codes = [r["code"] for r in recs0]
        print("补采 1m 特征: %d 只..." % len(codes), flush=True)
        res = backfill_intraday(day8, codes)
        print("补采结果:", res, flush=True)

    recs, paths = run(args.date, manual=manual, want_report=args.report)
    if args.do_print or not args.report:
        print(render_report(args.date, recs))
    print("首板 %d 只; 落盘: %s" % (len(recs), paths), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
