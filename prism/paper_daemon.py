# -*- coding: utf-8 -*-
"""模拟盘守护进程: 盘中实时盯盘 + 15:05收盘选股 + 次日开盘买入 + 收盘结算 + 缺口补算。

用法: python -m prism.paper_daemon
安全(设计 §8): 本进程绝不写 D:\\QMT_SIGNALS、不调用任何下单接口——纯记账;
实盘进程零影响; 行情/K线只读; 单轮异常不影响下一轮。"""
import logging
import threading
import time
from datetime import datetime

from prism import schedule
from prism.paper import PaperAccount

LOG = logging.getLogger("paper_daemon")
# 调度时点常量与时段/连接/退避共用 live_daemon(prism/schedule.py)
PICK_SLOT = schedule.PICK_SLOT     # 收盘选股时点(spec §5)
OPEN_WINDOW = schedule.OPEN_WINDOW  # 次日开盘买入窗口(spec §5)
SETTLE_AFTER = schedule.SETTLE_AFTER
POLL_SECONDS = 5                   # 模拟盘轮询间隔(纯内存记账, 可密)
INDEX_CODE = "000001.SH"          # 上证指数: 交易日历来源
ZT_REFRESH_INTERVAL = 6 * 3600    # 涨停池缓存刷新节流(秒)
ZT_REFRESH_WAIT = 120             # 采集前等刷新线程的上限(秒, I3); 超时照常继续


