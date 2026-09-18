# -*- coding: utf-8 -*-
"""1 分钟特征层(规格 §5) — 回测 F2(早封板) / F3(封单强度·分钟级代理) 离线测试。

覆盖:
  ① features_for: 读按月 JSON 缓存(tmp 目录夹具 + monkeypatch FEATURE_DIR),
     缓存缺失/脏文件 → None(因子 fail-open 0, 不造假)
  ② compute_features 纯算法: 首封时间/开板次数(连续多根算一次)/收盘封/
     一字板/板上量额(首封至收盘累加, 与探针 probe-1m-seal.py 同口径)
  ③ download_features: 离线(桩掉 xtdata 取数) → 按月原子落盘 + 幂等跳过 +
     早于 2025-09-15 的日期连下载都不发起(1m 不可回溯)
  ④ F2 复活: 回测注入 sealed=True + tick.timetag=首封毫秒 → 命中; 10:30 不命中
  ⑤ F3 代理分支: bt_seal_ratio≤T 命中且 note 含"回测代理"; >T 不命中;
     sealed=False 时代理绝不触发(防板上额=0 假"封得结实"); 无 bt_seal_ratio
     → 原实盘口径(bidVol)不变
  ⑥ build_day_feed(use_intraday=True): 从特征缓存合成 stock[code] 的
     tick/sealed/bt_seal_ratio; 缓存缺失静默降级; **绝不触发下载**(网页安全)
  ⑦ 报告 data_notes: day_feed 携带的覆盖说明进回测报告
  ⑧ M6: 1m 说明写明"缺 one_word → 不拦(未知), 计入 filter_stats.one_word_unknown"

全离线: 不调 QMT/网络(真 QMT 调用只在 bt_intraday 的下载函数里)。
"""
import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import pytest

import prism.factors  # noqa: F401  触发真实因子注册(F2/F3 参与断言)
import prism.registry as reg
import backtest.cli as cli  # noqa: E402
import prism.bt_intraday as bti  # noqa: E402  RED 时 ImportError = 特性缺失
from prism import backtest  # noqa: E402
from prism._utils import _parse_timetag_hhmm  # noqa: E402
from prism.context import FactorContext  # noqa: E402
from prism.engine import load_strategy  # noqa: E402

DAY = date(2026, 7, 7)          # 600000.SH 首板日(涨停池当天只有它)
FD8 = "20260707"


def _ms(d, hh, mm):
    """d 日 hh:mm 的毫秒 epoch(实盘 tick.timetag 同型)。"""
    return int(datetime(d.year, d.month, d.day, hh, mm).timestamp() * 1000)


def _write_month(feature_dir, ym, payload):
    """按月 JSON 缓存夹具(与生产同一 atomic_write 路径)。"""
    from shared.common import atomic_write
    feature_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(feature_dir / ("%s.json" % ym),
                 json.dumps(payload, ensure_ascii=False))


def _mk_strategy(scoring=("F1",), gate=("N1",), threshold=1, min_model=1):
    return load_strategy({
        "id": "bt_intraday", "name": "1m特征回测", "description": "",
        "market_gate": {"model": "node", "threshold": threshold,
                        "factors": list(gate)},
        "scoring_models": [
            {"id": "m1", "name": "M1", "weight": 1.0,
             "factors": [{"id": f, "op": ">", "threshold": 0}
                         for f in scoring]},
        ],
        "composite": {"mode": "sum"},
        "filters": {"candidate_min_model": min_model,
                    "environment_threshold": 1},
        "sell_rules": {"take_profit_pct": 0.01, "stop_loss_pct": 0.5,
                       "max_hold_days": 5},
    })


def _bt(strategy, zt, kf):
    """零成本回测器: 只验语义, 不让滑点/税费混淆断言。"""
    return backtest.Backtester(strategy, zt_feed=zt, kline_feed=kf,
                               fee_rate=0.0, slippage=0.0, stamp_duty=0.0,
                               transfer_fee=0.0)


def _pool_zt(days, code="600000.SH", boards=1):
    def zf(d):
        return [{"code": code, "boards": boards}] if d in days else []
    return zf


