# -*- coding: utf-8 -*-
"""
完整小demo：外部 Python 拉行情 → 筛今日涨停科技股 → 第二天挂涨停价买入信号到 sim
=============================================================================
运行环境：本机 Python 3.12 + xtquant（不是 QMT 内置编辑器）
前置条件：
  1. QMT 已打开且连接了行情（miniQMT 模式，端口 58610）
  2. 模拟端 QMT 正运行 qmt_signal_bridge_demo.py 策略（负责消费信号并下单）

链路：
  本脚本(选股+发信号)  --JSON-->  D:/QMT_SIGNALS/sim/pending/
        --每2秒轮询-->  模拟端QMT策略  --passorder-->  模拟挂单

安全：本 demo 只发到 sim（模拟盘），不会动真实资金。
      信号价格 = 明日涨停价(今收×(1+涨停幅度))，即"今天选中涨停、第二天挂涨停价买"。
"""

import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")   # 避免 Windows 控制台 GBK 中文乱码
except Exception:
    pass

from xtquant import xtdata

# ====================== 配置区（按需改） ======================
SIGNAL_ROOT = Path(r"D:/QMT_SIGNALS")
ENV = "sim"                     # 先发到模拟盘验证；学通了再改 "real"
SECTORS = [                     # 科技行业池（申万行业分类，miniQMT 无概念板块）
    "SW1电子",
    "SW1计算机",
    "SW1通信",
]
VOLUME = 100                    # 100 股 = 1手
ACCOUNT = ""                    # 留空让 QMT 端自动用登录账号（推荐）
DRY_SEND = True                 # True=只打印不发信号；False=真正写入 pending
# ===============================================================


def with_market_suffix(code):
    """裸代码补市场后缀：6/5/9开头→.SH，0/3/2开头→.SZ（带后缀的原样返回）"""
    code = str(code).strip()
    if "." in code:
        return code
    if code[0] in "659":
        return code + ".SH"
    if code[0] in "032":
        return code + ".SZ"
    return code


def limit_ratio(code):
    """按代码前缀返回涨跌停幅度：北交所30% / 创业科创20% / 主板10%"""
    if code.startswith(("8", "4")):      # 北交所
        return 0.30
    if code.startswith(("300", "301", "688")):   # 创业板 / 科创板
        return 0.20
    return 0.10                              # 主板 60/00/002 等


def limit_up_price(last_close, ratio):
    """涨停价 = 昨收 × (1+幅度)，四舍五入到分"""
    return round(last_close * (1 + ratio), 2)


def build_pool(sector_names):
    """把若干行业板块成分股合并去重"""
    codes = set()
    for name in sector_names:
        st = xtdata.get_stock_list_in_sector(name) or []
        codes.update(st)
    return sorted(codes)


def send_signal(stock_code, price, volume, account_id=""):
    """把一条买入信号写入 D:/QMT_SIGNALS/<env>/pending/"""
    order_id = "BUY_%s" % uuid.uuid4().hex[:8]
    signal = {
        "order_id": order_id,
        "action": "BUY",
        "stock_code": stock_code,
        "order_type": "BUY",
        "price": price,
        "volume": volume,
        "account_id": account_id,
        "created_at": datetime.now().isoformat(),
        "status": "pending",
    }
    pending_dir = SIGNAL_ROOT / ENV / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    (pending_dir / ("%s.json" % order_id)).write_text(
        json.dumps(signal, ensure_ascii=False, indent=2), encoding="utf-8")
    return order_id


def main():
    print("=" * 62)
    print("本机 Python 连接 QMT miniQMT 行情...")
    xtdata.connect()
    time.sleep(2)  # 给连接建立留点时间

    print("拉取科技行业池:", ", ".join(SECTORS))
    pool = build_pool(SECTORS)
    print("成分股共 %d 只，拉实时盘口..." % len(pool))
    ticks = xtdata.get_full_tick(pool)
    print("收到 %d 只有行情\n" % len(ticks))

    limit_ups = []          # 已涨停的
    for code in ticks:
        t = ticks[code]
        last = t.get("lastPrice") or 0
        pre = t.get("lastClose") or 0
        if last <= 0 or pre <= 0:
            continue
        ratio = limit_ratio(code)
        lu = limit_up_price(pre, ratio)
        # 现价 >= 涨停价(容差1分) 视为已涨停
        if last >= lu - 0.01:
            name = (xtdata.get_instrument_detail(code) or {}).get("InstrumentName", "?")
            limit_ups.append({
                "code": code, "name": name, "last": last,
                "pre": pre, "lu": lu, "ratio": ratio,
            })

    if not limit_ups:
        print("今日科技行业池内无涨停股（涨停 = 现价触及 %.1f%%/%.1f%%/%.1f%% 涨停价）"
              % (10, 20, 30))
        print("——这本身就是策略的正确输出：没有信号，不该发单。")
        return

    # 按涨停时间/涨幅排序（这里按涨停价溢价款排）
    limit_ups.sort(key=lambda x: x["last"] - x["lu"], reverse=True)

    print("%-10s %-8s %9s %9s %9s %7s" % ("代码", "名称", "现价", "今收", "涨停价", "幅度"))
    print("-" * 58)
    for r in limit_ups[:20]:
        print("%-10s %-8s %9.2f %9.2f %9.2f %6.0f%%"
              % (r["code"], r["name"], r["last"], r["pre"], r["lu"], r["ratio"] * 100))

    pick = limit_ups[0]
    # 次日涨停价 = 今收 × (1+幅度)。盘中拉的"今收"用现价近似，收盘后跑则用收盘价。
    next_lu = limit_up_price(pick["last"], pick["ratio"])
    print("\n选中：%s %s，现价 %.2f 已涨停(涨停价 %.2f)" % (pick["code"], pick["name"], pick["last"], pick["lu"]))
    print("次日挂涨停价买入：%.2f，数量 %d 股" % (next_lu, VOLUME))

    if DRY_SEND:
        print("\n[DRY_SEND=True] 未发信号。把 DRY_SEND 改成 False 后再运行即可真正发单。")
        return

    oid = send_signal(pick["code"], next_lu, VOLUME, ACCOUNT)
    print("\n[%s] 已发送信号：%s" % (ENV, oid))
    print("信号文件：%s" % (SIGNAL_ROOT / ENV / "pending" / ("%s.json" % oid)))
    print("接下来：切到模拟端 QMT，看策略日志是否出现 ORDER ...，再去委托面板确认挂单。")


if __name__ == "__main__":
    main()
