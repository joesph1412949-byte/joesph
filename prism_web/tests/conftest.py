# -*- coding: utf-8 -*-
"""prism_web 测试隔离: 每个测试前重扫因子库。

问题: prism/tests 的 test_registry.py / test_trader.py 会 reg.reset() 并注册
假因子(NG1/NG2/NG3/AS1 等)。当 prism/tests 与 prism_web/tests 同进程合跑时,
残留的注册表状态会让本套件假设的 26 个真实因子缺失 → /api/factors 与
策略加载(load_strategy 校验因子存在性)失败。

修复: autouse fixture 在每个测试前 reset + force 重扫 prism.factors,
保证本套件看到的注册表永远是 26 个真实因子, 与执行顺序无关。
"""
import pytest

from prism import registry as reg


@pytest.fixture(autouse=True)
def _fresh_factor_registry():
    reg.reset()
    # force=True: app.py import 时已扫过 prism.factors, 无 force 时 importlib
    # 一次性语义使重扫为 no-op → FACTORS 空 → 策略加载全部 UnknownFactorError
    reg.scan_factors("prism.factors", force=True)
    yield
