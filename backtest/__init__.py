# -*- coding: utf-8 -*-
"""backtest — 离线回测包。

- ``engine.py`` （原项目根的 ``backtest.py``）
    轻量离线回测引擎 ``BacktestEngine``：只用东财公开数据（历史涨停池 + 日K收盘价）
    回放"环境门槛 + 主线题材 + 连板高度"这套规则，与 QMT 完全解耦，可离线跑任意区间。
    **这是早期简化版**，用于验证规则与调参。

- ``cli.py`` （原项目根的 ``backtest_cli.py``）
    命令行入口。接的是**新版引擎** ``prism.backtest.Backtester``（36 因子、板块分、
    市场上下文齐全），支持 ``--compare`` 网格与 ``--oos`` 样本外。

用法::

    python -m backtest.cli --start 20260701 --end 20260731
    python -m backtest.cli --start 20260701 --end 20260731 --compare
    python -m backtest.cli --start 20260701 --end 20260731 --use-market-data

注意：``engine.py`` 与 ``prism/backtest.py`` 是**两套不同世代**的回测实现，
回测当前策略请用 ``backtest.cli``（走 prism 引擎）。
"""
from .engine import BacktestEngine  # noqa: F401

__all__ = ["BacktestEngine"]
