# -*- coding: utf-8 -*-
"""做T守护: 轮询决策 → (可选)落信号 → 记账 → 写运行时快照。

安全边界(与 prism/live_daemon.py 同款, 三道闸门串联):
  1. **dry_run**  —— 默认 True, 只算不落盘; 显式 --live 才写信号文件;
  2. **paused**   —— D:/QMT_SIGNALS/paused 存在即整体停发(一键急停);
  3. **armed**    —— D:/QMT_SIGNALS/<env>/armed.txt 须含当日 YYYYMMDD,
                     这是桥端消费信号的最终闸门, 本进程无法绕过。

本进程**绝不调用任何下单接口**, 只写信号文件; 真实成交由 QMT 桥端完成。
账本按挂单价做"理论成交"记账(theoretical), 真实成交以券商回报为准 ——
面板与日志里都明确标注, 不假装它是实际成交。

用法:
    python -m tt.daemon              # 演练(默认, 零副作用)
    python -m tt.daemon --live       # 真实写信号(需 paused 不存在 + armed 就绪)
    python -m tt.daemon --once       # 只跑一轮并打印
    python -m tt.daemon --sample     # 用离线样本行情演示(非交易时段可跑)
"""
import argparse
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from shared.common import STATE_DIR

from . import config as tt_config
from . import market
from .engine import TTEngine
from .state import Ledger

LOG = logging.getLogger("tt_daemon")

REPO = Path(__file__).resolve().parent.parent
STATE_PATH = STATE_DIR / "tt_state.json"
RUNTIME_PATH = STATE_DIR / "tt_runtime.json"
# 信号根目录可用环境变量覆盖, 便于演练/测试时与真实 QMT 目录隔离
SIGNAL_ROOT = Path(os.environ.get("TT_SIGNAL_ROOT") or r"D:/QMT_SIGNALS")
PAUSE_FILE = SIGNAL_ROOT / "paused"
DEFAULT_INTERVAL = 5.0


def _atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write(text)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


def is_paused(root=None):
    """一键急停: <信号根>/paused 存在即整体停发。

    与 prism.trader.check_paused 同口径(那个用固定的 D:/QMT_SIGNALS/paused)。
    root 传的是"信号根目录", paused 文件在其下 —— 不要把它当成文件路径本身。
    """
    base = Path(root) if root else SIGNAL_ROOT
    return (base / "paused").exists()


def armed_state(root=None, env="real", now=None):
    """桥端 ARM 闸门状态。返回 (armed, msg)。"""
    f = Path(root or SIGNAL_ROOT) / env / "armed.txt"
    today = (now or datetime.now()).strftime("%Y%m%d")
    if not f.exists():
        return False, "armed 文件缺失: %s" % f
    try:
        content = f.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        return False, "armed 读取失败: %r" % e
    if today not in content:
        return False, "armed 未含今日 %s" % today
    return True, "armed"


def write_signals(signals, env="real", root=None):
    """写信号到 <root>/<env>/pending/。已存在的 order_id 跳过(幂等)。

    优先复用 prism.trader.write_signals; 项目根不在 sys.path 时内联兜底。
    """
    if not signals:
        return 0
    d = Path(root or SIGNAL_ROOT) / env / "pending"
    d.mkdir(parents=True, exist_ok=True)
    written = 0
    for s in signals:
        p = d / ("%s.json" % s["order_id"])
        if p.exists():
            continue                       # 幂等: 同一 order_id 不重复落盘
        _atomic_write(p, json.dumps(s, ensure_ascii=False, indent=2))
        written += 1
    return written


