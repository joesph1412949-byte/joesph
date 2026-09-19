# -*- coding: utf-8 -*-
"""历史回放验收(rehearsal): 离线 / 确定性 / 零副作用地跑一遍守护的一天。

为什么需要它
    本仓实盘的"三步验收"(DRY_RUN 观察 → sim → 小额真实单)一步都没做过,
    而审计抓到的 Critical(被拒卖单记成已卖 → 下一轮真买入; 同轮重复卖;
    账本抹除)全是"上线即触发"级。回放的价值就是**在零风险下把这些抓住** ——
    所以本模块的重点不是"跑通", 而是把守护真实调度链路上的时点/闸门/信号
    内容完整走一遍并留下可断言的输出。

怎么做到零副作用(第一前提)
    * **全部注入到 tmp**: `state_file` / `positions_file` / `signal_root`
      指向本次回放的临时工作目录 —— 两个守护都支持注入, 见 R1 表;
    * **dry_run=True 写死**(CLI 不提供 --live), 因此信号只做**内容预览**;
    * **不构造下单通道**: 本模块不 import executor, 不碰 QMT; 输入只有
      `runtime/cache/**` 的只读缓存 + 调用方给的夹具;
    * 急停开关 `trader.PAUSE_FILE` 在回放期间临时改指工作目录, 结束即还原。

覆盖到哪 / 覆盖不到哪(如实标注, 不假装)
    能: prism/live_daemon.py 的四个时点段(15:05 选股落计划 / 次日开盘窗口发
        BUY / 盘中卖出巡检 / 15:00 对账), 全部闸门(急停、T+1、止盈止损、
        持有期、跌停顺延、可卖量、资产/现金缺失 fail-closed)。
    不能(只有联网/只有另一个进程才有):
        1. `trader.run_daily` 的 36 因子引擎与市场门禁 —— 需要
           `DataProvider.build_market_context()/get_limit_ups()`, 那是 QMT
           连接态。回放**降级**为"缓存涨停池按连板数取前 N", 见
           inputs.notes;
        2. 盘中真实 tick —— 离线无分钟/快照数据, 用**当日缓存收盘价**代替,
           属前视; 本回放只验收调度链路与闸门, **不得据此评判策略收益**;
        3. "跌停开盘跳过"闸门(自 2026-09-19 起 live_daemon._do_open_send 也有,
           判据含数据源真实 DownStopPrice/缺行情 fail-closed)在回放里只有
           **降级输入**: 开盘步的 tick 由计划价合成(见 inputs.notes), 因此它
           在回放中恒不命中; 一字板转排队仍只在 `paper.py::execute_open_buys`
           (模拟盘账户), 回放把 paper 侧那一段也跑一遍做对照, 并把这个不对称
           点名(见 paper_leg)。
"""
import argparse
import json
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from shared.common import limit_ratio_for_code, next_weekday
from prism import trader
from prism.live_daemon import LiveDaemon

# 一天的时间线。day=0 → --date 当天; day=1 → 次一工作日。
# 时点取自 prism/schedule.py 的调度常量(PICK_SLOT=15:05 /
# OPEN_WINDOW=09:26-09:35 / SETTLE_AFTER=15:00)。前两个自 2026-09-19 起由
# 策略 execution.pick_slot / execution.open_window 声明驱动(schedule 在 import 时
# 解析), 缺失/格式不符才回落上面这两个默认值; 本回放的时间线按现值写死。
DAY_PLAN = (
    ("15:05", 0, "live", "收盘选股 → 落次日计划"),
    ("15:06", 0, "live", "同日重跑(幂等: 不重复选股)"),
    ("09:26", 1, "live", "次日开盘窗口 → 发 BUY(dry-run 预览)"),
    ("09:27", 1, "paper", "模拟盘侧开盘消费(一字板转排队/跌停开盘跳过)"),
    ("09:31", 1, "live", "开盘窗口内重复轮询"),
    ("09:36", 1, "live", "盘中卖出巡检 #1(窗口外)"),
    ("10:00", 1, "live", "盘中卖出巡检 #2"),
    ("14:30", 1, "live", "盘中卖出巡检 #3(收敛时段)"),
    ("15:00", 1, "live", "收盘后(sync, 尚未到选股时点)"),
    ("15:05", 1, "live", "当日收盘选股 → 落下一日计划"),
    # 第 3 天: 当日买入的仓在第 1 天受 T+1 保护, 只有到了第 3 天才可能被卖出
    # 巡检真正判定 → 卖出通道(止盈/止损/持有期/跌停顺延)在这里才有对象。
    ("09:36", 2, "live", "T+2 卖出巡检 #1(上一日买入的仓已可卖)"),
    ("10:00", 2, "live", "T+2 卖出巡检 #2"),
    ("14:30", 2, "live", "T+2 卖出巡检 #3(收敛时段)"),
    ("15:00", 2, "live", "T+2 收盘后对账"),
    ("15:05", 2, "live", "T+2 收盘选股"),
)

