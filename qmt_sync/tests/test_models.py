from types import SimpleNamespace
from qmt_sync.models import AssetSnapshot, PositionSnapshot, TradeRecord

def test_asset_from_xt():
    xt = SimpleNamespace(account_id="8888", total_asset=123456.0, cash=20000.0,
                         market_value=103456.0, frozen_cash=0.0)
    a = AssetSnapshot.from_xt(xt)
    assert a.total_asset == 123456.0 and a.cash == 20000.0
    assert isinstance(a.update_time, str) and len(a.update_time) == 19

def test_position_from_xt_missing_attr():
    xt = SimpleNamespace(account_id="8888", stock_code="600000.SH", volume=100)
    p = PositionSnapshot.from_xt(xt)  # 缺字段 -> 0, 不抛错
    assert p.volume == 100 and p.market_value == 0.0 and p.open_price == 0.0

def test_trade_from_xt():
    xt = SimpleNamespace(account_id="8888", stock_code="600000.SH", order_type=23,
                         traded_id="t9", traded_time="20260812093000", traded_price=8.5,
                         traded_volume=100, traded_amount=850.0, order_id="o1")
    t = TradeRecord.from_xt(xt)
    assert t.traded_volume == 100 and t.stock_code == "600000.SH"
    assert t.traded_time == "2026-08-12 09:30:00"  # 紧凑时间被规整
    assert t.received_at and len(t.received_at) == 19

def test_trade_from_xt_noncompact_time():
    xt = SimpleNamespace(account_id="8888", stock_code="600000.SH", order_type=23,
                         traded_id="t10", traded_time="2026-08-12 09:30:00", traded_price=8.5,
                         traded_volume=100, traded_amount=850.0, order_id="o1")
    t = TradeRecord.from_xt(xt)
    assert t.traded_time == "2026-08-12 09:30:00"  # 非紧凑时间原样透传
