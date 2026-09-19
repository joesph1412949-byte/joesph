# -*- coding: utf-8 -*-
"""watchdog.py — 进程守护: 监控 datasource / vibe_backend / vibe_frontend,
进程挂了自动拉起, 避免服务静默死亡(网页打不开的常见原因之一)。

用法:
  python watchdog.py            # 前台运行(建议配合任务计划程序开机自启)
  python watchdog.py --once     # 只检查一次并拉起缺失服务后退出(可挂计划任务每小时跑)

单实例(2026-09-19): 常驻守护与 --once 同时启动时后者直接跳过(锁端口 5123),
避免两个 watchdog 各自按进程内冷却表把同一服务拉两次。退出码: 拉起失败 → 1。

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

# 冷却: 同一服务 30 秒内最多拉起一次(防崩溃-重启死循环)。
# 只在进程内内存即可 —— 有了下面的单实例锁, 同一时刻只有一个 watchdog 在跑。
_COOLDOWN_S = 30
_last_launch = {}

# 单实例锁端口(2026-09-19 C1/I3): 进程活着就占住, 退出/被杀由 OS 立即释放
# (自愈, 没有陈旧锁要清理)。没有它时, 常驻守护与计划任务的 --once 可以同时跑,
# 两边各自用进程内冷却表判断"该拉起" → 同一服务被拉起两次。
_LOCK_PORT = 5123
# 单实例锁的 socket 引用: 必须活着, 否则 CPython 立刻析构它 → 锁静默失效
_lock_sock = None

# ensure_service 的返回码(调用方据此定退出码, 见 --once)
_NOOP, _LAUNCHED, _LAUNCH_FAILED = 0, 1, 2


def port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _acquire_singleton():
    """单实例锁: 占到锁端口 → 返回 socket(调用方需持有到退出); 已被占 → None。

    不 listen 也不 accept, 所以不会进 TIME_WAIT, 端口立刻可复用。
    额外把 socket 存进模块全局: socket 一旦没人引用, CPython 引用计数会立刻
    关闭它 → 锁**静默失效**(实测踩过)。调用方仍应持有返回值。
    """
    global _lock_sock
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", _LOCK_PORT))
    except OSError:
        s.close()
        return None
    _lock_sock = s
    return s


def ensure_service(svc):
    """0=无需动作(已监听/冷却中), 1=已拉起, 2=拉起失败。"""
    name, port = svc["name"], svc["port"]
    if port_open(port):
        return _NOOP  # 正常, 无需动作
    now = time.time()
    if now - _last_launch.get(name, 0) < _COOLDOWN_S:
        logger.info("%s 端口 %d 未监听, 但在冷却期内, 跳过拉起", name, port)
        return _NOOP
    logger.warning("%s 端口 %d 未监听, 尝试拉起...", name, port)
    env = dict(os.environ)
    env.update(svc.get("env", {}))
    # with: 句柄在 Popen 拿到副本后立刻关掉(旧写法靠 CPython 引用计数兜底,
    # 在 -W error::ResourceWarning 下会报 "unclosed file")。
    try:
        with open(LOG_DIR / ("watchdog_" + name + ".log"), "ab") as log:
            subprocess.Popen(svc["cmd"], cwd=svc["cwd"], env=env,
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             creationflags=_DETACH)
    except Exception as e:
        logger.error("%s 拉起失败: %r", name, e)
        return _LAUNCH_FAILED
    _last_launch[name] = now
    logger.info("%s 拉起命令已发出", name)
    return _LAUNCHED


def main(argv=None):
    ap = argparse.ArgumentParser(description="QMT 量化服务进程守护")
    ap.add_argument("--once", action="store_true",
                    help="只检查一次并拉起缺失服务后退出")
    ap.add_argument("--interval", type=float, default=30.0,
                    help="轮询间隔秒数(默认 30)")
    args = ap.parse_args(argv)

    # --help 走 argparse 直接退出(rc=0, ops/smoke_check.py 依赖这一点), 锁在其后。
    lock = _acquire_singleton()
    if lock is None:
        logger.info("已有 watchdog 实例在运行(单实例锁 127.0.0.1:%d), 本次跳过",
                    _LOCK_PORT)
        return 0
    rc = 0  # 0=一切正常/无需动作; 1=至少一个服务拉起失败(不再"失败也报成功")
    try:
        logger.info("watchdog 启动: 监控 %s",
                    ", ".join(s["name"] for s in SERVICES))
        while True:
            for svc in SERVICES:
                if ensure_service(svc) == _LAUNCH_FAILED:
                    rc = 1
            if args.once:
                logger.info("--once: 单次检查完成, 退出(rc=%d)", rc)
                return rc
            time.sleep(args.interval)
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
