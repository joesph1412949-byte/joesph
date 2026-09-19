import threading
from types import SimpleNamespace
from qmt_sync.db import QmtDb
from qmt_sync.qmt_client import QmtCallback
from qmt_sync.sync import SyncEngine


class FakeClient:
    def __init__(self):
        self.asset = SimpleNamespace(account_id="A", total_asset=100000.0, cash=50000.0,
                                     market_value=50000.0, frozen_cash=0.0)
        # open_price 9.0: 现价 9000/1000=9.0 与成本持平, 避免默认开启的 stop_loss 触发
        self.positions = [SimpleNamespace(account_id="A", stock_code="600000.SH", volume=1000,
                                          can_use_volume=1000, open_price=9.0, market_value=9000.0,
                                          frozen_volume=0, on_road_volume=0, yesterday_volume=1000)]
    def query_asset(self): return self.asset
    def query_positions(self): return self.positions


def test_poll_once_writes_and_alerts(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    eng = SyncEngine(SimpleNamespace(alert_rules={}, poll_interval_s=5.0), db, FakeClient(),
                     now=lambda: "2026-08-12 10:00:00")
    eng.poll_once()
    a = db.latest_asset()
    assert a["total_asset"] == 100000.0
    pos = db.latest_positions()
    assert pos and pos[0]["stock_code"] == "600000.SH"
    # 持仓 9000/100000=9% < 30%, 无告警
    assert db.query_alerts() == []


def test_poll_detects_ratio_alert(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    rules = {"position_ratio": {"enabled": True, "max": 0.05}}
    client = FakeClient()
    eng = SyncEngine(SimpleNamespace(alert_rules=rules, poll_interval_s=5.0), db, client,
                     now=lambda: "2026-08-12 10:00:00")
    eng.poll_once()
    alerts = db.query_alerts()
    assert len(alerts) == 1 and alerts[0]["rule"] == "position_ratio"


def test_on_trade_records(tmp_path):
    db = QmtDb(str(tmp_path / "t.db"))
    eng = SyncEngine(SimpleNamespace(alert_rules={}, poll_interval_s=5.0), db, FakeClient(),
                     now=lambda: "2026-08-12 10:00:00")
    trade = SimpleNamespace(account_id="A", stock_code="600000.SH", order_type=23, traded_id="t1",
                            traded_time="20260812100000", traded_price=9.0, traded_volume=100,
                            traded_amount=900.0, order_id="o1")
    eng.on_trade(trade)
    trades = db.query_trades()
    assert len(trades) == 1 and trades[0]["traded_volume"] == 100


def test_on_trade_from_worker_thread(tmp_path):
    """xtquant 回调在 worker 线程跑: 跨线程写库必须可用。"""
    db = QmtDb(str(tmp_path / "t.db"))
    eng = SyncEngine(SimpleNamespace(alert_rules={}, poll_interval_s=5.0), db, FakeClient(),
                     now=lambda: "2026-08-12 10:00:00")
    trade = SimpleNamespace(account_id="A", stock_code="600000.SH", order_type=23, traded_id="t-thr",
                            traded_time="20260812100000", traded_price=9.0, traded_volume=100,
                            traded_amount=900.0, order_id="o1")
    t = threading.Thread(target=eng.on_trade, args=(trade,))
    t.start()
    t.join()
    trades = db.query_trades()
    assert len(trades) == 1 and trades[0]["traded_id"] == "t-thr"


def test_callback_single_arg_signatures():
    """xtquant dispatcher 以单参数调用回调 on_stock_trade(data)/on_stock_order(data)。"""
    calls = []

    class FakeEngine:
        def on_trade(self, trade): calls.append(("trade", trade))
        def on_order(self, order): calls.append(("order", order))
        def on_order_error(self, err): calls.append(("order_error", err))
        def on_cancel_error(self, err): calls.append(("cancel_error", err))
        def on_disconnect(self): calls.append(("disconnect", None))

    cb = QmtCallback(FakeEngine())
    trade = SimpleNamespace(traded_id="t1")
    order = SimpleNamespace(order_id="o1")
    err = SimpleNamespace(order_id="o1", error_msg="资金不足")
    cb.on_stock_trade(trade)
    cb.on_stock_order(order)
    cb.on_order_error(err)
    cb.on_cancel_error(err)
    cb.on_disconnected()
    assert calls == [("trade", trade), ("order", order), ("order_error", err),
                     ("cancel_error", err), ("disconnect", None)]


def _cfg(rules=None):
    return SimpleNamespace(alert_rules=rules or {}, poll_interval_s=5.0)


def _engine(db, client, clock):
    return SyncEngine(_cfg(), db, client, now=lambda: clock[0])


# ---------- I1: 空/None 持仓 = 本轮无数据, 不是"清仓" ----------

def test_none_positions_round_is_not_a_liquidation(tmp_path):
    """I1: query_positions() 返回 None(QMT 未就绪/断线瞬间) 是明确的查询失败。
    即使资产侧市值也是 0, 也不能当成"全部卖出"。"""
    db = QmtDb(str(tmp_path / "t.db"))
    client = FakeClient()
    clock = ["2026-08-12 10:00:00"]
    eng = _engine(db, client, clock)
    eng.poll_once()
    client.positions = None
    client.asset.market_value = 0.0
    eng.poll_once()
    assert db.query_alerts() == []
    assert eng._prev_codes == {"600000.SH"}  # 空轮不覆盖上一轮持仓
    # 库与告警不自相矛盾: latest_positions 仍是上一轮快照, 不是"空仓"
    assert [r["stock_code"] for r in db.latest_positions()] == ["600000.SH"]


def test_empty_positions_glitch_does_not_swallow_recovery(tmp_path):
    """I1: 空 list 但资产仍有市值 -> 两边不一致, 本轮视为无数据。
    旧行为发假"清仓", 且恢复轮的真"新开仓"被 insert_alert 的 60 分钟同规则同代码
    去重窗吞掉 —— 盘面只剩那条假清仓。修复后一轮抖动+恢复不产生任何持仓变化告警。"""
    db = QmtDb(str(tmp_path / "t.db"))
    client = FakeClient()
    clock = ["2026-08-12 10:00:00"]
    eng = _engine(db, client, clock)
    pos = client.positions[0]
    eng.poll_once()                      # prev = {600000.SH}
    client.positions = []                # 查询抖动(未就绪), 资产市值仍 50000
    eng.poll_once()
    assert db.query_alerts() == []       # 不评估 position_change
    assert eng._prev_codes == {"600000.SH"}
    client.positions = [pos]             # 下一轮恢复: 持仓集合没变 -> 无变化可报,
    eng.poll_once()                      # 更不能冒出"清仓/新开仓"这对自相矛盾的告警
    assert db.query_alerts() == [], [a["message"] for a in db.query_alerts()]


def test_glitch_round_does_not_poison_dedup_window(tmp_path):
    """I1: 假清仓最毒的后果是占掉 60 分钟去重窗, 把随后**真的**清仓告警吞掉。
    抖动轮 -> 真空仓轮(资产市值归零), 真清仓必须报出来。"""
    db = QmtDb(str(tmp_path / "t.db"))
    client = FakeClient()
    clock = ["2026-08-12 10:00:00"]
    eng = _engine(db, client, clock)
    eng.poll_once()                      # prev = {600000.SH}
    client.positions = []                # 抖动: 空但资产市值仍 50000
    eng.poll_once()
    clock[0] = "2026-08-12 10:00:05"
    client.asset.market_value = 0.0      # 真的清空了
    eng.poll_once()
    # 必须是 10:00:05 这条真告警; 旧行为只有 10:00:00 那条抖动轮伪造的(且真告警被去重吞掉)
    assert [(a["message"], a["triggered_at"]) for a in db.query_alerts()] == [
        ("清仓 600000.SH", "2026-08-12 10:00:05")]


def test_real_liquidation_still_alerts(tmp_path):
    """I1 边界判据: 持仓为空 **且** 资产市值归零 -> 用户真的清空了全部持仓, 必须报清仓。"""
    db = QmtDb(str(tmp_path / "t.db"))
    client = FakeClient()
    clock = ["2026-08-12 10:00:00"]
    eng = _engine(db, client, clock)
    eng.poll_once()
    client.positions = []
    client.asset.market_value = 0.0      # 资产侧证实没有市值 -> 真空仓
    client.asset.cash = 100000.0
    eng.poll_once()
    assert [a["message"] for a in db.query_alerts()] == ["清仓 600000.SH"]


# ---------- I2: 数据缺口 + 委托/撤单失败 ----------

def test_data_gap_alert_after_silence(tmp_path):
    """I2: 超过阈值没有有效数据 -> data_gap 告警(复用 insert_alert 的 60 分钟去重窗节流)。"""
    db = QmtDb(str(tmp_path / "t.db"))
    client = FakeClient()
    clock = ["2026-08-12 10:00:00"]
    eng = _engine(db, client, clock)
    eng.poll_once()                         # 心跳正常
    client.asset = None                     # QMT 断线/未就绪
    clock[0] = "2026-08-12 10:00:30"        # 30s < 60s 阈值
    eng.poll_once()
    assert db.query_alerts() == []
    clock[0] = "2026-08-12 10:01:30"        # 90s > 60s 阈值
    eng.poll_once()
    assert [a["rule"] for a in db.query_alerts()] == ["data_gap"]
    # 恢复后心跳重置: 不会再补一条
    client.asset = FakeClient().asset
    clock[0] = "2026-08-12 10:02:00"
    eng.poll_once()
    assert [a["rule"] for a in db.query_alerts()] == ["data_gap"]


def test_data_gap_rule_can_be_disabled(tmp_path):
    """I2: 盘后 QMT 未登录若嫌吵, 用现成的 alert_rules.json 关掉, 不新增配置机制。"""
    db = QmtDb(str(tmp_path / "t.db"))
    client = FakeClient()
    clock = ["2026-08-12 10:00:00"]
    eng = SyncEngine(_cfg({"data_gap": {"enabled": False}}), db, client, now=lambda: clock[0])
    eng.poll_once()
    client.asset = None
    clock[0] = "2026-08-12 10:05:00"
    eng.poll_once()
    assert db.query_alerts() == []


def test_asset_write_failure_counts_toward_data_gap():
    """I3+I2: insert_asset 返回 False(如 database is locked) -> 本轮不算有效数据;
    持续失败必须由 data_gap 告警暴露, 而不是只留一行日志静默丢行。"""
    class FailDb:
        def __init__(self): self.alerts = []
        def insert_asset(self, *a): return False
        def insert_alert(self, rule, code, msg, ts):
            self.alerts.append(rule)
            return True

    db = FailDb()
    clock = ["2026-08-12 10:00:00"]
    eng = _engine(db, FakeClient(), clock)
    eng.poll_once()
    clock[0] = "2026-08-12 10:00:30"
    eng.poll_once()
    assert db.alerts == []
    clock[0] = "2026-08-12 10:01:30"
    eng.poll_once()
    assert db.alerts == ["data_gap"]


def test_order_and_cancel_error_are_recorded(tmp_path):
    """I2: 废单/撤单失败原来在库里完全不可见(orders 表只有正常回报)。"""
    db = QmtDb(str(tmp_path / "t.db"))
    clock = ["2026-08-12 10:00:00"]
    eng = _engine(db, FakeClient(), clock)
    eng.on_order_error(SimpleNamespace(order_id="o9", stock_code="600000.SH",
                                       error_id=-1, error_msg="资金不足"))
    eng.on_cancel_error(SimpleNamespace(order_id="o10", stock_code="600519.SH",
                                        error_id=1, error_msg="该委托已成交"))
    assert sorted(a["rule"] for a in db.query_alerts()) == ["cancel_error", "order_error"]
    rows = db._conn.execute("SELECT order_id,status_msg FROM orders ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["o9", "o10"]
    assert "资金不足" in rows[0][1] and "已成交" in rows[1][1]


def test_disconnect_lands_in_alerts(tmp_path):
    """I2: on_disconnected 原来只写一行 warning, 对话侧/货主完全看不到断线。"""
    db = QmtDb(str(tmp_path / "t.db"))
    eng = _engine(db, FakeClient(), ["2026-08-12 10:00:00"])
    QmtCallback(eng).on_disconnected()
    assert [a["rule"] for a in db.query_alerts()] == ["disconnected"]
