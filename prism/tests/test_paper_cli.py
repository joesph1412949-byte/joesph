# -*- coding: utf-8 -*-
"""模拟盘 CLI + gitignore 测试 — 全离线。"""
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import prism.paper_daemon as dm
from prism.paper import PaperAccount, main


ROOT = Path(__file__).parent.parent.parent


def test_cli_init_idempotent(tmp_path, capsys):
    main(["--init"], account=PaperAccount(state_path=tmp_path / "p.json"))
    assert (tmp_path / "p.json").exists()
    main(["--init"], account=PaperAccount(state_path=tmp_path / "p.json"))
    out = capsys.readouterr().out
    assert "已初始化" in out


def test_cli_summary(tmp_path, capsys):
    main(["--init"], account=PaperAccount(state_path=tmp_path / "p.json"))
    main(["--summary"], account=PaperAccount(state_path=tmp_path / "p.json"))
    out = capsys.readouterr().out
    assert '"exists": true' in out


def test_cli_once_without_qmt(tmp_path, capsys, monkeypatch):
    def fail_connect(self, max_retry=10, retry_wait=5):
        return False
    monkeypatch.setattr(dm.PaperDaemon, "connect_provider", fail_connect)
    main(["--once"], account=PaperAccount(state_path=tmp_path / "p.json"))
    assert "QMT 连接失败" in capsys.readouterr().out


def test_cli_once_tick_error_wrapped(tmp_path, capsys, monkeypatch):
    """M-g: --once 单轮异常 → 打印"单轮执行失败"而非裸 traceback。"""
    monkeypatch.setattr(dm.PaperDaemon, "connect_provider",
                        lambda self, max_retry=10, retry_wait=5: True)

    def boom(self):
        raise RuntimeError("tick boom")
    monkeypatch.setattr(dm.PaperDaemon, "tick_once", boom)
    main(["--once"], account=PaperAccount(state_path=tmp_path / "p.json"))
    out = capsys.readouterr().out
    assert "单轮执行失败" in out


def test_gitignore_covers_paper_state():
    """真名必须被忽略 —— 账本 + shared/common.py atomic_write 的
    `<path>.<pid>.<tid>.tmp`(实测名 .paper_account.json.40120.51234.tmp)。

    旧断言查的是 .gitignore 的**文本**里有没有 ".paper_account.json.tmp" 这个
    子串(恒真形态): 规则写死旧 tmp 名、真实文件一个都不匹配, 也照样绿。
    """
    for name in (".paper_account.json",
                 ".paper_account.json.40120.51234.tmp",
                 "screen_result.json.40120.51234.tmp",
                 "limitup_result.json.40120.51234.tmp",
                 "Joesph_key.pem",
                 ".workbuddy/x.json",
                 "prism_web/_ui_backup_20260917/index.html",
                 "_scratch.py", "_scratch.txt"):
        r = subprocess.run(["git", "check-ignore", "-q", name],
                           cwd=str(ROOT), capture_output=True, text=True)
        assert r.returncode == 0, "未被忽略: %s" % name


def test_gitignore_does_not_overreach():
    """--no-index: 只判规则本身, 不让「已被 git 跟踪」掩盖误伤。

    `/` 锚定的 /_*.py 若漏掉前导 `/`, prism/_utils.py 这类子目录文件会被误伤。
    """
    for name in ("prism/_utils.py", "prism/paper.py", "docs/reports/x.md"):
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", name],
                           cwd=str(ROOT), capture_output=True, text=True)
        assert r.returncode == 1, "被误伤: %s" % name
