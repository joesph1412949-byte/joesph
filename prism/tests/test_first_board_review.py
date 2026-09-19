# -*- coding: utf-8 -*-
"""首板盘后深度拆解 测试(spec 2026-09-18)。

全部纯函数/假 deps, 不触网、不依赖 QMT 在线。
"""
import pytest

from prism import first_board_review as fbr


# ---------------------------------------------------------------- 工具

def _rec(**kw):
    """构造 BoardRecord, 只覆盖关心的字段。"""
    base = {"code": "600000.SH", "name": "某股",
            "seal_time": "09:35", "open_times": 0, "one_word": False,
            "on_board_amt": 1.2e8, "float_mv": 60e8,
            "amount": 3e8, "turnover": 0.08, "vol_ratio": 2.3,
            "prev5_avg_amt": 1.25e8,
            "sector_name": "半导体", "sector_zt_count": 7,
            "sector_stage": "启动期", "sector_stage_note": None,
            "sector_r5": 3.2, "seal_amount": 1.5e8, "sources": {}}
    base.update(kw)
    return base


# ---------------------------------------------------------------- A 封板质量

class TestScoreSeal:
    def test_early_seal_high(self):
        s = fbr.score_seal(_rec(seal_time="09:31", open_times=0))
        assert s["score"] >= 90

    def test_late_seal_lower(self):
        early = fbr.score_seal(_rec(seal_time="09:31"))
        late = fbr.score_seal(_rec(seal_time="10:30"))
        assert early["score"] > late["score"]

    def test_one_word_full(self):
        s = fbr.score_seal(_rec(seal_time="09:30", one_word=True, open_times=0))
        assert s["score"] == 100

    def test_open_times_penalty(self):
        clean = fbr.score_seal(_rec(open_times=0))
        dirty = fbr.score_seal(_rec(open_times=3))
        assert clean["score"] > dirty["score"]

    def test_missing_data_unavailable(self):
        s = fbr.score_seal(_rec(seal_time=None, open_times=None,
                                one_word=None, on_board_amt=None))
        assert s["available"] is False
        assert s["score"] is None      # 不是 0 —— 0 会被当成"已评估且很差"


# ---------------------------------------------------------------- B 板块共振

class TestScoreSector:
    def test_strong_sector(self):
        assert fbr.score_sector(_rec(sector_zt_count=7))["score"] >= 90

    def test_solo_limit_up_is_pulse(self):
        s = fbr.score_sector(_rec(sector_zt_count=1))
        assert s["score"] <= 35

    def test_monotonic(self):
        scores = [fbr.score_sector(_rec(sector_zt_count=n))["score"]
                  for n in (1, 2, 4, 7)]
        assert scores == sorted(scores)

    def test_missing_unavailable(self):
        s = fbr.score_sector(_rec(sector_zt_count=None))
        assert s["available"] is False


# ---------------------------------------------------------------- C 量能结构

class TestScoreVolume:
    def test_healthy_ratio(self):
        assert fbr.score_volume(_rec(vol_ratio=2.3))["score"] >= 80

    def test_shrink_low(self):
        assert fbr.score_volume(_rec(vol_ratio=0.7, amount=6e7))["score"] <= 45

    def test_extreme_ratio_lower(self):
        healthy = fbr.score_volume(_rec(vol_ratio=2.3))
        crazy = fbr.score_volume(_rec(vol_ratio=8.0))
        assert healthy["score"] > crazy["score"]

    def test_missing_unavailable(self):
        s = fbr.score_volume(_rec(vol_ratio=None, amount=None, prev5_avg_amt=None))
        assert s["available"] is False


# ---------------------------------------------------------------- D 资金行为

