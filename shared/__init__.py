# -*- coding: utf-8 -*-
"""shared — 跨项目共享底座（被所有子项目 import，自身不依赖任何子项目）。

本目录职责（**只放"谁都要用"的东西**，不要往这里塞业务逻辑）：

- ``common.py``
    路径与代码规则中枢：
      * ``PROJECT_ROOT`` / ``RUNTIME_DIR`` / ``CACHE_DIR`` / ``STATE_DIR`` / ``LOG_DIR``
        —— 全项目唯一的路径真相来源
      * ``SIGNAL_ROOT``（``D:/QMT_SIGNALS``）—— 与 QMT 桥的信号文件目录
      * ``SECTORS`` —— 默认科技板块池
      * ``setup_logging()`` —— 统一日志（按天轮转）
      * ``with_market_suffix()`` / ``em_code_to_tick_key()`` —— A股代码加交易所后缀
      * ``limit_ratio_for_code()`` —— 按板块返回涨跌停比例（主板10/创业科创20/北交30）

- ``exit_rules.py``
    卖出规则引擎（纯逻辑、无 IO）：
      * ``ExitRule`` —— 止盈 / 止损 / 持有期 / T+1（``enforce_t1``）
      * ``PositionBook`` —— 持仓档案（可承载券商 ``can_use_volume``）
      * ``is_limit_down()`` —— 跌停判定（跌停顺延卖出）

导入约定：各组件先把**项目根**注入 ``sys.path``，再 ``from shared.xxx import ...``。
放在此目录而不是项目根，是为了让"根目录只留入口和文档"。
"""
