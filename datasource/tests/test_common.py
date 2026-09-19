# -*- coding: utf-8 -*-
"""common.py 单元测试 — 后缀规则/涨跌停幅度/日志设置。"""
import sys
import logging
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 项目根(common.py)

import pandas as pd

import pytest

from shared.common import (SIGNAL_ROOT, SECTORS, LOG_DIR, em_code_to_tick_key,
                    limit_ratio_for_code, setup_logging, with_market_suffix)
from shared.exit_rules import limit_ratio as exit_limit_ratio
from datasource.data_source import DataSource
from datasource.factors import FactorEngine
from prism.tdx_source import _MKT_BJ, _MKT_SH, _MKT_SZ, _split_code
import prism.paper
import prism.zt_history


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
    ("300001", 0.20), ("301001", 0.20),                     # 创业板
    ("688001", 0.20), ("689009", 0.20),                     # 科创板 + 科创CDR
    ("830799", 0.30), ("920001.BJ", 0.30), ("430047.BJ", 0.30),  # 北交所/新三板
    ("400001", 0.05), ("420001", 0.05),                     # 老三板(险 400/420, 非北交所)
    ("", 0.10), ("ABC", 0.10), (None, 0.10),                # 坏值 fail-open 到主板档
])
def test_limit_ratio_for_code_by_board(code, expected):
    assert limit_ratio_for_code(code) == expected


# 涨跌停口径只允许有一份规格(shared/exit_rules.py)。下面是"不要再分裂"的常驻守卫:
# 全部副本逐码与权威规格相等才绿。覆盖全部前缀档 + 空串/None。
GUARD_CODES = [
    "600000", "601398", "000001", "002859", "300001", "301001",
    "688001", "689009", "920001.BJ", "830799.BJ", "430047.BJ",
    "400001", "420001", "", "ABC", None,
]

# 副本清单(2026-09-19 收敛后):
#   shared/common.py:limit_ratio_for_code     —— 镜像(内联, 不能互相 import)
#   prism/zt_history.py:_limit_ratio          —— 决定涨停池成员/连板数的那份
#   prism/paper.py:_limit_ratio               —— 跌停顺延兜底那份
#   tt_solo/ttcore/_vendor.py                 —— 刻意自包含, 只能按路径加载比对
#   datasource/factors.py (F1)                —— 档位内联在 compute_factors 里
#   datasource/data_source.py (涨停兜底)       —— 档位内联在 get_limit_up_stocks 里
# 后两者没有可导出的函数 ⇒ 用"探针"从**行为**上反解档位(见下面的探针测试)。
_VENDOR_PATH = (Path(__file__).resolve().parents[2]
                / "tt_solo" / "ttcore" / "_vendor.py")


def _guard_self_check():
    """守卫自证: 目标文件真实存在 —— 否则下面每条断言都会在"副本搬走了"时
    静默空转(本仓踩过"字符串级断言空转"的坑, 这里连路径也是行为输入)。"""
    assert _VENDOR_PATH.exists(), _VENDOR_PATH
    return True


