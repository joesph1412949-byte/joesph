# -*- coding: utf-8 -*-
"""因子体检: 注册/签名/假数据跑通 + 数据齐全场景下的命中率。

用法: python -m prism.factor_check

两层检查:
1. 生存检查 —— 全 None 上下文跑一遍, 验证因子不崩溃、返回含 score。
   (注意: 这层只能证明"不崩", 36 个因子全 PASS 且全 0 也照样通过 ——
    它区分不了"数据缺失导致恒 0"与"真的不命中"。)
2. 命中率检查 —— 构造多个数据齐全的合成场景(首板封板/放量突破/
   均线多头/妖股/市场门控), 每个因子逐场景跑。数据齐全仍恒 0 的因子
   会被标 ZERO-HIT, 需人工判断是"形态苛刻"还是"逻辑坏/字段对不上"。

合成场景与因子是一一对应的契约: 改因子取数字段时若体检报 ZERO-HIT,
先怀疑场景没喂对字段, 再怀疑因子逻辑。
"""
import inspect
import sys

import pandas as pd


# ---------------------------------------------------------------- 合成场景
def _kdf(closes, vols):
    """close/volume 序列 → 合成日K DataFrame(open/high/low 由 close 推导)。"""
    opens = [c * 0.995 for c in closes]
    highs = [max(o, c) * 1.005 for o, c in zip(opens, closes)]
    lows = [min(o, c) * 0.995 for o, c in zip(opens, closes)]
    idx = ["D%04d" % i for i in range(len(closes))]
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": vols}, index=idx)


def _base_extra():
    """mkt 快照合成段。

    设计对齐因子阈值: 板块日涨 0.8%(10 日 >5% 供 SEC2)、amount 恒定
    (近5日/240日比值=1 ≤2 供 SEC4, 且需 ≥241 点)、纳指日涨 1.2%
    (≥1% 供 N6)、美债/VIX 平稳(供 N7/N8)、板块资金流为正(供 SEC3)。
    """
    n = 250
    dates = ["D%03d" % i for i in range(n)]
    return {"mkt": {
        "sector": {"801010": {
            "dates": dates,
            "close": [round(100 * (1.008 ** i), 2) for i in range(n)],
            "amount": [1e8] * n}},
        "sector_flow": {"801010": {
            "dates": dates,
            "main_net_in": [1e6 + i * 1e5 for i in range(n)]}},
        "global": {
            "NDX": {"dates": ["G%03d" % i for i in range(30)],
                    "close": [round(20000 * (1.012 ** i), 2)
                              for i in range(30)]},
            "US10Y": {"dates": ["G%03d" % i for i in range(30)],
                      "close": [4.2 + (i % 3) * 0.01 for i in range(30)]},
            "VIX": {"dates": ["G%03d" % i for i in range(30)],
                    "close": [15.0] * 30}},
        # futures 键 = 申万行业代码(F8: futs.get(sector_map[code])),
        # 不是品种代码 —— 用错键会永远命中"板块无期货映射"
        "futures": {"801950": {"commodities": {"JM0": {
            "dates": ["F%03d" % i for i in range(30)],
            # 日涨 5 -> 20 日涨幅约 9.9%, 过 F8 的 +3% 阈值
            "close": [1000 + i * 5 for i in range(30)]}}}},
        "zt_prev": {"codes": ["600000.SH"]},
    }}


_FUND_OK = lambda: {"score": 1, "note": "合成数据"}  # noqa: E731


