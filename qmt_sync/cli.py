"""CLI: python -m qmt_sync [--once] [--import-statement FILE] [--conf PATH] [--account-id ID]"""
from __future__ import annotations
import argparse
import logging
import threading

from qmt_sync.config import Config
from qmt_sync.db import QmtDb
from qmt_sync.sync import SyncEngine


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="qmt_sync", description="miniQMT 账户实时监控同步")
    ap.add_argument("--conf", default=None, help="qmt_sync.conf 路径")
    ap.add_argument("--account-id", default="", help="资金账号(留空则用配置文件/默认)")
    ap.add_argument("--once", action="store_true", help="同步一次后退出")
    ap.add_argument("--import-statement", metavar="FILE", help="Phase 2 对账单导入(未实现)")
    ap.add_argument("--version", action="store_true", help="打印版本")
    return ap


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print("qmt_sync 0.1.0 (v1 realtime)")
        return 0
    if args.import_statement:
        print("[qmt_sync] --import-statement 是 Phase 2 功能(历史对账单导入), v1 未实现, 已按设计推迟。",
              file=__import__("sys").stderr)
        return 2

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = Config.load(args.conf)
    if args.account_id:
        cfg.account_id = args.account_id
    if not cfg.account_id:
        print("[qmt_sync] 未配置 account_id(命令行 --account-id 或 qmt_sync.conf)。", file=__import__("sys").stderr)
        return 3

    from qmt_sync.qmt_client import make_client
    db = QmtDb(cfg.db_path)
    engine = SyncEngine(cfg, db, None)
    client = make_client(cfg, engine)
    client.start()
    client.connect(cfg.account_id)
    engine.client = client

    if args.once:
        engine.poll_once()
        print("[qmt_sync] once done, db=%s" % cfg.db_path)
        return 0

    print("[qmt_sync] watching account=%s every %ss -> %s" % (cfg.account_id, cfg.poll_interval_s, cfg.db_path))
    stop = threading.Event()
    try:
        engine.run(stop)
    except KeyboardInterrupt:
        stop.set()
    return 0
