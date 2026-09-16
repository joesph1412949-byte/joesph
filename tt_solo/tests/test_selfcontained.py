# -*- coding: utf-8 -*-
"""自包含硬护栏: tt_solo 不得依赖主项目任何模块。

这是本次重构的完成定义 —— 用测试固化, 防止将来有人顺手 import 回去。
"""
import subprocess
import sys
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


CORE_MODULES = ("ttcore._vendor", "ttcore.grid", "ttcore.risk",
                "ttcore.state", "ttcore.config", "ttcore.broker",
                "ttcore.market", "ttcore.engine", "ttcore.executor",
                "ttcore.daemon")


def test_core_modules_importable_without_qmt():
    """核心模块必须能在没有 xtquant 的环境导入(惰性加载)。

    必须在【子进程里屏蔽 xtquant】验证 —— 本机装了 xtquant, 直接在当前进程
    import 的话这个测试恒过, 抓不到"把 xtquant 提到模块顶层"的回归。
    """
    code = "\n".join([
        "import sys",
        "class _Blocker:",
        "    def find_spec(self, name, path=None, target=None):",
        "        if name == 'xtquant' or name.startswith('xtquant.'):",
        "            raise ImportError('xtquant blocked for test')",
        "        return None",
        "sys.meta_path.insert(0, _Blocker())",
        # 负控: 屏蔽必须真的生效, 否则本测试又变成恒过
        "try:",
        "    import xtquant",
        "except ImportError:",
        "    pass",
        "else:",
        "    raise SystemExit('blocker ineffective: xtquant still importable')",
        "import %s" % ", ".join(CORE_MODULES),
        "print('OK')",
    ])
    # cwd=tt_solo/ 让 sys.path[0] 指向它, 与 conftest 的 sys.path 注入同源
    r = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                       capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert r.returncode == 0, "无 xtquant 环境下核心模块导入失败:\n%s" % r.stderr
    assert "OK" in r.stdout
