# -*- coding: utf-8 -*-
# QMT order probe v2 - ASCII only. Tries many order call variants in handlebar.
# Paste into a NEW QMT strategy, run, copy ALL [PROBE] lines back to Claude.

ACCOUNT = "88869979"

def init(ContextInfo):
    pass

def after_init(ContextInfo):
    print("[PROBE] after_init: will try order variants in handlebar", flush=True)

def _try(name, fn):
    try:
        r = fn()
        print("[PROBE] %-30s -> ret=%r type=%s" % (name, r, type(r).__name__), flush=True)
    except Exception as e:
        import traceback
        print("[PROBE] %-30s -> EXC %r" % (name, e), flush=True)
        print("[PROBE] %-30s traceback:\n%s" % (name, traceback.format_exc()[-500:]), flush=True)

def handlebar(ContextInfo):
    if not hasattr(ContextInfo, "_probe_done"):
        ContextInfo._probe_done = True
        print("[PROBE] === begin === code=000593.SZ account=%s" % ACCOUNT, flush=True)

        # passorder variants
        _try("A10_no_extra", lambda: passorder(
            0, 0, ACCOUNT, "000593.SZ", 0, 7.0, 100, "Probe", 2, 0))
        _try("B11_userOrderId", lambda: passorder(
            0, 0, ACCOUNT, "000593.SZ", 0, 7.0, 100, "Probe", 2, 0, "PROBE001"))
        _try("C11_ctx", lambda: passorder(
            0, 0, ACCOUNT, "000593.SZ", 0, 7.0, 100, "Probe", 2, 0, ContextInfo))
        _try("D12_both", lambda: passorder(
            0, 0, ACCOUNT, "000593.SZ", 0, 7.0, 100, "Probe", 2, 0, "PROBE001", ContextInfo))
        # quickTrade variants on the 10-arg form
        _try("E10_qt1", lambda: passorder(
            0, 0, ACCOUNT, "000593.SZ", 0, 7.0, 100, "Probe", 1, 0))
        _try("F10_qt0", lambda: passorder(
            0, 0, ACCOUNT, "000593.SZ", 0, 7.0, 100, "Probe", 0, 0))
        # alternative order functions (some QMT builds expose these)
        if "order_lots" in dir(__builtins__) or True:
            try:
                _try("G_order_lots", lambda: order_lots(ACCOUNT, "000593.SZ", 0, 7.0, 100, "Probe", 2, "PROBE002"))
            except Exception as e:
                print("[PROBE] G_order_lots not available: %r" % e, flush=True)
        try:
            _try("H_order_value", lambda: order_value(ACCOUNT, "000593.SZ", 0, 700.0, 100, "Probe", 2, "PROBE003"))
        except Exception as e:
            print("[PROBE] H_order_value not available: %r" % e, flush=True)

        print("[PROBE] === done ===", flush=True)
