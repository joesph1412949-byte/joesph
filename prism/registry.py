# -*- coding: utf-8 -*-
"""因子注册表: @factor 装饰器 + 扫描因子库自动注册。"""
import importlib
import os
import pkgutil
import sys


class UnknownFactorError(KeyError):
    """策略配置引用了不存在的因子。"""


FACTORS = {}  # id -> {"name","category","description","func"}


def reset():
    FACTORS.clear()


def factor(id, name, category="通用", description=""):
    """注册因子: 装饰器用法 @factor(id=..., name=..., ...)"""
    def deco(func):
        FACTORS[id] = {
            "id": id, "name": name, "category": category,
            "description": description, "func": func,
        }
        return func
    return deco


def get_factor(fid):
    if fid not in FACTORS:
        raise UnknownFactorError("未知因子: %s (请检查策略配置或因子库)" % fid)
    return dict(FACTORS[fid])


def list_factors(category=None):
    out = []
    for fid in sorted(FACTORS):
        f = dict(FACTORS[fid])
        if category and f["category"] != category:
            continue
        out.append(f)
    return out


def scan_factors(package="prism.factors", force=False):
    """import 包内所有 factor_*.py 模块, 触发装饰器注册。

    一次性语义: 同一进程内重复 scan 同一包不会重复注册, 除非 force=True
    或模块未被缓存 (importlib 会缓存已导入的模块)。force=True 时先
    从 sys.modules 弹出包及其 factor_* 子模块再重新 import, 确保装饰器
    重新触发注册。
    """
    if force:
        cached = sys.modules.get(package)
        if cached is not None:
            for path in getattr(cached, "__path__", ()):
                _clear_factor_bytecode(path)
        sys.modules.pop(package, None)
        for name in list(sys.modules):
            if name.startswith(package + "."):
                leaf = name.rsplit(".", 1)[-1]
                if leaf.startswith("factor_"):
                    sys.modules.pop(name, None)
    mod = importlib.import_module(package)
    for info in pkgutil.iter_modules(mod.__path__):
        if info.name.startswith("factor_") and not info.ispkg:
            importlib.import_module("%s.%s" % (package, info.name))


def _clear_factor_bytecode(path):
    """删除包目录 __pycache__ 下 factor_* 的字节码缓存。

    importlib 校验 .pyc 时只比较整数秒的源文件 mtime 与文件大小, 因此
    同秒内重写同长度的因子文件不会触发重编译, 会执行过期字节码; force
    重扫时需一并清除, 否则新代码不生效。
    """
    pycache = os.path.join(path, "__pycache__")
    try:
        names = os.listdir(pycache)
    except OSError:
        return
    for name in names:
        if name.startswith("factor_") and name.endswith((".pyc", ".pyo")):
            try:
                os.remove(os.path.join(pycache, name))
            except OSError:
                pass
