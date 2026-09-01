# -*- coding: utf-8 -*-
"""模拟盘守护进程: 盘中实时盯盘 + 三时点选股 + 收盘结算 + 缺口补算。

用法: python -m prism.paper_daemon
安全(设计 §8): 本进程绝不写 D:\\QMT_SIGNALS、不调用任何下单接口——纯记账;
实盘进程零影响; 行情/K线只读; 单轮异常不影响下一轮。"""
import logging
import time
from datetime import datetime

from prism.paper import PaperAccount

SCREEN_TIMES = ("10:00", "13:30", "14:30")
SETTLE_AFTER = "15:00"
POLL_SECONDS = 5
INDEX_CODE = "000001.SH"          # 上证指数: 交易日历来源


class PaperDaemon:
    """调度器: 每 POLL_SECONDS 一轮; 时点选股/收盘结算由 tick_once 触发。"""

    def __init__(self, account, ticks_fn=None, sleep_fn=None, now_fn=None):
        self.account = account
        self.provider = None
        self.ticks_fn = ticks_fn
        self.sleep_fn = sleep_fn
        self.now_fn = now_fn

    # ---------- 时段 ----------
    def in_session(self, now):
        """交易日时段: 周一~五 ∧ (09:30-11:30 ∨ 13:00-15:00)。"""
        if now.weekday() >= 5:
            return False
        hm = now.strftime("%H:%M")
        return ("09:30" <= hm <= "11:30") or ("13:00" <= hm <= "15:00")

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
        >= max_hold_days, 与回测/实盘 ExitRule 同口径;
        K线 bar 数(_due_by_kline)仅作数据缺失兜底, 不再走 daemon 默认路径。"""
        try:
            held = (datetime.strptime(day_str, "%Y-%m-%d")
                    - datetime.strptime(str(buy_date), "%Y-%m-%d")).days
        except (ValueError, TypeError):
            return False                # 日期坏值 → 不到期(保守)
        return held >= self.account.max_hold_days()

    # ---------- 单轮 ----------
    def tick_once(self, now=None):
        """一轮: 15:00后未结算→结算(幂等); 盘中→tick卖检查+到点选股; 其余 idle。

        行情异常/账本未初始化 → 本轮跳过不记账(设计 §8/§11)。"""
        now = now or (self.now_fn() if self.now_fn else datetime.now())
        out = {"action": "idle", "sells": [], "buys": [], "settle": None}
        if self.account.state is None and not self.account.load():
            return out
        d = now.strftime("%Y-%m-%d")
        hm = now.strftime("%H:%M")
        if now.weekday() < 5 and hm >= SETTLE_AFTER:    # M-e: 周末不结算不记平点
            # ponytail: 周中节假日仍会记平点, 完整解决需交易日历
            st = self.account.state
            if d not in st.get("settled_dates", []) and self.provider:
                # 终审 I-3: 到期按自然日口径, 闭包捕获本轮结算日 d
                # (due_fn 形参为 (code, buy_date), 结算日经 _due_natural 注入)
                out["settle"] = self.account.settle_day(
                    self.close_fn, due_fn=lambda c, bd: self._due_natural(bd, d),
                    provider=self.provider, now=now)
                out["action"] = "settle"
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
        for st_time in SCREEN_TIMES:
            if hm >= st_time and "%sT%s" % (d, st_time) \
                    not in self.account.state["screens_done"]:
                try:
                    self.provider.invalidate()   # 刷新涨停池缓存
                except Exception:
                    pass
                out["buys"].append(
                    # 终审 I-1: 幂等键=计划时点(slot), 14:00 重启补跑 10:00/
                    # 13:30 后各记一次, 不再按"实际分钟"重复选股
                    self.account.buy_from_screen(
                        self.provider, now=now, slot="%sT%s" % (d, st_time)))
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
        if self.sleep_fn:
            self.sleep_fn(sec)
        else:
            time.sleep(sec)

    def connect_provider(self, max_retry=60, retry_wait=10):
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
        filled = self.backfill()
        if filled:
            log.info("缺口日补算 %d 天", filled)
        log.info("模拟盘守护启动(策略=%s, 100万, 每%d秒一轮)",
                 self.account.strategy.get("id"), POLL_SECONDS)
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
