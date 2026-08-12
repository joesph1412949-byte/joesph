# qmt_sync v1 — miniQMT 账户实时监控同步

把 miniQMT 账户的资金/持仓/当日成交同步到 D:\QMT_SYNC\qmt_sync.db,
供 Vibe-Trading 的 `qmt_account` 工具查询。

## 前置
- QMT 终端已登录, 且开启 miniQMT 模式(券商 QMT 通常默认支持)
- xtquant 位于 D:\QMT\bin.x64\Lib\site-packages

## 运行(需 QMT 已登录)
```bash
cd /d/cc-joesph
PYTHONPATH=/d/QMT/bin.x64/Lib/site-packages \
  /c/Users/28037/AppData/Local/Programs/Python/Python37/python.exe -m qmt_sync --account-id <资金账号>
# 或: --once 同步一次后退出(用于验证)
PYTHONPATH=/d/QMT/bin.x64/Lib/site-packages \
  /c/Users/28037/AppData/Local/Programs/Python/Python37/python.exe -m qmt_sync --once --account-id <资金账号>
```

## 配置 D:\QMT_SYNC\qmt_sync.conf
```
db_path=D:/QMT_SYNC/qmt_sync.db
poll_interval_s=5
account_id=<资金账号>
```

## 告警规则 D:\QMT_SYNC\alert_rules.json
```json
{"position_ratio": {"enabled": true, "max": 0.30},
 "daily_loss": {"enabled": true, "max_loss_pct": 0.03},
 "stop_loss": {"enabled": true, "drop_pct": 0.08},
 "position_change": {"enabled": true}}
```
告警写入 alerts 表, 在 Vibe-Trading 对话里问 qmt_account 即可查看。

## 查询(在 Vibe-Trading 对话中)
"我现在账户什么情况" / "查我的持仓明细" / "最近有什么告警"

## Phase 2(未实现, 已占位)
`--import-statement <file>` 对账单导入 + 历史持仓重建。
