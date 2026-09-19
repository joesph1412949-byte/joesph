"""告警规则评估(纯函数, 无 IO)。规则未配置时用内置默认值。"""
from __future__ import annotations
from datetime import datetime
from typing import Iterator

_DEFAULTS = {
    "position_ratio": {"max": 0.30},
    "daily_loss": {"max_loss_pct": 0.03},
    "stop_loss": {"drop_pct": 0.08},
    # I2: 无有效数据的容忍时长。默认 60s = 12 个默认轮询周期(poll_interval_s=5),
    # 足以排除单轮查询抖动, 又不至于让断线/停摆被忽视。盘后 QMT 未登录若嫌吵,
    # 在现成的 alert_rules.json 里写 {"data_gap": {"enabled": false}} 关掉, 不新增配置机制。
    "data_gap": {"max_silence_s": 60},
}


def _rule(rules: dict, name: str) -> dict:
    r = dict(_DEFAULTS.get(name, {}))
    if isinstance(rules.get(name), dict):
        r.update(rules[name])
    return r


def _enabled(r: dict) -> bool:
    return r.get("enabled", True)  # 未显式关 => 默认开


def evaluate_asset_alerts(asset, prev_day_asset, rules: dict) -> Iterator[tuple[str, str, str]]:
    """资产级告警: 当日资产跌幅。返回 (rule, stock_code, message)。"""
    r = _rule(rules, "daily_loss")
    if not _enabled(r) or not prev_day_asset:
        return
    prev_total = float(prev_day_asset.get("total_asset") or 0.0)
    if prev_total <= 0:
        return
    chg = (asset.total_asset - prev_total) / prev_total
    if chg < -float(r["max_loss_pct"]):
        yield ("daily_loss", "", "当日资产跌幅 {:.1%} 超过阈值 {:.1%}".format(chg, -float(r["max_loss_pct"])))


def evaluate_position_alerts(asset, positions, prev_codes, rules: dict,
                             basis: str = "") -> Iterator[tuple[str, str, str]]:
    """持仓级告警: 单票占比 / 止损线 / 持仓变化。

    basis: 持仓**全空**时"清仓"的判据来源说明(由 sync 层传入 —— 只有那里知道资产侧市值)。
    只在 positions 为空时拼进文案: 将来真出现假告警, 用户一眼看得出判据在哪。
    """
    total = asset.total_asset or 0.0

    rr = _rule(rules, "position_ratio")
    if _enabled(rr) and total > 0:
        mx = float(rr["max"])
        for p in positions:
            ratio = (p.market_value or 0.0) / total
            if ratio > mx:
                yield ("position_ratio", p.stock_code,
                       "{} 市值占比 {:.1%} 超过阈值 {:.1%}".format(p.stock_code, ratio, mx))

    sr = _rule(rules, "stop_loss")
    if _enabled(sr):
        dp = float(sr["drop_pct"])
        for p in positions:
            cur = (p.market_value or 0.0) / p.volume if p.volume else 0.0
            cost = p.open_price or 0.0
            if cost > 0 and cur > 0:
                drop = (cur - cost) / cost
                if drop < -dp:
                    yield ("stop_loss", p.stock_code,
                           "{} 现价较成本 {:.1%} 跌破阈值 {:.1%}".format(p.stock_code, drop, -dp))

    cr = _rule(rules, "position_change")
    if _enabled(cr) and prev_codes is not None:
        cur_codes = {p.stock_code for p in positions}
        for c in sorted(cur_codes - prev_codes):
            yield ("position_change", c, "新开仓 {}".format(c))
        for c in sorted(prev_codes - cur_codes):
            msg = "清仓 {}".format(c)
            if basis and not positions:  # 全空轮: 注明判据来源
                msg += "(依据: {})".format(basis)
            yield ("position_change", c, msg)


def evaluate_gap_alert(last_ok_ts, now_ts, rules: dict) -> Iterator[tuple[str, str, str]]:
    """I2 数据缺口: 距上次"资产行真的落库"超过 max_silence_s 即告警。

    阈值依据见 _DEFAULTS["data_gap"]。节流交给 insert_alert 的 60 分钟去重窗, 不新建表。
    last_ok_ts 缺失或时间戳不可解析时静默返回(不误报, 也不抛错打断轮询)。
    """
    r = _rule(rules, "data_gap")
    if not _enabled(r) or not last_ok_ts:
        return
    try:
        gap = (datetime.strptime(str(now_ts), "%Y-%m-%d %H:%M:%S")
               - datetime.strptime(str(last_ok_ts), "%Y-%m-%d %H:%M:%S")).total_seconds()
    except (TypeError, ValueError):
        return
    mx = float(r["max_silence_s"])
    if gap > mx:
        yield ("data_gap", "",
               "已 {:.0f}s 无有效数据(阈值 {:.0f}s): qmt_sync 可能停摆或 QMT 断线".format(gap, mx))
