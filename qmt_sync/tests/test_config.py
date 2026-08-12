import os, tempfile
from qmt_sync.config import Config

def test_defaults():
    cfg = Config.load()  # 无配置文件 -> 默认值
    assert cfg.db_path == r"D:\QMT_SYNC\qmt_sync.db"
    assert cfg.poll_interval_s == 5.0

def test_load_from_file(tmp_path):
    conf = tmp_path / "qmt_sync.conf"
    conf.write_text("# test\npoll_interval_s=7\ndb_path=D:/x.db\n", encoding="utf-8")
    cfg = Config.load(str(conf))
    assert cfg.poll_interval_s == 7.0
    assert cfg.db_path == "D:/x.db"

def test_alert_rules_json(tmp_path):
    rules = tmp_path / "alert_rules.json"
    rules.write_text('{"daily_loss": {"enabled": false}}', encoding="utf-8")
    cfg = Config.load(alert_path=str(rules))
    assert cfg.alert_rules == {"daily_loss": {"enabled": False}}
