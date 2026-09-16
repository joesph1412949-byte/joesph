# -*- coding: utf-8 -*-
"""做T 人工闸门小工具: 当日放行 / 急停 / 查状态。

为什么需要它:
    tt 守护有**每日放行条**机制 —— D:/QMT_SIGNALS/real/armed.txt 必须含
    当日 YYYYMMDD 才会真正下单; 昨天的条今天自动失效。这是刻意的设计:
    程序可以常驻, 但每天必须有人点头。本工具就是那个"点头"按钮。

用法:
    python tt_solo/ttcore/arm_today.py             # 写今日放行条(急停中则拒发, 见 arm())
    python tt_solo/ttcore/arm_today.py --status    # 只看状态, 不动
    python tt_solo/ttcore/arm_today.py --pause     # 按急停(创建 paused 文件)
    python tt_solo/ttcore/arm_today.py --resume    # 解除急停(删 paused 文件)
    python tt_solo/ttcore/arm_today.py --disarm    # 撤销今日放行条(删 armed.txt)

安全:
    - 本工具**绝不下单**, 只读写两个闸门文件;
    - 默认信号根 D:/QMT_SIGNALS, 可用环境变量 TT_SIGNAL_ROOT 覆盖(演练隔离)。
"""
import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

# tt_solo 根不在 sys.path 时(直接 python tt_solo/ttcore/arm_today.py)也能取到 ttcore
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # -> tt_solo/
from ttcore._vendor import atomic_write  # noqa: E402

SIGNAL_ROOT = Path(os.environ.get("TT_SIGNAL_ROOT") or r"D:/QMT_SIGNALS")
PAUSE_FILE = SIGNAL_ROOT / "paused"
ENV = "real"
ARM_FILE = SIGNAL_ROOT / ENV / "armed.txt"


def _today():
    return datetime.now().strftime("%Y%m%d")


def _atomic_write(path, text):
    """原子写: 复用 _vendor.atomic_write(mkdir+fsync+os.replace)。"""
    atomic_write(path, text)


def status():
    t = _today()
    paused = PAUSE_FILE.exists()
    armed_txt = ""
    if ARM_FILE.exists():
        try:
            armed_txt = ARM_FILE.read_text(encoding="utf-8",
                                           errors="replace").strip()
        except OSError:
            armed_txt = "(读取失败)"
    armed_today = t in armed_txt
    print("信号根目录 : %s" % SIGNAL_ROOT)
    print("急停开关   : %s  (%s)"
          % ("已按下" if paused else "未按", PAUSE_FILE))
    print("放行条     : %s" % ARM_FILE)
    print("放行条内容 : %r" % armed_txt)
    print("今日日期   : %s" % t)
    print("今日是否放行: %s" % ("是" if armed_today else "否"))
    can_trade = armed_today and not paused
    print("当前可否下单: %s" % ("可以" if can_trade else "不可以"))
    return 0 if can_trade else 1


def arm():
    t = _today()
    if PAUSE_FILE.exists():
        print("!! 急停开关处于按下状态, 先解除再放行: %s" % PAUSE_FILE)
        print("   运行: python tt_solo/ttcore/arm_today.py --resume")
        return 2
    _atomic_write(ARM_FILE, "%s\n" % t)
    print("已写入今日放行条: %s -> %s" % (ARM_FILE, t))
    print("守护进程现在可以真实下单了。")
    return 0


def disarm():
    if ARM_FILE.exists():
        ARM_FILE.unlink()
        print("已撤销放行条: %s" % ARM_FILE)
        print("守护会停止下单(已入队的信号也会被拒)。")
    else:
        print("放行条本就不存在: %s" % ARM_FILE)
    return 0


def pause():
    _atomic_write(PAUSE_FILE, "paused by arm_today.py at %s\n"
                  % datetime.now().isoformat(timespec="seconds"))
    print("!! 已按下急停开关: %s" % PAUSE_FILE)
    print("   策略侧将整体停发(但已在途委托仍需手动撤)。")
    return 0


def resume():
    if PAUSE_FILE.exists():
        PAUSE_FILE.unlink()
        print("已解除急停: %s" % PAUSE_FILE)
    else:
        print("急停本就未按。")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="做T 人工闸门工具")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="只看状态")
    g.add_argument("--pause", action="store_true", help="按急停")
    g.add_argument("--resume", action="store_true", help="解除急停")
    g.add_argument("--disarm", action="store_true", help="撤销今日放行条")
    args = ap.parse_args(argv)

    if args.status:
        return status()
    if args.pause:
        return pause()
    if args.resume:
        return resume()
    if args.disarm:
        return disarm()
    return arm()


if __name__ == "__main__":
    raise SystemExit(main())
