# -*- coding: utf-8 -*-
"""env 信号通道测试: real / sim 切换 + sim 模式的闸门与隔离。

背景：tt 原先只写 real 通道。接入 QMT 模拟盘需要 ``--env sim``，
把信号写到 ``D:/QMT_SIGNALS/sim/pending/``，由 ``qmt_signal_bridge_demo.py`` 消费。
而那支桥 **不校验账户** —— 它下单用的是 QMT 当前登录的那个账户。所以这里的测试
必须覆盖四件事：

1. ``env`` 取值受白名单校验（写错一个字不许静默放行）；
2. ``sim`` 模式绝不污染 real 队列；
3. ``sim`` 仍受 ``armed`` 闸门约束（生成侧不能比桥端更松）；
4. 启动横幅必须把"桥不校验账户"这件事明确打出来。
"""
import pytest

from ttcore import config as tt_config
from ttcore import daemon as tt_daemon
from ttcore.engine import TTEngine
from ttcore.state import Ledger

FIXED_TODAY = "20260914"


# ---------------------------------------------------------------- config 层

def test_env_defaults_to_real(cfg):
    assert cfg["env"] == "real"


def test_env_accepts_sim():
    assert tt_config.load(overrides={"env": "sim"})["env"] == "sim"


def test_env_is_case_insensitive_and_stripped():
    assert tt_config.load(overrides={"env": "SIM"})["env"] == "sim"
    assert tt_config.load(overrides={"env": " sim "})["env"] == "sim"


@pytest.mark.parametrize("bad", ["live", "demo", "realx", "sim1", "  "])
def test_env_rejects_invalid(bad):
    with pytest.raises(tt_config.ConfigError):
        tt_config.load(overrides={"env": bad})


@pytest.mark.parametrize("blank", ["", None])
def test_env_blank_falls_back_to_real(blank):
    """空/未设 → 兜底 real（保守：不因为漏填就跑到别的通道去）。"""
    assert tt_config.load(overrides={"env": blank})["env"] == "real"


# ---------------------------------------------------------------- 启动横幅

def test_banner_sim_warns_about_account_risk(cfg):
    c = dict(cfg, env="sim")
    b = tt_daemon.env_banner(c, dry_run=True)
    assert "sim" in b
    assert "不校验账户" in b          # 桥端不校验账户这件事必须写出来
    assert "DRY-RUN" in b


def test_banner_real_has_no_sim_warning(cfg):
    c = dict(cfg, env="real")
    b = tt_daemon.env_banner(c, dry_run=True)
    assert "不校验账户" not in b


def test_banner_marks_live_mode(cfg):
    b = tt_daemon.env_banner(dict(cfg, env="real"), dry_run=False)
    assert "LIVE" in b


# ---------------------------------------------------------------- daemon 层

def _build(cfg, tmp_path, now_fn, feed):
    led = Ledger(path=tmp_path / "tt_state.json", now_fn=now_fn)
    eng = TTEngine(cfg, led, feed=feed, now_fn=now_fn, force_paper=True)
    return led, eng


def _feed(fake_feed, snap_factory):
    return fake_feed({"600900.SH": snap_factory(last=28.45, last_close=28.09)})


def test_sim_writes_to_sim_queue_only(cfg, tmp_path, now_fn, fake_feed,
                                     snap_factory):
    c = dict(cfg, env="sim")
    led, eng = _build(c, tmp_path, now_fn, _feed(fake_feed, snap_factory))
    root = tmp_path / "sig"
    (root / "sim").mkdir(parents=True)
    (root / "sim" / "armed.txt").write_text(FIXED_TODAY, encoding="utf-8")

    d = tt_daemon.TTDaemon(c, ledger=led, engine=eng, dry_run=False,
                           now_fn=now_fn, signal_root=root)
    rt = d.run_once()

    assert rt["env"] == "sim"
    assert rt["armed"] is True
    assert rt["blocked"] == ""
    assert rt["signals_written"] > 0
    assert list((root / "sim" / "pending").glob("*.json"))
    # 关键隔离：real 队列一个字节都不能被碰
    assert not (root / "real").exists()


