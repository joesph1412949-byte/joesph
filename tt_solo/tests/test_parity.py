# -*- coding: utf-8 -*-
"""把新旧对照固化为测试。

**结论的准确读法**: 除场景B钉死的那**一条北交所 920xxx 涨跌停比例例外**之外,
搬迁前后逐字段一致。不是"逐位相同" —— 旧侧把 920xxx 当 ±10%, 会把第3档误判
BAND_OUT 拒单; 新侧按 ±30% 正确放行。该例外是刻意保留的修正(0.30 才对),
当前部署标的集不含 920xxx, 故目前潜伏。

覆盖三个面: plan() 决策输出 / 账本成交侧(record_fill + set_units → snapshot) /
日终归档行。任一面出现例外以外的差异 → 断言失败。

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


def test_main_scenario_matches(tmp_path):
    """场景A: 3 只标的 2 轮 plan→成交记账→日终归档, plan/账本/归档行逐字段一致。"""
    from tools.compare_legacy import compare_main
    assert compare_main(tmp_path)["problems"] == []


def test_920xxx_divergence_is_pinned_as_expected(tmp_path):
    """场景B: 唯一允许的分歧 —— 旧侧把 920xxx 第3档误判 BAND_OUT, 新侧放行。

    除该档外的任何差异(多一个档位、少一个档位、别的字段动了)都会失败。
    """
    from tools.compare_legacy import compare_bj
    assert compare_bj(tmp_path)["problems"] == []
