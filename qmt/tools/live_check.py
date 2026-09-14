# -*- coding: utf-8 -*-
"""miniQMT 实盘接入 · 只读就绪自检（preflight）。

用途: 下发任何真实委托之前跑一遍, 一次性确认"接口 / 数据格式 / 配置 / 运行条件"
四类前置是否齐备。**严格只读**: 只做 query_* 查询与方法存在性检查, 绝不调用
order_stock / order_stock_async / cancel_order_stock / passorder 等任何下单接口。

用法:
    python -m qmt.tools.live_check            # 全量自检
    python -m qmt.tools.live_check --quiet    # 只打印失败项与结论

退出码: 0 = 关键项全通过; 1 = 存在 FAIL(阻断实盘); 2 = 存在 WARN(可继续但不完备)
"""
import argparse
import json
import os
import pickle
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 本文件位于 qmt/tools/, 上三级才是项目根
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(REPO, "runtime")
QMT_DATA_DIR = r"D:\QMT\userdata_mini"
SIGNAL_ROOT = r"D:/QMT_SIGNALS"
PAUSE_FILE = os.path.join(SIGNAL_ROOT, "paused")
ARMED_FILE = os.path.join(SIGNAL_ROOT, "real", "armed.txt")
DEDUP_FILE = os.path.join(SIGNAL_ROOT, "real", "placed_today.json")
BRIDGE = os.path.join(REPO, "qmt", "bridge", "signal_bridge_real.py")
STRATEGY_POINTER = os.path.join(REPO, "prism", "strategies", ".active.json")
MKT_CACHE = os.path.join(RUNTIME, "cache", ".market_data_cache.pkl")
ZT_CACHE = os.path.join(RUNTIME, "cache", ".zt_history_cache.pkl")
LIVE_DAEMON = os.path.join(REPO, "prism", "live_daemon.py")
LIVE_ACCOUNT = os.path.join(REPO, "prism", "live_account.py")
LIVE_STATE = os.path.join(RUNTIME, "state", "live_state.json")
POSITIONS = os.path.join(RUNTIME, "state", "positions.json")

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_rows = []


def rec(level, section, item, detail):
    _rows.append((level, section, item, detail))


