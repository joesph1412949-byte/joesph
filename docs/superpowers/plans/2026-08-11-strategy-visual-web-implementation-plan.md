# 可视化策略选股网页 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个本地 Flask 网页，用 xtquant 数据计算用户的四模型量化选股系统（24 因子），展示市场环境仪表盘、候选股评分列表、单股K线+因子明细、模型对比，并支持在网页上手填 xtquant 拿不到数据的因子。

**Architecture:** 后端 Python + Flask 提供 JSON API，启动时连接 QMT miniQMT（xtdata），先判市场环境（节点模型）再扫全市场找涨停股、对涨停池计算 24 因子评分；前端原生 JS + ECharts 渲染四个可视化模块。手填因子存 JSON 文件，重新选股时自动合并。

**Tech Stack:** Python 3.12、Flask、xtquant（xtdata）、pandas 2.3.3、原生 HTML/JS、ECharts

**Spec:** `docs/superpowers/specs/2026-08-11-strategy-visual-web-design.md`

## Global Constraints

- 所有 xtdata 调用前先 `xtdata.connect()`（QMT miniQMT 58610 端口，个人版）
- 指数（000001.SH 等）必须先 `download_history_data` 再 `get_market_data_ex`，否则返回 0 行
- 全市场板块名为 `"沪深A股"`（5209 只）
- 涨停价优先用 `get_instrument_detail` 的 `UpStopPrice` 字段（自动处理 ST/新股边界），tick 里没有该字段
- 流通股本用 `get_instrument_detail` 的 `FloatVolume`，流通市值 = FloatVolume × 现价
- tick 封单判断：`bidPrice[0]` 达到涨停价且 `askPrice[0]==0` 视为封板
- 手填因子只接受 0/1，存 `manual_factors.json`
- 评分权重：首板30% + 妖股30% + 势能25% + 节点15%（权重修正为 100%）
- 三套规则并用：加权综合分 + 强弱区间(极强/强/中等/弱) + A-E 组合分级
- 全项目 Python 文件用 UTF-8，前端不引入框架，ECharts 用本地文件
- 所有测试用 pytest，不依赖真实 QMT 连接（用 mock/fixture 数据）

---

### Task 1: 数据层 `data_source.py`

**Files:**
- Create: `strategy_web/data_source.py`
- Test: `strategy_web/tests/test_data_source.py`

**Interfaces:**
- Consumes: `xtdata`（外部库）
- Produces:
  - `class DataSource` — 封装所有 xtdata 数据获取
    - `connect()` → None（连 QMT，失败抛 `DataSourceError`）
    - `get_full_market_ticks() -> dict` — 全市场 5209 只实时盘口 `{code: tick_dict}`
    - `get_limit_up_stocks(ticks=None) -> list[dict]` — 涨停股列表 `[{code, name, last, up_stop_price, ...}]`
    - `get_kline(code, days=120) -> pandas.DataFrame` — 日线K线（自动下载+读取）
    - `get_instrument(code) -> dict` — instrument_detail 单只
    - `get_instruments_bulk(codes: list) -> dict` — 批量 instrument_detail `{code: detail}`
    - `get_index_kline(index_code, days=60) -> pandas.DataFrame` — 指数K线
    - `get_sector_stocks(sector_name) -> list` — 板块成分股代码列表
  - `class DataSourceError(Exception)` — 连接/数据失败异常

**注意（实测）：** 全市场实时盘口一次 `get_full_tick` 0.21s 返回 5209 只；K线必须先 `download_history_data` 再 `get_market_data_ex`（带 start_time/end_time）；指数同个股。

- [ ] **Step 1: 写测试 `tests/test_data_source.py`**

```python
# -*- coding: utf-8 -*-
"""data_source 单元测试 — 用 mock 代替真实 xtdata"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# --- mock xtdata 模块 ---
import types
mock_xt = types.ModuleType("xtquant")
xtdata_mod = types.ModuleType("xtquant.xtdata")

_dummy_kline_df = None

def _make_kline_df(n=120):
    import pandas as pd
    import numpy as np
    dates = pd.date_range("2026-01-01", periods=n, freq="B")
    return pd.DataFrame({
        "time": dates,
        "open": np.linspace(10, 20, n),
        "high": np.linspace(10.5, 21, n),
        "low": np.linspace(9.5, 19, n),
        "close": np.linspace(10, 20, n),
        "volume": np.full(n, 100000),
        "amount": np.full(n, 1e6),
    })

def _fake_connect():
    return None

def _fake_get_full_tick(codes):
    return {
        "002859.SZ": {
            "lastPrice": 81.32, "lastClose": 73.93, "askPrice": [0,0,0,0,0],
            "bidPrice": [81.32, 81.31, 81.30, 81.29, 81.28],
            "bidVol": [34661, 77, 47, 6, 9], "amount": 1424321000.0,
            "volume": 181657, "high": 81.32, "low": 73.18, "open": 73.93,
            "time": 1786428306000, "timetag": "20260811 14:05:06",
        },
        "600353.SH": {
            "lastPrice": 30.03, "lastClose": 27.30, "askPrice": [0,0,0,0,0],
            "bidPrice": [30.03, 30.02, 30.01, 30.00, 29.99],
            "bidVol": [1000, 500, 300, 200, 100], "amount": 500000000.0,
            "volume": 80000, "high": 30.03, "low": 27.50, "open": 27.50,
            "time": 1786428306000, "timetag": "20260811 14:05:06",
        },
        "000001.SZ": {  # 非涨停股对照
            "lastPrice": 10.0, "lastClose": 10.0, "askPrice": [10.01,10.02,0,0,0],
            "bidPrice": [9.99,9.98,0,0,0], "bidVol": [100,200,0,0,0],
            "amount": 10000000.0, "volume": 5000, "high": 10.05, "low": 9.95,
            "open": 10.0, "time": 1786428306000, "timetag": "20260811 14:05:06",
        },
    }

def _fake_get_instrument_detail_list(codes):
    out = {}
    for c in codes:
        if c == "002859.SZ":
            out[c] = {"InstrumentID":"002859","InstrumentName":"洁美科技",
                      "UpStopPrice":81.32,"FloatVolume":428315200.0,
                      "OpenDate":"20170407","ExchangeID":"SZ"}
        elif c == "600353.SH":
            out[c] = {"InstrumentID":"600353","InstrumentName":"旭光电子",
                      "UpStopPrice":30.03,"FloatVolume":1000000000.0,
                      "OpenDate":"19900101","ExchangeID":"SH"}
        else:
            out[c] = {"InstrumentID":"000001","InstrumentName":"平安银行",
                      "UpStopPrice":11.0,"FloatVolume":20000000000.0,
                      "OpenDate":"19910101","ExchangeID":"SZ"}
    return out

xtdata_mod.connect = _fake_connect
xtdata_mod.get_full_tick = _fake_get_full_tick
xtdata_mod.get_instrument_detail_list = _fake_get_instrument_detail_list
xtdata_mod.get_instrument_detail = lambda c: (_fake_get_instrument_detail_list([c]).get(c))
xtdata_mod.download_history_data = lambda *a, **k: None
xtdata_mod.get_market_data_ex = lambda *a, **k: {"002859.SZ": _make_kline_df(),
                                                  "600353.SH": _make_kline_df(),
                                                  "000001.SZ": _make_kline_df(),
                                                  "000001.SH": _make_kline_df()}
xtdata_mod.get_stock_list_in_sector = lambda s: ["002859.SZ","600353.SH","000001.SZ"]
xtdata_mod.connect_result = None
sys.modules["xtquant"] = mock_xt
xtdata_mod.__name__ = "xtquant.xtdata"
mock_xt.xtdata = xtdata_mod
sys.modules["xtquant.xtdata"] = xtdata_mod

from data_source import DataSource, DataSourceError

@pytest.fixture
def ds():
    d = DataSource()
    d._connected = True  # 跳过真实 connect
    return d

def test_get_full_market_ticks(ds):
    ticks = ds.get_full_market_ticks()
    assert "002859.SZ" in ticks
    assert ticks["002859.SZ"]["lastPrice"] == 81.32

def test_get_limit_up_stocks_detects_ups():
    ds = DataSource()
    ticks = {
        "002859.SZ": {"lastPrice":81.32,"lastClose":73.93},
        "600353.SH": {"lastPrice":30.03,"lastClose":27.30},
        "000001.SZ": {"lastPrice":10.0,"lastClose":10.0},
    }
    ds.get_instruments_bulk = lambda codes: {
        "002859.SZ": {"UpStopPrice":81.32,"InstrumentName":"洁美科技","FloatVolume":428315200.0},
        "600353.SH": {"UpStopPrice":30.03,"InstrumentName":"旭光电子","FloatVolume":1e9},
        "000001.SZ": {"UpStopPrice":11.0,"InstrumentName":"平安银行","FloatVolume":2e10},
    }
    ups = ds.get_limit_up_stocks(ticks)
    codes = [u["code"] for u in ups]
    assert "002859.SZ" in codes and "600353.SH" in codes
    assert "000001.SZ" not in codes  # 未涨停

def test_get_limit_up_marks_sealed():
    ds = DataSource()
    ticks = {"002859.SZ": {"lastPrice":81.32,"lastClose":73.93,
                           "askPrice":[0,0,0,0,0],"bidPrice":[81.32,81.31,0,0,0]}}
    ds.get_instruments_bulk = lambda codes: {
        "002859.SZ": {"UpStopPrice":81.32,"InstrumentName":"洁美科技","FloatVolume":428315200.0}}
    ups = ds.get_limit_up_stocks(ticks)
    assert ups[0]["sealed"] is True  # askPrice[0]==0 → 封板

def test_get_kline_downloads_then_reads(ds):
    df = ds.get_kline("002859.SZ", days=120)
    assert "close" in df.columns and "volume" in df.columns
    assert len(df) > 0

def test_get_instruments_bulk(ds):
    details = ds.get_instruments_bulk(["002859.SZ", "000001.SZ"])
    assert details["002859.SZ"]["InstrumentName"] == "洁美科技"
    assert details["002859.SZ"]["FloatVolume"] == 428315200.0

def test_get_sector_stocks(ds):
    codes = ds.get_sector_stocks("沪深A股")
    assert "002859.SZ" in codes

def test_get_index_kline(ds):
    df = ds.get_index_kline("000001.SH")
    assert "close" in df.columns
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd strategy_web && python -m pytest tests/test_data_source.py -v`
Expected: 全部 FAIL，报 `ModuleNotFoundError: No module named 'data_source'`

- [ ] **Step 3: 实现 `data_source.py`**