_KLINE2 = [("2026-07-01", 10.0), ("2026-07-02", 10.5), ("2026-07-03", 11.0),
           ("2026-07-04", 11.2), ("2026-07-05", 11.4), ("2026-07-06", 11.6),
           ("2026-07-07", 11.0), ("2026-07-08", 11.2), ("2026-07-09", 11.4)]


@pytest.fixture(autouse=True)
def _real_factors():
    """真实因子库在场: 本文件的断言直接验 F2/F3 复活效果。"""
    reg.reset()
    reg.scan_factors("prism.factors", force=True)
    yield


# ---------------- ① features_for 读缓存 ----------------

def test_features_for_reads_monthly_cache(monkeypatch, tmp_path):
    """① 命中缓存返回特征 dict(拷贝); 日期三形态可传; 缺失 → None。"""
    fd = tmp_path / "bt_intraday"
    feat = {"limit_price": 11.0, "first_seal_hm": "09:32", "opened": True,
            "open_times": 1, "sealed_close": True, "one_word": False,
            "on_board_amt": 3.94e8, "on_board_vol": 36000.0, "bars": 240}
    _write_month(fd, "2026-09", {"605577.SH": {"2026-09-04": feat}})
    monkeypatch.setattr(bti, "FEATURE_DIR", fd)
    assert bti.features_for("605577.SH", "2026-09-04") == feat
    assert bti.features_for("605577.SH", "20260904") == feat
    assert bti.features_for("605577.SH", date(2026, 9, 4)) == feat
    # 缓存缺失(该日/该股/该月文件都没有) → None
    assert bti.features_for("605577.SH", "2026-09-05") is None
    assert bti.features_for("000001.SZ", "2026-09-04") is None
    assert bti.features_for("605577.SH", "2025-09-01") is None
    # 拷贝: 调用方原地改不污染缓存
    got = bti.features_for("605577.SH", "2026-09-04")
    got["first_seal_hm"] = "XX"
    assert bti.features_for("605577.SH", "2026-09-04")["first_seal_hm"] == "09:32"


def test_features_for_corrupt_month_file_returns_none(monkeypatch, tmp_path):
    """① 脏缓存文件 → None 而不是炸(回测不该被半个坏 JSON 打断)。"""
    fd = tmp_path / "bt_intraday"
    fd.mkdir(parents=True)
    (fd / "2026-09.json").write_text("{oops", encoding="utf-8")
    monkeypatch.setattr(bti, "FEATURE_DIR", fd)
    assert bti.features_for("605577.SH", "2026-09-04") is None


# ---------------- ② compute_features 纯算法 ----------------

def test_compute_features_first_seal_open_and_onboard():
    """② 首封/开板/回封/收盘封 + 板上量额=首封至收盘累加(含开板时段, 探针口径)。"""
    rows = [
        ("09:30", 10.5, 10.0, 10.2, 100_000.0, 1_000_000.0),
        ("09:31", 11.0, 10.9, 11.0, 50_000.0, 550_000.0),    # 首封
        ("09:45", 11.0, 10.4, 10.4, 30_000.0, 330_000.0),   # 开板
        ("10:00", 11.0, 11.0, 11.0, 20_000.0, 220_000.0),   # 回封
        ("15:00", 11.0, 11.0, 11.0, 10_000.0, 110_000.0),   # 收盘仍封
    ]
    f = bti.compute_features(rows, 11.0)
    assert f["limit_price"] == 11.0
    assert f["first_seal_hm"] == "09:31"
    assert f["opened"] is True and f["open_times"] == 1
    assert f["sealed_close"] is True and f["one_word"] is False
    assert f["on_board_amt"] == pytest.approx(1_210_000.0)   # 55+33+22+11 万
    assert f["on_board_vol"] == pytest.approx(1100.0)        # 11万股 → 1100手
    assert f["bars"] == 5


def test_compute_features_open_times_counts_runs_not_bars():
    """② 开板次数 = "连续低于板的 run" 数(同一次开板的 N 根只记一次)。"""
    rows = [
        ("09:30", 11.0, 11.0, 11.0, 10_000.0, 110_000.0),   # 第 1 根封板
        ("09:31", 11.0, 10.4, 10.4, 10_000.0, 110_000.0),   # 开板 #1
        ("09:32", 11.0, 10.4, 10.4, 10_000.0, 110_000.0),   # 仍开(同一次)
        ("09:33", 11.0, 11.0, 11.0, 10_000.0, 110_000.0),   # 回封
        ("09:34", 11.0, 10.5, 10.5, 10_000.0, 110_000.0),   # 开板 #2
        ("15:00", 11.0, 11.0, 11.0, 10_000.0, 110_000.0),
    ]
    f = bti.compute_features(rows, 11.0)
    assert f["opened"] is True and f["open_times"] == 2