# ---------------------------------------------------------------- 接口
def check_interfaces():
    try:
        from xtquant import xtdata, xttrader, xttype, xtconstant  # noqa: F401
    except Exception as e:
        rec(FAIL, "接口", "xtquant 导入", "失败: %r" % (e,))
        return {"connected": False, "account_id": ""}
    rec(OK, "接口", "xtquant 导入", "xtdata/xttrader/xttype/xtconstant 均可导入")

    # 常量口径(外部 API 与 QMT 内 passorder 参数体系不同, 是最易踩的坑)
    try:
        pairs = [("STOCK_BUY", 23), ("STOCK_SELL", 24), ("FIX_PRICE", 11)]
        bad = [(n, getattr(xtconstant, n, None)) for n, v in pairs
               if getattr(xtconstant, n, None) != v]
        if bad:
            rec(WARN, "接口", "下单常量", "与预期不符: %s" % bad)
        else:
            rec(OK, "接口", "下单常量",
                "STOCK_BUY=23 / STOCK_SELL=24 / FIX_PRICE=11(外部 API 口径)")
    except Exception as e:
        rec(WARN, "接口", "下单常量", repr(e)[:80])

    # 行情连接
    try:
        xtdata.connect()
        rec(OK, "接口", "xtdata 行情连接", "连接成功")
    except Exception as e:
        rec(FAIL, "接口", "xtdata 行情连接", "失败: %r" % (e,))

    # 交易连接 + 只读查询
    if not os.path.isdir(QMT_DATA_DIR):
        rec(FAIL, "接口", "QMT 数据目录", "不存在: %s" % QMT_DATA_DIR)
        return {"connected": False, "account_id": ""}
    try:
        trader = xttrader.XtQuantTrader(QMT_DATA_DIR, int(time.time()))
        trader.start()
        rc = trader.connect()
        if rc != 0:
            rec(FAIL, "接口", "xttrader 交易连接", "connect()=%s(应为 0)" % rc)
            return {"connected": False, "account_id": ""}
        rec(OK, "接口", "xttrader 交易连接", "connect()=0")
    except Exception as e:
        rec(FAIL, "接口", "xttrader 交易连接", "异常: %r" % (e,))
        return {"connected": False, "account_id": ""}

    # 账号枚举 + 订阅 + 资产/持仓
    account_id = ""
    try:
        infos = trader.query_account_infos() or []
        ids = [str(getattr(a, "account_id", "") or "") for a in infos]
        ids = [i for i in ids if i]
        if ids:
            account_id = ids[0]
            rec(OK, "接口", "账号枚举", "已登录资金账号: %s" % ", ".join(ids))
        else:
            rec(FAIL, "接口", "账号枚举", "未发现已登录账号(请在 QMT 终端登录)")
    except Exception as e:
        rec(WARN, "接口", "账号枚举", "query_account_infos 不可用: %r" % (e,))

    if account_id:
        try:
            acc = xttype.StockAccount(account_id, "STOCK")
            sub = trader.subscribe(acc)
            rec(OK if sub == 0 else FAIL, "接口", "账号订阅", "subscribe()=%s" % sub)
            asset = trader.query_stock_asset(acc)
            if asset:
                rec(OK, "接口", "资产查询",
                    "总资产=%.2f 可用=%.2f 市值=%.2f 冻结=%.2f"
                    % (asset.total_asset, asset.cash, asset.market_value,
                       asset.frozen_cash))
            else:
                rec(WARN, "接口", "资产查询", "返回 None")
            pos = trader.query_stock_positions(acc) or []
            rec(OK if pos is not None else WARN, "接口", "持仓查询",
                "%d 只持仓" % len(pos))
            orders = trader.query_stock_orders(acc) or []
            trades = trader.query_stock_trades(acc) or []
            rec(OK, "接口", "委托/成交查询",
                "当日委托 %d 笔 / 成交 %d 笔" % (len(orders), len(trades)))
        except Exception as e:
            rec(FAIL, "接口", "账号订阅/查询", "异常: %r" % (e,))

    # 下单能力: 只检查方法存在, 绝不调用
    try:
        names = dir(trader)
        need = ["order_stock", "order_stock_async", "cancel_order_stock",
                "cancel_order_stock_async", "query_stock_asset",
                "query_stock_positions", "query_stock_orders",
                "query_stock_trades"]
        miss = [n for n in need if n not in names]
        rec(FAIL if miss else OK, "接口", "下单/撤单接口可用性",
            ("缺失: %s" % miss) if miss else
            "order_stock / order_stock_async / cancel_order_stock 均存在(仅检查未调用)")
    except Exception as e:
        rec(WARN, "接口", "下单接口探测", repr(e)[:80])

    try:
        trader.stop()
    except Exception:
        pass

    # QMT 内置策略通道(passorder)只能粘进终端跑, 此处仅确认桥脚本在位
    if os.path.isfile(BRIDGE):
        rec(OK, "接口", "QMT 内置桥脚本", "存在: %s" % os.path.basename(BRIDGE))
    else:
        rec(WARN, "接口", "QMT 内置桥脚本", "缺失: %s" % BRIDGE)
    return {"connected": True, "account_id": account_id}


