# -*- coding: utf-8 -*-
"""状态机与持久化测试。"""
import json
import threading
from datetime import datetime

import pytest

from ttcore import _vendor
from ttcore.state import Ledger


def test_new_ledger_is_today(ledger):
    s = ledger.load()
    assert s["date"] == "2026-09-14"
    assert s["symbols"] == {}


def test_record_fill_sell_then_buy_pairs_pnl(ledger):
    """正T: 先卖后买, 价差为收益。"""
    ledger.load()
    ledger.record_fill("600900.SH", "SELL", 28.239, 500, hhmm="10:00")
    s = ledger.sym("600900.SH")
    assert s["sold_today"] == 500
    assert s["realized_pnl"] == 0.0            # 卖出未配对, 无盈亏

    ev = ledger.record_fill("600900.SH", "BUY", 27.94, 500, hhmm="14:00")
    # (28.239 - 27.94) * 500 = 149.5
    assert ev["matched_qty"] == 500
    assert abs(ev["pnl"] - 149.5) < 1e-6
    assert ledger.sym("600900.SH")["bought_today"] == 500
    assert ledger.sym("600900.SH")["trips"] == 1


def test_record_fill_buy_then_sell_pairs_pnl(ledger):
    """反T: 先买后卖, 卖出时结出价差。"""
    ledger.load()
    ledger.record_fill("600900.SH", "BUY", 27.90, 300)
    assert ledger.sym("600900.SH")["realized_pnl"] == 0.0
    ev = ledger.record_fill("600900.SH", "SELL", 28.30, 300)
    assert ev["matched_qty"] == 300
    assert abs(ev["pnl"] - (28.30 - 27.90) * 300) < 1e-6


def test_partial_pairing_leaves_remainder(ledger):
    ledger.load()
    ledger.record_fill("600900.SH", "SELL", 28.00, 500)
    ev = ledger.record_fill("600900.SH", "BUY", 27.50, 300)
    assert ev["matched_qty"] == 300
    s = ledger.sym("600900.SH")
    # 卖出剩 200 股留在队列里(净卖出敞口)
    assert s["sell_queue"] == [[28.00, 200]]
    assert s["bought_today"] - s["sold_today"] == -200


def test_fifo_multi_lot(ledger):
    ledger.load()
    ledger.record_fill("600900.SH", "BUY", 10.0, 100)
    ledger.record_fill("600900.SH", "BUY", 12.0, 100)
    ev = ledger.record_fill("600900.SH", "SELL", 15.0, 200)
    # FIFO: (15-10)*100 + (15-12)*100 = 500 + 300 = 800
    assert abs(ev["pnl"] - 800.0) < 1e-6


def test_loss_recorded_negative(ledger):
    ledger.load()
    ledger.record_fill("600900.SH", "SELL", 28.00, 100)
    ev = ledger.record_fill("600900.SH", "BUY", 28.50, 100)
    assert ev["pnl"] < 0
    assert ledger.total_realized_pnl() < 0


def test_daily_roll_resets_on_new_day(tmp_path):
    t = {"now": datetime(2026, 9, 14, 10, 0)}
    led = Ledger(path=tmp_path / "s.json", now_fn=lambda: t["now"])
    led.load()
    led.record_fill("600900.SH", "SELL", 28.0, 500)
    assert led.sym("600900.SH")["sold_today"] == 500

    t["now"] = datetime(2026, 9, 15, 10, 0)
    assert led.roll_if_new_day() is True
    assert led.state["date"] == "2026-09-15"
    assert led.sym("600900.SH", create=False) is None       # 新日干净
    assert led.daily_trades() == 0


def test_load_ignores_stale_day(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"version": 1, "date": "2020-01-01",
                             "symbols": {"X": {"sold_today": 999}},
                             "events": []}), encoding="utf-8")
    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 10, 0))
    led.load()
    assert led.state["date"] == "2026-09-14"
    assert "X" not in led.state["symbols"]


