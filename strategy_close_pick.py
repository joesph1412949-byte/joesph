# -*- coding: utf-8 -*-
"""
正式收盘选股策略：选今日涨停的科技股 → 次日挂涨停价买入
====================================================================
用法（在本机 Python 运行）：
  python strategy_close_pick.py screen    # 收盘后选股, 生成明日候选清单
  python strategy_close_pick.py send      # 次日开盘前, 把候选清单发成买入信号
  python strategy_close_pick.py send --code 002859.SZ   # 只发指定股票

为什么分两步：
  QMT 桥每 2 秒扫一次 pending, 拿到信号会立刻 passorder。
  但收盘后(15:00之后)是非交易时段, 直接发信号会被拒单/报非交易时间。
  所以"收盘选股"和"次日发单"必须分开:
    - screen: 15:00 后跑, 只选股存盘, 不动盘
    - send:   次日 9:15 开盘前跑, 才真正写信号进 pending
  如果你只要模拟盘随手验证(不真成交), 也可以 screen 后立刻 send。

安全：默认发 sim(模拟盘)。改 ENV="real" 才发真实盘。
      发真实盘前, 确认 QMT 登录的是你的真实资金账号。
"""

import argparse
import json
import sys
import time
import uuid
from datetime import datetime, date
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from xtquant import xtdata

from common import SIGNAL_ROOT, SECTORS, limit_ratio_for_code, with_market_suffix

# ====================== 配置区 ======================
ENV = "sim"                          # "sim"模拟盘 / "real"真实盘
STATE_FILE = Path(__file__).parent / "close_pick_state.json"   # 候选清单存盘
VOLUME = 100                         # 每只 100 股
MAX_PICKS = 5                        # 每天最多选几只(防止一次发太多)
ACCOUNT = ""                         # 留空让 QMT 端自动用登录账号
MAX_PICK_AGE_DAYS = 3                # send 允许的清单最大年龄(天): 覆盖周末(周五选→周一发=3天)
# ===================================================

LIMIT_RATIO_CACHE = {}


def limit_ratio(code):
    """涨跌停幅度: 北交所30% / 创业科创20% / 主板10%; ST股5%由 name 判断
    (实现收敛到 common.limit_ratio_for_code, 本地仅保留缓存)"""
    if code in LIMIT_RATIO_CACHE:
        return LIMIT_RATIO_CACHE[code]
    r = limit_ratio_for_code(code)
    LIMIT_RATIO_CACHE[code] = r
    return r


def is_st(name):
    """按名称判断 ST / *ST / 退市股"""
    n = (name or "").upper()
    return "ST" in n or "退" in n


def limit_up_price(close, ratio):
    return round(close * (1 + ratio), 2)


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"date": "", "picks": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def build_pool(sector_names):
    codes = set()
    for name in sector_names:
        st = xtdata.get_stock_list_in_sector(name) or []
        codes.update(st)
    return sorted(codes)


def screen_today(close_approx=None):
    """选今日涨停科技股。
    close_approx: None=用实时价(盘中/收盘后都行); 也可传收盘价。
    返回 [ {code,name,close,lu,ratio}, ... ] 按涨幅排序。
    """
    print("拉取科技行业池:", ", ".join(SECTORS))
    pool = build_pool(SECTORS)
    print("成分股 %d 只, 拉行情..." % len(pool))
    ticks = xtdata.get_full_tick(pool)
    print("收到行情 %d 只\n" % len(ticks))

    picks = []
    for code in ticks:
        t = ticks[code]
        last = t.get("lastPrice") or 0
        pre = t.get("lastClose") or 0
        if last <= 0 or pre <= 0:
            continue
        name = (xtdata.get_instrument_detail(code) or {}).get("InstrumentName", "?")
        if is_st(name):
            continue                       # 剔除 ST/退市
        ratio = limit_ratio(code)
        lu = limit_up_price(pre, ratio)
        close = close_approx if close_approx else last   # 收盘价或现价
        if close >= lu - 0.01:             # 触及涨停
            picks.append({"code": code, "name": name, "close": close,
                          "lu": lu, "ratio": ratio})
    picks.sort(key=lambda x: x["close"] - x["lu"], reverse=True)
    return picks


