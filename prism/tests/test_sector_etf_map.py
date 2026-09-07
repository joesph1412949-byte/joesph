# -*- coding: utf-8 -*-
"""sector_etf_map 结构测试 — 全离线(QMT 存在性验证是一次性真跑, 不进测试)。

31 个申万一级行业全覆盖; 有锚点的值形态固定; spec §1 候选基准逐码断言;
无专属/过小 ETF 的行业显式留空 {}(不硬凑)。真实代码的存在性/名称一致性
由 QMT 验证脚本保证(报告留档), 这里只验证结构与既定映射。
"""
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import prism.sector_etf_map as sem


# 申万一级行业 31 个(与 market_data 缓存 sectors 段 801xxx 名称同口径)
SW31 = {
    "农林牧渔", "基础化工", "钢铁", "有色金属", "电子", "家用电器",
    "食品饮料", "纺织服饰", "轻工制造", "医药生物", "公用事业", "交通运输",
    "房地产", "商贸零售", "社会服务", "综合", "建筑材料", "建筑装饰",
    "电力设备", "国防军工", "计算机", "传媒", "通信", "银行", "非银金融",
    "汽车", "机械设备", "煤炭", "石油石化", "环保", "美容护理",
}


def test_covers_all_31_sw_industries():
    assert set(sem.SECTOR_ETF_MAP) == SW31


def test_value_shape():
    """值要么 {} 留空, 要么恰含 code/name 且代码带正确交易所后缀。"""
    for ind, v in sem.SECTOR_ETF_MAP.items():
        assert v == {} or set(v) == {"code", "name"}, (ind, v)
        if v:
            assert re.fullmatch(r"\d{6}\.(SH|SZ)", v["code"]), (ind, v)
            assert isinstance(v["name"], str) and v["name"], (ind, v)


def test_spec_candidates_exact_codes():
    """spec §1 候选基准(实现者经 QMT 验证后不得改码, 改码须改测试并留档)。"""
    expect = {
        # name 以 QMT InstrumentName 为准(2026-09-07 验证, spec 示例"银行ETF"
        # 实际全名"银行ETF华宝")
        "银行": ("512800.SH", "银行ETF华宝"),
        "传媒": ("512980.SH", None),
        "国防军工": ("512660.SH", None),
        "煤炭": ("515220.SH", None),
        "有色金属": ("512400.SH", None),
        "医药生物": ("512010.SH", None),
        "房地产": ("512200.SH", None),
        "通信": ("515880.SH", None),
        "钢铁": ("515210.SH", None),
        "环保": ("512580.SH", None),
        "农林牧渔": ("159865.SZ", None),
        "家用电器": ("159996.SZ", None),
        "非银金融": ("512070.SH", None),
        "电子": ("159997.SZ", None),
        "基础化工": ("159870.SZ", None),
    }
    for ind, (code, name) in expect.items():
        v = sem.SECTOR_ETF_MAP[ind]
        assert v, "行业 %s 不应留空" % ind
        assert v["code"] == code, (ind, v)
        if name:
            assert v["name"] == name, (ind, v)


def test_no_etf_industries_left_empty():
    """无专属/规模过小 ETF 的行业留空 {} — 不硬凑(留空集合按设计拍板)。"""
    for ind in ("纺织服饰", "轻工制造", "综合", "社会服务", "美容护理",
                "商贸零售"):
        assert sem.SECTOR_ETF_MAP[ind] == {}, ind


def test_anchor_disclaimer_in_docstring():
    """锚点口径声明: ETF 跟踪指数与申万行业有偏差, 代表品种性质。"""
    doc = (sem.__doc__ or "")
    assert "申万" in doc
    assert "偏差" in doc or "代表" in doc