def test_sim_still_requires_armed(cfg, tmp_path, now_fn, fake_feed,
                                  snap_factory):
    """生成侧不能比桥端更松：即使 sim 桥不校验 arm，tt 也要校验。"""
    c = dict(cfg, env="sim")
    led, eng = _build(c, tmp_path, now_fn, _feed(fake_feed, snap_factory))
    root = tmp_path / "sig"
    (root / "sim").mkdir(parents=True)          # 故意不写 armed.txt

    d = tt_daemon.TTDaemon(c, ledger=led, engine=eng, dry_run=False,
                           now_fn=now_fn, signal_root=root)
    rt = d.run_once()

    assert rt["armed"] is False
    assert rt["signals_written"] == 0
    assert rt["blocked"].startswith("not_armed")
    assert not (root / "sim" / "pending").exists()


def test_sim_armed_with_wrong_date_is_blocked(cfg, tmp_path, now_fn,
                                              fake_feed, snap_factory):
    c = dict(cfg, env="sim")
    led, eng = _build(c, tmp_path, now_fn, _feed(fake_feed, snap_factory))
    root = tmp_path / "sig"
    (root / "sim").mkdir(parents=True)
    (root / "sim" / "armed.txt").write_text("20200101", encoding="utf-8")

    d = tt_daemon.TTDaemon(c, ledger=led, engine=eng, dry_run=False,
                           now_fn=now_fn, signal_root=root)
    rt = d.run_once()

    assert rt["armed"] is False
    assert rt["signals_written"] == 0


def test_pause_switch_blocks_sim_too(cfg, tmp_path, now_fn, fake_feed,
                                     snap_factory, monkeypatch):
    """paused 是全局急停：把根目录换成 tmp_path 后必须仍然生效。"""
    c = dict(cfg, env="sim")
    led, eng = _build(c, tmp_path, now_fn, _feed(fake_feed, snap_factory))
    root = tmp_path / "sig"
    (root / "sim").mkdir(parents=True)
    (root / "sim" / "armed.txt").write_text(FIXED_TODAY, encoding="utf-8")
    (root / "paused").write_text("stop", encoding="utf-8")

    d = tt_daemon.TTDaemon(c, ledger=led, engine=eng, dry_run=False,
                           now_fn=now_fn, signal_root=root)
    rt = d.run_once()

    assert rt["paused"] is True
    assert rt["signals_written"] == 0
    assert rt["blocked"].startswith("paused")


def test_dry_run_beats_armed_in_any_env(cfg, tmp_path, now_fn, fake_feed,
                                        snap_factory):
    """dry_run 是最高优先级闸门，armed 就绪也不许落盘。"""
    c = dict(cfg, env="sim")
    led, eng = _build(c, tmp_path, now_fn, _feed(fake_feed, snap_factory))
    root = tmp_path / "sig"
    (root / "sim").mkdir(parents=True)
    (root / "sim" / "armed.txt").write_text(FIXED_TODAY, encoding="utf-8")

    d = tt_daemon.TTDaemon(c, ledger=led, engine=eng, dry_run=True,
                           now_fn=now_fn, signal_root=root)
    rt = d.run_once()

    assert rt["blocked"] == "dry_run"
    assert rt["signals_written"] == 0


def test_armed_state_reads_env_specific_file(tmp_path, now_fn):
    """armed 文件按 env 分目录：sim 的 arm 不应该放行 real。"""
    root = tmp_path / "sig"
    (root / "sim").mkdir(parents=True)
    (root / "sim" / "armed.txt").write_text(FIXED_TODAY, encoding="utf-8")

    ok_sim, _ = tt_daemon.armed_state(root, "sim", now_fn())
    ok_real, msg_real = tt_daemon.armed_state(root, "real", now_fn())

    assert ok_sim is True
    assert ok_real is False
    assert "armed.txt" in msg_real
