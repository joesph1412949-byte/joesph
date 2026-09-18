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

M12(2026-09-18): 写-写串行**不等于** replace 一定成功。Python 的 `open()` 在
Windows 上不带 FILE_SHARE_DELETE, 所以只要目标文件此刻被**任何读者**打开,
`os.replace` 就抛 `PermissionError(13)` —— 单个写者 + 单个读者就够(实测见
`test_atomic_write_survives_concurrent_reader`)。修法: `os.replace` 外包一层
**有界退避重试**(shared.common.replace_with_retry), 读者关掉句柄后就落地。
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

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
    # 真守卫: tmp 名现在是 `cache.json.<pid>.<tid>.tmp`, 断言"目录里除目标文件
    # 外没有任何残留"(旧断言只在旧命名下有意义)
    assert [q for q in tmp_path.iterdir() if q != target] == [], "不留任何残留"


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
    assert [q for q in tmp_path.iterdir() if q != target] == [], "不留任何残留"


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
    assert [q for q in p.parent.iterdir() if q != p] == [], "不留任何残留"


# ---------------- M12: os.replace 撞并发读者 → 有界退避重试 ----------------

def test_atomic_write_retries_replace_until_it_succeeds(tmp_path, monkeypatch):
    """`os.replace` 前几次抛 PermissionError(13) → 退避重试后**成功落地**。

    这就是 Windows 的真实形状: 目标文件此刻被读者句柄打开, replace 拿不到
    删除权 → EACCES; 读者关掉句柄后重试即成功。断言: 写最终成功、内容完整、
    不留残留。"""
    target = tmp_path / "cache.json"
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise PermissionError(13, "拒绝访问。")
        return real_replace(src, dst)

    monkeypatch.setattr(common.os, "replace", flaky_replace)
    common.atomic_write(target, "v-after-retry")

    assert calls["n"] == 4, "前 3 次 EACCES 应被重试, 不该放弃"
    assert target.read_text(encoding="utf-8") == "v-after-retry"
    assert [q for q in tmp_path.iterdir() if q != target] == []


def test_atomic_write_retries_windows_sharing_violation(tmp_path, monkeypatch):
    """可重试集合含 `OSError` 且 `winerror ∈ {5, 32}`(拒绝访问/共享冲突)。"""
    target = tmp_path / "cache.json"
    real_replace = os.replace
    calls = {"n": 0}

    def sharing_violation(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(13, "另一个程序正在使用此文件", None, 32)
        return real_replace(src, dst)

    monkeypatch.setattr(common.os, "replace", sharing_violation)
    common.atomic_write(target, "ok")
    assert calls["n"] == 2
    assert target.read_text(encoding="utf-8") == "ok"


def test_atomic_write_reraises_original_error_after_retry_budget(tmp_path,
                                                                 monkeypatch):
    """重试**有界**且耗尽后原样抛出原始 PermissionError —— 不许静默当成功。

    目标文件必须保持上一次的完整内容(写失败 ≠ 内容被清掉), 总耗时受控。"""
    target = tmp_path / "cache.json"
    common.atomic_write(target, "old")

    def always_denied(src, dst):
        raise PermissionError(13, "拒绝访问。")

    monkeypatch.setattr(common.os, "replace", always_denied)
    t0 = time.monotonic()
    with pytest.raises(PermissionError) as ei:
        common.atomic_write(target, "new")
    elapsed = time.monotonic() - t0

    assert ei.value.errno == 13
    assert ei.value is not None and "拒绝访问" in str(ei.value)
    assert target.read_text(encoding="utf-8") == "old", "写失败不许动目标内容"
    assert elapsed < 5.0, "重试预算必须有界(实测 %.2fs)" % elapsed


def test_atomic_write_does_not_retry_non_sharing_errors(tmp_path, monkeypatch):
    """只重试"目标被占用"类错误: 其它 OSError(如 ENOENT)一次就抛。

    (否则一个真正的 bug 会被 10 次重试掩成"慢", 而调用方拿到的是迟到 1s 的
    同款异常 —— 没有收益, 只有延迟。)"""
    target = tmp_path / "cache.json"
    calls = {"n": 0}

    def broken(src, dst):
        calls["n"] += 1
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(common.os, "replace", broken)
    with pytest.raises(FileNotFoundError):
        common.atomic_write(target, "v")
    assert calls["n"] == 1, "非共享冲突错误不该被重试"


def test_atomic_write_survives_concurrent_reader(tmp_path, monkeypatch):
    """M12 主症状回归: 目标文件被**读者句柄**持有期间写同一路径 → N 轮全零异常。

    每一轮的时序用 Event 握手钉死, 不靠 sleep/调度碰运气:
      1. 读者线程打开目标文件并**一直持有句柄**(生产里的读/解析形态);
      2. 写者线程进 `atomic_write` → 第一次 `os.replace` 必然撞上"目标被打开"
         (先断言它**真的**撞上了, 否则本轮什么都没测到);
      3. 读者在这时关句柄(远在 0.95s 预算之内) → 写者必须靠重试落地, 零异常。

    修复前:第 2 步之后写者立刻抛 `PermissionError(13, '拒绝访问。')` → 红。
    为什么不用"定时持有/放手的读者线程跑 N 轮":那种读者的句柄持有时间是
    wall-clock 的, 线程被抢占时"握着句柄下 CPU"能让任何**有界**预算落空 ——
    全量跑里实测 flake(2/300)。那是本修法的诚实边界(报告"未覆盖边界"), 不该
    拿来当断言。频率数字见报告 M12:同款循环读者下修复前 15~262/300 轮 EACCES。
    """
    target = tmp_path / "cache.json"
    common.atomic_write(target, json.dumps({"i": -1}))

    holding, release = threading.Event(), threading.Event()
    collided = threading.Event()
    real_replace = os.replace

    def reader():
        with open(target, "rb") as f:
            f.read()
            holding.set()
            release.wait(10)           # 读者处理这份数据: 句柄一直开着

    def watched_replace(src, dst):
        try:
            return real_replace(src, dst)
        except PermissionError:
            collided.set()             # 只观察, 不改行为
            raise

    monkeypatch.setattr(common.os, "replace", watched_replace)

    for rnd in range(10):
        holding.clear()
        release.clear()
        collided.clear()
        r = threading.Thread(target=reader, name="R%d" % rnd)
        r.start()
        assert holding.wait(10), "第 %d 轮编排失败: 读者没打开目标文件" % rnd

        errs = []

        def writer():
            try:
                common.atomic_write(target, json.dumps({"i": rnd}))
            except Exception as exc:   # PermissionError(13) / WinError 5|32
                errs.append(repr(exc))

        w = threading.Thread(target=writer, name="W%d" % rnd)
        w.start()
        try:
            assert collided.wait(10), (
                "第 %d 轮编排失败: 写者第一次 replace 没撞上读者句柄 —— 本轮"
                "没有制造出 EACCES, 断言等于没跑" % rnd)
        finally:
            release.set()              # 读者放手(在重试预算之内)
        w.join(10)
        r.join(10)

        assert errs == [], ("第 %d 轮: 读者在预算内放手后写必须成功: %r"
                            % (rnd, errs))
        assert json.loads(target.read_text(encoding="utf-8"))["i"] == rnd
    assert [q for q in tmp_path.iterdir() if q != target] == [], "不留任何残留"