def _scn_first_board():
    """首板封板: 250 日横盘 10 元, 今日涨停 11 元巨量封单。"""
    from prism.context import FactorContext
    closes = [10.0 * (1 + (i % 7 - 3) * 0.002) for i in range(259)] + [11.0]
    vols = [1000000.0] * 259 + [3000000.0]
    tick = {"lastPrice": 11.0, "lastClose": 10.0,
            "askPrice": [0.0] * 5, "askVol": [0] * 5,
            "bidPrice": [11.0] * 5, "bidVol": [500000] * 5,
            "timetag": "20260905 09:31:00", "amount": 3.3e8,
            "volume": 3000000.0}
    ctx = FactorContext(
        code="600000.SH", kline=_kdf(closes, vols), tick=tick,
        index_kline=_kdf([3800 + i * 5 for i in range(30)],
                         [1e8] * 30),
        float_mv=2e9, float_vol=2e8, last=11.0, last_close=10.0,
        up_price=11.0, sealed=True,
        sector_map={"600000.SH": "801010"},
        limit_ups=[{"code": "600000.SH", "sealed": True, "last": 11.0,
                    "last_close": 10.0}] * 60,
        em={"daily_counts": [40, 45, 50, 55, 60],
            "max_boards": 5, "yesterday_codes": ["600000.SH"],
            "yesterday_boards": [3, 2, 2, 1]},
        fund={k: _FUND_OK() for k in ("Y1", "Y2", "Y5", "Y6", "Y7",
                                            "Y8", "F7")},
        **_base_extra())
    ctx._extra["sh_index_kline"] = _kdf(
        [3800 + i * 5 for i in range(30)], [1e8] * 30)
    return ctx


def _scn_volume_breakout():
    """放量突破: 60 日缩量横盘, 今日 3 倍量创新高。"""
    from prism.context import FactorContext
    closes = [10.0 + (i % 5 - 2) * 0.01 for i in range(259)] + [10.8]
    vols = [800000.0] * 260
    # S2 需 60 日内 >=20 天量 > ma60*1.5(放量段计入 ma60 后阈值约 2.2e6,
    # 3e6 仍过); F5 需最后 20 日内 >=5 天 > ma5*1.5(放量段避开最后 5 日)
    vols[200:215] = [3_000_000.0] * 15
    vols[240:245] = [3_000_000.0] * 5
    ctx = FactorContext(
        code="600000.SH", kline=_kdf(closes, vols),
        float_mv=2e9, float_vol=2e8, last=10.8, last_close=10.0,
        sector_map={"600000.SH": "801010"}, limit_ups=[], em={},
        fund={}, **_base_extra())
    return ctx


def _scn_ma_bull():
    """均线多头: 260 根匀速上涨 + 温和放量。"""
    from prism.context import FactorContext
    closes = [5.0 * (1 + i * 0.005) for i in range(260)]
    vols = [1e6 * (1 + i * 0.002) for i in range(260)]
    ctx = FactorContext(
        code="600000.SH", kline=_kdf(closes, vols),
        float_mv=2e9, float_vol=2e8, last=closes[-1],
        sector_map={"600000.SH": "801010"}, limit_ups=[], em={},
        fund={}, **_base_extra())
    return ctx


def _scn_market_gate():
    """市场门控: 涨停池 60 家、连板梯队、指数站上均线。"""
    from prism.context import FactorContext
    pool = [{"code": "600%03d.SH" % i, "sealed": True, "last": 10.0,
             "last_close": 9.1} for i in range(60)]
    idx = _kdf([3800 + i * 8 for i in range(30)], [1e8] * 30)
    ctx = FactorContext(
        code="__MARKET__", limit_ups=pool,
        em={"daily_counts": [35, 42, 48, 55, 60], "max_boards": 6,
            "yesterday_codes": ["600001.SH", "600002.SH"],
            "yesterday_boards": [4, 3, 2]},
        index_kline=idx,
        # N5 两市成交额阈值 2 万亿: 60 只 x 4e10 = 2.4e12
        ticks={"600%03d.SH" % i: {"amount": 4e10} for i in range(60)},
        **_base_extra())
    ctx._extra["sh_index_kline"] = idx
    return ctx


