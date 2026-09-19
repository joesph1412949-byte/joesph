# -*- coding: utf-8 -*-
"""common.py 单元测试 — 后缀规则/涨跌停幅度/日志设置。"""
import sys
import logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 项目根(common.py)

import pytest

from shared.common import (SIGNAL_ROOT, SECTORS, LOG_DIR, em_code_to_tick_key,
                    limit_ratio_for_code, setup_logging, with_market_suffix)
from shared.exit_rules import limit_ratio as exit_limit_ratio


def test_with_market_suffix():
    assert with_market_suffix("600000") == "600000.SH"
    assert with_market_suffix("000001") == "000001.SZ"
    assert with_market_suffix("300001") == "300001.SZ"
    assert with_market_suffix("688001") == "688001.SH"
    assert with_market_suffix("830799") == "830799.BJ"
    assert with_market_suffix("920001") == "920001.BJ"
    assert with_market_suffix("600000.SH") == "600000.SH"   # 已带后缀原样
    assert with_market_suffix("") == ""


def test_em_code_to_tick_key_alias():
    assert em_code_to_tick_key("002859") == "002859.SZ"
    assert em_code_to_tick_key("000001") == "000001.SZ"


@pytest.mark.parametrize("code,expected", [
    ("600000", 0.10), ("601398", 0.10), ("000001", 0.10), ("002859", 0.10),
    ("300001", 0.20), ("301001", 0.20), ("688001", 0.20),   # 创业板/科创板
    ("830799", 0.30), ("920001.BJ", 0.30), ("430047.BJ", 0.30),  # 北交所
    ("400001", 0.30), ("420001", 0.30),                     # 老三板/退市(同 exit_rules)
    ("", 0.10), ("ABC", 0.10), (None, 0.10),                # 坏值 fail-open 到主板档
])
def test_limit_ratio_for_code_by_board(code, expected):
    assert limit_ratio_for_code(code) == expected


# 涨跌停口径只允许有一份规格(shared/exit_rules.py)。本测试是"不要再分裂"的常驻
# 守卫: 两边逐条相等才绿。覆盖全部前缀档 + 非法/空串/None。
@pytest.mark.parametrize("code", [
    "600000", "601398", "000001", "002859", "300001", "301001",
    "688001", "689009", "920001.BJ", "830799.BJ", "430047.BJ",
    "400001", "420001", "", "ABC", None,
])
def test_limit_ratio_consistent_with_exit_rules(code):
    assert limit_ratio_for_code(code) == exit_limit_ratio(code)


def test_shared_config_values():
    assert SIGNAL_ROOT.name == "QMT_SIGNALS"
    assert "SW1电子" in SECTORS
    assert "SW1计算机" in SECTORS


def test_setup_logging_idempotent(tmp_path):
    logger = setup_logging("test_common", log_dir=tmp_path)
    n_handlers = len(logger.handlers)
    logger2 = setup_logging("test_common", log_dir=tmp_path)
    assert len(logger2.handlers) == n_handlers      # 重复调用不加 handler
    assert any(isinstance(h, logging.handlers.TimedRotatingFileHandler)
               for h in logger.handlers)
    assert (tmp_path / "test_common.log").exists()