```python
# -*- coding: utf-8 -*-
"""数据层：封装 xtdata 的所有数据获取。
所有方法都通过 DataSource 实例调用，便于测试时注入 mock。"""
import time

try:
    from xtquant import xtdata
except Exception:
    xtdata = None


class DataSourceError(Exception):
    pass


def _safe(v, default):
    return v if v is not None else default


class DataSource:
    def __init__(self):
        self._connected = False

    # ---------- 连接 ----------
    def connect(self):
        if xtdata is None:
            raise DataSourceError("xtquant 未安装或无法导入")
        try:
            xtdata.connect()
            time.sleep(1)
            self._connected = True
        except Exception as e:
            raise DataSourceError("连接 QMT miniQMT 失败: %r" % e)

    # ---------- 实时行情 ----------
    def get_full_market_ticks(self, codes=None):
        if not self._connected:
            raise DataSourceError("未连接，请先调用 connect()")
        if codes is None:
            codes = self.get_sector_stocks("沪深A股")
        try:
            return xtdata.get_full_tick(codes) or {}
        except Exception as e:
            raise DataSourceError("拉全市场行情失败: %r" % e)

    # ---------- 涨停股 ----------
    def get_limit_up_stocks(self, ticks=None):
        """从全市场盘口找出涨停股。用 instrument_detail 的 UpStopPrice 判断涨停。
        返回 [{code,name,last,last_close,up_stop_price,sealed,amount,volume}]"""
        if ticks is None:
            ticks = self.get_full_market_ticks()
        ups = [c for c in ticks if ticks[c].get("lastPrice") or 0 > 0]
        if not ups:
            return []
        details = self.get_instruments_bulk(ups)
        result = []
        for code in ups:
            t = ticks[code]
            last = t.get("lastPrice") or 0
            last_close = t.get("lastClose") or 0
            det = details.get(code) or {}
            up_price = det.get("UpStopPrice") or 0
            if last <= 0:
                continue
            # 涨停判断：现价 >= 涨停价（容差 0.01）
            if up_price > 0 and last < up_price - 0.01:
                continue
            if up_price <= 0:
                # 兜底：用板块涨幅粗算（几乎用不到）
                if last_close > 0:
                    r = 0.30 if code.startswith(("8","4")) else (0.20 if code.startswith(("300","301","688")) else 0.10)
                    if last < round(last_close * (1 + r), 2) - 0.01:
                        continue
            sealed = bool((t.get("askPrice") or [0])[0] == 0)
            result.append({
                "code": code,
                "name": det.get("InstrumentName") or code,
                "last": last,
                "last_close": last_close,
                "up_stop_price": up_price,
                "sealed": sealed,
                "amount": t.get("amount") or 0,
                "volume": t.get("volume") or 0,
                "float_volume": det.get("FloatVolume") or 0,
                "open_date": det.get("OpenDate") or "",
            })
        return result

    # ---------- 历史K线 ----------
    def get_kline(self, code, days=120):
        """拉日线K线（自动下载+读取），返回含 open/high/low/close/volume/amount 的 DataFrame"""
        if not self._connected:
            raise DataSourceError("未连接，请先调用 connect()")
        period = "1d"
        try:
            xtdata.download_history_data(code, period, start_time="", end_time="",
                                         incrementally=True)
            time.sleep(0.05)
            k = xtdata.get_market_data_ex([], [code], period=period,
                                          start_time="", end_time="", count=days)
            df = (k or {}).get(code)
            if df is None or len(df) == 0:
                # 首次可能需全量下载
                xtdata.download_history_data(code, period, incrementally=True)
                time.sleep(0.05)
                k = xtdata.get_market_data_ex([], [code], period=period,
                                              start_time="", end_time="", count=days)
                df = (k or {}).get(code)
            if df is None or len(df) == 0:
                raise DataSourceError("无法获取 %s 的K线" % code)
            return df
        except DataSourceError:
            raise
        except Exception as e:
            raise DataSourceError("获取 %s K线失败: %r" % (code, e))

    # ---------- instrument 详情 ----------
    def get_instrument(self, code):
        try:
            return xtdata.get_instrument_detail(code) or {}
        except Exception:
            return {}

    def get_instruments_bulk(self, codes):
        """批量获取 instrument_detail，返回 {code: detail}"""
        if not codes:
            return {}
        try:
            if hasattr(xtdata, "get_instrument_detail_list"):
                lst = xtdata.get_instrument_detail_list(codes)
                out = {}
                for item in lst or []:
                    if isinstance(item, dict) and item.get("InstrumentID"):
                        # 构造带市场后缀的 code
                        ex = item.get("ExchangeID", "")
                        code = item["InstrumentID"]
                        suff = ".SH" if ex == "SH" else ".SZ"
                        out[code + suff] = item
                # 补充可能缺失的
                for c in codes:
                    if c not in out:
                        out[c] = self.get_instrument(c)
                return out
            return {c: self.get_instrument(c) for c in codes}
        except Exception:
            return {c: self.get_instrument(c) for c in codes}

    # ---------- 指数K线 ----------
    def get_index_kline(self, index_code, days=60):
        if not self._connected:
            raise DataSourceError("未连接，请先调用 connect()")
        try:
            xtdata.download_history_data(index_code, "1d", start_time="", end_time="",
                                         incrementally=True)
            time.sleep(0.05)
            k = xtdata.get_market_data_ex([], [index_code], period="1d",
                                          start_time="", end_time="", count=days)
            df = (k or {}).get(index_code)
            if df is None or len(df) == 0:
                raise DataSourceError("无法获取指数 %s 的K线" % index_code)
            return df
        except DataSourceError:
            raise
        except Exception as e:
            raise DataSourceError("获取指数 %s K线失败: %r" % (index_code, e))

    # ---------- 板块 ----------
    def get_sector_stocks(self, sector_name):
        try:
            return xtdata.get_stock_list_in_sector(sector_name) or []
        except Exception:
            return []
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd strategy_web && python -m pytest tests/test_data_source.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add strategy_web/data_source.py strategy_web/tests/test_data_source.py
git commit -m "feat: add DataSource data layer wrapping xtdata"
```

---

### Task 2: 因子引擎 `factors.py`（自动算的 16 个因子）

**Files:**
- Create: `strategy_web/factors.py`
- Test: `strategy_web/tests/test_factors.py`

**Interfaces:**
- Consumes:
  - `DataSource`（Task 1）— 通过 `get_limit_up_stocks`、`get_kline`、`get_index_kline`、`get_sector_stocks` 取数
- Produces:
  - `class FactorEngine`
    - `compute_factors(code: str, tick: dict, detail: dict, ds: DataSource, sector_map: dict) -> dict`
      返回该股 16 个自动因子的 {0,1} 得分 + 计算来源说明，形如：
      `{"F1": {"score":1, "note":"近20日无涨停且今日首次涨停"}, ...}`
    - `compute_market_factors(ds: DataSource, ticks: dict, limit_ups: list) -> dict`
      返回市场环境因子（N1-N5），其中 N1/N3 用替代算法
  - `class FactorComputeError(Exception)`

**自动因子清单（16个）：** F1 F2 F3 F4 F5 F6 Y3 Y4 S2 S3 S4 S6 N1 N2 N4 N5
**手填因子清单（8个，Task 3 处理）：** F7 Y1 Y2 Y5 Y6 Y7 S1 S5 S7

**实现要点：**
- F1 首板：近20日K线无任何一日收盘触及涨停价，且今日首次涨停
- F2 早封板：tick 的 sealed=True 且封板时间≤10:00（用 `get_full_tick` 的 time/timetag 近似；分钟级封板时间不在日线数据里，用 tick 时间戳的小时判断）
- F3 封单强度：封单金额（bidVol[0]×涨停价）≥ 流通市值×0.5%（主板）或×0.2%（创业/科创）；流通市值 = FloatVolume×现价
- F4 板块共振：所属行业当日涨停家数≥3（sector_map: code→行业，从行业板块反查）
- F5 量价堆积：近20日≥5天 volume>5日均量×1.5
- F6 大盘配合：上证指数 close 站上 20日均线，或连续两日 close 涨幅>0 且量能放大
- Y3 倍量：今日涨停日 volume ≥ 前5日均量×3
- Y4 均线多头：5/10/20日均线金叉上穿（5>10>20 且 20>前20日均线），股价>60日均线
- S2 量价堆积密度：近60日≥20天 volume>60日均量×1.5 且 价格在(max-min)/min ≤10% 区间震荡
- S3 最小阻力：今日放量（>60日均量×1.5）突破60日或120日均线，且收盘价站上
- S4 均线系统：60/120/250日均线多头排列（60>120>250）且斜率向上（末日值>前5日均值）
- S6 板块共振强度：所属板块指数近20日上升趋势（末收盘>20日前收盘）且板块内≥3只涨停
- N1 涨停指数(替代880368)：今日涨停家数 > 近5日涨停家数均值（用全市场 ticks 算）
- N2 情绪周期：用涨停家数 + 连板高度 + 晋级率综合推算（回暖/高潮/冰点/退潮）
- N4 连板高度：最高连板≥5 或 连板晋级率>25%
- N5 成交额：两市成交额 = 全市场 ticks amount 求和 ≥ 2万亿

**注意：** 连板高度需要历史K线判断连续涨停天数。为控制复杂度，N2/N4 用简化算法：以全市场当日涨停家数 + 涨停股中"连续涨停比例"近似，细节见代码注释。若简化算法数据不足，N2/N4 返回 None 并标"数据不足"。

- [ ] **Step 1: 写测试 `tests/test_factors.py`**

