# -*- coding: utf-8 -*-
"""模拟盘 CLI + gitignore 测试 — 全离线。"""
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


def test_gitignore_covers_paper_state():
    txt = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".paper_account.json" in txt
    assert ".paper_account.json.tmp" in txt