def test_load_recovers_from_corrupt_file(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("{ this is not json", encoding="utf-8")
    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 10, 0))
    led.load()
    assert led.state["date"] == "2026-09-14"
    assert led.state["symbols"] == {}


def test_persistence_roundtrip(tmp_path):
    t = {"now": datetime(2026, 9, 14, 10, 0)}
    p = tmp_path / "s.json"
    led = Ledger(path=p, now_fn=lambda: t["now"])
    led.load()
    led.record_fill("600900.SH", "SELL", 28.0, 100)
    led.set_ref("600900.SH", 28.09)
    led.set_units("600900.SH", "SELL", 2)

    led2 = Ledger(path=p, now_fn=lambda: t["now"])
    led2.load()
    assert led2.sym("600900.SH")["sold_today"] == 100
    assert led2.get_ref("600900.SH") == 28.09
    assert led2.get_units("600900.SH", "SELL") == 2


def test_atomic_write_leaves_no_tmp(tmp_path):
    p = tmp_path / "s.json"
    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 10, 0))
    led.load()
    led.save()
    assert p.exists()
    # tmp 名带 pid+线程(`s.json.tmp.<pid>.<tid>`): 旧字面量 `s.json.tmp` 的断言
    # 恒真 = 没守卫; 改成真守卫: 目录里除目标文件外没有任何残留
    assert [q.name for q in tmp_path.iterdir() if q.name != "s.json"] == []


def test_events_capped(ledger):
    ledger.load()
    from ttcore.state import MAX_EVENTS
    for _ in range(MAX_EVENTS + 40):
        ledger.record_fill("600900.SH", "SELL", 28.0, 100)
    assert len(ledger.state["events"]) <= MAX_EVENTS


def test_save_survives_concurrent_reader(tmp_path, monkeypatch):
    """M12(tt 侧): 账本文件被**读者句柄**持有期间 save → N 轮全零异常。

    `ttcore/_vendor.py::atomic_write` 是 tt_solo 自包含的**第三份**原子写拷贝
    (刻意不 import shared/prism), 同样撞 Windows 的"目标被读者句柄占用 →
    `os.replace` EACCES":CPython 的 `open()` 不带 `FILE_SHARE_DELETE`, 只要
    有读者句柄在, `MoveFileEx(REPLACE_EXISTING)` 就拿不到 DELETE 权限。
    实测来源:本轮全量跑 `test_events_capped` 就因此红过一次(WinError 5, 那次
    没有读者线程 —— 外部句柄同样会挡); 有读者时修复前 16~22/300 轮抛错。

    每一轮用 Event 握手钉死时序: 读者先打开并持有句柄(面板/守护读账本的真实
    形态) → 写者 save, 第一次 replace **必然**撞上它(先断言真的撞上了) →
    读者放手(预算内) → 写者必须靠重试落地, 零异常。不用"定时持有/放手的读者
    跑 300 轮":那种读者被抢占时会握着句柄下 CPU, 能饿死任何**有界**预算
    (全量跑实测 flake 2/300)。"""
    p = tmp_path / "tt_state.json"
    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 10, 0))
    led.load()
    led.save()

    holding, release = threading.Event(), threading.Event()
    collided = threading.Event()
    real_replace = _vendor.os.replace

    def reader():
        with open(p, "rb") as f:
            f.read()
            holding.set()
            release.wait(10)           # 读者处理这份数据: 句柄一直开着

    def watched_replace(src, dst):
        try:
            return real_replace(src, dst)
        except PermissionError:
            collided.set()             # 只观察, 不改行为
            raise

    monkeypatch.setattr(_vendor.os, "replace", watched_replace)

    for rnd in range(10):
        holding.clear()
        release.clear()
        collided.clear()
        r = threading.Thread(target=reader, name="R%d" % rnd)
        r.start()
        assert holding.wait(10), "第 %d 轮编排失败: 读者没打开账本文件" % rnd

        errs = []

        def writer():
            try:
                led.save()
            except Exception as exc:   # PermissionError(WinError 5/32)
                errs.append(repr(exc))

        w = threading.Thread(target=writer, name="W%d" % rnd)
        w.start()
        try:
            assert collided.wait(10), (
                "第 %d 轮编排失败: 写者第一次 replace 没撞上读者句柄 —— 本轮"
                "没有制造出 EACCES, 断言等于没跑" % rnd)
        finally:
            release.set()              # 读者放手(在重试预算之内)
        w.join(10)
        r.join(10)

        assert errs == [], ("第 %d 轮: 读者在预算内放手后 save 必须成功: %s"
                            % (rnd, errs))
        assert json.loads(p.read_text(encoding="utf-8"))["date"] == "2026-09-14"


