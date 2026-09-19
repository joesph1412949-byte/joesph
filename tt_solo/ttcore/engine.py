# -*- coding: utf-8 -*-
"""做T策略编排: 行情 + 网格 + 账本 + 风控 → 可执行意图(Intent)。

一轮 plan() 的职责边界:
  - **只算不落盘**: 产出 intents/signals 列表返回给调用方, 自己不写信号文件、
    不记账。落盘与记账由 daemon 在"真的发出去了"之后做, 保证幂等与可回放。
  - **每个标的独立降级**: 某只票行情缺失/被熔断, 不影响其余标的的判定。
  - **意图幂等**: order_id 由 (日期, 代码, 方向, 档位) 确定性推导, 重启不会
    重复下单(与 prism/live_daemon.py 的惯例一致)。

T+1 的两道保险:
  1. 账本层 net exposure —— 默认"买回不超过已卖出"(日内净持仓只减不增);
  2. 券商层 can_use_volume —— 卖出量硬约束在当日可卖量之内。
  两道同时满足才算合规, 任一道拦下即跳过该笔。
"""
from collections import namedtuple
from datetime import datetime
from pathlib import Path

from . import config as tt_config
from . import grid
from . import market
from .risk import RiskGate

Intent = namedtuple("Intent", [
    "code", "name", "side", "price", "volume", "reason",
    "ok", "reject_code", "reject_msg", "order_id", "meta",
])

# 与 qmt_signal_bridge_real.py / prism.trader.build_signal 同构的字段集
SIGNAL_KEYS = ("order_id", "action", "stock_code", "order_type", "price",
               "volume", "account_id", "created_at", "status", "strategy_id")


def _now_iso(now=None):
    return (now or datetime.now()).isoformat(timespec="seconds")


def _code_tag(code):
    return str(code).replace(".", "").replace("-", "").upper()


