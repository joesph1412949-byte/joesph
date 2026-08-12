"""告警规则评估(纯函数, 无 IO)。规则未配置时用内置默认值。"""
from __future__ import annotations
from typing import Iterator

_DEFAULTS = {
    "position_ratio": {"max": 0.30},
    "daily_loss": {"max_loss_pct": 0.03},
    "stop_loss": {"drop_pct": 0.08},
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


def evaluate_position_alerts(asset, positions, prev_codes, rules: dict) -> Iterator[tuple[str, str, str]]:
    """持仓级告警: 单票占比 / 止损线 / 持仓变化。"""
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
            yield ("position_change", c, "清仓 {}".format(c))