def test_snapshot_shape(ledger):
    ledger.load()
    ledger.record_fill("600900.SH", "SELL", 28.0, 500)
    ledger.record_fill("600900.SH", "BUY", 27.5, 500)
    snap = ledger.snapshot()
    assert snap["date"] == "2026-09-14"
    s = snap["symbols"]["600900.SH"]
    assert s["sold_today"] == 500
    assert s["bought_today"] == 500
    assert s["net_exposure"] == 0
    assert snap["total_trips"] == 1
    assert snap["daily_trades"] == 2


def test_units_ops(ledger):
    ledger.load()
    assert ledger.get_units("X.SH", "SELL") == 0
    ledger.set_units("X.SH", "SELL", 3)
    assert ledger.get_units("X.SH", "SELL") == 3


def test_archive_writes_one_line_on_roll(tmp_path, now_fn):
    """跨日重置前, 前一日的账本摘要须追加到 history.jsonl。"""
    from datetime import datetime
    from ttcore.state import Ledger

    day1 = lambda: datetime(2026, 9, 14, 10, 0, 0)
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=day1)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    assert led.archive_current() is True
    lines = (tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    import json
    row = json.loads(lines[0])
    assert row["date"] == "2026-09-14"
    assert row["sold_total"] == 100
    assert "realized_pnl" in row


def test_archive_skips_empty_ledger(tmp_path, now_fn):
    """空账本不产生归档行(避免跨日被反复写垃圾行)。"""
    from ttcore.state import Ledger
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    led.load()
    assert led.archive_current() is False
    assert not (tmp_path / "tt_history.jsonl").exists()


def test_archive_is_idempotent_per_date(tmp_path, now_fn):
    """同一天重复调用只归档一次。"""
    from datetime import datetime
    from ttcore.state import Ledger
    day1 = lambda: datetime(2026, 9, 14, 10, 0, 0)
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=day1)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    assert led.archive_current() is True
    assert led.archive_current() is False
    assert len((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()) == 1


def test_archive_failure_does_not_raise(tmp_path, now_fn, monkeypatch):
    """归档异常必须被吞掉 —— 绝不影响交易主流程。

    注: 归档自终审修订起走 append(open/flush/fsync, 不再读-改-写整file),
    所以故障注入点从 atomic_write 移到 fsync —— 断言与意图不变。
    """
    from ttcore import state as st
    led = st.Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(st.os, "fsync", boom)
    assert led.archive_current() is False        # 不抛异常


def test_load_archives_stale_day_before_reset(tmp_path):
    """load() 发现旧日账本时, 必须先归档再重置(否则历史永久丢失)。"""
    import json
    from datetime import datetime
    from ttcore.state import Ledger

    p = tmp_path / "tt_state.json"
    p.write_text(json.dumps({
        "version": 1, "date": "2026-09-14",
        "updated_at": "2026-09-14T15:00:00",
        "symbols": {"600900.SH": {"sold_today": 200, "bought_today": 100,
                                  "trips": 1, "realized_pnl": 33.0}},
        "events": [],
    }), encoding="utf-8")

    day2 = lambda: datetime(2026, 9, 15, 10, 0, 0)
    led = Ledger(path=p, now_fn=day2)
    led.load()
    assert led.state["date"] == "2026-09-15"          # 已重置
    row = json.loads((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip())
    assert row["date"] == "2026-09-14"
    assert row["sold_total"] == 200


def test_read_history_returns_rows(tmp_path, now_fn):
    from datetime import datetime
    from ttcore.state import Ledger
    day1 = lambda: datetime(2026, 9, 14, 10, 0, 0)
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=day1)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    led.archive_current()
    rows = Ledger.read_history(tmp_path / "tt_state.json")
    assert len(rows) == 1 and rows[0]["date"] == "2026-09-14"


def test_corrupt_history_does_not_break_trading_path(tmp_path, now_fn):
    """history.jsonl 损坏(非 UTF-8)不得让账本构造/记账失败 —— 归档是旁路。

    __init__ 会读一次归档来建幂等集合, 这一读在交易路径上, 必须 fail-open。
    """
    from ttcore.state import Ledger
    (tmp_path / "tt_history.jsonl").write_bytes(b"\xff\xfe\x00 not utf8")
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    assert led.sym("600900.SH")["sold_today"] == 100


def test_roll_if_new_day_archives_previous_day(tmp_path):
    """roll_if_new_day → reset_day 这条重置路径也必须先归档(否则历史丢失)。"""
    import json
    from datetime import datetime
    from ttcore.state import Ledger

    t = {"now": datetime(2026, 9, 14, 10, 0)}
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=lambda: t["now"])
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                    reason="档位1", order_id="TT_1")
    t["now"] = datetime(2026, 9, 15, 10, 0)
    assert led.roll_if_new_day() is True
    assert led.state["date"] == "2026-09-15"          # 已重置
    row = json.loads((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip())
    assert row["date"] == "2026-09-14"
    assert row["sold_total"] == 100


def test_fresh_instance_does_not_re_archive(tmp_path, now_fn):
    """幂等靠读文件, 不只靠内存 —— 另开一个实例(守护/面板各持一个)不重复写。"""
    from ttcore.state import Ledger
    p = tmp_path / "tt_state.json"
    a = Ledger(path=p, now_fn=now_fn)
    a.load()
    a.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00",
                  reason="档位1", order_id="TT_1")
    assert a.archive_current() is True
    b = Ledger(path=p, now_fn=now_fn)                  # 新实例
    b.load()
    assert b.archive_current() is False
    assert len((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()) == 1


# ---------------- 复审修复(Task 3 review findings) ----------------

def _hist_rows(tmp_path):
    """读归档行(下面几个用例共用)。"""
    p = tmp_path / "tt_history.jsonl"
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip()]


