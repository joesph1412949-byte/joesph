# -*- coding: utf-8 -*-
"""ops/smoke_check.py 的判别力守卫 — 全离线, 不碰生产服务(5000 端口)。

背景(2026-09-19): `_cli()` 的守卫条件 `"--help" not in args[-1]` 对
`--help` 恒为 False, 任何 rc 都被吞掉 ⇒ 该项**无条件**打印"CLI --help 可用";
且它引用的 `scripts/make_prism_summary_pdf.py` 根本不存在(真身在 ops/)。

这两个用例分别钉死这两点: --help 非 0 必须抛出来; 引用的脚本路径必须真实存在。
不打桩的话要跑真子进程, 所以这里 patch subprocess.run —— 只测关卡逻辑本身。
"""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

import ops.smoke_check as sc


def _fake_run(rc):
    return lambda *a, **k: SimpleNamespace(returncode=rc, stdout="", stderr="")


def test_cli_help_nonzero_is_reported(monkeypatch):
    """RED: 修前恒假条件吞掉 rc=1, 此用例不抛 AssertionError。"""
    monkeypatch.setattr(subprocess, "run", _fake_run(1))
    with pytest.raises(AssertionError) as ei:
        sc._cli()
    assert "rc=1" in str(ei.value)


def test_cli_help_all_zero_passes(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(0))
    assert "CLI --help 可用" in sc._cli()


def test_cli_help_script_paths_exist():
    """回归: 曾被引用的 scripts/make_prism_summary_pdf.py 不存在。"""
    for args in sc.CLI_HELP_CMDS:
        if args[0] != "-m":
            assert (sc.ROOT / args[0]).is_file(), args
