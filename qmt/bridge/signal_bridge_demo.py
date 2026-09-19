#encoding:gbk
# QMT signal bridge - ASCII only (no CJK to avoid QMT GBK encoding issues)
# ============================================================
#  SIMULATION bridge. NOT for real trading.
#  It has NO per-day dedup (HAS_DAILY_DEDUP = False) and NO paused gate, so
#  ENVIRONMENT = "real" is REFUSED at startup by _check_safety() (execution
#  constraint, decided 2026-09-19) -- real orders go through
#  qmt/bridge/signal_bridge_real.py, which carries the full gate chain
#  (paused / dated armed file / per-day dedup / same-round sell guard /
#  price-volume sanity / optional account whitelist).
#  It also does not validate the account -- it orders on whatever account the
#  current QMT terminal is logged into.
# ============================================================
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime

# ---------------- config ----------------
SIGNAL_ROOT = r"D:/QMT_SIGNALS"

# env: "sim" = simulation (safe), "real" = live (must arm + DRY_RUN=False)
ENVIRONMENT = "sim"

# This bridge deliberately has NO per-day dedup (see the header). Kept as an
# explicit constant so the flag is machine-checkable instead of comment-only.
HAS_DAILY_DEDUP = False

# live arm file: only when this exists (dated today) does real mode allow
# running/ordering
ARM_FILE = os.path.join(SIGNAL_ROOT, ".REAL_ARMED")

# DRY_RUN=True: log only, never call passorder. real mode forbids True.
DRY_RUN = False

SCAN_INTERVAL = 2
FILE_MIN_AGE = 1.0
KEEP_RESULT_DAYS = 7
ADD_MARKET_SUFFIX = True

# ---------------- env dirs ----------------
PENDING_DIR = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "pending")
DONE_DIR = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "done")
FAILED_DIR = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "failed")
TRADES_DIR = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "trades")

# action -> passorder opType (0 buy 1 sell)
ACTION_TO_OP = {
    "BUY": 0,
    "SELL": 1,
}

# ---------------- signal queue (scanner thread -> main thread) ----------------
_signal_queue = []
_queue_lock = threading.Lock()
_queued_files = set()

# ---------------- safety ----------------
def _is_armed():
    """Check the arm file. Real mode runs ONLY while an arm file exists whose
    content contains today's YYYYMMDD.
    Same rule as signal_bridge_real.py _is_armed() -- a stale arm file must NOT
    keep the live channel open forever.
    Returns (ok: bool, message: str)."""
    try:
        if not os.path.exists(ARM_FILE):
            return False, "armed file missing: %s" % ARM_FILE
        with open(ARM_FILE, "r", encoding="utf-8") as fp:
            content = (fp.read() or "").strip()
        today = datetime.now().strftime("%Y%m%d")
        if today not in content:
            return False, "armed file not dated today (%s): %s" % (today, ARM_FILE)
        return True, "armed"
    except Exception as e:
        return False, "armed file check error %r" % e

def _check_safety():
    if ENVIRONMENT == "real":
        # Execution constraint, not a comment: this bridge has NO per-day dedup
        # (HAS_DAILY_DEDUP = False) and NO paused gate, so real orders must never
        # go through it. Refuse to start and point at the real bridge. The arm
        # file state is still reported so the operator sees what it would have
        # been (e.g. a stale arm file).
        _armed, armed_msg = _is_armed()
        return False, ("real mode REFUSED: this bridge has no per-day dedup and "
                       "no paused gate -> use qmt/bridge/signal_bridge_real.py "
                       "for real orders (arm file state: %s)" % armed_msg)
    return True, "simulation mode (safe)"