def test_same_day_version_mismatch_does_not_pin_date(tmp_path):
    """同日但版本不符的账本不得归档 —— 否则半日行钉死今天, 日终真行永不落盘。

    触发场景: 盘中 STATE_VERSION 升版 / 手工修过 state 文件。
    """
    t = {"now": datetime(2026, 9, 14, 10, 0)}
    p = tmp_path / "tt_state.json"
    p.write_text(json.dumps({
        "version": 2, "date": "2026-09-14",              # 同日, 仅版本不符
        "updated_at": "2026-09-14T10:00:00",
        "symbols": {"600900.SH": {"sold_today": 999, "bought_today": 0,
                                  "trips": 9, "realized_pnl": 9.99}},
        "events": [],
    }), encoding="utf-8")

    led = Ledger(path=p, now_fn=lambda: t["now"])
    led.load()
    assert led.state["date"] == "2026-09-14"
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:30",
                    reason="档位1", order_id="TT_1")

    t["now"] = datetime(2026, 9, 15, 10, 0)              # 收盘 → 跨日
    assert led.roll_if_new_day() is True

    same = [r for r in _hist_rows(tmp_path) if r["date"] == "2026-09-14"]
    assert len(same) == 1                                # 只有一行, 且是真行
    assert same[0]["sold_total"] == 100                  # 不是 999 的半日行


