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
from exit_rules import PositionBook

# ====================== 配置区 ======================
ENV = "sim"                          # "sim"模拟盘 / "real"真实盘
STATE_FILE = Path(__file__).parent / "close_pick_state.json"   # 候选清单存盘
POSITIONS_FILE = Path(__file__).parent / "positions.json"      # 持仓档案(卖出策略用)
VOLUME = 100                         # 每只 100 股(无资金查询时的兜底)
MAX_PICKS = 5                        # 每天最多选几只(防止一次发太多)
ACCOUNT = ""                         # 留空让 QMT 端自动用登录账号
MAX_PICK_AGE_DAYS = 3                # send 允许的清单最大年龄(天): 覆盖周末(周五选→周一发=3天)

# 仓位/资金管理: 单只占用可用资金的最高比例(0.30 = 最多用 30% 资金买一只)
POSITION_RATIO = 0.30
# 卖出策略默认参数(exit 命令用, 可 --take-profit/--stop-loss/--hold-days 覆盖)
TAKE_PROFIT_PCT = 0.08               # 止盈: +8%
STOP_LOSS_PCT = 0.05                 # 止损: -5%
MAX_HOLD_DAYS = 5                    # 持有 N 个自然日后强制平仓
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


def send_signal(stock_code, price, volume, account_id="", action="BUY"):
    """写买入/卖出信号到 pending 目录。action: "BUY" / "SELL"(桥已支持 SELL)。"""
    prefix = "SELL" if action == "SELL" else "BUY"
    order_id = "%s_%s" % (prefix, uuid.uuid4().hex[:8])
    signal = {
        "order_id": order_id,
        "action": action,
        "stock_code": stock_code,
        "order_type": action,
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


# ---------------- 持仓档案(卖出策略用) ----------------

def load_positions():
    if POSITIONS_FILE.exists():
        try:
            return PositionBook.from_json(
                json.loads(POSITIONS_FILE.read_text(encoding="utf-8")))
        except Exception:
            pass
    return PositionBook()


def save_positions(book):
    POSITIONS_FILE.write_text(
        json.dumps(book.all(), ensure_ascii=False, indent=2), encoding="utf-8")


def record_buy_positions(picks):
    """买入信号发出后记账: 以"明日涨停价"作为买入成本价记录持仓。"""
    book = load_positions()
    for p in picks:
        price = limit_up_price(p["close"], p["ratio"])
        book.add(p["code"], p["name"], buy_price=price,
                 buy_date=date.today().isoformat())
    save_positions(book)
    print("持仓档案已更新: %d 只 (%s)" % (len(picks), POSITIONS_FILE))


# ---------------- 仓位/资金管理 ----------------

def query_available_cash():
    """查询账户可用资金(需 QMT 交易通道)。失败/未连接 → None(调用方回退固定 VOLUME)。"""
    try:
        from xtquant import xttrader, xttype
        import time as _t
        cb = xttrader.XtQuantTraderCallback.__new__(xttrader.XtQuantTraderCallback)
        trader = xttrader.XtQuantTrader(r"D:\QMT\userdata_mini", int(_t.time()), cb)
        trader.start()
        if trader.connect() != 0:
            return None
        acc = xttype.StockAccount(ACCOUNT)
        trader.subscribe(acc)
        asset = trader.query_stock_asset(acc)
        return float(getattr(asset, "cash", 0) or 0)
    except Exception:
        return None


def calc_buy_volume(price, available_cash):
    """按仓位管理计算单只买入股数: 可用资金 × POSITION_RATIO ÷ 挂单价,
    向下取整到 100 股倍数。资金不可用/不足 → 回退固定 VOLUME。"""
    if price <= 0:
        return VOLUME
    if available_cash is None or available_cash <= 0:
        return VOLUME
    budget = available_cash * POSITION_RATIO
    shares = int(budget / price // 100 * 100)
    if shares < 100:
        return VOLUME
    return shares


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

    # 仓位/资金管理: 查询可用资金, 按 POSITION_RATIO 计算每只股数
    cash = query_available_cash()
    if cash:
        print("账户可用资金: %.2f 元 (单只仓位上限 %.0f%%)"
              % (cash, POSITION_RATIO * 100))
    else:
        print("未查询到账户资金(QMT交易通道不可用), 按固定股数 %d 股/只" % VOLUME)
    vols = {p["code"]: calc_buy_volume(limit_up_price(p["close"], p["ratio"]), cash)
            for p in picks}
    print("将发 %d 只买入信号:" % len(picks))
    for p in picks:
        print("  %s %s 挂单 %.2f x %d 股"
              % (p["code"], p["name"], limit_up_price(p["close"], p["ratio"]),
                 vols[p["code"]]))

    if not args.yes:
        r = input("确认写入 pending? [y/N] ").strip().lower()
        if r != "y":
            print("已取消。")
            return

    for p in picks:
        oid = send_signal(p["code"], limit_up_price(p["close"], p["ratio"]),
                          vols[p["code"]], ACCOUNT)
        print("  已发 %s %s -> %s" % (p["code"], p["name"], oid))
    # 卖出策略: 买入信号发出即记账(成本=挂单价), 供 exit 命令后续判定
    record_buy_positions(picks)
    print("\n[%s] 发单完成。切到 QMT 看桥日志 ORDER..., 再查委托面板。" % ENV)


def cmd_exit(args):
    """卖出策略: 拉持仓最新价, 按 止盈/止损/持有期 规则判定, 触发则发 SELL 信号。"""
    print("=" * 62)
    print("卖出巡检 exit | %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("规则: 止盈 +%.0f%% / 止损 -%.0f%% / 持有 %d 天强制"
          % (args.take_profit * 100, args.stop_loss * 100, args.hold_days))
    book = load_positions()
    if not book.all():
        print("持仓档案为空 (%s)。请先 send 发单(会自动记账)。" % POSITIONS_FILE)
        return

    # 拉持仓最新价(全市场盘口)
    codes = list(book.all().keys())
    ticks = {}
    try:
        ticks = xtdata.get_full_tick(codes)
    except Exception as e:
        print("拉行情失败: %r" % e)
        return
    last_prices = {}
    for code in codes:
        t = ticks.get(code) or {}
        lp = t.get("lastPrice") or 0
        if lp > 0:
            last_prices[code] = lp

    hits = book.evaluate_all(last_prices, take_profit_pct=args.take_profit,
                             stop_loss_pct=args.stop_loss,
                             max_hold_days=args.hold_days)
    if not hits:
        print("无触发卖出的持仓 (%d 只巡检完毕)" % len(codes))
        return

    if ENV == "sim":
        print("目标: sim 模拟盘")
    else:
        print("!! 目标: real 真实盘 !! 请确认 QMT 登录的是真实资金账号")
    print("触发卖出 %d 只:" % len(hits))
    for code, pos, action, reason in hits:
        print("  %s %s 现价 %.2f 成本 %.2f | %s"
              % (code, pos.get("name"), last_prices.get(code, 0),
                 pos.get("buy_price"), reason))

    if not args.yes:
        r = input("确认写入 SELL 信号? [y/N] ").strip().lower()
        if r != "y":
            print("已取消。")
            return

    for code, pos, action, reason in hits:
        volume = int(pos.get("volume") or 0)
        if volume <= 0:
            print("  %s 无股数记录, 跳过(请手工处理)" % code)
            continue
        # SELL 用市价(price=0), 桥端按对手价成交; 也支持 --price 指定
        oid = send_signal(code, args.price or 0, volume, ACCOUNT, action="SELL")
        print("  已发 SELL %s %s x%d -> %s (%s)" % (code, pos.get("name"),
                                                    volume, oid, reason))
        book.remove(code)
    save_positions(book)
    print("\n[%s] SELL 发单完成, 持仓档案已更新(已触发者移除)。" % ENV)


def main():
    ap = argparse.ArgumentParser(description="收盘涨停科技股策略")
    ap.add_argument("cmd", choices=["screen", "send", "exit"])
    ap.add_argument("--code", help="send 时只发指定股票代码")
    ap.add_argument("--yes", action="store_true", help="跳过确认")
    ap.add_argument("--force", action="store_true",
                    help="send 时强制发送过期(>1天)的候选清单")
    # 卖出策略参数
    ap.add_argument("--take-profit", type=float, default=TAKE_PROFIT_PCT,
                    help="止盈幅度(默认0.08=+8%%)")
    ap.add_argument("--stop-loss", type=float, default=STOP_LOSS_PCT,
                    help="止损幅度(默认0.05=-5%%)")
    ap.add_argument("--hold-days", type=int, default=MAX_HOLD_DAYS,
                    help="持有 N 个自然日后强制平仓(默认5)")
    ap.add_argument("--price", type=float, default=0.0,
                    help="exit 时 SELL 委托价(默认0=市价/对手价)")
    args = ap.parse_args()

    xtdata.connect()
    time.sleep(2)

    if args.cmd == "screen":
        cmd_screen(args)
    elif args.cmd == "exit":
        cmd_exit(args)
    else:
        cmd_send(args)


if __name__ == "__main__":
    main()