def _load_tt_vendor():
    """按路径加载 tt_solo 的自包含 _vendor(它**不许** import shared, 见
    tt_solo/tests/test_selfcontained.py), 只比对返回值。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_tt_vendor_guard", _VENDOR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_TT_VENDOR = _load_tt_vendor()

# 探针参数: 前收 10.00 元 + "今日"现价 12.00 元(只为让 F1 的"今日涨停"恒真,
# 与档位无关; 12.00 那根在 F1 的回看窗口之外)。
_PROBE_CLOSE = 10.0
_PROBE_TODAY = 12.0


def _factors_counts_prev_day_as_limit_up(code, prev_close):
    """datasource/factors.py 的 F1 档位探针: K线 [10.0, X, 12.0]。

    F1 的回看是 range(start, len-1), 最后一根("今日")被排除 ⇒ 只有第二根
    [10.0 → X] 参与判断。F1=0 ⇔ 它认为那一天涨停过 ⇔ 它的档位 ≤ (X/10.0 - 1)。"""
    kline = pd.DataFrame({"close": [_PROBE_CLOSE, prev_close, _PROBE_TODAY],
                          "volume": [1.0, 1.0, 1.0]})
    tick = {"lastPrice": _PROBE_TODAY, "lastClose": _PROBE_CLOSE,
            "askPrice": [10.05],           # 未封板 → 绕开 F3(那里另有一套 0.5%/0.2% 口径)
            "bidPrice": [12.0], "bidVol": [1], "timetag": "20260919 09:31:00"}
    detail = {"UpStopPrice": _PROBE_TODAY, "FloatVolume": 1e9}
    out = FactorEngine().compute_factors(code, tick, detail, None, {},
                                         kline=kline)
    return out["F1"]["score"] == 0


def _data_source_counts_as_limit_up(code, last):
    """datasource/data_source.py 的档位探针: 走 UpStopPrice 缺失那条兜底分支
    (get_instruments_bulk 返回空 → up_price=0), 命中 ⇔ 它认为该涨幅是涨停。"""
    ds = DataSource()
    ds.get_instruments_bulk = lambda codes: {}
    ticks = {code: {"lastPrice": last, "lastClose": _PROBE_CLOSE,
                    "askPrice": [last]}}
    return [u["code"] for u in ds.get_limit_up_stocks(ticks)] == [code]


@pytest.mark.parametrize("code", GUARD_CODES)
def test_limit_ratio_consistent_with_exit_rules(code):
    """逐副本**行为**断言(拒绝源码文本扫描): 每份必须逐码等于权威规格。"""
    assert _guard_self_check()
    r = exit_limit_ratio(code)
    assert limit_ratio_for_code(code) == r                  # shared/common.py(镜像)
    assert prism.zt_history._limit_ratio(code) == r         # 涨停池成员那份
    assert prism.paper._limit_ratio(code) == r              # 跌停顺延兜底那份
    assert _TT_VENDOR.limit_ratio_for_code(code) == r       # tt_solo 自包含副本


@pytest.mark.parametrize("code", [c for c in GUARD_CODES if c is not None])
def test_inline_limit_ratio_copies_match_exit_rules(code):
    """档位内联在函数里的两份(factors / data_source): 在权威涨停价的**两侧**
    各探一次 —— 正好到权威价必须算涨停, 差 0.02 元必须不算。任一侧不符 ⇒ 它
    的档位与权威不等(factors 的 0.02 容差是它自己的 -0.01 容差, 不构成两侧同时命中)。

    None 不在本参数里: 那两个函数在别处还会对 code 调 .startswith(与本口径无关),
    None 会在到达档位判断前就 AttributeError —— 拿它当探针测的不是档位。"""
    r = exit_limit_ratio(code)
    limit_px = round(_PROBE_CLOSE * (1 + r), 2)
    assert _factors_counts_prev_day_as_limit_up(code, limit_px) is True
    assert _factors_counts_prev_day_as_limit_up(code, limit_px - 0.02) is False
    assert _data_source_counts_as_limit_up(code, limit_px) is True
    assert _data_source_counts_as_limit_up(code, limit_px - 0.02) is False


_SUFFIX_OF_MARKET = {_MKT_SH: ".SH", _MKT_SZ: ".SZ", _MKT_BJ: ".BJ", None: ""}


@pytest.mark.parametrize("code", [
    "600000", "601398", "000001", "002859", "300001", "301001",
    "688001", "689009", "920001", "830799", "430047", "400001", "420001",
])
def test_tdx_split_code_agrees_with_market_suffix(code):
    """prism/tdx_source._split_code 是**同一批前缀**的交易所判定(不是档位): 它必须与
    shared.common.with_market_suffix 给出同一个交易所, 否则该标的行情会打到别的市场
    (旧实现缺 "92" ⇒ 920xxx 落到兜底 _MKT_SH, 行情取沪市 = 静默取空)。

    88/99 开头是通达信板块指数(该函数刻意另算)、非数字串/None 无市场 —— 不在本守卫内。"""
    bare, mkt = _split_code(code)
    assert bare + _SUFFIX_OF_MARKET[mkt] == with_market_suffix(code)


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