class TestScoreFund:
    def test_with_seal_amount(self):
        s = fbr.score_fund(_rec(seal_amount=1.5e8, float_mv=60e8))
        assert s["available"] is True
        assert s["score"] is not None

    def test_without_seal_amount_uses_proxy_and_flags(self):
        s = fbr.score_fund(_rec(seal_amount=None, on_board_amt=1.2e8,
                                float_mv=60e8))
        # 有代理数据 → 可评估, 但必须标明是代理, 不是真封单
        assert s["available"] is True
        assert "代理" in " ".join(s["evidence"])

    def test_no_data_at_all_unavailable(self):
        s = fbr.score_fund(_rec(seal_amount=None, on_board_amt=None, float_mv=None))
        assert s["available"] is False
        assert s["score"] is None


# ---------------------------------------------------------------- E 产业逻辑

class TestScoreIndustry:
    def test_uses_top_score(self):
        s = fbr.score_industry(_rec(top_score=85))
        assert s["score"] == 85

    def test_default_neutral_when_missing(self):
        s = fbr.score_industry(_rec(top_score=None))
        assert s["score"] == 60      # 主观维默认中性
        assert s["available"] is True


# ---------------------------------------------------------------- 汇总: 权重归一

class TestTotalScore:
    def test_all_present(self):
        dims = {k: {"score": 80, "available": True}
                for k in ("seal", "sector", "volume", "fund", "industry")}
        total, label = fbr.total_score(dims)
        assert total == 80
        assert label == "高"

    def test_missing_dim_renormalizes_not_zero(self):
        """封单缺失 → 权重从分母剔除, 其余归一; 绝不能当 0 分拉低总分。"""
        dims = {"seal": {"score": 90, "available": True},
                "sector": {"score": 90, "available": True},
                "volume": {"score": 90, "available": True},
                "fund": {"score": None, "available": False},
                "industry": {"score": 60, "available": True}}
        total, label = fbr.total_score(dims)
        # 若按 0 分算会远低于 85; 归一化后应接近 90 区间
        assert total >= 80
        assert label == "高"

    def test_all_missing_no_crash(self):
        dims = {k: {"score": None, "available": False}
                for k in ("seal", "sector", "volume", "fund", "industry")}
        total, label = fbr.total_score(dims)
        assert total is None
        assert label == "数据不足"

    def test_confidence_bands(self):
        def mk(v):
            return {k: {"score": v, "available": True} for k in fbr.WEIGHTS}
        assert fbr.total_score(mk(80))[1] == "高"
        assert fbr.total_score(mk(65))[1] == "中"
        assert fbr.total_score(mk(45))[1] == "低"

    def test_pulse_label_overrides(self):
        """板块共振极弱 → 跟风脉冲, 即使总分不低。"""
        dims = {k: {"score": 80, "available": True} for k in fbr.WEIGHTS}
        dims["sector"] = {"score": 30, "available": True}
        total, label = fbr.total_score(dims)
        assert label == "跟风脉冲"


# ---------------------------------------------------------------- analyze

class TestAnalyze:
    def test_returns_five_dims(self):
        out = fbr.analyze(_rec())
        assert set(out["dims"]) == {"seal", "sector", "volume", "fund", "industry"}
        assert isinstance(out["total"], int)
        assert out["confidence"] in ("高", "中", "低", "跟风脉冲", "数据不足")

    def test_evidence_is_list_of_str(self):
        out = fbr.analyze(_rec())
        for d in out["dims"].values():
            assert isinstance(d["evidence"], list)
            assert all(isinstance(x, str) for x in d["evidence"])


# ---------------------------------------------------------------- 采集层

