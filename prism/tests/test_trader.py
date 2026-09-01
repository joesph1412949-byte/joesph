# -*- coding: utf-8 -*-
"""Task 7 交易模块测试: 信号生成 / 写入 / 暂停开关 / 盘后流程。

信号文件协议与 qmt_signal_bridge_real.py 的 pending JSON 同构
(桥端读取 order_id/action/stock_code/price/volume/account_id)。
"""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.registry as reg
import prism.trader as trader


def test_generate_signals_shape():
    result = {
        "environment_ok": True,
        "candidates": [{"code": "600000.SH", "name": "浦发", "last": 10.5,
                        "up_stop_price": 10.55, "scores": {"composite": 5.0}}],
    }
    strategy = {"id": "default", "sell_rules": {}}
    sigs = trader.generate_signals(result, strategy, env="sim")
    assert len(sigs) == 1
    s = sigs[0]
    assert s["action"] == "BUY"
    assert s["stock_code"] == "600000.SH"
    assert s["strategy_id"] == "default"
    assert s["status"] == "pending"
    # 桥端消费字段必须齐全(order_id/action/stock_code/price/volume/account_id)
    for key in ("order_id", "action", "stock_code", "price", "volume", "account_id"):
        assert key in s
    assert s["volume"] == 100
    assert s["price"] == 10.55


def test_generate_signals_empty_when_env_bad():
    result = {"environment_ok": False, "candidates": []}
    assert trader.generate_signals(result, {"id": "x"}, env="sim") == []


def test_check_paused(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "PAUSE_FILE", str(tmp_path / "paused"))
    assert trader.check_paused() is False
    (tmp_path / "paused").write_text("", encoding="utf-8")
    assert trader.check_paused() is True


