# -*- coding: utf-8 -*-
"""写入侧不再产出 filters.environment_threshold(2026-09-19 决策 (b))。

该键与 market_gate.threshold 是同一个数 —— engine.validate_strategy_payload
从同一个 gt 写出两份(market_gate.threshold 是**唯一读取点**), 全仓零读取点。
既然它只是重复别名, 就**停止声明**: 只从旧 JSON 里删掉不够, 写入侧必须同步,
否则下次网页保存又长回来(本用例钉的就是这一条)。

写入侧 = /api/strategies/create(生产代码里唯一把策略落盘的地方)。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism_web.app as app_module

# 与 test_app.py 的编辑器 payload 同形(最小可用)
_PAYLOAD = {
    "name": "测试组合",
    "models": [{"id": "first_board", "name": "首板", "weight": 1.0,
                "factors": ["F1", "F8"], "weights": [1, 1]}],
    "gate_factors": ["N1"], "gate_threshold": 1,
    "candidate_min_model": 3,
    "sell": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
             "max_hold_days": 5}}


@pytest.fixture
def client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_create_does_not_persist_the_duplicate_key(client, tmp_path,
                                                   monkeypatch):
    """判别力: 保存新策略后落盘文件里**没有** environment_threshold, 其余键一个不少。
    改前 validate_strategy_payload 会写出该键 ⇒ 这条必红。"""
    monkeypatch.setattr(app_module, "STRATEGIES_DIR", tmp_path)
    r = client.post("/api/strategies/create", json=_PAYLOAD)
    assert r.status_code == 200, r.get_json()
    p = tmp_path / ("%s.json" % r.get_json()["id"])
    s = json.loads(p.read_text(encoding="utf-8"))
    assert "environment_threshold" not in s["filters"]
    assert s["filters"]["candidate_min_model"] == 3
    assert s["market_gate"]["threshold"] == 1      # 门槛本体不受影响
