# -*- coding: utf-8 -*-
"""做T策略配置层: 加载 + 深合并默认值 + 校验。

设计要点:
  - 默认值内联在 DEFAULT_CONFIG, 配置文件只写"想覆盖的部分";
  - 校验 fail-closed: 配置非法宁可抛 ConfigError 也不带着坏参数交易
    (坏 band / 坏权重 / 未知代码 都会让风控形同虚设);
  - 不做任何 IO 之外的副作用, 便于测试注入 dict。
"""
import json
import copy
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).parent / "tt_config.json"

DEFAULT_CONFIG = {
    "version": 1,
    "env": "real",
    "dry_run": True,
    "account_id": "",
    "paper_total_asset": 500000.0,   # 账户不可用时的纸面推演基数(仅演示/演练)
    "paper_positions": {             # 纸面底仓(可卖股数), 仅演示用
        "600900.SH": 1000,
        "600938.SH": 700,
        "601088.SH": 300,
        "603268.SH": 100
    },
    "max_units_per_round": 2,        # 单轮单标的最多补几档(防一轮打满)
    "grid": {
        "band_mode": "sigma",      # sigma=日波动率×k | fixed=直接用 band_pct
        "band_k": 1.0,
        "n_units": 5,              # 阶梯深度 + 每档金额分母(总资产×weight÷n_units)
        "max_units": 5,            # 日内实际使用的最大档数(≤n_units); 底仓容量决定
        "ref_mode": "prev_close",  # prev_close=前收 | open=当日开盘
        "sigma_window": 60,
    },
    "session": {
        "open_start": "09:30",
        "open_end": "14:55",
        "converge_after": "14:30",
        "hard_stop_after": "14:57",
    },
    "risk": {
        "max_single_order_amount": 50000,
        "max_daily_trades": 20,
        "max_daily_loss": 3000,
        "max_price_deviation_pct": 0.05,
        "max_position_pct": 0.20,
        "max_net_buy_today_ratio": 0.0,
        "max_consecutive_failures": 3,
        "max_slippage_pct": 0.03,
    },
    # symbols 无默认值: 空标的池 = 空跑, 属于合法配置
    "symbols": [],
}

# 风控阈值的安全上界(超过即视为笔误 → 拒绝加载)
_RISK_BOUNDS = {
    "max_single_order_amount": (100, 10_000_000),
    "max_daily_trades": (1, 500),
    "max_daily_loss": (10, 10_000_000),
    "max_price_deviation_pct": (0.001, 0.30),
    "max_position_pct": (0.01, 1.0),
    "max_net_buy_today_ratio": (0.0, 1.0),
    "max_consecutive_failures": (1, 50),
    "max_slippage_pct": (0.0, 0.20),
}


class ConfigError(ValueError):
    """配置非法。"""


def _deep_merge(base, override):
    """递归合并: override 的叶子覆盖 base, dict 逐键下沉。返回新对象。"""
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _norm_time(s):
    """'9:30' -> '09:30'; 非法 -> None。"""
    try:
        h, m = str(s).strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return "%02d:%02d" % (h, m)
    except (ValueError, AttributeError):
        return None