def test_same_day_manual_reset_does_not_pin_date(tmp_path):
    """同日手动 reset_day 不归档 —— 今天还没完结, 归档位不能被提前占掉。"""
    t = {"now": datetime(2026, 9, 14, 10, 0)}
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=lambda: t["now"])
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00", reason="档位1")
    led.reset_day()                                      # 同日手动重置
    led.record_fill("600900.SH", "SELL", 28.5, 40, hhmm="11:00", reason="档位1")

    t["now"] = datetime(2026, 9, 15, 10, 0)
    assert led.roll_if_new_day() is True
    same = [r for r in _hist_rows(tmp_path) if r["date"] == "2026-09-14"]
    assert len(same) == 1
    assert same[0]["sold_total"] == 40


def test_load_archives_stale_day_with_events_only(tmp_path):
    """load() 的"非空账本"口径须与 archive_current 一致: 只有 events 也算有料。"""
    p = tmp_path / "tt_state.json"
    p.write_text(json.dumps({
        "version": 1, "date": "2026-09-14", "updated_at": "2026-09-14T15:00:00",
        "symbols": {},
        "events": [{"hhmm": "10:00", "code": "600900.SH", "side": "SELL",
                    "price": 28.5, "volume": 100, "pnl": 0.0}],
    }), encoding="utf-8")

    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 15, 10, 0))
    led.load()
    assert led.state["date"] == "2026-09-15"
    row = _hist_rows(tmp_path)[0]
    assert row["date"] == "2026-09-14"
    assert row["trades"] == 1


def test_read_history_survives_corrupt_archive(tmp_path):
    """归档文件损坏(非 UTF-8)时 read_history 返回 [] —— 与 _load_archived_dates 同款 fail-safe。"""
    (tmp_path / "tt_history.jsonl").write_bytes(b"\xff\xfe\x00 not utf8")
    assert Ledger.read_history(tmp_path / "tt_state.json") == []


# ---------------- §B 回归修复(findings 1-2) ----------------

def test_same_day_version_mismatch_preserves_original_file(tmp_path):
    """同日仅版本不符: 不归档, 但也不准就地覆盖 —— 原账本须改名留档。

    场景: 收盘后升版重启(正常发版窗口)或手工改过 state 文件。曾直接
    _empty_state + save 把原始数据盖掉, 于是第二天无料可归档, 当天在
    history 里彻底消失且不可恢复。
    """
    p = tmp_path / "tt_state.json"
    original = {
        "version": 2, "date": "2026-09-14",              # 同日, 仅版本不符
        "updated_at": "2026-09-14T15:00:00",
        "symbols": {"600900.SH": {"sold_today": 800, "bought_today": 300,
                                  "trips": 1, "realized_pnl": 120.0}},
        "events": [{"hhmm": "10:00", "code": "600900.SH", "side": "SELL"}],
    }
    p.write_text(json.dumps(original), encoding="utf-8")

    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 15, 10))
    led.load()

    bak = tmp_path / "tt_state.json.pre-v2-2026-09-14.bak"
    assert bak.exists(), "同日版本不符的账本被直接覆盖, 原始数据丢失"
    assert json.loads(bak.read_text(encoding="utf-8")) == original
    assert led.state["date"] == "2026-09-14"             # 照常重置为新账本
    assert led.state["symbols"] == {}
    assert _hist_rows(tmp_path) == []                    # 今天没被钉上半日行


