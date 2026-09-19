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
# REAL-order safety gates (keep enabled). Each one is evaluated PER ORDER
# inside drain_queue() (the decision logic is the pure function _should_place):
# 1) PAUSE file: <SIGNAL_ROOT>/paused exists -> place NOTHING (emergency stop).
#    Pending files are KEPT while paused so they can be processed after release.
#    Same convention as prism/trader.check_paused and ttcore/daemon.is_paused:
#    the file lives in the signal ROOT, not inside the env dir.
# 2) ARM file: pending signals are consumed ONLY while an armed file dated
#    today exists at SIGNAL_ROOT/real/armed.txt  (content contains today YYYYMMDD).
#    Prevents accidental order placement when the strategy runs by mistake.
# 3) Per-day dedup: the same order_id is ordered at most once per calendar
#    day. Keyed on order_id, NOT stock_code -- a T+0 grid strategy places
#    several orders for one stock per day (one per ladder rung, both ways);
#    keying on stock_code would reject rung 2+ with DUPLICATE.
# 4) Same-round sell guard: if a SELL for a code was NOT accepted by the
#    terminal in THIS drain round, later BUYs for the SAME code are not sent
#    (the buy-back only makes sense if the sell was actually accepted).
# 5) Price sanity: reject absurd limit prices (e.g. 999999.99 from a typo).
#    A-share stocks are all well below this; keeps a fat-finger order out.
#    This one lives in _call_passorder (it needs the parsed order params).
# NOTE on time-based checks: never gate on tick.time/timetag -- QMT re-stamps
# them to the current wall clock every round (see tt_solo/ttcore/market.py).
# If a session-window check is ever needed, use the local wall clock plus a
# once-a-day WARNING.
PAUSE_FILE = os.path.join(SIGNAL_ROOT, "paused")
ARMED_FILE = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "armed.txt")
DEDUP_FILE = os.path.join(SIGNAL_ROOT, ENVIRONMENT, "placed_today.json")
DEDUP_ENABLED = True
MAX_ORDER_PRICE = 100000.0
# 6) Optional hardening (opt-in -- BOTH defaults keep today's behaviour):
#    ALLOWED_ACCOUNTS empty  = accept whatever account QMT is logged into (the
#      current default). Put your REAL fund account id(s) here to whitelist
#      them; anything else is then rejected with the account id printed.
#    MAX_ORDER_VOLUME 0 = no limit. Set a positive number to reject a
#      fat-fingered volume (a hand-written JSON with one digit too many); the
#      same pattern as MAX_ORDER_PRICE.
ALLOWED_ACCOUNTS = ()
MAX_ORDER_VOLUME = 0
# NOT DONE on purpose (decided after review, 2026-09-19):
# - Limit-up/limit-down band check: this bridge is deliberately self-contained
#   (no project imports, no market-data access), so it has no previous close /
#   limit price to compare against. It would need a QMT-side API whose
#   availability in the strategy context is UNVERIFIED. MAX_ORDER_PRICE stays.
# - pid/lock file against a SECOND QMT terminal running this same bridge: a lock
#   is the only mechanism that can SILENTLY BLOCK real orders and needs a human
#   to clear it (a crash leaves a stale lock -> no orders all day, unannounced).
#   It would trade an operational mistake (loading the real bridge twice) for a
#   brand-new failure mode inside the ordering path. The per-order re-read plus
#   the atomic write in _save_placed shrink that window to a single order
#   instead; keep it an operational rule, not code.
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

def _is_paused():
    """Emergency stop: <SIGNAL_ROOT>/paused exists -> place nothing.

    Same convention as prism/trader.check_paused and ttcore/daemon.is_paused:
    the file lives in the signal ROOT, not inside the env dir. Being a plain
    existence test it is cheap enough to re-run for EVERY order. While it exists,
    pending files are kept (not consumed) so releasing the stop resumes them."""
    return os.path.exists(PAUSE_FILE)

def _load_placed():
    """Load the per-day placed record {date: [keys]}.

    - file missing -> {} (nothing placed yet today)
    - file present but unreadable / not a dict -> None = FAIL CLOSED. The caller
      must refuse to place rather than read it as "{}" and silently re-open the
      dedup gate (that is how a truncated file turns into a duplicate order)."""
    if not os.path.exists(DEDUP_FILE):
        return {}
    try:
        with open(DEDUP_FILE, "r", encoding="utf-8") as fp:
            data = json.load(fp)
        if isinstance(data, dict):
            return data
    except Exception as e:
        print("[SignalBridge] load placed ERR %r (fail-closed)" % e, flush=True)
    return None

