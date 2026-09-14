# -*- coding: utf-8 -*-
"""common.py 单元测试 — 后缀规则/涨跌停幅度/日志设置。"""
import sys
import logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 项目根(common.py)

from shared.common import (SIGNAL_ROOT, SECTORS, LOG_DIR, em_code_to_tick_key,
                    limit_ratio_for_code, setup_logging, with_market_suffix)


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


def test_limit_ratio_for_code():
    assert limit_ratio_for_code("600000") == 0.10   # 主板
    assert limit_ratio_for_code("000001") == 0.10
    assert limit_ratio_for_code("300001") == 0.20   # 创业板
    assert limit_ratio_for_code("688001") == 0.20   # 科创板
    assert limit_ratio_for_code("830799") == 0.30   # 北交所
    assert limit_ratio_for_code("400001") == 0.30


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
