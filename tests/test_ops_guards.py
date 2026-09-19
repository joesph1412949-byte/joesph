# -*- coding: utf-8 -*-
"""ops 启动链守卫(2026-09-19 批次) — 全离线, 绝不拉起任何真实服务。

覆盖:
- C1 端口归属: watchdog 单实例锁(锁端口被占 → 跳过, 退出码 0)
- I1 install_watchdog.bat 计划任务命令行(cmd 的 `cd /d` 在 PowerShell 里非法)
- I2 make_summary_pdf.py `--help` 不得写文件(旧版会真生成 PDF) + prod_dirs 全部存在
- I3 watchdog: 日志句柄不泄漏 / `--once` 拉起失败必须非 0 / 启动前跳过已监听端口

所有拉起路径都打桩 `subprocess.Popen` + `webbrowser.open`, 只有 assert 与临时目录。
"""
import socket
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ops.start_all as sa
import ops.watchdog as w


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _fake_svc(name="fake"):
    return {"name": name, "port": 1, "cmd": ["x"], "cwd": ".", "env": {}}


# ================= C1/I3: watchdog 单实例锁 =================

def test_watchdog_singleton_lock_is_exclusive(monkeypatch):
    monkeypatch.setattr(w, "_LOCK_PORT", _free_port())
    first = w._acquire_singleton()
    assert first is not None
    try:
        assert w._acquire_singleton() is None      # 第二个实例拿不到
    finally:
        first.close()
    again = w._acquire_singleton()                 # 进程退出/释放后可再拿
    assert again is not None
    again.close()


def test_watchdog_once_skips_when_instance_running(monkeypatch):
    """已有实例占着锁 → 不重复拉起, 且退出码 0(跳过不是失败)。"""
    monkeypatch.setattr(w, "_LOCK_PORT", _free_port())
    monkeypatch.setattr(w, "SERVICES", [_fake_svc()])
    launched = []
    monkeypatch.setattr(w.subprocess, "Popen",
                        lambda *a, **k: launched.append(a))
    holder = w._acquire_singleton()
    try:
        assert w.main(["--once"]) == 0
    finally:
        holder.close()
    assert launched == []


# ================= I3: --once 退出码 =================

def test_watchdog_once_nonzero_on_launch_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "_LOCK_PORT", _free_port())
    monkeypatch.setattr(w, "SERVICES", [_fake_svc()])
    monkeypatch.setattr(w, "LOG_DIR", tmp_path)
    monkeypatch.setattr(w, "port_open", lambda *a, **k: False)

    def _boom(*a, **k):
        raise OSError("boom")

    monkeypatch.setattr(w.subprocess, "Popen", _boom)
    assert w.main(["--once"]) == 1                 # 拉起失败不再报成功


def test_watchdog_once_zero_when_all_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "_LOCK_PORT", _free_port())
    monkeypatch.setattr(w, "SERVICES", [_fake_svc()])
    monkeypatch.setattr(w, "port_open", lambda *a, **k: True)
    assert w.main(["--once"]) == 0


def test_watchdog_once_zero_after_successful_launch(tmp_path, monkeypatch):
    monkeypatch.setattr(w, "_LOCK_PORT", _free_port())
    monkeypatch.setattr(w, "SERVICES", [_fake_svc()])
    monkeypatch.setattr(w, "LOG_DIR", tmp_path)
    monkeypatch.setattr(w, "port_open", lambda *a, **k: False)
    monkeypatch.setattr(w.subprocess, "Popen", lambda *a, **k: None)
    assert w.main(["--once"]) == 0


# ================= I3: 日志句柄显式关闭 =================

_HANDLE_DRIVER = '''# -*- coding: utf-8 -*-
import sys
from pathlib import Path
ROOT = Path(r"%s")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ops"))
import watchdog as w
tmp = Path(sys.argv[1]); tmp.mkdir(parents=True, exist_ok=True)
w.LOG_DIR = tmp
w.port_open = lambda *a, **k: False
w._last_launch = {}
w.subprocess.Popen = lambda *a, **k: None      # 不打桩就会真拉起服务
print("ensure_service ->", w.ensure_service(
    {"name": "handleprobe", "port": 1, "cmd": ["x"], "cwd": ".", "env": {}}))
print("LOGDONE")
'''