def test_write_signals_writes_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    sigs = [{"order_id": "BUY_1", "action": "BUY", "stock_code": "600000.SH",
             "volume": 100, "price": 10.55, "account_id": "",
             "created_at": "2026-08-14T00:00:00", "status": "pending",
             "strategy_id": "default"}]
    n = trader.write_signals(sigs, env="sim")
    assert n == 1
    pending = tmp_path / "sim" / "pending"
    files = list(pending.glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["order_id"] == "BUY_1"
    assert data["stock_code"] == "600000.SH"
    assert data["action"] == "BUY"


def test_write_signals_env_isolation(tmp_path, monkeypatch):
    """补充: env 目录隔离 — real 写入不得污染 sim。"""
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    sigs = [{"order_id": "BUY_2", "action": "BUY", "stock_code": "000001.SZ",
             "volume": 100, "price": 12.3, "account_id": "",
             "created_at": "2026-08-14T00:00:00", "status": "pending",
             "strategy_id": "default"}]
    trader.write_signals(sigs, env="real")
    assert (tmp_path / "real" / "pending" / "BUY_2.json").exists()
    assert not (tmp_path / "sim" / "pending").exists()


def test_write_signals_empty_returns_zero(tmp_path, monkeypatch):
    """补充: 空信号列表不建目录、返回 0。"""
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    assert trader.write_signals([], env="sim") == 0
    assert not (tmp_path / "sim" / "pending").exists()


def test_run_daily_paused_returns_paused(tmp_path, monkeypatch):
    """补充: 暂停时 run_daily 直接返回 paused=True, 不触碰 provider、不写信号。"""
    monkeypatch.setattr(trader, "PAUSE_FILE", str(tmp_path / "paused"))
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    (tmp_path / "paused").write_text("", encoding="utf-8")
    result = trader.run_daily({"id": "x", "scoring_models": []}, provider=None)
    assert result["paused"] is True
    assert result["environment_ok"] is False
    assert result["candidates"] == []
    assert result["signals_written"] == 0
    assert not (tmp_path / "sim" / "pending").exists()


class _FakeProvider:
    """run_daily 全流程假数据源: 涨停池 + 上下文构建。

    up_price: 标量(所有候选同价)或 {code: up_price} 字典(逐候选定价,
    供混合候选测试)。None/0 表示该候选缺价。
    """

    def __init__(self, limit_ups, up_price=None):
        self._lus = limit_ups
        self._up_price = up_price

    def build_market_context(self):
        from prism.context import FactorContext
        return FactorContext(code="__MARKET__", ticks={},
                             limit_ups=self._lus, em={})

    def get_limit_ups(self):
        return self._lus

    def build_stock_context(self, code):
        from prism.context import FactorContext
        up = self._up_price
        if isinstance(up, dict):
            up = up.get(code)
        return FactorContext(code=code, tick={"lastPrice": 10.5},
                             up_price=up, last=10.5)


@pytest.fixture(autouse=True)
def _factors(tmp_path, monkeypatch):
    """注册 run_daily 全流程所需的门槛/个股因子(NG1-3 市场门槛, AS1 个股)。

    同时隔离真实暂停开关: PAUSE_FILE 统一指向 tmp_path,
    测试不得读取实盘暂停文件 D:/QMT_SIGNALS/paused。
    """
    monkeypatch.setattr(trader, "PAUSE_FILE", str(tmp_path / "paused"))
    reg.reset()
    for fid in ("NG1", "NG2", "NG3"):
        reg.factor(id=fid, name=fid, category="node", description="")(
            lambda ctx, _f=fid: {"score": 1, "note": ""})

    @reg.factor(id="AS1", name="as1", category="stock", description="")
    def f_as1(ctx):
        return {"score": 1, "note": ""}
    yield


def test_run_daily_full_flow(tmp_path, monkeypatch):
    """补充: 盘后完整流程 — 选股 → archive 回调 → 生成信号 → 写入 pending。"""
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    strategy = {
        "id": "flow",
        "market_gate": {"threshold": 3, "factors": ["NG1", "NG2", "NG3"]},
        "scoring_models": [{"id": "m1", "factors": ["AS1"]}],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1},
    }
    provider = _FakeProvider([{"code": "600000.SH"}, {"code": "000001.SZ"}])
    archived = []
    result = trader.run_daily(strategy, provider, env="sim", volume=200,
                              archive=lambda cands: archived.append(cands))
    assert result["paused"] is False
    assert result["environment_ok"] is True
    assert len(result["candidates"]) == 2
    assert result["signals_written"] == 2
    assert len(archived) == 1 and len(archived[0]) == 2
    files = list((tmp_path / "sim" / "pending").glob("*.json"))
    assert len(files) == 2
    for f in files:
        data = json.loads(f.read_text(encoding="utf-8"))
        assert data["action"] == "BUY"
        assert data["volume"] == 200
        assert data["strategy_id"] == "flow"
        assert data["status"] == "pending"


def _flow_strategy():
    """run_daily real 保护测试用策略(与 full_flow 同构)。"""
    return {
        "id": "flow",
        "market_gate": {"threshold": 3, "factors": ["NG1", "NG2", "NG3"]},
        "scoring_models": [{"id": "m1", "factors": ["AS1"]}],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": 1},
    }


def test_generate_signals_price_zero_when_no_up_stop_price():
    """候选缺 up_stop_price 时信号 price=0(桥端按对手价市价单); real 拒单在 run_daily。"""
    result = {
        "environment_ok": True,
        "candidates": [{"code": "600000.SH", "scores": {"composite": 5.0}}],
    }
    sigs = trader.generate_signals(result, {"id": "x"}, env="real")
    assert len(sigs) == 1 and sigs[0]["price"] == 0


def test_run_daily_real_rejects_without_up_stop_price(tmp_path, monkeypatch):
    """补充(审查要求): env=real 且候选全缺 up_stop_price → 拒单并返回 error。"""
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    provider = _FakeProvider([{"code": "600000.SH"}])   # up_price=None
    result = trader.run_daily(_flow_strategy(), provider, env="real")
    assert result["signals_written"] == 0
    assert "error" in result and "up_stop_price" in result["error"]
    assert not (tmp_path / "real" / "pending").exists()