def _floor_lot(value, price, lot=100):
    """按金额与单价算整手股数。任何非法输入 → 0。"""
    try:
        value, price, lot = float(value), float(price), int(lot)
    except (TypeError, ValueError):
        return 0
    if value <= 0 or price <= 0 or lot <= 0:
        return 0
    return int(value / price // lot * lot)


class TTEngine:
    """做T决策引擎。依赖全部可注入(feed/account/now_fn), 便于离线测试。"""

    def __init__(self, cfg, ledger, feed=None, account=None, now_fn=None,
                 gate=None, lot=100, force_paper=False):
        self.cfg = cfg
        self.ledger = ledger
        self.feed = feed if feed is not None else market.make_feed()
        self.account = account
        self.now_fn = now_fn or datetime.now
        self.gate = gate or RiskGate(cfg.get("risk"))
        self.lot = int(lot or 100)
        self.grid_cfg = cfg.get("grid", {})
        self.session_cfg = cfg.get("session", {})
        # force_paper: 完全不碰真实账户(离线演示/测试), 与 --sample 配套
        self.force_paper = bool(force_paper)
        self._acct = None

    # ------------------------------------------------------------ 账户

    def account_state(self):
        """账户快照。取不到 → 纸面推演(paper), 并标注 source。

        返回 {'ok','source','total_asset','cash','positions','can_use'}
        """
        if self._acct is not None:
            return self._acct
        if not self.force_paper:
            real = self._real_account_state()
            if real:
                self._acct = real
                return real
        paper = float(self.cfg.get("paper_total_asset") or 0)
        pp = self.cfg.get("paper_positions") or {}
        positions = {c: {"volume": int(v), "can_use_volume": int(v),
                         "market_value": 0.0}
                     for c, v in pp.items() if int(v) > 0}
        self._acct = {
            "ok": paper > 0, "source": "paper", "total_asset": paper,
            "cash": paper, "positions": positions,
            "can_use": {c: int(v) for c, v in pp.items() if int(v) > 0},
            "note": "账户不可用, 按纸面资产/底仓推演(不产生真实资金校验)",
        }
        return self._acct

    def _real_account_state(self):
        acc = self.account
        if acc is None:
            try:
                from .broker import LiveAccount
                acc = LiveAccount(account_id=self.cfg.get("account_id") or "")
                self.account = acc
            except Exception:
                return None
        try:
            asset = acc.asset()
            if not asset:
                return None
            positions = acc.positions() or {}
            total = float(asset.get("total_asset") or 0)
            if total <= 0:
                return None
            return {
                "ok": True, "source": "qmt", "total_asset": total,
                "cash": float(asset.get("cash") or 0),
                "market_value": float(asset.get("market_value") or 0),
                "positions": positions,
                "can_use": {c: int((p or {}).get("can_use_volume", 0))
                            for c, p in positions.items()},
            }
        except Exception:
            return None

    def invalidate_account(self):
        """强制下一轮重新查询账户(守护每轮调用, 拿最新持仓)。"""
        self._acct = None

    # ------------------------------------------------------------ 单标的

    def symbol_context(self, sym):
        """算出一个标的的完整上下文(行情/指标/开关/档位/水位)。"""
        code = sym["code"]
        count = max(80, int(self.grid_cfg.get("sigma_window", 60)) + 25)
        snap = self.feed.snapshot(
            code, count=count,
            sigma_window=int(self.grid_cfg.get("sigma_window", 60)))
        if not snap:
            return {"code": code, "name": sym.get("name", code),
                    "skip": "行情缺失", "enabled": bool(sym.get("enabled"))}

        tick, ind = snap["tick"], snap["ind"]
        last = ind.get("last")
        last_close = tick.get("last_close") or last
        if not last or not last_close:
            return {"code": code, "name": sym.get("name", code),
                    "skip": "价格缺失"}

        sw = grid.switch_state(last, ind.get("ma20"), ind.get("ma20_prev"),
                               ind.get("r20_pct"),
                               sym.get("switch") or {})
        band = grid.band_of(sym, ind.get("sigma"),
                            self.grid_cfg.get("band_k", 1.0),
                            self.grid_cfg.get("band_mode", "sigma"))

        # ref 当日固定: 跨日重置后账本里 ref=0, 此处自动重设
        ref = self.ledger.get_ref(code)
        ref_src = "ledger"
        if not ref or ref <= 0:
            if self.grid_cfg.get("ref_mode") == "open":
                ref, src = tick.get("open"), "open"
            else:
                # 前收口径: 以"今天"为参照系取最后一根已完成日K(不信 tick 时间戳)
                ref, src = market.prev_close(
                    tick, snap.get("daily"), snap.get("daily_prev"),
                    self.now_fn().strftime("%Y%m%d"), last_close)
            if ref and ref > 0:
                self.ledger.set_ref(code, ref)
                ref_src = src

        ladder = grid.build_ladder(ref, band, sym.get("n_units")
                                   or self.grid_cfg.get("n_units", 5)) \
            if (ref and band) else None

        ma20 = ind.get("ma20")
        dev_pct = (last / ma20 - 1) * 100 if (ma20 and ma20 > 0) else None
        ma20p = ind.get("ma20_prev")
        slope_pct = (ma20 / ma20p - 1) * 100 if (ma20 and ma20p and ma20p > 0) \
            else None

        return {
            "code": code, "name": sym.get("name", code),
            "enabled": bool(sym.get("enabled")),
            "skip": "" if (ladder and sw != grid.DISABLED) else
                    ("带宽不可用" if not ladder else "开关停用"),
            "last": last, "last_close": last_close, "open": tick.get("open"),
            "high": tick.get("high"), "low": tick.get("low"),
            "ma20": ma20, "ma20_prev": ma20p,
            "dev_pct": dev_pct, "slope_pct": slope_pct,
            "r20_pct": ind.get("r20_pct"), "sigma": ind.get("sigma"),
            "trend_degree": ind.get("trend_degree"),
            "band": band, "ref": ref, "ref_src": ref_src,
            "ladder": ladder, "switch": sw,
            "scale": grid.half_scale(sw),
            "source": snap.get("source"),
        }

    def plan_symbol(self, sym, acct, hhmm, phase):
        """单标的 → (infos, intents)。infos 供网页展示, intents 为决策结果。"""
        ctx = self.symbol_context(sym)
        code = sym["code"]
        if ctx.get("skip"):
            return ctx, []

        ladder = ctx["ladder"]
        n_units = int(ladder.get("n_units", 5))
        # max_units: 日内实际使用的最大档数(底仓容量决定), 缺省=n_units。
        max_units = int(self.grid_cfg.get("max_units") or n_units)
        depth = min(n_units, max_units)
        # HALF: 只做到前一半档位; 整数至少 1 档
        n_eff = max(1, int(round(depth * ctx["scale"]))) \
            if ctx["scale"] > 0 else 0

        high = ctx.get("high") or ctx["last"]
        low = ctx.get("low") or ctx["last"]
        target_sell = min(grid.crossed_sell(ladder, high), n_eff)
        target_buy = min(grid.crossed_buy(ladder, low), n_eff)

        filled_sell = self.ledger.get_units(code, "SELL")
        filled_buy = self.ledger.get_units(code, "BUY")
        # 跨日账本被重置时水位可能高于目标(旧ref档位更深) → 归零重来
        if filled_sell > target_sell and filled_sell > 0 and \
                ctx["ref_src"] != "ledger":
            filled_sell = 0
        if filled_buy > target_buy and filled_buy > 0 and \
                ctx["ref_src"] != "ledger":
            filled_buy = 0

        ctx.update({"filled_sell_units": filled_sell,
                    "filled_buy_units": filled_buy,
                    "target_sell_units": target_sell,
                    "target_buy_units": target_buy,
                    "n_eff_units": n_eff})

        max_round = int(self.cfg.get("max_units_per_round", 2))
        want_sell = min(max(0, target_sell - filled_sell), max_round)
        want_buy = min(max(0, target_buy - filled_buy), max_round)

        sym_led = self.ledger.sym(code)
        intents = []
        # 先卖后买: 先腾出净敞口与现金, 再考虑买回(与日内资金现实一致)
        for i in range(want_sell):
            unit = filled_sell + i + 1
            intents.append(self._make_intent(
                sym, ctx, "SELL", unit, acct, hhmm, phase))
        for i in range(want_buy):
            unit = filled_buy + i + 1
            intents.append(self._make_intent(
                sym, ctx, "BUY", unit, acct, hhmm, phase))

        ctx["net_exposure"] = int(sym_led.get("bought_today", 0)) \
            - int(sym_led.get("sold_today", 0))
        ctx["realized_pnl"] = float(sym_led.get("realized_pnl", 0) or 0)
        ctx["trips"] = int(sym_led.get("trips", 0) or 0)
        return ctx, intents

    def _make_intent(self, sym, ctx, side, unit, acct, hhmm, phase):
        code = sym["code"]
        ladder = ctx["ladder"]
        price = grid.ladder_price(ladder, side, unit)
        date_str = (self.now_fn().strftime("%Y%m%d"))
        order_id = "TT_%s_%s_%s_%d" % (date_str, _code_tag(code), side, unit)

        def bad(rc, msg):
            return Intent(code, ctx["name"], side, price or 0, 0,
                          "档位%d" % unit, False, rc, msg, order_id,
                          {"unit": unit})

        if price is None or price <= 0:
            return bad("LADDER_MISS", "第%d档无价" % unit)

        # ---- 运行时交叉校验(band_mode=sigma 专用) ----
        # sigma 模式的 band 由日波动率×band_k 现算, 配置期无法静态判定最深档偏离。
        # 这里算完就用同口径校验: 若 depth*band > max_price_deviation_pct, 说明
        # 深层档位会被偏离闸门整片拦掉(功能静默缺失) → 直接报错并给出可做档数。
        # 用 max_units(实际用几档) 而非 n_units(阶梯算几档) —— 后者可大于前者。
        band = float(ladder.get("band") or 0)
        max_dev = float(self.cfg.get("risk", {}).get(
            "max_price_deviation_pct", 0.05) or 0)
        eff_depth = min(int(ladder.get("n_units", 5)),
                        int(self.grid_cfg.get("max_units")
                            or ladder.get("n_units", 5)))
        if band > 0 and max_dev > 0:
            worst_dev = eff_depth * band
            if worst_dev > max_dev + 1e-9:
                usable = int(max_dev / band)
                return bad("DEPTH_BEYOND_DEVIATION",
                           "最深档(第%d档)固有偏离 %.2f%% > 偏离闸门 %.2f%% "
                           "(band=%.3f%%×%d) → 该档必被拦; 本配置下最多做到第 %d 档"
                           % (eff_depth, worst_dev * 100, max_dev * 100,
                              band * 100, eff_depth, usable))

        # ---- 股数: 浮仓单位金额 ÷ 挂单价 ----
        weight = float(sym.get("weight") or 0)
        n_units = int(ladder.get("n_units", 5))
        total_asset = float(acct.get("total_asset") or 0)
        unit_value = total_asset * weight / n_units if n_units else 0
        volume = _floor_lot(unit_value, price, self.lot)
        if volume <= 0:
            return bad("SIZE_ZERO",
                       "单价 %.3f 下单位金额 %.0f 元不足一手" % (price, unit_value))

        # ---- 运行时约束(纯逻辑层之外的账户事实) ----
        sym_led = self.ledger.sym(code)
        sold_today = int(sym_led.get("sold_today", 0))
        bought_today = int(sym_led.get("bought_today", 0))
        can_use = (acct.get("can_use") or {}).get(code)
        held = (acct.get("positions") or {}).get(code) or {}
        held_value = float(held.get("market_value") or 0)

        if side == "BUY":
            cash = float(acct.get("cash") or 0)
            need = price * volume
            if acct.get("source") == "qmt" and cash < need:
                return bad("CASH_INSUFFICIENT",
                           "现金 %.0f 元 < 需 %.0f 元" % (cash, need))
        else:
            # 账户里根本没有这只票 → 归因到"无底仓", 比笼统的"拿不到可卖量"
            # 更容易定位(真实账户最常见的情形)
            positions = acct.get("positions")
            if positions is not None and code not in positions:
                return bad("NO_BASE_POSITION",
                           "账户无该标的持仓, 无法卖出(T+1 无底仓)")
            # 卖出必须有底仓(可卖量按"当日已卖"递减, 避免同轮重复卖同一批底仓)
            if can_use is not None:
                remaining = int(can_use) - sold_today
                if volume > remaining:
                    vol2 = _floor_lot(remaining * price, price, self.lot) \
                        if remaining > 0 else 0
                    if vol2 <= 0:
                        return bad("NO_BASE_POSITION",
                                   "可卖 %d 股(已卖 %d), 无足够底仓"
                                   % (int(can_use), sold_today))
                    volume = vol2

        # ---- 风控总闸门 ----
        # ladder_price_ref 语义 = "本该挂的档位价"(阶梯理论价), 与 price(实际委托价)
        # 分开传入, 让 check_slippage 真正生效。计划阶段两者相同 → 滑点 0 恒过;
        # 执行层(executor)改用对手价/追价时, 传入真实委托价, 闸门才有意义。
        # 历史缺陷: 此处曾传 ladder_price_ref=price(同值) → 滑点恒 0, 纯装饰。
        ladder_ref = grid.ladder_price(ladder, side, unit)
        max_net_buy_qty = self._max_net_buy_qty(acct, sym, price)
        v = self.gate.check(
            side=side, code=code, price=price, volume=volume,
            ref_price=ctx["ref"],
            ladder_price_ref=ladder_ref if ladder_ref else price,
            last_close=ctx["last_close"], hhmm=hhmm,
            session_cfg=self.session_cfg, lot=self.lot,
            sold_today=sold_today, bought_today=bought_today,
            can_use_volume=can_use, held_value=held_value,
            total_asset=total_asset or None,
            daily_trades=self.ledger.daily_trades(),
            realized_pnl=self.ledger.total_realized_pnl(),
            max_net_buy_qty=max_net_buy_qty,
            enabled=bool(sym.get("enabled")),
        )
        meta = {"unit": unit, "band": ladder.get("band"),
                "ref": ctx["ref"], "switch": ctx["switch"],
                "target_units": ctx.get("target_sell_units"
                                        if side == "SELL"
                                        else "target_buy_units")}
        if not v.ok:
            return Intent(code, ctx["name"], side, price, volume,
                          "档位%d" % unit, False, v.code, v.msg, order_id, meta)
        return Intent(code, ctx["name"], side, price, volume,
                      "档位%d 距中枢%+.2f%%" % (unit, (unit * ladder["band"]) * 100),
                      True, "", "", order_id, meta)

    def _max_net_buy_qty(self, acct, sym, price):
        """日内净买入上限(股)。ratio=0 → 严格归位(只能买回等量)。"""
        risk = self.cfg.get("risk", {})
        ratio = float(risk.get("max_net_buy_today_ratio") or 0)
        if ratio <= 0:
            return 0
        total = float(acct.get("total_asset") or 0)
        if total <= 0 or price <= 0:
            return 0
        return int(total * ratio / price // self.lot * self.lot)

    # ------------------------------------------------------------ 全量

    def plan(self, codes=None):
        """跑一轮完整决策。返回可直接序列化给网页的 dict。"""
        now = self.now_fn()
        hhmm = now.strftime("%H:%M")
        rolled = self.ledger.roll_if_new_day()
        self.invalidate_account()
        acct = self.account_state()
        phase = None
        try:
            from .risk import session_phase
            phase = session_phase(hhmm, self.session_cfg)
        except Exception:
            phase = "UNKNOWN"

        syms = [s for s in self.cfg.get("symbols", [])
                if codes is None or s["code"] in codes]
        infos, intents = [], []
        for sym in syms:
            try:
                ctx, its = self.plan_symbol(sym, acct, hhmm, phase)
            except Exception as e:              # 单标的异常不拖垮整轮
                ctx = {"code": sym.get("code"), "name": sym.get("name"),
                       "skip": "异常: %r" % e, "enabled": False}
                its = []
            infos.append(ctx)
            intents.extend(its)

        ok_intents = [i for i in intents if i.ok]
        rejected = [i for i in intents if not i.ok]
        signals = [self._to_signal(i) for i in ok_intents]

        return {
            "ok": True,
            "hhmm": hhmm,
            "phase": phase,
            "rolled": rolled,
            "dry_run": bool(self.cfg.get("dry_run", True)),
            "env": self.cfg.get("env", "real"),
            "source": getattr(self.feed, "last_source", "") or "unknown",
            "account": {
                "source": acct.get("source"), "ok": acct.get("ok"),
                "total_asset": acct.get("total_asset"),
                "cash": acct.get("cash"),
                "positions": acct.get("positions") or {},
                "note": acct.get("note", ""),
            },
            "risk": {
                "breaker_tripped": self.gate.breaker.tripped,
                "breaker_count": self.gate.breaker.count,
                "breaker_reason": self.gate.breaker.reason,
                "daily_trades": self.ledger.daily_trades(),
                "max_daily_trades": self.cfg.get("risk", {}).get("max_daily_trades"),
                "realized_pnl": self.ledger.total_realized_pnl(),
                "max_daily_loss": self.cfg.get("risk", {}).get("max_daily_loss"),
            },
            "symbols": infos,
            "intents": [i._asdict() for i in ok_intents],
            "rejected": [i._asdict() for i in rejected],
            "signals": signals,
            "counts": {"symbols": len(infos), "intents": len(ok_intents),
                       "rejected": len(rejected)},
        }

    def _to_signal(self, intent):
        """Intent → 桥端协议信号(与 prism.trader.build_signal 同构)。"""
        sig = {
            "order_id": intent.order_id,
            "action": intent.side,
            "stock_code": intent.code,
            "order_type": intent.side,
            "price": float(intent.price),
            "volume": int(intent.volume),
            "account_id": self.cfg.get("account_id") or "",
            "created_at": _now_iso(self.now_fn()),
            "status": "pending",
            "strategy_id": "tt_grid_v1",
            "reason": intent.reason,
            "unit": (intent.meta or {}).get("unit"),
        }
        return sig


def make_engine(cfg, ledger, prefer_sample=False, account=None, now_fn=None):
    """便捷构造。"""
    feed = market.make_feed(prefer_sample=prefer_sample, now_fn=now_fn)
    return TTEngine(cfg, ledger, feed=feed, account=account, now_fn=now_fn)


__all__ = ["TTEngine", "Intent", "make_engine", "tt_config", "Path"]
