# -*- coding: utf-8 -*-
"""tt — A股底仓+浮仓 做T策略(miniQMT 实盘出口)。

定位: 在 cc-joesph 既有实盘链路上, 追加一个"日内高抛低吸"策略。
它复用而不是重造以下既有能力:
  - common.py                代码后缀 / 涨跌停幅度 / 日志
  - exit_rules.py            跌停判定
  - prism.live_account.py    账户只读查询(资产/持仓/可卖量)
  - prism.trader.py          信号文件落盘(桥端消费) + 暂停开关

安全模型(与项目既有惯例一致):
  1. 默认 dry_run=True, 必须显式 --live 才落信号;
  2. 本进程只写信号文件, 绝不调用任何下单接口;
  3. 真实盘最终闸门在桥端 armed.txt(含当日 YYYYMMDD);
  4. 风控前置: 时段/偏离/限额/次数/亏损/净敞口 六道校验, 任一不过即跳过;
  5. 卖出量硬约束在券商 can_use_volume 之内 —— T+1 天然合规。
"""

__all__ = ["config", "grid", "risk", "state", "market", "engine", "daemon"]
