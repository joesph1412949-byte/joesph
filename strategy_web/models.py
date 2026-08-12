# -*- coding: utf-8 -*-
"""三模型个股评分(首板/妖股/势能) + 节点(仅市场闸门) + 综合分 + 组合分级 + 强弱区间。
综合分 = first_board*0.30 + monster*0.30 + momentum*0.25 (最高 5.95);
分级用 best/second 绝对阈值; 强弱区间纯按最强模型分, 与节点无关。"""

# 模型权重(节点已退出个股综合分, 只当市场闸门)
MODEL_WEIGHTS = {
    "first_board": 0.30,
    "monster": 0.30,
    "momentum": 0.25,
}

# 因子归属
MODEL_FACTORS = {
    "first_board": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"],
    "monster": ["Y1", "Y2", "Y3", "Y4", "Y5", "Y6", "Y7"],
    "momentum": ["S1", "S2", "S3", "S4", "S5", "S6", "S7"],
    "node": ["N1", "N2", "N3", "N4", "N5"],
}


class ModelScorer:
    def score_stock(self, factors):
        """输入完整因子字典 {因子: 0/1}，返回模型评分结果。缺失因子按0计。
        综合分 = first_board*0.30 + monster*0.30 + momentum*0.25 (最高 5.95);
        分级: A best≥6 / B best≥5 且 second≥3 / C best≥4 / D best≥3 / else E;
        强弱区间纯按最强模型分, 与节点无关。"""
        model_scores = {}
        for model, names in MODEL_FACTORS.items():
            model_scores[model] = sum(1 for n in names if factors.get(n) == 1)

        fb = model_scores["first_board"]
        mo = model_scores["monster"]
        mom = model_scores["momentum"]
        nd = model_scores["node"]

        composite = (fb * MODEL_WEIGHTS["first_board"] +
                     mo * MODEL_WEIGHTS["monster"] +
                     mom * MODEL_WEIGHTS["momentum"])

        # 组合分级 A-E（绝对阈值，节点只当市场闸门）
        best = max(fb, mo, mom)
        second = sorted([fb, mo, mom])[-2]
        if best >= 6:
            grade = "A"
        elif best >= 5 and second >= 3:
            grade = "B"
        elif best >= 4:
            grade = "C"
        elif best >= 3:
            grade = "D"
        else:
            grade = "E"

        # 强弱区间（节点退出后，纯按最强模型分）
        if best >= 6:
            strength, position = "极强", "仓位上限75%"
        elif best >= 5:
            strength, position = "强", "仓位上限50%"
        elif best >= 4:
            strength, position = "中等", "仓位上限30%"
        else:
            strength, position = "弱", "观察/空仓"

        return {
            "first_board": fb, "monster": mo, "momentum": mom, "node": nd,
            "composite": round(composite, 2),
            "grade": grade, "strength": strength, "position": position,
        }

    def classify_market(self, node_score):
        """节点模型得分 → 情绪阶段。5=高潮 4=回暖 3=冰点 2以下=退潮"""
        if node_score >= 5:
            return "高潮期"
        if node_score == 4:
            return "回暖期"
        if node_score == 3:
            return "冰点期"
        return "退潮期"
