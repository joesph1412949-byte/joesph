# -*- coding: utf-8 -*-
"""选股流程编排：先判市场环境，达标才扫涨停池算分，输出候选清单。
依赖 data_source / factors / manual_store / models，可注入假实现便于测试。"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # 项目根(common.py)
from common import SECTORS

from data_source import DataSource
from eastmoney import EastMoneyFeed
from factors import FactorEngine
from fundamental import FundamentalFeed
from manual_store import ManualStore
from models import ModelScorer

logger = logging.getLogger(__name__)

# 默认科技行业池（用于板块映射 F4/S6, 定义收敛到 common.SECTORS）

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

    def _build_sector_map(self, limit_ups, theme_map=None):
        """涨停池 code → 所属行业/题材。用科技行业板块反查 + 东财题材映射叠加。
        theme_map: 东财涨停池 {裸代码: 题材名}(em["today_theme_map"]), 提供题材维度。
        题材优先(打板逻辑里题材比申万行业更贴近主线), 申万行业兜底。"""
        sector_map = {}
        ups = [u["code"] for u in limit_ups]
        if not ups:
            return sector_map
        # 1) 申万行业兜底
        for sector in SECTORS:
            members = set(self.ds.get_sector_stocks(sector))
            for code in ups:
                if code in members:
                    sector_map.setdefault(code, sector)
        # 2) 东财题材叠加(优先级更高): 裸代码 → 带后缀后覆盖
        if theme_map:
            for code in ups:
                theme = theme_map.get(code) or theme_map.get(code.split(".")[0])
                if theme:
                    sector_map[code] = theme
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
        # 主线题材(东财涨停池按 hybk 聚合) — 供页面展示; em 不可用则为空
        top_themes = (em or {}).get("top_themes") or []
        theme_map = (em or {}).get("today_theme_map") or {}

        result = {
            "market": {
                "node_score": node_score,
                "stage": stage,
                "factors": market_factors,
                "total_amount": total_amount,
                "limit_up_count": len(limit_ups),
                "top_themes": top_themes[:10],   # 主线题材 Top10(涨停家数/连板高度排序)
            },
            "environment_ok": node_score >= ENV_THRESHOLD,
            "candidates": [],
            "summary": {"candidate_count": 0, "a_count": 0, "b_count": 0,
                        "c_count": 0, "d_count": 0},
        }

        # 4. 环境门槛：不达标直接返回
        if not result["environment_ok"]:
            return result

        # 预拉指数K线 + 批量涨停股K线(提速: 避免每只涨停股重复拉取)
        index_kline = None
        try:
            index_kline = self.ds.get_index_kline("000001.SH", days=30)
        except Exception:
            index_kline = None
        kline_map = {}
        if limit_ups:
            try:
                kline_map = self.ds.get_kline_bulk([u["code"] for u in limit_ups], days=250)
            except Exception:
                kline_map = {}

        # 5. 板块映射（供 F4/S6）— 申万行业 + 东财题材(题材优先)
        sector_map = self._build_sector_map(limit_ups, theme_map)

        # 6. 对涨停池每只算因子 + 评分
        for lu in limit_ups:
            code = lu["code"]
            try:
                tick = ticks.get(code, {})
                detail = {"UpStopPrice": lu.get("up_stop_price"),
                          "FloatVolume": lu.get("float_volume")}
                auto_raw = self.engine.compute_factors(code, tick, detail, self.ds,
                                                       sector_map, limit_ups, None,
                                                       kline=kline_map.get(code),
                                                       index_kline=index_kline)
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
