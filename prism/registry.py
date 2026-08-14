# -*- coding: utf-8 -*-
"""因子注册表: @factor 装饰器 + 扫描因子库自动注册。"""
import importlib
import pkgutil


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
    return FACTORS[fid]


def list_factors(category=None):
    out = []
    for fid in sorted(FACTORS):
        f = dict(FACTORS[fid])
        if category and f["category"] != category:
            continue
        out.append(f)
    return out


def scan_factors(package="prism.factors"):
    """import 包内所有 factor_*.py 模块, 触发装饰器注册。"""
    mod = importlib.import_module(package)
    for info in pkgutil.iter_modules(mod.__path__):
        if info.name.startswith("factor_"):
            importlib.import_module("%s.%s" % (package, info.name))
