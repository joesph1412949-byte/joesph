# -*- coding: utf-8 -*-
"""因子库包: import 本包即自动扫描注册 factors/factor_*.py。"""
from prism.registry import scan_factors

scan_factors("prism.factors")
