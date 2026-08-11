# -*- coding: utf-8 -*-
"""手填因子持久化：xtdata 拿不到数据的因子，用户在网页上手填得分(0/1)。
数据存 JSON 文件，重新选股时自动合并到自动因子。"""
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class ManualStore:
    # 可手填的因子集合（东财已自动的 Y1/Y5/F7/Y7/S5/Y6/Y2 移出手填;
    # 实测探针: S5 融资接口无法从东财确认 → 保持手填; S7 探针未确认暂留手填）
    MANUAL_FACTORS = ["S1", "S5", "S7"]

    def __init__(self, path="manual_factors.json"):
        self.path = Path(path)
        self._data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                # 损坏的 JSON: 重命名为 <path>.corrupt-<epoch秒> 保留原始字节,
                # 否则下次 _save 会静默覆盖, 永久丢失手填因子。
                backup = Path("%s.corrupt-%d" % (self.path, int(time.time())))
                try:
                    self.path.rename(backup)
                    logger.warning("manual_factors.json 损坏, 已备份到 %s: %s", backup, e)
                except OSError as rn_err:
                    logger.warning("manual_factors.json 损坏且备份失败: %r", rn_err)
                return {}
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
