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
                移除、can_use_volume 回写、**券商有账本无的持仓保守采纳**(账本被
                抹/JSON 损坏时自愈, 否则该持仓止盈止损永久失效); 只有拿到券商
                持仓事实(None ≠ {})才动账本, 账本绝不被写成"查询失败=空仓"。
  #6 幂等       确定性 order_id(BUY_<日期>_<代码> / SELL_<日期>_<代码>),
                重启重跑不产生新文件; 桥端 per-day dedup 兜底。
  #8 T+1        当日买入不卖: exit_rules enforce_t1 + 券商 can_use_volume
                双保险; 跌停日顺延(exit_rules.is_limit_down)。
  #9 开盘闸门   发单前判跌停: **真实跌停价优先**(ds.get_instrument 的
                 DownStopPrice, 取法同 prism/paper_daemon.py::_open_buy_ticks
                 —— 唯一能识别 ST ±5% 的口径; 分板系数只按代码前缀分板, 没有
                 ST 档, 会把主板 ST 的 −5% 跌停算成 −10% 而漏判), 拿不到才回落
                 昨收 × (1−分板幅度)(权威 shared.common)。**跌停开盘/窗口内砸到
                 跌停 → 跳过该只**(否则挂在涨停价上的限价买单会立即成交在跌停板;
                 判据同 prism/paper.py::execute_open_buys)。
                 **行情缺失(前收/价格缺) → 跳过该只(fail-closed)**: 与
                 paper.execute_open_buys 的"无行情 = skip"同口径 —— 同一策略的
                 两条执行路径必须同口径, 且买入侧错发(可能成交在跌停板)比漏发贵。

安全边界(与 prism/paper_daemon.py §8 同款):
  - **默认 dry_run=True**: 只记录/日志, 零副作用; 显式 --live 才落信号;
  - 本进程只写信号文件, **绝不调用任何下单接口**(下单在 QMT 桥端);
  - 行情/账户查询全 fail-open(不抛异常), 但**决策 fail-closed**: 拿不到账户
    事实 → 不建仓/不对账/不卖, 绝不拿"不知道"当"没有";
  - 每轮开头查 trader.check_paused()(D:/QMT_SIGNALS/paused), 急停时本进程
    自己也不落 BUY/SELL;
  - 真实盘最终闸门在桥端 armed.txt(当日日期), 本进程无法绕过。

用法:
    python -m prism.live_daemon                 # 演练(默认, 不落信号)
    python -m prism.live_daemon --live          # 真实写信号(需桥端已 armed)
    python -m prism.live_daemon --once          # 只跑一轮并打印结果
