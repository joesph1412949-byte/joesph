# -*- coding: utf-8 -*-
"""模拟实盘账户引擎(100万/first_board_v04) — 设计规格 2026-09-01-paper-trading。

安全边界(设计 §8): 本模块绝不写 D:\\QMT_SIGNALS、绝不调用下单接口;
账本 .paper_account.json 原子写, 保存成功才算交易发生; 现金恒>=0、持仓<=max_positions。
数据(行情/K线)只读。"""
import copy
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from shared.common import STATE_DIR
from prism import engine
from prism.engine import load_strategy

STATE_FILENAME = ".paper_account.json"
_DEFAULT_STRATEGY = Path(__file__).parent / "strategies" / "first_board_v04.json"
_REQUIRED_KEYS = ("version", "created", "initial_capital", "cash", "holdings",
                  "trades", "nav_history", "live_nav", "screens_done",
                  "settled_dates")


def _next_weekday(d):
    """下一交易日(仅跳周六日, 不含节假日历——ponytail: 模拟盘可接受)。"""
    from datetime import date, timedelta
    y, m, dd = map(int, d.split("-"))
    cur = date(y, m, dd) + timedelta(days=1)
    while cur.weekday() >= 5:
        cur += timedelta(days=1)
    return cur.isoformat()


def _limit_ratio(code):
    """分板涨停系数回落: 688/300/301→20%, 其余→10%。
    ponytail: 近似——tick 无股票名, 不含 ST(±5%)/北交所(±30%); F1 因子
    (factor_f1_first_board.py)另有 8/4→30% 档但系数是局部变量不可 import。
    真实值以 daemon 注入的 upStopPrice/downStopPrice 为准, 此处仅兜底。"""
    return 0.20 if code.startswith(("300", "301", "688")) else 0.10


def _tick_same_day(t, now):
    """tick 自校验: time 字段(毫秒时间戳, int/float)日期 == now 日期。
    缺失/无法解析/不符 → False(fail-closed, 宁可不买不可幽灵成交):
    假日/非交易时段 QMT 返回昨日完整快照(open/lastClose 齐全), 盲成交
    会按昨日开盘价造幽灵持仓+净值污染; 新直接成交路径无 queue_expire
    兜底, 只能在这里挡。"""
    try:
        ts = float(t.get("time"))
        return datetime.fromtimestamp(ts / 1000.0).date() == now.date()
    except Exception:
        return False