def test_compute_features_one_word():
    """② 一字板: 首封在第 1 根且从未开板。"""
    rows = [("09:30", 11.0, 11.0, 11.0, 10_000.0, 110_000.0),
            ("15:00", 11.0, 11.0, 11.0, 10_000.0, 110_000.0)]
    f = bti.compute_features(rows, 11.0)
    assert f["one_word"] is True and f["opened"] is False
    assert f["first_seal_hm"] == "09:30" and f["sealed_close"] is True


def test_compute_features_never_sealed():
    """② 从未触板 → first_seal_hm=None/板上量额 0/收盘封 False(特征仍可落盘)。"""
    rows = [("09:30", 10.5, 10.0, 10.2, 100_000.0, 1_000_000.0),
            ("15:00", 10.5, 10.1, 10.3, 10_000.0, 100_000.0)]
    f = bti.compute_features(rows, 11.0)
    assert f["first_seal_hm"] is None and f["opened"] is False
    assert f["open_times"] == 0 and f["one_word"] is False
    assert f["on_board_amt"] == 0.0 and f["on_board_vol"] == 0.0
    assert f["sealed_close"] is False
    assert bti.compute_features([], 11.0) is None


def test_compute_features_accepts_epoch_and_compact_times():
    """② bar 时间兼容 毫秒 epoch / "YYYYMMDDHHMMSS" / ISO / Timestamp 字符串。"""
    rows = [(_ms(date(2026, 9, 4), 9, 33), 11.0, 11.0, 11.0, 10_000.0, 110_000.0)]
    assert bti.compute_features(rows, 11.0)["first_seal_hm"] == "09:33"
    rows2 = [("20260904093400", 11.0, 11.0, 11.0, 10_000.0, 110_000.0)]
    assert bti.compute_features(rows2, 11.0)["first_seal_hm"] == "09:34"


def test_seal_timetag_roundtrip_matches_f2_parser():
    """② seal_timetag: "HH:MM" → 当日毫秒 epoch, F2 的解析函数还原同一分钟。"""
    ms = bti.seal_timetag("2026-09-04", "09:32")
    assert _parse_timetag_hhmm(ms) == (9, 32)
    assert bti.seal_timetag("2026-09-04", None) is None
    assert bti.seal_timetag("2026-09-04", "--") is None


# ---------------- ③ download_features(离线桩) ----------------

def test_download_features_writes_monthly_cache_idempotent(monkeypatch, tmp_path):
    """③ 同一 ISO 周 → 合并一批下载; 落盘按月 JSON; 重跑幂等(不再下载/重算)。"""
    fd = tmp_path / "bt_intraday"
    monkeypatch.setattr(bti, "FEATURE_DIR", fd)
    monkeypatch.setattr(bti, "_sleep", lambda s: None)
    monkeypatch.setattr(bti, "_prev_daily_close", lambda code, iso: 10.0)
    bars = [("09:30", 10.5, 10.0, 10.2, 100_000.0, 1_000_000.0),
            ("09:31", 11.0, 10.9, 11.0, 50_000.0, 550_000.0),    # 首封
            ("15:00", 11.0, 11.0, 11.0, 10_000.0, 110_000.0)]
    monkeypatch.setattr(bti, "_load_1m", lambda code, iso: list(bars))
    downloads = []

    def fake_dl(codes, start, end):
        downloads.append((sorted(codes), start, end))

    monkeypatch.setattr(bti, "_download_batch", fake_dl)
    # date 对象 key / 裸 6 位码 也要吃; 键保持池条目原样
    res = bti.download_features({"2026-09-02": ["600000", "000001.SZ"],
                                 date(2026, 9, 4): ["600000.SH"]})
    assert res["written"] == 3 and res["stock_days"] == 3
    assert len(downloads) == 1, "同一 ISO 周的 (code,day) 合并一批下载"
    assert downloads[0][0] == ["000001.SZ", "600000.SH"]     # QMT 侧补后缀
    assert downloads[0][1] == "20260902" and downloads[0][2] == "20260904"
    # 裸码进缓存也按池键原样可查
    assert bti.features_for("600000", "2026-09-02")["limit_price"] == 11.0
    assert bti.features_for("600000.SH", "2026-09-04")["first_seal_hm"] == "09:31"
    assert (fd / "2026-09.json").exists()
    # 幂等: 已缓存 → 不再发起下载, 也不重算
    downloads.clear()
    res2 = bti.download_features({"2026-09-04": ["600000.SH"]})
    assert res2 == {"stock_days": 0, "written": 0}
    assert downloads == []


