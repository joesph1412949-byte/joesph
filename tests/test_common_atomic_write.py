# -*- coding: utf-8 -*-
"""shared/common.atomic_write 的并发安全(M11) — 离线单元测试。

背景: 同一份缓存/账本现在有多个写者(守护快照线程 + 手动 CLI + 实盘选股器),
旧实现用**固定 tmp 名**(`path + ".tmp"`): 两个写者同进程并发时互相踩 tmp ——
后写者覆盖/搬走先写者的 tmp, 先写者的 `os.replace` 落地的是**别人的内容**
(或直接 `FileNotFoundError`); 而 `FundamentalFeed._load_cache` 会把坏 JSON
**静默当 `{}`**(历史全丢)。tmp 名必须带 pid + 线程 id。
"""
import json
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import common  # noqa: E402


def test_atomic_write_concurrent_writers_publish_their_own_text(tmp_path,
                                                               monkeypatch):
    """两个线程同路径并发写 → **最后 replace 的那份文本必须落地**。

    确定性复现(不靠裸 sleep 撞竞态):
      ① T1 写完自己的文本(利用 `os.fsync` —— write 与 replace 之间唯一的
         可注入点)后置事件;
      ② T2 等事件后才开始写(覆盖同一个 tmp), 并**先** replace → 落地 B;
      ③ T1 **最后** replace:
         - 唯一 tmp 名 → 搬自己的 tmpA → 落地 A(正确);
         - 固定 tmp 名 → 搬的是已被 T2 覆盖/搬走的 tmp → 落地仍是 B, 或抛
           FileNotFoundError(两种都是数据被写坏)。
    """
    target = tmp_path / "cache.json"
    a = json.dumps({"writer": "A", "pad": "a" * 300}, ensure_ascii=False)
    b = json.dumps({"writer": "B", "pad": "b" * 300}, ensure_ascii=False)
    real_replace, real_fsync = os.replace, os.fsync
    t1_written, t2_replaced = threading.Event(), threading.Event()
    errors = []

    def gated_fsync(fd):
        real_fsync(fd)
        if threading.current_thread().name == "T1":
            t1_written.set()          # T1 的文本已进 tmp(还没 replace)

    def ordered_replace(src, dst):
        name = threading.current_thread().name
        if name == "T1":
            assert t2_replaced.wait(5), "编排失败: T2 必须先 replace"
        real_replace(src, dst)
        if name == "T2":
            t2_replaced.set()

    def writer(text, wait_first=False):
        try:
            if wait_first:
                assert t1_written.wait(5), "编排失败: T1 必须先写完"
            common.atomic_write(target, text)
        except Exception as exc:                  # 收集, 不在子线程里炸
            errors.append(exc)

    monkeypatch.setattr(common.os, "replace", ordered_replace)
    monkeypatch.setattr(common.os, "fsync", gated_fsync)
    t1 = threading.Thread(target=writer, args=(a,), name="T1")
    t2 = threading.Thread(target=writer, args=(b, True), name="T2")
    t1.start()
    t2.start()
    t1.join(10)
    t2.join(10)

    assert errors == [], "并发写不许互相踩 tmp: %r" % (errors,)
    got = target.read_text(encoding="utf-8")
    assert got == a, ("最后 replace 的是 T1 → 落地的必须是 T1 的文本; "
                      "实际落地的 %s" % ("B(被后写者覆盖)" if got == b else "其它"))
    assert json.loads(got)["writer"] == "A"
    assert not list(tmp_path.glob("*.tmp")), "不留临时文件"


def test_atomic_write_leaves_no_tmp_and_overwrites(tmp_path):
    """基线行为不变: 目录自动创建、覆盖写、无 tmp 残留。"""
    p = tmp_path / "deep" / "x.json"
    common.atomic_write(p, "v1")
    assert p.read_text(encoding="utf-8") == "v1"
    common.atomic_write(p, "v2")
    assert p.read_text(encoding="utf-8") == "v2"
    assert list(p.parent.glob("*.tmp")) == []