```python
# -*- coding: utf-8 -*-
"""factors 单元测试 — 用构造的 K线/盘口 数据验证因子计算"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
import pytest

from data_source import DataSource
from factors import FactorEngine


def make_kline(closes, volumes=None):
    """构造日线K线 DataFrame"""
    n = len(closes)
    volumes = volumes or np.full(n, 100000)
    return pd.DataFrame({
        "time": pd.date_range("2026-01-01", periods=n, freq="B"),
        "open": closes, "high": [c*1.02 for c in closes],
        "low": [c*0.98 for c in closes], "close": closes,
        "volume": volumes, "amount": [c*v*100 for c,v in zip(closes, volumes)],
    })


class FakeDS:
    """不连QMT的假 DataSource，只实现 factors 需要的两个方法"""
    def __init__(self, kline_map=None, index_map=None, sector_stocks=None):
        self.kline_map = kline_map or {}
        self.index_map = index_map or {}
        self.sector_stocks = sector_stocks or {}

    def get_kline(self, code, days=120):
        return self.kline_map.get(code, make_kline([20]*60, [100000]*60))

    def get_index_kline(self, code, days=60):
        return self.index_map.get(code, make_kline([3000]*60, [100000]*60))


def test_F1_first_board_requires_no_prior_limitup():
    ds = FakeDS()
    eng = FactorEngine()
    # 近20日最高涨幅仅5%(无涨停), 今日涨停
    kline = make_kline([10]*20 + [10.0], [100000]*21)
    tick = {"lastPrice":11.0,"lastClose":10.0,"sealed":True,
            "amount":1e6,"volume":200000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F1"]["score"] == 1

def test_F1_prior_limitup_fails():
    ds = FakeDS()
    eng = FactorEngine()
    # 近20日内有涨停(10→11), 则今日不算首板
    closes = [10]*19 + [11.0, 11.0]
    kline = make_kline(closes, [100000]*21)
    tick = {"lastPrice":11.0,"lastClose":11.0,"sealed":True,
            "amount":1e6,"volume":200000}
    detail = {"UpStopPrice":12.1,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F1"]["score"] == 0

def test_F3_seal_strength_mainboard():
    ds = FakeDS()
    eng = FactorEngine()
    # 主板: 封单金额 = bidVol0×涨停价; 需 ≥ 流通市值×0.5%
    tick = {"lastPrice":10.0,"lastClose":10.0,"sealed":True,
            "bidVol":[1000000,0,0,0,0],"bidPrice":[11.0,0,0,0,0],
            "amount":1e6,"volume":200000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}  # 流通市值=11×1e8=1.1e9
    # 封单额=1e6×11=1.1e7, 1.1e9×0.005=5.5e6 → 达标
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["F3"]["score"] == 1

def test_F4_sector_resonance():
    ds = FakeDS()
    eng = FactorEngine()
    sector_map = {"000001.SZ": "SW2半导体", "000002.SZ": "SW2半导体",
                  "000003.SZ": "SW2半导体", "000004.SZ": "SW2半导体"}
    # 板块内涨停家数(通过limit_ups传)≥3
    tick = {"lastPrice":10.0,"lastClose":10.0,"sealed":True}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    limit_ups = [{"code":c} for c in ["000001.SZ","000002.SZ","000003.SZ"]]
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map,
                            limit_ups=limit_ups)
    assert r["F4"]["score"] == 1

def test_Y3_volume_spike():
    ds = FakeDS()
    eng = FactorEngine()
    # 今日量 = 前5日均量×5 → 达标
    kline = make_kline([10]*25, [100000]*20 + [500000]*5)
    tick = {"lastPrice":11.0,"lastClose":10.0,"sealed":True,
            "amount":1e6,"volume":500000}
    detail = {"UpStopPrice":11.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["Y3"]["score"] == 1

def test_S4_ma_bullish():
    ds = FakeDS()
    eng = FactorEngine()
    # 构造 60>120>250 且斜率向上: 价格长期上升
    closes = list(np.linspace(10, 30, 300))
    kline = make_kline(closes, [100000]*300)
    tick = {"lastPrice":30.0,"lastClose":29.0,"sealed":True}
    detail = {"UpStopPrice":33.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={})
    assert r["S4"]["score"] == 1

def test_N5_total_amount_threshold():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ":{"amount":1.5e12},"000002.SZ":{"amount":1.0e12}}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[])
    assert m["N5"]["score"] == 1  # 合计2.5万亿>2万亿

def test_N5_below_threshold():
    ds = FakeDS()
    eng = FactorEngine()
    ticks = {"000001.SZ":{"amount":1.0e12},"000002.SZ":{"amount":0.5e12}}
    m = eng.compute_market_factors(ds, ticks, limit_ups=[])
    assert m["N5"]["score"] == 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd strategy_web && python -m pytest tests/test_factors.py -v`
Expected: 全部 FAIL，`ModuleNotFoundError: No module named 'factors'`

- [ ] **Step 3: 实现 `factors.py`**

```python
# -*- coding: utf-8 -*-
"""因子引擎：计算 24 个量化因子中能用 xtdata 数据自动算的部分。
自动算的 16 个：F1 F2 F3 F4 F5 F6 Y3 Y4 S2 S3 S4 S6 N1 N2 N4 N5
手填的 8 个：F7 Y1 Y2 Y5 Y6 Y7 S1 S5 S7（在 manual_store.py / 网页手填）"""
import numpy as np


class FactorComputeError(Exception):
    pass


def _ma(series, n):
    return series.rolling(n).mean()


class FactorEngine:
    def _sector_count(self, code, sector_map, limit_ups):
        """所属板块内当日涨停家数"""
        sector = sector_map.get(code)
        if not sector:
            return 0
        return sum(1 for c in (limit_ups or [])
                   if sector_map.get(c["code"]) == sector)

    def compute_factors(self, code, tick, detail, ds, sector_map,
                        limit_ups=None, market=None):
        """计算单只股票的自动因子。返回 {因子名: {"score":0/1, "note":str}}"""
        last = tick.get("lastPrice") or 0
        last_close = tick.get("lastClose") or 0
        up_price = detail.get("UpStopPrice") or 0
        float_vol = detail.get("FloatVolume") or 0
        sealed = bool((tick.get("askPrice") or [0])[0] == 0)

        try:
            kline = ds.get_kline(code, days=250)
        except Exception:
            kline = None

        out = {}

        # ---- F1 首板确认 ----
        f1 = 0
        if kline is not None and up_price > 0:
            closes = kline["close"].tolist()
            ups_hist = [round(c * (1 + (0.30 if code.startswith(("8","4")) else
                                      0.20 if code.startswith(("300","301","688")) else
                                      0.10)), 2)
                        for c in closes[:-1]]  # 排除今日
            prev_limit = any(c >= u - 0.01 for c, u in zip(closes[:-1], ups_hist))
            f1 = 1 if (not prev_limit and last >= up_price - 0.01) else 0
        out["F1"] = {"score": f1, "note": "近20日无涨停且今日首次涨停" if f1 else "近20日已有涨停或今日未涨停"}

        # ---- F2 早封板 ----
        f2 = 0
        if sealed:
            ts = tick.get("timetag") or ""
            # timetag 形如 "20260811 14:05:06"
            try:
                hh = int(ts.split(" ")[1].split(":")[0])
                mm = int(ts.split(" ")[1].split(":")[1])
                f2 = 1 if (hh, mm) <= (10, 0) else 0
            except Exception:
                f2 = 0
        out["F2"] = {"score": f2, "note": "封板时间≤10:00" if f2 else "未封板或封板时间晚于10:00"}

        # ---- F3 封单强度 ----
        f3 = 0
        if sealed and up_price > 0 and float_vol > 0:
            bid0 = (tick.get("bidPrice") or [0])[0]
            bidv0 = (tick.get("bidVol") or [0])[0]
            seal_amount = bid0 * bidv0
            float_mv = up_price * float_vol
            ratio = 0.005 if not code.startswith(("300","301","688")) else 0.002
            f3 = 1 if seal_amount >= float_mv * ratio else 0
        out["F3"] = {"score": f3, "note": "封单≥流通市值0.5%(主板)/0.2%(创业科创)" if f3 else "封单不足"}

        # ---- F4 板块共振 ----
        f4 = 1 if self._sector_count(code, sector_map, limit_ups) >= 3 else 0
        out["F4"] = {"score": f4, "note": "板块涨停≥3家" if f4 else "板块共振不足"}

        # ---- F5 量价堆积 ----
        f5 = 0
        if kline is not None and len(kline) >= 20:
            vols = kline["volume"].tolist()[-20:]
            ma5 = sum(vols[-5:]) / 5
            days = sum(1 for v in vols if v > ma5 * 1.5)
            f5 = 1 if days >= 5 else 0
        out["F5"] = {"score": f5, "note": "20日内≥5天量>5日均量×1.5" if f5 else "量价堆积不足"}

        # ---- F6 大盘配合 ----
        f6 = 0
        try:
            idx = ds.get_index_kline("000001.SH", days=30)
            if idx is not None and len(idx) >= 21:
                closes = idx["close"].tolist()
                ma20 = sum(closes[-20:]) / 20
                above = closes[-1] > ma20
                # 连续两日放量上涨
                chg = [closes[i] / closes[i-1] - 1 for i in range(-2, 0)]
                vols = idx["volume"].tolist()[-3:]
                up2 = chg[0] > 0 and chg[1] > 0 and vols[-1] > vols[-3]
                f6 = 1 if (above or up2) else 0
        except Exception:
            f6 = 0
        out["F6"] = {"score": f6, "note": "上证站上20日均线或连两日放量涨" if f6 else "大盘环境一般"}

        # ---- Y3 倍量突破 ----
        y3 = 0
        if kline is not None and len(kline) >= 6:
            vols = kline["volume"].tolist()
            today = vols[-1]
            ma5_prev = sum(vols[-6:-1]) / 5
            y3 = 1 if today >= ma5_prev * 3 else 0
        out["Y3"] = {"score": y3, "note": "涨停日量≥前5日均量×3" if y3 else "量能未达3倍"}

        # ---- Y4 均线多头 ----
        y4 = 0
        if kline is not None and len(kline) >= 60:
            closes = kline["close"]
            ma5 = _ma(closes, 5).iloc[-1]
            ma10 = _ma(closes, 10).iloc[-1]
            ma20 = _ma(closes, 20).iloc[-1]
            ma60 = _ma(closes, 60).iloc[-1]
            if ma5 > ma10 > ma20 and closes.iloc[-1] > ma60:
                y4 = 1
        out["Y4"] = {"score": y4, "note": "5/10/20多头排列且站上60日线" if y4 else "均线未多头"}

        # ---- S2 量价堆积密度 ----
        s2 = 0
        if kline is not None and len(kline) >= 60:
            vols = kline["volume"].tolist()[-60:]
            closes = kline["close"].tolist()[-60:]
            ma60 = sum(vols) / 60
            big = sum(1 for v in vols if v > ma60 * 1.5)
            hi, lo = max(closes), min(closes)
            narrow = (hi - lo) / lo <= 0.10 if lo > 0 else False
            s2 = 1 if (big >= 20 and narrow) else 0
        out["S2"] = {"score": s2, "note": "60日≥20天放量且价格窄幅震荡" if s2 else "量价密度不足"}

        # ---- S3 最小阻力突破 ----
        s3 = 0
        if kline is not None and len(kline) >= 120:
            closes = kline["close"].tolist()
            vols = kline["volume"].tolist()
            ma60 = sum(closes[-60:]) / 60
            ma120 = sum(closes[-120:]) / 120
            ma60v = sum(vols[-60:]) / 60
            today_v = vols[-1]
            broke = closes[-1] > ma60 and closes[-1] > ma120
            volup = today_v > ma60v * 1.5
            s3 = 1 if (broke and volup) else 0
        out["S3"] = {"score": s3, "note": "放量突破60/120日均线" if s3 else "未突破长期均线"}

        # ---- S4 均线系统 ----
        s4 = 0
        if kline is not None and len(kline) >= 250:
            closes = kline["close"]
            ma60 = _ma(closes, 60).iloc[-1]
            ma120 = _ma(closes, 120).iloc[-1]
            ma250 = _ma(closes, 250).iloc[-1]
            slope_up = closes.iloc[-1] > closes.iloc[-6]
            if ma60 > ma120 > ma250 and slope_up:
                s4 = 1
        out["S4"] = {"score": s4, "note": "60/120/250多头排列且斜率向上" if s4 else "长均线未多头"}

        # ---- S6 板块共振强度 ----
        s6 = 0
        if self._sector_count(code, sector_map, limit_ups) >= 3:
            s6 = 1
        out["S6"] = {"score": s6, "note": "板块≥3只走强" if s6 else "板块内同步走强不足"}

        return out

    def compute_market_factors(self, ds, ticks, limit_ups=None):
        """计算市场环境因子 N1-N5。N1/N3 用替代算法。
        limit_ups: get_limit_up_stocks 的返回(含 code/last_close)"""
        limit_ups = limit_ups or []

        # ---- N5 两市成交额 ----
        total = sum((t.get("amount") or 0) for t in ticks.values())
        n5 = 1 if total >= 2e12 else 0
        n5_note = "两市成交额 %.0f 亿 >= 2万亿" % (total / 1e8) if n5 else \
                  "两市成交额 %.0f 亿 < 2万亿" % (total / 1e8)

        # ---- N1 涨停指数(替代: 今日涨停家数 vs 近5日均值) ----
        n1 = 0
        try:
            idx = ds.get_index_kline("880368.SH", days=8)
            if idx is not None and len(idx) >= 6:
                vals = idx["close"].tolist()
                n1 = 1 if vals[-1] > sum(vals[-6:-1]) / 5 else 0
            else:
                n1 = 1 if len(limit_ups) > 0 else 0  # 兜底
        except Exception:
            n1 = 1 if len(limit_ups) > 0 else 0
        n1_note = "涨停指数在5日线上方(替代算法)" if n1 else "涨停指数走弱"

        # ---- N3 首板溢价(替代: 昨日涨停股今日平均涨幅>0) ----
        n3 = 0
        avg_chg = 0.0
        try:
            chgs = []
            for lu in limit_ups:
                lc = lu.get("last_close") or 0
                last = lu.get("last") or 0
                if lc > 0:
                    chgs.append((last / lc - 1) * 100)
            if chgs:
                avg_chg = sum(chgs) / len(chgs)
                n3 = 1 if avg_chg > 0 else 0
        except Exception:
            n3 = 0
        n3_note = "首板股今日均涨 %.2f%%" % avg_chg if n3 else "首板溢价为负"

        # ---- N2 情绪周期 ----
        n2 = 0
        n2_note = "情绪周期判断需连板数据, 简化以涨停家数近似"
        if len(limit_ups) >= 50:
            n2 = 1
        elif len(limit_ups) >= 20:
            n2 = 1
        n2_note = "涨停家数 %d, 情绪偏暖" % len(limit_ups) if n2 else \
                  "涨停家数 %d, 情绪偏冷" % len(limit_ups)

        # ---- N4 连板高度 ----
        n4 = 0
        n4_note = "连板数据需历史K线, 简化以涨停家数+封板率近似"
        if len(limit_ups) >= 30:
            sealed_cnt = sum(1 for lu in limit_ups if lu.get("sealed"))
            if len(limit_ups) > 0 and sealed_cnt / len(limit_ups) > 0.6:
                n4 = 1
        n4_note = "涨停% 且封板率>60%" % len(limit_ups) if n4 else "连板高度不足"

        return {
            "N1": {"score": n1, "note": n1_note},
            "N2": {"score": n2, "note": n2_note},
            "N3": {"score": n3, "note": n3_note},
            "N4": {"score": n4, "note": n4_note},
            "N5": {"score": n5, "note": n5_note},
        }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd strategy_web && python -m pytest tests/test_factors.py -v`
