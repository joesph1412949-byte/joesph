# -*- coding: utf-8 -*-
"""qmt — miniQMT 相关代码集中地（桥接脚本 + 诊断工具）。

为什么要独立一包：桥接脚本原本散落在项目根，与策略代码混在一起，
既看不出"哪些是跑在 QMT 终端里的"，也容易误改。现在按**运行位置**分：

- ``bridge/``  **放进 QMT 终端里运行**的脚本（在 QMT 的 Python 环境里加载）。
  它们与 prism 主进程之间只通过 ``D:/QMT_SIGNALS/`` 下的 JSON 信号文件通信，
  **不 import 本项目任何模块**（自包含），因此可以在 QMT 的 GBK 解释器里单独跑。
- ``tools/``   在**本项目 Python 环境里手动跑**的一次性检查/探测脚本，全部只读。

安全约定：本包内所有代码都不得自动下单；真正下单只发生在 ``bridge/`` 里，
且受三道闸门约束（``D:/QMT_SIGNALS/paused`` 急停 / ``armed.txt`` 当日日期 /
当日 order_id 去重），外加同轮"卖出未受理则禁同标的买单"与价格 sanity。
桥端**每笔委托前重读** ``paused`` 与 ``armed``（批次中途按急停/撤防对剩余
委托立即生效）；``paused`` 期间 pending 文件**保留不删**，解除后可继续处理。

注意：``bridge/signal_bridge_demo.py`` 只跑 sim 通道，**没有**当日去重
（``HAS_DAILY_DEDUP = False``），不得用于 real；real 单只走
``signal_bridge_real.py``。
"""
