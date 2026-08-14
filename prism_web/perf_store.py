# -*- coding: utf-8 -*-
"""绩效追踪: 把每日候选清单存档, 用之后 N 个交易日的实际行情回填涨跌,
统计按等级(A-E)分组的胜率/平均收益, 验证打分体系是否真的有效。

数据流:
  screen 完成 → archive_daily(candidates) 存到 perf/YYYYMMDD.json
  (手动/定时) → backfill(days=5) 用最新行情回填未结算的存档
  查询        → summary() 返回按等级分组的胜率统计

回填需要行情来源(xtdata / 注入假实现), 存档/统计纯本地文件, 可离线测试。
"""
import json
import logging
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 存档根目录: strategy_web/perf/
PERF_DIR = Path(__file__).parent / "perf"


def _date_key(d=None):
    return (d or date.today()).strftime("%Y%m%d")


class PerfStore:
    """候选清单绩效档案。

    archive_path / backfill: 可注入路径便于测试; kline_source: callable(code, days)
    → DataFrame(含 close 列), 默认 None(需外部注入, 否则回填跳过)。"""

    def __init__(self, perf_dir=None, kline_source=None):
        self.perf_dir = Path(perf_dir) if perf_dir else PERF_DIR
        self.kline_source = kline_source
        self.perf_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 存档 ----------------
    def archive_daily(self, candidates, d=None):
        """把当日候选清单落盘(覆盖同一天, 幂等)。返回存档路径。
        candidates: [{code, name, composite, grade, ...}]"""
        key = _date_key(d)
        record = {
            "date": key,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "settled": False,
            "entries": [
                {
                    "code": c.get("code"),
                    "name": c.get("name") or c.get("code"),
                    "composite": c.get("scores", {}).get("composite") if isinstance(
                        c.get("scores"), dict) else None,
                    "grade": c.get("scores", {}).get("grade") if isinstance(
                        c.get("scores"), dict) else None,
                    "entry_close": None,   # 回填时填入选股日收盘价
                    "exit_close": None,    # 回填时填入 N 日后收盘价
                    "return_pct": None,
                }
                for c in candidates
            ],
        }
        path = self.perf_dir / ("%s.json" % key)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        logger.info("绩效存档 %s: %d 只候选", key, len(record["entries"]))
        return path

    # ---------------- 读取 ----------------
    def _load(self, key):
        p = self.perf_dir / ("%s.json" % key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def list_days(self):
        """已存档的日期(升序)。"""
        days = []
        for p in sorted(self.perf_dir.glob("*.json")):
            days.append(p.stem)
        return days

    def get_day(self, key):
        return self._load(key)

    # ---------------- 回填 ----------------
    def backfill(self, days=5, max_days=30):
        """用 kline_source 回填未结算存档的 N 日收益。

        kline_source(code, kdays) 需返回含 'close' 列的 DataFrame(升序, 最后一行=最新)。
        对每个未结算存档: entry_close=选股日收盘, exit_close=N 个交易日后收盘
        (取该股票 K 线中"选股日之后第 N 根"的收盘; 数据不足则跳过该股)。
        返回 {"settled": n, "skipped": m, "errors": [...]}"""
        if self.kline_source is None:
            logger.warning("回填跳过: 未注入 kline_source")
            return {"settled": 0, "skipped": 0, "errors": ["no kline_source"]}
        settled = skipped = 0
        errors = []
        for key in self.list_days():
            rec = self._load(key)
            if rec is None or rec.get("settled"):
                continue
            changed = False
            for e in rec.get("entries", []):
                if e.get("return_pct") is not None:
                    continue
                code = e.get("code")
                try:
                    df = self.kline_source(code, days + 60)
                except Exception as ex:
                    errors.append("%s:%s %r" % (key, code, ex))
                    continue
                if df is None or len(df) < 2 or "close" not in df.columns:
                    skipped += 1
                    continue
                closes = df["close"].tolist()
                # 需要"选股日收盘 + 之后 N 根"才能算 N 日收益; K线不足则跳过
                if len(closes) <= days:
                    skipped += 1
                    continue
                entry_close = closes[-1 - days]   # 选股日(现价起点前 N 根)收盘
                exit_close = closes[-1]           # 最新收盘
                if entry_close and exit_close:
                    e["entry_close"] = round(float(entry_close), 4)
                    e["exit_close"] = round(float(exit_close), 4)
                    e["return_pct"] = round((exit_close / entry_close - 1) * 100, 2)
                    changed = True
                else:
                    skipped += 1
            if changed:
                rec["settled"] = True
                rec["settled_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                rec["horizon_days"] = days
                (self.perf_dir / ("%s.json" % key)).write_text(
                    json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
                settled += 1
        return {"settled": settled, "skipped": skipped, "errors": errors[:20]}

    # ---------------- 统计 ----------------
    def summary(self):
        """按等级分组: 候选数/已结算数/胜率/平均收益。返回按等级排序列表。"""
        stats = {}
        for key in self.list_days():
            rec = self._load(key)
            if not rec:
                continue
            for e in rec.get("entries", []):
                grade = e.get("grade") or "?"
                s = stats.setdefault(grade, {"grade": grade, "total": 0,
                                             "settled": 0, "wins": 0,
                                             "returns": []})
                s["total"] += 1
                rp = e.get("return_pct")
                if rp is not None:
                    s["settled"] += 1
                    s["returns"].append(rp)
                    if rp > 0:
                        s["wins"] += 1
        order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "?": 5}
        result = []
        for s in stats.values():
            n = s["settled"]
            result.append({
                "grade": s["grade"],
                "total": s["total"],
                "settled": n,
                "win_rate": round(s["wins"] / n, 4) if n else None,
                "avg_return": round(sum(s["returns"]) / n, 2) if n else None,
            })
        result.sort(key=lambda x: order.get(x["grade"], 9))
        return result