# 剔除的时间戳字段: `trader.build_signal` 的 created_at 是**墙钟** isoformat,
# 留着它 --json 两次跑就不一致。剔除后本模块的输出逐字节可复现。
WALLCLOCK_KEYS = ("created_at",)


# ---------------------------------------------------------------- 缓存(只读)

def load_zt_cache(cache_path=None, index_path=None):
    """只读载入 zt 历史缓存 → (cache, index)。缺文件/损坏 → ({}, {})。"""
    from prism.zt_history import _load_cache, _load_index
    if cache_path is None and index_path is None:
        return _load_cache(), _load_index()
    import pickle
    out = []
    for p in (cache_path, index_path):
        try:
            out.append(pickle.loads(Path(p).read_bytes()))
        except Exception:
            out.append({})
    return out[0], out[1]


def close_at(cache, code, day_iso):
    """缓存里 code 在 day_iso(YYYY-MM-DD)的收盘价; 缺失/非正 → None。"""
    rec = cache.get(code) or {}
    try:
        i = list(rec.get("dates") or []).index(day_iso)
        c = float((rec.get("close") or [])[i])
    except (ValueError, IndexError, TypeError):
        return None
    return c if c > 0 else None


def make_cache_source(cache, index, top_n, notes, cache_present=True):
    """缓存降级选股/行情源 → (screen_fn, tick_lookup)。

    screen_fn(now) → run_daily 形状的 dict: 候选 = 该日涨停池按
    (连板数降序, 代码升序)取前 top_n; 挂单价 = **次日涨停价**
    = 当日收盘 × (1 + 分板幅度), 与 zt_history 判涨停同源 ——
    这正是实盘 `up_stop_price` 的语义, 不是"拿收盘价凑数"。

    tick_lookup(code, tick_iso, prev_iso) → {lastPrice, lastClose} 或 None,
    取自缓存日线(见模块 docstring 的降级说明 ②)。

    全部输入来自内存里的只读缓存, 不联网、不连 QMT、不写任何文件。
    """
    from prism.zt_history import qmt_zt_feed

    def screen_fn(now):
        day = now.strftime("%Y%m%d")
        day_iso = now.strftime("%Y-%m-%d")
        pool = qmt_zt_feed(day, cache, index=index) or []
        pool = sorted(pool, key=lambda p: (-int(p.get("boards") or 0),
                                           str(p.get("code") or "")))
        sigs, skipped_no_price = [], 0
        for p in pool:
            code = str(p.get("code") or "")
            if not code:
                continue
            c = close_at(cache, code, day_iso)
            if not c:
                skipped_no_price += 1
                continue
            price = round(c * (1 + limit_ratio_for_code(code)), 2)
            sigs.append({"order_id": "PLAN_%s_%s" % (day, tag(code)),
                         "action": "BUY", "stock_code": code, "name": code,
                         "price": price, "volume": 100,
                         "strategy_id": "rehearsal", "composite": None})
            if len(sigs) >= int(top_n):
                break
        return {"environment_ok": True, "candidates": sigs, "signals": sigs,
                "signals_written": 0, "paused": False, "source": "cache",
                "skipped_no_price": skipped_no_price}

    def tick_lookup(code, tick_iso, prev_iso):
        last = close_at(cache, code, tick_iso)
        prev = close_at(cache, code, prev_iso)
        if not last or not prev:
            return None
        return {"lastPrice": last, "lastClose": prev}

    if cache_present:
        notes.append(
            "选股降级: 未跑 36 因子引擎与市场门禁(需 QMT 连接态的 "
            "DataProvider.build_market_context/get_limit_ups) → 候选取自 "
            "runtime/cache/.zt_history_cache.pkl 的当日涨停池, 按(连板数 desc, "
            "代码 asc)取前 %d; environment_ok 恒 True(未算)" % top_n)
        notes.append(
            "挂单价口径: 次日涨停价 = 缓存当日收盘 × (1+分板幅度), 与 "
            "zt_history 判涨停同源(等价于实盘 up_stop_price), 非凑数")
        notes.append(
            "盘中 tick 降级: 离线无分钟/快照数据 → 用**当日缓存收盘价**代替 "
            "lastPrice(前收用前一交易日缓存收盘价)。这是前视, 本回放只验收"
            "调度链路与闸门, 不得据此评判策略收益")
    return screen_fn, tick_lookup


