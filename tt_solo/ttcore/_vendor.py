# -*- coding: utf-8 -*-
"""自包含底座: 路径根 + 原子写 + 涨跌停比例 + 分级写护栏。

ponytail: vendored from shared/common.py @2026-09-16 —— tt_solo 要能被整体
拷走独立运行, 故刻意不 import shared。5 个符号约 60 行, 为它们造一层包结构
属于过度设计, 内联到单文件即可(每个函数标注来源保留回溯)。

注意 limit_ratio_for_code 的口径: 权威实现是 shared/exit_rules.py 的
limit_ratio()(镜像 shared/common.limit_ratio_for_code)—— 北交所/新三板 92/8/4
→ 30%, 科创(含 689 CDR)/创业板 300/301/688/689 → 20%, 老三板 400/420 → 5%,
其余 10%(400/420 必须先判, 否则被 "4" 抢去当北交所 30%)。@2026-09-19 对齐:
这份此前是 ("8","4","92") 且缺 689/400/420, 与规格脱钩; tt_solo 刻意自包含
(不许 import shared), 故仍是就地实现, 等式改由 datasource/tests/test_common.py
的守卫逐码钉死 —— 那边是按路径加载本文件比对返回值, 不是扫源码文本。
"""
import os
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent      # -> tt_solo/
RUNTIME_DIR = Path(os.environ.get("TT_RUNTIME_DIR")
                   or PROJECT_ROOT / "runtime")
STATE_DIR = RUNTIME_DIR / "state"
LOG_DIR = RUNTIME_DIR / "log"


# ---------------------------------------------------------------- io

# ---------------------------------------------------- os.replace 退避重试
# vendored from shared/common.py @2026-09-18(同款写法与口径, 就地实现: tt_solo
# 必须自包含, 不许 import shared/prism —— tests/test_selfcontained.py 有护栏)。
# Windows: 只要目标文件此刻被**任何读者句柄**打开, os.replace 就抛
# PermissionError(拒绝访问) —— CPython 的 open() 不带 FILE_SHARE_DELETE,
# MoveFileEx(REPLACE_EXISTING) 拿不到目标上的 DELETE 权限。这不是写者之间的
# 问题(锁解决不了), 只能等读者关句柄: 有界退避重试, 预算耗尽**原样抛出**
# (写失败绝不许看起来像成功)。实测来源: 全量跑里 test_events_capped 因
# WinError 5 红过一次; 有读者时高频 save 修复前 16~70/300 轮抛错(随读者占空比)。
_REPLACE_ATTEMPTS = 10
_REPLACE_DELAY0 = 0.01
_REPLACE_DELAY_MAX = 0.16


def _is_replace_retryable(exc):
    """True = "目标被占用"类错误(只有这类才值得重试)。"""
    if isinstance(exc, PermissionError):
        return True
    return (isinstance(exc, OSError)
            and getattr(exc, "winerror", None) in (5, 32))


def replace_with_retry(src, dst):
    """os.replace + 有界退避重试(10 次尝试 / 最坏 0.95s, 不忙等)。"""
    delay = _REPLACE_DELAY0
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except OSError as exc:
            if attempt == _REPLACE_ATTEMPTS - 1 or not _is_replace_retryable(exc):
                raise
            time.sleep(delay)
            delay = min(delay * 2, _REPLACE_DELAY_MAX)


def atomic_write(path, text):
    """原子写: mkdir + 同目录 .tmp + flush + fsync + os.replace(带退避重试)。

    崩溃/断电都不能留下半截文件(账本读者会把截断文件当损坏)。
    fsync 是断电存活的关键, 不是可选项。
    vendored from shared/common.py @2026-09-16(重试+唯一 tmp 名 @2026-09-18)。

    tmp 名带 pid+线程 id(与 shared/common.py 同款): 同进程两个写者(守护 +
    面板重算/Flask 多线程)并写同一路径时不会互踩同一个 tmp; 跨进程靠 pid 段
    隔离。os.replace 撞读者句柄那一类由 replace_with_retry 兜住 —— 两者互补,
    都不可省。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.tmp.%d.%d" % (
        path.name, os.getpid(), threading.get_ident()))
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    try:
        replace_with_retry(tmp, path)
    except BaseException:
        # 落地失败别留垃圾 tmp(下一次写会被唯一名绕过, 但目录会持续变脏)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- 规则

def limit_ratio_for_code(code):
    """按板块返回涨跌停比例: 北交所/新三板 30%, 科创(含 689)/创业板 20%,
    老三板 400/420 5%, 主板 10%。

    就地实现(vendored from shared/exit_rules.limit_ratio @2026-09-16, 对齐 @2026-09-19):
    tt_solo 要能整体拷走独立运行, 不许 import shared/prism。
    """
    c = str(code).strip()
    if c.startswith(("400", "420")):       # 老三板必须先判, 否则被 "4" 当北交所
        return 0.05
    if c.startswith(("92", "8", "4")):     # 北交所/新三板
        return 0.30
    if c.startswith(("300", "301", "688", "689")):
        return 0.20
    return 0.10


# ---------------------------------------------------------------- 分级护栏

def _is_rfc1918(ip):
    """RFC1918 私网段(局域网 = 可信本机档)。"""
    if ip.startswith(("10.", "192.168.")):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def is_local_request(cf_ip, remote_addr):
    """分级写护栏判据。有 CF-Connecting-IP = 经隧道 = 远程; 无头且
    回环/RFC1918 = 本机。vendored from shared/common.py @2026-09-16。"""
    if cf_ip:
        return False
    ra = remote_addr or ""
    return ra in ("127.0.0.1", "::1") or _is_rfc1918(ra)