# ---------------------------------------------------------------- 配置
def check_config():
    # 信号目录结构
    for env in ("sim", "real"):
        for sub in ("pending", "done", "failed", "trades"):
            d = os.path.join(SIGNAL_ROOT, env, sub)
            if not os.path.isdir(d):
                rec(WARN, "配置", "信号目录 %s/%s" % (env, sub), "缺失: %s" % d)
    if os.path.isdir(SIGNAL_ROOT):
        rec(OK, "配置", "信号根目录", SIGNAL_ROOT)

    # 真实盘安全闸门
    armed_ok = False
    if os.path.isfile(ARMED_FILE):
        try:
            content = open(ARMED_FILE, encoding="utf-8").read().strip()
            today = datetime.now().strftime("%Y%m%d")
            armed_ok = today in content
            rec(OK if armed_ok else WARN, "配置", "实盘 armed 闸门",
                "文件存在, 含今日 %s: %s" % (today, armed_ok))
        except Exception as e:
            rec(WARN, "配置", "实盘 armed 闸门", "读取失败: %r" % (e,))
    else:
        rec(WARN, "配置", "实盘 armed 闸门",
            "未武装(正常 —— 未创建 D:/QMT_SIGNALS/real/armed.txt 即不下真实单)")
    if not armed_ok:
        rec(WARN, "配置", "实盘下单被拦截",
            "armed 缺失 → 桥端把 pending 全部判 NOT ARMED 转 failed(安全)")

    # 暂停开关
    rec(OK if os.path.isfile(PAUSE_FILE) else WARN, "配置", "一键暂停开关",
        "已暂停(存在 %s)" % PAUSE_FILE if os.path.isfile(PAUSE_FILE)
        else "未暂停(不存在 %s)" % PAUSE_FILE)

    # 去重账
    if os.path.isfile(DEDUP_FILE):
        try:
            d = json.load(open(DEDUP_FILE, encoding="utf-8"))
            today = datetime.now().strftime("%Y%m%d")
            rec(OK, "配置", "当日去重账", "今日已下单 %d 只" % len(d.get(today, [])))
        except Exception as e:
            rec(WARN, "配置", "当日去重账", "解析失败: %r" % (e,))
    else:
        rec(OK, "配置", "当日去重账", "尚未产生(今日未下单)")

    # 桥脚本 DRY_RUN 状态
    if os.path.isfile(BRIDGE):
        try:
            txt = open(BRIDGE, encoding="gbk", errors="replace").read()
            dry = "DRY_RUN = True" in txt
            fixed = "FIXED_ACCOUNT = \"\"" in txt
            rec(OK, "配置", "桥脚本 DRY_RUN",
                "DRY_RUN=True(仅日志不下单)" if dry
                else "DRY_RUN=False(会下真实单 —— 确认这是你要的)")
            rec(OK, "配置", "桥脚本账号", "FIXED_ACCOUNT 留空(自动用登录账号)"
                if fixed else "FIXED_ACCOUNT 已硬编码")
        except Exception as e:
            rec(WARN, "配置", "桥脚本解析", repr(e)[:80])

    # 策略指针
    if os.path.isfile(STRATEGY_POINTER):
        try:
            p = json.load(open(STRATEGY_POINTER, encoding="utf-8"))
            sid = p.get("id")
            f = os.path.join(REPO, "prism", "strategies", "%s.json" % sid)
            rec(OK if os.path.isfile(f) else FAIL, "配置", "激活策略",
                "id=%s %s" % (sid, "(文件在位)" if os.path.isfile(f) else "(策略文件缺失!)"))
        except Exception as e:
            rec(WARN, "配置", "激活策略", "解析失败: %r" % (e,))
    else:
        rec(WARN, "配置", "激活策略", "指针缺失: %s" % STRATEGY_POINTER)


