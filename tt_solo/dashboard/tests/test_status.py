# -*- coding: utf-8 -*-
"""面板主接口测试: `/api/status` 的「守护不在线 → 现场只读重算」路径。

为什么单开一个文件: 这条路径(`current_status` / `readonly_plan` /
`/api/kline/<code>`)此前没有任何测试, 而它正是守护挂掉、有人在排查故障时
真正跑的路。面板的整个安全叙事是"监控面不改交易状态", 这个承诺必须由测试
钉住 —— 写在文档里不算数。

全离线: 行情走 `FEED_FACTORY` 打桩, 账户走 `ACCOUNT` 打桩, STATE_PATH /
RUNTIME_PATH / SIGNAL_ROOT 全部打到 tmp, 不碰 D:/QMT_SIGNALS 与真实运行目录。
"""
import hashlib
import json
from datetime import datetime

import pytest

from dashboard import app as dash
from tests.conftest import FakeFeed, make_snapshot

CODE = "600900.SH"
# 面板的 Ledger 用 datetime.now() 判日, "过期账本"只能写一个显然不是今天的日期
STALE_DAY = "2020-01-01"

# 重算结果里前端(index.html)与运维真正消费的字段 —— 少一个就是白屏/字段错位
PLAN_KEYS = (
    "ok", "hhmm", "phase", "dry_run", "env", "source", "account", "risk",
    "symbols", "intents", "rejected", "signals", "counts",
    "runtime_at", "ledger", "signals_written", "paused", "armed", "armed_msg",
    "blocked", "_source",
)


def _ledger_text(day, sold=300):
    return json.dumps({
        "version": 1, "date": day, "updated_at": day + "T10:00:00",
        "symbols": {CODE: {
            "sold_today": sold, "bought_today": 0, "filled_sell_units": 1,
            "filled_buy_units": 0, "trips": 1, "realized_pnl": 12.5,
            "sell_queue": [], "buy_queue": [], "ref_price": 28.0,
            "last_sell_price": 28.5, "last_buy_price": 0.0,
            "switch_state": "", "note": ""}},
        "events": [],
    }, ensure_ascii=False)


def _fingerprint(d):
    """目录指纹: 文件名 + (mtime_ns, 大小, sha256) —— 任何写入都躲不过。"""
    out = {}
    for p in sorted(d.rglob("*")):
        if p.is_file():
            b = p.read_bytes()
            out[str(p.relative_to(d))] = (p.stat().st_mtime_ns, len(b),
                                          hashlib.sha256(b).hexdigest())
    return out


@pytest.fixture
def feed():
    return FakeFeed({CODE: make_snapshot(
        last=28.45, last_close=28.09, high=28.57, low=28.10,
        ma20=28.19, ma20_prev=28.19)})


# 账户替身: 重算路径不该碰真实 QMT。不只要"没有第三方告警"—— 真账户在线时
# 持仓/资金会渗进 plan, 测试结果随机器漂移(本机 QMT 常开, 实测真账户在场)。
STUB_ASSET = {"total_asset": 500000.0, "cash": 200000.0, "market_value": 300000.0}


class StubAccount:
    """TTEngine(account=...) 的离线替身: 只实现引擎用到的 asset()/positions()。"""

    def asset(self):
        return dict(STUB_ASSET)

    def positions(self):
        return {CODE: {"volume": 5000, "can_use_volume": 5000,
                       "market_value": 142250.0}}


@pytest.fixture
def client(monkeypatch, tmp_path, feed):
    """面板级隔离: 三条路径全打 tmp + 行情/账户打桩 + 清掉 5 秒结果缓存。

    缓存必须清: 它是模块级的, 上一个用例的结果会漏进来, 让"重算路径"根本没跑。
    """
    monkeypatch.setattr(dash, "STATE_PATH", tmp_path / "tt_state.json")
    # 守护快照指向不存在的文件 = 守护不在线 → 强制走现场重算
    monkeypatch.setattr(dash, "RUNTIME_PATH", tmp_path / "no_runtime.json")
    monkeypatch.setattr(dash, "SIGNAL_ROOT", tmp_path / "signals")
    monkeypatch.setattr(dash, "FEED_FACTORY", lambda: feed)
    monkeypatch.setattr(dash, "ACCOUNT", StubAccount())
    dash._cache["at"], dash._cache["data"] = 0.0, None
    dash.app.config["TESTING"] = True
    yield dash.app.test_client()
    dash._cache["at"], dash._cache["data"] = 0.0, None


