from types import SimpleNamespace
from qmt_sync.alerts import evaluate_asset_alerts, evaluate_position_alerts

def A(**kw): return SimpleNamespace(account_id="A", total_asset=kw.get("total", 100000.0))
def P(**kw): return SimpleNamespace(stock_code=kw["code"], volume=kw.get("vol", 1000),
                                    open_price=kw.get("cost", 10.0), market_value=kw.get("mv", 10000.0))

def test_daily_loss_triggers():
    asset = A(total=95000.0)
    prev = {"total_asset": 100000.0}
    hits = list(evaluate_asset_alerts(asset, prev, {"daily_loss": {"enabled": True, "max_loss_pct": 0.03}}))
    assert len(hits) == 1 and hits[0][0] == "daily_loss"

def test_daily_loss_ok():
    asset = A(total=99000.0)
    prev = {"total_asset": 100000.0}
    hits = list(evaluate_asset_alerts(asset, prev, {"daily_loss": {"enabled": True, "max_loss_pct": 0.03}}))
    assert hits == []

def test_position_ratio():
    asset = A(total=100000.0)
    pos = [P(code="600000.SH", mv=40000.0, vol=1000, cost=40.0)]  # 40% > 30%
    hits = list(evaluate_position_alerts(asset, pos, set(), {"position_ratio": {"enabled": True, "max": 0.30}}))
    assert hits and hits[0][0] == "position_ratio"

def test_stop_loss():
    asset = A(total=100000.0)
    pos = [P(code="600000.SH", mv=9000.0, vol=1000, cost=10.0)]  # 现价9 vs 成本10 = -10% < -8%
    hits = list(evaluate_position_alerts(asset, pos, set(), {"stop_loss": {"enabled": True, "drop_pct": 0.08}}))
    assert hits and hits[0][0] == "stop_loss"

def test_position_change():
    asset = A(total=100000.0)
    pos = [P(code="600000.SH")]
    hits = list(evaluate_position_alerts(asset, pos, {"600000.SH", "000001.SZ"},
                                         {"position_change": {"enabled": True}}))
    msgs = [h[2] for h in hits]
    assert any("清仓" in m for m in msgs)
