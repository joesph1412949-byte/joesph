# -*- coding: utf-8 -*-
"""shared/common.atomic_write 的并发安全(M11/M11b) — 离线单元测试。

背景: 同一份缓存/账本现在有多个写者(守护快照线程 + 手动 CLI + 实盘选股器),
旧实现用**固定 tmp 名**(`path + ".tmp"`): 两个写者同进程并发时互相踩 tmp ——
后写者覆盖/搬走先写者的 tmp, 先写者的 `os.replace` 落地的是**别人的内容**
(或直接 `FileNotFoundError`); 而 `FundamentalFeed._load_cache` 会把坏 JSON
**静默当 `{}`**(历史全丢)。tmp 名必须带 pid + 线程 id。

M11b(2026-09-18): tmp 名唯一**只**解决"互踩同一个 tmp"。两个线程**同时**
`os.replace` 到**同一个目标**, 在 Windows 上仍会撞目标句柄 -> PermissionError
(实测旧实现 200 轮 26 轮抛错/失败); 调用方会误以为"写失败", 而读取侧对坏文件
静默当 `{}`。修法: `atomic_write` 加**按路径**的进程内锁(见 prism/zt_history.py
同一手法), 本进程同路径串行; 跨进程仍靠 tmp 名带 pid + os.replace 原子性。
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import common  # noqa: E402


def test_atomic_write_never_overlaps_replace_on_same_path(tmp_path,
                                                          monkeypatch):
    """同进程两个写者不许**同时**对同一目标 os.replace(Windows 目标竞态)。

    把 `os.replace` 换成"登记-观察"版本: 进入 replace 的线程先登记自己, 再等
    一个观察窗, 然后看**是不是还有别的线程也登记在 replace 里** —— 有, 就说明
    "两个线程同时 replace 同一目标"这个竞态窗口真实存在(Windows 上就是它抛
    PermissionError)。

    - 无锁(旧实现): T2 与 T1 同时在 replace 里 -> overlaps 非空 -> 失败;
    - 有锁: 另一线程被挡在锁外, 压根进不来 -> overlaps 空 -> 通过。
    """
    target = tmp_path / "cache.json"
    real_replace = os.replace
    state = threading.Lock()
    insiders = set()
    overlaps = []
    errors = []
    gate = threading.Barrier(2, timeout=10)

    def watched_replace(src, dst):
        name = threading.current_thread().name
        if name not in ("T1", "T2"):          # 别拖慢测试进程里别的 replace
            return real_replace(src, dst)
        with state:
            insiders.add(name)
        try:
            time.sleep(1.5)                   # 观察窗: 让对方有机会撞进 replace
            with state:
                peer_inside = len(insiders) > 1
            if peer_inside:
                overlaps.append(name)
            real_replace(src, dst)
        finally:
            with state:
                insiders.discard(name)

    def writer(text):
        try:
            gate.wait()
            common.atomic_write(target, text)
        except Exception as exc:              # 收集, 不在子线程里炸
            errors.append(exc)

    monkeypatch.setattr(common.os, "replace", watched_replace)
    t1 = threading.Thread(target=writer, args=("A" * 300,), name="T1")
    t2 = threading.Thread(target=writer, args=("B" * 300,), name="T2")
    t1.start()
    t2.start()
    t1.join(20)
    t2.join(20)

    assert errors == [], "并发写不许互相踩/抛错: %r" % (errors,)
    assert overlaps == [], ("两个线程同时在 replace 同一个目标"
                           "(= Windows 目标竞态窗口): %r" % (overlaps,))
    got = target.read_text(encoding="utf-8")
    assert got in ("A" * 300, "B" * 300), "落地必须是某一次**完整**写入"
    assert not list(tmp_path.glob("*.tmp")), "不留临时文件"


def test_atomic_write_concurrent_same_path_stress(tmp_path):
    """高频并发(N 轮 × 4 线程同路径) → 零异常, 每轮落地都是某一次的完整文本。

    不注入 sleep, 纯靠真实调度去撞 Windows 的目标竞态 -> 旧实现间歇失败
    (实测 200 轮 26 轮)。有锁后同一目标串行, 结构上不可能再撞。
    """
    target = tmp_path / "cache.json"
    rounds, workers = 150, 4
    payloads = [json.dumps({"writer": i, "pad": "x" * (40 * (i + 1))},
                           ensure_ascii=False) for i in range(workers)]
    errors = []
    bad = []

    for rnd in range(rounds):
        gate = threading.Barrier(workers, timeout=20)

        def writer(text):
            try:
                gate.wait()
                common.atomic_write(target, text)
            except Exception as exc:
                errors.append((rnd, threading.current_thread().name, repr(exc)))

        threads = [threading.Thread(target=writer, args=(t,), name="W%d" % i)
                   for i, t in enumerate(payloads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        try:
            got = target.read_text(encoding="utf-8")
        except OSError as exc:                # 没落地 / 读不到
            bad.append((rnd, repr(exc)))
            continue
        if got not in payloads:               # 半截 / 两个写者混淆
            bad.append((rnd, got[:80]))

    assert errors == [], ("同路径并发写抛错 %d/%d 轮(前 3): %r"
                          % (len(errors), rounds, errors[:3]))
    assert bad == [], ("落地内容不是某一次完整写入 %d/%d 轮(前 3): %r"
                       % (len(bad), rounds, bad[:3]))
    assert not list(tmp_path.glob("*.tmp")), "不留临时文件"


def test_atomic_write_does_not_serialize_different_paths(tmp_path,
                                                         monkeypatch):
    """锁必须**按路径**: 写 A 的线程卡在 replace 里, 写 B 的线程不许被连带卡住。

    (防"一把全局大锁"式修法: 那会把无关文件的写串行化。)
    """
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    real_replace = os.replace
    t1_inside = threading.Event()
    release = threading.Event()
    errors = []

    def watched_replace(src, dst):
        if threading.current_thread().name == "T1":
            t1_inside.set()
            assert release.wait(10), "编排失败"
        return real_replace(src, dst)

    def writer(path, text):
        try:
            common.atomic_write(path, text)
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(common.os, "replace", watched_replace)
    t1 = threading.Thread(target=writer, args=(a, "A"), name="T1")
    t1.start()
    assert t1_inside.wait(10), "编排失败: T1 没进 replace"
    t2 = threading.Thread(target=writer, args=(b, "B"), name="T2")
    t2.start()
    t2.join(5)
    other_blocked = t2.is_alive()             # T1 还卡着, T2 却没写完 = 全局锁
    release.set()
    t1.join(10)
    t2.join(10)

    assert errors == [], "并发写不许抛错: %r" % (errors,)
    assert not other_blocked, "写不同路径的线程被连带串行化了(用了全局大锁?)"
    assert b.read_text(encoding="utf-8") == "B"


def test_atomic_write_leaves_no_tmp_and_overwrites(tmp_path):
    """基线行为不变: 目录自动创建、覆盖写、无 tmp 残留。"""
    p = tmp_path / "deep" / "x.json"
    common.atomic_write(p, "v1")
    assert p.read_text(encoding="utf-8") == "v1"
    common.atomic_write(p, "v2")
    assert p.read_text(encoding="utf-8") == "v2"
    assert list(p.parent.glob("*.tmp")) == []
