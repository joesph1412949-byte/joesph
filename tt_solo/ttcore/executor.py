# -*- coding: utf-8 -*-
"""做T直连执行器: 把 Intent 变成真实委托, 走外部 Python 直连 miniQMT。

与 daemon 里"写信号文件 → QMT 内桥消费"那条通道并列的第二条通道。
本模块是**唯一会真正下单的地方**, 因此设计原则与只读层完全相反:

  安全边界(全部 fail-closed, 任一不确定即拒单):
    1. **执行层纵深防御**: 金额/价格上限在这里再兜一道; 若调用方用
       `propose_price` 顶替了档位价, 还会用 `Intent.meta` 里的阶梯价与中枢
       **复核一次滑点与偏离**(没有可比的参考价 → 直接拒, 否则 propose_price
       就等于绕过闸门)。默认链路不传 propose_price, 那时唯一的闸门在引擎侧
       (`RiskGate.check`) —— 这里**不**重复跑全部闸门;
    2. **拒单不抛异常**: 单笔失败只记录, 不终止守护(与 daemon 同款韧性);
    3. **幂等**: 同一 order_id 当日只下过一次。已提交的 order_id 落到
       `placed_path`(append-only JSONL, 与 tt_history.jsonl 同目录), 启动时
       载入 —— **跨进程 / 跨账本重置重启也不重复报单**(只认当日; 被柜台拒的
       单**不**记录, 保留重试窗口);
    4. **dry_run 默认开**: 不显式 propose=True/--live 就只打印不提交;
    5. **账户校验**: 下单前核对 account_id 与连接到的账户一致, 防止打到错账户;
    6. **方向校验**: side/action 不是 BUY/SELL 一律拒 —— 绝不把空串或乱码
       静默降级成 SELL。

为什么是外部直连而不是 QMT 内桥:
  实测 `xtquant.xttrader.XtQuantTrader` 在本机可直接 `connect()==0`,
  且 `order_stock / order_stock_async / cancel_order_stock` 全部可用
  (`order_stock` 签名: account, stock_code, order_type, order_volume,
   price_type, price, strategy_name='', order_remark='')。
  因此无需把脚本粘进 QMT 编辑器 —— 那条路受 GBK 编码、无法调试、无法回归测试、
  且 QMT 内现有桥是密文不可核对的多重限制。

用法:
    ex = DirectExecutor(account_id="88869979")
    ex.connect()
    ex.execute(intents, dry_run=True)     # 演练: 只打印
    ex.execute(intents, dry_run=False)    # 真实提交

常量的两套体系(务必分清):
  - 外部 API(本模块用): order_type = xtconstant.STOCK_BUY(23) / STOCK_SELL(24),
    price_type = xtconstant.FIX_PRICE(11) 表示限价;
  - QMT 内 passorder: opType 0/1 —— **不是同一套**, 别混用。
"""
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from .risk import check_price_deviation, check_slippage

LOG = logging.getLogger("tt_executor")

# 已提交 order_id 的落盘文件名(与账本/tt_history.jsonl 同目录)
PLACED_NAME = "tt_placed.jsonl"

# 外部 API 常量(缺 xtquant 时用字面量兜底, 数值与官方一致)
STOCK_BUY = 23
STOCK_SELL = 24
FIX_PRICE = 11

# 委托状态: 已成 / 部成 / 已撤 (xtconstant.ORDER_* 的稳定取值)
_ORDER_SUCCEEDED = 56
_ORDER_PART_SUCC = 55
_ORDER_CANCELED = 54
_ORDER_JUNK = 57

STRATEGY_NAME = "tt_grid_v1"


def _import_xt():
    """懒加载 xtquant。返回 (xttrader, xttype) 或抛 ImportError。"""
    from xtquant import xttrader, xttype
    return xttrader, xttype


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _attr(obj, *names, default=None):
    for n in names:
        v = getattr(obj, n, None)
        if v is not None:
            return v
    return default


