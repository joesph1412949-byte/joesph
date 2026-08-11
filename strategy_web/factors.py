# -*- coding: utf-8 -*-
"""因子引擎：计算 24 个量化因子中能用 xtdata 数据自动算的部分。
自动算的 16 个：F1 F2 F3 F4 F5 F6 Y3 Y4 S2 S3 S4 S6 N1 N2 N4 N5
手填的 8 个：F7 Y1 Y2 Y5 Y6 Y7 S1 S5 S7（在 manual_store.py / 网页手填）"""
import numpy as np


class FactorComputeError(Exception):
    pass


def _ma(series, n):
    return series.rolling(n).mean()


class FactorEngine:
    def _sector_count(self, code, sector_map, limit_ups):
        """所属板块内当日涨停家数"""
        sector = sector_map.get(code)
        if not sector:
            return 0
        return sum(1 for c in (limit_ups or [])
                   if sector_map.get(c["code"]) == sector)

    def compute_factors(self, code, tick, detail, ds, sector_map,
                        limit_ups=None, market=None):
        """计算单只股票的自动因子。返回 {因子名: {"score":0/1, "note":str}}"""
        last = tick.get("lastPrice") or 0
        last_close = tick.get("lastClose") or 0
        up_price = detail.get("UpStopPrice") or 0
        float_vol = detail.get("FloatVolume") or 0
        sealed = bool((tick.get("askPrice") or [0])[0] == 0)

        try:
            kline = ds.get_kline(code, days=250)
        except Exception:
            kline = None

        out = {}

        # ---- F1 首板确认 ----
        f1 = 0
        if kline is not None and up_price > 0:
            closes = kline["close"].tolist()
            ratio = (0.30 if code.startswith(("8", "4")) else
                     0.20 if code.startswith(("300", "301", "688")) else 0.10)
            # 历史每日是否涨停: 当日收盘 >= 基于"前一日收盘"算的涨停价 (排除今日)。
            # (原实现对当日 close 自身算涨停价, 对正常正价格恒不成立, 使"近历史有涨停"分支成死代码;
            #  终审修复: 涨停价应基于前一日收盘)
            prev_limit = False
            for i in range(1, len(closes)):
                limit_px = round(closes[i - 1] * (1 + ratio), 2)
                if closes[i] >= limit_px - 0.01:
                    prev_limit = True
                    break
            f1 = 1 if (not prev_limit and last >= up_price - 0.01) else 0
        out["F1"] = {"score": f1, "note": "近20日无涨停且今日首次涨停" if f1 else "近20日已有涨停或今日未涨停"}

        # ---- F2 早封板 ----
        f2 = 0
        if sealed:
            ts = tick.get("timetag") or ""
            # timetag 形如 "20260811 14:05:06"
            try:
                hh = int(ts.split(" ")[1].split(":")[0])
                mm = int(ts.split(" ")[1].split(":")[1])
                f2 = 1 if (hh, mm) <= (10, 0) else 0
            except Exception:
                f2 = 0
        out["F2"] = {"score": f2, "note": "封板时间≤10:00" if f2 else "未封板或封板时间晚于10:00"}

        # ---- F3 封单强度 ----
        f3 = 0
        if sealed and up_price > 0 and float_vol > 0:
            bid0 = (tick.get("bidPrice") or [0])[0]
            bidv0 = (tick.get("bidVol") or [0])[0]
            seal_amount = bid0 * bidv0
            float_mv = up_price * float_vol
            ratio = 0.005 if not code.startswith(("300","301","688")) else 0.002
            f3 = 1 if seal_amount >= float_mv * ratio else 0
        out["F3"] = {"score": f3, "note": "封单≥流通市值0.5%(主板)/0.2%(创业科创)" if f3 else "封单不足"}

        # ---- F4 板块共振 ----
        f4 = 1 if self._sector_count(code, sector_map, limit_ups) >= 3 else 0
        out["F4"] = {"score": f4, "note": "板块涨停≥3家" if f4 else "板块共振不足"}

        # ---- F5 量价堆积 ----
        f5 = 0
        if kline is not None and len(kline) >= 20:
            vols = kline["volume"].tolist()[-20:]
            ma5 = sum(vols[-5:]) / 5
            days = sum(1 for v in vols if v > ma5 * 1.5)
            f5 = 1 if days >= 5 else 0
        out["F5"] = {"score": f5, "note": "20日内≥5天量>5日均量×1.5" if f5 else "量价堆积不足"}

        # ---- F6 大盘配合 ----
        f6 = 0
        try:
            idx = ds.get_index_kline("000001.SH", days=30)
            if idx is not None and len(idx) >= 21:
                closes = idx["close"].tolist()
                ma20 = sum(closes[-20:]) / 20
                above = closes[-1] > ma20
                # 连续两日放量上涨
                chg = [closes[i] / closes[i-1] - 1 for i in range(-2, 0)]
                vols = idx["volume"].tolist()[-3:]
                up2 = chg[0] > 0 and chg[1] > 0 and vols[-1] > vols[-3]
                f6 = 1 if (above or up2) else 0
        except Exception:
            f6 = 0
        out["F6"] = {"score": f6, "note": "上证站上20日均线或连两日放量涨" if f6 else "大盘环境一般"}

        # ---- Y3 倍量突破 ----
        y3 = 0
        if kline is not None and len(kline) >= 6:
            vols = kline["volume"].tolist()
            today = vols[-1]
            ma5_prev = sum(vols[-6:-1]) / 5
            y3 = 1 if today >= ma5_prev * 3 else 0
        out["Y3"] = {"score": y3, "note": "涨停日量≥前5日均量×3" if y3 else "量能未达3倍"}

        # ---- Y4 均线多头 ----
        y4 = 0
        if kline is not None and len(kline) >= 60:
            closes = kline["close"]
            ma5 = _ma(closes, 5).iloc[-1]
            ma10 = _ma(closes, 10).iloc[-1]
            ma20 = _ma(closes, 20).iloc[-1]
            ma60 = _ma(closes, 60).iloc[-1]
            if ma5 > ma10 > ma20 and closes.iloc[-1] > ma60:
                y4 = 1
        out["Y4"] = {"score": y4, "note": "5/10/20多头排列且站上60日线" if y4 else "均线未多头"}

        # ---- S2 量价堆积密度 ----
        s2 = 0
        if kline is not None and len(kline) >= 60:
            vols = kline["volume"].tolist()[-60:]
            closes = kline["close"].tolist()[-60:]
            ma60 = sum(vols) / 60
            big = sum(1 for v in vols if v > ma60 * 1.5)
            hi, lo = max(closes), min(closes)
            narrow = (hi - lo) / lo <= 0.10 if lo > 0 else False
            s2 = 1 if (big >= 20 and narrow) else 0
        out["S2"] = {"score": s2, "note": "60日≥20天放量且价格窄幅震荡" if s2 else "量价密度不足"}

        # ---- S3 最小阻力突破 ----
        s3 = 0
        if kline is not None and len(kline) >= 120:
            closes = kline["close"].tolist()
            vols = kline["volume"].tolist()
            ma60 = sum(closes[-60:]) / 60
            ma120 = sum(closes[-120:]) / 120
            ma60v = sum(vols[-60:]) / 60
            today_v = vols[-1]
            broke = closes[-1] > ma60 and closes[-1] > ma120
            volup = today_v > ma60v * 1.5
            s3 = 1 if (broke and volup) else 0
        out["S3"] = {"score": s3, "note": "放量突破60/120日均线" if s3 else "未突破长期均线"}

        # ---- S4 均线系统 ----
        s4 = 0
        if kline is not None and len(kline) >= 250:
            closes = kline["close"]
            ma60 = _ma(closes, 60).iloc[-1]
            ma120 = _ma(closes, 120).iloc[-1]
            ma250 = _ma(closes, 250).iloc[-1]
            slope_up = closes.iloc[-1] > closes.iloc[-6]
            if ma60 > ma120 > ma250 and slope_up:
                s4 = 1
        out["S4"] = {"score": s4, "note": "60/120/250多头排列且斜率向上" if s4 else "长均线未多头"}

        # ---- S6 板块共振强度 ----
        s6 = 0
        if self._sector_count(code, sector_map, limit_ups) >= 3:
            s6 = 1
        out["S6"] = {"score": s6, "note": "板块≥3只走强" if s6 else "板块内同步走强不足"}

        return out

    def compute_market_factors(self, ds, ticks, limit_ups=None, em=None):
        """计算市场环境因子 N1-N5。
        em: 东财真数据 {"daily_counts":[c1..c5], "yesterday_codes":[...], "max_boards":N} 或 None。
        N1/N3/N4 优先用 em 真数据(东财涨停池); em 缺失/不可用 → 回退替代算法(兜底)。
        limit_ups: get_limit_up_stocks 的返回(含 code/last_close)"""
        limit_ups = limit_ups or []
        em = em or {}

        # ---- N5 两市成交额 ----
        total = sum((t.get("amount") or 0) for t in ticks.values())
        n5 = 1 if total >= 2e12 else 0
        n5_note = "两市成交额 %.0f 亿 >= 2万亿" % (total / 1e8) if n5 else \
                  "两市成交额 %.0f 亿 < 2万亿" % (total / 1e8)

        # ---- N1 涨停指数 ----
        # 真数据(东财): 今日涨停家数 vs 近5日均值; 兜底: 880368 指数, 再无则今日有涨停即1
        n1 = 0
        daily_counts = em.get("daily_counts") or []
        if len(daily_counts) >= 3:
            mean5 = sum(daily_counts) / len(daily_counts)
            n1 = 1 if len(limit_ups) > mean5 else 0
            n1_note = "今日涨停 %d > 近5日均值 %.1f (东财真数据)" % (len(limit_ups), mean5) if n1 else \
                      "今日涨停 %d <= 近5日均值 %.1f (东财真数据)" % (len(limit_ups), mean5)
        else:
            try:
                idx = ds.get_index_kline("880368.SH", days=8)
                if idx is not None and len(idx) >= 6:
                    vals = idx["close"].tolist()
                    n1 = 1 if vals[-1] > sum(vals[-6:-1]) / 5 else 0
                    n1_note = "涨停指数在5日线上方(兜底)" if n1 else "涨停指数走弱(兜底)"
                else:
                    n1 = 1 if len(limit_ups) > 0 else 0  # 兜底
                    n1_note = "今日涨停 %d 家(兜底)" % len(limit_ups)
            except Exception:
                n1 = 1 if len(limit_ups) > 0 else 0
                n1_note = "今日涨停 %d 家(兜底)" % len(limit_ups)

        # ---- N3 首板溢价 ----
        # 真数据(东财): 昨日涨停股今日平均涨幅>0; 兜底: 今日涨停池均价
        n3 = 0
        avg_chg = 0.0
        yesterday_codes = em.get("yesterday_codes") or []
        if yesterday_codes:
            chgs = []
            for code in yesterday_codes:
                t = ticks.get(code)
                if not t:
                    continue
                last = t.get("lastPrice") or 0
                last_close = t.get("lastClose") or 0
                if last > 0 and last_close > 0:
                    chgs.append((last / last_close - 1) * 100)
            if chgs:
                avg_chg = sum(chgs) / len(chgs)
                n3 = 1 if avg_chg > 0 else 0
            n3_note = "昨日涨停股今日均涨 %.2f%% (东财真数据)" % avg_chg if chgs else \
                      "昨日涨停股无今日行情 (东财真数据)"
        else:
            chgs = []
            try:
                for lu in limit_ups:
                    lc = lu.get("last_close") or 0
                    last = lu.get("last") or 0
                    if lc > 0:
                        chgs.append((last / lc - 1) * 100)
                if chgs:
                    avg_chg = sum(chgs) / len(chgs)
                    n3 = 1 if avg_chg > 0 else 0
            except Exception:
                n3 = 0
            n3_note = "首板股今日均涨 %.2f%% (兜底)" % avg_chg if chgs else "首板溢价为负(兜底)"

        # ---- N2 情绪周期 ----
        n2 = 0
        n2_note = "情绪周期判断需连板数据, 简化以涨停家数近似"
        if len(limit_ups) >= 50:
            n2 = 1
        elif len(limit_ups) >= 20:
            n2 = 1
        n2_note = "涨停家数 %d, 情绪偏暖" % len(limit_ups) if n2 else \
                  "涨停家数 %d, 情绪偏冷" % len(limit_ups)

        # ---- N4 连板高度 ----
        # 真数据(东财): 规格规则为"最高连板≥5 或 晋级率>25%"。em 只携带 max_boards
        # (东财涨停池无晋级率数据), 晋级率>25% 子句无法从 em 计算, 故阈值仅用最高连板≥5。
        n4 = 0
        max_boards = em.get("max_boards") if em else None
        if max_boards is not None:
            n4 = 1 if max_boards >= 5 else 0
            n4_note = "最高连板 %d 板 >= 5 (东财真数据)" % max_boards if n4 else \
                      "最高连板 %d 板 < 5 (东财真数据)" % max_boards
        else:
            n4_note = "连板数据需历史K线, 简化以涨停家数+封板率近似"
            if len(limit_ups) >= 30:
                sealed_cnt = sum(1 for lu in limit_ups if lu.get("sealed"))
                if len(limit_ups) > 0 and sealed_cnt / len(limit_ups) > 0.6:
                    n4 = 1
            n4_note = "涨停家数 %d 且封板率>60%% (兜底)" % len(limit_ups) if n4 else "连板高度不足(兜底)"

        return {
            "N1": {"score": n1, "note": n1_note},
            "N2": {"score": n2, "note": n2_note},
            "N3": {"score": n3, "note": n3_note},
            "N4": {"score": n4, "note": n4_note},
            "N5": {"score": n5, "note": n5_note},
        }
