# -*- coding: utf-8 -*-
"""注册表隔离: 每个测试后把 FACTORS 还原到测试前状态, 防止测试因子污染其他套件。

另含全局"离线测试"约束: 测试期间关闭通达信取数, 保证单测不联网、结果确定。
"""
import pytest
from prism import registry as reg


@pytest.fixture(autouse=True, scope="session")
def _tdx_offline():
    """测试期间禁用真实网络数据源, 保持 fail-open 语义可断言。

    不禁用会导致: 注入的假数据源明明抛异常, 通达信兜底却真去联网拉到 260 根
    真实日K, 使 "数据缺失 → None" 这类断言失效(踩过一次)。
    """
    try:
        from prism import tdx_source
    except Exception:
        yield
        return
    prev = tdx_source.is_enabled()
    tdx_source.set_enabled(False)
    try:
        yield
    finally:
        tdx_source.set_enabled(prev)
        tdx_source.close()


@pytest.fixture(autouse=True)
def _registry_snapshot_restore():
    snapshot = {fid: dict(meta) for fid, meta in reg.FACTORS.items()}
    yield
    # 删除测试新增的
    for fid in list(reg.FACTORS):
        if fid not in snapshot:
            del reg.FACTORS[fid]
    # 恢复被覆盖的
    for fid, meta in snapshot.items():
        reg.FACTORS[fid] = meta