def test_download_features_skips_days_before_1m_retention(monkeypatch, tmp_path):
    """③ 早于 2025-09-15(QMT 1m 最早可回溯日) → 跳过, 绝不发下载请求。"""
    fd = tmp_path / "bt_intraday"
    monkeypatch.setattr(bti, "FEATURE_DIR", fd)
    monkeypatch.setattr(bti, "_sleep", lambda s: None)
    monkeypatch.setattr(bti, "_load_1m", lambda code, iso: [])
    downloads = []
    monkeypatch.setattr(bti, "_download_batch",
                        lambda codes, s, e: downloads.append((s, e)))
    res = bti.download_features({"2025-09-10": ["600000.SH"],
                                 "2025-09-16": ["600000.SH"]})
    assert res["stock_days"] == 0 and res["written"] == 0
    assert downloads == [("20250916", "20250916")], "只对 ≥2025-09-15 的日期下载"


def test_download_features_counts_no_data_as_missing(monkeypatch, tmp_path):
    """③ 当日无 1m 数据/无昨收 → 特征缺失不落盘(宁缺勿假), 计数如实。"""
    fd = tmp_path / "bt_intraday"
    monkeypatch.setattr(bti, "FEATURE_DIR", fd)
    monkeypatch.setattr(bti, "_sleep", lambda s: None)
    monkeypatch.setattr(bti, "_load_1m", lambda code, iso: [])
    monkeypatch.setattr(bti, "_prev_daily_close", lambda code, iso: 10.0)
    # 测试卫生(2026-09-16): 必须桩掉批量下载 —— 漏桩会真打 QMT
    # (download_history_data2), 无 QMT/网络慢时实测偶发 >300s 卡死, 违反
    # "测试必须离线可跑"约束。本用例只验"无数据 → 不落盘 + 计数如实",
    # 下载本身由 ③ 幂等/分片两个用例(已桩)覆盖。
    monkeypatch.setattr(bti, "_download_batch", lambda codes, s, e: None)
    res = bti.download_features({"2026-09-04": ["600000.SH", "000001.SZ"]})
    assert res["stock_days"] == 0 and res["written"] == 0
    assert bti.features_for("600000.SH", "2026-09-04") is None


# ---------------- ④ F2 复活(因子级 + 回测级) ----------------

def test_f2_hits_when_seal_before_1000_else_zero():
    """④ 回测注入 sealed=True + timetag 首封毫秒: 09:32 → 1, 10:30 → 0。"""
    for hh, mm, want in ((9, 32, 1), (10, 30, 0)):
        ctx = FactorContext(code="600000.SH", sealed=True, up_price=11.0,
                            tick={"timetag": _ms(DAY, hh, mm)})
        assert reg.get_factor("F2")["func"](ctx)["score"] == want


def test_f2_backtest_end_to_end():
    """④ 回测级: stock 带 sealed/tick → F2 命中出交易; 晚封 → 无候选(旧行为)。"""
    s = _mk_strategy(scoring=("F2",), gate=("N1",))
    stock_hit = {"600000.SH": {"up_price": 11.0, "last": 11.0,
                               "last_close": 10.0, "sealed": True,
                               "tick": {"timetag": _ms(DAY, 9, 32)}}}
    stock_late = {"600000.SH": dict(stock_hit["600000.SH"],
                                    tick={"timetag": _ms(DAY, 10, 30)})}
    bt = _bt(s, _pool_zt({FD8}), lambda c: _KLINE2)
    assert bt.run(DAY, date(2026, 7, 8),
                  day_feed=lambda d: ({"stock": stock_hit}
                                      if d == DAY else None))["trades"] == 1
    assert bt.run(DAY, date(2026, 7, 8),
                  day_feed=lambda d: ({"stock": stock_late}
                                      if d == DAY else None))["trades"] == 0