class TTDaemon:
    """做T守护。依赖可注入(engine/ledger/feed/now_fn)便于离线测试。

    direct 模式: 不写信号文件, 由 executor 直接下单(外部 Python 直连 miniQMT)。
    这是与"信号文件→QMT内桥"并列的第二条通道, 二选一。
    """

    def __init__(self, cfg, ledger=None, engine=None, feed=None,
                 dry_run=None, now_fn=None, runtime_path=None,
                 signal_root=None, executor=None, direct=False,
                 book_dry_run=False):
        self.cfg = cfg
        self.ledger = ledger or Ledger(path=STATE_PATH, now_fn=now_fn)
        self.now_fn = now_fn or datetime.now
        if engine is not None:
            self.engine = engine
        else:
            self.engine = TTEngine(
                cfg, self.ledger,
                feed=feed or market.make_feed(prefer_sample=False,
                                              now_fn=now_fn),
                now_fn=self.now_fn)
        self.dry_run = bool(cfg.get("dry_run", True)) if dry_run is None \
            else bool(dry_run)
        self.runtime_path = Path(runtime_path or RUNTIME_PATH)
        self.signal_root = Path(signal_root or SIGNAL_ROOT)
        self.direct = bool(direct)
        self.executor = executor
        # 演练记账: dry-run 时也推进本地账本, 让"低吸回补"那条腿可见。
        # 只动本地账本, 与下单通道无关; 必须配独立 --state(见 build_daemon 守卫)。
        self.book_dry_run = bool(book_dry_run)
        self.rounds = 0

    # ---------------- 一轮 ----------------

    def run_once(self):
        self.ledger.load()
        self.ledger.roll_if_new_day()
        plan = self.engine.plan()

        paused = is_paused(self.signal_root)
        env = self.cfg.get("env", "real")
        armed, armed_msg = armed_state(self.signal_root, env, self.now_fn())

        signals = plan.get("signals", [])
        written = 0
        booked = 0
        blocked = ""
        exec_results = []
        if self.dry_run:
            blocked = "dry_run"
            if self.direct and signals:
                exec_results = self._exec_direct(signals, dry_run=True)
            if self.book_dry_run and signals:
                # 演练记账: 只推进本地账本(理论成交) + 档位水位, 好让下一轮的
                # 买入腿拿到 net_exposure 额度。仍在 dry_run 分支内 → 无下单可能。
                self._book(signals, plan.get("hhmm", ""))
                booked = len(signals)
        elif paused:
            blocked = "paused(急停开关打开)"
        elif self.direct:
            # 直连通道仍要求 armed —— 保留人工放行闸门, 只是不写文件
            if not armed:
                blocked = "not_armed: %s" % armed_msg
            else:
                exec_results = self._exec_direct(signals, dry_run=False)
                self._book(signals, plan.get("hhmm", ""))
        elif not armed:
            blocked = "not_armed: %s" % armed_msg
        else:
            written = write_signals(signals, env=env, root=self.signal_root)
            self._book(signals, plan.get("hhmm", ""))

        runtime = dict(plan)
        runtime.update({
            "env": env,
            "direct": self.direct,
            "dry_run": self.dry_run,
            "paused": paused,
            "armed": armed,
            "armed_msg": armed_msg,
            "signals_written": written,
            "blocked": blocked,
            "book_dry_run": self.book_dry_run,
            "booked": booked,
            "rounds": self.rounds,
            "runtime_at": self.now_fn().isoformat(timespec="seconds"),
            "ledger": self.ledger.snapshot(),
        })
        if exec_results:
            runtime["exec"] = exec_results
            runtime["exec_stats"] = dict(
                getattr(self.executor, "stats", {}) or {})
            # 真实下单后账户必然变化, 下一轮重查
            if not self.dry_run:
                self.engine.invalidate_account()
        self._write_runtime(runtime)
        self.rounds += 1
        return runtime

    def _exec_direct(self, signals, dry_run):
        """经由 executor 直连下单。未配置 executor → 返回空并告警。"""
        if self.executor is None:
            LOG.error("direct 模式但未注入 executor, 跳过下单")
            return []
        try:
            return self.executor.execute(signals, dry_run=dry_run)
        except Exception as e:              # 单轮异常不终止守护
            LOG.exception("直连下单异常: %r", e)
            return []


    def _book(self, signals, hhmm):
        """按挂单价做理论成交记账 + 推进档位水位。"""
        for s in signals:
            code = s["stock_code"]
            side = s["action"]
            unit = s.get("unit")
            try:
                self.ledger.record_fill(
                    code, side, float(s.get("price") or 0),
                    int(s.get("volume") or 0), hhmm=hhmm,
                    reason=s.get("reason", ""), order_id=s["order_id"])
            except Exception as e:
                LOG.warning("记账失败 %s: %r", s.get("order_id"), e)
                continue
            if unit:
                self.ledger.set_units(code, side, int(unit))
        # 成交后账户事实变化, 下一轮必须重查
        self.engine.invalidate_account()

    def _write_runtime(self, runtime):
        try:
            _atomic_write(self.runtime_path,
                          json.dumps(runtime, ensure_ascii=False, indent=1))
        except OSError as e:
            LOG.warning("写运行时快照失败: %r", e)

    # ---------------- 循环 ----------------

    def run_forever(self, interval=DEFAULT_INTERVAL, max_rounds=None):
        LOG.info("tt daemon 启动 dry_run=%s env=%s interval=%.1fs",
                 self.dry_run, self.cfg.get("env"), interval)
        while True:
            try:
                rt = self.run_once()
                LOG.info("round %d hhmm=%s intents=%d rejected=%d written=%d "
                         "booked=%d %s",
                         rt.get("rounds"), rt.get("hhmm"),
                         rt["counts"]["intents"], rt["counts"]["rejected"],
                         rt.get("signals_written"), rt.get("booked", 0),
                         ("blocked=%s" % rt["blocked"]) if rt.get("blocked") else "")
            except KeyboardInterrupt:
                LOG.info("收到中断, 退出")
                return
            except Exception as e:              # 单轮异常不终止守护
                LOG.exception("轮询异常: %r", e)
            if max_rounds is not None and self.rounds >= max_rounds:
                return
            time.sleep(interval)