def _scn_monster():
    """妖股: 小市值 + 筹码/游资/概念齐全。"""
    from prism.context import FactorContext
    closes = [10.0 * (1 + (i % 6 - 2) * 0.003) for i in range(259)] + [11.0]
    vols = [1000000.0] * 259 + [3500000.0]
    ctx = FactorContext(
        code="300001.SZ", kline=_kdf(closes, vols),
        float_mv=2e9, float_vol=2e8, last=11.0, last_close=10.0,
        up_price=11.0, sealed=True,
        sector_map={"300001.SZ": "801950"},
        limit_ups=[{"code": "300001.SZ", "sealed": True, "last": 11.0,
                    "last_close": 10.0}] * 60,
        em={"daily_counts": [40, 45, 50, 55, 60]},
        fund={k: _FUND_OK() for k in ("Y1", "Y2", "Y3", "Y5", "Y6",
                                            "Y7", "Y8", "F7")},
        **_base_extra())
    return ctx


def _rich_scenarios():
    """场景名 → 上下文。每个因子逐场景跑, 任一非零即算命中。"""
    return [
        ("首板封板", _scn_first_board),
        ("放量突破", _scn_volume_breakout),
        ("均线多头", _scn_ma_bull),
        ("市场门控", _scn_market_gate),
        ("妖股", _scn_monster),
    ]


# ---------------------------------------------------------------- 检查
def _fake_context():
    """给因子一个最小假上下文(全部 None), 验证不崩溃。"""
    from prism.context import FactorContext
    return FactorContext(code="600000.SH")


def run_checks(scan=True, rich=True):
    """返回 [(factor_id, ok, message)]。

    ok=False 仅表示"崩溃/签名坏/缺 score"。
    rich=True 时 message 附带合成场景命中情况(全 0 会标 ZERO-HIT,
    但不计为失败 —— 形态苛刻的因子合法地可能全场景不命中)。
    """
    from prism import registry as reg
    if scan:
        reg.reset()
        # force=True: 进程可能已 import 过 prism.factors, 无 force 时 importlib
        # 一次性语义使重扫为 no-op → FACTORS 空 → 静默假阴性
        reg.scan_factors("prism.factors", force=True)
    scenarios = []
    if rich:
        for name, fn in _rich_scenarios():
            try:
                scenarios.append((name, fn()))
            except Exception as e:
                print("警告: 合成场景 %s 构造失败: %r" % (name, e))
    out = []
    for fid, meta in sorted(reg.FACTORS.items()):
        func = meta["func"]
        try:
            params = inspect.signature(func).parameters
            if len(params) < 1:
                out.append((fid, False, "签名错误: compute 缺 ctx 参数"))
                continue
            res = func(_fake_context())
            if not isinstance(res, dict) or "score" not in res:
                out.append((fid, False, "返回值缺少 score: %r" % (res,)))
                continue
            msg = "OK score=%r" % res["score"]
            if scenarios:
                hits = []
                for name, ctx in scenarios:
                    try:
                        r = func(ctx)
                        if isinstance(r, dict) and r.get("score"):
                            hits.append("%s(%s)" % (name, r["score"]))
                    except Exception as e:
                        hits.append("%s(异常 %s)" % (name, type(e).__name__))
                if hits:
                    msg += " | rich命中: " + ", ".join(hits)
                else:
                    msg += " | ZERO-HIT: %d 个数据齐全场景均未命中" % len(
                        scenarios)
            out.append((fid, True, msg))
        except Exception as e:
            out.append((fid, False, "运行异常: %r" % e))
    return out


def main():
    results = run_checks()
    bad = [r for r in results if not r[1]]
    zero = [r for r in results if r[1] and "ZERO-HIT" in r[2]]
    for fid, ok, msg in results:
        print("[%s] %s: %s" % ("PASS" if ok else "FAIL", fid, msg))
    if bad:
        print("\n%d 个因子体检失败!" % len(bad))
    if zero:
        print("\n%d 个因子 ZERO-HIT(数据齐全仍恒 0, 需人工判断形态苛刻还是逻辑坏):"
              % len(zero))
        for fid, _ok, _msg in zero:
            print("  - %s" % fid)
    if bad:
        return 1
    print("\n全部 %d 个因子体检通过(其中 %d 个 ZERO-HIT 待人工确认)。"
          % (len(results), len(zero)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
