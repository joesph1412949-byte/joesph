# -*- coding: utf-8 -*-
"""qmt.bridge — 放进 QMT 终端里运行的信号桥（自包含，不 import 本项目模块）。

- ``signal_bridge_real.py``  实盘桥：读 ``D:/QMT_SIGNALS/real/pending/*.json``，
  校验三道闸门后调用 ``order_stock`` 下单。**这是唯一会真下单的脚本。**
- ``signal_bridge_demo.py``  模拟通道桥：读 ``sim/`` 目录，用于演练。
  ⚠️ 它**不校验账户**，下单用的是 QMT 当前登录的账户 —— 用之前务必确认
  QMT 登的是模拟账号。
- ``connection.py``          ``XtQuantTrader`` 连接/回调的薄封装示例。

用法：在 QMT 终端里新建策略，把对应脚本内容粘进去运行（不是在本项目里跑）。
"""
