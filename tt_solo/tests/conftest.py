# -*- coding: utf-8 -*-
"""tt_solo 测试公共夹具。"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]      # D:/cc-joesph/tt_solo
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
