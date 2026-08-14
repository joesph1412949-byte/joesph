# -*- coding: utf-8 -*-
# QMT passorder signature diagnostic (ASCII only)
import inspect
import sys

# passorder is provided by QMT runtime at startup; this stub only silences
# IDE static-analysis warnings and is never called by QMT.
def passorder(*args, **kwargs):
    return 0

def init(ContextInfo):
    pass

def after_init(ContextInfo):
    try:
        sig = inspect.signature(passorder)
        print("[DIAG] passorder signature:", sig)
    except Exception as e:
        print("[DIAG] signature FAILED:", repr(e))
    try:
        print("[DIAG] passorder doc:", passorder.__doc__)
    except Exception as e:
        print("[DIAG] doc FAILED:", repr(e))
    try:
        print("[DIAG] passorder module:", passorder.__module__)
    except Exception as e:
        print("[DIAG] module FAILED:", repr(e))
    print("[DIAG] python version:", sys.version)

def handlebar(ContextInfo):
    pass