class PaperAccount:
    """模拟账户: 账本 + 买入/卖出执行 + 结算 + 查询。

    执行临界段纪律: 先在内存改, 校验不变量, 再 save(); save 失败 →
    _restore_state 回滚 —— 保存成功才视为交易发生(设计 §5)。"""

    def __init__(self, strategy_path=None, initial_capital=1000000.0,
                 position_ratio=0.3, max_positions=5, fee_rate=0.00025,
                 slippage=0.001, stamp_duty=0.0005, transfer_fee=0.00001,
                 state_path=None):
        self.strategy_path = Path(strategy_path) if strategy_path \
            else _DEFAULT_STRATEGY
        self.initial_capital = float(initial_capital)
        self.position_ratio = float(position_ratio)
        self._position_ratio_ctor = self.position_ratio   # execution.pct 缺失时的回落基线
        self.max_positions = int(max_positions)
        self.fee_rate = float(fee_rate)
        self.slippage = float(slippage)
        self.stamp_duty = float(stamp_duty)
        self.transfer_fee = float(transfer_fee)
        if state_path is not None:
            self.state_path = Path(state_path)
        else:
            self.state_path = STATE_DIR / STATE_FILENAME
        self.state = None
        self._strategy = None

    # ---------- 策略(动态读默认指针, 与实盘同一份 JSON) ----------
    @property
    def strategy(self):
        """策略(动态读默认指针; 指针变化→热重载; 坏文件保留旧策略, spec §10)。"""
        sid = engine.active_strategy_id()
        if self._strategy is None or self._strategy.get("id") != sid:
            try:
                from prism import registry as reg
                reg.scan_factors(force=True)   # 幂等重扫注册因子库(load_strategy 校验依赖)
                self._strategy = load_strategy(
                    engine.STRATEGIES_DIR / ("%s.json" % sid))
                # execution.pct 覆盖默认仓位(spec §4); 无 execution 块回落构造参数
                exec_pct = ((self._strategy or {}).get("execution") or {}) \
                    .get("pct")
                self.position_ratio = (float(exec_pct) if exec_pct
                                       else self._position_ratio_ctor)
            except Exception as e:
                if self._strategy is None:
                    raise            # 首载且失败 → 无退路, 照抛
                # 已有旧策略 → 保留, 不中断交易循环(spec §10); 记日志留痕
                logging.getLogger("prism.paper").warning(
                    "默认策略热重载失败, 保留旧策略 %s: %r", sid, e)
        return self._strategy

    # ---------- 账本 ----------
    def init_account(self, created=None):
        """初始化账本(幂等: 已存在返回现有, 不覆盖)。"""
        if self.load():
            return self.state
        now = datetime.now()
        self.state = {
            "version": 1,
            "created": created or now.strftime("%Y-%m-%d"),
            "initial_capital": self.initial_capital,
            "cash": self.initial_capital,
            "holdings": [],
            "trades": [],
            "nav_history": [],
            "live_nav": self.initial_capital,
            "screens_done": [],
            "settled_dates": [],
            "pending_buys": [],
            "canceled_pending_codes": [],
        }
        self.save()
        return self.state

    def load(self):
        """读账本; 不存在/损坏/结构不符 → False(绝不覆盖原文件)。"""
        if not self.state_path.exists():
            return False
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return False
        if not isinstance(raw, dict) or any(k not in raw
                                            for k in _REQUIRED_KEYS):
            return False
        if raw.get("version") != 1:
            return False
        raw.setdefault("pending_buys", [])
        raw.setdefault("canceled_pending_codes", [])
        self.state = raw
        return True

    def save(self):
        """原子写: 先写 .tmp 再 os.replace; 任何异常向上抛(调用方回滚)。"""
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.state_path)

    # ---------- 执行临界段 ----------
    def _snapshot_state(self):
        return copy.deepcopy(self.state)

    def _restore_state(self, snap):
        self.state = snap

    # ---------- 查询 ----------
    def summary(self):
        if self.state is None and not self.load():
            return {"exists": False}
        st = self.state
        nav = float(st["live_nav"])
        return {
            "exists": True,
            "created": st["created"],
            "cash": round(float(st["cash"]), 2),
            "nav": round(nav, 2),
            "total_return_pct": round((nav / st["initial_capital"] - 1) * 100, 2),
            "holdings_count": len(st["holdings"]),
            "nav_points": len(st["nav_history"]),
            "pending_count": len(self.state.get("pending_buys", [])),
            "updated_at": (st["trades"][-1]["ts"]
                           if st["trades"] else st["created"]),
        }

    def detail(self, trade_limit=50):
        if self.state is None and not self.load():
            return {"exists": False}
        st = self.state
        return {
            "exists": True,
            "holdings": st["holdings"],
            "trades": st["trades"][-trade_limit:][::-1],
            "nav_history": st["nav_history"],
            "pending": list(self.state.get("pending_buys", [])),
        }

    # ---------- 净值估算 ----------
    def _nav_estimate(self, ticks=None):
        """现金 + Σ持仓市值(ticks 有则按实时价, 无则按成本)。"""
        st = self.state
        if ticks:
            nav = st["cash"] + sum(
                h["shares"] * float((ticks.get(h["code"]) or {}).get(
                    "lastPrice") or h["cost"])
                for h in st["holdings"])
        else:
            nav = st["cash"] + sum(h["shares"] * h["cost"]
                                   for h in st["holdings"])
        return max(float(nav), 0.0)

    # ---------- 买入 ----------
    def buy_from_screen(self, provider, now=None, slot=None,
                        tick_provider=None):
        """时点选股(first_board_v04, 同一引擎) + 排板委托创建(改道: 不再直接成交)。

        tick_provider: callable(codes)->{code: tick}, daemon 在时点前拉一次候选池
        盘口注入; 不传 → tick={} → create_pending_buy 保守不建(记 skips
        "排板不通过")。幂等键: slot 显式传入(守护按计划时点补跑, 终审 I-1)优先,
        否则 YYYY-MM-DDTHH:MM(实际分钟, 向后兼容)。键已在 screens_done →
        already_done。选股失败/保存失败 fail-closed(不记账), 报告 error。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return {"error": "未初始化"}
        d = now.strftime("%Y-%m-%d")
        ts_key = slot or "%sT%s" % (d, now.strftime("%H:%M"))
        if ts_key in self.state["screens_done"]:
            return {"candidates": 0, "bought": [], "skipped": [],
                    "env_ok": False, "already_done": True}
        try:
            result = self._screen_candidates(provider)
        except Exception as e:
            snap = self._snapshot_state()
            self.state["screens_done"].append(ts_key)
            try:
                self.save()
            except Exception:
                self._restore_state(snap)
            return {"error": "选股失败: %r" % e}
        env_ok = bool(result.get("environment_ok"))
        bought, skipped = [], []
        if env_ok:
            nav = self._nav_estimate()
            provider_tick = {}
            if tick_provider is not None:
                try:      # 时点前拉一次候选池盘口; 失败保守视为无盘口(下方全部不建)
                    provider_tick = tick_provider(
                        [c.get("code") for c in result.get("candidates", [])]) or {}
                except Exception:
                    provider_tick = {}
            for c in result.get("candidates", []):
                code = c.get("code")
                up = float(c.get("up_stop_price") or 0)
                if not code or up <= 0:
                    skipped.append({"code": code, "reason": "缺涨停价"})
                    continue
                reason = self._buyable(code, nav, now)
                if reason:
                    skipped.append({"code": code, "reason": reason})
                    continue
                if self._one_word_board(code, up, provider):
                    skipped.append({"code": code, "reason": "一字板买不到"})
                    continue
                tick = (provider_tick or {}).get(code) or {}
                p = self.create_pending_buy(code, up, now, tick)
                if p is None:
                    skipped.append({"code": code, "reason": "排板不通过"})
                    continue
                bought.append(p)
                nav = self._nav_estimate()   # 建委托后更新可用净值(冻结口径)
        snap = self._snapshot_state()
        self.state["screens_done"].append(ts_key)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return {"candidates": len(result.get("candidates", [])),
                    "bought": bought, "skipped": skipped, "env_ok": env_ok,
                    "error": "状态保存失败"}
        return {"candidates": len(result.get("candidates", [])),
                "bought": bought, "skipped": skipped, "env_ok": env_ok}

    def _screen_candidates(self, provider):
        """门禁求值 + 引擎选股(pick/盘中排队共用编排)。"""
        from prism.engine import run_screen
        from prism import registry as reg
        market_ctx = provider.build_market_context()
        gate_fids = (self.strategy.get("market_gate") or {}).get("factors", [])
        gate_factors = {}
        for fid in gate_fids:
            try:
                res = reg.get_factor(fid)["func"](market_ctx)
                if isinstance(res, dict):
                    gate_factors[fid] = 1 if res.get("score") else 0
                else:
                    gate_factors[fid] = 1 if res else 0
            except Exception:
                gate_factors[fid] = 0
        limit_ups = provider.get_limit_ups()
        stock_contexts = {}
        for lu in limit_ups:
            try:
                stock_contexts[lu["code"]] = provider.build_stock_context(
                    lu["code"])
            except Exception:
                pass
        result = run_screen(self.strategy, market_ctx,
                            gate_factors=gate_factors,
                            stock_contexts=stock_contexts)
        return result

    def pick_top5_at_close(self, provider, now=None, slot=None):
        """收盘选股(spec §5): 门禁→打分→前 top_n 存 planned_buys(整体替换)。

        幂等键 pickT 前缀(slot 优先, 缺省含日期)与盘中 T 键不冲突;
        门禁不过 → 空计划; 同分按 code 升序决胜; 选股失败 fail-closed。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return {"error": "未初始化"}
        d = now.strftime("%Y-%m-%d")
        ts_key = "pickT%s" % (slot or "%sT%s" % (d, now.strftime("%H:%M")))
        if ts_key in self.state["screens_done"]:
            return {"picked": [], "env_ok": False, "already_done": True}
        top_n = int(((self.strategy.get("execution") or {}).get("top_n"))
                    or 5)
        try:
            result = self._screen_candidates(provider)
        except Exception as e:
            snap = self._snapshot_state()
            self.state["screens_done"].append(ts_key)
            try:
                self.save()
            except Exception:
                self._restore_state(snap)
            return {"error": "选股失败: %r" % e}
        env_ok = bool(result.get("environment_ok"))
        cands = sorted(result.get("candidates", []),
                       key=lambda c: (-float(c.get("scores", {})
                                             .get("composite", 0)),
                                      str(c.get("code"))))
        picked = [{"code": c["code"],
                   "score": float(c.get("scores", {}).get("composite", 0)),
                   "date": d, "for_date": _next_weekday(d)}
                  for c in cands[:top_n if env_ok else 0]]
        snap = self._snapshot_state()
        self.state["planned_buys"] = picked
        self.state["screens_done"].append(ts_key)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return {"error": "状态保存失败"}
        return {"picked": picked, "env_ok": env_ok}

    def execute_open_buys(self, tick_provider, now=None):
        """开盘买入窗口消费(spec §5): 按开盘价买/一字板转排队/跌停跳过。
        仅消费 for_date==今日 的计划; 处理后整体清空。窗口判定在守护侧。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return {"error": "未初始化"}
        today = now.strftime("%Y-%m-%d")
        plans = [p for p in self.state.get("planned_buys", [])
                 if p.get("for_date") == today]
        if not plans:
            return {"bought": [], "queued": [], "skipped": []}
        try:
            ticks = tick_provider([p["code"] for p in plans]) or {}
        except Exception:
            ticks = {}
        bought, queued, skipped = [], [], []
        snap = self._snapshot_state()
        try:
            for p in plans:
                code = p["code"]
                t = ticks.get(code)
                if not t or not t.get("open") or not t.get("lastClose"):
                    skipped.append({"code": code, "reason": "无行情"})
                    continue
                if not _tick_same_day(t, now):   # 陈旧快照守卫(假日幽灵成交)
                    skipped.append({"code": code, "reason": "非当日行情"})
                    continue
                prev = float(t["lastClose"])
                # 涨跌停价: tick 显式字段优先(daemon 注入真实值, 引擎口径=
                # 数据源 UpStopPrice); 缺失 → 分板系数回落(禁全局 ±10%:
                # 20cm/ST 误判会让涨停开盘死价排队或照买, spec §5①)
                up = float(t.get("upStopPrice") or 0)
                low = float(t.get("downStopPrice") or 0)
                if not up:
                    up = round(prev * (1 + _limit_ratio(code)), 2)
                if not low:
                    low = round(prev * (1 - _limit_ratio(code)), 2)
                open_px = float(t["open"])
                nav = self._nav_estimate()
                reason = self._buyable(code, nav, now)
                if reason:
                    skipped.append({"code": code, "reason": reason})
                    continue
                if open_px >= up - 0.001:          # 开盘即板 → 排队替补
                    if self.create_pending_buy(code, up, now, t):
                        queued.append(code)
                    else:
                        skipped.append({"code": code, "reason": "排板不通过"})
                elif open_px <= low + 0.001:       # 跌停开盘 → 保护跳过
                    skipped.append({"code": code, "reason": "跌停开盘"})
                elif self._execute_buy(code, open_px, now, slip=0.0):
                    bought.append(code)
                else:
                    skipped.append({"code": code, "reason": "买入失败"})
            self.state["planned_buys"] = [
                x for x in self.state.get("planned_buys", [])
                if x.get("for_date") != today]
            self.save()
        except Exception:
            self._restore_state(snap)
            return {"error": "开盘买入失败"}
        return {"bought": bought, "queued": queued, "skipped": skipped}

    def _buyable(self, code, nav, now):
        """买入前置判定(排板四道+现金/仓位); None=可排。今日判定用注入 now 的
        日期(终审 M-a, 不直读系统时钟)。"今日已交易"=今日已成交(queue_fill/screen
        执行口径)——开板撤单/收盘失效的未成交流水不计入, 由"今日已撤单"键单独挡。"""
        st = self.state
        today = now.strftime("%Y-%m-%d")
        if any(h["code"] == code for h in st["holdings"]):
            return "已持仓"
        if any(t.get("side") == "buy" and t.get("code") == code
               and t.get("date") == today
               and t.get("reason") in ("screen", "queue_fill")
               for t in st["trades"]):
            return "今日已交易"
        if any(p.get("code") == code for p in st.get("pending_buys", [])):
            return "已在排队中"
        if code in st.get("canceled_pending_codes", []):
            return "今日已撤单"
        if len(st["holdings"]) >= self.max_positions:
            return "仓位已满"
        if st["cash"] < nav * self.position_ratio:
            return "现金不足"
        return None

    def _one_word_board(self, code, up_price, provider):
        """一字板判定: 当日K线 low >= up_price-0.01(全天未开板) → True。"""
        try:
            df = provider.ds.get_kline(code, days=1)
        except Exception:
            return True              # 拿不到K线 → 保守视为买不到
        if df is None or len(df) == 0 or "low" not in getattr(df, "columns", []):
            return True
        low = float(df["low"].iloc[-1])
        return low >= up_price - 0.01

    # ---------- 排板队列状态机(设计 §2/§3) ----------
    def available_cash(self):
        """可用现金 = cash − Σ冻结(排板挂单锁定额)。"""
        return self.state["cash"] - sum(p.get("frozen", 0.0)
                                        for p in self.state.get("pending_buys", []))

    def create_pending_buy(self, code, up_price, now, tick):
        """排板委托: 前置检查(五关+封单+尾盘)→冻结→入 pending。
        tick 缺失返回 None(保守: 无盘口不排)。"""
        if not tick:
            return None
        nav = self._nav_estimate()
        reason = self._buyable(code, nav, now)
        if reason:
            return None
        if now.strftime("%H:%M") >= "14:30":
            return None
        bv = tick.get("bidVol") or [0]
        bid_vol = bv[0] or 0
        # C 守卫: 半残 tick(行情未就绪, lastVolume 缺失/0) → 不建委托,
        # 否则 base_volume=0 会破坏 ΔV 口径(2026-09-04 实测 3 笔中招)。
        base_volume = int(tick.get("lastVolume") or 0)
        if base_volume <= 0:
            return None
        # 量纲(审查 I-1): bidVol[0] 单位=手(xtdata 手数口径) → ×100 折股;
        # 封单金额(元) = 股×价, 门槛 2000 万。
        if bid_vol * 100 * up_price < 20_000_000:
            return None
        # 金额(沿用 _execute_buy 的手数/金额口径: 净值30% → 100股取整)
        amount = nav * self.position_ratio
        shares = int(amount // (up_price * 100)) * 100
        if shares <= 0:
            return None
        frozen = shares * up_price
        if self.available_cash() < frozen:
            return None
        snap = self._snapshot_state()
        self.state["pending_buys"].append({
            "code": code, "shares": shares, "price": up_price,
            "amount": round(frozen, 4), "frozen": round(frozen, 4),
            # queued_shares 单位=股: bidVol[0](手)×100, 与 holdings.shares 同量纲
            "queued_shares": int(bid_vol) * 100,
            # base_volume 单位=手: 与 lastVolume 同口径原样存; check 时手差×100→股
            "base_volume": base_volume,
            "created": now.strftime("%Y-%m-%dT%H:%M:%S"),
            "slot": now.strftime("T%H:%M")})
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return None
        return self.state["pending_buys"][-1]

    def check_pending_buys(self, ticks, now=None):
        """每轮排板判定: 成交/开板撤单/保持。每事件独立临界段。"""
        now = now or datetime.now()
        filled, canceled = [], []
        for p in list(self.state.get("pending_buys", [])):
            t = ticks.get(p["code"])
            if not t:
                continue                      # tick 缺失 → 保持排队
            last = t.get("lastPrice")
            if last is None:
                continue
            if last < p["price"] - 0.001:
                # 路径②(真实打板主成交通道): 炸板时若建单时初始队列已被卖单
                # 吃穿(ΔV ≥ queued+shares), 说明已轮到我们 → 按涨停价成交;
                # 未吃穿 → 真没排到, 撤单(流水记 ΔV 供事后归因)。
                dvol_break = ((int(t.get("lastVolume") or 0)
                               - p["base_volume"]) * 100)
                if dvol_break >= p["queued_shares"] + p["shares"] \
                        and self._execute_fill_buy(p, now):
                    filled.append(p["code"])
                    continue
                self._dispose_pending(p["code"], "queue_cancel_break",
                                      now, dvol=dvol_break)
                canceled.append(p["code"])
                continue
            # dvol 单位=股: lastVolume(手) − base_volume(手) = 手差, ×100 折股,
            # 与 queued_shares(股)+shares(股) 同量纲比较(成交条件才可满足)
            dvol = (int(t.get("lastVolume") or 0) - p["base_volume"]) * 100
            if dvol >= p["queued_shares"] + p["shares"] \
                    and abs(last - p["price"]) <= 0.001:
                if self._execute_fill_buy(p, now):
                    filled.append(p["code"])
        return {"filled": filled, "canceled": canceled}

    def _execute_fill_buy(self, p, now):
        """排板成交记账(临界段): 按挂单价成交, 无上滑; 解冻差额隐含
        (frozen 不扣——成交扣实际金额, pending 移除后冻结自然释放)。"""
        code, shares, price = p["code"], p["shares"], p["price"]
        amount = shares * price
        fee = amount * (self.fee_rate + self.transfer_fee)   # 佣金万2.5+过户万0.1
        snap = self._snapshot_state()
        try:
            st = self.state
            if st["cash"] < amount + fee:
                return False
            nav = self._nav_estimate()
            d = now.strftime("%Y-%m-%d")
            ts = now.strftime("%Y-%m-%dT%H:%M:%S")
            st["cash"] = round(st["cash"] - amount - fee, 4)
            # holdings 字段与既有 _execute_buy 完全对齐(cost/buy_price/entry_nav)
            st["holdings"].append({"code": code, "shares": shares,
                                   "cost": price, "buy_date": d,
                                   "buy_price": price, "entry_nav": nav})
            st["trades"].append({"side": "buy", "code": code, "shares": shares,
                                 "price": price, "fee": round(fee, 4),
                                 "date": d, "reason": "queue_fill", "ts": ts})
            st["pending_buys"] = [x for x in st["pending_buys"]
                                  if x["code"] != code]
            self.save()
            return True
        except Exception:
            self._restore_state(snap)
            return False

    def _dispose_pending(self, code, reason, now, dvol=None):
        """撤单/失效(临界段): 解冻 + 未成交流水 + 幂等键。
        dvol: 撤单时累计成交量(股), 仅开板撤单传入——记入流水供事后归因。"""
        snap = self._snapshot_state()
        try:
            st = self.state
            p = next((x for x in st["pending_buys"] if x["code"] == code), None)
            if p is None:
                return
            st["pending_buys"] = [x for x in st["pending_buys"]
                                  if x["code"] != code]
            entry = {"side": "buy", "code": code,
                     "shares": p["shares"], "price": p["price"],
                     "date": now.strftime("%Y-%m-%d"),
                     "reason": reason,
                     "ts": now.strftime("%Y-%m-%dT%H:%M:%S")}
            if dvol is not None:
                entry["dvol_shares"] = dvol     # 撤单时累计成交量(股), 事后归因
            st["trades"].append(entry)
            if reason == "queue_cancel_break":
                if code not in st["canceled_pending_codes"]:
                    st["canceled_pending_codes"].append(code)
            self.save()
        except Exception:
            self._restore_state(snap)

    def _execute_buy(self, code, up_price, now=None, slip=None):
        """买入执行(临界段: 内存改→不变量校验→save, 失败回滚)。"""
        now = now or datetime.now()
        snap = self._snapshot_state()
        nav = self._nav_estimate()
        target = nav * self.position_ratio
        buy_price = round(up_price * (1 + (self.slippage if slip is None else slip)), 4)
        shares = int(target / buy_price / 100) * 100
        if shares <= 0:
            return None
        amount = round(shares * buy_price, 2)
        fee = round(amount * (self.fee_rate + self.transfer_fee), 2)
        cash_after = round(self.state["cash"] - amount - fee, 2)
        if cash_after < 0:
            return None
        d = now.strftime("%Y-%m-%d")
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        self.state["cash"] = cash_after
        self.state["holdings"].append({
            "code": code, "shares": shares, "cost": buy_price,
            "buy_date": d, "buy_price": buy_price, "entry_nav": nav})
        self.state["trades"].append({
            "ts": ts, "date": d, "side": "buy", "code": code,
            "price": buy_price, "shares": shares, "amount": amount,
            "fee": fee, "reason": "screen", "cash_after": cash_after})
        self.state["live_nav"] = round(self._nav_estimate(), 2)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return None
        return {"code": code, "shares": shares, "price": buy_price,
                "amount": amount, "fee": fee}

    # ---------- 卖出 ----------
    def sell_check(self, ticks, now=None):
        """tick 监控: 止盈/止损触发卖出; T+1(当日买入不卖); 同步 live_nav。

        ticks: {code: tick_dict}; 缺该股 tick 或价格<=0 → 跳过。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return []
        d = now.strftime("%Y-%m-%d")
        rules = self.strategy.get("sell_rules") or {}
        # 注: 策略/回测中 sell_rules 以小数存储(0.08/0.05, 同 backtest.py),
        # 直接读小数; 缺省 8%/5% —— 简报蓝图误除以 100, 按测试意图(8%/5%)修正。
        tp = float(rules.get("take_profit_pct") or 0.08)
        sl = float(rules.get("stop_loss_pct") or 0.05)
        out = []
        for h in list(self.state["holdings"]):
            if h["buy_date"] >= d:          # T+1: 当日买入不卖
                continue
            t = (ticks or {}).get(h["code"]) or {}
            price = float(t.get("lastPrice") or 0)
            if price <= 0:
                continue
            # 跌停日卖不出(设计 §4, 顺延次日): 现价 ≤ 昨收×(1-幅度); 幅度
            # 30/68 前缀 20%, 其余 10%(ST 不特殊, §9 披露); 昨收缺失 →
            # 不判照常卖(fail-open)。跳过=持仓保留, 止盈止损次日再判。
            lc = float(t.get("lastClose") or 0)
            if lc > 0:
                ratio = 0.20 if h["code"].startswith(("30", "68")) else 0.10
                if price <= lc * (1 - ratio) + 0.001:
                    continue
            cost = float(h["cost"])
            if price >= cost * (1 + tp):
                reason = "take_profit"
            elif price <= cost * (1 - sl):
                reason = "stop_loss"
            else:
                continue
            done = self._execute_sell(h["code"], price, reason, now=now)
            if done:
                out.append(done)
        snap = self._snapshot_state()
        self.state["live_nav"] = round(self._nav_estimate(ticks), 2)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
        return out

    def _execute_sell(self, code, price, reason, now=None):
        """卖出执行(临界段): 成交价=price×(1-slippage), 印花税仅卖出侧。"""
        now = now or datetime.now()
        snap = self._snapshot_state()
        idx = next((i for i, h in enumerate(self.state["holdings"])
                    if h["code"] == code), None)
        if idx is None:
            return None
        h = self.state["holdings"][idx]
        sell_price = round(price * (1 - self.slippage), 4)
        amount = round(h["shares"] * sell_price, 2)
        fee = round(amount * (self.fee_rate + self.stamp_duty
                              + self.transfer_fee), 2)
        cash_after = round(self.state["cash"] + amount - fee, 2)
        d = now.strftime("%Y-%m-%d")
        ts = now.strftime("%Y-%m-%dT%H:%M:%S")
        self.state["cash"] = cash_after
        self.state["holdings"].pop(idx)
        self.state["trades"].append({
            "ts": ts, "date": d, "side": "sell", "code": code,
            "price": sell_price, "shares": h["shares"], "amount": amount,
            "fee": fee, "reason": reason, "cash_after": cash_after})
        self.state["live_nav"] = round(self._nav_estimate(), 2)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return None
        return {"code": code, "price": sell_price, "shares": h["shares"],
                "amount": amount, "fee": fee, "reason": reason}

    # ---------- 盘后结算 ----------
    @staticmethod
    def _n8(s):
        """日期归一化: 去横线("2026-09-01"→"20260901"), 与K线索引同口径比较。"""
        return str(s).replace("-", "")

    def _kline_day_strs(self, df):
        """K线逐日日期(YYYYMMDD 列表, 与 df 行序一致)。

        优先 df["time"] 列(epoch 毫秒→本地日期, 与 zt_history._kline_dates
        同款语义); 无 time 列才用 index 兜底且只保留 8 位纯数字项。
        真实数据源(xtdata.get_market_data_ex)新 schema: df 可能以 time 列
        承载日期、index 为 RangeIndex/整数 —— 直接 str(index) 产出非日期串,
        到期判定永假(持仓静默永不出场)。坏值行跳过, 不毒化整段提取。"""
        out = []
        if "time" in getattr(df, "columns", []):
            for t in df["time"]:
                try:
                    out.append(datetime.fromtimestamp(
                        int(t) / 1000.0).strftime("%Y%m%d"))
                except Exception:
                    continue
            return out
        for ix in df.index:
            s = self._n8(ix)
            if len(s) == 8 and s.isdigit():
                out.append(s)
        return out

    def max_hold_days(self):
        """策略最大持有交易日(sell_rules.max_hold_days, 缺省 5)。"""
        rules = self.strategy.get("sell_rules") or {}
        return int(float(rules.get("max_hold_days") or 5))

    def _due_by_kline(self, code, buy_date, provider):
        """到期判定(兜底口径): 持仓股K线中 buy_date 之后的交易日数 >= max_hold_days。

        注(终审 I-3): 自然日为主口径(与回测/实盘 ExitRule 一致, 见
        PaperDaemon._due_natural); K线 bar 数仅作数据缺失兜底, 不再被
        daemon 默认路径使用。"""
        try:
            df = provider.ds.get_kline(code, days=15)
            days = self._kline_day_strs(df)
        except Exception:
            return False
        if df is None or len(df) == 0:
            return False
        b = self._n8(buy_date)
        after = [s for s in days if s > b]
        return len(after) >= self.max_hold_days()

    def _nav_at(self, close_fn, d):
        """收盘盯市净值(结算/补算共用): 现金 + Σ持仓×(收盘价, 缺价回退成本)。"""
        return self.state["cash"] + sum(
            h["shares"] * float(close_fn(h["code"], d) or h["cost"])
            for h in self.state["holdings"])

    def settle_day(self, close_fn, due_fn=None, provider=None, now=None):
        """盘后结算: 到期持仓按收盘价卖出 + 当日净值盯市定格 + 幂等。

        close_fn(code, day=None) -> float|None; due_fn(code, buy_date) -> bool
        (缺省 _due_by_kline); 拿不到收盘价的到期持仓保留(下个交易日再结)。"""
        now = now or datetime.now()
        d = now.strftime("%Y-%m-%d")
        if self.state is None and not self.load():
            return {"error": "未初始化"}
        if d in self.state["settled_dates"]:
            return {"already_done": True}
        closed = []
        for h in list(self.state["holdings"]):
            due = due_fn(h["code"], h["buy_date"]) if due_fn \
                else self._due_by_kline(h["code"], h["buy_date"], provider)
            if not due:
                continue
            px = close_fn(h["code"], d)
            if not px or px <= 0:
                continue
            done = self._execute_sell(h["code"], px, "hold_expire", now=now)
            if done:
                closed.append(done)
        snap = self._snapshot_state()
        nav = self._nav_at(close_fn, d)
        self.state["live_nav"] = round(nav, 2)
        self.state["nav_history"].append({"date": d, "nav": round(nav, 2)})
        self.state["settled_dates"].append(d)
        try:
            self.save()
        except Exception:
            self._restore_state(snap)
            return {"error": "状态保存失败"}
        return {"closed": closed, "nav": round(nav, 2)}

    def backfill_nav(self, close_fn, trade_days, now=None):
        """缺口日补算: nav_history 末日后、<=今日的交易日逐日盯市。

        close_fn(code, day) -> float|None(该日该股收盘价); 未来日跳过;
        今日补算时同时记入 settled_dates(幂等防重复结算)。"""
        now = now or datetime.now()
        if self.state is None and not self.load():
            return 0
        last = (self.state["nav_history"][-1]["date"]
                if self.state["nav_history"] else None)
        today = now.strftime("%Y-%m-%d")
        snap = self._snapshot_state()
        filled = 0
        try:
            for d in trade_days:
                if d < self.state["created"]:   # M-b: 不补建账日之前
                    continue
                if last and d <= last:
                    continue
                if d > today:
                    continue
                nav = self._nav_at(close_fn, d)
                self.state["nav_history"].append(
                    {"date": d, "nav": round(nav, 2)})
                if d == today and d not in self.state["settled_dates"]:
                    self.state["settled_dates"].append(d)
                filled += 1
            if filled:
                self.save()
        except Exception:
            self._restore_state(snap)
            return 0
        return filled


