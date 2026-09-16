# -*- coding: utf-8 -*-
"""tt_solo 做T策略核心(自包含, 不依赖 prism/shared)。

模块职责:
    _vendor  底座内联(原子写/涨跌停比例/分级护栏/路径根)
    grid     网格纯逻辑(零 IO)
    risk     风控闸门(零 IO)
    state    日账本 + 日终归档
    market   行情与指标
    broker   miniQMT 账户只读
    engine   决策编排
    executor 直连下单
    config   配置加载与校验
    daemon   守护
"""
__version__ = "1.0.0"