def tag(code):
    return str(code).replace(".", "").replace("-", "").upper()


# ---------------------------------------------------------------- 账户替身

class RehearsalAccount:
    """账户替身(只读): 资产/现金由构造给定, 持仓**实时读回放账本**。

    fail="asset" 模拟查询失败(返回 None) —— 必须是"拿不到", 不是"资产为 0",
    否则验不出 live_daemon 的 fail-closed 语义。
    """

    def __init__(self, total_asset, cash=None, positions_path=None,
                 can_use=None, fail=None):
        self.total = float(total_asset)
        self.cash = self.total if cash is None else float(cash)
        self.positions_path = Path(positions_path) if positions_path else None
        self.can_use = dict(can_use) if can_use is not None else None
        self.fail = fail

    def _book(self):
        if self.positions_path and self.positions_path.exists():
            try:
                d = json.loads(self.positions_path.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
        return {}

    def asset(self):
        if self.fail == "asset":
            return None
        return {"total_asset": self.total, "cash": self.cash,
                "market_value": 0.0, "frozen_cash": 0.0}

    def positions(self):
        if self.fail == "positions":
            return None
        out = {}
        for code, p in self._book().items():
            out[code] = {"volume": int(p.get("volume") or 0),
                         "can_use_volume": int(p.get("can_use_volume",
                                                     p.get("volume")) or 0),
                         "open_price": float(p.get("buy_price") or 0),
                         "market_value": 0.0}
        return out

    def total_asset(self):
        return self.total if self.asset() else None

    def available_cash(self):
        return self.cash if self.asset() else None

    def can_use_map(self):
        if self.fail == "positions":
            return None
        if self.can_use is not None:
            return dict(self.can_use)
        return {c: v["can_use_volume"] for c, v in self.positions().items()}


class _Provider:
    """非 None 的 provider 占位(live_daemon 用它的存在性做闸门)。"""

    class _DS:
        def get_full_market_ticks(self, codes=None):
            return {}

        def get_instrument(self, code):
            return {}          # 离线无 instrument 详情 → daemon 回落到分板系数

    def __init__(self):
        self.ds = self._DS()


# ---------------------------------------------------------------- 回放

class Rehearsal:
    """一次离线回放。全部依赖走 LiveDaemon 既有的注入缝(不改守护签名)。"""

    def __init__(self, day, *, workdir=None, env="real", strategy=None,
                 source="cache", top_n=5, total_asset=1_000_000.0, cash=None,
                 account=None, screen_fn=None, ticks_fn=None,
                 tick_script=None, seed_positions=None, seed_plans=None,
                 pause=False, book_fixture=True, paper_leg=True,
                 position_ratio=None, max_positions=None):
        self.day = _as_date(day)
        self.env = env
        self.strategy = strategy
        self.source = source
        self.top_n = int(top_n)
        self.total_asset = float(total_asset)
        self.cash = cash
        self.notes = []
        self._account = account
        self._screen_fn = screen_fn
        self._ticks_fn = ticks_fn
        self._tick_script = tick_script or {}
        self._tick_lookup = None
        self._cache = None
        self.book_fixture = bool(book_fixture)
        self.paper_leg = bool(paper_leg)
        self.pause = bool(pause)
        self.position_ratio = position_ratio
        self.max_positions = max_positions
        self.seed_positions = dict(seed_positions or {})
        self.seed_plans = list(seed_plans or [])
        self.workdir = Path(workdir) if workdir else Path(
            tempfile.mkdtemp(prefix="prism_rehearsal_"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.workdir / "live_state.json"
        self.positions_path = self.workdir / "positions.json"
        self.signal_root = self.workdir / "signals"
        self.pause_path = self.workdir / "paused"
        self._cache = None
        self._index = None
        self._cur = {}
        self._cur_now = datetime.combine(self.day, _hm("15:05"))

    # ---------- 外部输入装配 ----------

    def _prepare_inputs(self):
        if self.source == "cache" and self._screen_fn is None:
            self._cache, self._index = load_zt_cache()
            if not self._cache:
                self.notes.append(
                    "缓存缺失/损坏: runtime/cache/.zt_history_cache.pkl 读不到 "
                    "→ 选股源为空, 回放只剩调度骨架(计划恒空)")
            fn, self._tick_lookup = make_cache_source(
                self._cache, self._index, self.top_n, self.notes,
                cache_present=bool(self._cache))
            self._screen_fn = fn
        elif self._screen_fn is None:
            self.notes.append("source=%s: 未提供 screen_fn → 选股源为空"
                              % self.source)
            self._screen_fn = lambda now: {
                "environment_ok": False, "candidates": [], "signals": [],
                "signals_written": 0, "paused": False}
        if self._ticks_fn is None:
            def ticks_fn(codes):
                return {c: dict(self._cur[c]) for c in codes if c in self._cur}
            self._ticks_fn = ticks_fn
        self.notes.append(
            "PICK_SLOT/OPEN_WINDOW 取自 prism/schedule.py(自 2026-09-19 起由策略 "
            "execution.pick_slot/open_window 解析, 现值 15:05 / 09:26-09:35), "
            "SETTLE_AFTER=15:00 仍是 schedule 常量; 本回放时间线按现值写死")
        self.notes.append(
            "dry_run 的两处**重复**是回放形状而非生产形状: `_do_open_send` 的 "
            "dry_run 分支在 book.add **之前** return ⇒ 计划不被消费, 开盘窗口内"
            "每次 tick 都重发同一批 BUY; `_do_sell_patrol` 只在 `not dry_run` 时"
            "写 exit_signaled ⇒ 同一天每次巡检都重发同一批 SELL。两条都由**确定性"
            "order_id + 桥端 per-day dedup** 兜底, 实盘路径首次发出后即去重。回放"
            "把重复留在时间线上, 免得把 dry_run 的形状当成生产形状")
        self.notes.append(
            "开盘步行情: 计划股在开盘窗口还不是持仓, 且离线缓存只到选股日 ⇒ "
            "回放按**当日计划的代码**加载行情, 仍拿不到时用**计划价**合成中性 "
            "tick(现价=前收=计划价, 落盘价仍由计划决定)。这是回放的降级声明: "
            "实盘 fail-closed 下缺行情=整批不发, 回放必须有输入才能预览内容, "
            "不代表真实开盘价/跌停价")
        if self.day.weekday() >= 5:
            self.notes.append(
                "回放起始日 %s 是周末 ⇒ 守护全程不在交易时段, 零信号属预期"
                % self.day.isoformat())

    def _seed_files(self):
        if self.seed_positions:
            trader_atomic(self.positions_path,
                          json.dumps(self.seed_positions, ensure_ascii=False,
                                     indent=2))
        st = {"version": 1, "plans": self.seed_plans, "pick_slots": [],
              "exit_signaled": {}}
        trader_atomic(self.state_path,
                      json.dumps(st, ensure_ascii=False, indent=2))

    def _account_obj(self):
        if self._account is not None:
            return self._account
        return RehearsalAccount(self.total_asset, self.cash,
                                positions_path=self.positions_path)

    # ---------- 行情 ----------

    def _price_of(self, code, hm, tick_iso, prev_iso):
        if self._tick_lookup is not None:
            try:
                got = self._tick_lookup(code, tick_iso, prev_iso)
            except Exception:
                got = None
            if got:
                return got
        return dict(self._tick_script.get(hm, {}).get(code) or {})

    def _load_ticks(self, hm, codes, tick_iso, prev_iso):
        self._cur = {}
        for c in codes:
            t = self._price_of(c, hm, tick_iso, prev_iso)
            if t:
                self._cur[c] = t
        return self._cur

    # ---------- 单步 ----------

    def _preview(self, signals):
        out = []
        for s in signals:
            payload = {k: v for k, v in s.items() if k not in WALLCLOCK_KEYS}
            out.append({"file": "%s/pending/%s.json" % (self.env,
                                                        s["order_id"]),
                        "payload": payload})
        return out

    def _strip(self, signals):
        for s in signals:
            for k in WALLCLOCK_KEYS:
                s.pop(k, None)
        return signals

    def _book_fixture_positions(self, plans, for_date):
        """dry_run 下守护**刻意不记账**(见 _do_open_send), 于是卖出巡检永远
        看不到新仓。回放用夹具把"本应记下的持仓"补进 tmp 账本, 好让
        09:31+ 的巡检有对象 —— 只动本次回放自己的 positions.json。"""
        if not self.book_fixture or not plans:
            return []
        book = {}
        if self.positions_path.exists():
            try:
                book = json.loads(self.positions_path.read_text(
                    encoding="utf-8")) or {}
            except Exception:
                book = {}
        added = []
        for p in plans:
            book[p["code"]] = {"code": p["code"], "name": p.get("name") or "",
                               "buy_price": p["price"], "buy_date": for_date,
                               "volume": int(p["volume"])}
            added.append(p["code"])
        trader_atomic(self.positions_path,
                      json.dumps(book, ensure_ascii=False, indent=2))
        return added

    def _live_step(self, now, hm, label):
        self._cur = {}
        today = now.strftime("%Y-%m-%d")
        plans = [p for p in self.daemon._load_state()["plans"]
                 if p.get("for_date") == today]
        if now.weekday() < 5 and hm not in ("15:05", "15:06", "15:00"):
            # 开盘步也要行情: 计划股此刻还不是持仓, 只按持仓取行情会让开盘步
            # 一只都拿不到 tick —— 实盘 fail-closed 下那是整批跳过、预览变空。
            # 故代码集 = 持仓 ∪ 当日计划(09:26 也从排除表里拿掉)。
            codes = set(self.daemon._load_positions().all()) \
                | {p["code"] for p in plans}
            self._load_ticks(hm, codes, today, _prev_iso(self._cache, now))
        for p in plans:
            # 计划股仍无行情(离线缓存只到选股日, 次一/次二工作日没有 K 线, 且
            # 未给 tick_script)→ 用**计划价**合成一条中性 tick(现价=前收=
            # 计划价): 闸门有输入可判、预览有内容。这不是假装知道开盘价 ——
            # 落盘价由计划决定, 这里只避免"fail-closed 让回放整段变空"。
            self._cur.setdefault(p["code"], {"lastPrice": float(p["price"]),
                                            "lastClose": float(p["price"])})
        out = self.daemon.tick_once(now)
        entry = {"at": now.strftime("%Y-%m-%dT%H:%M"), "hm": hm,
                 "label": label, "kind": "live", "action": out["action"],
                 "planned": out["planned"], "buys": self._strip(out["buys"]),
                 "sells": self._strip(out["sells"]),
                 "skipped": list(out["skipped"]), "screen": out["screen"],
                 "sync": out["sync"]}
        entry["would_write"] = self._preview(entry["buys"] + entry["sells"])
        if out["action"] == "open_send":
            plans = [p for p in self.daemon._load_state()["plans"]
                     if p.get("for_date") == now.strftime("%Y-%m-%d")]
            entry["book_fixture"] = self._book_fixture_positions(
                plans, now.strftime("%Y-%m-%d"))
        return entry

    def _paper_step(self, now, hm, label, plans):
        """模拟盘侧的同一时点(对照): 一字板转排队 / 跌停开盘跳过在这里。"""
        entry = {"at": now.strftime("%Y-%m-%dT%H:%M"), "hm": hm,
                 "label": label, "kind": "paper", "action": "paper",
                 "planned": [], "buys": [], "sells": [], "skipped": [],
                 "screen": None, "sync": 0, "would_write": []}
        if self.pause:
            # 两个守护都在自己进程内先查急停开关: 对照腿同样零信号
            entry["action"] = "paused"
            entry["note"] = "急停开关存在 → 不消费计划"
            return entry
        if not self.paper_leg:
            entry["action"] = "skipped"
            entry["skipped_reason"] = "paper_leg=False"
            return entry
        try:
            from prism.paper import PaperAccount
            acc = PaperAccount(state_path=self.workdir / "paper_account.json")
            acc.init_account()
            today = now.strftime("%Y-%m-%d")
            acc.state["planned_buys"] = [
                {"code": p["code"], "name": p.get("name") or "",
                 "price": p["price"], "volume": p["volume"], "for_date": today}
                for p in (plans or []) if p.get("for_date") == today]
            if not acc.state["planned_buys"]:
                entry["result"] = {"bought": [], "queued": [], "skipped": []}
                entry["note"] = "当日无计划可消费"
                return entry
            codes = [p["code"] for p in acc.state["planned_buys"]]
            self._load_ticks(hm, codes, today, _prev_iso(self._cache, now))
            r = acc.execute_open_buys(self._open_tick_provider(now), now)
            entry["result"] = {k: r.get(k) for k in ("bought", "queued",
                                                     "skipped", "error")
                               if k in r}
            entry["tick_fields_note"] = (
                "离线 tick 无 bidVol/lastVolume(盘口/量能=联网数据) ⇒ 一字板"
                "排板必被 '排板不通过' 拒绝; 该分支判据由 "
                "prism/tests/test_open_top5.py 覆盖")
        except Exception as e:                       # 对照腿不可用要看得见
            entry["error"] = "%s: %r" % (type(e).__name__, e)
        return entry

    def _open_tick_provider(self, now):
        ms = int(now.timestamp() * 1000)

        def provide(codes):
            out = {}
            for c in codes:
                t = dict(self._cur.get(c) or {})
                if not t:
                    continue
                prev = float(t.get("lastClose") or 0)
                px = float(t.get("lastPrice") or 0)
                ratio = limit_ratio_for_code(c)
                out[c] = {"time": ms, "open": px, "lastPrice": px,
                          "lastClose": prev,
                          "upStopPrice": round(prev * (1 + ratio), 2),
                          "downStopPrice": round(prev * (1 - ratio), 2)}
                # 盘口/量能字段是联网数据: 离线 tick 没有 bidVol/lastVolume,
                # 于是 create_pending_buy 必然"排板不通过" —— 属标注过的降级,
                # 不在这里合成盘口假装覆盖(该分支判据见 tests/test_open_top5.py)。
                for k in ("bidVol", "lastVolume", "askVol", "volume"):
                    if k in t:
                        out[c][k] = t[k]
            return out

        return provide

    # ---------- 主流程 ----------

    def run(self):
        self._prepare_inputs()
        self._seed_files()
        day1 = next_weekday(self.day)
        books = {0: self.day, 1: day1, 2: next_weekday(day1)}
        self.daemon = LiveDaemon(
            provider=_Provider(), account=self._account_obj(),
            strategy=self.strategy, env=self.env, dry_run=True,
            position_ratio=self.position_ratio,
            max_positions=self.max_positions,
            state_file=self.state_path, positions_file=self.positions_path,
            signal_root=self.signal_root, screen_fn=self._screen_fn,
            ticks_fn=self._ticks_fn, now_fn=lambda: self._cur_now)
        prev_pause = trader.PAUSE_FILE
        trader.PAUSE_FILE = self.pause_path
        steps = []
        try:
            if self.pause:
                self.pause_path.write_text("1", encoding="utf-8")
            elif self.pause_path.exists():
                self.pause_path.unlink()
            for hm, off, kind, label in DAY_PLAN:
                d = books[off]
                self._cur_now = datetime.combine(d, _hm(hm))
                if kind == "live":
                    steps.append(self._live_step(self._cur_now, hm, label))
                else:
                    steps.append(self._paper_step(self._cur_now, hm, label,
                                                  self._last_plans()))
        finally:
            trader.PAUSE_FILE = prev_pause
        return self._result(steps)

    def _last_plans(self):
        st = self.daemon._load_state()
        today = self._cur_now.strftime("%Y-%m-%d")
        return [p for p in st["plans"] if p.get("for_date") == today]

    def _result(self, steps):
        buys = [s for st in steps for s in st.get("buys", [])]
        sells = [s for st in steps for s in st.get("sells", [])]
        planned = [p for st in steps for p in (st.get("planned") or [])]
        skipped = [s for st in steps for s in st.get("skipped", [])]
        state = _read_json(self.state_path)
        return {
            "date": self.day.strftime("%Y%m%d"),
            "next_weekday": next_weekday(self.day).strftime("%Y%m%d"),
            "dry_run": True,
            "env": self.env,
            "signal_root": "<tmp>/signals",
            "inputs": {"source": "provided" if self.source != "cache"
                       else "runtime/cache(只读)",
                       "top_n": self.top_n,
                       "notes": list(self.notes),
                       "excluded_wallclock_keys": list(WALLCLOCK_KEYS)},
            "steps": steps,
            "paper_leg": next((st for st in steps if st["kind"] == "paper"),
                              None),
            "summary": {
                "planned": planned,
                "buys": buys,
                "sells": sells,
                "signals": buys + sells,
                "skipped": skipped,
                "pick_slots": state.get("pick_slots", []),
                "plans_left": state.get("plans", []),
                "actions": [(st["hm"], st["action"]) for st in steps
                            if st["kind"] == "live"],
            },
        }


def _read_json(p):
    try:
        d = json.loads(Path(p).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _prev_iso(cache, now):
    """前一交易日的 ISO 日期(缓存里严格早于今天的最后一根)。"""
    if not cache:
        return (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    best = ""
    for rec in cache.values():
        for d in rec.get("dates") or []:
            if best < d < today:
                best = d
    return best or (now.date() - timedelta(days=1)).strftime("%Y-%m-%d")


def _as_date(day):
    if isinstance(day, datetime):
        return day.date()
    if isinstance(day, date):
        return day
    s = str(day).strip()
    return datetime.strptime(s, "%Y%m%d").date() if len(s) == 8 \
        else date.fromisoformat(s)


def _hm(hm):
    h, m = str(hm).split(":")
    return datetime(2000, 1, 1, int(h), int(m)).time()


def trader_atomic(path, text):
    """原子写(与守护同款, 复用 shared.common.atomic_write)。"""
    from shared.common import atomic_write
    atomic_write(path, text)


# ---------------------------------------------------------------- 人类可读输出

def render(res, out=None):
    """时间线日志(每个时点做了什么 / 产出哪些信号 / 被哪些闸门拦下)。"""
    w = (out or sys.stdout).write
    w("=" * 78 + "\n")
    w("历史回放(离线/确定性/零副作用) --date %s  次一工作日 %s  env=%s\n"
      % (res["date"], res["next_weekday"], res["env"]))
    w("signal_root=%s  dry_run=True(只预览内容, 不落盘)\n" % res["signal_root"])
    w("=" * 78 + "\n")
    for n in res["inputs"]["notes"]:
        w("  [降级/口径] %s\n" % n)
    w("-" * 78 + "\n")
    for st in res["steps"]:
        w("%s  %-10s %s\n" % (st["at"], st.get("action", st["kind"]),
                              st["label"]))
        scr = st.get("screen")
        if scr:
            w("      选股: env_ok=%s 候选=%s 信号=%s error=%s paused=%s\n"
              % (scr.get("environment_ok"), scr.get("candidates"),
                 scr.get("signals"), scr.get("error"), scr.get("paused")))
        for p in st.get("planned") or []:
            w("      [计划] %s %s %s股 @ %.2f  for_date=%s\n"
              % (p["code"], p.get("name") or "", p["volume"], p["price"],
                 p.get("for_date")))
        for s in st.get("buys", []):
            w("      [BUY ] %s %s股 @ %.2f order_id=%s\n"
              % (s["stock_code"], s["volume"], s["price"], s["order_id"]))
        for s in st.get("sells", []):
            w("      [SELL] %s %s股 reason=%s order_id=%s\n"
              % (s["stock_code"], s["volume"], s.get("reason", ""),
                 s["order_id"]))
        for sk in st.get("skipped", []):
            w("      [闸门拦下] %s\n" % sk)
        for pv in st.get("would_write", []):
            w("      [若 --live 会落盘] %s: %s\n"
              % (pv["file"], json.dumps(pv["payload"], ensure_ascii=False,
                                        sort_keys=True)))
        if st.get("book_fixture"):
            w("      [回放夹具补账] dry_run 下守护不记账 → 按计划价补记 %s\n"
              % ",".join(st["book_fixture"]))
        if st["kind"] == "paper":
            if st.get("error"):
                w("      [对照腿不可用] %s\n" % st["error"])
            elif st.get("result"):
                w("      [模拟盘侧] bought=%s queued=%s skipped=%s\n"
                  % (st["result"].get("bought"), st["result"].get("queued"),
                     st["result"].get("skipped")))
            elif st.get("note"):
                w("      [模拟盘侧] %s\n" % st["note"])
    s = res["summary"]
    w("-" * 78 + "\n")
    w("汇总: 计划 %d / BUY %d / SELL %d / 闸门记录 %d / pick_slots=%s\n"
      % (len(s["planned"]), len(s["buys"]), len(s["sells"]),
         len(s["skipped"]), s["pick_slots"]))
    w("时点动作: %s\n" % s["actions"])
    w("=" * 78 + "\n")
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="prism 实盘守护历史回放(离线/确定性/零副作用)")
    ap.add_argument("--date", required=True,
                    help="回放起始交易日 YYYYMMDD(选股日; 发单在次一工作日)")
    ap.add_argument("--strategy", default=None, help="策略 id(缺省当前指针)")
    ap.add_argument("--json", action="store_true", help="额外打印结构化摘要")
    ap.add_argument("--top-n", type=int, default=5, help="候选上限(缺省 5)")
    ap.add_argument("--total-asset", type=float, default=1_000_000.0)
    ap.add_argument("--workdir", default=None,
                    help="工作目录(缺省临时目录; 状态/账本/信号根都在其下)")
    ap.add_argument("--seed-positions", default=None,
                    help="初始持仓 JSON 文件(夹具; 缺省空账本)")
    ap.add_argument("--no-paper", action="store_true",
                    help="不跑模拟盘侧对照腿")
    ap.add_argument("--keep", action="store_true", help="保留工作目录")
    args = ap.parse_args(argv)

    seed_positions = None
    if args.seed_positions:
        seed_positions = json.loads(Path(args.seed_positions).read_text(
            encoding="utf-8"))
    workdir = Path(args.workdir) if args.workdir else None
    r = Rehearsal(args.date, workdir=workdir, strategy=args.strategy,
                  top_n=args.top_n, total_asset=args.total_asset,
                  seed_positions=seed_positions, paper_leg=not args.no_paper)
    res = r.run()
    render(res)
    print("工作目录: %s" % r.workdir)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, sort_keys=True, indent=2))
    if workdir is None and not args.keep:
        shutil.rmtree(r.workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
