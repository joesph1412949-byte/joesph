#encoding:gbk
# QMT REAL signal bridge - ASCII only (no CJK to avoid QMT GBK encoding issues)
# ============================================================
#  WARNING: THIS PLACES REAL ORDERS WITH REAL MONEY.
#  Run this ONLY inside the REAL QMT terminal, logged in with
#  your REAL fund account. Never run it in the simulation
#  terminal, and never leave the simulation strategy running
#  while this is active.
#  Safety relies on the QMT client itself (login + confirm).
# ============================================================
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime

# ================= CONFIG - review before enabling =================
SIGNAL_ROOT = r"D:/QMT_SIGNALS"
ENVIRONMENT = "real"            # fixed: real orders
DRY_RUN = False                 # MUST stay False to place real orders
SCAN_INTERVAL = 2
FILE_MIN_AGE = 1.0
KEEP_RESULT_DAYS = 7
ADD_MARKET_SUFFIX = True
# Optional: hardcode your REAL fund account below.
# Leave "" to auto-use the account currently logged into QMT (recommended).
FIXED_ACCOUNT = ""
# REAL-order safety gates (keep enabled):
# 1) ARM file: pending signals are consumed ONLY while an armed file dated
#    today exists at SIGNAL_ROOT/real/armed.txt  (content contains today YYYYMMDD).
#    Prevents accidental order placement when the strategy runs by mistake.
ARMED_FILE = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "armed.txt")
# 2) Per-day dedup: the same stock code is ordered at most once per calendar day.
#    Prevents double orders when the same signal file is re-sent or scanned twice.
DEDUP_FILE = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "placed_today.json")
DEDUP_ENABLED = True
# 3) Price sanity: reject absurd limit prices (e.g. 999999.99 from a typo).
#    A-share stocks are all well below this; keeps a fat-finger order out.
MAX_ORDER_PRICE = 100000.0
# ===================================================================

PENDING_DIR = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "pending")
DONE_DIR = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "done")
FAILED_DIR = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "failed")
TRADES_DIR = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "trades")

ACTION_TO_OP = {"BUY": 0, "SELL": 1}

_signal_queue = []
_queue_lock = threading.Lock()
_queued_files = set()

# ---------------- utils ----------------
def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def _ensure_dirs():
    for d in (PENDING_DIR, DONE_DIR, FAILED_DIR, TRADES_DIR):
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass

# ---------------- real-order safety gates ----------------

def _is_armed():
    """Check the armed file. Pending REAL signals are consumed ONLY while an
    armed file exists whose content contains today's YYYYMMDD.
    Returns (ok: bool, message: str)."""
    try:
        if not os.path.exists(ARMED_FILE):
            return False, "armed file missing: %s" % ARMED_FILE
        with open(ARMED_FILE, "r", encoding="utf-8") as fp:
            content = (fp.read() or "").strip()
        today = datetime.now().strftime("%Y%m%d")
        if today not in content:
            return False, "armed file not dated today (%s): %s" % (today, ARMED_FILE)
        return True, "armed"
    except Exception as e:
        return False, "armed file check error %r" % e

def _load_placed():
    """Load the per-day placed-codes record. Returns dict {date: [codes]}."""
    try:
        with open(DEDUP_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}