@pytest.mark.parametrize("raw", ["[]", "5", '"x"', "null"])
def test_non_dict_state_json_is_preserved(tmp_path, raw):
    """合法 JSON 但**非 dict** 的账本也要改名留档, 不许就地覆盖。

    旧实现只在 `isinstance(raw, dict)` 时才走留档分支 —— 非 dict 直接落到
    "重置为新账本 + save()", 把盘上那份原始内容就地盖掉, 与 load() 自己的
    注释("结构不符 → 改名留档")矛盾。取证副本是事故后唯一能复盘的东西。
    """
    p = tmp_path / "tt_state.json"
    p.write_text(raw, encoding="utf-8")

    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 15, 10))
    led.load()

    baks = list(tmp_path.glob("*.bak"))
    assert len(baks) == 1, "非 dict 账本被直接覆盖, 原始数据丢失"
    assert baks[0].read_text(encoding="utf-8") == raw
    assert led.state["date"] == "2026-09-14"        # 照常重置为新账本
    assert led.state["symbols"] == {}
    assert _hist_rows(tmp_path) == []               # 今天没被钉上半日行


def test_non_dict_state_preserve_failure_does_not_block_load(tmp_path,
                                                             monkeypatch):
    """非 dict 的留档失败也必须只是丢备份, 不许拦住载入。"""
    p = tmp_path / "tt_state.json"
    p.write_text("[]", encoding="utf-8")

    def boom(self, target):
        raise RuntimeError("disk full")
    monkeypatch.setattr(type(p), "replace", boom)

    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 15, 10))
    led.load()
    assert led.state["date"] == "2026-09-14"
    assert led.state["symbols"] == {}


def test_preserve_failure_does_not_block_load(tmp_path, monkeypatch):
    """留档失败(盘满/无权限)只能丢备份, 绝不许拦住账本载入 —— 与归档同款 fail-safe。"""
    p = tmp_path / "tt_state.json"
    p.write_text(json.dumps({
        "version": 2, "date": "2026-09-14",
        "symbols": {"600900.SH": {"sold_today": 800}}, "events": [],
    }), encoding="utf-8")

    def boom(self, target):
        raise RuntimeError("disk full")        # 非 OSError: load 外层不会顺手吞掉
    monkeypatch.setattr(type(p), "replace", boom)

    led = Ledger(path=p, now_fn=lambda: datetime(2026, 9, 14, 15, 10))
    led.load()                                           # 不抛异常
    assert led.state["date"] == "2026-09-14"
    assert led.state["symbols"] == {}


def test_archived_dates_ignore_scalar_line(tmp_path, now_fn):
    """归档文件混入标量行不得毒掉整个日期集合 —— 否则已归档日被写第二遍。"""
    (tmp_path / "tt_history.jsonl").write_text(
        '{"date": "2026-09-14", "sold_total": 100}\n5\n', encoding="utf-8")
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00", reason="档位1")
    assert led.archive_current() is False
    assert len((tmp_path / "tt_history.jsonl").read_text(
        encoding="utf-8").strip().splitlines()) == 2     # 没多写第二行


def test_read_history_skips_scalar_line(tmp_path):
    """read_history 遇到标量行只跳过该行, 不整份丢弃。"""
    (tmp_path / "tt_history.jsonl").write_text(
        '5\nnull\n{"date": "2026-09-14", "sold_total": 100}\n', encoding="utf-8")
    rows = Ledger.read_history(tmp_path / "tt_state.json")
    assert len(rows) == 1 and rows[0]["date"] == "2026-09-14"


def test_archive_row_payload_values(tmp_path):
    """归档行的字段值须与账本事实一致(仪表盘收益曲线直接消费这些值)。"""
    led = Ledger(path=tmp_path / "tt_state.json",
                 now_fn=lambda: datetime(2026, 9, 14, 10, 0))
    led.load()
    # 600900.SH 反T: 买 300@27.90 → 卖 300@28.30, 配对 300, pnl=(28.30-27.90)*300=120.0
    led.record_fill("600900.SH", "BUY", 27.90, 300, hhmm="10:00", reason="档位1")
    led.record_fill("600900.SH", "SELL", 28.30, 300, hhmm="10:30", reason="档位1")
    # 601398.SH 只卖 500@5.00, 未配对 → pnl 0
    led.record_fill("601398.SH", "SELL", 5.00, 500, hhmm="11:00", reason="档位2")

    assert led.archive_current() is True
    row = _hist_rows(tmp_path)[0]
    assert row["date"] == "2026-09-14"
    assert row["sold_total"] == 800                  # 300 + 500
    assert row["bought_total"] == 300
    assert row["trips"] == 1                         # 仅 600900.SH 配对上
    assert row["realized_pnl"] == 120.0
    assert row["trades"] == 3                        # events 条数
    assert row["symbols"] == {"600900.SH": 120.0, "601398.SH": 0.0}
    assert row["archived_at"]                        # 时间戳存在即可(非本用例重点)


