# -*- coding: utf-8 -*-
"""dashboard 测试夹具: 把 tt_solo/ 挂上 sys.path。

必须独立于 tt_solo/tests/conftest.py —— 后者不是本目录的父目录,
pytest 不会加载它。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]      # -> tt_solo/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
