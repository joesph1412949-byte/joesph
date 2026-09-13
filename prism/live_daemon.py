# -*- coding: utf-8 -*-
"""实盘信号守护: prism 36 因子引擎 → QMT 实盘出口(买入) + 盘中卖出巡检。

补的缺口(体检报告 §3.2):
  #1 调度出口   15:05 收盘选股(只落计划, 不写信号) → 次日 09:26-09:35
                写 BUY 信号(桥端 armed 闸门 + per-day dedup 消费)。
                收盘就写信号会被桥端在非交易时段拒单丢进 failed, 故两段式。
  #2 真实仓位   总资产 × 单只比例 ÷ 挂单价 → 整手(不再写死 100 股);
                单只比例/持仓上限复用策略 execution 块(pct/top_n)。
  #3 卖出链路   复用 exit_rules.ExitRule + positions.json, 盘中巡检 → SELL。
  #4 持仓对账   每轮(节流)用券商 query_stock_positions 校正账本: 消失的持仓
                移除、can_use_volume 回写(仅查询链路可信时才动账本)。
  #6 幂等       确定性 order_id(BUY_<日期>_<代码> / SELL_<日期>_<代码>),
                重启重跑不产生新文件; 桥端 per-day dedup 兜底。
  #8 T+1        当日买入不卖: exit_rules enforce_t1 + 券商 can_use_volume
                双保险; 跌停日顺延(exit_rules.is_limit_down)。

安全边界(与 prism/paper_daemon.py §8 同款):
  - **默认 dry_run=True**: 只记录/日志, 零副作用; 显式 --live 才落信号;
  - 本进程只写信号文件, **绝不调用任何下单接口**(下单在 QMT 桥端);
  - 行情/账户查询全 fail-open, 单轮异常不影响下一轮;
  - 真实盘最终闸门在桥端 armed.txt(当日日期), 本进程无法绕过。

用法:
    python -m prism.live_daemon                 # 演练(默认, 不落信号)
    python -m prism.live_daemon --live          # 真实写信号(需桥端已 armed)
    python -m prism.live_daemon --once          # 只跑一轮并打印结果
"""
import argparse
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

from exit_rules import PositionBook, is_limit_down
from prism import trader
from prism.live_account import LiveAccount, calc_buy_volume

LOG = logging.getLogger("live_daemon")

REPO = Path(__file__).resolve().parent.parent
STATE_FILE = REPO / "live_state.json"
POSITIONS_FILE = REPO / "positions.json"

PICK_SLOT = "15:05"               # 收盘选股时点(策略 execution.pick_slot)
OPEN_WINDOW = ("09:26", "09:35")  # 次日开盘买入窗口(execution.open_window)
SETTLE_AFTER = "15:00"
POLL_SECONDS = 30
SYNC_INTERVAL = 60                # 券商对账节流(秒)
STATE_VERSION = 1
DEFAULT_TAKE_PROFIT = 0.15        # 策略无 sell_rules 时的实盘缺省(同 full_factor_v1)
DEFAULT_STOP_LOSS = 0.05
DEFAULT_MAX_HOLD = 5


def next_weekday(d):
    """下一个工作日(无节假日日历, 见体检报告 §3.2 #7 —— 桥端 armed 兜底)。"""
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt


def _code_tag(code):
    """order_id 里用的代码片段(去点/去横线)。"""
    return str(code).replace(".", "").replace("-", "").upper()