def _save_placed(placed):
    try:
        with open(DEDUP_FILE, "w", encoding="utf-8") as fp:
            json.dump(placed, fp, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[SignalBridge] save placed ERR %r" % e, flush=True)

def _already_placed_today(sig, placed):
    """True if this stock code was already placed today (dedup)."""
    code = _with_market_suffix(sig.get("stock_code") or "") if ADD_MARKET_SUFFIX \
        else str(sig.get("stock_code") or "")
    today = datetime.now().strftime("%Y%m%d")
    return code in placed.get(today, [])

def _mark_placed_today(sig, placed):
    """Record this stock code as placed today."""
    code = _with_market_suffix(sig.get("stock_code") or "") if ADD_MARKET_SUFFIX \
        else str(sig.get("stock_code") or "")
    today = datetime.now().strftime("%Y%m%d")
    codes = placed.setdefault(today, [])
    if code not in codes:
        codes.append(code)
        _save_placed(placed)


def _get_attr(obj, *names, default=""):
    if obj is None:
        return default
    for n in names:
        try:
            v = getattr(obj, n, None)
            if v is not None:
                return v
        except Exception:
            pass
    return default

def _safe_unlink(f):
    try:
        os.remove(f)
    except OSError:
        pass

def _with_market_suffix(code):
    code = str(code).strip()
    if "." in code:
        return code
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

# ---------------- account resolution ----------------
def _resolve_account(sig, ctx=None):
    acc = str(FIXED_ACCOUNT or sig.get("account_id") or "").strip()
    if acc and acc not in ("YOUR_ACCOUNT", "YOUR-REAL-ACCOUNT"):
        return acc
    if ctx is None:
        ctx = globals().get("ContextInfo", None)
    acc = _get_attr(ctx, "accountID", "account_id", "m_accountID", "accountId", default="")
    return str(acc).strip()

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
    if action not in ACTION_TO_OP:
        return False, "unknown action %r" % action
    # Price sanity (fat-finger guard): a positive limit price must be sane.
    if price < 0 or price > MAX_ORDER_PRICE:
        return False, "price out of range %r (max %.0f)" % (price, MAX_ORDER_PRICE)

    account_id = _resolve_account(sig, ctx)
    if not account_id:
        return False, "no account: set FIXED_ACCOUNT or leave signal account empty and log in"

    code = _with_market_suffix(stock_code) if ADD_MARKET_SUFFIX else str(stock_code)

    if price > 0:
        pr_type, order_price = 0, price        # 0 = limit price
    else:
        pr_type, order_price = 2, 0            # 2 = counterparty price (market)

    if DRY_RUN:
        return True, "DRY_RUN no order (code=%s, %s %d @ %s, account=%s)" % (
            code, action, volume, order_price or "market", account_id)

    # QMT passorder overloads (from runtime error message) - all end with a
    # strategy-context object (boost::python::api::object), which QMT accesses
    # .request_id on. Passing int/str there crashes. We use the 8-arg form:
    #   passorder(int,int,str,str,int,double,double,object)
    try:
        if ctx is None:
            ctx = globals().get("ContextInfo", None)
        print("[SignalBridge] ORDER account=%s code=%s %s %d @ %s prType=%d"
              % (account_id, code, action, volume, order_price or "market", pr_type), flush=True)
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
        return True, "submitted %s %s %d @ %s (prType=%d, account=%s)" % (
            code, action, volume, order_price or "market", pr_type, account_id)
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
    # REAL-order safety gate 1: armed file must be present and dated today.
    armed, armed_msg = _is_armed()
    placed = _load_placed() if DEDUP_ENABLED else {}
    for sig, f in items:
        try:
            if not armed:
                _write_result(FAILED_DIR, sig, False, "NOT ARMED: %s" % armed_msg)
                _safe_unlink(f)
                continue
            # REAL-order safety gate 2: same stock code only once per day.
            if DEDUP_ENABLED and _already_placed_today(sig, placed):
                _write_result(FAILED_DIR, sig, False,
                              "DUPLICATE: %s already placed today" % sig.get("stock_code"))
                _safe_unlink(f)
                continue
            success, detail = _call_passorder(sig, ctx)
            _write_result(DONE_DIR if success else FAILED_DIR, sig, success, detail)
            if success:
                _mark_placed_today(sig, placed)
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
    print("================================================================", flush=True)
    print("[SignalBridge] REAL MODE env=%s scan=%s" % (ENVIRONMENT, PENDING_DIR), flush=True)
    print("[SignalBridge] WARNING: this strategy places REAL orders.", flush=True)
    print("================================================================", flush=True)
    if DRY_RUN:
        print("[SignalBridge] DRY_RUN=True: log only, no real order.", flush=True)
    else:
        print("[SignalBridge] DRY_RUN=False: orders are LIVE.", flush=True)
    armed, armed_msg = _is_armed()
    print("[SignalBridge] armed=%s (%s)" % (armed, armed_msg), flush=True)
    print("[SignalBridge] dedup=%s file=%s" % (DEDUP_ENABLED, DEDUP_FILE), flush=True)
    if not armed:
        print("[SignalBridge] !!! NOT ARMED: create %s containing today's YYYYMMDD "
              "to allow order placement." % ARMED_FILE, flush=True)
    start_bridge()

def handlebar(ContextInfo):
    drain_queue(ContextInfo)

def stop(ContextInfo):
    print("[SignalBridge] stopped", flush=True)
    stop_bridge()

# NOTE: no __main__ self-test here on purpose. QMT runs this file as a
# strategy (init/after_init/handlebar/stop). A __main__ block would print
# misleading "local test" lines and never start the scan thread.