def test_status_recompute_returns_expected_shape(client, tmp_path):
    """守护不在线时 /api/status 现场重算, 且顶层字段齐备。"""
    (tmp_path / "tt_state.json").write_text(_ledger_text(STALE_DAY),
                                            encoding="utf-8")
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    d = body["data"]
    missing = [k for k in PLAN_KEYS if k not in d]
    assert not missing, "重算结果缺字段: %s" % missing
    assert d["_source"] == "live_recompute"
    assert d["_age"] is None
    assert d["signals_written"] == 0 and d["signals"] == []
    assert d["blocked"] == "面板只读重算(未落信号)"
    assert d["paused"] is False and d["armed"] is False
    # 账户确实来自注入替身: 真去连 QMT 的话 source 与数值都不会长这样
    # (换言之这条断言同时钉住"重算路径没碰 xtquant/真实持仓")
    assert d["account"]["source"] == "qmt"
    assert d["account"]["total_asset"] == STUB_ASSET["total_asset"]
    assert d["account"]["positions"][CODE]["can_use_volume"] == 5000
    # 标的栏必须真的算出阶梯(前端靠它画档位)
    sym = next(s for s in d["symbols"] if s["code"] == CODE)
    assert sym["ref"] > 0 and sym["ladder"]
    # 账本快照的字段形状(前端第 4 区块直接读)
    assert set(("date", "symbols", "total_realized_pnl", "daily_trades",
                "total_trips", "events")) <= set(d["ledger"])


def test_status_reads_on_disk_ledger(client, tmp_path):
    """只读 != 不读: 当日账本必须真的被面板读进来(否则显示空账, 与实际不符)。"""
    today = datetime.now().strftime("%Y-%m-%d")
    (tmp_path / "tt_state.json").write_text(_ledger_text(today, sold=300),
                                           encoding="utf-8")
    d = client.get("/api/status").get_json()["data"]
    assert d["ledger"]["date"] == today
    assert d["ledger"]["symbols"][CODE]["sold_today"] == 300
    assert d["ledger"]["total_realized_pnl"] == 12.5


def test_status_recompute_writes_nothing(client, tmp_path):
    """**面板重算绝不许写交易状态** —— 过期账本 + 守护不在线也不行。

    以前 GET /api/status 会: 把过期账本归档进 tt_history.jsonl、把 tt_state.json
    重置成空账本落盘。于是"面板决定了这一天什么时候翻页", 监控面真的动了交易状态。
    """
    (tmp_path / "tt_state.json").write_text(_ledger_text(STALE_DAY),
                                            encoding="utf-8")
    before = _fingerprint(tmp_path)
    r = client.get("/api/status")
    assert r.status_code == 200 and r.get_json()["ok"] is True
    after = _fingerprint(tmp_path)
    changed = sorted(set(before) ^ set(after)) or sorted(
        k for k in before if before[k] != after.get(k))
    assert after == before, "面板重算写盘了: %s" % changed


def test_status_prefers_fresh_daemon_snapshot(client, monkeypatch, tmp_path):
    """快照新鲜时直接用守护实测值, 不做任何重算(行情都不该被碰)。"""
    rt = tmp_path / "tt_runtime.json"
    rt.write_text(json.dumps({"ok": True, "blocked": "dry_run",
                              "counts": {"intents": 0}}), encoding="utf-8")
    monkeypatch.setattr(dash, "RUNTIME_PATH", rt)
    monkeypatch.setattr(dash, "FEED_FACTORY",
                        lambda: pytest.fail("快照新鲜时不该重算行情"))
    d = client.get("/api/status").get_json()["data"]
    assert d["_source"] == "daemon"
    assert d["_age"] is not None and d["_age"] <= dash.RUNTIME_MAX_AGE


def test_kline_uses_injected_feed(client, feed):
    """/api/kline 也走 FEED_FACTORY(离线可测), 无 high/low 时用 close 兜底。"""
    feed.snaps[CODE]["_closes"] = [28.1, 28.2, 28.3]
    body = client.get("/api/kline/%s?n=20" % CODE).get_json()
    assert body["ok"] is True
    assert [r["close"] for r in body["data"]["rows"]] == [28.1, 28.2, 28.3]


def test_kline_clamps_n(client):
    """n 越界/非法一律夹到 [20, 250], 非法值不 500。"""
    for q in ("n=1", "n=9999", "n=abc"):
        r = client.get("/api/kline/%s?%s" % (CODE, q))
        assert r.status_code == 200 and r.get_json()["ok"] is True