# ---------------- 终审修复: 只读账本(I1) + 归档 append/去重(I2) ----------------

def _stale_ledger(day="2026-09-14", sold=800):
    return {
        "version": 1, "date": day, "updated_at": day + "T15:00:00",
        "symbols": {"600900.SH": {"sold_today": sold, "bought_today": 300,
                                  "trips": 1, "realized_pnl": 120.0}},
        "events": [],
    }


def test_readonly_ledger_stale_load_writes_nothing(tmp_path):
    """`writable=False` 的账本 load() 跨日不得归档/改名/落盘。

    这是"监控面绝不改交易状态"的根: 面板以前只是 `led.path = None` 堵住
    save(), 而 load() 的归档走的是 history_path, plan() 还能经
    roll_if_new_day → reset_day 再归档一次 —— 两处都绕过了那个 hack。
    """
    p = tmp_path / "tt_state.json"
    p.write_text(json.dumps(_stale_ledger()), encoding="utf-8")
    before = p.read_bytes()

    led = Ledger(path=p, writable=False,
                 now_fn=lambda: datetime(2026, 9, 15, 10, 0))
    led.load()

    assert p.read_bytes() == before                 # 原账本一字未动
    assert not (tmp_path / "tt_history.jsonl").exists()   # 没归档
    assert list(tmp_path.glob("*.bak")) == []             # 没改名留档
    assert led.state["date"] == "2026-09-15"              # 内存里按今天算
    assert led.state["symbols"] == {}                     # 旧账本不冒充今日事实


def test_readonly_ledger_roll_and_archive_are_noops(tmp_path):
    """只读模式的 archive/reset/save 一律不落盘, 翻页只在内存里发生。

    内存翻页是刻意的: 盘上账本是昨天的 → 面板该显示"今天从零开始", 而不是
    拿昨天的成交冒充今天(那会让重算出的档位/净敞口全错)。
    """
    t = {"now": datetime(2026, 9, 14, 10, 0)}
    led = Ledger(path=tmp_path / "tt_state.json", writable=False,
                 now_fn=lambda: t["now"])
    led.load()
    led.sym("600900.SH")["sold_today"] = 500        # 内存里造料
    assert led.archive_current() is False           # 不写归档
    t["now"] = datetime(2026, 9, 15, 10, 0)
    assert led.roll_if_new_day() is True            # 内存翻页
    assert led.state["date"] == "2026-09-15" and led.state["symbols"] == {}
    led.set_units("600900.SH", "SELL", 2)           # 内部会调 save()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00")
    assert not (tmp_path / "tt_state.json").exists()
    assert not (tmp_path / "tt_history.jsonl").exists()


def test_readonly_ledger_reads_todays_file(tmp_path):
    """只读 != 不读: 当日有效账本必须被读进来(面板要显示真实账本)。"""
    p = tmp_path / "tt_state.json"
    p.write_text(json.dumps(_stale_ledger(day="2026-09-15")),
                 encoding="utf-8")
    before = p.read_bytes()
    led = Ledger(path=p, writable=False,
                 now_fn=lambda: datetime(2026, 9, 15, 10, 0))
    led.load()
    assert led.sym("600900.SH")["sold_today"] == 800
    assert led.snapshot()["total_realized_pnl"] == 120.0
    assert p.read_bytes() == before                 # 读到也不回写(updated_at 不变)


