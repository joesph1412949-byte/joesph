# tt — A股「底仓 + 浮仓」做T策略（**已退役，见 `tt_solo/`**）

> **本文件是旧入口的指针，不再是操作手册。**
> 2026-09-16 起，做T策略整体抽取为自包含包 **`tt_solo/`**（模块逐字节相同，
> 有一份可证伪的对照取证）。**旧 `tt/` 与旧 `tt_web/`（面板 5010）已退役**：
> 不要再按本文件的历史命令启动任何东西 —— 旧守护写的是旧账本，新面板
> （5011）读的是 `tt_solo` 的账本，两者共用同一个 `D:/QMT_SIGNALS`，
> 照着旧命令跑会出现「**面板空账，真实委托照发**」。
>
> **操作手册唯一权威副本：[`tt_solo/README.md`](tt_solo/README.md)**
> （自包含性、6 种运行方式、三道闸门、T+1 约束、面板、测试、安全边界都在那里）

---

## 1. 现在该敲的命令（全部在 `tt_solo/` 下）

| 用途 | 命令 | 对应入口 |
|---|---|---|
| 守护 · 信号文件通道 · 演练（默认） | `python -m ttcore.daemon --interval 5` | `tifosi` 主菜单 1 |
| 守护 · QMT 模拟通道 · 演练 | `python -m ttcore.daemon --interval 5 --env sim` | `tifosi` 主菜单 3 |
| 守护 · 直连 miniQMT · 演练 | `python -m ttcore.daemon --direct --interval 5` | `tifosi` 主菜单 2 |
| 守护 · 直连 miniQMT · **实盘** ⚠️ | `python -m ttcore.daemon --direct --live --interval 5` | `tifosi` 高级选项 A → 1（再按 `Y` 确认） |
| 当日放行条 / 急停 | `python ttcore/arm_today.py` | `tifosi` 主菜单 5（急停/解除/撤销在高级选项 A） |
| 监控面板（**5011**） | `python dashboard/app.py` | `tifosi` 主菜单 4（闸门状态见主菜单 6） |

**每个交易日都必须重新放行一次**（`armed.txt` 只认当日 `YYYYMMDD`），这是刻意的人工确认点。

## 2. 三道闸门（串联短路，顺序不变）

| 闸门 | 位置 | 语义 |
|---|---|---|
| `dry_run` | 配置 / `--live` | 默认 `true`，只算不落盘；`--live` 只在内存里翻成 `false`，从不落盘 |
| `paused` | `D:/QMT_SIGNALS/paused` | 存在即整体停发。**创建 = 一键急停** |
| `armed` | `D:/QMT_SIGNALS/<env>/armed.txt` | 必须含**当日** `YYYYMMDD`；昨天的条今天自动作废 |

`arm_today.py` 只操作 `real` 通道；`--env sim` 的 `sim/armed.txt` 得自己写
（生成侧比桥端更严是刻意的：不能因为桥松就跟着松）。

## 3. QMT 桥端（`qmt/`，与本次抽取无关，仍然有效）

信号文件通道由 QMT 终端内的桥脚本消费信号；这部分知识不在 `tt_solo/` 里：

- QMT 内是 **GBK**，桥脚本刻意全用英文注释，别改成中文；
- `FILE_MIN_AGE` 建议 `1.0 → 0.2`（做T对延迟敏感；策略侧已原子写，1 秒是纯等待）；
- 日去重已从 `stock_code` 改为 **`order_id`**（原逻辑会让每票每天只放行 1 单，多档全废）；
- `passorder` 的 `opType` 是 **0/1**，与外部 API 的 `STOCK_BUY=23/24` 不是一套，别混；
- `qmt/bridge/signal_bridge_demo.py`（sim）**不校验账户**，会用 QMT 当前登录的账户报单 ——
  跑模拟通道前务必确认 QMT 登在模拟号上。

参数对应与 A1（QMT 内原生写网格）对照见 **`docs/大QMT网格参数手册.md`**。

---

> 本策略仅供研究与技术演示，不构成投资建议。做T属高频交易行为，实际成交受
> 流动性、滑点与执行纪律影响，可能产生显著偏离；历史表现不代表未来收益。
