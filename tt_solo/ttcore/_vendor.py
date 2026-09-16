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
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent      # -> tt_solo/
RUNTIME_DIR = Path(os.environ.get("TT_RUNTIME_DIR")
                   or PROJECT_ROOT / "runtime")
STATE_DIR = RUNTIME_DIR / "state"
LOG_DIR = RUNTIME_DIR / "log"


# ---------------------------------------------------------------- io

def atomic_write(path, text):
    """原子写: mkdir + 同目录 .tmp + flush + fsync + os.replace。

    崩溃/断电都不能留下半截文件(账本读者会把截断文件当损坏)。
    fsync 是断电存活的关键, 不是可选项。
    vendored from shared/common.py @2026-09-16。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


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
