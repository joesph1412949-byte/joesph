# -*- coding: utf-8 -*-
"""注册表隔离: 每个测试后把 FACTORS 还原到测试前状态, 防止测试因子污染其他套件。

另含全局"离线测试"约束: 测试期间关闭通达信取数, 保证单测不联网、结果确定。
"""
import pytest
from prism import registry as reg

# 真实因子库的哨兵 id: 存在于即认为真实库已加载。
# （test_backtest / test_registry 等会用 reg.reset() 换成自己的测试因子）
_REAL_SENTINEL = "SEC1"
# 会话级"干净基线": 首次见到真实因子库时快照一次, 供被污染的用例兜底还原。
_pristine = None


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
    global _pristine
    if _pristine is None and _REAL_SENTINEL in reg.FACTORS:
        # 第一次见到真实因子库 → 记成会话基线(此时尚未被任何测试动过)
        _pristine = {fid: dict(meta) for fid, meta in reg.FACTORS.items()}

    snapshot = {fid: dict(meta) for fid, meta in reg.FACTORS.items()}
    yield
    # 删除测试新增的
    for fid in list(reg.FACTORS):
        if fid not in snapshot:
            del reg.FACTORS[fid]
    # 恢复被覆盖的
    for fid, meta in snapshot.items():
        reg.FACTORS[fid] = meta

    # ---- 真实因子库保底自愈 ----
    # 有些用例(test_backtest/test_registry 等)在自己的 autouse fixture 里直接
    # reg.reset() 再注册测试因子; 由于 pytest 的 autouse fixture 实例化顺序,
    # 本 fixture 的 snapshot 有可能在它们 reset 之后才取到 —— 于是快照里只有
    # 1~2 个测试因子, "还原"反而把真实因子库从整个 session 里抹掉。表现为后续
    # 依赖真实库的用例(如 test_sector_factors 直接 get_factor("SEC1"))整片
    # UnknownFactorError。
    # 这里以哨兵判定: 还原后真实库若缺席, 就用会话基线(或 force 重扫)补回,
    # 保证"还原"的结果永远是有效状态。
    if _REAL_SENTINEL not in reg.FACTORS:
        reg.reset()
        if _pristine:
            for fid, meta in _pristine.items():
                reg.FACTORS[fid] = dict(meta)
        else:
            reg.scan_factors("prism.factors", force=True)