def test_run_daily_real_allows_with_up_stop_price(tmp_path, monkeypatch):
    """补充: env=real 且候选带 up_stop_price → 正常生成信号, price 为涨停价。"""
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    provider = _FakeProvider([{"code": "600000.SH"}], up_price=10.55)
    result = trader.run_daily(_flow_strategy(), provider, env="real")
    assert "error" not in result
    assert result["signals_written"] == 1
    assert result["skipped_no_price"] == 0
    files = list((tmp_path / "real" / "pending").glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["price"] == 10.55


# ---------------- C1(审查 Critical): real 逐候选无价拒单保护 ----------------

def test_run_daily_real_mixed_candidates_skips_no_price(tmp_path, monkeypatch):
    """审查 C1: real 混合候选(1 有价 + 1 缺价)→ 只生成带价候选的信号,
    缺价候选跳过并计入 skipped_no_price, 不再整体拒单。"""
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    provider = _FakeProvider([{"code": "600000.SH"}, {"code": "000001.SZ"}],
                             up_price={"600000.SH": 10.55, "000001.SZ": None})
    result = trader.run_daily(_flow_strategy(), provider, env="real")
    assert "error" not in result
    assert result["signals_written"] == 1
    assert result["skipped_no_price"] == 1
    files = list((tmp_path / "real" / "pending").glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["stock_code"] == "600000.SH"
    assert data["price"] == 10.55
    # 返回候选仍含缺价者(便于盘后追溯)
    assert len(result["candidates"]) == 2


def test_run_daily_real_zero_up_stop_price_treated_missing(tmp_path, monkeypatch):
    """审查 C1: up_stop_price=0 视为缺价 → 全部候选被跳过 → 拒单 + error。"""
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    provider = _FakeProvider([{"code": "600000.SH"}, {"code": "000001.SZ"}],
                             up_price=0)
    result = trader.run_daily(_flow_strategy(), provider, env="real")
    assert result["signals_written"] == 0
    assert result["skipped_no_price"] == 2
    assert "error" in result and "up_stop_price" in result["error"]
    assert not (tmp_path / "real" / "pending").exists()


def test_run_daily_real_mixed_zero_and_priced(tmp_path, monkeypatch):
    """审查 C1: 混合候选(1 价 0 + 1 带价)→ 只生成带价信号, 0 价者跳过。"""
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    provider = _FakeProvider([{"code": "600000.SH"}, {"code": "000001.SZ"}],
                             up_price={"600000.SH": 10.55, "000001.SZ": 0})
    result = trader.run_daily(_flow_strategy(), provider, env="real")
    assert "error" not in result
    assert result["signals_written"] == 1
    assert result["skipped_no_price"] == 1
    files = list((tmp_path / "real" / "pending").glob("*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["stock_code"] == "600000.SH"


def test_run_daily_sim_keeps_no_price_candidates(tmp_path, monkeypatch):
    """审查 C1: sim 环境不检查价格 — 缺价候选照常生成 price=0 信号(桥端 sim 无风险)。"""
    monkeypatch.setattr(trader, "SIGNAL_ROOT", tmp_path)
    provider = _FakeProvider([{"code": "600000.SH"}, {"code": "000001.SZ"}],
                             up_price=None)
    result = trader.run_daily(_flow_strategy(), provider, env="sim")
    assert "error" not in result
    assert result["signals_written"] == 2
    assert result["skipped_no_price"] == 0
    files = list((tmp_path / "sim" / "pending").glob("*.json"))
    assert len(files) == 2


# ---------------- v5 信号透传 sector_score ----------------

def test_generate_signals_passes_sector_score():
    """候选带 sector_score → 透传信号; 不带 → null(协议兼容)。"""
    result = {"environment_ok": True, "candidates": [
        {"code": "600000.SH", "up_stop_price": 10.0,
         "scores": {"composite": 3}, "sector_score": 82.3},
        {"code": "000001.SZ", "up_stop_price": 9.0,
         "scores": {"composite": 2}},
    ]}
    sigs = trader.generate_signals(result, {"id": "s1"})
    assert sigs[0]["sector_score"] == 82.3
    assert sigs[1]["sector_score"] is None
