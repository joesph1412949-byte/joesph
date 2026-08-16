# -*- coding: utf-8 -*-
"""空白因子模板 — 复制本文件创建新因子:
1) 文件名: factor_<id小写>_<英文名>.py
2) 改 @factor 的 id/name/category/description
3) 在 compute(ctx) 里写逻辑, 返回 {"score": 0或1或数值, "note": "说明"}
4) 运行 python -m prism.factor_check 体检
"""
from prism.registry import factor


@factor(id="NEW1", name="你的因子名", category="通用",
        description="一句话说明这个因子判断什么")
def compute(ctx):
    # 示例: 从上下文取数据, 永远 fail-open
    kline = ctx.kline
    if kline is None or len(kline) < 2:
        return {"score": 0, "note": "K线不足"}
    # 在这里写你的逻辑...
    return {"score": 0, "note": "未命中"}