class PaperDaemon:
    """调度器: 每 POLL_SECONDS 一轮; 15:05选股/开盘买入窗口/收盘结算由
    tick_once 触发。"""

    def __init__(self, account, ticks_fn=None, sleep_fn=None, now_fn=None,
                 zt_refresh_fn=None, fund_snapshot_fn=None):
        self.account = account
        self.provider = None
        self.ticks_fn = ticks_fn
        self.sleep_fn = sleep_fn
        self.now_fn = now_fn
        if zt_refresh_fn is None:
            # 真实路径: lambda 里懒 import, 刷新存量尾部 + 重建按日索引
            def zt_refresh_fn():
                from prism import zt_history
                r = zt_history.refresh_cache()
                r["index_days"] = len(zt_history.build_index())
                return r
        self.zt_refresh_fn = zt_refresh_fn
        self._zt_refresh_at = 0.0    # 上次刷新触发时刻(epoch秒, 0=未刷过)
        self._zt_thread = None       # 最近一次刷新线程(I3: 采集前要等它结束)
        if fund_snapshot_fn is None:
            # 真实路径(规格 §7, Task 3): 当日涨停池逐只基本面快照(Y2/Y5 等),
            # 由 feed 自身落既有缓存; 同样懒 import + 可注入(测试假实现)
            def fund_snapshot_fn():
                from prism import fund_snapshot
                return fund_snapshot.snapshot_once(fund_snapshot.default_codes())
        self.fund_snapshot_fn = fund_snapshot_fn

    # ---------- 时段 ----------
    def in_session(self, now):
        """交易日时段(与实盘守护同一份判定, prism/schedule.py)。"""
        return schedule.in_session(now)

    # ---------- 数据函数(结算/补算用, 全部只读) ----------
    def close_fn(self, code, day=None):
        """该股收盘价: K线中 <=day 的最后一根(无 day 取最新); 拿不到 → None。"""
        if self.provider is None:
            return None
        try:
            df = self.provider.ds.get_kline(code, days=5)
            days = self.account._kline_day_strs(df)
        except Exception:
            return None
        if df is None or len(df) == 0:
            return None
        target = self.account._n8(day) if day else None
        rows = [(k, float(c)) for k, c in zip(days, df["close"])
                if not target or k <= target]
        return rows[-1][1] if rows and rows[-1][1] > 0 else None

    def _due_natural(self, buy_date, day_str):
        """到期判定(主口径, 终审 I-3): 自然日 (结算日 - buy_date).days
        >= max_hold_days, 与回测/实盘 ExitRule 同口径。"""
        try:
            held = (datetime.strptime(day_str, "%Y-%m-%d")
                    - datetime.strptime(str(buy_date), "%Y-%m-%d")).days
        except (ValueError, TypeError):
            return False                # 日期坏值 → 不到期(保守)
        return held >= self.account.max_hold_days()

    # ---------- 排板轮询行情(设计 §5) ----------
    def _quote_ticks(self, codes):
        """排板轮询行情(原生字段透传, daemon 不转量纲); 异常 → 空(fail-open)。"""
        if not codes or getattr(self.provider, "ds", None) is None:
            return {}
        try:
            return self.provider.ds.get_full_market_ticks(list(codes)) or {}
        except Exception:
            return {}

    # ---------- 开盘买入行情(spec §5, C2 注入) ----------
    def _open_buy_ticks(self, codes):
        """开盘买入行情: 轮询行情 + 注入真实涨跌停价(来源 ds.get_instrument
        的 UpStopPrice/DownStopPrice, data.py 同模式); 拿不到 → 不补
        (paper 层自动分板回落)。"""
        ticks = self._quote_ticks(codes)
        for code, t in ticks.items():
            try:
                det = self.provider.ds.get_instrument(code) or {}
            except Exception:
                continue
            if det.get("UpStopPrice"):
                t["upStopPrice"] = det["UpStopPrice"]
            if det.get("DownStopPrice"):
                t["downStopPrice"] = det["DownStopPrice"]
        return ticks

    # ---------- 涨停池缓存自动刷新(6h 节流, daemon 线程) ----------
    def _maybe_refresh_zt(self):
        """到点(距上次触发 >=6h)在 daemon 线程跑 zt_refresh_fn
        (refresh_cache+build_index), 启动自愈 + 收盘后保鲜。

        节流检查与时间戳置位在调用线程做(tick 单线程, 先置位再孵化,
        防重复孵化); 整体 try/except 落日志 — 绝不阻塞/炸掉 tick 主循环。
        线程存 self._zt_thread: 基本面快照采集前要 join 它(I3), 否则 15:05
        索引还没重建完就会空采。
        """
        now = time.time()
        if now - self._zt_refresh_at < ZT_REFRESH_INTERVAL:
            return
        self._zt_refresh_at = now

        def _worker():
            try:
                r = self.zt_refresh_fn()
                LOG.info("涨停池缓存刷新完成: %r", r)
            except Exception as e:
                LOG.warning("涨停池缓存刷新失败(忽略, 下个节流窗口重试): %r", e)

        self._zt_thread = threading.Thread(target=_worker, daemon=True,
                                           name="zt-refresh")
        self._zt_thread.start()

    # ---------- 基本面快照采集挂钩(规格 §7, Task 3, daemon 线程) ----------
    def _force_zt_refresh_and_wait(self):
        """无视 6h 节流强制刷新涨停池索引并等它跑完(I3 重试用)。

        空池场景的真凶常是"索引还没生成": 守护在 15:00~15:05 之间重启时,
        启动那次刷新跑完时索引里还没有当日池, 15:05 的刷新又被 6h 节流挡掉
        (`self._zt_thread` 是**已完成**线程 → join 立即返回)→ default_codes()
        返回 []。这里把节流时间戳归零后重跑一次, 并 join 到结束再返回。
        """
        self._zt_refresh_at = 0.0
        self._maybe_refresh_zt()
        t = self._zt_thread
        if t is not None and t.is_alive():
            t.join(ZT_REFRESH_WAIT)
            if t.is_alive():
                LOG.warning("涨停池强制刷新 %d 秒未结束 → 仍按现状重试采集",
                            ZT_REFRESH_WAIT)

    def _maybe_fund_snapshot(self):
        """收盘选股后采集当日基本面快照(Y2/Y5 无历史可回补, 从今天起积累)。

        与 _maybe_refresh_zt 同模式: 注入式钩子(fund_snapshot_fn) + daemon
        线程 + 整体 try/except 落日志 —— 快照失败绝不影响选股结果/主循环。
        触发频率由调用点保证: 只在收盘选股分支里调(每日一次, 幂等键在选股侧)。

        I3(审查修复): 默认钩子按**本地 zt 索引**取当日涨停池, 而索引正被
        _maybe_refresh_zt 在**另一个线程**里重建 —— 15:05 时当日索引可能还没
        生成, default_codes() 返回 [] → 空采(日志像成功, 当天 Y2/Y5 却永久
        丢失)。所以采集前先 join 刷新线程(上限 ZT_REFRESH_WAIT 秒, 超时照常
        继续并说明); 空池结果带 skipped → 打 WARNING, 绝不写成"采集完成"。

        I3 补强(2026-09-18): 空池还可能是"索引里确实没有当日池(尚未刷新)"
        —— 光告警救不回当天数据。空池时**无视节流强制刷新一次索引再重试一次**
        采集(重试仍空才告警, 只重试一次, 不空转)。

        I1 修复(2026-09-18): 网络全挂时 `snapshot_once` 返回
        {"saved": 0, "failed": 40}(**无 skipped 键**) —— 旧 `_empty` 只看
        skipped → 既不走重试也不打 WARNING, 直接落 INFO"采集完成"; 而 pick 分支
        每日只跑一次(screens_done 幂等键) → 当天不会再有第二次采集, Y2/Y5 按
        设计无历史可回补 ⇒ 一次瞬时失败 = 那天快照永久缺失, 日志却写"完成"。
        现在"saved==0 且 failed>0"也走同一条"强制刷新 + 重试一次"路径。
        `saved>0 且 failed>0`(部分成功)**不算失败**, 照常 INFO(如实带计数)。
        """
        def _failed(r):
            """空池(skipped)或全股取数失败(saved==0 且 failed>0) → True。

            部分成功(saved>0, failed>0)不是失败: 至少采到了一部分。
            """
            if not isinstance(r, dict):
                return False
            return bool(r.get("skipped")) or (
                not r.get("saved") and bool(r.get("failed")))

        def _worker():
            try:
                t = self._zt_thread
                if t is not None and t.is_alive():
                    t.join(ZT_REFRESH_WAIT)
                    if t.is_alive():
                        LOG.warning(
                            "涨停池刷新 %d 秒未结束 → 照常采集(可能取到旧索引)",
                            ZT_REFRESH_WAIT)
                r = self.fund_snapshot_fn()
                if _failed(r):
                    # 空池多半是索引里还没有当日池(节流把 15:05 那次刷新挡掉);
                    # 全失败多半是瞬时网络/接口故障 → 都强制刷新后重试一次,
                    # 别把当天的 Y2/Y5 直接丢掉
                    LOG.info("基本面快照未采到(%s) → 无视节流强制刷新后重试一次",
                             r.get("skipped") or "全股取数失败 saved=%s failed=%s"
                             % (r.get("saved"), r.get("failed")))
                    self._force_zt_refresh_and_wait()
                    r = self.fund_snapshot_fn()
                if _failed(r):
                    if r.get("skipped"):
                        LOG.warning(
                            "基本面快照未采集(%s): 涨停池索引里没有当日池 —— "
                            "若今天非交易日或索引尚未刷新属正常; 否则当天 Y2/Y5 "
                            "无历史可回补, 请手动补采: python -m prism.fund_snapshot",
                            r["skipped"])
                    else:
                        LOG.warning(
                            "基本面快照未采集(重试后仍全部 %s 只取数失败): "
                            "网络/接口异常 —— 当天 Y2/Y5 无历史可回补, "
                            "请手动补采: python -m prism.fund_snapshot",
                            r.get("failed"))
                else:
                    LOG.info("基本面快照采集完成: %r", r)
            except Exception as e:
                LOG.warning("基本面快照采集失败(忽略, 不影响选股): %r", e)

        threading.Thread(target=_worker, daemon=True,
                         name="fund-snapshot").start()

    # ---------- 单轮 ----------
    def tick_once(self, now=None):
        """一轮: 15:00后未结算→结算(幂等)+15:05收盘选股; 09:26-09:35 开盘
        买入窗口; 盘中→tick卖检查; 其余 idle。

        行情异常/账本未初始化 → 本轮跳过不记账(设计 §8/§11)。"""
        now = now or (self.now_fn() if self.now_fn else datetime.now())
        out = {"action": "idle", "sells": [], "buys": [], "settle": None,
               "pending_filled": [], "pending_canceled": []}
        if self.account.state is None and not self.account.load():
            return out
        d = now.strftime("%Y-%m-%d")
        hm = now.strftime("%H:%M")
        if now.weekday() < 5 and hm >= SETTLE_AFTER:    # M-e: 周末不结算不记平点
            # ponytail: 周中节假日仍会记平点, 完整解决需交易日历
            st = self.account.state
            if d not in st.get("settled_dates", []) and self.provider:
                # 收盘失效(设计 §2.3.3): 结算前 pending 全撤(解冻+expire 流水);
                # 撤单幂等键随日切重置(I-2), 与撤单流水一并由 settle_day 落盘
                for p in list(st.get("pending_buys", [])):
                    self.account._dispose_pending(p["code"], "queue_expire", now)
                    out["pending_canceled"].append(p["code"])
                st["canceled_pending_codes"] = []
                # 终审 I-3: 到期按自然日口径, 闭包捕获本轮结算日 d
                # (due_fn 形参为 (code, buy_date), 结算日经 _due_natural 注入)
                out["settle"] = self.account.settle_day(
                    self.close_fn, due_fn=lambda c, bd: self._due_natural(bd, d),
                    now=now)
                out["action"] = "settle"
            # 收盘选股(spec §5): 15:05, 同 tick 先结算后选股; 幂等键带日期
            # (C1: pickT<日>T15:05 每日唯一), 结果落日志(C3)
            if hm >= PICK_SLOT and self.provider \
                    and "pickT%sT%s" % (d, PICK_SLOT) \
                    not in st["screens_done"]:
                try:
                    self.provider.invalidate()   # 刷新涨停池缓存
                except Exception:
                    pass
                out["pick"] = self.account.pick_top5_at_close(
                    self.provider, now=now, slot="%sT%s" % (d, PICK_SLOT))
                out["action"] = "pick"
                err = out["pick"].get("error")
                if err:
                    # F1: 选股失败 → paper 侧未登记当日幂等键, 本分支下一轮
                    # (POLL_SECONDS 后)仍会进来真重试。绝不吞掉 error 再打
                    # "env_ok=None picked=[]" 的假成功 INFO(与"今天没票"不可区分)。
                    LOG.warning("收盘选股失败: error=%s → 未占用今日幂等键, "
                                "约 %d 秒后自动重试", err, POLL_SECONDS)
                    return out
                LOG.info("收盘选股: env_ok=%s picked=%s",
                         out["pick"].get("env_ok"),
                         [p["code"] for p in out["pick"].get("picked", [])])
                # 涨停池缓存保鲜(6h 节流, daemon 线程, 不阻塞)
                self._maybe_refresh_zt()
                # 基本面快照采集(规格 §7): 选股落定后当日涨停池逐只快照,
                # daemon 线程 + 失败只落日志, 不影响选股返回与主循环
                self._maybe_fund_snapshot()
            return out
        # 开盘买入窗口(spec §5): 09:26-09:35, 消费 for_date==今日 的计划;
        # 触发轮直接返回(买优先于盯盘), 计划被消费后下轮恢复正常盯盘
        if now.weekday() < 5 and OPEN_WINDOW[0] <= hm <= OPEN_WINDOW[1] \
                and self.provider and any(
                    p.get("for_date") == d
                    for p in self.account.state.get("planned_buys", [])):
            out["open_buys"] = self.account.execute_open_buys(
                self._open_buy_ticks, now=now)
            out["action"] = "open_buys"
            LOG.info("开盘买入: bought=%s queued=%s skipped=%s",
                     out["open_buys"].get("bought"),
                     out["open_buys"].get("queued"),
                     out["open_buys"].get("skipped"))
            return out
        if not self.in_session(now) or self.provider is None:
            return out
        try:
            ticks = (self.ticks_fn or
                     (lambda: self.provider.ds.get_full_market_ticks()))()
        except Exception:
            return out              # 行情失败 → 本轮跳过, 不记账
        out["action"] = "tick"
        out["sells"] = self.account.sell_check(ticks, now=now)
        # 排板轮询(设计 §2.3): 每轮对 pending 判成交/开板撤单(每事件临界段)
        pcodes = [p["code"] for p in self.account.state.get("pending_buys", [])]
        if pcodes:
            r = self.account.check_pending_buys(self._quote_ticks(pcodes),
                                                now=now)
            out["pending_filled"] = r["filled"]
            out["pending_canceled"] = r["canceled"]
        return out

    # ---------- 缺口补算 ----------
    def _trade_days(self):
        """近30日K线日期(YYYY-MM-DD): 有持仓用持仓股, 否则上证指数。"""
        if self.provider is None:
            return []
        code = (self.account.state["holdings"][0]["code"]
                if self.account.state and self.account.state["holdings"]
                else INDEX_CODE)
        try:
            df = self.provider.ds.get_kline(code, days=30)
            days = self.account._kline_day_strs(df)
        except Exception:
            return []
        if df is None or len(df) == 0:
            return []
        return ["%s-%s-%s" % (s[:4], s[4:6], s[6:8]) for s in days]

    def backfill(self):
        """重启后缺口日补算(净值曲线无洞); 拿不到交易日 → 0。

        终审 I-2: 盘中(now < 15:00, 真实/注入时钟)重启时今日未收盘 →
        过滤今日, 今日净值点与 settled 留给 15:00 的 settle_day;
        收盘后重启保持原语义(含今日)。"""
        now = self.now_fn() if self.now_fn else datetime.now()
        days = self._trade_days()
        if now.strftime("%H:%M") < SETTLE_AFTER:
            today = now.strftime("%Y-%m-%d")
            days = [x for x in days if x != today]
        if not days:
            return 0
        return self.account.backfill_nav(self.close_fn, days, now=now)

    # ---------- 连接与主循环 ----------
    def _sleep(self, sec):
        schedule.sleep(sec, self.sleep_fn)

    def connect_provider(self, max_retry=60, retry_wait=10):
        """连接 QMT 行情数据源(与 live_daemon 同一份重试, prism/schedule.py)。"""
        return schedule.connect_provider(self, max_retry, retry_wait)

    def startup_guard(self):
        """启动守卫(§8.3, 终审 I-4): 账本存在但损坏/版本不符 → 拒绝启动,
        保留原文件不覆盖(init 也不跑); 账本缺失(可初始化)或加载正常 → True。"""
        if self.account.state_path.exists() and not self.account.load():
            logging.getLogger("paper_daemon").error(
                "账本损坏/版本不符 → 拒绝启动, 保留原文件: %s",
                self.account.state_path)
            return False
        return True

    def run_forever(self):
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s %(levelname)s %(message)s")
        log = logging.getLogger("paper_daemon")
        if not self.startup_guard():
            return
        if self.account.state is None and not self.account.load():
            log.info("账本不存在 → 初始化 100 万模拟账户")
            self.account.init_account()
        # 修正并注明: 简报蓝图顺序为 init→backfill→connect, 但 backfill 依赖
        # self.provider(_trade_days 无 provider 恒返回 []) → 生产路径缺口补算
        # 恒空转, 违背设计 §5 重启补算承诺 → 移到 connect 成功之后。
        if not self.connect_provider():
            log.error("QMT 连接失败(重试上限), 退出")
            return
        # 启动自愈: 守护停了几天再开 → 先补涨停池缓存(6h 节流内跳过)
        self._maybe_refresh_zt()
        filled = self.backfill()
        if filled:
            log.info("缺口日补算 %d 天", filled)
        # 跨日启动清理(设计 §2.4): 旧 pending 收盘失效; 撤单幂等键(I-2)按
        # 最后流水日重置——非今日 ⇒ 键属上一交易日 → 清。须先清键再失效
        # (queue_expire 流水会把"最后流水日"推到今日, 顺序反了会误保留)。
        now0 = self.now_fn() if self.now_fn else datetime.now()
        today = now0.strftime("%Y-%m-%d")
        st = self.account.state
        if (st.get("trades") or [{}])[-1].get("date", "") != today:
            st["canceled_pending_codes"] = []
        for p in list(st.get("pending_buys", [])):
            if not (p.get("created") or "").startswith(today):
                self.account._dispose_pending(p["code"], "queue_expire", now0)
        # 计划清理(spec §6): for_date<今日 的开盘买入计划作废(错过开盘窗口,
        # 记日志不追买); 今日计划保留待 09:26 窗口消费
        stale = [p for p in st.get("planned_buys", [])
                 if p.get("for_date", "") < today]
        if stale:
            st["planned_buys"] = [p for p in st["planned_buys"]
                                  if p.get("for_date", "") >= today]
            self.account.save()
            log.info("计划清理: 作废 %d 只(错过开盘窗口)", len(stale))
        log.info("模拟盘守护启动(策略=%s, 100万, 每%d秒一轮, fee_rate=%s "
                 "min_commission=%s)", self.account.strategy.get("id"),
                 POLL_SECONDS, self.account.fee_rate,
                 self.account.min_commission)
        while True:
            try:
                out = self.tick_once()
                log.info("tick %s", {k: (len(v) if isinstance(v, list) else v)
                                     for k, v in out.items()})
            except Exception as e:
                log.exception("tick 异常(忽略, 下轮重试): %r", e)
            self._sleep(POLL_SECONDS)


def main():
    daemon = PaperDaemon(PaperAccount())
    daemon.run_forever()


if __name__ == "__main__":
    main()
