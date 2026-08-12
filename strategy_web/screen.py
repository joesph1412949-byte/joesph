# -*- coding: utf-8 -*-
"""选股流程编排：先判市场环境，达标才扫涨停池算分，输出候选清单。
依赖 data_source / factors / manual_store / models，可注入假实现便于测试。"""
import logging

from data_source import DataSource
from eastmoney import EastMoneyFeed
from factors import FactorEngine
from fundamental import FundamentalFeed
from manual_store import ManualStore
from models import ModelScorer

logger = logging.getLogger(__name__)

# 默认科技行业池（用于板块映射 F4/S6）
SECTORS = ["SW1电子", "SW1计算机", "SW1通信"]

# 环境门槛：节点模型得分达标才选股（冰点期阈值）
ENV_THRESHOLD = 3
# 个股门槛：最强模型分(首板/妖股/势能任一)低于此值的不进候选清单
CANDIDATE_MIN_MODEL = 3


class ScreenRunner:
    def __init__(self, ds=None, engine=None, store=None, scorer=None,
                 em_feed=None, fund_feed=None):
        self.ds = ds or DataSource()
        self.engine = engine or FactorEngine()
        self.store = store or ManualStore()
        self.scorer = scorer or ModelScorer()
        self.em_feed = em_feed or EastMoneyFeed()
        self.fund_feed = fund_feed or FundamentalFeed()

    def _build_sector_map(self, limit_ups):
        """涨停池 code → 所属行业。用科技行业板块反查。"""
        sector_map = {}
        ups = [u["code"] for u in limit_ups]
        if not ups:
            return sector_map
        for sector in SECTORS:
            members = set(self.ds.get_sector_stocks(sector))
            for code in ups:
                if code in members:
                    sector_map.setdefault(code, sector)
        return sector_map

    def run(self):
        """完整选股流程。返回结果字典（见模块 docstring）。"""
        # 1. 全市场行情 + 涨停池
        ticks = self.ds.get_full_market_ticks()
        limit_ups = self.ds.get_limit_up_stocks(ticks)

        # 2. 东财涨停池真数据(N1/N3/N4) — 尽力而为, 失败则 em=None 走代理兜底
        em = None
        try:
            em = self.em_feed.get_market_stats()
        except Exception:
            em = None

        # 3. 市场环境因子 N1-N5
        market_factors = self.engine.compute_market_factors(self.ds, ticks, limit_ups, em)
        node_score = sum(1 for n in ["N1","N2","N3","N4","N5"]
                         if market_factors.get(n, {}).get("score") == 1)
        stage = self.scorer.classify_market(node_score)
        total_amount = sum((t.get("amount") or 0) for t in ticks.values())

        result = {
            "market": {
                "node_score": node_score,
                "stage": stage,
                "factors": market_factors,
                "total_amount": total_amount,
                "limit_up_count": len(limit_ups),
            },
            "environment_ok": node_score >= ENV_THRESHOLD,
            "candidates": [],
            "summary": {"candidate_count": 0, "a_count": 0, "b_count": 0,
                        "c_count": 0, "d_count": 0},
        }

        # 4. 环境门槛：不达标直接返回
        if not result["environment_ok"]:
            return result

        # 5. 板块映射（供 F4/S6）
        sector_map = self._build_sector_map(limit_ups)

        # 6. 对涨停池每只算因子 + 评分
        for lu in limit_ups:
            code = lu["code"]
            try:
                tick = ticks.get(code, {})
                detail = {"UpStopPrice": lu.get("up_stop_price"),
                          "FloatVolume": lu.get("float_volume")}
                auto_raw = self.engine.compute_factors(code, tick, detail, self.ds,
                                                       sector_map, limit_ups, None)
                # 拍扁: compute_factors 返回 {因子:{score,note}}, merge/score 需扁平 {因子:0/1}
                auto = {k: (v.get("score", 0) if isinstance(v, dict) else v)
                        for k, v in auto_raw.items()}
            except Exception as e:
                logger.warning("选股 %s 自动因子计算失败, 按空因子处理: %r", code, e)
                auto = {}
            # 合并手填因子 + 东财个股因子(Y1/Y5/F7/Y7/S5/Y6/Y2), 失败自动跳过 → 回落手填
            float_mv = (lu.get("float_volume") or 0) * (lu.get("last") or 0)
            try:
                fund = self.fund_feed.compute_for_stock(code, float_mv=float_mv)
            except Exception as e:
                # 东财因子整体失败 → 按空因子处理, 绝不 500
                logger.warning("东财个股因子 %s 计算失败, 按空因子处理: %r", code, e)
                fund = {}
            auto_plus = dict(auto)
            auto_plus.update({k: (v.get("score") if isinstance(v, dict) else 0)
                              for k, v in fund.items()})
            factors = self.store.merge(auto_plus, code)
            scores = self.scorer.score_stock(factors)
            # 来源: QMT自动 / 东财fundamental / 手填manual
            auto_manual = {
                f: ("auto" if f in auto else
                    "fundamental" if f in fund else "manual")
                for f in factors
            }
            result["candidates"].append({
                "code": code, "name": lu.get("name") or code,
                "last": lu.get("last"), "up_stop_price": lu.get("up_stop_price"),
                "sealed": lu.get("sealed"), "float_mv": float_mv,
                "scores": scores, "factors": factors, "auto_manual": auto_manual,
            })

        # 7. 个股达标门槛: 最强模型分低于门槛的不进清单 (spec v2 §3)
        result["candidates"] = [
            c for c in result["candidates"]
            if max(c["scores"]["first_board"], c["scores"]["monster"],
                   c["scores"]["momentum"]) >= CANDIDATE_MIN_MODEL
        ]

        # 8. 综合分从高到低排序 (spec §4②)
        result["candidates"].sort(key=lambda c: c["scores"]["composite"], reverse=True)

        # 9. 汇总
        c = result["candidates"]
        result["summary"] = {
            "candidate_count": len(c),
            "a_count": sum(1 for x in c if x["scores"]["grade"] == "A"),
            "b_count": sum(1 for x in c if x["scores"]["grade"] == "B"),
            "c_count": sum(1 for x in c if x["scores"]["grade"] == "C"),
            "d_count": sum(1 for x in c if x["scores"]["grade"] == "D"),
        }
        return result
