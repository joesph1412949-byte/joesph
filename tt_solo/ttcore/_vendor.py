# -*- coding: utf-8 -*-
"""自包含底座: 路径根 + 原子写 + 涨跌停比例 + 分级写护栏。

ponytail: vendored from shared/common.py @2026-09-16 —— tt_solo 要能被整体
拷走独立运行, 故刻意不 import shared。5 个符号约 60 行, 为它们造一层包结构
属于过度设计, 内联到单文件即可(每个函数标注来源保留回溯)。

注意 limit_ratio_for_code 的差异: shared/common.py 的判定是 ("8", "4"),
而 tt/risk.py 的兜底实现是 ("8", "4", "92")。北交所 920xxx 属 30% 涨跌幅,
故此处采用 tt 的更正确版本("92"); shared 版本缺这条, 是主项目侧的潜在缺陷。
"""
import os
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
    vendored from shared/common.py @2026-09-16(重试 @2026-09-18)。

    ponytail: tmp 名仍是固定的 `path + ".tmp"`(没跟 shared/common 一起改成
    pid+线程) —— 同进程两个写者(守护 + 面板重算/Flask 多线程)仍可能互踩这个
    tmp; 那一路频率低, 本轮只补 replace 重试。真观测到互踩再改成唯一名(一行)。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    replace_with_retry(tmp, path)


# ---------------------------------------------------------------- 规则

def limit_ratio_for_code(code):
    """按板块返回涨跌停比例: 北交所 30%, 创业板/科创 20%, 主板 10%。

    vendored from tt/risk.py 的兜底实现 @2026-09-16(含 "92" 段)。
    """
    c = str(code).strip()
    if c.startswith(("8", "4", "92")):
        return 0.30
    if c.startswith(("300", "301", "688")):
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
