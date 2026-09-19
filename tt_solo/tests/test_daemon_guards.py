# -*- coding: utf-8 -*-
"""daemon 启动参数与闸门文件的守卫测试(fail-closed)。

覆盖:
  - I5  `--live` 与 `--sample` 互斥(否则只读样本行情会写进真实信号队列);
  - I6  `--live` 是**唯一**关闭 dry_run 的途径(配置里的 dry_run:false 只警告);
  - Minor 4 启动横幅用实际生效的 --signal-root, 不是模块常量;
  - Minor 6 armed 放行条按**行**比较(子串匹配会认下 x20260914000 / 120260914)。

全部只构造对象、不跑 run_once, 因此不碰 tt_runtime.json 与真实信号根。
"""
import argparse
import logging

import pytest

from ttcore import config as tc
from ttcore import daemon as tt_daemon

TODAY = "20260914"


def _args(**kw):
    """与 test_drill_book._args 同款的最小 args 命名空间。"""
    base = dict(live=False, sample=True, direct=False, state=None,
                signal_root=None, env=None, fake_now=True,
                book_dry_run=False, once=True, interval=5.0, rounds=None)
    base.update(kw)
    return argparse.Namespace(**base)


# ---------------------------------------------------------------- I5 互斥

def test_live_and_sample_are_mutually_exclusive():
    """--live + --sample → 直接 SystemExit(fail-closed)。

    旧实现没有守卫: dry_run=False + SampleBackend + force_paper 时, 4 个信号
    文件会真的落进 <root>/real/pending/, 而样本 CSV 末根是 2026-09-11 的价
    (与真价差约 10%) —— 等于拿过期样本价往实盘队列塞单。
    """
    with pytest.raises(SystemExit) as ei:
        tt_daemon.build_daemon(_args(live=True, sample=True))
    msg = str(ei.value)
    assert "--live" in msg and "--sample" in msg


def test_sample_alone_is_fine(tmp_path):
    d = tt_daemon.build_daemon(
        _args(sample=True, state=str(tmp_path / "s.json"),
              signal_root=str(tmp_path / "sig")))
    assert d.dry_run is True


def test_live_alone_is_fine(tmp_path):
    d = tt_daemon.build_daemon(
        _args(live=True, sample=False, state=str(tmp_path / "s.json"),
              signal_root=str(tmp_path / "sig")))
    assert d.dry_run is False


# ---------------------------------------------------------------- I6 dry_run 收紧

def test_config_dry_run_false_is_ignored_without_live(tmp_path, monkeypatch,
                                                      caplog):
    """配置里 dry_run:false + 不给 --live → 仍必须是 dry_run(并打 WARNING)。"""
    raw = dict(tc.load(), dry_run=False)
    monkeypatch.setattr(tt_daemon.tt_config, "load", lambda **kw: raw)
    with caplog.at_level(logging.WARNING, logger="tt_daemon"):
        d = tt_daemon.build_daemon(
            _args(live=False, state=str(tmp_path / "s.json"),
                  signal_root=str(tmp_path / "sig")))
    assert d.dry_run is True
    assert "dry_run" in caplog.text and "被忽略" in caplog.text


def test_config_dry_run_false_takes_effect_with_live(tmp_path, monkeypatch):
    """配置 dry_run:false + --live → 才是实盘(dry_run False)。"""
    raw = dict(tc.load(), dry_run=False)
    monkeypatch.setattr(tt_daemon.tt_config, "load", lambda **kw: raw)
    d = tt_daemon.build_daemon(
        _args(live=True, sample=False, state=str(tmp_path / "s.json"),
              signal_root=str(tmp_path / "sig")))
    assert d.dry_run is False


def test_live_overrides_config_dry_run_true(tmp_path, monkeypatch):
    """配置 dry_run:true + --live → --live 优先(显式意愿压过配置)。"""
    raw = dict(tc.load(), dry_run=True)
    monkeypatch.setattr(tt_daemon.tt_config, "load", lambda **kw: raw)
    d = tt_daemon.build_daemon(
        _args(live=True, sample=False, state=str(tmp_path / "s.json"),
              signal_root=str(tmp_path / "sig")))
    assert d.dry_run is False


# ---------------------------------------------------------------- Minor 4 横幅

def test_banner_uses_effective_signal_root(tmp_path):
    root = tmp_path / "sig"
    d = tt_daemon.build_daemon(_args(state=str(tmp_path / "s.json"),
                                     signal_root=str(root)))
    b = tt_daemon.env_banner(d.cfg, d.dry_run, direct=d.direct,
                             signal_root=d.signal_root)
    assert str(root) in b
    assert "QMT_SIGNALS" not in b        # 不再是模块常量的真实目录


def test_banner_default_signal_root_unchanged():
    b = tt_daemon.env_banner({"env": "real"}, dry_run=True)
    assert "QMT_SIGNALS" in b            # 不给 root → 保持原样


# ---------------------------------------------------------------- Minor 6 armed 行比较

@pytest.mark.parametrize("content,expect", [
    ("20260914", True),
    ("20260914\n", True),
    ("20260913\n20260914\n", True),      # 当日条在里面(多行历史)
    ("x20260914000", False),             # 子串命中但整行不是日期
    ("120260914", False),
    ("202609140", False),
    ("20260913", False),                 # 昨天
    ("20260915", False),                 # 未来
    ("", False),                         # 空文件
    ("2026-09-14", False),               # 带分隔符不算
])
def test_armed_state_matches_whole_line(tmp_path, content, expect):
    root = tmp_path / "sig"
    (root / "real").mkdir(parents=True)
    (root / "real" / "armed.txt").write_text(content, encoding="utf-8")
    ok, msg = tt_daemon.armed_state(root, "real", _now())
    assert ok is expect, "%r → %s (%s)" % (content, ok, msg)


def test_armed_file_missing(tmp_path):
    ok, msg = tt_daemon.armed_state(tmp_path / "sig", "real", _now())
    assert ok is False and "armed.txt" in msg


def _now():
    from datetime import datetime
    return datetime(2026, 9, 14, 10, 0, 0)