def _save_placed(placed):
    """Write the dedup record ATOMICALLY: serialize first, then tmp + fsync +
    os.replace. A crash or power loss can therefore never leave a truncated
    file behind (which _load_placed would read as unusable and refuse on).

    On Windows os.replace() onto a path that any process holds an open read
    handle for raises PermissionError (WinError 32) -- retry once after a short
    pause, then give up loudly (the next order will hit fail-closed instead of
    running with a stale account)."""
    tmp = DEDUP_FILE + ".tmp"
    try:
        blob = json.dumps(placed, ensure_ascii=False, indent=2)
        with open(tmp, "w", encoding="utf-8") as fp:
            fp.write(blob)
            fp.flush()
            os.fsync(fp.fileno())
        for attempt in (1, 2):
            try:
                os.replace(tmp, DEDUP_FILE)
                return
            except PermissionError:
                if attempt == 1:
                    time.sleep(0.05)
                    continue
                raise
    except Exception as e:
        print("[SignalBridge] save placed ERR %r" % e, flush=True)

def _code_key(sig):
    """Normalized stock code, for comparing two signals on the same stock.
    Same rule as shared/common.py with_market_suffix (see _with_market_suffix)."""
    code = str(sig.get("stock_code") or "")
    return _with_market_suffix(code) if ADD_MARKET_SUFFIX else code

def _dedup_key(sig):
    """Dedup key for a signal. Prefer order_id (unique per trade), fall back
    to stock_code for hand-written signals that carry no order_id.

    Why order_id and NOT stock_code: a day-trading (T+0 grid) strategy sends
    MULTIPLE orders for the SAME stock per day (one per ladder rung, both
    directions). Keying on stock_code would reject the 2nd order onward with
    DUPLICATE, silently breaking the whole ladder. order_id is deterministic
    per (date, code, side, rung) so it still blocks true re-sends/restarts.
    """
    oid = str(sig.get("order_id") or "").strip()
    if oid:
        return oid
    return _code_key(sig)

def _already_placed_today(sig, placed):
    """True if this signal (by order_id) was already placed today (dedup)."""
    return _dedup_key(sig) in placed.get(datetime.now().strftime("%Y%m%d"), [])

def _mark_placed_today(sig, placed):
    """Record this signal's dedup key as placed today."""
    key = _dedup_key(sig)
    if not key:
        return
    keys = placed.setdefault(datetime.now().strftime("%Y%m%d"), [])
    if key not in keys:
        keys.append(key)
        _save_placed(placed)


def _should_place(sig, armed, paused, placed, rejected_sells=()):
    """Pure gate chain: no file I/O, no order, no QMT. Returns (decision, detail).

    decision: "paused" | "not_armed" | "dedup_unreadable" | "duplicate"
              | "sell_not_accepted" | "place"

    Chain order (first hit wins):
        paused -> armed -> per-day dedup -> same-round sell guard.

    Price/volume sanity is deliberately NOT here: it needs the parsed order
    parameters, so it stays in _call_passorder (the single choke point before
    passorder is called).

    `placed` is the loaded per-day record; None means the dedup file exists but
    could not be read -> fail closed. `rejected_sells` holds the normalized
    codes whose SELL was not accepted in this drain round.
    """
    if paused:
        return "paused", "paused file exists: %s" % PAUSE_FILE
    if not armed:
        return "not_armed", "not armed"
    if DEDUP_ENABLED:
        if placed is None:
            return "dedup_unreadable", "DEDUP UNREADABLE (fail-closed): %s" % DEDUP_FILE
        if _already_placed_today(sig, placed):
            return "duplicate", "DUPLICATE: %s already placed today" % sig.get("stock_code")
    if rejected_sells:
        action = str(sig.get("action") or sig.get("order_type") or "").upper()
        if action == "BUY" and _code_key(sig) in rejected_sells:
            return ("sell_not_accepted",
                    "SELL_NOT_ACCEPTED: sell of %s not accepted this round"
                    % sig.get("stock_code"))
    return "place", ""


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
    # Keep in sync with common.py with_market_suffix. This file stays
    # self-contained on purpose: QMT runs it as a pasted strategy where
    # project-root imports are not available.
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
    # Volume sanity (opt-in fat-finger guard): MAX_ORDER_VOLUME = 0 disables it.
    if MAX_ORDER_VOLUME and volume > MAX_ORDER_VOLUME:
        return False, "volume out of range %r (max %d)" % (volume, MAX_ORDER_VOLUME)

    account_id = _resolve_account(sig, ctx)
    if not account_id:
        return False, "no account: set FIXED_ACCOUNT or leave signal account empty and log in"
    # Account whitelist (opt-in): ALLOWED_ACCOUNTS = () accepts any account.
    if ALLOWED_ACCOUNTS and account_id not in ALLOWED_ACCOUNTS:
        return False, "account %s not in ALLOWED_ACCOUNTS %s" % (
            account_id, list(ALLOWED_ACCOUNTS))

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
# Files kept (not consumed) while a keep-gate blocks them; logged at most once
# per process so an emergency stop does not spam the log every scan tick.
_kept_files = set()