class TestCollect:
    def _deps(self, pool, intraday=None, smap=None, kline=None):
        return {"zt_feed": lambda d: pool,
                "intraday": (lambda code, day: (intraday or {}).get(code)),
                "sector_map": smap or {},
                "kline_batch": lambda codes, day: (kline or {}),
                "float_mv_batch": lambda codes: {},
                "snapshot": lambda code: None}

    def test_filters_first_board_only(self):
        pool = [{"code": "600001.SH", "boards": 1},
                {"code": "600002.SH", "boards": 3},
                {"code": "600003.SH", "boards": 1}]
        recs = fbr.collect("20260918", deps=self._deps(pool))
        assert [r["code"] for r in recs] == ["600001.SH", "600003.SH"]

    def test_missing_one_minute_does_not_crash(self):
        pool = [{"code": "600001.SH", "boards": 1}]
        recs = fbr.collect("20260918", deps=self._deps(pool))
        assert recs[0]["seal_time"] is None
        assert recs[0]["sources"]["seal_time"] == "unknown"

    def test_sector_count_from_pool(self):
        pool = [{"code": "600001.SH", "boards": 1},
                {"code": "600002.SH", "boards": 2},
                {"code": "600003.SH", "boards": 1}]
        smap = {"600001.SH": "半导体", "600002.SH": "半导体",
                "600003.SH": "煤炭"}
        recs = fbr.collect("20260918", deps=self._deps(pool, smap=smap))
        by = {r["code"]: r for r in recs}
        # 板块内涨停家数含连板(600002 也是同板块涨停)
        assert by["600001.SH"]["sector_zt_count"] == 2
        assert by["600003.SH"]["sector_zt_count"] == 1

    def test_empty_pool_returns_empty(self):
        assert fbr.collect("20260918", deps=self._deps([])) == []


# ---------------------------------------------------------------- 覆盖层

class TestApplyManual:
    def test_manual_overrides_and_marks(self):
        recs = [_rec(code="600001.SH", seal_amount=None)]
        recs[0]["sources"] = {"seal_amount": "unknown"}
        out = fbr.apply_manual(recs, {"600001.SH": {"seal_amount": 2e8}})
        assert out[0]["seal_amount"] == 2e8
        assert out[0]["sources"]["seal_amount"] == "manual"

    def test_no_manual_is_noop(self):
        recs = [_rec(code="600001.SH")]
        assert fbr.apply_manual(recs, None) == recs


# ---------------------------------------------------------------- 报告

class TestRenderReport:
    def test_contains_code_and_total(self):
        pool = [{"code": "600001.SH", "boards": 1}]
        deps = {"zt_feed": lambda d: pool, "intraday": lambda c, d: None,
                "sector_map": {}, "kline_batch": lambda c, d: {},
                "float_mv_batch": lambda c: {}, "snapshot": lambda c: None}
        recs = fbr.collect("20260918", deps=deps)
        md = fbr.render_report("20260918", recs)
        assert "600001.SH" in md
        assert "封板质量" in md

    def test_marks_unknown_fields(self):
        pool = [{"code": "600001.SH", "boards": 1}]
        deps = {"zt_feed": lambda d: pool, "intraday": lambda c, d: None,
                "sector_map": {}, "kline_batch": lambda c, d: {},
                "float_mv_batch": lambda c: {}, "snapshot": lambda c: None}
        recs = fbr.collect("20260918", deps=deps)
        md = fbr.render_report("20260918", recs)
        assert "未知" in md

    def test_empty_records_message(self):
        md = fbr.render_report("20260918", [])
        assert "无首板" in md


# ---------------------------------------------------------------- run 落盘

class TestRun:
    def test_skips_persist_when_empty(self, tmp_path, monkeypatch):
        """空日 + persist_when_empty=False → 不落盘(免污染日期列表)。"""
        monkeypatch.setattr(fbr, "collect", lambda day: [])
        recs, paths = fbr.run("20260101", out_dir=tmp_path,
                              persist_when_empty=False)
        assert recs == []
        assert paths == {}
        assert list(tmp_path.glob("*.json")) == []

    def test_persists_normally(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fbr, "collect",
                            lambda day: [_rec(code="600001.SH")])
        # 报告目录必须隔离: 否则会覆盖 docs/reports/ 里的真实报告
        monkeypatch.setattr(fbr, "_REPORT_DIR", tmp_path / "reports")
        recs, paths = fbr.run("20260918", out_dir=tmp_path,
                              persist_when_empty=False)
        assert len(recs) == 1
        assert "json" in paths
        assert (tmp_path / "first_board_review_20260918.json").exists()
