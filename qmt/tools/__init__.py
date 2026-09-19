# -*- coding: utf-8 -*-
"""qmt.tools — miniQMT 只读诊断/自检工具（在本项目 Python 环境里跑）。

- ``live_check.py`` （原项目根 ``qmt_live_check.py``）
    实盘接入前必跑的**只读就绪自检**：环境/接口/数据格式/配置/运行/实盘出口
    共 30+ 项。只做 ``query_*`` 与方法存在性检查，**绝不调用下单接口**。
    退出码 0=全绿 / 1=有阻断 / 2=异常。
    用法::

        python -m qmt.tools.live_check            # 全量
        python -m qmt.tools.live_check --quiet    # 只看问题项
"""
