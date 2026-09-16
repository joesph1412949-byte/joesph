# -*- coding: utf-8 -*-
"""自包含硬护栏: tt_solo 不得依赖主项目任何模块。

这是本次重构的完成定义 —— 用测试固化, 防止将来有人顺手 import 回去。
"""
import importlib
from pathlib import Path

from ._import_scan import FORBIDDEN_ROOT_IMPORTS, forbidden_imports

ROOT = Path(__file__).resolve().parents[1]        # tt_solo/


def _py_files():
    return [p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_scanner_discriminates():
    """负控: 扫描器必须真能判别禁用 import(否则整条护栏是空的)。"""
    assert forbidden_imports("x.py", "import prism\n") == ["prism"]
    assert forbidden_imports("x.py", "import prism.foo\n") == ["prism"]
    assert forbidden_imports("x.py", "def f():\n    import shared\n") == ["shared"]
    assert forbidden_imports("x.py", "from . import grid\n") == []
    assert forbidden_imports("x.py", "from .broker import X\n") == []
    # 散文里的 prism 不算依赖(这正是选 AST 的理由)
    assert forbidden_imports("x.py", '"""与 prism.trader 同构"""\n') == []
    # 禁用清单本身不得被改窄
    assert {"prism", "shared", "qmt_sync", "backtest", "legacy"} <= \
        FORBIDDEN_ROOT_IMPORTS


def test_no_forbidden_imports_anywhere():
    bad = []
    for p in _py_files():
        hits = forbidden_imports(p)
        if hits:
            bad.append("%s: %s" % (p.relative_to(ROOT), hits))
    assert bad == [], "tt_solo 出现外部依赖: %s" % bad


def test_core_modules_importable_without_qmt():
    """核心模块必须能在没有 xtquant 的环境导入(惰性加载)。"""
    for name in ("ttcore._vendor", "ttcore.grid", "ttcore.risk",
                 "ttcore.state", "ttcore.config", "ttcore.broker",
                 "ttcore.market", "ttcore.engine", "ttcore.executor",
                 "ttcore.daemon"):
        importlib.import_module(name)