def test_watchdog_closes_log_handle(tmp_path):
    """拉起后必须显式关闭日志句柄(`with open`)。

    CPython 引用计数会在函数返回时兜底析构 —— 所以"能否删掉该文件"分不出对错
    (旧代码也删得掉)。判据改用 `-W error::ResourceWarning`: 旧代码会打
    "unclosed file", 改成 with 后一条都没有。
    """
    drv = tmp_path / "drv.py"
    drv.write_text(_HANDLE_DRIVER % ROOT, encoding="utf-8")
    r = subprocess.run([sys.executable, "-W", "error::ResourceWarning",
                        str(drv), str(tmp_path / "logs")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    assert "LOGDONE" in r.stdout, (r.stdout, r.stderr[-800:])
    assert "unclosed file" not in (r.stdout + r.stderr), r.stderr[-1200:]


# ================= C1: start_all 启动前跳过已监听端口 =================

def test_start_all_skips_listening_port(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sa, "LOGS", str(tmp_path))
    monkeypatch.setattr(sa, "port_open", lambda port, *a, **k: port == 8899)
    launched = []
    monkeypatch.setattr(sa.subprocess, "Popen",
                        lambda cmd, **k: launched.append(" ".join(cmd)))
    monkeypatch.setattr(sa.webbrowser, "open", lambda url: None)

    assert sa.main() == 0
    joined = " | ".join(launched)
    assert "8899" not in joined                  # 已监听 → 不再拉起
    assert "qmt_sync" in joined                  # 无端口的进程照旧拉起
    assert "prism_web" in joined
    out = capsys.readouterr().out
    assert "vibe_backend" in out and "跳过" in out


# ================= I2: make_summary_pdf =================

def _pdf_stat():
    p = (ROOT / "docs" / "reports"
         / ("prism项目总结_招聘用_%s.pdf" % date.today().strftime("%Y%m%d")))
    return None if not p.exists() else (p.stat().st_mtime_ns, p.stat().st_size)


def test_make_summary_pdf_help_writes_nothing():
    """旧版无参数解析: `--help` 会直接 build() 往 docs/reports/ 写 PDF。"""
    before = _pdf_stat()
    r = subprocess.run([sys.executable, "ops/make_summary_pdf.py", "--help"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode == 0, (r.returncode, r.stdout[-500:], r.stderr[-500:])
    assert "用法" in r.stdout
    assert _pdf_stat() == before


def test_make_summary_pdf_unknown_arg_writes_nothing():
    before = _pdf_stat()
    r = subprocess.run([sys.executable, "ops/make_summary_pdf.py", "--dry-run"],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    assert r.returncode != 0                     # 未识别参数 → fail-closed
    assert _pdf_stat() == before


def test_make_summary_pdf_prod_dirs_all_exist():
    import ops.make_summary_pdf as msp
    missing = [d for d in msp.PROD_DIRS if not (ROOT / d).is_dir()]
    assert missing == []
    assert "strategy_web" not in msp.PROD_DIRS   # 该目录早已不存在


# ================= I1: 计划任务命令行 =================

def test_install_watchdog_bat_uses_powershell_syntax():
    txt = (ROOT / "install_watchdog.bat").read_text(encoding="utf-8")
    create = [l for l in txt.splitlines()
              if l.strip().startswith("schtasks /Create")]
    assert len(create) == 1, create
    tr = create[0]
    assert "cd /d" not in tr          # cmd 语法, 在 PowerShell 里是非法位置参数
    assert "-Command" in tr
    assert "%PY312%" in tr and "%SCRIPT%" in tr
    assert "Python312" in txt         # python 用绝对路径, 不依赖 PATH 解析
    # 脚本也用绝对路径(watchdog.py 自己 sys.path.insert 项目根 → cwd 无关)
    assert r"set SCRIPT=D:\cc-joesph\ops\watchdog.py" in txt