"""
import argparse
import json
import logging
from datetime import date, datetime
from pathlib import Path

from shared.common import STATE_DIR, atomic_write, limit_ratio_for_code, next_weekday
from shared.exit_rules import PositionBook, is_limit_down
from prism import schedule, trader
from prism.live_account import LiveAccount, _num, calc_buy_volume

LOG = logging.getLogger("live_daemon")

STATE_FILE = STATE_DIR / "live_state.json"
POSITIONS_FILE = STATE_DIR / "positions.json"

# 调度时点常量与时段/连接/退避共用 paper_daemon(prism/schedule.py)
PICK_SLOT = schedule.PICK_SLOT    # 收盘选股时点(策略 execution.pick_slot)
OPEN_WINDOW = schedule.OPEN_WINDOW  # 次日开盘买入窗口(execution.open_window)
SETTLE_AFTER = schedule.SETTLE_AFTER
POLL_SECONDS = 30                 # 实盘轮询间隔(比模拟盘宽: 有券商查询开销)
SYNC_INTERVAL = 60                # 券商对账节流(秒)
STATE_VERSION = 1
DEFAULT_TAKE_PROFIT = 0.15        # 策略无 sell_rules 时的实盘缺省(同 full_factor_v1)
DEFAULT_STOP_LOSS = 0.05
DEFAULT_MAX_HOLD = 5


def _code_tag(code):
    """order_id 里用的代码片段(去点/去横线)。"""
    return str(code).replace(".", "").replace("-", "").upper()


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
        self._down_stop_day = ""      # 真实跌停价的当日缓存(见 _down_stop_price)
        self._down_stop_cache = {}

    # ---------------- 策略(动态读默认指针, 与模拟盘/回测同一份 JSON) ----------------
    def _resolve_strategy(self):
        if isinstance(self.strategy, dict):
            return self.strategy
        from prism import engine
        sid = self.strategy or engine.active_strategy_id()
        if self._strat_cache is not None \
                and self._strat_cache.get("id") == sid:
            return self._strat_cache
        self._strat_cache = engine.resolve_strategy(sid)
        # execution 块优先(策略 spec 单一事实源), 缺省回落构造参数
        pct, top_n = engine.execution_sizing(self._strat_cache)
        if pct and self._position_ratio_ctor is None:
            self.position_ratio = pct
        if top_n and self._max_positions_ctor is None:
            self.max_positions = top_n
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
        atomic_write(self.state_path,
                     json.dumps(st, ensure_ascii=False, indent=2))

    def _load_positions(self):
        if self.positions_path.exists():
            try:
                data = json.loads(self.positions_path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("顶层不是对象")
                return PositionBook.from_json(data)
            except Exception as e:
                # 读失败不静默当空书(否则真实持仓被当"无持仓", 卖出永久空转);
                # 也不在此覆盖文件 —— 留痕交人工核对, 券商对账(R2)会自愈
                LOG.warning("positions.json 读取/解析失败(%r) → 本轮按空账本处理; "
                            "文件保持原样, 请人工核对", e)
        return PositionBook()

    def _save_positions(self, book):
        atomic_write(self.positions_path,
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
    def _sync_positions(self, today=None):
        """用券商持仓校正本地账本。返回 (变更数, 链路是否可信)。

        只有拿到券商持仓事实才动账本: asset()/positions() 拿不到(或抛异常) →
        (0, False), 账本一字不动 —— 防"查询失败 → 误判空仓 → 抹掉账本 + 空账本
        落盘"(positions() 为 None 是"拿不到", {} 才是"券商确认空仓")。
        反向也要自愈: 券商有、账本无(账本被抹/JSON 损坏) → 保守采纳, 否则该持仓
        的止盈止损通道永久失效。"""
        if self.account is None:
            return 0, False
        try:
            if self.account.asset() is None:
                return 0, False
            pos = self.account.positions()
        except Exception as e:
            LOG.warning("券商持仓查询异常 → 本轮不对账, 账本保持原样: %r", e)
            return 0, False
        if pos is None:
            LOG.warning("券商持仓查询失败(拿不到账户事实) → 本轮不对账, 账本保持原样")
            return 0, False
        book = self._load_positions()
        changed = 0
        for code in list(book.all()):
            if code not in pos:          # 券商已无持仓 → 已了结/已卖出 → 移除
                book.remove(code)
                changed += 1
        for code, p in pos.items():
            cur = book.get(code)
            if cur is not None:
                if book.update(code, can_use_volume=int(
                        p.get("can_use_volume") or 0)):
                    changed += 1
                continue
            # 券商有、账本无 → 保守采纳(buy_date 取今日保证 T+1 安全, 不会当天就卖)
            open_price = _num(p.get("open_price"), 0)
            if open_price <= 0:
                LOG.warning("对账: 券商持仓 %s 拿不到成本价(open_price=%r) → "
                            "跳过采纳, 请人工核对", code, p.get("open_price"))
                continue
            adopt_day = today or date.today()
            book.add(code, code, buy_price=open_price, buy_date=adopt_day,
                     volume=int(_num(p.get("volume"), 0)),
                     can_use_volume=int(_num(p.get("can_use_volume"), 0)))
            changed += 1
            LOG.warning("对账: 账本缺失 %s → 已按券商事实采纳(成本 %.2f, 数量 %s, "
                        "买入日按 %s 保证 T+1 安全)", code, open_price,
                        p.get("volume"), adopt_day)
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
            out["sync"] = self._sync_positions(today=now.date())[0]
        except Exception as e:
            LOG.warning("对账失败(忽略): %r", e)

    # ---------------- 收盘选股(gap #1/#2) ----------------
    def _do_close_pick(self, now, st, out):
        """15:05 收盘选股 → 落次日计划。

        返回值 = **本次选股是否真正跑完**: True 才让调用方登记当日 pick_slots。
        - True: 选股跑完, 无论有无候选(门禁不过/今天没票、仓位已满都是合法结果);
        - False: 账户/现金事实缺失, 或 run_daily 带 error(全缺价拒单)/paused
          —— 这些是**瞬时失败**, 登记 slot 等于把"不知道"当"今天没票",
          QMT 抖一下当天零建仓、次日无计划, 日志还与真没票不可区分。
        失败分支不登记 ⇒ 下一轮(POLL_SECONDS)自然重试。

        F1 与 paper 侧同口径(prism/paper.py pick_top5_at_close): 只有成功的
        选股才写当日幂等键(screens_done/pickT ↔ pick_slots/pickT)。
        """
        res = self.screen_fn(now) or {}
        sigs = res.get("signals") or []
        out["screen"] = {"environment_ok": res.get("environment_ok"),
                         "candidates": len(res.get("candidates") or []),
                         "signals": len(sigs),
                         "paused": res.get("paused"),
                         "error": res.get("error")}
        err = res.get("error")
        if err or res.get("paused"):
            LOG.warning("收盘选股未完成(error=%r paused=%r) → 不占当日 slot, "
                        "约 %d 秒后自动重试", err, res.get("paused"), POLL_SECONDS)
            return False
        if not sigs:
            LOG.info("收盘选股: 无信号(env_ok=%s) → 合法结果, 当日 slot 已登记",
                     res.get("environment_ok"))
            return True
        asset = self.account.total_asset() if self.account else None
        if asset is None:
            out["skipped"].append("asset_unavailable")
            LOG.warning("收盘选股: 拿不到账户总资产 → 本日不建仓(fail-closed), "
                        "不占当日 slot, 约 %d 秒后重试", POLL_SECONDS)
            return False
        cash = self.account.available_cash() if self.account else None
        if cash is None:
            out["skipped"].append("cash_unavailable")
            LOG.warning("收盘选股: 拿不到可用资金 → 本日不建仓(fail-closed); "
                        "只按总资产算股数会下出付不起的单; 不占当日 slot, "
                        "约 %d 秒后重试", POLL_SECONDS)
            return False
        # 建仓预算: 总资产只是上限, 真能掏出来的钱是可用现金; 逐只扣减
        budget = min(asset, cash)
        book = self._load_positions()
        slots = self.max_positions - len(book.all())
        if slots <= 0:
            out["skipped"].append("max_positions")
            LOG.info("收盘选股: 持仓已满(%d 只) → 不建仓(合法结果, slot 已登记)",
                     self.max_positions)
            return True
        for_date = next_weekday(now.date()).isoformat()
        plans = []
        for s in sigs:
            price = float(s.get("price") or 0)
            vol = calc_buy_volume(price, budget, self.position_ratio)
            if vol <= 0:
                out["skipped"].append(s.get("stock_code"))
                continue
            budget -= vol * price
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
        LOG.info("收盘选股: 计划 %d 只(预算 %.0f = min(总资产 %.0f, 可用现金 %.0f), "
                 "单只 %.0f%%, 上限 %d 只) → %s 发单",
                 len(plans), min(asset, cash), asset, cash,
                 self.position_ratio * 100, self.max_positions, for_date)
        return True

    # ---------------- 次日开盘发单 ----------------
    def _down_stop_price(self, code, day):
        """真实跌停价(数据源 ds.get_instrument 的 DownStopPrice); 拿不到 → 0。

        与 prism/paper_daemon.py::_open_buy_ticks 同一条路径、同一字段(见
        prism/data.py:149 的取法)。为什么不用分板系数就够: 分板系数只按代码
        前缀分板, 没有(也无法有)ST 档 —— 主板 ST 的 −5% 会被算成 −10%,
        开盘跌停买不到闸门。当日成功值缓存: 开盘窗口 POLL_SECONDS(30s)一轮,
        每只只查一次(≤ 计划只数次/日); 失败**不**缓存(QMT 抖一下不许把当天
        钉死在这个口径上, 下一轮还会重试)。
        """
        code = str(code)
        if self._down_stop_day != day:
            self._down_stop_day, self._down_stop_cache = day, {}
        if code in self._down_stop_cache:
            return self._down_stop_cache[code]
        ds = getattr(self.provider, "ds", None)
        if ds is None:
            return 0.0
        try:
            det = ds.get_instrument(code) or {}
        except Exception as e:
            LOG.warning("开盘发单: %s 真实跌停价查询失败(%r) → 回落到分板系数",
                        code, e)
            return 0.0
        px = _num(det.get("DownStopPrice"))
        if px > 0:
            self._down_stop_cache[code] = px
        return px

    def _do_open_send(self, now, st, out):
        d = now.strftime("%Y-%m-%d")
        plans = [p for p in st["plans"] if p.get("for_date") == d]
        if not plans:
            return
        ticks = self._ticks([p["code"] for p in plans])
        ready = []
        for p in plans:
            code = p["code"]
            t = ticks.get(code) or {}
            prev = _num(t.get("lastClose"))        # 跌停价的唯一基准
            px = min([x for x in (_num(t.get("open")), _num(t.get("lastPrice")))
                      if x > 0], default=0.0)
            if prev <= 0 or px <= 0:
                # 行情缺失 → 跳过该只(fail-closed)。口径依据: 同一策略的两条
                # 执行路径必须同口径 —— prism/paper.py::execute_open_buys 对
                # "无行情"就是 skip; 且买入侧**错发**(挂单价可能成交在跌停板/
                # 涨停板上)远比漏发贵。计划当日消费(与 paper 同款: 不在窗口内
                # 30 秒一轮反复重试一只没有行情的票)。
                out["skipped"].append("no_quote:%s" % code)
                LOG.warning("开盘发单: %s 拿不到行情(前收/价格缺) → 跳过"
                            "(fail-closed, 今日不买)", code)
                continue
            # 跌停价: tick 自带真实值 → 用它; 否则查数据源注入(同 paper_daemon);
            # 都拿不到才回落到分板系数(近似口径: 无股票名 ⇒ 不含 ST ±5%)
            low = _num(t.get("downStopPrice")) \
                or self._down_stop_price(code, d) \
                or round(prev * (1 - limit_ratio_for_code(code)), 2)
            if px <= low + 0.001:
                # 跌停开盘(或窗口内砸到跌停) → 保护跳过: 挂在涨停价上的限价
                # 买单会立即成交在跌停板(判据同 paper.execute_open_buys)。
                out["skipped"].append("limit_down_open:%s" % code)
                LOG.warning("开盘发单: %s 开盘/现价 %.2f ≤ 跌停价 %.2f "
                            "→ 跳过(今日不买)", code, px, low)
                continue
            ready.append(p)
        sigs = [trader.build_signal(
            code=p["code"], action="BUY", price=p["price"],
            volume=p["volume"],
            order_id="BUY_%s_%s" % (d.replace("-", ""), _code_tag(p["code"])),
            strategy_id=p.get("strategy_id") or "live",
            composite=p.get("composite")) for p in ready]
        n = self._emit(sigs, "buys", out)
        if self.dry_run:
            LOG.info("开盘发单(DRY-RUN): 将发 %d 只, 未落盘", len(sigs))
            return
        if ready:
            book = self._load_positions()
            for p in ready:
                book.add(p["code"], p.get("name") or p["code"],
                         buy_price=p["price"], buy_date=d, volume=p["volume"])
            self._save_positions(book)
        # 跌停跳过的计划一并消费(今日不买, 与 paper 的 "跌停开盘" skip 同款)
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
            can_use = self.account.can_use_map() if self.account else None
        except Exception as e:
            LOG.warning("券商可卖量查询失败 → 本轮退回账本记录的可卖量: %r", e)
            can_use = None
        tp, sl, hold = self._sell_rules()
        # 传 {} 而非 None: 让 evaluate_all 在券商拿不到该股时退回账本 can_use_volume
        # (传 None 会完全不按可卖量过滤, 连账本记的 0 也忽略)
        hits = book.evaluate_all(last, can_use=can_use or {},
                                 enforce_t1=True, today=now.date(),
                                 take_profit_pct=tp, stop_loss_pct=sl,
                                 max_hold_days=hold)
        sigs = []
        for code, pos, action, reason in hits:
            if is_limit_down(code, last.get(code), last_close.get(code)):
                out["skipped"].append("limit_down:%s" % code)
                continue
            avail = (can_use or {}).get(code)      # 券商事实优先
            if avail is None:
                avail = pos.get("can_use_volume")  # 退回账本记的可卖量
            if avail is None:
                # 两条来源都没有 → 本轮不卖(fail-closed; 按 volume 全量硬卖会出废单)
                out["skipped"].append("no_can_use:%s" % code)
                LOG.warning("卖出巡检: %s 拿不到可卖量(券商/账本皆无) → 本轮跳过",
                            code)
                continue
            vol = int(avail)
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
        """交易日时段(与模拟盘同一份判定, prism/schedule.py)。"""
        return schedule.in_session(now)

    def tick_once(self, now=None):
        now = now or (self.now_fn() if self.now_fn else datetime.now())
        out = {"action": "idle", "planned": [], "buys": [], "sells": [],
               "skipped": [], "sync": 0, "screen": None}
        # 急停开关最先判定: 早于任何分支/状态变更/计划消费/信号生成。
        # 本进程自己 build_signal + 落 pending(BUY 和 SELL 都落), 不查急停就绕过了它
        if trader.check_paused():
            out["action"] = "paused"
            LOG.warning("急停开关存在(%s) → 本轮不生成任何信号, 不消费计划",
                        trader.PAUSE_FILE)
            return out
        d = now.strftime("%Y-%m-%d")
        hm = now.strftime("%H:%M")
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
                # 只有选股真正跑完才登记 slot(F1): 账户/现金查询失败或
                # run_daily 带 error 时不登记, 下一轮真重试(与 paper 侧同口径)
                if self._do_close_pick(now, st, out):
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
        schedule.sleep(sec, self.sleep_fn)

    def connect_provider(self, max_retry=60, retry_wait=10):
        """连接 QMT 行情数据源(与 paper_daemon 同一份重试, prism/schedule.py)。"""
        return schedule.connect_provider(self, max_retry, retry_wait)

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