def _field(obj, name, default=None):
    """同时兼容 Intent(属性) 与信号 dict(键) 两种入参。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class DirectExecutor:
    """外部 Python 直连 miniQMT 下单。backend 可注入以便离线测试。

    与 prism.live_account._QmtBackend 共用同一套连接方式, 但多了写能力。
    """

    def __init__(self, account_id="", data_dir=r"D:\QMT\userdata_mini",
                 backend=None, lot=100, now_fn=None, max_order_amount=None,
                 max_price=None, max_slippage_pct=None,
                 max_price_deviation_pct=None, placed_path=None):
        self.account_id = str(account_id or "").strip()
        self.data_dir = data_dir
        self.lot = int(lot or 100)
        self.now_fn = now_fn or datetime.now
        self.backend = backend or _DirectBackend(data_dir, self.account_id)
        # 执行层纵深防御(即便 engine 已查过, 这里再兜一道 —— 手工构造 Intent
        # 或配置漂移时不至于裸奔)。None/<=0 表示该道上限不启用。
        self.max_order_amount = max_order_amount
        self.max_price = max_price
        # propose_price 顶替档位价时的复核阈值(见 _one)
        self.max_slippage_pct = max_slippage_pct
        self.max_price_deviation_pct = max_price_deviation_pct
        self._placed = {}          # order_id -> 下单时间(当日已提交)
        self.placed_path = Path(placed_path) if placed_path else None
        self._load_placed()
        self.stats = {"submitted": 0, "rejected": 0, "failed": 0, "skipped": 0}

    # ------------------------------------------------------------ 幂等落盘

    def _load_placed(self):
        """启动时载入**当日**已提交的 order_id。读不到 → 空(不阻断)。"""
        if not self.placed_path:
            return
        today = self.now_fn().strftime("%Y%m%d")
        try:
            p = self.placed_path
            if not p.exists():
                return
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue                  # 坏行跳过(与 tt_history 同款)
                if (isinstance(obj, dict) and obj.get("date") == today
                        and obj.get("order_id")):
                    self._placed[str(obj["order_id"])] = str(obj.get("at") or "")
        except Exception:                     # noqa: BLE001 - 见下 fail-open
            # 幂等记录是旁路: 读不到只意味着"这次重启可能重复报单", 而拦住
            # 载入会让整个守护起不来。故一律 fail-open(与 state 归档同款)。
            LOG.warning("载入已提交 order_id 失败: %s", self.placed_path)

    def _record_placed(self, oid, when):
        """记一笔"当日已提交"。append + fsync(照 state.archive_current 的手法)。

        只在**柜台已受理**之后调用 —— 被拒的单必须保留重试窗口。
        落盘失败只告警: 内存里已经记下了, 本进程内幂等仍然成立。
        """
        self._placed[oid] = when
        if not self.placed_path:
            return
        try:
            p = self.placed_path
            p.parent.mkdir(parents=True, exist_ok=True)
            row = {"date": self.now_fn().strftime("%Y%m%d"),
                   "order_id": oid, "at": when}
            with open(p, "a", encoding="utf-8") as fp:
                fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                fp.flush()
                os.fsync(fp.fileno())     # 与 atomic_write 同款: 掉电不丢这一行
        except Exception as e:            # noqa: BLE001 - 见上
            LOG.warning("落盘已提交 order_id 失败 %s: %r", oid, e)

    # ------------------------------------------------------------ 连接

    def connect(self):
        try:
            return bool(self.backend.connect())
        except Exception as e:
            LOG.warning("连接失败: %r", e)
            return False

    @property
    def connected_account(self):
        return getattr(self.backend, "account_id", "") or self.account_id

    # ------------------------------------------------------------ 下单

    def execute(self, intents, dry_run=True, propose_price=None):
        """执行一批 Intent。返回逐笔结果列表。

        intents: 已通过风控的 Intent 列表(ok=True 的才会被提交, 其余记 skipped)
        dry_run: True 只打印不提交(默认)
        propose_price: 可选 callable(intent) -> 实际委托价。给了就用它顶替
                       intent.price(例如改用对手价), 并**必须**能用 Intent.meta
                       里的 ladder_price/ref 复核通过滑点与偏离闸门。
        """
        results = []
        if not intents:
            return results
        if not dry_run and not self.connect():
            LOG.error("未连接, 整批拒单(%d 笔)", len(intents))
            for it in intents:
                results.append({"order_id": _field(it, "order_id", "(no-id)"),
                                "ok": False,
                                "code": "NOT_CONNECTED", "msg": "未连接 miniQMT"})
                self.stats["failed"] += 1
            return results

        # 账户一致性: 非 dry_run 时账户号必须对得上, 防空挂到别的账户
        if not dry_run and self.account_id:
            actual = self.connected_account
            if actual and str(actual) != self.account_id:
                LOG.error("账户不匹配: 期望 %s, 实际 %s → 整批拒单",
                          self.account_id, actual)
                for it in intents:
                    results.append({"order_id": _field(it, "order_id",
                                                       "(no-id)"),
                                    "ok": False,
                                    "code": "ACCOUNT_MISMATCH",
                                    "msg": "连接账户 %s != 配置 %s"
                                           % (actual, self.account_id)})
                    self.stats["failed"] += 1
                return results

        for it in intents:
            r = self._one(it, dry_run=dry_run, propose_price=propose_price)
            results.append(r)
        return results

    def _verify_proposed_price(self, oid, it, price):
        """`propose_price` 生效时的滑点/偏离复核(fail-closed)。

        参考价取自 `Intent.meta`: ladder_price = 本该挂的档位价, ref = 当日中枢。
        两个都拿不到 → 拒(`PRICE_UNVERIFIED`), 否则 propose_price 就成了绕过
        引擎闸门的后门。返回与 _one 同形的结果 dict, ok=True 表示放行。
        """
        meta = _field(it, "meta") or {}
        ladder = _num(meta.get("ladder_price"))
        base = _num(meta.get("ref"))
        if ladder <= 0 and base <= 0:
            self.stats["rejected"] += 1
            return {"order_id": oid, "ok": False, "code": "PRICE_UNVERIFIED",
                    "msg": "propose_price %s 无阶梯价/中枢可比, 拒绝提交" % price}
        # 只跑"配了阈值且拿得到参考价"的那几道(阈值 None/<=0 = 该道不启用,
        # 与 check_slippage 的自身语义一致)。
        verdict = None
        if ladder > 0 and (_num(self.max_slippage_pct) or 0) > 0:
            verdict = check_slippage(price, ladder, self.max_slippage_pct)
        if ((verdict is None or verdict.ok) and base > 0
                and (_num(self.max_price_deviation_pct) or 0) > 0):
            verdict = check_price_deviation(price, base,
                                            self.max_price_deviation_pct)
        if verdict is not None and not verdict.ok:
            self.stats["rejected"] += 1
            LOG.warning("propose_price 复核未过 %s: %s %s", oid, verdict.code,
                        verdict.msg)
            return {"order_id": oid, "ok": False, "code": verdict.code,
                    "msg": verdict.msg}
        return {"order_id": oid, "ok": True}

    def _one(self, it, dry_run=True, propose_price=None):
        oid = _field(it, "order_id", "") or "(no-id)"
        # Intent 用 ok 标记是否通过风控; 裸信号 dict 没有该字段 → 一律放行,
        # 由调用方(daemon)保证只把已通过的信号递进来。
        approved = _field(it, "ok", True)
        if not approved:
            self.stats["skipped"] += 1
            return {"order_id": oid, "ok": False, "code": "NOT_APPROVED",
                    "msg": "未通过风控: %s" % _field(it, "reject_code", "")}

        # 幂等: 同一 order_id 本进程内只提交一次
        if oid in self._placed:
            self.stats["skipped"] += 1
            return {"order_id": oid, "ok": False, "code": "ALREADY_PLACED",
                    "msg": "本进程已提交过 %s" % oid}

        price = float(_field(it, "price", 0) or 0)
        if propose_price is not None:
            try:
                alt = float(propose_price(it) or 0)
            except Exception as e:
                LOG.warning("propose_price 失败, 用档位价: %r", e)
                alt = 0.0
            if alt > 0:
                if abs(alt - price) <= 1e-9:
                    price = alt                       # 没改价 → 无需复核
                else:
                    r = self._verify_proposed_price(oid, it, alt)
                    if not r["ok"]:
                        return r
                    price = alt
        if price <= 0:
            self.stats["failed"] += 1
            return {"order_id": oid, "ok": False, "code": "PRICE_INVALID",
                    "msg": "委托价非法: %r" % _field(it, "price")}

        volume = int(_field(it, "volume", 0) or 0)
        if volume <= 0 or volume % self.lot != 0:
            self.stats["failed"] += 1
            return {"order_id": oid, "ok": False, "code": "VOLUME_INVALID",
                    "msg": "股数非法: %r" % _field(it, "volume")}

        # 执行层纵深防御(即使 engine 已查过也再兜一道)
        if self.max_price and price > self.max_price:
            self.stats["rejected"] += 1
            return {"order_id": oid, "ok": False, "code": "PRICE_TOO_HIGH",
                    "msg": "委托价 %.3f > 执行层上限 %.3f"
                           % (price, self.max_price)}
        amt = price * volume
        if self.max_order_amount and amt > self.max_order_amount:
            self.stats["rejected"] += 1
            return {"order_id": oid, "ok": False, "code": "AMOUNT_TOO_BIG",
                    "msg": "单笔金额 %.0f > 执行层上限 %.0f"
                           % (amt, self.max_order_amount)}

        side_raw = str(_field(it, "side") or _field(it, "action") or "") \
            .strip().upper()
        code = _field(it, "code") or _field(it, "stock_code") or ""
        # 绝不静默降级: 旧实现是 startswith("B") 否则 SELL —— 空串/乱码会变成
        # **真实卖出**。只认字面 BUY/SELL。
        if side_raw == "BUY":
            side, order_type = "BUY", STOCK_BUY
        elif side_raw == "SELL":
            side, order_type = "SELL", STOCK_SELL
        else:
            self.stats["rejected"] += 1
            return {"order_id": oid, "ok": False, "code": "SIDE_INVALID",
                    "msg": "方向非法(须为 BUY/SELL): %r"
                           % (_field(it, "side") or _field(it, "action"))}

        if dry_run:
            self.stats["skipped"] += 1
            LOG.info("[DRY] %s %s %s %d股 @%.3f", oid, side, code,
                     volume, price)
            return {"order_id": oid, "ok": True, "code": "DRY_RUN",
                    "msg": "演练未提交", "price": price, "volume": volume,
                    "side": side}

        # ---- 真实提交 ----
        try:
            seq = self.backend.order(code, order_type, volume, price,
                                     strategy=STRATEGY_NAME, remark=oid)
        except Exception as e:
            self.stats["failed"] += 1
            LOG.exception("下单异常 %s: %r", oid, e)
            return {"order_id": oid, "ok": False, "code": "ORDER_EXCEPTION",
                    "msg": repr(e)}
        if seq is None or int(seq) < 0:
            self.stats["failed"] += 1
            return {"order_id": oid, "ok": False, "code": "ORDER_REJECTED",
                    "msg": "柜台拒绝, seq=%r" % seq}
        self._record_placed(oid, self.now_fn().isoformat(timespec="seconds"))
        self.stats["submitted"] += 1
        LOG.info("[已提交] %s %s %s %d股 @%.3f seq=%s", oid, side, code,
                 volume, price, seq)
        return {"order_id": oid, "ok": True, "code": "SUBMITTED",
                "msg": "已提交", "seq": int(seq), "price": price,
                "volume": volume, "side": side}

    # ------------------------------------------------------------ 查询/撤单

    def orders_today(self):
        """今日委托(柜台为准)。查不到 → []。"""
        try:
            return self.backend.orders() or []
        except Exception as e:
            LOG.warning("查询委托失败: %r", e)
            return []

    def trades_today(self):
        """今日成交(柜台为准)。查不到 → []。"""
        try:
            return self.backend.trades() or []
        except Exception as e:
            LOG.warning("查询成交失败: %r", e)
            return []

    def cancel(self, order_id):
        """按柜台 order_id 撤单。"""
        try:
            return bool(self.backend.cancel(order_id))
        except Exception as e:
            LOG.warning("撤单失败 %r: %r", order_id, e)
            return False

    def cancel_all(self):
        """撤掉今日所有可撤委托。返回撤单成功数。"""
        n = 0
        for o in self.orders_today():
            st = int(_num(_attr(o, "order_status", "orderStatus", default=0)))
            if st in (_ORDER_SUCCEEDED, _ORDER_CANCELED, _ORDER_JUNK):
                continue
            oid = _attr(o, "order_id", "orderId")
            if oid is not None and self.cancel(oid):
                n += 1
        return n


class _DirectBackend:
    """真实 backend: 依赖注入点, 离线测试用 fake 替身。"""

    def __init__(self, data_dir, account_id=""):
        self.data_dir = data_dir
        self.account_id = account_id
        self._trader = None
        self._acc = None

    def connect(self):
        if self._trader is not None and self._acc is not None:
            return True
        xttrader, xttype = _import_xt()
        trader = xttrader.XtQuantTrader(self.data_dir, int(time.time()))
        trader.start()
        if trader.connect() != 0:
            return False
        acc_id = self.account_id
        if not acc_id:
            infos = trader.query_account_infos() or []
            ids = [str(_attr(a, "account_id", "accountId", "accountID",
                             default="")) for a in infos]
            ids = [i for i in ids if i]
            if not ids:
                return False
            acc_id = ids[0]
            self.account_id = acc_id
        acc = xttype.StockAccount(acc_id, "STOCK")
        trader.subscribe(acc)
        self._trader, self._acc = trader, acc
        return True

    def order(self, code, order_type, volume, price,
              strategy=STRATEGY_NAME, remark=""):
        """限价下单。返回 order seq(<0 表示失败)。"""
        seq = self._trader.order_stock(
            self._acc, code, order_type, int(volume), FIX_PRICE,
            float(price), strategy, remark)
        return seq

    def orders(self):
        return self._trader.query_stock_orders(self._acc) or []

    def trades(self):
        return self._trader.query_stock_trades(self._acc) or []

    def cancel(self, order_id):
        return self._trader.cancel_order_stock(self._acc, int(order_id)) == 0

    def asset(self):
        a = self._trader.query_stock_asset(self._acc)
        if a is None:
            return None
        return {"total_asset": _num(_attr(a, "total_asset", "totalAsset")),
                "cash": _num(_attr(a, "cash")),
                "market_value": _num(_attr(a, "market_value", "marketValue")),
                "frozen_cash": _num(_attr(a, "frozen_cash", "frozenCash"))}

    def positions(self):
        rows = self._trader.query_stock_positions(self._acc) or []
        out = {}
        for p in rows:
            code = str(_attr(p, "stock_code", "stockCode", default="") or "")
            if not code:
                continue
            out[code] = {
                "volume": int(_num(_attr(p, "volume"))),
                "can_use_volume": int(_num(_attr(
                    p, "can_use_volume", "canUseVolume"))),
                "open_price": _num(_attr(p, "open_price", "openPrice")),
                "market_value": _num(_attr(p, "market_value", "marketValue")),
            }
        return out


def execute_intents(intents, account_id="", dry_run=True, backend=None,
                    propose_price=None, lot=100, now_fn=None, placed_path=None):
    """便捷函数: 一次性构造执行器并提交。返回 (results, executor)。"""
    ex = DirectExecutor(account_id=account_id, backend=backend, lot=lot,
                        now_fn=now_fn, placed_path=placed_path)
    results = ex.execute(intents, dry_run=dry_run,
                         propose_price=propose_price)
    return results, ex
