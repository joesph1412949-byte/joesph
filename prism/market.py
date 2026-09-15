# -*- coding: utf-8 -*-
"""市场情绪分类(等价于旧 datasource.models.ModelScorer.classify_market)。

网页 /api/screen 的 market.stage 使用本函数。刻意不 import datasource
(双份模块问题, 见 Task 8 审查 I2) —— prism 侧单源实现, 语义与旧站一致:
5=高潮期 / 4=回暖期 / 3=冰点期 / <3=退潮期。
"""


def classify_market(node_score):
    """节点模型得分 → 情绪阶段。5=高潮 4=回暖 3=冰点 2以下=退潮。"""
    if node_score >= 5:
        return "高潮期"
    if node_score == 4:
        return "回暖期"
    if node_score == 3:
        return "冰点期"
    return "退潮期"