def _guard_drill_state(state_arg):
    """`--book-dry-run` 的账本隔离守卫(fail-closed)。

    演练会往账本写"理论成交"(sold_today / 档位水位)。若误用实盘账本, 这些假
    数据会让后续真实运行时 net_exposure 误判(放过本该拦下的买单), 档位水位也
    会错乱。所以强制两条: **必须显式 --state**, 且**不能指向实盘默认账本**。
    """
    if not state_arg:
        raise SystemExit(
            "--book-dry-run 必须配合显式 --state <演练账本路径> 使用。\n"
            "  原因: 演练会往账本写理论成交, 误用实盘账本 %s 会污染\n"
            "        sold_today 与档位水位, 进而放过本该拦下的买单。\n"
            "  例: --state runtime/state/tt_state.drill.json" % STATE_PATH)
    try:
        same = Path(state_arg).resolve() == Path(STATE_PATH).resolve()
    except OSError:
        same = False
    if same:
        raise SystemExit(
            "--book-dry-run 的 --state 不能指向实盘默认账本 %s, 请换一个路径。"
            % STATE_PATH)


def build_daemon(args):
    overrides = {}
    if args.live:
        overrides["dry_run"] = False
    env = getattr(args, "env", None)
    if env:
        overrides["env"] = env
    cfg = tt_config.load(overrides=overrides or None)
    if args.live:
        cfg["dry_run"] = False
    now_fn = (lambda: datetime(2026, 9, 14, 10, 0, 0)) if args.fake_now else None
    feed = market.make_feed(prefer_sample=args.sample, now_fn=now_fn)
    book_dry = bool(getattr(args, "book_dry_run", False))
    drill_runtime = None
    if book_dry:
        _guard_drill_state(args.state)
        # runtime 快照同样隔离 —— 否则演练数据会盖掉面板读的那份
        drill_runtime = str(Path(args.state).with_suffix(".runtime.json"))
    led = Ledger(path=args.state or STATE_PATH, now_fn=now_fn)
    # --sample: 全离线(样本行情 + 纸面账户), 绝不触碰真实账户
    eng = TTEngine(cfg, led, feed=feed, now_fn=now_fn,
                   force_paper=bool(args.sample))
    executor = None
    if getattr(args, "direct", False):
        # 直连执行器: 账户号取配置; --sample 时不给 executor(纯离线)
        if not args.sample:
            from .executor import DirectExecutor
            risk = cfg.get("risk", {})
            executor = DirectExecutor(
                account_id=cfg.get("account_id") or "",
                lot=getattr(eng, "lot", 100),
                max_order_amount=risk.get("max_single_order_amount"),
                now_fn=now_fn)
    return TTDaemon(cfg, ledger=led, engine=eng, dry_run=cfg["dry_run"],
                    now_fn=now_fn, signal_root=args.signal_root,
                    executor=executor, direct=bool(getattr(args, "direct",
                                                           False)),
                    runtime_path=drill_runtime, book_dry_run=book_dry)



