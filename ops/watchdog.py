# -*- coding: utf-8 -*-
"""watchdog.py — 进程守护: 监控 datasource / vibe_backend / vibe_frontend,
进程挂了自动拉起, 避免服务静默死亡(网页打不开的常见原因之一)。

用法:
  python watchdog.py            # 前台运行(建议配合任务计划程序开机自启)
  python watchdog.py --once     # 只检查一次并拉起缺失服务后退出(可挂计划任务每小时跑)

依赖 start_all.py 的启动方式(PROCS 定义), 每个服务带独立日志文件。
"""
import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 项目根
from shared.common import LOG_DIR, setup_logging

logger = setup_logging("watchdog")

# 与 start_all.py 保持一致的服务定义(端口 → 探测目标)
SERVICES = [
    {
        "name": "prism_web",
        "port": 5000,
        "cmd": [r"C:\Users\28037\AppData\Local\Programs\Python\Python312\python.exe",
                r"prism_web\app.py"],
        "cwd": r"D:\cc-joesph",
        "env": {"APP_DEBUG": "0"},
    },
    {
        "name": "vibe_backend",
        "port": 8899,
        "cmd": [r"D:\Vibe-Trading\.venv\Scripts\vibe-trading.exe",
                "serve", "--port", "8899"],
        "cwd": r"D:\Vibe-Trading",
        "env": {},
    },
    {
        "name": "vibe_frontend",
        "port": 5899,
        "cmd": ["npm.cmd", "run", "dev"],
        "cwd": r"D:\Vibe-Trading\frontend",
        "env": {},
    },
]

# 与 start_all.py 相同的脱离控制台策略: 关窗口不杀子进程
_DETACH = getattr(subprocess, "DETACHED_PROCESS", 0)

# 冷却: 同一服务 30 秒内最多拉起一次(防崩溃-重启死循环)
_COOLDOWN_S = 30
_last_launch = {}


def port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure_service(svc):
    name, port = svc["name"], svc["port"]
    if port_open(port):
        return False  # 正常, 无需动作
    now = time.time()
    if now - _last_launch.get(name, 0) < _COOLDOWN_S:
        logger.info("%s 端口 %d 未监听, 但在冷却期内, 跳过拉起", name, port)
        return False
    logger.warning("%s 端口 %d 未监听, 尝试拉起...", name, port)
    env = dict(os.environ)
    env.update(svc.get("env", {}))
    log = open(LOG_DIR / ("watchdog_" + name + ".log"), "ab")
    try:
        subprocess.Popen(svc["cmd"], cwd=svc["cwd"], env=env,
                         stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                         creationflags=_DETACH)
        _last_launch[name] = now
        logger.info("%s 拉起命令已发出", name)
        return True
    except Exception as e:
        logger.error("%s 拉起失败: %r", name, e)
        return False


def main():
    ap = argparse.ArgumentParser(description="QMT 量化服务进程守护")
    ap.add_argument("--once", action="store_true",
                    help="只检查一次并拉起缺失服务后退出")
    ap.add_argument("--interval", type=float, default=30.0,
                    help="轮询间隔秒数(默认 30)")
    args = ap.parse_args()

    logger.info("watchdog 启动: 监控 %s", ", ".join(s["name"] for s in SERVICES))
    while True:
        for svc in SERVICES:
            ensure_service(svc)
        if args.once:
            logger.info("--once: 单次检查完成, 退出")
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
