# -*- coding: utf-8 -*-
"""Shared config & code helpers for the cc-joesph quant stack.

Centralizes values/logic that used to be duplicated across:
  - datasource/factors.py    (_tick_key_for_em_code)
  - strategy_close_pick.py   (with_market_suffix, SECTORS, SIGNAL_ROOT)
  - qmt_signal_bridge_real.py(_with_market_suffix, SIGNAL_ROOT)
  - v04 screen.py (removed 2026-09-15; SECTORS now lives here)

NOTE: keep comments pure ASCII. QMT strategies import this file under a
GBK interpreter; module-level string VALUES may contain CJK (the file is
UTF-8 and declares so), but comments must stay ASCII to avoid confusion.
"""
import logging
import logging.handlers
import os
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- paths
# Root of the signal-file bridge between the web app and QMT.
SIGNAL_ROOT = Path(r"D:/QMT_SIGNALS")

# Default tech-sector pool used for sector mapping (F4/S6) and close-pick.
SECTORS = ["SW1电子", "SW1计算机", "SW1通信"]

# ------------------------------------------------------- runtime paths
# 项目根 = 本文件的上两级(shared/common.py -> shared -> 项目根)。
PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 所有"运行期产出"统一收在 runtime/ 下, 与代码分离, 便于备份与清理:
#   runtime/cache/  可重建的采集缓存
#   runtime/state/  不可重建的账本与状态
#   runtime/log/    组件日志
RUNTIME_DIR = PROJECT_ROOT / "runtime"
CACHE_DIR = RUNTIME_DIR / "cache"
STATE_DIR = RUNTIME_DIR / "state"

# Where component logs go (web app, qmt_sync, close-pick, watchdog).
LOG_DIR = RUNTIME_DIR / "log"

# ---------------------------------------------------------------- logging

def setup_logging(name, log_dir=None, level=logging.INFO):
    """Configure one TimedRotatingFileHandler per component (daily rotation).

    Also mirrors WARNING+ to stderr so a console run still shows problems.
    Idempotent: calling twice for the same logger does not add a second
    handler. Returns the logger."""
    log_dir = Path(log_dir) if log_dir else LOG_DIR
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    if any(isinstance(h, logging.handlers.TimedRotatingFileHandler)
           for h in logger.handlers):
        return logger
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.TimedRotatingFileHandler(
        log_dir / ("%s.log" % name), when="midnight", backupCount=14,
        encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setLevel(logging.WARNING)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger

# ---------------------------------------------------------------- codes
# A-share market suffix conventions.
#   6xxxxx / 5xxxxx / 9xxxxx -> .SH
#   0xxxxx / 3xxxxx / 2xxxxx -> .SZ
#   8xxxxx / 4xxxxx / 92xxxx -> .BJ
# These rules must stay consistent across every component, otherwise an
# order or a tick lookup silently misses (e.g. N3 used to miss forever).

def with_market_suffix(code):
    """Ensure a 6-digit code carries its exchange suffix. Bare -> suffixed;
    already-suffixed values are returned unchanged; unknown -> unchanged."""
    s = str(code).strip()
    if not s or "." in s:
        return s
    if s.startswith("92"):
        return s + ".BJ"                       # 北交所 92 开头(须在 9 之前)
    if s.startswith(("8", "4")):
        return s + ".BJ"
    if s.startswith(("6", "5", "9")):
        return s + ".SH"
    if s.startswith(("0", "3", "2")):
        return s + ".SZ"
    return s


def em_code_to_tick_key(code):
    """Eastmoney bare 6-digit code -> QMT tick-dict key (with suffix).

    Same rule as with_market_suffix but named for its use site: turning
    Eastmoney '000001' into '000001.SZ' so it can be looked up in the
    xtdata full-tick dict (which is keyed with suffixes)."""
    return with_market_suffix(code)


def limit_ratio_for_code(code):
    """Price-limit ratio by board: BJ 30%, ChiNext/STAR 20%, main 10%."""
    c = str(code).strip()
    if c.startswith(("8", "4")):
        return 0.30
    if c.startswith(("300", "301", "688")):
        return 0.20
    return 0.10


# ---------------------------------------------------------------- io / dates
# NOTE: comments here stay ASCII (same rule as the rest of this file).

def atomic_write(path, text):
    """Write text to path atomically: mkdir + same-dir .tmp + fsync + replace.

    A crash mid-write must never leave a half-written ledger/state file
    (readers of paper/live state treat a truncated file as corrupt).
    The fsync is what makes this survive a power loss, not just a process
    crash - tt/ carried the stronger variant, so it won here (2026-09-15)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


def next_weekday(d):
    """Next weekday (Sat/Sun skipped). Accepts date or 'YYYY-MM-DD';
    returns the same type.

    ponytail: no holiday calendar - a midweek holiday still counts as a
    trading day; upgrade path is a real trading calendar."""
    as_str = isinstance(d, str)
    if as_str:
        d = datetime.strptime(d, "%Y-%m-%d").date()
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.isoformat() if as_str else nxt