# ---------------- ⑤ F3 代理分支 ----------------

def test_f3_proxy_hit_note_mentions_backtest_proxy():
    """⑤ ratio=0.001 ≤ T(0.005) → 命中, note 含"回测代理"。"""
    ctx = FactorContext(code="600000.SH", sealed=True, up_price=11.0,
                        bt_seal_ratio=0.001)
    res = reg.get_factor("F3")["func"](ctx)
    assert res["score"] == 1
    assert "回测代理" in res["note"]


def test_f3_proxy_miss_when_ratio_above_threshold():
    """⑤ ratio=0.02 > T → 不命中, note 同样标"回测代理"(口径可追溯)。"""
    ctx = FactorContext(code="600000.SH", sealed=True, up_price=11.0,
                        bt_seal_ratio=0.02)
    res = reg.get_factor("F3")["func"](ctx)
    assert res["score"] == 0
    assert "回测代理" in res["note"]


def test_f3_proxy_never_fires_when_not_sealed():
    """⑤ 防假"封得结实": sealed=False 时板上额=0 → ratio=0≤T, 必须不触发代理。"""
    ctx = FactorContext(code="600000.SH", sealed=False, up_price=11.0,
                        bt_seal_ratio=0.0)
    res = reg.get_factor("F3")["func"](ctx)
    assert res["score"] == 0
    assert "回测代理" not in res["note"]


def test_f3_live_bidvol_path_unchanged_without_bt_seal_ratio():
    """⑤ 无 bt_seal_ratio → 原实盘口径 bidVol[0]×100×bidPrice[0] ≥ 流通市值×0.5%。"""
    hit = FactorContext(code="000001.SZ", sealed=True, up_price=11.0,
                        float_vol=1e8,
                        tick={"bidPrice": [11.0, 0, 0, 0, 0],
                              "bidVol": [10000, 0, 0, 0, 0]})
    assert reg.get_factor("F3")["func"](hit)["score"] == 1
    miss = FactorContext(code="000001.SZ", sealed=True, up_price=11.0,
                         float_vol=1e8,
                         tick={"bidPrice": [11.0, 0, 0, 0, 0],
                               "bidVol": [100, 0, 0, 0, 0]})
    res = reg.get_factor("F3")["func"](miss)
    assert res["score"] == 0
    assert "回测代理" not in res["note"]


def test_f3_backtest_end_to_end_proxy():
    """⑤ 回测级: bt_seal_ratio 小 → F3 命中; 大 → 候选被 min_model 过滤。"""
    s = _mk_strategy(scoring=("F3",), gate=("N1",))
    base = {"up_price": 11.0, "last": 11.0, "last_close": 10.0, "sealed": True}
    bt = _bt(s, _pool_zt({FD8}), lambda c: _KLINE2)
    tight = dict(base, bt_seal_ratio=0.001)
    assert bt.run(DAY, date(2026, 7, 8),
                  day_feed=lambda d: ({"stock": {"600000.SH": tight}}
                                      if d == DAY else None))["trades"] == 1
    loose = dict(base, bt_seal_ratio=0.02)
    assert bt.run(DAY, date(2026, 7, 8),
                  day_feed=lambda d: ({"stock": {"600000.SH": loose}}
                                      if d == DAY else None))["trades"] == 0


# ---------------- ⑥ build_day_feed(use_intraday=True) ----------------

_INTRA_FEAT = {"limit_price": 11.0, "first_seal_hm": "09:31", "opened": False,
               "open_times": 0, "sealed_close": True, "one_word": True,
               "on_board_amt": 5.5e5, "on_board_vol": 600.0, "bars": 240}