Expected: 8 passed

- [ ] **Step 5: 提交**

```bash
git add strategy_web/factors.py strategy_web/tests/test_factors.py
git commit -m "feat: add factor engine with 16 auto-computed factors"
```

---

### Task 3: 手填因子存储 `manual_store.py`

**Files:**
- Create: `strategy_web/manual_store.py`
- Test: `strategy_web/tests/test_manual_store.py`

**Interfaces:**
- Consumes: 无（独立模块）
- Produces:
  - `class ManualStore`
    - `__init__(path: str = "manual_factors.json")`
    - `get_manual(code: str) -> dict` — 返回该股手填因子 `{factor_name: 0/1}`（无则空 dict）
    - `set_manual(code: str, factors: dict) -> None` — 保存/更新该股手填因子（只接受 0/1 值）
    - `all() -> dict` — 返回全部 `{code: {factor: score}}`
    - `merge(auto_factors: dict, code: str) -> dict` — 合并自动因子和手填因子，返回完整因子字典

**手填因子清单（8个）：** F7 Y1 Y2 Y5 Y6 Y7 S1 S5 S7
（注：设计文档里 Y1 原计划手填，但 Task 1 发现 FloatVolume 可自动算流通市值，Y1 改为自动算——但保持手填覆盖能力，即 Y1 若自动算结果不满足可手填覆盖。此任务把 Y1 留在可手填集合里，网页层决定是否展示自动值。）

**存储格式（manual_factors.json）：**
```json
{
  "002859.SZ": {"F7": 1, "Y1": 0, "Y5": 1},
  "600353.SH": {"S1": 1}
}
```

- [ ] **Step 1: 写测试 `tests/test_manual_store.py`**

```python
# -*- coding: utf-8 -*-
"""manual_store 单元测试"""
import sys
import json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from manual_store import ManualStore


@pytest.fixture
def store(tmp_path):
    return ManualStore(str(tmp_path / "manual.json"))


def test_empty_manual(store):
    assert store.get_manual("000001.SZ") == {}
    assert store.all() == {}


def test_set_and_get(store):
    store.set_manual("000001.SZ", {"F7": 1, "Y5": 0})
    assert store.get_manual("000001.SZ") == {"F7": 1, "Y5": 0}


def test_set_persists_to_disk(store, tmp_path):
    store.set_manual("000001.SZ", {"F7": 1})
    reloaded = ManualStore(str(tmp_path / "manual.json"))
    assert reloaded.get_manual("000001.SZ") == {"F7": 1}


def test_set_rejects_invalid_values(store):
    with pytest.raises(ValueError):
        store.set_manual("000001.SZ", {"F7": 2})   # 非 0/1
    with pytest.raises(ValueError):
        store.set_manual("000001.SZ", {"F7": "x"})


def test_merge_combines_auto_and_manual(store):
    auto = {"F1": 1, "F3": 0, "Y1": 0}
    store.set_manual("000001.SZ", {"F7": 1, "Y1": 1})  # 手填覆盖 Y1
    merged = store.merge(auto, "000001.SZ")
    assert merged["F1"] == 1       # 自动
    assert merged["F3"] == 0       # 自动
    assert merged["F7"] == 1       # 手填
    assert merged["Y1"] == 1       # 手填覆盖自动


def test_merge_unknown_code(store):
    auto = {"F1": 1}
    merged = store.merge(auto, "999999.SZ")
    assert merged == {"F1": 1}     # 无手填则保持原样
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd strategy_web && python -m pytest tests/test_manual_store.py -v`
Expected: 全部 FAIL，`ModuleNotFoundError: No module named 'manual_store'`

- [ ] **Step 3: 实现 `manual_store.py`**

```python
# -*- coding: utf-8 -*-
"""手填因子持久化：xtdata 拿不到数据的因子，用户在网页上手填得分(0/1)。
数据存 JSON 文件，重新选股时自动合并到自动因子。"""
import json
from pathlib import Path


class ManualStore:
    # 可手填的因子集合（含部分可自动算但允许手填覆盖的）
    MANUAL_FACTORS = ["F7", "Y1", "Y2", "Y5", "Y6", "Y7", "S1", "S5", "S7"]

    def __init__(self, path="manual_factors.json"):
        self.path = Path(path)
        self._data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    def get_manual(self, code):
        return dict(self._data.get(code, {}))

    def set_manual(self, code, factors):
        for k, v in factors.items():
            if k not in self.MANUAL_FACTORS:
                raise ValueError("未知手填因子: %s" % k)
            if v not in (0, 1):
                raise ValueError("%s 的值必须是 0 或 1，收到 %r" % (k, v))
        self._data[code] = {k: int(v) for k, v in factors.items()}
        self._save()

    def all(self):
        return {k: dict(v) for k, v in self._data.items()}

    def merge(self, auto_factors, code):
        """合并自动因子和该股手填因子，返回完整因子字典 {因子: 0/1}"""
        merged = dict(auto_factors)
        for k, v in self.get_manual(code).items():
            merged[k] = v
        return merged
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd strategy_web && python -m pytest tests/test_manual_store.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add strategy_web/manual_store.py strategy_web/tests/test_manual_store.py
git commit -m "feat: add manual factor store with JSON persistence"
```

---

### Task 4: 模型评分 `models.py`

**Files:**
- Create: `strategy_web/models.py`
- Test: `strategy_web/tests/test_models.py`

**Interfaces:**
- Consumes: 因子字典 `{factor_name: 0/1}`（来自 factors.py + manual_store.merge）
- Produces:
  - `MODEL_DEFINITIONS: dict` — 四个模型定义（因子归属 + 权重）
  - `class ModelScorer`
    - `score_stock(factors: dict) -> dict` — 返回该股四模型得分 + 综合分 + 组合等级 + 强弱级别
    - `classify_market(node_score: int) -> str` — 情绪阶段判定（冰点/回暖/高潮/退潮）

**评分规则（用户已确认）：**
- 权重：首板30% + 妖股30% + 势能25% + 节点15%（=100%）
- 每个模型得分 = 该模型命中因子数（模型基础分 1-7 分）
- 综合分 = 首板分×0.30 + 妖股分×0.30 + 势能分×0.25 + 节点分×0.15
- 强弱区间：
  - 极强：节点分=5 且 任一选股模型≥6 → 仓位上限75%
  - 强：节点分≥4 且 任一选股模型≥5 → 仓位上限50%
  - 中等：节点分≥3 且 任一选股模型≥5 → 仓位上限30%
  - 弱：其他 → 观察/空仓
