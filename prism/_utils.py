# -*- coding: utf-8 -*-
"""因子库共享工具函数(从 datasource/factors.py 迁移, 与旧实现逐行一致)。

Task 5 迁移: F2 用 _parse_timetag_hhmm; Y4/S4 用 ma; F4/S6 用 sector_count。
全部保持旧逻辑不变。东财裸代码 → QMT 键统一走 shared.common.with_market_suffix。
"""
import re
import sys
from datetime import datetime
from pathlib import Path

_ROOT = str(Path(__file__).parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _parse_timetag_hhmm(ts):
    """timetag → (hh, mm)。兼容三种格式:
    - 字符串 "YYYYMMDD HH:MM:SS"(旧版 QMT)
    - 字符串 "YYYY-MM-DD HH:MM:SS"(ISO)
    - 毫秒级 epoch int(新版 xtquant, 见 app.py _fmt_date 注释)
    无法解析 → None(F2 保持 0, 不误判)。"""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        # 13位毫秒 or 10位秒
        try:
            sec = ts / 1000.0 if ts > 1e11 else ts
            dt = datetime.fromtimestamp(sec)
            return (dt.hour, dt.minute)
        except Exception:
            return None
    m = re.search(r"(\d{1,2}):(\d{2})", str(ts))
    if not m:
        return None
    try:
        return (int(m.group(1)), int(m.group(2)))
    except (TypeError, ValueError):
        return None


def ma(series, n):
    """N 日均线(与旧 factors.py _ma 一致)。"""
    return series.rolling(n).mean()


def sector_count(code, sector_map, limit_ups):
    """所属板块内当日涨停家数(与旧 FactorEngine._sector_count 一致)。"""
    sector = sector_map.get(code)
    if not sector:
        return 0
    return sum(1 for c in (limit_ups or [])
               if sector_map.get(c["code"]) == sector)
