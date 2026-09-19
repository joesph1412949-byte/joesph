from types import SimpleNamespace
from qmt_sync.alerts import evaluate_asset_alerts, evaluate_gap_alert, evaluate_position_alerts

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


def test_liquidation_basis_only_when_fully_empty():
    """I1 文案判据: "全空轮"才注明依据来源。

    部分清仓(还剩别的票)时资产市值不为 0, 若也拼上"market_value≈0"就是**假判据**,
    会误导事后判断 —— 该 guard 是文案正确性的一部分。
    """
    asset = A(total=100000.0)
    rules = {"position_change": {"enabled": True}}
    basis = "券商资产侧 market_value≈0 且持仓为空"
    # 全空 -> 带依据
    full = [h[2] for h in evaluate_position_alerts(asset, [], {"600000.SH"}, rules, basis)]
    assert full == ["清仓 600000.SH(依据: {})".format(basis)]
    # 部分(还剩 600000.SH) -> 不带依据
    part = [h[2] for h in evaluate_position_alerts(asset, [P(code="600000.SH")],
                                                   {"600000.SH", "000001.SZ"}, rules, basis)]
    assert [m for m in part if "清仓" in m] == ["清仓 000001.SZ"]


def test_gap_alert_threshold():
    """I2: 默认阈值 60s(≈12 个默认轮询周期)。未超不报, 超过报一条 data_gap。"""
    rules = {}
    assert list(evaluate_gap_alert("2026-08-12 10:00:00", "2026-08-12 10:01:00", rules)) == []
    hits = list(evaluate_gap_alert("2026-08-12 10:00:00", "2026-08-12 10:01:01", rules))
    assert len(hits) == 1 and hits[0][0] == "data_gap"


def test_gap_alert_configurable_and_fail_safe():
    # 阈值可配(复用现成 alert_rules.json 结构)
    hits = list(evaluate_gap_alert("2026-08-12 10:00:00", "2026-08-12 10:00:10",
                                   {"data_gap": {"max_silence_s": 5}}))
    assert len(hits) == 1
    # 无心跳起点 / 时间不可解析 -> 不误报, 不抛错
    assert list(evaluate_gap_alert(None, "2026-08-12 10:10:00", {})) == []
    assert list(evaluate_gap_alert("bad", "worse", {})) == []
