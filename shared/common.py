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
import threading
import time
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
    """Price-limit ratio by board: BJ/NEEQ 30%, ChiNext/STAR 20%,
    old-third-board 400/420 5%, main 10%.

    Authority: shared/exit_rules.limit_ratio. The two copies MUST stay in
    sync (guarded by datasource/tests/test_common.py::
    test_limit_ratio_consistent_with_exit_rules); each stays inline so
    neither module depends on the other's import path. 400/420 must be
    tested before the "4" prefix, else the old third board reads as BJ."""
    c = str(code).strip()
    if c.startswith(("400", "420")):        # old third board (delisted) +/-5%
        return 0.05
    if c.startswith(("92", "8", "4")):      # BJ / NEEQ +/-30%
        return 0.30
    if c.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


# ---------------------------------------------------------------- io / dates
# NOTE: comments here stay ASCII (same rule as the rest of this file).

# One lock per target path, NOT one global lock: unrelated files must not
# serialize each other. _WRITE_LOCKS holds one entry per distinct write
# target, a small and bounded set in this project (cache/state/monthly
# feature files), so the dict does not need eviction.
_WRITE_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _write_lock_for(path):
    """Process-local lock serializing same-path writers (M11b).

    Keyed by normalized absolute path so differently spelled paths for
    the same file ('D:/x/a.json' vs 'D:\\x\\a.json') still share one
    lock. Callers here pass absolute paths, so cwd does not shift the
    key between calls."""
    key = os.path.normcase(os.path.abspath(str(path)))
    with _LOCKS_GUARD:
        lock = _WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _WRITE_LOCKS[key] = lock
        return lock


def atomic_write(path, text):
    """Write text to path atomically: mkdir + same-dir .tmp + fsync + replace.

    A crash mid-write must never leave a half-written ledger/state file
    (readers of paper/live state treat a truncated file as corrupt).
    The fsync is what makes this survive a power loss, not just a process
    crash - tt/ carried the stronger variant, so it won here (2026-09-15).

    The tmp name carries pid + thread id (M11, 2026-09-18). A fixed
    `path + ".tmp"` is unsafe once one file has several writers in the
    same process (fundamental cache: daemon snapshot thread + manual CLI
    + live picker): the later writer truncates/steals the earlier one's
    tmp, so the earlier `os.replace` publishes the WRONG text (or raises
    FileNotFoundError). Either way the cache can land corrupt - and
    FundamentalFeed._load_cache silently treats a corrupt file as {}.

    The replace is also serialized PER PATH in-process (M11b,
    2026-09-18). A unique tmp name only stops writers from stealing each
    other's tmp; two threads calling os.replace onto the SAME target at
    the same time still collide on the destination handle and raise
    PermissionError(13) on Windows (measured on the pre-fix code: 119 of
    150 rounds with 4 concurrent writers). The caller then reports "write
    failed" while readers silently fall back to {}. Same treatment as
    prism/zt_history.py::_atomic_pickle.

    The replace additionally retries with bounded backoff (M12,
    2026-09-18, see replace_with_retry): writer/writer serialization does
    NOT help when the failure is a READER. CPython's open() omits
    FILE_SHARE_DELETE on Windows, so ANY thread holding the target open
    (e.g. _load_cache / qmt_zt_feed reading) makes os.replace fail with
    EACCES until that handle closes - one writer plus one reader is
    enough (measured pre-fix: 15 of 300 rounds with the polling reader of
    test_atomic_write_survives_concurrent_reader, 262 of 300 with two
    tight-spinning readers).

    Guarantee, exactly: inside this process, writers of the same path run
    their replace one after another, so the file always holds the
    complete text of one write (never a half or a mix), and a reader that
    lets go of its handle within the retry budget does not turn the write
    into a failure. It does NOT coordinate across processes - that case
    still rests on the unique pid-carrying tmp name plus os.replace being
    atomic per target. A reader holding the handle for longer than the
    whole budget still sees the original PermissionError; that is
    reported, not hidden.

    A failed publish does NOT leave the tmp behind: the pid+tid name is
    unique, so nothing would ever reuse or clean that file (M13,
    2026-09-18). Same treatment as tt_solo/ttcore/_vendor.py."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path("%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident()))
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    with _write_lock_for(path):
        try:
            replace_with_retry(tmp, path)
        except BaseException:
            _unlink_quietly(tmp)
            raise


def _unlink_quietly(tmp):
    """Best-effort removal of a tmp that failed to publish (never raises)."""
    try:
        os.unlink(tmp)
    except OSError:
        pass


# -------------------------------------------------- os.replace retry (M12)
# Windows: os.replace() onto a destination that is currently open by ANY
# reader fails with ERROR_ACCESS_DENIED (winerror 5) or
# ERROR_SHARING_VIOLATION (winerror 32), surfacing as PermissionError(13)
# "Access is denied". Python's open() opens files without FILE_SHARE_DELETE,
# so the rename cannot delete the old destination until every reader closes
# it. This is NOT a writer/writer race (a per-path write lock cannot fix it)
# and NOT something the writer can force - the reader just has to finish.
# So: retry a bounded number of times with backoff, then re-raise the
# ORIGINAL exception. Honest ceiling: this only widens the window, it does
# not make replace safe against a reader that holds the handle longer than
# the whole budget (~0.95s).
_REPLACE_ATTEMPTS = 10
_REPLACE_DELAY0 = 0.01
_REPLACE_DELAY_MAX = 0.16


def _is_replace_retryable(exc):
    """True for the Windows errors that mean "the destination is in use"."""
    if isinstance(exc, PermissionError):
        return True
    return (isinstance(exc, OSError)
            and getattr(exc, "winerror", None) in (5, 32))


def replace_with_retry(src, dst):
    """os.replace(src, dst) with bounded backoff on Windows sharing errors.

    Only the retryable "destination held open" errors are retried. Once the
    budget is spent the original exception propagates: a write that truly
    failed must never look like a success (callers cache it as {} / report
    "saved" otherwise, and the data is silently gone).

    Total worst-case sleep: 0.01+0.02+0.04+0.08+0.16*5 = 0.95s over 9
    retries (10 attempts)."""
    delay = _REPLACE_DELAY0
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if attempt == _REPLACE_ATTEMPTS - 1 or not _is_replace_retryable(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, _REPLACE_DELAY_MAX)


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


# ---------------------------------------------------------------- guard
def _is_rfc1918(ip):
    """RFC1918 private ranges (LAN = trusted local tier)."""
    if ip.startswith(("10.", "192.168.")):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def is_local_request(cf_ip, remote_addr):
    """Tiered-write guard predicate (spec 2026-09-15-tiered-guard).

    CF-Connecting-IP present  -> public tunnel origin -> NOT local.
    No CF header and loopback/RFC1918 remote  -> local machine.
    Future: switch to CF Access email allowlist by editing this only."""
    if cf_ip:
        return False
    ra = remote_addr or ""
    return ra in ("127.0.0.1", "::1") or _is_rfc1918(ra)
