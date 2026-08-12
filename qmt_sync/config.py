"""配置加载: qmt_sync.conf(INI 风格) + alert_rules.json, 缺省用默认值。"""
from __future__ import annotations
import json
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONF_PATH = r"D:\QMT_SYNC\qmt_sync.conf"
DEFAULT_ALERT_RULES_PATH = r"D:\QMT_SYNC\alert_rules.json"


@dataclass
class Config:
    db_path: str = r"D:\QMT_SYNC\qmt_sync.db"
    data_dir: str = r"D:\QMT_SYNC"
    qmt_data_dir: str = r"D:\QMT\userdata_mini"
    poll_interval_s: float = 5.0
    account_id: str = ""
    alert_rules: dict = field(default_factory=dict)

    def _set(self, key: str, value: str) -> None:
        if key == "db_path":
            self.db_path = value
        elif key == "data_dir":
            self.data_dir = value
        elif key == "qmt_data_dir":
            self.qmt_data_dir = value
        elif key == "poll_interval_s":
            self.poll_interval_s = float(value)
        elif key == "account_id":
            self.account_id = value

    @staticmethod
    def load(conf_path: str | None = None, alert_path: str | None = None) -> "Config":
        cfg = Config()
        p = Path(conf_path or DEFAULT_CONF_PATH)
        if p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                try:
                    cfg._set(k.strip(), v.strip())
                except ValueError:
                    pass  # 坏行忽略, 保持默认
        rules: dict = {}
        rp = Path(alert_path or DEFAULT_ALERT_RULES_PATH)
        if rp.is_file():
            try:
                rules = json.loads(rp.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                rules = {}
        cfg.alert_rules = rules
        return cfg
