from qmt_sync.config import Config

def test_defaults(tmp_path):
    # 显式指向不存在的路径。旧写法 Config.load() 读机器绝对路径
    # D:\QMT_SYNC\qmt_sync.conf, 靠「该文件恰好不存在」为绿 —— 实测在部署目录
    # 放一个 poll_interval_s=7 的配置, 旧写法立刻变红。
    cfg = Config.load(conf_path=str(tmp_path / "no.conf"),
                      alert_path=str(tmp_path / "no.json"))
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