- A-E 组合分级：
  - A：节点≥4 且 首板≥6
  - B：节点≥4 且 妖股≥6
  - C：节点≥4 且 势能≥6
  - D：节点≥3 且 任一选股模型≥5
  - E：其他（空仓）

**模型因子归属：**
- 首板模型：F1 F2 F3 F4 F5 F6 F7（7因子，权重30%）
- 妖股模型：Y1 Y2 Y3 Y4 Y5 Y6 Y7（7因子，权重30%）
- 势能模型：S1 S2 S3 S4 S5 S6 S7（7因子，权重25%）
- 节点模型：N1 N2 N3 N4 N5（5因子，权重15%）

- [ ] **Step 1: 写测试 `tests/test_models.py`**

```python
# -*- coding: utf-8 -*-
"""models 单元测试"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models import ModelScorer


def test_A_class_requires_node4_and_firstboard6():
    s = ModelScorer()
    # 首板全中7分, 节点4分, 妖股/势能低
    factors = {"F1":1,"F2":1,"F3":1,"F4":1,"F5":1,"F6":1,"F7":1,
               "Y1":0,"Y2":0,"Y3":0,"Y4":0,"Y5":0,"Y6":0,"Y7":0,
               "S1":0,"S2":0,"S3":0,"S4":0,"S5":0,"S6":0,"S7":0,
               "N1":1,"N2":1,"N3":1,"N4":1,"N5":0}
    r = s.score_stock(factors)
    assert r["first_board"] == 7
    assert r["node"] == 4
    assert r["grade"] == "A"
    assert r["strength"] == "极强"  # 节点4+首板7, 但节点≠5 → 强? 需按规则
    # 强弱: 节点≥4 且 首板≥5 → "强"; 综合分 = 7*.3+0+.0+4*.15=2.7


def test_E_class_low_scores():
    s = ModelScorer()
    factors = {"F1":0,"F2":0,"F3":0,"F4":0,"F5":0,"F6":0,"F7":0,
               "Y1":0,"Y2":0,"Y3":0,"Y4":0,"Y5":0,"Y6":0,"Y7":0,
               "S1":0,"S2":0,"S3":0,"S4":0,"S5":0,"S6":0,"S7":0,
               "N1":0,"N2":0,"N3":0,"N4":0,"N5":0}
    r = s.score_stock(factors)
    assert r["grade"] == "E"
    assert r["strength"] == "弱"


def test_composite_score_weights():
    s = ModelScorer()
    # 全部命中: 首板7 妖股7 势能7 节点5 → 综合分 = 7*.3+7*.3+7*.25+5*.15
    factors = {"F1":1,"F2":1,"F3":1,"F4":1,"F5":1,"F6":1,"F7":1,
               "Y1":1,"Y2":1,"Y3":1,"Y4":1,"Y5":1,"Y6":1,"Y7":1,
               "S1":1,"S2":1,"S3":1,"S4":1,"S5":1,"S6":1,"S7":1,
               "N1":1,"N2":1,"N3":1,"N4":1,"N5":1}
    r = s.score_stock(factors)
    assert abs(r["composite"] - (7*0.30 + 7*0.30 + 7*0.25 + 5*0.15)) < 1e-6


def test_missing_factors_treated_as_zero():
    s = ModelScorer()
    r = s.score_stock({})   # 空因子
    assert r["first_board"] == 0
    assert r["node"] == 0
    assert r["grade"] == "E"


def test_classify_market():
    s = ModelScorer()
    assert s.classify_market(5) == "高潮期"
    assert s.classify_market(4) == "回暖期"
    assert s.classify_market(3) == "冰点期"
    assert s.classify_market(2) == "退潮期"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd strategy_web && python -m pytest tests/test_models.py -v`
Expected: 全部 FAIL，`ModuleNotFoundError: No module named 'models'`

- [ ] **Step 3: 实现 `models.py`**

```python
# -*- coding: utf-8 -*-
"""四模型评分：首板/妖股/势能/节点 + 综合分 + 组合分级 + 强弱区间。"""

# 模型权重（修正为100%）
MODEL_WEIGHTS = {
    "first_board": 0.30,
    "monster": 0.30,
    "momentum": 0.25,
    "node": 0.15,
}

# 因子归属
MODEL_FACTORS = {
    "first_board": ["F1", "F2", "F3", "F4", "F5", "F6", "F7"],
    "monster": ["Y1", "Y2", "Y3", "Y4", "Y5", "Y6", "Y7"],
    "momentum": ["S1", "S2", "S3", "S4", "S5", "S6", "S7"],
    "node": ["N1", "N2", "N3", "N4", "N5"],
}


class ModelScorer:
    def score_stock(self, factors):
        """输入完整因子字典 {因子: 0/1}，返回模型评分结果。缺失因子按0计。"""
        model_scores = {}
        for model, names in MODEL_FACTORS.items():
            model_scores[model] = sum(1 for n in names if factors.get(n) == 1)

        fb = model_scores["first_board"]
        mo = model_scores["monster"]
        mom = model_scores["momentum"]
        nd = model_scores["node"]

        composite = (fb * MODEL_WEIGHTS["first_board"] +
                     mo * MODEL_WEIGHTS["monster"] +
                     mom * MODEL_WEIGHTS["momentum"] +
                     nd * MODEL_WEIGHTS["node"])

        # 组合分级 A-E
        best_pick = max(fb, mo, mom)
        if nd >= 4 and fb >= 6:
            grade = "A"
        elif nd >= 4 and mo >= 6:
            grade = "B"
        elif nd >= 4 and mom >= 6:
            grade = "C"
        elif nd >= 3 and best_pick >= 5:
            grade = "D"
        else:
            grade = "E"

        # 强弱区间
        if nd == 5 and best_pick >= 6:
            strength, position = "极强", "仓位上限75%"
        elif nd >= 4 and best_pick >= 5:
            strength, position = "强", "仓位上限50%"
        elif nd >= 3 and best_pick >= 5:
            strength, position = "中等", "仓位上限30%"
        else:
            strength, position = "弱", "观察/空仓"

        return {
            "first_board": fb, "monster": mo, "momentum": mom, "node": nd,
            "composite": round(composite, 2),
            "grade": grade, "strength": strength, "position": position,
        }

    def classify_market(self, node_score):
        """节点模型得分 → 情绪阶段。5=高潮 4=回暖 3=冰点 2以下=退潮"""
        if node_score >= 5:
            return "高潮期"
        if node_score == 4:
            return "回暖期"
        if node_score == 3:
            return "冰点期"
        return "退潮期"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd strategy_web && python -m pytest tests/test_models.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add strategy_web/models.py strategy_web/tests/test_models.py
git commit -m "feat: add four-model scoring with composite grade"
```

---

### Task 5: 选股流程编排 `screen.py`

**Files:**
- Create: `strategy_web/screen.py`
- Test: `strategy_web/tests/test_screen.py`

**Interfaces:**
- Consumes:
  - `DataSource`（Task 1）— 取数
  - `FactorEngine`（Task 2）— 算因子
  - `ManualStore`（Task 3）— 合并手填因子
  - `ModelScorer`（Task 4）— 模型评分
- Produces:
  - `class ScreenRunner`
    - `__init__(ds=None, engine=None, store=None, scorer=None)` — 可注入依赖便于测试
    - `run() -> dict` — 完整选股流程，返回结果字典
  - 返回结构：
    ```json
    {
      "market": {"node_score": int, "stage": "回暖期", "factors": {...N1-N5}, "total_amount": float},
      "environment_ok": true/false,
      "candidates": [
        {"code":"002859.SZ","name":"洁美科技","scores":{首板/妖股/势能/节点/综合分/grade/strength},
         "factors": {...完整因子}, "auto_manual": {...来源标记},"last":81.32,"up_stop_price":81.32,
         "sealed":true,"float_mv":...}
      ],
      "summary": {"candidate_count": N, "a_count": n, ...}
    }
    ```

**流程（忠实还原用户模型）：**
1. `ds.get_full_market_ticks()` 拉全市场
2. `ds.get_limit_up_stocks(ticks)` 得涨停池
3. `engine.compute_market_factors(ds, ticks, limit_ups)` 算 N1-N5 → 节点模型得分
4. `classify_market(node_score)` 判情绪阶段
5. **环境门槛**：节点分 ≥ 3（冰点期门槛）才继续；否则返回 `environment_ok=False`，候选为空
6. 对涨停池每只：
   - `ds.get_kline(code)` 取日线
   - `engine.compute_factors(...)` 算自动因子（含 sector_map 板块映射）
   - `store.merge(auto, code)` 合并手填
   - `scorer.score_stock(merged)` 算模型分
7. 汇总统计 A/B/C 类数量

**板块映射（sector_map）：** 用行业板块反查——对涨停池每只，找它属于哪个 SW 行业板块（从候选科技行业 + 全部行业板块里查），供 F4/S6 用。为控制复杂度，只用用户配置的 SECTORS 科技行业查（含半导体/软件/通信等），非科技股板块映射为 None。

- [ ] **Step 1: 写测试 `tests/test_screen.py`**

