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
当日 order_id 去重），外加同轮"卖出未受理则禁同标的买单"、价格/数量 sanity 与
可选的账号白名单（``ALLOWED_ACCOUNTS``/``MAX_ORDER_VOLUME`` 默认不启用）。
桥端**每笔委托前重读** ``paused`` 与 ``armed``（批次中途按急停/撤防对剩余
委托立即生效）；``paused`` 期间 pending 文件**保留不删**，解除后可继续处理。

注意：``bridge/signal_bridge_demo.py`` 只跑 sim 通道，**没有**当日去重
（``HAS_DAILY_DEDUP = False``）、也没有 paused 闸门 —— ``ENVIRONMENT="real"``
会被 ``_check_safety()`` **直接拒绝启动**（执行约束，不只是注释）；real 单只走
``signal_bridge_real.py``。

**刻意不做**（2026-09-19 复核后的决策，别反复提）：① 涨跌停区间校验 —— 桥刻意
自包含、没有昨收/涨跌停价，要做就得依赖 QMT 内 API（可用性未验证）；
② pid/锁文件排除"两个 QMT 终端同跑 real 桥" —— 锁是唯一会让真实委托被静默挡住、
且崩溃后需人工清锁才能恢复的机制，用它换掉一次操作失误得不偿失（改为每单重读
去重账 + 原子写，把窗口收窄到单笔）。
"""