def validate(cfg):
    """校验配置, 非法抛 ConfigError。返回归一化后的 cfg(原地规范化时间字段)。"""
    if not isinstance(cfg, dict):
        raise ConfigError("配置根必须是 dict")

    # ---- 顶层数值 ----
    try:
        cfg["paper_total_asset"] = float(cfg.get("paper_total_asset") or 0)
        cfg["max_units_per_round"] = int(cfg.get("max_units_per_round") or 1)
    except (TypeError, ValueError):
        raise ConfigError("paper_total_asset / max_units_per_round 必须为数值")
    if cfg["paper_total_asset"] < 0:
        raise ConfigError("paper_total_asset 不能为负")
    if not (1 <= cfg["max_units_per_round"] <= 20):
        raise ConfigError("max_units_per_round 应在 [1, 20]")

    pp = cfg.get("paper_positions") or {}
    if not isinstance(pp, dict):
        raise ConfigError("paper_positions 必须是对象 {代码: 股数}")
    clean_pp = {}
    for k, v in pp.items():
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise ConfigError("paper_positions[%s] 必须为整数股数" % k)
        if n < 0:
            raise ConfigError("paper_positions[%s] 不能为负" % k)
        clean_pp[str(k)] = n
    cfg["paper_positions"] = clean_pp

    # ---- env (信号通道) ----
    # real = 实盘通道(D:/QMT_SIGNALS/real), sim = QMT 模拟通道(D:/QMT_SIGNALS/sim)。
    # 注意: 这只决定"写到哪个队列", 不决定桥端连的是哪个账户 ——
    # qmt_signal_bridge_demo.py 下单用的是 QMT 当前登录账户。
    env = str(cfg.get("env") or "real").strip().lower()
    if env not in ("real", "sim"):
        raise ConfigError("env 必须是 real 或 sim, 当前 %r" % cfg.get("env"))
    cfg["env"] = env

    # ---- session ----
    sess = cfg.setdefault("session", {})
    for key in ("open_start", "open_end", "converge_after", "hard_stop_after"):
        norm = _norm_time(sess.get(key))
        if norm is None:
            raise ConfigError("session.%s 时间非法: %r" % (key, sess.get(key)))
        sess[key] = norm
    if sess["open_start"] >= sess["open_end"]:
        raise ConfigError("session.open_start 必须早于 open_end")
    if sess["converge_after"] > sess["hard_stop_after"]:
        raise ConfigError("session.converge_after 不能晚于 hard_stop_after")

    # ---- grid ----
    grid = cfg.setdefault("grid", {})
    if grid.get("band_mode") not in ("sigma", "fixed"):
        raise ConfigError("grid.band_mode 必须是 sigma 或 fixed")
    if grid.get("ref_mode") not in ("prev_close", "open"):
        raise ConfigError("grid.ref_mode 必须是 prev_close 或 open")
    try:
        k = float(grid.get("band_k"))
        n = int(grid.get("n_units"))
        win = int(grid.get("sigma_window"))
    except (TypeError, ValueError):
        raise ConfigError("grid.band_k / n_units / sigma_window 必须为数值")
    if not (0.1 <= k <= 5.0):
        raise ConfigError("grid.band_k 应在 [0.1, 5.0], 当前 %r" % k)
    if not (1 <= n <= 20):
        raise ConfigError("grid.n_units 应在 [1, 20], 当前 %r" % n)
    if not (20 <= win <= 250):
        raise ConfigError("grid.sigma_window 应在 [20, 250], 当前 %r" % win)
    grid["band_k"], grid["n_units"], grid["sigma_window"] = k, n, win

    # grid.max_units: 日内实际使用的最大档位数(缺省=n_units)。
    # 存在的理由: n_units 同时决定「阶梯深度」与「每档金额的分母」
    # (unit_value = 总资产×weight÷n_units)。当底仓只够 3 档却想让每档
    # 金额维持 1/5 时, 把 n_units 留 5、max_units 设 3 即可 —— 两个语义解耦。
    mu = grid.get("max_units")
    if mu is None:
        mu = n
    else:
        try:
            mu = int(mu)
        except (TypeError, ValueError):
            raise ConfigError("grid.max_units 必须为整数")
    if not (1 <= mu <= n):
        raise ConfigError("grid.max_units 应在 [1, %d](即不超过 n_units), 当前 %r"
                          % (n, mu))
    grid["max_units"] = mu

    # ---- risk ----
    risk = cfg.setdefault("risk", {})
    for name, (lo, hi) in _RISK_BOUNDS.items():
        try:
            v = float(risk.get(name))
        except (TypeError, ValueError):
            raise ConfigError("risk.%s 必须为数值, 当前 %r" % (name, risk.get(name)))
        if not (lo <= v <= hi):
            raise ConfigError("risk.%s 越界 [%s, %s], 当前 %r" % (name, lo, hi, v))
        risk[name] = v
    risk["max_daily_trades"] = int(risk["max_daily_trades"])
    risk["max_consecutive_failures"] = int(risk["max_consecutive_failures"])
    risk["max_single_order_amount"] = float(risk["max_single_order_amount"])

    # ---- symbols ----
    syms = cfg.get("symbols")
    if not isinstance(syms, list):
        raise ConfigError("symbols 必须是数组")
    seen = set()
    for i, s in enumerate(syms):
        if not isinstance(s, dict):
            raise ConfigError("symbols[%d] 必须是对象" % i)
        code = str(s.get("code") or "").strip()
        if "." not in code:
            raise ConfigError("symbols[%d].code 必须带市场后缀(如 600900.SH): %r"
                              % (i, code))
        if code in seen:
            raise ConfigError("symbols 中代码重复: %s" % code)
        seen.add(code)
        s["code"] = code
        s["name"] = str(s.get("name") or code)
        s["enabled"] = bool(s.get("enabled", True))
        try:
            s["weight"] = float(s.get("weight", 0.0))
            s["band_pct"] = float(s.get("band_pct", 0.0))
            s["n_units"] = int(s.get("n_units") or grid["n_units"])
        except (TypeError, ValueError):
            raise ConfigError("symbols[%d] weight/band_pct/n_units 必须为数值" % i)
        if not (0.0 <= s["weight"] <= 1.0):
            raise ConfigError("symbols[%d].weight 应在 [0,1]" % i)
        if s["band_pct"] < 0:
            raise ConfigError("symbols[%d].band_pct 不能为负" % i)
        if not (1 <= s["n_units"] <= 20):
            raise ConfigError("symbols[%d].n_units 应在 [1,20]" % i)
        # 未给 band_pct 且 band_mode=fixed → 无带宽, 该标的无法做T
        if grid["band_mode"] == "fixed" and s["band_pct"] <= 0:
            raise ConfigError("band_mode=fixed 时 symbols[%d].band_pct 必须 > 0" % i)

        # ---- 交叉校验: 最深"实际使用档"的固有偏离须仍在偏离中枢闸门内 ----
        # 第 n 档挂单价 = ref*(1 ± band*n) → 固有偏离 = n*band。
        # 若 max_units*band > max_price_deviation_pct, 深档会被 DEVIATION_TOO_BIG
        # 静默拦掉(功能缺失而非安全), 故 fail-closed 报错, 强制人显式调参。
        # 用 max_units(实际用几档) 而非 n_units(阶梯算几档), 后者可大于前者。
        # 单位: band_pct 是百分数(0.53 = 0.53%), 换算成小数再比。
        if grid["band_mode"] == "fixed":
            worst_band = s["band_pct"] / 100.0
            eff_n = min(s["n_units"], grid["max_units"])
            if worst_band > 0:
                worst_dev = eff_n * worst_band
                if worst_dev > risk["max_price_deviation_pct"] + 1e-9:
                    raise ConfigError(
                        "symbols[%d] %s: 实际档数(%d) × band(%.2f%%) = %.2f%% 超过 "
                        "risk.max_price_deviation_pct(%.2f%%) → 第 %d 档起会被"
                        "偏离闸门静默拦下。请降 grid.max_units 或收紧 band_pct, "
                        "或放宽该闸门"
                        % (i, code, eff_n, s["band_pct"], worst_dev * 100,
                           risk["max_price_deviation_pct"] * 100,
                           min(eff_n,
                               int(risk["max_price_deviation_pct"]
                                   / worst_band) + 1)))
        # band_mode=sigma 时带宽由日波动率×band_k 现算, 无法在配置期静态判定,
        # 由 engine 在算完 meta['band'] 后做同口径运行时校验(见 engine._make_intent)。

    return cfg


def load(path=None, overrides=None):
    """加载配置: 文件 → 深合并默认值 → 应用 overrides → 校验。"""
    path = Path(path or DEFAULT_CONFIG_PATH)
    raw = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as e:
            raise ConfigError("读取配置失败 %s: %r" % (path, e))
    cfg = _deep_merge(DEFAULT_CONFIG, raw)
    if overrides:
        cfg = _deep_merge(cfg, overrides)
    return validate(cfg)


def enabled_symbols(cfg):
    """返回启用中的标的列表。"""
    return [s for s in cfg.get("symbols", []) if s.get("enabled")]


def symbol_by_code(cfg, code):
    for s in cfg.get("symbols", []):
        if s["code"] == code:
            return s
    return None