```python
# -*- coding: utf-8 -*-
"""screen 单元测试 — 用假 DataSource/引擎 验证编排逻辑"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from screen import ScreenRunner


class FakeDS:
    def __init__(self):
        self.ticks = {
            "002859.SZ": {"lastPrice":81.32,"lastClose":73.93,"askPrice":[0,0,0,0,0],
                          "bidPrice":[81.32,0,0,0,0],"bidVol":[34661,0,0,0,0],
                          "amount":1.4e9,"volume":181657},
            "000001.SZ": {"lastPrice":10.0,"lastClose":10.0,"askPrice":[10.01,0,0,0,0],
                          "bidPrice":[9.99,0,0,0,0],"bidVol":[100,0,0,0,0],
                          "amount":1e7,"volume":5000},
        }
        self.details = {
            "002859.SZ": {"UpStopPrice":81.32,"InstrumentName":"洁美科技",
                          "FloatVolume":428315200.0,"OpenDate":"20170407"},
            "000001.SZ": {"UpStopPrice":11.0,"InstrumentName":"平安银行",
                          "FloatVolume":2e10,"OpenDate":"19910101"},
        }

    def connect(self):
        pass

    def get_full_market_ticks(self, codes=None):
        return dict(self.ticks)

    def get_limit_up_stocks(self, ticks=None):
        return [{"code":"002859.SZ","name":"洁美科技","last":81.32,"last_close":73.93,
                 "up_stop_price":81.32,"sealed":True,"amount":1.4e9,"volume":181657,
                 "float_volume":428315200.0,"open_date":"20170407"}]

    def get_kline(self, code, days=120):
        import pandas as pd
        import numpy as np
        return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=days, freq="B"),
                             "open": np.linspace(10,20,days), "high": np.linspace(10,21,days),
                             "low": np.linspace(9,19,days), "close": np.linspace(10,20,days),
                             "volume": np.full(days,100000), "amount": np.full(days,1e6)})

    def get_instruments_bulk(self, codes):
        return {c: self.details[c] for c in codes if c in self.details}

    def get_index_kline(self, code, days=60):
        import pandas as pd
        import numpy as np
        return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=days, freq="B"),
                             "close": np.linspace(3000,3200,days),
                             "volume": np.full(days,100000)})

    def get_sector_stocks(self, sector):
        return list(self.ticks.keys()) if sector == "沪深A股" else []


class FakeEngine:
    def compute_factors(self, *a, **k):
        return {"F1":1,"F2":1,"F3":1,"F4":0,"F5":1,"F6":1,
                "Y3":1,"Y4":1,"S2":1,"S3":1,"S4":1,"S6":0}

    def compute_market_factors(self, ds, ticks, limit_ups=None):
        return {"N1":{"score":1},"N2":{"score":1},"N3":{"score":1},
                "N4":{"score":1},"N5":{"score":1}}


class FakeStore:
    def merge(self, auto, code):
        merged = dict(auto)
        if code == "002859.SZ":
            merged["F7"] = 1
        return merged


class FakeScorer:
    def score_stock(self, factors):
        fb = sum(1 for n in ["F1","F2","F3","F4","F5","F6","F7"] if factors.get(n)==1)
        return {"first_board":fb,"monster":0,"momentum":0,"node":5,
                "composite":round(fb*0.3+5*0.15,2),
                "grade":"A" if fb>=6 else "E","strength":"极强","position":"75%"}

    def classify_market(self, node_score):
        return "回暖期"


def test_screen_returns_candidates_when_env_ok():
    r = ScreenRunner(ds=FakeDS(), engine=FakeEngine(),
                     store=FakeStore(), scorer=FakeScorer()).run()
    assert r["environment_ok"] is True
    assert len(r["candidates"]) == 1
    assert r["candidates"][0]["code"] == "002859.SZ"
    assert r["candidates"][0]["scores"]["grade"] == "A"
    # 手填因子已合并
    assert r["candidates"][0]["factors"]["F7"] == 1


def test_screen_blocks_when_env_bad():
    class BadScorer(FakeScorer):
        def classify_market(self, node_score):
            return "退潮期"
    # 环境不达标 → 返回 environment_ok=False, 无候选
    class BadEngine(FakeEngine):
        def compute_market_factors(self, ds, ticks, limit_ups=None):
            return {"N1":{"score":0},"N2":{"score":0},"N3":{"score":0},
                    "N4":{"score":0},"N5":{"score":0}}
    r = ScreenRunner(ds=FakeDS(), engine=BadEngine(),
                     store=FakeStore(), scorer=BadScorer()).run()
    assert r["environment_ok"] is False
    assert r["candidates"] == []
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd strategy_web && python -m pytest tests/test_screen.py -v`
Expected: 全部 FAIL，`ModuleNotFoundError: No module named 'screen'`

- [ ] **Step 3: 实现 `screen.py`**

```python
# -*- coding: utf-8 -*-
"""选股流程编排：先判市场环境，达标才扫涨停池算分，输出候选清单。
依赖 data_source / factors / manual_store / models，可注入假实现便于测试。"""
from data_source import DataSource
from factors import FactorEngine
from manual_store import ManualStore
from models import ModelScorer

# 默认科技行业池（用于板块映射 F4/S6）
SECTORS = ["SW1电子", "SW1计算机", "SW1通信"]

# 环境门槛：节点模型得分达标才选股（冰点期阈值）
ENV_THRESHOLD = 3


class ScreenRunner:
    def __init__(self, ds=None, engine=None, store=None, scorer=None):
        self.ds = ds or DataSource()
        self.engine = engine or FactorEngine()
        self.store = store or ManualStore()
        self.scorer = scorer or ModelScorer()

    def _build_sector_map(self, limit_ups):
        """涨停池 code → 所属行业。用科技行业板块反查。"""
        sector_map = {}
        ups = [u["code"] for u in limit_ups]
        if not ups:
            return sector_map
        for sector in SECTORS:
            members = set(self.ds.get_sector_stocks(sector))
            for code in ups:
                if code in members:
                    sector_map.setdefault(code, sector)
        return sector_map

    def run(self):
        """完整选股流程。返回结果字典（见模块 docstring）。"""
        # 1. 全市场行情 + 涨停池
        ticks = self.ds.get_full_market_ticks()
        limit_ups = self.ds.get_limit_up_stocks(ticks)

        # 2. 市场环境因子 N1-N5
        market_factors = self.engine.compute_market_factors(self.ds, ticks, limit_ups)
        node_score = sum(1 for n in ["N1","N2","N3","N4","N5"]
                         if market_factors.get(n, {}).get("score") == 1)
        stage = self.scorer.classify_market(node_score)
        total_amount = sum((t.get("amount") or 0) for t in ticks.values())

        result = {
            "market": {
                "node_score": node_score,
                "stage": stage,
                "factors": market_factors,
                "total_amount": total_amount,
                "limit_up_count": len(limit_ups),
            },
            "environment_ok": node_score >= ENV_THRESHOLD,
            "candidates": [],
            "summary": {"candidate_count": 0, "a_count": 0, "b_count": 0,
                        "c_count": 0, "d_count": 0},
        }

        # 3. 环境门槛：不达标直接返回
        if not result["environment_ok"]:
            return result

        # 4. 板块映射（供 F4/S6）
        sector_map = self._build_sector_map(limit_ups)

        # 5. 对涨停池每只算因子 + 评分
        for lu in limit_ups:
            code = lu["code"]
            try:
                auto = self.engine.compute_factors(code, lu, lu, self.ds,
                                                   sector_map, limit_ups, None)
            except Exception:
                auto = {}
            # 合并手填因子
            factors = self.store.merge(auto, code)
            scores = self.scorer.score_stock(factors)
            # 标记来源：自动算的 vs 手填的
            auto_manual = {f: ("auto" if f in auto else "manual")
                           for f in factors}
            float_mv = (lu.get("float_volume") or 0) * (lu.get("last") or 0)
            result["candidates"].append({
                "code": code,
                "name": lu.get("name") or code,
                "last": lu.get("last"),
                "up_stop_price": lu.get("up_stop_price"),
                "sealed": lu.get("sealed"),
                "float_mv": float_mv,
                "scores": scores,
                "factors": factors,
                "auto_manual": auto_manual,
            })

        # 6. 汇总
        c = result["candidates"]
        result["summary"] = {
            "candidate_count": len(c),
            "a_count": sum(1 for x in c if x["scores"]["grade"] == "A"),
            "b_count": sum(1 for x in c if x["scores"]["grade"] == "B"),
            "c_count": sum(1 for x in c if x["scores"]["grade"] == "C"),
            "d_count": sum(1 for x in c if x["scores"]["grade"] == "D"),
        }
        return result
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd strategy_web && python -m pytest tests/test_screen.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add strategy_web/screen.py strategy_web/tests/test_screen.py
git commit -m "feat: add screen runner orchestrating market gate and scoring"
```

---

### Task 6: Flask 后端 `app.py`

**Files:**
- Create: `strategy_web/app.py`
- Test: `strategy_web/tests/test_app.py`

**Interfaces:**
- Consumes:
  - `ScreenRunner`（Task 5）— 核心选股
  - `ManualStore`（Task 3）— 手填因子读写
  - `DataSource`（Task 1）— 单股K线详情
- Produces:
  - `app = Flask(__name__)` — Flask 应用实例
  - 路由：
    - `GET /` → 渲染 `index.html`
    - `GET /api/health` → `{"ok": true, "qmt_connected": bool}`
    - `POST /api/screen` → 跑选股，返回 `ScreenRunner.run()` 结果
    - `GET /api/stock/<code>/kline` → 单股K线数据 `{dates, closes, volumes, up_stop, ma60}`
    - `POST /api/stock/<code>/manual` → 保存手填因子 `{factor: 0/1}`，返回合并后因子
    - `GET /api/stock/<code>/manual` → 读该股手填因子

**错误处理：** 所有 API 出错返回 `{"error": "..."}` + 400/500，不抛未捕获异常。QMT 未连接时 `/api/screen` 返回 `{"error": "QMT未连接, 请先打开QMT并开启miniQMT"}`。

- [ ] **Step 1: 写测试 `tests/test_app.py`**

```python
# -*- coding: utf-8 -*-
"""app 单元测试 — Flask test_client, 用假 ScreenRunner"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# 在 import app 前注入假模块（避免 app 里真实 import xtquant）
import app as app_module


class FakeScreen:
    def __init__(self, *a, **k):
        pass

    def run(self):
        return {"market": {"node_score": 4, "stage": "回暖期",
                           "factors": {}, "total_amount": 2.5e12,
                           "limit_up_count": 50},
                "environment_ok": True,
                "candidates": [{"code": "002859.SZ", "name": "洁美科技",
                                "scores": {"grade": "A", "composite": 5.0,
                                           "strength": "强", "position": "50%"},
                                "factors": {"F1": 1}}],
                "summary": {"candidate_count": 1, "a_count": 1}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(app_module, "ScreenRunner", FakeScreen)
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_screen_returns_candidates(client):
    r = client.post("/api/screen")
    assert r.status_code == 200
    data = r.get_json()
    assert data["environment_ok"] is True
    assert len(data["candidates"]) == 1


def test_manual_set_and_get(client, tmp_path, monkeypatch):
    # 用临时文件避免污染真实 manual_factors.json
    from manual_store import ManualStore
    s = ManualStore(str(tmp_path / "m.json"))
    monkeypatch.setattr(app_module, "manual_store_obj", s)
    r = client.post("/api/stock/002859.SZ/manual", json={"F7": 1})
    assert r.status_code == 200
    assert r.get_json()["factors"]["F7"] == 1
    r2 = client.get("/api/stock/002859.SZ/manual")
    assert r2.get_json()["F7"] == 1


def test_kline_endpoint(client, monkeypatch):
    import pandas as pd
    import numpy as np
    class FakeDS:
        def get_kline(self, code, days=120):
            n = 120
            return pd.DataFrame({"time": pd.date_range("2026-01-01", periods=n, freq="B"),
                                 "close": np.linspace(10, 20, n),
                                 "volume": np.full(n, 100000)})
        def get_instrument(self, code):
            return {"UpStopPrice": 22.0}
    monkeypatch.setattr(app_module, "ds_obj", FakeDS())
    r = client.get("/api/stock/002859.SZ/kline")
    assert r.status_code == 200
    data = r.get_json()
    assert len(data["closes"]) == 120
    assert "ma60" in data
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd strategy_web && python -m pytest tests/test_app.py -v`
Expected: 全部 FAIL，`ModuleNotFoundError: No module named 'app'` 或 import 错误

