# -*- coding: utf-8 -*-
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.context import FactorContext


def test_context_fields_default_none():
    ctx = FactorContext(code="600000.SH")
    assert ctx.code == "600000.SH"
    assert ctx.kline is None
    assert ctx.tick is None
    assert ctx.float_mv is None


def test_context_from_data():
    ctx = FactorContext.from_data(code="000001.SZ", last=10.0, float_mv=1e8)
    assert ctx.code == "000001.SZ"
    assert ctx.last == 10.0
    assert ctx.float_mv == 1e8


def test_context_get_missing_returns_none():
    ctx = FactorContext(code="600000.SH")
    assert ctx.get("nonexistent") is None