# ---------------- utils ----------------
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _ensure_dirs():
    for d in (PENDING_DIR, DONE_DIR, FAILED_DIR, TRADES_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass

def _get_attr(obj, *names, default=""):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default

def _safe_unlink(f):
    try:
        os.remove(f)
    except OSError:
        pass

def _with_market_suffix(code):
    # Keep in sync with signal_bridge_real.py / shared/common.py
    # with_market_suffix. This file stays self-contained on purpose: QMT runs it
    # as a pasted strategy where project-root imports are not available.
    # BJ 92xx MUST be matched before the "9" -> .SH branch.
    code = str(code).strip()
    if "." in code:
        return code
    if code.startswith("92"):
        return code + ".BJ"                    # BJ 92xx (before the "9" branch)
    if code.startswith(("8", "4")):
        return code + ".BJ"
    if code.startswith(("6", "5", "9")):
        return code + ".SH"
    if code.startswith(("0", "3", "2")):
        return code + ".SZ"
    return code

def _list_json_files(d):
    try:
        names = os.listdir(d)
    except OSError:
        return []
    files = []
    for n in names:
        if n.endswith(".json"):
            files.append(os.path.join(d, n))
    files.sort()
    return files

def _read_json(f):
    with open(f, "r", encoding="utf-8") as fp:
        return json.load(fp)

def _write_json(dest, data):
    with open(dest, "w", encoding="utf-8") as fp:
        fp.write(json.dumps(data, ensure_ascii=False, indent=2))

def _write_result(dest_dir, sig, success, detail):
    key = str(sig.get("order_id", "unknown"))
    safe = "".join(c for c in key if c.isalnum() or c in "-_") or "unknown"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result = {
        "order_id": sig.get("order_id"),
        "stock_code": sig.get("stock_code"),
        "action": sig.get("action"),
        "price": sig.get("price"),
        "volume": sig.get("volume"),
        "account_id": sig.get("account_id"),
        "processed_at": _now(),
        "success": success,
        "detail": detail,
    }
    _write_json(os.path.join(dest_dir, "%s_%s.json" % (stamp, safe)), result)

def _write_trade(trd, status):
    _ensure_dirs()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    rec = {
        "status": status,
        "account_id": _get_attr(trd, "m_accountID", "accountID", "account_id", default=""),
        "order_id": _get_attr(trd, "m_entrustNo", "entrustNo", "order_id", default=""),
        "stock_code": _get_attr(trd, "m_stockCode", "stockCode", "stock_code", default=""),
        "order_volume": _get_attr(trd, "m_orderVolume", "orderVolume", default=""),
        "traded_volume": _get_attr(trd, "m_tradedVolume", "tradedVolume", default=""),
        "price": _get_attr(trd, "m_price", "price", default=""),
        "traded_price": _get_attr(trd, "m_tradedPrice", "tradedPrice", default=""),
        "order_type": _get_attr(trd, "m_orderType", "orderType", default=""),
        "occur_time": _get_attr(trd, "m_occurTime", "occurTime", default=""),
        "received_at": _now(),
    }
    name = "%s_%s_%s.json" % (stamp, status, rec["order_id"] or rec["stock_code"] or "trade")
    _write_json(os.path.join(TRADES_DIR, name), rec)

# ---------------- trade/order callbacks ----------------
def on_trade(ContextInfo, account, trade):
    try:
        _write_trade(trade, "TRADE")
        print("[SignalBridge] TRADE %s %s @ %s" % (
            _get_attr(trade, "m_stockCode", "stockCode", "stock_code", default="?"),
            _get_attr(trade, "m_tradedVolume", "tradedVolume", default="?"),
            _get_attr(trade, "m_tradedPrice", "tradedPrice", default="?")), flush=True)
    except Exception as e:
        print("[SignalBridge] on_trade ERR %r" % e, flush=True)

def on_order(ContextInfo, account, order):
    try:
        _write_trade(order, "ORDER")
    except Exception as e:
        print("[SignalBridge] on_order ERR %r" % e, flush=True)

# ---------------- core: place order (main thread only) ----------------
def _call_passorder(sig, ctx=None):
    stock_code = sig.get("stock_code")
    account_id = str(sig.get("account_id", "")).strip()
    action = str(sig.get("action") or sig.get("order_type") or "").upper()
    try:
        price = float(sig.get("price") or 0)
        volume = int(sig.get("volume") or 0)
    except (TypeError, ValueError):
        return False, "price/volume invalid"

    if not stock_code:
        return False, "missing stock_code"
    if volume <= 0:
        return False, "invalid volume %r" % volume
    if not account_id:
        return False, "missing account_id"
    if action not in ACTION_TO_OP:
        return False, "unknown action %r" % action

    code = _with_market_suffix(stock_code) if ADD_MARKET_SUFFIX else str(stock_code)

    if price > 0:
        pr_type, order_price = 0, price        # 0 = limit price
    else:
        pr_type, order_price = 2, 0            # 2 = counterparty price (market)

    if DRY_RUN:
        direction = "BUY" if ACTION_TO_OP[action] == 0 else "SELL"
        return True, "DRY_RUN no order (code=%s, %s %d @ %s)" % (
            code, direction, volume, order_price or "market")

    # QMT C++ passorder overloads (from runtime error message) - all end with
    # a strategy-context object (boost::python::api::object):
    #   passorder(int,int,str,str,int,double,double,object)                    <-- we use this (8 args)
    #   passorder(int,int,str,str,int,double,double,int,object)
    #   passorder(int,int,str,str,int,double,double,str,object)
    #   passorder(int,int,str,str,int,double,double,str,int,object)
    #   passorder(int,int,str,str,int,double,double,str,int,str,object)
    # The trailing object is the strategy ContextInfo. Do NOT pass an int/str
    # there - QMT accesses .request_id on it and crashes.
    try:
        if ctx is None:
            ctx = ContextInfo
        err_code = passorder(
            ACTION_TO_OP[action],  # opType 0 buy 1 sell
            0,                     # orderType 0 = limit order
            account_id,            # accountid
            code,                  # orderCode
            pr_type,               # prType
            order_price,           # price
            volume,                # volume
            ctx,                   # strategy context object (MUST be object)
        )
    except Exception as e:
        tb = traceback.format_exc()
        print("[SignalBridge] passorder ERR traceback:\n%s" % tb, flush=True)
        return False, "passorder ERR %r" % e

    if err_code == 0:
        direction = "BUY" if ACTION_TO_OP[action] == 0 else "SELL"
        return True, "submitted %s %s %d @ %s (prType=%d)" % (
            code, direction, volume, order_price or "market", pr_type)
    return False, "passorder err code %s" % err_code

# ---------------- scanner (background thread): parse + enqueue ----------------
def scan_and_execute():
    _ensure_dirs()
    now = time.time()
    for f in _list_json_files(PENDING_DIR):
        try:
            if now - os.path.getmtime(f) < FILE_MIN_AGE:
                continue
            with _queue_lock:
                if f in _queued_files:
                    continue
            try:
                sig = _read_json(f)
            except Exception as e:
                _write_result(FAILED_DIR,
                              {"order_id": os.path.splitext(os.path.basename(f))[0],
                               "stock_code": os.path.basename(f)},
                              False, "JSON parse ERR %r" % e)
                _safe_unlink(f)
                continue
            if DRY_RUN:
                success, detail = _call_passorder(sig)
                _write_result(DONE_DIR if success else FAILED_DIR, sig, success, detail)
                _safe_unlink(f)
            else:
                with _queue_lock:
                    _signal_queue.append((sig, f))
                    _queued_files.add(f)
        except Exception as e:
            print("[SignalBridge] scan %s ERR %r" % (os.path.basename(f), e), flush=True)

# ---------------- main thread: drain queue + place orders ----------------
def drain_queue(ctx=None):
    with _queue_lock:
        items = _signal_queue[:]
        _signal_queue[:] = []
    for sig, f in items:
        try:
            success, detail = _call_passorder(sig, ctx)
            _write_result(DONE_DIR if success else FAILED_DIR, sig, success, detail)
            _safe_unlink(f)
        except Exception as e:
            print("[SignalBridge] order %s ERR %r" % (os.path.basename(f), e), flush=True)
            _write_result(FAILED_DIR, sig, False, "handler ERR %r" % e)
            _safe_unlink(f)
        finally:
            with _queue_lock:
                _queued_files.discard(f)

def _cleanup_old_results():
    deadline = time.time() - KEEP_RESULT_DAYS * 86400
    for d in (DONE_DIR, FAILED_DIR, TRADES_DIR):
        for f in _list_json_files(d):
            try:
                if os.path.getmtime(f) < deadline:
                    _safe_unlink(f)
            except OSError:
                pass

# ---------------- background poll ----------------
_stop_event = threading.Event()

def _poll_loop():
    while not _stop_event.is_set():
        try:
            scan_and_execute()
            _cleanup_old_results()
        except Exception as e:
            print("[SignalBridge] poll ERR %r" % e, flush=True)
        _stop_event.wait(SCAN_INTERVAL)

def start_bridge():
    _stop_event.clear()
    t = threading.Thread(target=_poll_loop, name="SignalBridge", daemon=True)
    t.start()
    return t

def stop_bridge():
    _stop_event.set()

# ---------------- QMT strategy entry ----------------
def init(ContextInfo):
    pass

def after_init(ContextInfo):
    print("[SignalBridge] env=%s scan=%s" % (ENVIRONMENT, PENDING_DIR), flush=True)
    ok, msg = _check_safety()
    print("[SignalBridge] safety: %s" % msg, flush=True)
    if not ok:
        print("[SignalBridge] startup aborted: %s" % msg, flush=True)
        return
    start_bridge()

def handlebar(ContextInfo):
    drain_queue(ContextInfo)

def stop(ContextInfo):
    print("[SignalBridge] stopped", flush=True)
    stop_bridge()

# ---------------- local self-test ----------------
if __name__ == "__main__":
    if "passorder" not in globals():
        def passorder(opType, orderType, accountid, orderCode, prType, price, volume,
                      strategyName, quickTrade, validType):
            print("  [stub] passorder(%d,%d,%s,%s prType=%d price=%s vol=%d)"
                  % (opType, orderType, accountid, orderCode, prType, price, volume))
            return 0
    print("[SignalBridge] local test env=%s DRY_RUN=%s" % (ENVIRONMENT, DRY_RUN))
    ok, msg = _check_safety()
    print("[SignalBridge] safety: %s" % msg)
    if ok:
        scan_and_execute()
        drain_queue()
        print("[SignalBridge] test done, check %s" % DONE_DIR)
    else:
        print("[SignalBridge] test aborted: %s" % msg)