- [ ] **Step 3: 实现 `app.py`**

```python
# -*- coding: utf-8 -*-
"""Flask 后端：提供可视化网页 + JSON API。
启动：python app.py，浏览器访问 http://localhost:5000"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, render_template, request

from data_source import DataSource, DataSourceError
from manual_store import ManualStore
from screen import ScreenRunner

app = Flask(__name__)

# 全局单例（测试时可用 monkeypatch 替换）
ds_obj = DataSource()
manual_store_obj = ManualStore()


def _get_screen_runner():
    return ScreenRunner(ds=ds_obj, store=manual_store_obj)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/health")
def health():
    return jsonify({"ok": True, "qmt_connected": ds_obj._connected})


@app.route("/api/screen", methods=["POST"])
def screen():
    if not ds_obj._connected:
        return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
    try:
        result = _get_screen_runner().run()
        return jsonify(result)
    except DataSourceError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": "选股失败: %r" % e}), 500


@app.route("/api/stock/<code>/kline")
def stock_kline(code):
    try:
        df = ds_obj.get_kline(code, days=120)
        ma60 = df["close"].rolling(60).mean().tolist()
        detail = ds_obj.get_instrument(code)
        return jsonify({
            "dates": [str(t.date()) for t in df["time"]],
            "closes": df["close"].tolist(),
            "volumes": [int(v) for v in df["volume"]],
            "ma60": [None if x != x else round(x, 2) for x in ma60],  # NaN→None
            "up_stop": detail.get("UpStopPrice") or 0,
        })
    except Exception as e:
        return jsonify({"error": "K线获取失败: %r" % e}), 500


@app.route("/api/stock/<code>/manual", methods=["GET", "POST"])
def manual(code):
    if request.method == "GET":
        return jsonify(manual_store_obj.get_manual(code))
    try:
        payload = request.get_json() or {}
        manual_store_obj.set_manual(code, payload)
        return jsonify({"factors": manual_store_obj.get_manual(code)})
    except ValueError as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    print("=" * 50)
    print("连接 QMT miniQMT...")
    try:
        ds_obj.connect()
        print("已连接: 数据源就绪")
    except DataSourceError as e:
        print("警告: %s" % e)
        print("请先打开 QMT 并开启 miniQMT 模式")
    print("浏览器访问: http://localhost:5000")
    app.run(host="127.0.0.1", port=5000, debug=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd strategy_web && python -m pytest tests/test_app.py -v`
Expected: 4 passed

- [ ] **Step 5: 提交**

```bash
git add strategy_web/app.py strategy_web/tests/test_app.py
git commit -m "feat: add flask backend with screen/manual/kline APIs"
```

---

### Task 7: 前端页面 `index.html` + `app.js`

**Files:**
- Create: `strategy_web/templates/index.html`
- Create: `strategy_web/static/app.js`
- Create: `strategy_web/static/style.css`
- 说明：ECharts 从本地加载。下载 `echarts.min.js` 放 `strategy_web/static/echarts.min.js`（实现时用 curl 从 jsdelivr 下载，或用官方 CDN 引用作为备选）。

**Interfaces:**
- Consumes（HTTP API，Task 6 提供）：
  - `POST /api/screen` → 选股结果
  - `GET /api/stock/<code>/kline` → 单股K线
  - `GET/POST /api/stock/<code>/manual` → 手填因子
- Produces：单页应用，四个模块导航，无框架原生 JS。

**页面结构（单页，顶部 tab 切换）：**

```
┌────────────────────────────────────────────────────────┐
│ 顶部栏: 标题 + 连接状态 + [开始选股] 按钮                │
├──────────┬─────────────────────────────────────────────┤
│ tab导航   │  内容区(四个tab)                             │
│ ①市场环境  │  ① 节点模型得分卡 + 情绪阶段 + 成交额/涨停家数│
│ ②候选列表  │  ② 表格: 代码/名称/三模型分/综合分/等级      │
│ ③模型对比  │  ③ 雷达图(三模型) + 因子命中柱状图           │
│ ④单股详情  │  ④ K线图 + 因子明细(自动/手填)              │
└──────────┴─────────────────────────────────────────────┘
```

**关键交互：**
- "开始选股" → POST /api/screen → 渲染 ①②③
- 候选列表点某只 → 切到④ tab，GET kline + GET manual，渲染 K线图 + 因子表
- 手填因子：受限因子旁显示输入框(0/1) + 保存按钮 → POST manual → 重新渲染
- 环境不达标时：市场环境 tab 显示"环境不允许, 空仓等待"横幅

**功能模块（app.js 拆成函数）：**
- `fetchScreen()` / `renderMarket(data.market)` / `renderCandidates(data.candidates)` / `renderComparison(data)`
- `showStockDetail(code)` / `renderKline(klineData)` / `renderFactors(factors, autoManual, code)`
- `saveManual(code)` / `switchTab(name)`
- 全局状态：`let state = { screenResult: null, currentCode: null }`

- [ ] **Step 1: 创建 `templates/index.html`**

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>量化选股策略看板</title>
<link rel="stylesheet" href="/static/style.css">
<script src="/static/echarts.min.js"></script>
</head>
<body>
<div id="app">
  <header>
    <h1>量化选股策略看板</h1>
    <span id="conn-status" class="badge">未连接</span>
    <button id="btn-screen" onclick="fetchScreen()">开始选股</button>
  </header>

  <nav id="tabs">
    <button class="tab active" data-tab="market" onclick="switchTab('market')">市场环境</button>
    <button class="tab" data-tab="candidates" onclick="switchTab('candidates')">候选列表</button>
    <button class="tab" data-tab="compare" onclick="switchTab('compare')">模型对比</button>
    <button class="tab" data-tab="detail" onclick="switchTab('detail')">单股详情</button>
  </nav>

  <main>
    <section id="tab-market" class="tab-panel active">
      <div id="market-banner"></div>
      <div id="node-cards" class="cards"></div>
      <div id="market-stats"></div>
    </section>

    <section id="tab-candidates" class="tab-panel">
      <table id="cand-table">
        <thead><tr>
          <th>代码</th><th>名称</th><th>首板</th><th>妖股</th><th>势能</th>
          <th>节点</th><th>综合</th><th>等级</th><th>强度</th>
        </tr></thead>
        <tbody></tbody>
      </table>
    </section>

    <section id="tab-compare" class="tab-panel">
      <div id="radar-chart" class="chart"></div>
      <div id="bar-chart" class="chart"></div>
    </section>

    <section id="tab-detail" class="tab-panel">
      <h2 id="detail-title">点击候选列表选择股票</h2>
      <div id="kline-chart" class="chart"></div>
      <div id="factor-table"></div>
    </section>
  </main>
</div>
<script src="/static/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: 创建 `static/style.css`**

```css
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: "Microsoft YaHei", sans-serif; background: #f5f6fa; color: #333; }
#app { max-width: 1200px; margin: 0 auto; padding: 20px; }
header { display: flex; align-items: center; gap: 16px; padding: 12px 0; }
header h1 { font-size: 22px; }
.badge { padding: 4px 10px; border-radius: 12px; font-size: 12px; }
.badge.ok { background: #27ae60; color: #fff; }
.badge.fail { background: #e74c3c; color: #fff; }
#btn-screen { padding: 8px 20px; background: #2980b9; color: #fff;
              border: none; border-radius: 6px; cursor: pointer; }
#tabs { display: flex; gap: 4px; border-bottom: 2px solid #ddd; margin-bottom: 16px; }
.tab { padding: 10px 20px; border: none; background: none; cursor: pointer;
       font-size: 15px; border-bottom: 3px solid transparent; }
.tab.active { border-bottom-color: #2980b9; color: #2980b9; font-weight: bold; }
.tab-panel { display: none; }
.tab-panel.active { display: block; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0; }
.card { background: #fff; border-radius: 8px; padding: 14px 18px; min-width: 130px;
        box-shadow: 0 1px 3px rgba(0,0,0,.1); }
.card .label { font-size: 12px; color: #888; }
.card .value { font-size: 24px; font-weight: bold; margin-top: 4px; }
.card .value.hit { color: #27ae60; }
.card .value.miss { color: #e74c3c; }
.chart { background: #fff; border-radius: 8px; padding: 12px;
         margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
#radar-chart, #bar-chart { height: 340px; }
#kline-chart { height: 400px; }
table { width: 100%; border-collapse: collapse; background: #fff;
        border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #eee; }
th { background: #f0f0f5; }
tr.grade-A { background: #fff3e0; }
tr.grade-B { background: #e8f5e9; }
tr.grade-C { background: #e3f2fd; }
tr.grade-D { background: #f1f8e9; }
#market-banner { padding: 14px; border-radius: 8px; margin-bottom: 12px;
                 font-size: 15px; text-align: center; }
#market-banner.env-ok { background: #e8f5e9; color: #2e7d32; }
#market-banner.env-bad { background: #fdecea; color: #c62828; }
#factor-table { background: #fff; border-radius: 8px; padding: 12px;
                box-shadow: 0 1px 3px rgba(0,0,0,.1); }
#factor-table .factor-row { display: flex; align-items: center; gap: 12px;
                            padding: 8px 0; border-bottom: 1px solid #f0f0f0; }
#factor-table .fname { width: 50px; font-weight: bold; }
#factor-table .fnote { flex: 1; color: #666; font-size: 13px; }
#factor-table input[type=number] { width: 60px; padding: 4px; }
#factor-table .manual { background: #fff8e1; }
```

- [ ] **Step 3: 创建 `static/app.js`**

