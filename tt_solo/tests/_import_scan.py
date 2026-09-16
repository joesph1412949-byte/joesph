# -*- coding: utf-8 -*-
"""自包含扫描器(单一实现): 判定一个 Python 文件是否 import 了禁用包。

用 AST 而非文本正则 —— tt_solo 刻意保留出处散文(如 "与 prism.trader 同构"),
文本扫描会把散文误判成依赖; 而 AST 只看真实的 Import/ImportFrom 节点,
散文永远自由, 且能覆盖函数内/条件内的嵌套 import。
"""
import ast
from pathlib import Path

FORBIDDEN_ROOT_IMPORTS = frozenset(
    {"prism", "shared", "qmt_sync", "backtest", "legacy"})


def imported_roots(source):
    """返回源码 import 到的顶层包名集合(相对 import 不计)。

    ast.walk 会遍历全部后代节点, 故函数内/条件内的 import 也会被抓到。
    取根段(name.split(".")[0]), 所以 `import prism.foo` 归为 `prism`。
    相对 import(from . import x / from .broker import Y)解析不到顶层包, 跳过。
    """
    tree = ast.parse(source)
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                roots.add(node.module.split(".")[0])
    return roots


def forbidden_imports(path, source=None):
    """该文件的禁用 import 列表(已排序); 无则空列表。"""
    if source is None:
        source = Path(path).read_text(encoding="utf-8")
    hits = imported_roots(source) & FORBIDDEN_ROOT_IMPORTS
    return sorted(hits)