def _intra_io(monkeypatch, tmp_path, with_cache=True, feat=None):
    """离线桩 build_day_feed 的全部 IO(含 1m 特征缓存, 按需预写)。

    feat: 覆盖缓存条目(如"旧版本缓存缺 one_word 键"的跨版本场景)。
    """
    fd = tmp_path / "bt_intraday"
    monkeypatch.setattr(bti, "FEATURE_DIR", fd)
    if with_cache:
        _write_month(fd, "2026-07",
                     {"600000.SH": {"2026-07-07": dict(feat or _INTRA_FEAT)}})
    # 下载相关的一切调用都视为违规(网页请求路径绝不下载)
    monkeypatch.setattr(bti, "_download_batch",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("回测路径绝不能触发 1m 下载")))
    monkeypatch.setattr(bti, "download_features",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("回测路径绝不能调用 download_features")))
    monkeypatch.setattr(cli, "_get_zt_index",
                        lambda: {"20260706": 1, "20260707": 1})
    monkeypatch.setattr(cli, "_index_daily", lambda *a, **k: [
        ("2026-07-06", 3800.0, 4.0e8, 8.7e11),
        ("2026-07-07", 3850.0, 4.1e8, 9.0e11)])
    monkeypatch.setattr(cli, "zt_feed",
                        lambda d8: [{"code": "600000.SH", "boards": 1}])
    monkeypatch.setattr(cli, "_batch_klines", lambda codes: {
        "600000.SH": [("2026-07-06", 10.0, 10.2, 9.9, 10.0, 100.0),
                      ("2026-07-07", 10.9, 11.0, 10.85, 11.0, 200.0)]})
    monkeypatch.setattr(cli, "_float_volumes", lambda codes: {"600000.SH": 3.0e9})


def test_build_day_feed_intraday_synthesizes_stock_fields(monkeypatch, tmp_path):
    """⑥ use_intraday=True: stock[code] 增 sealed/tick(首封毫秒)/bt_seal_ratio。"""
    _intra_io(monkeypatch, tmp_path)
    feed = cli.build_day_feed(date(2026, 7, 6), date(2026, 7, 7),
                              use_intraday=True)
    ctx = feed(DAY)
    st = ctx["stock"]["600000.SH"]
    assert st["sealed"] is True
    assert st["bt_seal_ratio"] == pytest.approx(5.5e5 / (3.0e9 * 11.0))
    tick = st["tick"]
    assert tick["lastPrice"] == 11.0 and tick["lastClose"] == 10.0
    assert tick["amount"] == pytest.approx(5.5e5)
    assert _parse_timetag_hhmm(tick["timetag"]) == (9, 31)
    # F3 代理在真实 build_day_feed 载荷上命中(note 含"回测代理")。
    # 该载荷 one_word=True(一字板)会被成交约束拦下买入 —— 本用例只验 F3 命中,
    # 故按"未知"口径去掉该键(不拦不假); one_word 的搬运由 ⑥ 下一个用例专测。
    st.pop("one_word")
    s = _mk_strategy(scoring=("F3",), gate=("N1",))
    rep = _bt(s, _pool_zt({FD8}), lambda c: _KLINE2).run(
        DAY, date(2026, 7, 8), day_feed=lambda d: (ctx if d == DAY else None))
    assert rep["trades"] == 1


def test_build_day_feed_intraday_missing_cache_silent_degrade(monkeypatch, tmp_path):
    """⑥ 缓存缺失 → 静默降级(stock 不带 sealed/tick/bt_seal_ratio) + data_notes 说明。"""
    _intra_io(monkeypatch, tmp_path, with_cache=False)
    feed = cli.build_day_feed(date(2026, 7, 6), date(2026, 7, 7),
                              use_intraday=True)
    ctx = feed(DAY)
    st = ctx["stock"]["600000.SH"]
    assert "sealed" not in st and "tick" not in st and "bt_seal_ratio" not in st
    assert feed.data_notes and "覆盖 0/1" in feed.data_notes[0]


def test_build_day_feed_without_intraday_unchanged_and_no_notes(monkeypatch, tmp_path):
    """⑥ use_intraday=False(默认): 输出与 Task1 逐字段一致, 不带 data_notes。"""
    _intra_io(monkeypatch, tmp_path)
    feed = cli.build_day_feed(date(2026, 7, 6), date(2026, 7, 7))
    ctx = feed(DAY)
    st = ctx["stock"]["600000.SH"]
    assert "sealed" not in st and "tick" not in st
    assert not getattr(feed, "data_notes", [])