# ---------------------------------------------------------------- 实盘出口
def check_live_exit():
    """prism 引擎 → 实盘出口是否就位(P0 补齐项)。

    检查: 模块在位且可导入 / live_state.json 与 positions.json / 策略
    execution 与 sell_rules 参数。注意本项**不检查守护进程是否在跑**
    (无法可靠探测), 是否已启动以 live_state.json 的 mtime 为旁证。"""
    for label, path in (("live_daemon.py", LIVE_DAEMON),
                        ("live_account.py", LIVE_ACCOUNT)):
        rec(OK if os.path.isfile(path) else FAIL, "实盘出口", label,
            path if os.path.isfile(path) else "缺失: %s" % path)
    try:
        import importlib
        for mod in ("shared.exit_rules", "prism.live_account", "prism.live_daemon"):
            importlib.import_module(mod)
        rec(OK, "实盘出口", "模块可导入",
            "shared.exit_rules / prism.live_account / prism.live_daemon")
    except Exception as e:
        rec(FAIL, "实盘出口", "模块可导入", "失败: %r" % (e,))

    for label, path in (("live_state.json", LIVE_STATE),
                        ("positions.json", POSITIONS)):
        if os.path.isfile(path):
            try:
                json.load(open(path, encoding="utf-8"))
                rec(OK, "实盘出口", label, "在位可解析 (%s)" % path)
            except Exception as e:
                rec(FAIL, "实盘出口", label, "解析失败: %r" % (e,))
        else:
            rec(WARN, "实盘出口", label,
                "不存在(守护尚未跑过): %s" % path)

    try:
        p = json.load(open(STRATEGY_POINTER, encoding="utf-8"))
        f = os.path.join(REPO, "prism", "strategies", "%s.json" % p.get("id"))
        if os.path.isfile(f):
            s = json.load(open(f, encoding="utf-8"))
            ex = s.get("execution") or {}
            miss = [k for k in ("pct", "top_n", "open_window", "pick_slot")
                    if not ex.get(k)]
            rec(OK if not miss else WARN, "实盘出口", "策略 execution 参数",
                "pct=%s top_n=%s 窗口=%s" % (ex.get("pct"), ex.get("top_n"),
                                             ex.get("open_window"))
                if not miss else "缺 %s(守护将回落默认值)" % miss)
            sr = s.get("sell_rules") or {}
            rec(OK if sr else WARN, "实盘出口", "策略 sell_rules",
                "止盈 %s / 止损 %s / 持有 %s 日"
                % (sr.get("take_profit_pct"), sr.get("stop_loss_pct"),
                   sr.get("max_hold_days")))
        else:
            rec(WARN, "实盘出口", "策略 execution 参数", "策略文件缺失: %s" % f)
    except Exception as e:
        rec(WARN, "实盘出口", "策略 execution 参数", "读取失败: %r" % (e,))


# ---------------------------------------------------------------- 数据
def check_data():
    # 市场数据缓存
    if os.path.isfile(MKT_CACHE):
        try:
            d = pickle.load(open(MKT_CACHE, "rb"))
            kl = d.get("kline") or {}
            g = d.get("global") or {}
            sw = sorted(c for c in (d.get("sectors") or {}) if c.startswith("80"))

            def last(seg, c):
                dd = (seg.get(c) or {}).get("dates") or []
                return dd[-1] if dd else "-"

            kl_last = max((last(kl, c) for c in sw if c in kl), default="-")
            rec(OK if kl_last != "-" else WARN, "数据", "板块K线缓存", "末日 %s" % kl_last)
            gl = {k: last(g, k) for k in sorted(g)}
            rec(OK if gl else WARN, "数据", "全球指数缓存", str(gl))
            fl = d.get("flow") or {}
            rec(OK if len(fl) >= 31 else WARN, "数据", "板块资金流缓存",
                "覆盖 %d/31" % len(fl))
            fr = len((d.get("flow_rank") or {}).get("dates") or [])
            rec(OK if fr >= 5 else WARN, "数据", "资金惯性序列",
                "%d 天(>=5 天才判'系统性增配')" % fr)
            bm = (d.get("benchmark") or {}).get("dates") or []
            rec(OK if bm else WARN, "数据", "上证基准缓存",
                "末日 %s" % (bm[-1] if bm else "-"))
            rec(OK, "数据", "行业映射", "%d 只" % len(d.get("sector_map") or {}))
        except Exception as e:
            rec(FAIL, "数据", "市场数据缓存", "读取失败: %r" % (e,))
    else:
        rec(WARN, "数据", "市场数据缓存", "缺失: %s" % MKT_CACHE)

    # 涨停池缓存
    if os.path.isfile(ZT_CACHE):
        mt = datetime.fromtimestamp(os.path.getmtime(ZT_CACHE))
        age_h = (datetime.now() - mt).total_seconds() / 3600.0
        rec(OK if age_h <= 30 else WARN, "数据", "涨停池缓存",
            "更新于 %s(%.1f 小时前)" % (mt.strftime("%Y-%m-%d %H:%M"), age_h))
    else:
        rec(FAIL, "数据", "涨停池缓存", "缺失: %s" % ZT_CACHE)

    # 磁盘守卫: 收盘选股依赖的实时链
    try:
        import prism  # noqa: F401
        rec(OK, "数据", "prism 包导入", "正常")
    except Exception as e:
        rec(FAIL, "数据", "prism 包导入", repr(e)[:100])