def test_readonly_ledger_does_not_rename_version_mismatch(tmp_path):
    """同日版本不符 → 只读模式不改名留档(.bak 也是写), 只是不认它。"""
    p = tmp_path / "tt_state.json"
    p.write_text(json.dumps({"version": 99, "date": "2026-09-15",
                             "symbols": {"600900.SH": {"sold_today": 800}},
                             "events": []}), encoding="utf-8")
    before = p.read_bytes()
    led = Ledger(path=p, writable=False,
                 now_fn=lambda: datetime(2026, 9, 15, 10, 0))
    led.load()
    assert p.read_bytes() == before
    assert list(tmp_path.glob("*.bak")) == []
    assert led.state["symbols"] == {}               # 不认版本不符的账本


def test_readonly_ledger_missing_file_creates_nothing(tmp_path):
    """盘上没账本时, 只读账本不得"顺手建一个空的"。"""
    led = Ledger(path=tmp_path / "tt_state.json", writable=False,
                 now_fn=lambda: datetime(2026, 9, 15, 10, 0))
    led.load()
    assert led.state["date"] == "2026-09-15"
    assert list(tmp_path.iterdir()) == []


def test_archive_appends_even_if_history_has_garbage(tmp_path):
    """归档是 append: 文件里混了非 UTF-8 垃圾时, 新行仍必须写进去。

    读-改-写会先 `read_text(utf-8)` 在这行上抛 UnicodeDecodeError, 异常被
    fail-safe 吞掉后**这一天的归档行直接丢了**(历史静默缺一天, 收益曲线断点);
    读者本来就按行容错(坏行跳过), 所以 append 严格更安全。
    """
    hist = tmp_path / "tt_history.jsonl"
    hist.write_bytes(b'{"date": "2026-09-13", "sold_total": 7}\n\xff\xfe bad\n')
    led = Ledger(path=tmp_path / "tt_state.json",
                 now_fn=lambda: datetime(2026, 9, 14, 10, 0))
    led.load()
    led.record_fill("600900.SH", "SELL", 28.5, 100, hhmm="10:00", reason="档位1")
    assert led.archive_current() is True
    assert b'"date": "2026-09-14"' in hist.read_bytes()


def test_read_history_dedupes_duplicate_dates(tmp_path):
    """同日多行(两个写入者各追加一次)只算一次 —— 前端收益曲线是累加的。

    幂等判断靠 __init__ 读一次得到的日期集合, 两个写入者可能都认为"没归档过",
    于是同一天落两行。重复日会**静默翻倍**当日盈亏, 正是归档要防的事。
    """
    (tmp_path / "tt_history.jsonl").write_text(
        '{"date": "2026-09-14", "sold_total": 100, "realized_pnl": 10.0}\n'
        '{"date": "2026-09-15", "sold_total": 200, "realized_pnl": 20.0}\n'
        '{"date": "2026-09-14", "sold_total": 999, "realized_pnl": 99.0}\n',
        encoding="utf-8")
    rows = Ledger.read_history(tmp_path / "tt_state.json")
    assert [r["date"] for r in rows] == ["2026-09-14", "2026-09-15"]
    assert rows[0]["realized_pnl"] == 99.0          # 保留最后一次(重写的那行)
    assert rows[1]["realized_pnl"] == 20.0


def test_read_history_limit_counts_distinct_days(tmp_path):
    """limit 按"天"算, 不被重复行挤占(否则图上少画几天)。"""
    (tmp_path / "tt_history.jsonl").write_text(
        '{"date": "2026-09-14", "realized_pnl": 1.0}\n'
        '{"date": "2026-09-15", "realized_pnl": 2.0}\n'
        '{"date": "2026-09-14", "realized_pnl": 9.0}\n',
        encoding="utf-8")
    rows = Ledger.read_history(tmp_path / "tt_state.json", limit=1)
    assert [r["date"] for r in rows] == ["2026-09-15"]
