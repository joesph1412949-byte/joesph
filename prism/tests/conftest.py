# -*- coding: utf-8 -*-
"""注册表隔离: 每个测试后把 FACTORS 还原到测试前状态, 防止测试因子污染其他套件。"""
import pytest
from prism import registry as reg


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