# sim 通道的醒目提示: 桥端不校验账户, 必须由人来确认登录的是模拟账户
SIM_NOTICE = (
    "  !! sim 通道: 信号写 D:/QMT_SIGNALS/sim/pending/, 由 QMT 内的\n"
    "     qmt_signal_bridge_demo.py 消费。该桥【不校验账户】—— 它下单用的是\n"
    "     QMT 当前登录的那个账户。启动桥之前务必确认 QMT 登录的是模拟账户,\n"
    "     否则模拟信号会打到真实资金账户上。"
)


def env_banner(cfg, dry_run, direct=False, book_dry_run=False):
    """返回启动横幅(多行)。sim 通道额外给出账户警告。"""
    env = cfg.get("env", "real")
    if direct:
        lines = ["  执行通道: 直连 miniQMT (外部 Python order_stock, 不写信号文件)",
                 "  账户: %s" % (cfg.get("account_id") or "(自动枚举已登录账号)"),
                 "  下发模式: %s" % ("DRY-RUN — 只算不提交" if dry_run
                                     else "LIVE — 会真实报单")]
    else:
        lines = ["  信号通道: %s  (%s/)" % (env, SIGNAL_ROOT / env),
                 "  下发模式: %s" % ("DRY-RUN — 只算不落盘" if dry_run
                                     else "LIVE — 会写信号文件")]
    if env == "sim" and not direct:
        lines.append(SIM_NOTICE)
    if dry_run and book_dry_run:
        lines.append("  !! 演练记账已开: 会推进本地账本(理论成交), 仍然不下单。\n"
                     "     请确认 --state 指向演练专用账本, 而非实盘账本。")
    return "\n".join(lines)


def main(argv=None):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    ap = argparse.ArgumentParser(description="A股做T策略守护(默认演练)")
    ap.add_argument("--live", action="store_true",
                    help="真实写信号(仍需 paused 不存在 + armed 就绪)")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--sample", action="store_true", help="用离线样本行情")
    ap.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    ap.add_argument("--rounds", type=int, default=None, help="最多跑几轮")
    ap.add_argument("--state", default=None, help="状态文件路径")
    ap.add_argument("--signal-root", default=None, help="信号根目录")
    ap.add_argument("--env", choices=("real", "sim"), default=None,
                    help="信号通道: real=实盘(默认) | sim=QMT 模拟通道")
    ap.add_argument("--direct", action="store_true",
                    help="直连模式: 外部 Python 直接下单(不写信号文件桥)")
    ap.add_argument("--fake-now", action="store_true",
                    help="用固定时钟(2026-09-14 10:00)便于演练")
    ap.add_argument("--book-dry-run", action="store_true",
                    help="演练记账: dry-run 也推进本地账本(须配独立 --state), "
                         "让买入腿可见")
    args = ap.parse_args(argv)

    d = build_daemon(args)
    banner = env_banner(d.cfg, d.dry_run, direct=d.direct,
                        book_dry_run=d.book_dry_run)
    if args.once:
        rt = d.run_once()
        print(banner)
        print(json.dumps({k: rt[k] for k in
                          ("ok", "hhmm", "phase", "dry_run", "paused", "armed",
                           "env", "blocked", "signals_written", "booked",
                           "counts")},
                         ensure_ascii=False, indent=2))
        for it in rt.get("intents", []):
            print("  [可执行] %s %s %s %s股 @ %.3f (%s)"
                  % (it["code"], it["name"], it["side"], it["volume"],
                     it["price"], it["reason"]))
        for rj in rt.get("rejected", []):
            print("  [已拦下] %s %s %s股 @ %.3f → %s: %s"
                  % (rj["code"], rj["name"], rj["volume"], rj["price"] or 0,
                     rj["reject_code"], rj["reject_msg"]))
        for ex in rt.get("exec", []):
            print("  [下单] %s %s ok=%s %s"
                  % (ex.get("order_id"), ex.get("code"), ex.get("ok"),
                     ex.get("msg", "")))
        return 0
    print(banner)
    d.run_forever(interval=args.interval, max_rounds=args.rounds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