def _atomic_write(path, text):
    """原子写: 同目录 tmp + os.replace, 防半截文件。"""
    path = Path(path)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class LiveDaemon:
    """实盘调度器。依赖全部可注入(provider/account/screen_fn/ticks_fn/now_fn),
    便于离线测试与演练。"""

    def __init__(self, provider=None, account=None, strategy=None,
                 env="real", dry_run=True, position_ratio=None,
                 max_positions=None, account_id="",
                 state_file=None, positions_file=None, signal_root=None,
                 screen_fn=None, ticks_fn=None, now_fn=None, sleep_fn=None):
        self.provider = provider
        self.account = account if account is not None \
            else LiveAccount(account_id=account_id)
        self.strategy = strategy          # None → 动态读默认策略指针
        self.env = env
        self.dry_run = bool(dry_run)
        self._position_ratio_ctor = position_ratio
        self._max_positions_ctor = max_positions
        self.position_ratio = float(position_ratio) if position_ratio else 0.15
        self.max_positions = int(max_positions) if max_positions else 5
        self.state_path = Path(state_file or STATE_FILE)
        self.positions_path = Path(positions_file or POSITIONS_FILE)
        self.signal_root = signal_root    # None → trader.SIGNAL_ROOT
        self.screen_fn = screen_fn or self._default_screen
        self.ticks_fn = ticks_fn
        self.now_fn = now_fn
        self.sleep_fn = sleep_fn
        self._strat_cache = None
        self._last_sync_ts = 0.0

    # ---------------- 策略(动态读默认指针, 与模拟盘/回测同一份 JSON) ----------------
    def _resolve_strategy(self):
        if isinstance(self.strategy, dict):
            return self.strategy
        from prism import engine
        sid = self.strategy or engine.active_strategy_id()
        if self._strat_cache is not None \
                and self._strat_cache.get("id") == sid:
            return self._strat_cache
        from prism import registry as reg
        from prism.engine import load_strategy
        reg.scan_factors(force=True)      # 幂等重扫(load_strategy 校验依赖)
        self._strat_cache = load_strategy(
            engine.STRATEGIES_DIR / ("%s.json" % sid))
        exec_blk = (self._strat_cache.get("execution") or {})
        # execution 块优先(策略 spec 单一事实源), 缺省回落构造参数
        pct = exec_blk.get("pct")
        if pct and self._position_ratio_ctor is None:
            self.position_ratio = float(pct)
        top_n = exec_blk.get("top_n")
        if top_n and self._max_positions_ctor is None:
            self.max_positions = int(top_n)
        return self._strat_cache

    def _sell_rules(self):
        try:
            strat = self._resolve_strategy() or {}
        except Exception:
            strat = {}
        rules = strat.get("sell_rules") or {}
        return (float(rules.get("take_profit_pct") or DEFAULT_TAKE_PROFIT),
                float(rules.get("stop_loss_pct") or DEFAULT_STOP_LOSS),
                int(rules.get("max_hold_days") or DEFAULT_MAX_HOLD))

    def _default_screen(self, now):
        return trader.run_daily(self._resolve_strategy(), self.provider,
                                env=self.env, write=False)

    # ---------------- 状态文件 ----------------
    def _load_state(self):
        if self.state_path.exists():
            try:
                d = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    d.setdefault("version", STATE_VERSION)
                    d.setdefault("plans", [])
                    d.setdefault("pick_slots", [])
                    d.setdefault("exit_signaled", {})
                    return d
            except Exception:
                pass
        return {"version": STATE_VERSION, "plans": [], "pick_slots": [],
                "exit_signaled": {}}

    def _save_state(self, st):
        _atomic_write(self.state_path,
                      json.dumps(st, ensure_ascii=False, indent=2))

    def _load_positions(self):
        if self.positions_path.exists():
            try:
                return PositionBook.from_json(json.loads(
                    self.positions_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return PositionBook()

    def _save_positions(self, book):
        _atomic_write(self.positions_path,
                      json.dumps(book.all(), ensure_ascii=False, indent=2))

    # ---------------- 行情 ----------------
    def _ticks(self, codes):
        """{code: tick}; 无数据源/异常 → {}(fail-open, 本轮不判定卖出)。"""
        if not codes:
            return {}
        if self.ticks_fn:
            try:
                return self.ticks_fn(list(codes)) or {}
            except Exception:
                return {}
        try:
            return self.provider.ds.get_full_market_ticks(list(codes)) or {}
        except Exception:
            return {}

    # ---------------- 信号落盘 ----------------
    def _emit(self, signals, out_key, out):
        """写信号(dry_run → 只记录不落盘)。返回写入数。"""
        out[out_key] = signals
        if self.dry_run or not signals:
            return 0
        return trader.write_signals(signals, env=self.env,
                                    root=self.signal_root)

    # ---------------- 券商对账(gap #4) ----------------
    def _sync_positions(self):
        """用券商持仓校正本地账本。返回 (变更数, 链路是否可信)。

        仅当 asset() 查询成功才动账本 —— 拿不到账户事实时宁可不改
        (防"查询失败 → 误判空仓 → 抹掉账本")。"""
        if self.account is None:
            return 0, False
        if self.account.asset() is None:
            return 0, False
        pos = self.account.positions()
        book = self._load_positions()
        changed = 0
        for code in list(book.all()):
            if code not in pos:          # 券商已无持仓 → 已了结/已卖出 → 移除
                book.remove(code)
                changed += 1
        for code, p in pos.items():
            if book.get(code) is not None:
                if book.update(code, can_use_volume=int(
                        p.get("can_use_volume") or 0)):
                    changed += 1
        if changed:
            self._save_positions(book)
        return changed, True

    def _maybe_sync(self, now, out):
        """节流对账(默认 SYNC_INTERVAL 一次); 异常不打断 tick。"""
        try:
            ts = now.timestamp()
            if ts - self._last_sync_ts < SYNC_INTERVAL:
                return
            self._last_sync_ts = ts
            out["sync"] = self._sync_positions()[0]
        except Exception as e:
            LOG.warning("对账失败(忽略): %r", e)

    # ---------------- 收盘选股(gap #1/#2) ----------------
    def _do_close_pick(self, now, st, out):
        res = self.screen_fn(now) or {}
        sigs = res.get("signals") or []
        out["screen"] = {"environment_ok": res.get("environment_ok"),
                         "candidates": len(res.get("candidates") or []),
                         "signals": len(sigs),
                         "error": res.get("error")}
        if not sigs:
            LOG.info("收盘选股: 无信号(env_ok=%s)", res.get("environment_ok"))
            return
        asset = self.account.total_asset() if self.account else None
        if asset is None:
            out["skipped"].append("asset_unavailable")
            LOG.warning("收盘选股: 拿不到账户总资产 → 本日不建仓(fail-closed)")
            return
        book = self._load_positions()
        slots = self.max_positions - len(book.all())
        if slots <= 0:
            out["skipped"].append("max_positions")
            LOG.info("收盘选股: 持仓已满(%d 只) → 不建仓", self.max_positions)
            return
        for_date = next_weekday(now.date()).isoformat()
        plans = []
        for s in sigs:
            price = float(s.get("price") or 0)
            vol = calc_buy_volume(price, asset, self.position_ratio)
            if vol <= 0:
                out["skipped"].append(s.get("stock_code"))
                continue
            plans.append({"code": s["stock_code"], "name": s.get("name") or "",
                          "price": price, "volume": vol,
                          "for_date": for_date,
                          "created": now.strftime("%Y-%m-%dT%H:%M:%S"),
                          "strategy_id": s.get("strategy_id"),
                          "composite": s.get("composite")})
            if len(plans) >= slots:
                break
        st["plans"] = [p for p in st["plans"]
                       if p.get("for_date") != for_date] + plans
        out["planned"] = plans
        LOG.info("收盘选股: 计划 %d 只(总资产 %.0f, 单只 %.0f%%, 上限 %d 只) → %s 发单",
                 len(plans), asset, self.position_ratio * 100,
                 self.max_positions, for_date)

    # ---------------- 次日开盘发单 ----------------
    def _do_open_send(self, now, st, out):
        d = now.strftime("%Y-%m-%d")
        plans = [p for p in st["plans"] if p.get("for_date") == d]
        if not plans:
            return
        sigs = [trader.build_signal(
            code=p["code"], action="BUY", price=p["price"],
            volume=p["volume"],
            order_id="BUY_%s_%s" % (d.replace("-", ""), _code_tag(p["code"])),
            strategy_id=p.get("strategy_id") or "live",
            composite=p.get("composite")) for p in plans]
        n = self._emit(sigs, "buys", out)
        if self.dry_run:
            LOG.info("开盘发单(DRY-RUN): 将发 %d 只, 未落盘", len(sigs))
            return
        book = self._load_positions()
        for p in plans:
            book.add(p["code"], p.get("name") or p["code"],
                     buy_price=p["price"], buy_date=d, volume=p["volume"])
        self._save_positions(book)
        st["plans"] = [p for p in st["plans"] if p.get("for_date") != d]
        LOG.info("开盘发单: %d 只写入 pending(已记账, 待券商对账校正)", n)

    # ---------------- 盘中卖出巡检(gap #3/#8) ----------------
    def _do_sell_patrol(self, now, st, out):
        d = now.strftime("%Y-%m-%d")
        book = self._load_positions()
        signaled = st.get("exit_signaled") or {}
        codes = [c for c, p in book.all().items()
                 if str(p.get("buy_date", "")) < d and signaled.get(c) != d]
        if not codes:
            return
        ticks = self._ticks(codes)
        last, last_close = {}, {}
        for c in codes:
            t = ticks.get(c) or {}
            lp = float(t.get("lastPrice") or 0)
            if lp > 0:
                last[c] = lp
            lc = float(t.get("lastClose") or 0)
            if lc > 0:
                last_close[c] = lc
        try:
            can_use = self.account.can_use_map() if self.account else {}
        except Exception:
            can_use = {}
        tp, sl, hold = self._sell_rules()
        hits = book.evaluate_all(last, can_use=can_use or None,
                                 enforce_t1=True, today=now.date(),
                                 take_profit_pct=tp, stop_loss_pct=sl,
                                 max_hold_days=hold)
        sigs = []
        for code, pos, action, reason in hits:
            if is_limit_down(code, last.get(code), last_close.get(code)):
                out["skipped"].append("limit_down:%s" % code)
                continue
            avail = can_use.get(code) if can_use else None
            vol = int(avail if avail else (pos.get("volume") or 0))
            if vol <= 0:
                out["skipped"].append("no_volume:%s" % code)
                continue
            sig = trader.build_signal(
                code=code, action="SELL", price=0, volume=vol,
                order_id="SELL_%s_%s" % (d.replace("-", ""), _code_tag(code)),
                strategy_id="live")
            sig["reason"] = reason          # 桥端忽略未知键; 供盘后追溯
            sigs.append(sig)
        self._emit(sigs, "sells", out)
        if not self.dry_run and sigs:
            for s in sigs:
                st["exit_signaled"][s["stock_code"]] = d
            self._save_state(st)
        if sigs:
            LOG.info("卖出巡检: %d 只触发 → %s", len(sigs),
                     [(s["stock_code"], s.get("reason")) for s in sigs])

    # ---------------- 单轮 ----------------
    def in_session(self, now):
        """交易日时段: 周一~五 ∧ (09:30-11:30 ∨ 13:00-15:00)。"""
        if now.weekday() >= 5:
            return False
        hm = now.strftime("%H:%M")
        return ("09:30" <= hm <= "11:30") or ("13:00" <= hm <= "15:00")

    def tick_once(self, now=None):
        now = now or (self.now_fn() if self.now_fn else datetime.now())
        d = now.strftime("%Y-%m-%d")
        hm = now.strftime("%H:%M")
        out = {"action": "idle", "planned": [], "buys": [], "sells": [],
               "skipped": [], "sync": 0, "screen": None}
        st = self._load_state()

        # 过期计划清理(跨日重启): for_date < 今日 → 作废, 不追买
        stale = [p for p in st["plans"] if p.get("for_date", "") < d]
        if stale:
            st["plans"] = [p for p in st["plans"]
                           if p.get("for_date", "") >= d]
            self._save_state(st)

        # 开盘买入窗口: 09:26-09:35 优先于盯盘
        if now.weekday() < 5 and OPEN_WINDOW[0] <= hm <= OPEN_WINDOW[1] \
                and any(p.get("for_date") == d for p in st["plans"]):
            self._do_open_send(now, st, out)
            self._save_state(st)
            out["action"] = "open_send"
            return out

        # 收盘后: 对账 + 15:05 选股(每日一次)
        if now.weekday() < 5 and hm >= SETTLE_AFTER:
            self._maybe_sync(now, out)
            slot = "%sT%s" % (d, PICK_SLOT)
            if hm >= PICK_SLOT and self.provider and slot not in st["pick_slots"]:
                self._do_close_pick(now, st, out)
                st["pick_slots"] = (st["pick_slots"] + [slot])[-30:]
                self._save_state(st)
                out["action"] = "close_pick"
            return out

        # 盘中: 节流对账 + 卖出巡检
        if not self.in_session(now) or self.provider is None:
            return out
        out["action"] = "tick"
        self._maybe_sync(now, out)
        self._do_sell_patrol(now, st, out)
        return out

    # ---------------- 连接与主循环 ----------------
    def _sleep(self, sec):
        if self.sleep_fn:
            self.sleep_fn(sec)
        else:
            time.sleep(sec)

    def connect_provider(self, max_retry=60, retry_wait=10):
        """连接 QMT 行情数据源(与 paper_daemon.connect_provider 同款重试)。"""
        if self.provider is not None:
            return True
        from prism.data import DataProvider
        for _ in range(max_retry):
            try:
                p = DataProvider()
                p.connect()
                if p.connected:
                    self.provider = p
                    return True
            except Exception:
                pass
            self._sleep(retry_wait)
        return False

    def run_forever(self):
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        log = logging.getLogger("live_daemon")
        if not self.connect_provider():
            log.error("QMT 行情连接失败(重试上限), 退出")
            return
        mode = "DRY-RUN(不落信号)" if self.dry_run else "LIVE(写信号文件)"
        log.info("实盘信号守护启动: env=%s, %s, 单只 %.0f%%, 上限 %d 只, 每 %d 秒一轮",
                 self.env, mode, self.position_ratio * 100, self.max_positions,
                 POLL_SECONDS)
        if self.dry_run:
            log.warning("演练模式: 只日志不落盘; 确认无误后加 --live 才真实发单")
        else:
            log.warning("真实模式: 会写 %s/pending; 桥端 armed.txt 需含当日日期",
                        self.env)
        while True:
            try:
                out = self.tick_once()
                log.info("tick %s", {k: (len(v) if isinstance(v, (list, dict))
                                         else v) for k, v in out.items()})
            except Exception as e:
                log.exception("tick 异常(忽略, 下轮重试): %r", e)
            self._sleep(POLL_SECONDS)


def main(argv=None):
    ap = argparse.ArgumentParser(description="prism 实盘信号守护")
    ap.add_argument("--live", action="store_true",
                    help="真实写信号(默认演练: 只日志不落盘)")
    ap.add_argument("--once", action="store_true", help="只跑一轮后退出")
    ap.add_argument("--env", default="real", choices=["real", "sim"])
    ap.add_argument("--ratio", type=float, default=None,
                    help="单只仓位比例(默认取策略 execution.pct)")
    ap.add_argument("--max-positions", type=int, default=None,
                    help="最大持仓数(默认取策略 execution.top_n)")
    ap.add_argument("--account", default="", help="资金账号(默认自动枚举)")
    ap.add_argument("--strategy", default=None, help="策略 id(默认当前指针)")
    args = ap.parse_args(argv)

    daemon = LiveDaemon(account=LiveAccount(account_id=args.account),
                        strategy=args.strategy, env=args.env,
                        dry_run=not args.live,
                        position_ratio=args.ratio,
                        max_positions=args.max_positions)
    if args.once:
        daemon.connect_provider(max_retry=1, retry_wait=0)
        out = daemon.tick_once()
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return
    daemon.run_forever()


if __name__ == "__main__":
    main()
