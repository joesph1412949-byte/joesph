# -*- coding: utf-8 -*-
"""手填因子持久化：xtdata 拿不到数据的因子，用户在网页上手填得分(0/1)。
数据存 JSON 文件，重新选股时自动合并到自动因子。"""
import json
from pathlib import Path


class ManualStore:
    # 可手填的因子集合（含部分可自动算但允许手填覆盖的）
    MANUAL_FACTORS = ["F7", "Y1", "Y2", "Y5", "Y6", "Y7", "S1", "S5", "S7"]

    def __init__(self, path="manual_factors.json"):
        self.path = Path(path)
        self._data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def get_manual(self, code):
        return dict(self._data.get(code, {}))

    def set_manual(self, code, factors):
        for k, v in factors.items():
            if k not in self.MANUAL_FACTORS:
                raise ValueError("未知手填因子: %s" % k)
            if v not in (0, 1):
                raise ValueError("%s 的值必须是 0 或 1，收到 %r" % (k, v))
        self._data[code] = {k: int(v) for k, v in factors.items()}
        self._save()

    def all(self):
        return {k: dict(v) for k, v in self._data.items()}

    def merge(self, auto_factors, code):
        """合并自动因子和该股手填因子，返回完整因子字典 {因子: 0/1}"""
        merged = dict(auto_factors)
        for k, v in self.get_manual(code).items():
            merged[k] = v
        return merged
