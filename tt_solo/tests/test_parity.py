# -*- coding: utf-8 -*-
"""把新旧对照固化为测试: 搬家不得改变任何决策输出。

注意: 本测试依赖旧 tt 包仍存在。删掉 tt/ 后本测试应被同步移除
(见计划 Task 14 —— 删除时一并处理)。
"""
import sys
from pathlib import Path

import pytest

TT_SOLO = Path(__file__).resolve().parents[1]
REPO = TT_SOLO.parent
for p in (str(REPO), str(TT_SOLO)):
    if p not in sys.path:
        sys.path.insert(0, p)

pytest.importorskip("tt.engine", reason="旧 tt 包已删除, 对照测试不再适用")


def test_plans_match_between_old_and_new(tmp_path):
    from tools.compare_legacy import run_side, strip_volatile
    a, b = tmp_path / "old", tmp_path / "new"
    a.mkdir(); b.mkdir()
    old_plan, old_led = run_side("tt", a)
    new_plan, new_led = run_side("ttcore", b)
    assert strip_volatile(old_plan) == strip_volatile(new_plan)
    assert strip_volatile(old_led) == strip_volatile(new_led)