def send_signal(stock_code, price, volume, account_id=""):
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


def cmd_screen(args):
    print("=" * 62)
    print("收盘选股 screen | %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    picks = screen_today()

    if not picks:
        print("今日科技行业池内无涨停股 —— 无候选, 不动盘。")
        save_state({"date": date.today().isoformat(), "picks": []})
        return

    picks = picks[:MAX_PICKS]
    print("%-11s %-8s %9s %9s %7s" % ("代码", "名称", "收盘", "明日涨停", "幅度"))
    print("-" * 52)
    for p in picks:
        print("%-11s %-8s %9.2f %9.2f %6.0f%%"
              % (p["code"], p["name"], p["close"], limit_up_price(p["close"], p["ratio"]), p["ratio"] * 100))

    save_state({"date": date.today().isoformat(), "picks": picks})
    print("\n已存候选清单到 %s (%d 只)" % (STATE_FILE, len(picks)))
    print("下一步: 次日开盘前运行  python strategy_close_pick.py send")


def cmd_send(args):
    print("=" * 62)
    print("次日发单 send | %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    state = load_state()
    if not state.get("picks"):
        print("候选清单为空。请先运行 screen 选股。")
        return

    # 新鲜度校验: 候选清单必须是最近 MAX_PICK_AGE_DAYS 天内选出来的。
    # 防呆: 若清单过期(例如忘了重新 screen, 拿上周的旧单去发),
    # 直接拒绝, 避免拿过期价格挂单。--force 可强制绕过。
    try:
        pick_date = datetime.strptime(state.get("date", ""), "%Y-%m-%d").date()
        age_days = (date.today() - pick_date).days
    except (ValueError, TypeError):
        pick_date, age_days = None, 99
    if age_days > MAX_PICK_AGE_DAYS and not args.force:
        print("!! 候选清单生成于 %s (%d 天前), 已过期(允许 %d 天)。"
              % (state.get("date") or "未知", age_days, MAX_PICK_AGE_DAYS))
        print("   请先重新运行 screen 选股; 若确需强制发旧单, 加 --force。")
        return

    if args.code:
        picks = [p for p in state["picks"] if p["code"].upper() == args.code.upper() or
                 with_market_suffix(p["code"]).upper() == args.code.upper()]
        if not picks:
            print("清单里没有 %s。清单:" % args.code)
            for p in state["picks"]:
                print("  ", p["code"], p["name"])
            return
    else:
        picks = state["picks"]

    if ENV == "sim":
        print("目标: sim 模拟盘")
    else:
        print("!! 目标: real 真实盘 !! 请确认 QMT 登录的是真实资金账号")
    print("将发 %d 只买入信号 (各 %d 股, 挂次日涨停价):" % (len(picks), VOLUME))
    for p in picks:
        print("  %s %s 挂单 %.2f" % (p["code"], p["name"], limit_up_price(p["close"], p["ratio"])))

    if not args.yes:
        r = input("确认写入 pending? [y/N] ").strip().lower()
        if r != "y":
            print("已取消。")
            return

    for p in picks:
        oid = send_signal(p["code"], limit_up_price(p["close"], p["ratio"]), VOLUME, ACCOUNT)
        print("  已发 %s %s -> %s" % (p["code"], p["name"], oid))
    print("\n[%s] 发单完成。切到 QMT 看桥日志 ORDER..., 再查委托面板。" % ENV)


def main():
    ap = argparse.ArgumentParser(description="收盘涨停科技股策略")
    ap.add_argument("cmd", choices=["screen", "send"])
    ap.add_argument("--code", help="send 时只发指定股票代码")
    ap.add_argument("--yes", action="store_true", help="send 时跳过确认")
    ap.add_argument("--force", action="store_true",
                    help="send 时强制发送过期(>1天)的候选清单")
    args = ap.parse_args()

    xtdata.connect()
    time.sleep(2)

    if args.cmd == "screen":
        cmd_screen(args)
    else:
        cmd_send(args)


if __name__ == "__main__":
    main()