def drain_queue(ctx=None):
    with _queue_lock:
        items = _signal_queue[:]
        _signal_queue[:] = []
    # Codes whose SELL was not accepted in THIS round (see _should_place gate 4).
    rejected_sells = set()
    for sig, f in items:
        try:
            # Both emergency gates (paused / armed) are re-read PER ORDER on
            # purpose. The files are the operator's stop button and one drain
            # round may hold many signals: reading them once per batch would let
            # a stop pressed mid-batch still place every remaining order (and the
            # batch is exactly when someone panics and presses stop). The cost is
            # one os.path.exists plus one small file read per order -- orders are
            # rare, so this is nothing, and there is no lock to race for.
            armed, armed_msg = _is_armed()
            paused = _is_paused()
            # The dedup account is re-read per order too. Combined with the
            # atomic write in _save_placed it shrinks the window in which two
            # QMT terminals running this same bridge both fail to see each
            # other's record down to a single order. It is NOT a lock: a true
            # read-check-place interleave across two terminals is still possible
            # (fixing that needs a pid/lock file -- deliberately not added here,
            # a stale lock would block real orders).
            placed = _load_placed() if DEDUP_ENABLED else {}
            decision, why = _should_place(sig, armed, paused, placed, rejected_sells)

            # Gates that KEEP the pending file: nothing was ordered and nothing
            # failed, so there is no done/failed result to write -- the file is
            # simply left for the next round once the operator clears the block.
            if decision in ("paused", "dedup_unreadable"):
                if f not in _kept_files:
                    _kept_files.add(f)
                    print("[SignalBridge] KEPT %s (%s)"
                          % (os.path.basename(f), why), flush=True)
                continue

            if decision == "not_armed":
                _write_result(FAILED_DIR, sig, False, "NOT ARMED: %s" % armed_msg)
                _safe_unlink(f)
                continue
            if decision == "duplicate":
                _write_result(FAILED_DIR, sig, False, why)
                _safe_unlink(f)
                continue
            if decision == "sell_not_accepted":
                # Drop the buy-back outright instead of keeping the file: the
                # scanner would re-queue it on the NEXT round (2 s later) with
                # no memory of the rejected sell, so "keep" would make this gate
                # a no-op. The failed result records why.
                _write_result(FAILED_DIR, sig, False, why)
                _safe_unlink(f)
                continue

            action = str(sig.get("action") or sig.get("order_type") or "").upper()
            success, detail = _call_passorder(sig, ctx)
            _write_result(DONE_DIR if success else FAILED_DIR, sig, success, detail)
            # Record a passorder EXCEPTION as placed, too: the exception may hit
            # after the order already reached the broker (submit timeout), and a
            # re-send would then be a SECOND real order -- missing one order
            # beats sending it twice. A non-zero return code is a definite
            # rejection by the terminal (nothing reached the broker), so that is
            # NOT recorded. Local validation failures return other messages and
            # are not recorded either.
            if success or detail.startswith("passorder ERR"):
                _mark_placed_today(sig, placed)
            if action == "SELL" and not success:
                rejected_sells.add(_code_key(sig))
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
    print("[SignalBridge] paused=%s (file %s)" % (_is_paused(), PAUSE_FILE), flush=True)
    print("[SignalBridge] dedup=%s file=%s" % (DEDUP_ENABLED, DEDUP_FILE), flush=True)
    if _is_paused():
        print("[SignalBridge] !!! PAUSED: %s exists -> no orders until it is "
              "removed (pending files are kept)." % PAUSE_FILE, flush=True)
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
