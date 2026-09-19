# -*- coding: utf-8 -*-
"""backtest — 离线回测包。

- ``cli.py`` （原项目根的 ``backtest_cli.py``）
    命令行入口。接的是**新版引擎** ``prism.backtest.Backtester``（36 因子、板块分、
    市场上下文齐全），支持 ``--oos`` 样本外验证。

用法::

    python -m backtest.cli --start 20260701 --end 20260731
    python -m backtest.cli --start 20260701 --end 20260731 --oos
    python -m backtest.cli --help

注意：回测当前策略请用 ``backtest.cli``（走 prism 引擎）。
"""
