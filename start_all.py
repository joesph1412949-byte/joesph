# -*- coding: utf-8 -*-
"""一键启动 qmt_sync + strategy_web + Vibe-Trading(后端+前端)。
用法: python start_all.py (start_all.bat 双击入口)
"""
import os
import subprocess
import sys
import webbrowser

PY37 = r"C:\Users\28037\AppData\Local\Programs\Python\Python37\python.exe"
QMT_LIB = r"D:\QMT\bin.x64\Lib\site-packages"
LOGS = r"D:\QMT_SYNC\logs"
ACCOUNT_ID = "88869979"

PROCS = [
    {
        "name": "qmt_sync",
        "cmd": [PY37, "-m", "qmt_sync", "--account-id", ACCOUNT_ID],
        "cwd": r"d:\cc-joesph",
        "env": {"PYTHONPATH": QMT_LIB},
    },
    {
        "name": "strategy_web",
        "cmd": [PY37, "app.py"],
        "cwd": r"d:\cc-joesph\strategy_web",
        # 不带 PYTHONPATH: xtquant 已通过 junction 链接进 Py3.7 site-packages,
        # 挂 QMT_LIB 会污染 numpy/pandas (QMT 自带 cp36 二进制)
        "env": {"APP_DEBUG": "0"},
    },
    {
        "name": "vibe_backend",
        "cmd": [r"D:\Vibe-Trading\.venv\Scripts\vibe-trading.exe", "serve", "--port", "8899"],
        "cwd": r"D:\Vibe-Trading",
        "env": {},
    },
    {
        "name": "vibe_frontend",
        # 必须用 npm.cmd: Windows 上 Popen 裸 "npm"(无扩展名) 找不到可执行文件,
        # CreateProcess 抛 FileNotFoundError → start_all 崩在第 4 步, 浏览器不打开。
        "cmd": ["npm.cmd", "run", "dev"],
        "cwd": r"D:\Vibe-Trading\frontend",
        "env": {},
    },
]

# Windows: 让子进程脱离当前控制台。否则 start_all.bat 的窗口一关,
# Windows 向该控制台所有进程发 CTRL_CLOSE_EVENT, strategy_web 等全部被杀
# (症状: 点 start_all 后服务正常, 关窗口就"网页打不开")。
_DETACH = getattr(subprocess, "DETACHED_PROCESS", 0)


def main() -> int:
    os.makedirs(LOGS, exist_ok=True)
    for p in PROCS:
        env = dict(os.environ)
        env.update(p["env"])
        log = open(os.path.join(LOGS, p["name"] + ".log"), "ab")
        subprocess.Popen(p["cmd"], cwd=p["cwd"], env=env,
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         creationflags=_DETACH)
        print("[start_all] launched %s (log=%s)" % (p["name"], p["name"] + ".log"))
    # 打开量化选股看板(用户入口); Vibe-Trading 前端在 http://localhost:5899
    webbrowser.open("http://localhost:5000")
    print("[start_all] all processes launched. 看板已打开 http://localhost:5000"
          " (Vibe前端 http://localhost:5899)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