# ---------- CLI ----------
def main(argv=None, account=None):
    """CLI: --init 初始化(幂等) / --summary 摘要JSON / --once 单轮(需QMT在线)。"""
    import argparse
    ap = argparse.ArgumentParser(description="模拟实盘账户(100万/first_board_v04)")
    ap.add_argument("--once", action="store_true",
                    help="连 QMT 跑一轮当前时点逻辑(选股/监控/结算)")
    ap.add_argument("--init", action="store_true", help="初始化账户(幂等)")
    ap.add_argument("--summary", action="store_true", help="打印账户摘要")
    a = ap.parse_args(argv)
    acc = account or PaperAccount()
    if a.init:
        acc.init_account()
        print("账户已初始化/存在: %s (初始资金 %.0f)" %
              (acc.state_path, acc.initial_capital))
        return
    if a.summary:
        print(json.dumps(acc.summary(), ensure_ascii=False, indent=1))
        return
    if a.once:
        from prism.paper_daemon import PaperDaemon
        d = PaperDaemon(acc)
        if not d.connect_provider(max_retry=10, retry_wait=5):
            print("QMT 连接失败(需盘中在线)")
            return
        try:
            print(json.dumps(d.tick_once(), ensure_ascii=False, indent=1,
                             default=str))
        except Exception as e:                  # M-g: 单轮异常不裸 traceback
            print("单轮执行失败: %r" % e)
        return
    ap.print_help()


if __name__ == "__main__":
    main()