def test_build_day_feed_intraday_carries_one_word(monkeypatch, tmp_path):
    """⑥ one_word 接线(规格 §6 成交约束): 缓存有该日 → 带布尔值**原样**搬运;
    缓存缺该日 → **不设键**(未知 ≠ False, Backtester 按未知处理)。

    缓存值取 True: 若写成 False, 接线字段名写错(取到 None → bool(None)=False)
    也能过 —— 只有 True 才能证明"特征值真的被搬过来了"。
    """
    _intra_io(monkeypatch, tmp_path)
    st = cli.build_day_feed(date(2026, 7, 6), date(2026, 7, 7),
                            use_intraday=True)(DAY)["stock"]["600000.SH"]
    assert st["one_word"] is True         # _INTRA_FEAT["one_word"] = True
    # 缺缓存: one_word 键不存在(不是 False) —— 回测侧据此判"未知"。
    # 换独立目录(bt_intraday 按月分片带 memo, 键含 FEATURE_DIR → 不复用旧缓存)
    _intra_io(monkeypatch, tmp_path / "nocache", with_cache=False)
    st2 = cli.build_day_feed(date(2026, 7, 6), date(2026, 7, 7),
                             use_intraday=True)(DAY)["stock"]["600000.SH"]
    assert "one_word" not in st2


def test_build_day_feed_one_word_missing_key_is_unknown(monkeypatch, tmp_path):
    """⑥ 缓存条目在、但**没有** one_word 键(旧版本缓存跨版本存活)→ 不设键
    (= 未知), 绝不压成 False(= 已知买得到)。"""
    old = {k: v for k, v in _INTRA_FEAT.items() if k != "one_word"}
    _intra_io(monkeypatch, tmp_path, feat=old)
    st = cli.build_day_feed(date(2026, 7, 6), date(2026, 7, 7),
                            use_intraday=True)(DAY)["stock"]["600000.SH"]
    assert "one_word" not in st           # 缺键 = 未知, 不是 False
    assert st["sealed"] is True           # 同一条目的其余字段照常搬运


# ---------------- ⑦ 报告 data_notes ----------------

def test_report_includes_data_notes_from_feed():
    """⑦ day_feed 携带 data_notes → 原样进报告; 无属性/无注入 → []。"""
    class Feed:
        data_notes = ["1m特征: 个股日覆盖 3/4 (缓存缺失静默降级 → F2/F3 fail-open 0)"]

        def __call__(self, d):
            return {"em": {"max_boards": 5}} if d == DAY else None

    s = _mk_strategy(scoring=("F1",), gate=("N4",))
    bt = _bt(s, _pool_zt({FD8}), lambda c: _KLINE2)
    rep = bt.run(DAY, date(2026, 7, 8), day_feed=Feed())
    assert rep["data_notes"] == Feed.data_notes
    # day_feed 无 data_notes 属性(普通 lambda) → []
    rep2 = bt.run(DAY, date(2026, 7, 8),
                  day_feed=lambda d: {"em": {"max_boards": 5}})
    assert rep2["data_notes"] == []
    # 零交易报告(门关)也带 data_notes 键
    rep3 = bt.run(DAY, date(2026, 7, 8), day_feed=lambda d: None)
    assert rep3["data_notes"] == [] and rep3["trades"] == 0


# ---------------- ⑧ M6: 1m 说明点明 one_word 未知的处置 ----------------

def test_intra_note_states_one_word_unknown_bucket():
    """M6: 1m 覆盖说明要写明"缺 one_word → 买入不拦(未知)"及其计数去处。

    否则读者看到 `filter_stats.one_word_unknown>0` 只能靠翻代码才懂:
    缺字段 ≠ 买得到, 只是不拦(宁可未知也不拦错)。
    """
    notes = []
    cli._refresh_intra_note(notes, {"got": 1, "asked": 2})
    n = notes[0]
    assert "覆盖 1/2" in n, "原有覆盖计数必须保留"
    assert "one_word" in n and "one_word_unknown" in n, \
        "必须点明计数去处(报告 filter_stats.one_word_unknown)"
    assert "不拦" in n and "未知" in n, "必须说明处置: 未知 → 不拦"

