# -*- coding: utf-8 -*-
"""prism.market 市场情绪分类测试(与旧 strategy_web.models.classify_market 等价)。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from prism.market import classify_market


def test_classify_market_tiers():
    """5=高潮 4=回暖 3=冰点 2以下=退潮(与旧 screen.py 语义一致)。"""
    assert classify_market(5) == "高潮期"
    assert classify_market(6) == "高潮期"
    assert classify_market(4) == "回暖期"
    assert classify_market(3) == "冰点期"
    assert classify_market(2) == "退潮期"
    assert classify_market(0) == "退潮期"
