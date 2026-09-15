# -*- coding: utf-8 -*-
"""qmt_signal_bridge_real 安全闸门单元测试 — 授权文件机制 + 当日去重。
桥脚本顶层无副作用(只定义常量/函数, 无 __main__ 自测), 可安全 import。
测试只动 tmp_path 下的文件, 绝不触碰真实 D:/QMT_SIGNALS。"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))  # 项目根

from qmt.bridge import signal_bridge_real as bridge


# ---------- 授权文件机制 ----------

def test_armed_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(bridge, "ARMED_FILE", str(tmp_path / "armed.txt"))
    ok, msg = bridge._is_armed()
    assert ok is False
    assert "missing" in msg


def test_armed_dated_today(monkeypatch, tmp_path):
    f = tmp_path / "armed.txt"
    f.write_text(datetime.now().strftime("%Y%m%d"), encoding="utf-8")
    monkeypatch.setattr(bridge, "ARMED_FILE", str(f))
    ok, msg = bridge._is_armed()
    assert ok is True
    assert "armed" in msg


def test_armed_stale_date(monkeypatch, tmp_path):
    f = tmp_path / "armed.txt"
    f.write_text("20200101", encoding="utf-8")   # 过期日期
    monkeypatch.setattr(bridge, "ARMED_FILE", str(f))
    ok, msg = bridge._is_armed()
    assert ok is False
    assert "not dated today" in msg


def test_armed_empty_content(monkeypatch, tmp_path):
    f = tmp_path / "armed.txt"
    f.write_text("", encoding="utf-8")
    monkeypatch.setattr(bridge, "ARMED_FILE", str(f))
    ok, _ = bridge._is_armed()
    assert ok is False


# ---------- 当日去重 ----------

def test_duplicate_detection_after_mark(monkeypatch, tmp_path):
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    sig = {"stock_code": "600000.SH", "order_id": "BUY_1"}
    placed = bridge._load_placed()
    assert bridge._already_placed_today(sig, placed) is False
    bridge._mark_placed_today(sig, placed)
    placed2 = bridge._load_placed()
    assert bridge._already_placed_today(sig, placed2) is True


def test_duplicate_distinct_codes_not_blocked(monkeypatch, tmp_path):
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    sig_a = {"stock_code": "600000.SH"}
    sig_b = {"stock_code": "000001.SZ"}
    placed = bridge._load_placed()
    bridge._mark_placed_today(sig_a, placed)
    placed2 = bridge._load_placed()
    assert bridge._already_placed_today(sig_a, placed2) is True
    assert bridge._already_placed_today(sig_b, placed2) is False


def test_duplicate_bare_code_suffix_normalized(monkeypatch, tmp_path):
    # 同一股票裸代码/带后缀写法 → 视作同一只 → 去重命中
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    sig = {"stock_code": "600000"}          # 裸代码
    sig2 = {"stock_code": "600000.SH"}      # 带后缀
    placed = bridge._load_placed()
    bridge._mark_placed_today(sig, placed)
    placed2 = bridge._load_placed()
    assert bridge._already_placed_today(sig2, placed2) is True


def test_duplicate_rolls_over_new_day(monkeypatch, tmp_path):
    # 去重记录带日期: 昨天的记录不应拦截今天的下单(按当天日期取)
    dedup = tmp_path / "placed_today.json"
    monkeypatch.setattr(bridge, "DEDUP_FILE", str(dedup))
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    sig = {"stock_code": "600000.SH"}
    placed = bridge._load_placed()
    bridge._mark_placed_today(sig, placed)
    # 手动把记录日期改到昨天 → 不再算"今日已下单"
    today = datetime.now().strftime("%Y%m%d")
    yesterday = (datetime.now().date().toordinal() - 1)
    import datetime as _dt
    yesterday_key = _dt.date.fromordinal(yesterday).strftime("%Y%m%d")
    placed2 = bridge._load_placed()
    assert today in placed2
    # 把今天的记录"挪"到昨天
    placed2[yesterday_key] = placed2.pop(today)
    assert bridge._already_placed_today(sig, placed2) is False


# ---------- 价格合理性校验(fat-finger 防护) ----------

def test_price_absurd_rejected(monkeypatch):
    # 手误价格(如 999999.99) → 拒单, 不进 passorder
    monkeypatch.setattr(bridge, "DRY_RUN", True)   # 即便 dry-run 也先做 sanity 校验
    ok, msg = bridge._call_passorder(
        {"stock_code": "600000.SH", "action": "BUY", "price": 999999.99, "volume": 100})
    assert ok is False
    assert "out of range" in msg


def test_price_negative_rejected(monkeypatch):
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    ok, msg = bridge._call_passorder(
        {"stock_code": "600000.SH", "action": "BUY", "price": -5.0, "volume": 100})
    assert ok is False
    assert "out of range" in msg


def test_price_sane_accepted_under_dry_run(monkeypatch):
    # 正常涨停价(如 20.50)在范围内 → dry-run 通过(返回 DRY_RUN 提示)
    monkeypatch.setattr(bridge, "DRY_RUN", True)
    monkeypatch.setattr(bridge, "FIXED_ACCOUNT", "88869979")
    monkeypatch.setattr(bridge, "ADD_MARKET_SUFFIX", True)
    ok, msg = bridge._call_passorder(
        {"stock_code": "600000.SH", "action": "BUY", "price": 20.50, "volume": 100})
    assert ok is True
    assert "DRY_RUN" in msg