# ---------------------------------------------------------------- 运行条件
def check_runtime(ifaces=None):
    ifaces = ifaces or {}
    now = datetime.now()
    wd = now.weekday()
    hm = now.strftime("%H:%M")
    rec(OK if wd < 5 else WARN, "运行", "自然日",
        "%s(%s)" % (now.strftime("%Y-%m-%d"), "工作日" if wd < 5 else "周末"))
    in_sess = wd < 5 and (("09:30" <= hm <= "11:30") or ("13:00" <= hm <= "15:00"))
    rec(OK, "运行", "交易时段", "盘中" if in_sess else "非盘中(当前 %s)" % hm)
    rec(WARN, "运行", "交易日历",
        "prism 仅按 weekday 判断, 无节假日日历 —— 节假日会误判为交易日")

    # QMT 终端: 以"交易接口连上"为第一证据(比 tasklist 可靠), tasklist 仅作补充。
    # 注意 tasklist 在受限环境可能被拦而返回空 —— 空输出代表"无法检测",
    # 绝不能当成"进程未运行", 否则会误报阻断。
    if ifaces.get("connected"):
        rec(OK, "运行", "QMT 终端进程",
            "已在运行(证据: xttrader connect()=0, 账号 %s)"
            % (ifaces.get("account_id") or "-"))
    else:
        rec(FAIL, "运行", "QMT 终端进程",
            "交易接口未连上 —— 请确认已登录 QMT 并开启 miniQMT 模式")

    try:
        import subprocess

        def _tasklist(image):
            """返回 (可检测: bool, 输出文本)。检测不可用时返回 (False, '')。"""
            try:
                r = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq %s" % image, "/FO", "CSV"],
                    capture_output=True, text=True, timeout=10,
                    encoding="utf-8", errors="replace")
                out = r.stdout
                if not out or not out.strip():
                    return False, ""
                return True, out
            except Exception:
                return False, ""

        usable, out = _tasklist("python.exe")
        if usable:
            n_py = out.lower().count("python.exe")
            rec(OK if n_py else WARN, "运行", "python 进程数", "%d 个" % n_py)
        else:
            rec(WARN, "运行", "python 进程数",
                "tasklist 不可用(受限环境), 无法检测(不等于 0 个)")
    except Exception as e:
        rec(WARN, "运行", "进程检查", repr(e)[:80])


def main():
    ap = argparse.ArgumentParser(description="miniQMT 实盘接入只读自检")
    ap.add_argument("--quiet", action="store_true", help="只打印非 OK 项")
    args = ap.parse_args()

    print("=" * 72)
    print("miniQMT 实盘接入 · 只读就绪自检   %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("(本工具不下任何单, 只做 query_* 与方法存在性检查)")
    print("=" * 72)

    ifaces = check_interfaces()
    check_config()
    check_live_exit()
    check_data()
    check_runtime(ifaces)

    order = {FAIL: 0, WARN: 1, OK: 2}
    _rows.sort(key=lambda r: order[r[0]])
    cur = None
    for level, section, item, detail in _rows:
        if args.quiet and level == OK:
            continue
        if section != cur:
            print("\n[%s]" % section)
            cur = section
        print("  %-4s %-22s %s" % (level, item, detail))

    n_fail = sum(1 for r in _rows if r[0] == FAIL)
    n_warn = sum(1 for r in _rows if r[0] == WARN)
    print("\n" + "=" * 72)
    print("汇总: %d 项通过 / %d 项警告 / %d 项阻断" %
          (sum(1 for r in _rows if r[0] == OK), n_warn, n_fail))
    if n_fail:
        print("结论: 存在阻断项, 不具备实盘条件。")
    elif n_warn:
        print("结论: 关键项通过, 但存在警告项(不影响下单, 建议逐条确认)。")
    else:
        print("结论: 全部通过。")
    print("提示: 本自检通过 ≠ 可以下单。真实下单前请完成"
          "「DRY_RUN 演练 → 模拟盘通道(sim) → 小额真实单」三步。")
    return 1 if n_fail else (2 if n_warn else 0)


if __name__ == "__main__":
    sys.exit(main())