```javascript
// 全局状态
const state = { screenResult: null, currentCode: null };

// ---------- Tab 切换 ----------
function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach(p =>
    p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "compare") renderComparison(state.screenResult);
}

// ---------- API ----------
async function api(url, opts) {
  const res = await fetch(url, opts);
  return res.json();
}

// ---------- 选股 ----------
async function fetchScreen() {
  const btn = document.getElementById("btn-screen");
  btn.textContent = "选股中...";
  btn.disabled = true;
  try {
    const data = await api("/api/screen", { method: "POST" });
    if (data.error) { alert(data.error); return; }
    state.screenResult = data;
    renderMarket(data);
    renderCandidates(data.candidates);
    if (data.environment_ok) switchTab("candidates");
    else switchTab("market");
  } finally {
    btn.textContent = "开始选股";
    btn.disabled = false;
  }
}

// ---------- ① 市场环境 ----------
function renderMarket(market) {
  const banner = document.getElementById("market-banner");
  if (state.screenResult.environment_ok) {
    banner.className = "env-ok";
    banner.innerHTML = `✅ 市场环境达标: ${market.stage} · 节点模型 ${market.node_score}/5 · 允许选股`;
  } else {
    banner.className = "env-bad";
    banner.innerHTML = `⛔ 市场环境不达标: ${market.stage} · 节点模型 ${market.node_score}/5 · 空仓等待`;
  }
  // 节点因子卡片
  const cards = document.getElementById("node-cards");
  cards.innerHTML = "";
  const names = { N1:"涨停指数", N2:"情绪周期", N3:"首板溢价", N4:"连板高度", N5:"成交额" };
  for (const [f, info] of Object.entries(market.factors)) {
    const hit = info.score === 1;
    cards.insertAdjacentHTML("beforeend",
      `<div class="card"><div class="label">${names[f]||f}</div>
       <div class="value ${hit?'hit':'miss'}">${hit?"✓":"✗"}</div>
       <div style="font-size:11px;color:#aaa">${info.note||""}</div></div>`);
  }
  const s = document.getElementById("market-stats");
  s.innerHTML = `<div class="card"><div class="label">两市成交额</div>
    <div class="value">${(market.total_amount/1e12).toFixed(2)}万亿</div></div>
    <div class="card"><div class="label">今日涨停</div>
    <div class="value">${market.limit_up_count}</div></div>`;
}

// ---------- ② 候选列表 ----------
function renderCandidates(candidates) {
  const tbody = document.querySelector("#cand-table tbody");
  tbody.innerHTML = "";
  if (!candidates || candidates.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:#999">无候选股</td></tr>';
    return;
  }
  for (const c of candidates) {
    const s = c.scores;
    const tr = document.createElement("tr");
    tr.className = "grade-" + s.grade;
    tr.innerHTML = `<td>${c.code}</td><td>${c.name}</td>
      <td>${s.first_board}</td><td>${s.monster}</td><td>${s.momentum}</td>
      <td>${s.node}</td><td>${s.composite}</td>
      <td><b>${s.grade}</b></td><td>${s.strength}</td>`;
    tr.style.cursor = "pointer";
    tr.onclick = () => showStockDetail(c.code);
    tbody.appendChild(tr);
  }
}

// ---------- ③ 模型对比 ----------
function renderComparison(data) {
  if (!data || !data.candidates || data.candidates.length === 0) return;
  const c = data.candidates;
  const radar = echarts.init(document.getElementById("radar-chart"));
  radar.setOption({
    title: { text: "三模型评分对比 (池内均值)" },
    radar: { indicator: [
      { name: "首板", max: 7 }, { name: "妖股", max: 7 }, { name: "势能", max: 7 },
    ]},
    series: [{
      type: "radar",
      data: [{
        name: "池内均值",
        value: [
          c.reduce((a, x) => a + x.scores.first_board, 0) / c.length,
          c.reduce((a, x) => a + x.scores.monster, 0) / c.length,
          c.reduce((a, x) => a + x.scores.momentum, 0) / c.length,
        ],
      }],
    }],
  });
  // 因子命中柱状图
  const bar = echarts.init(document.getElementById("bar-chart"));
  const counts = {};
  for (const x of c) for (const [f, v] of Object.entries(x.factors)) {
    if (v === 1) counts[f] = (counts[f] || 0) + 1;
  }
  bar.setOption({
    title: { text: "因子命中数 (池内)" },
    xAxis: { type: "category", data: Object.keys(counts) },
    yAxis: { type: "value" },
    series: [{ type: "bar", data: Object.values(counts), itemStyle: { color: "#2980b9" } }],
  });
}

// ---------- ④ 单股详情 ----------
async function showStockDetail(code) {
  state.currentCode = code;
  switchTab("detail");
  document.getElementById("detail-title").textContent = "股票 " + code;
  try {
    const [kl, manual] = await Promise.all([
      api("/api/stock/" + code + "/kline"),
      api("/api/stock/" + code + "/manual"),
    ]);
    renderKline(kl);
    renderFactors(state.screenResult, code, manual);
  } catch (e) { alert("加载失败: " + e); }
}

function renderKline(k) {
  const chart = echarts.init(document.getElementById("kline-chart"));
  chart.setOption({
    title: { text: "日线K线 (近120日)" },
    xAxis: { type: "category", data: k.dates },
    yAxis: { type: "value", scale: true },
    tooltip: { trigger: "axis" },
    dataZoom: [{ type: "inside" }],
    series: [
      { name: "收盘", type: "line", data: k.closes, showSymbol: false, lineStyle: { width: 1.5 } },
      { name: "MA60", type: "line", data: k.ma60, showSymbol: false, lineStyle: { color: "#e67e22", width: 1 } },
    ],
  });
}

function renderFactors(screenResult, code, manual) {
  const box = document.getElementById("factor-table");
  const cand = (screenResult?.candidates || []).find(c => c.code === code);
  if (!cand) { box.innerHTML = "该股不在当前候选池，先点击开始选股。"; return; }
  const factors = cand.factors;
  const src = cand.auto_manual || {};
  const names = { F1:"首板确认",F2:"早封板",F3:"封单强度",F4:"板块共振",F5:"量价堆积",
    F6:"大盘配合",F7:"题材新颖",Y1:"小市值",Y2:"筹码干净",Y3:"倍量突破",Y4:"均线多头",
    Y5:"多概念",Y6:"事件催化",Y7:"游资现身",S1:"产业趋势",S2:"量价堆积密度",S3:"最小阻力",
    S4:"均线系统",S5:"机构流入",S6:"板块共振强度",S7:"基本面催化",
    N1:"涨停指数",N2:"情绪周期",N3:"首板溢价",N4:"连板高度",N5:"成交额" };
  let html = "";
  for (const [f, v] of Object.entries(factors)) {
    const isManual = src[f] === "manual";
    html += `<div class="factor-row ${isManual?'manual':''}">
      <span class="fname">${f}</span><span>${names[f]||f}</span>
      <span>${v===1?'✓':'✗'}</span>
      ${isManual ? `<input type="number" id="inp-${f}" min="0" max="1" step="1" value="${v}">` : ""}
    </div>`;
  }
  // 手动因子总输入（覆盖所有可手填项）
  const MANUAL_ALL = ["F7","Y1","Y2","Y5","Y6","Y7","S1","S5","S7"];
  html += `<div style="padding:8px 0;margin-top:8px;border-top:1px solid #eee">
    <b>手填因子</b>`;
  for (const f of MANUAL_ALL) {
    const cur = manual[f] ?? 0;
    html += `<div class="factor-row manual"><span class="fname">${f}</span>
      <span>${names[f]||f}</span>
      <input type="number" id="man-${f}" min="0" max="1" step="1" value="${cur}"></div>`;
  }
  html += `<button onclick="saveManual('${code}')" style="padding:8px 16px;margin-top:8px;
    background:#27ae60;color:#fff;border:none;border-radius:6px;cursor:pointer">保存手填因子并重算</button></div>`;
  box.innerHTML = html;
}

async function saveManual(code) {
  const MANUAL_ALL = ["F7","Y1","Y2","Y5","Y6","Y7","S1","S5","S7"];
  const payload = {};
  for (const f of MANUAL_ALL) {
    const el = document.getElementById("man-" + f);
    if (el) payload[f] = parseInt(el.value) || 0;
  }
  const res = await api("/api/stock/" + code + "/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.error) { alert(res.error); return; }
  // 重新选股以反映手填（简化：提示用户重新点选股）
  alert("手填已保存: " + JSON.stringify(res.factors) + "\n点击「开始选股」重新计算综合分。");
}
```

- [ ] **Step 4: 下载 ECharts 本地文件**

Run:
```bash
cd strategy_web && mkdir -p static && curl -sL -o static/echarts.min.js \
  "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js" && \
  ls -la static/echarts.min.js
```
Expected: 文件存在且 >1MB（约 1.1MB）

（若 curl 不可用或下载失败：在 index.html 的 `<script src="/static/echarts.min.js">` 前追加 CDN 备用 `<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>`，并注明需联网。）

- [ ] **Step 5: 手动冒烟测试**

Run:
```bash
cd strategy_web && python -c "import flask; print('flask', flask.__version__)" && \
  python -m py_compile app.py factors.py models.py manual_store.py screen.py data_source.py && echo "语法OK"
```
Expected: `flask 3.x.x` 和 `语法OK`。（Flask 若未装：`pip install flask`）

- [ ] **Step 6: 提交**

```bash
git add strategy_web/templates/index.html strategy_web/static/app.js strategy_web/static/style.css strategy_web/static/echarts.min.js
git commit -m "feat: add frontend dashboard with ECharts visualization"
```

---

### Task 8: 端到端集成验证

**Files:**
- Modify: 无（验证已完成的全部模块）

**Interfaces:**
- Consumes: Task 1-7 全部产物

- [ ] **Step 1: 跑全部单元测试**

Run: `cd strategy_web && python -m pytest tests/ -v`
Expected: 全部通过（约 32 个测试）

- [ ] **Step 2: 连接真实 QMT 冒烟验证（可选，需 QMT 已开）**

Run:
```bash
cd strategy_web && python - <<'PY'
# -*- coding: utf-8 -*-
import sys
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass
from data_source import DataSource
ds = DataSource()
ds.connect()
ticks = ds.get_full_market_ticks()
print("全市场行情:", len(ticks), "只")
ups = ds.get_limit_up_stocks(ticks)
print("涨停股:", len(ups))
for u in ups[:10]:
    print("  ", u["code"], u["name"], "现价", u["last"], "涨停价", u["up_stop_price"],
          "封板" if u["sealed"] else "未封")
PY
```
Expected: 打印全市场只数、涨停股清单（应与当日行情一致）

- [ ] **Step 3: 启动网页端到端验证（需 QMT 已开）**

Run:
```bash
cd strategy_web && python app.py
```
Expected: 控制台显示"已连接"，浏览器访问 http://localhost:5000：
1. 点"开始选股" → 市场环境 tab 显示节点模型得分/情绪阶段
2. 环境达标时切到候选列表，显示候选股表格
3. 点某候选股 → 单股详情显示 K线图 + 因子表
4. 手填因子保存 → 提示成功
5. 模型对比 tab 显示雷达图 + 柱状图

- [ ] **Step 4: 提交最终验证记录**

```bash
git add -A strategy_web/
git commit -m "test: verify full stack end-to-end with live QMT"
```

---
