# -*- coding: utf-8 -*-
"""tt_solo 测试公共夹具。"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]      # D:/cc-joesph/tt_solo
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ttcore.state import Ledger                 # noqa: E402


FIXED_NOW = datetime(2026, 9, 14, 10, 0, 0)


# ponytail: 此处只搬 test_state.py 需要的两个夹具(照抄 tt/tests/conftest.py);
# cfg / FakeFeed / make_snapshot / sample_dir 等留到 Task 5 搬 market+config 时
# 一并全量补齐 —— 那时本节会被其超集覆盖, 不会留重复。
@pytest.fixture
def now_fn():
    return lambda: FIXED_NOW


@pytest.fixture
def ledger(tmp_path, now_fn):
    return Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
