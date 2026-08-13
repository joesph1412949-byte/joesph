# QMT 本地数据源 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 strategy_web 扩展成 QMT 本地数据服务(新增批量K线/涨停股/实时盘口三个只读接口),优化选股 51 秒重筛(指数K线拉一次 + K线批量),并在 Vibe-Trading 侧新增 `qmt_data` 工具让 agent 走本地数据。

**Architecture:** strategy_web(Py3.7, 唯一能 import xtquant 的进程)新增三个 Flask 路由暴露已有 `DataSource` 能力;`ScreenRunner.run()` 预拉指数K线 + 批量拉涨停股K线传入 `compute_factors`;Vibe-Trading(Py3.12)新增 `qmt_data` 工具用 urllib 读这些接口。数据只在 127.0.0.1 流转,只读、不下单、不外发。

**Tech Stack:** Flask 2.1.3 + xtquant(xtdata) / 标准库 urllib / pytest(mock xtdata)。

## Global Constraints

- 只读、本地、无下单、无外发;不 push 到任何 remote(本地工作流)。
- strategy_web 用 **Python 3.7.4**(`C:\Users\28037\AppData\Local\Programs\Python\Python37\python.exe`),Vibe-Trading 用 `.venv`(Python 3.12)。
- xtdata **无 `connect()`**(RPC 自动连接);`app.run()` 前需 monkeypatch `socket.getfqdn`(已存在,勿删)。
- agent 工具遵循既有 BaseTool 模式(子类、`execute(**kwargs)`、fail-open 返回 JSON、不抛异常)。
- strategy_web 测试命令:`cd d:\cc-joesph\strategy_web && py -3.12 -m pytest tests/<file>.py -v`(pytest 在 py3.12,离线 mock xtdata)。
- Vibe-Trading 测试命令:`cd D:\Vibe-Trading && .venv\Scripts\python.exe -m pytest agent/tests/<file>.py -v`。
- 现有 100(strategy_web)+ 7(strategy_screen)测试必须保持通过。

---

### Task 1: `DataSource.get_kline_bulk` 批量K线方法

**Files:**
- Modify: `strategy_web/data_source.py`(在 `get_kline` 之后新增方法)
- Test: `strategy_web/tests/test_data_source.py`

**Interfaces:**
- Produces: `DataSource.get_kline_bulk(codes, days=250, period="1d") -> dict[str, DataFrame]`,失败个股跳过(不进返回 dict);空 codes 返回 `{}`;未连接抛 `DataSourceError`。Task 3/4 依赖。
- Uses: `xtdata.download_history_data2(codes, period, start_time="", end_time="")` + `xtdata.get_market_data_ex([], codes, period=period, start_time="", end_time="", count=days)`(返回 `{code: DataFrame}`)。

- [ ] **Step 1: 写失败测试** — 在 `test_data_source.py` 末尾加:

```python
def test_get_kline_bulk_batches_codes_once(monkeypatch):
    calls = {"codes": None}
    def spy_dl2(codes, period, start_time="", end_time=""):
        calls["codes"] = list(codes)
    monkeypatch.setattr(xtdata_mod, "download_history_data2", spy_dl2)
    d = DataSource()
    d._connected = True
    k = d.get_kline_bulk(["002859.SZ", "600353.SH"], days=250)
    assert calls["codes"] == ["002859.SZ", "600353.SH"]   # 批量下载一次
    assert set(k.keys()) == {"002859.SZ", "600353.SH"}    # 只含请求的 codes

def test_get_kline_bulk_skips_missing_code():
    d = DataSource()
    d._connected = True
    k = d.get_kline_bulk(["002859.SZ", "999999.SZ"], days=250)
    assert "002859.SZ" in k
    assert "999999.SZ" not in k      # mock get_market_data_ex 无 999999 → 跳过

def test_get_kline_bulk_requires_connect():
    d = DataSource()
    with pytest.raises(DataSourceError):
        d.get_kline_bulk(["002859.SZ"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd d:\cc-joesph\strategy_web && py -3.12 -m pytest tests/test_data_source.py -v`
Expected: 三个新测试 FAIL(`AttributeError: 'DataSource' object has no attribute 'get_kline_bulk'`)。

- [ ] **Step 3: 实现** — 在 `data_source.py` 的 `get_kline` 方法后新增:

```python
    def get_kline_bulk(self, codes, days=250, period="1d"):
        """批量拉多只股票日K线, 返回 {code: DataFrame}。失败个股跳过。"""
        if not self._connected:
            raise DataSourceError("未连接，请先调用 connect()")
        codes = list(codes)
        if not codes:
            return {}
        try:
            xtdata.download_history_data2(codes, period, start_time="", end_time="")
            time.sleep(0.05)
            k = xtdata.get_market_data_ex([], codes, period=period,
                                          start_time="", end_time="", count=days)
            out = {}
            for c in codes:
                df = (k or {}).get(c)
                if df is not None and len(df) > 0:
                    out[c] = df
            return out
        except Exception as e:
            raise DataSourceError("批量获取K线失败: %r" % e)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd d:\cc-joesph\strategy_web && py -3.12 -m pytest tests/test_data_source.py -v`
Expected: 全部 PASS(含既有 data_source 测试)。

- [ ] **Step 5: Commit**

```bash
git add strategy_web/data_source.py strategy_web/tests/test_data_source.py
git commit -m "feat(data_source): get_kline_bulk 批量拉多只K线(download_history_data2)"
```

---

### Task 2: `FactorEngine.compute_factors` 支持注入 kline/index_kline

**Files:**
- Modify: `strategy_web/factors.py`(签名 + 两处拉取逻辑)
- Test: `strategy_web/tests/test_factors.py`

**Interfaces:**
- Produces: `compute_factors(self, code, tick, detail, ds, sector_map, limit_ups=None, market=None, kline=None, index_kline=None)`。`kline`/`index_kline` 为 None 时回落内部调用 `ds.get_kline`/`ds.get_index_kline`(保持现有测试兼容);传入则不再回调 ds。Task 3 依赖。
- Consumes: `ds.get_kline(code, days=250)` / `ds.get_index_kline("000001.SH", days=30)`(回落路径)。

- [ ] **Step 1: 写失败测试** — 在 `test_factors.py` 末尾加:

```python
def test_compute_factors_uses_injected_kline_and_index():
    # 传入 kline/index_kline 后, 不再回调 ds.get_kline/get_index_kline
    calls = {"kline": 0, "index": 0}
    class SpyDS(FakeDS):
        def get_kline(self, code, days=120):
            calls["kline"] += 1
            return super().get_kline(code, days)
        def get_index_kline(self, code, days=60):
            calls["index"] += 1
            return super().get_index_kline(code, days)
    kline = make_kline(list(np.linspace(10, 30, 300)), [100000]*300)
    idx = make_kline([3000]*60, [100000]*60)
    ds = SpyDS(kline_map={"000001.SZ": kline}, index_map={"000001.SH": idx})
    eng = FactorEngine()
    tick = {"lastPrice":30.0,"lastClose":29.0,"sealed":True,"amount":1e6,"volume":200000}
    detail = {"UpStopPrice":33.0,"FloatVolume":1e8}
    r = eng.compute_factors("000001.SZ", tick, detail, ds, sector_map={},
                            kline=kline, index_kline=idx)
    assert calls["kline"] == 0        # 注入后不再逐只拉
    assert calls["index"] == 0        # 注入后不再重复拉指数
    assert "F6" in r and "S4" in r    # 用注入数据仍算得出因子
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd d:\cc-joesph\strategy_web && py -3.12 -m pytest tests/test_factors.py::test_compute_factors_uses_injected_kline_and_index -v`
Expected: FAIL(`TypeError: compute_factors() got an unexpected keyword argument 'kline'`)。

- [ ] **Step 3: 实现** — 在 `factors.py` 改三处:

(1) 签名:

```python
    def compute_factors(self, code, tick, detail, ds, sector_map,
                        limit_ups=None, market=None, kline=None, index_kline=None):
```

(2) K线拉取(原 `try: kline = ds.get_kline(code, days=250) ...` 处)改为:

```python
        if kline is None:
            try:
                kline = ds.get_kline(code, days=250)
            except Exception:
                kline = None
```

(3) F6 指数拉取(原 `idx = ds.get_index_kline("000001.SH", days=30)` 处)改为:

```python
            idx = index_kline
            if idx is None:
                idx = ds.get_index_kline("000001.SH", days=30)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd d:\cc-joesph\strategy_web && py -3.12 -m pytest tests/test_factors.py -v`
Expected: 全部 PASS(含既有 factors 测试——它们不传 kline/index_kline,走回落路径)。

- [ ] **Step 5: Commit**

```bash
git add strategy_web/factors.py strategy_web/tests/test_factors.py
git commit -m "feat(factors): compute_factors 支持注入 kline/index_kline(回落兼容)"
```

---

### Task 3: `ScreenRunner.run()` 提速(指数拉一次 + K线批量)

**Files:**
- Modify: `strategy_web/screen.py`(`run()` 方法)
- Modify: `strategy_web/tests/test_screen.py`(`FakeDS` 加 `get_kline_bulk`;`SpyEngine.compute_factors` 签名加参数;新增提速验证测试)

**Interfaces:**
- Consumes: `DataSource.get_index_kline("000001.SH", days=30)`、`DataSource.get_kline_bulk(codes, days=250)`(Task 1);`FactorEngine.compute_factors(..., kline=..., index_kline=...)`(Task 2)。
- 行为不变:run() 返回结构、候选清单、门槛过滤、排序完全等价,仅数据获取方式改变。

- [ ] **Step 1: 写失败测试** — 在 `test_screen.py` 末尾加:

```python
def test_screen_batches_kline_and_index_once():
    calls = {"kline_bulk": 0, "index": 0}
    class SpyDS(FakeDS):
        def get_kline_bulk(self, codes, days=250, period="1d"):
            calls["kline_bulk"] += 1
            return {c: self.get_kline(c, days) for c in codes}
        def get_index_kline(self, code, days=60):
            calls["index"] += 1
            return super().get_index_kline(code, days)
    class SpyEngine(FakeEngine):
        def compute_factors(self, code, tick, detail, ds, sector_map,
                            limit_ups=None, market=None, kline=None, index_kline=None):
            self.seen = (kline, index_kline)
            return FakeEngine.compute_factors(self, code, tick, detail, ds,
                                              sector_map, limit_ups, market)
    eng = SpyEngine()
    r = ScreenRunner(ds=SpyDS(), engine=eng, store=FakeStore(), scorer=FakeScorer(),
                     em_feed=FakeEastMoneyFeed(stats=DEFAULT_EM),
                     fund_feed=FakeFundFeed()).run()
    assert r["candidates"][0]["code"] == "002859.SZ"
    assert calls["kline_bulk"] == 1        # 批量拉一次
    assert calls["index"] == 1             # 指数拉一次
    assert eng.seen[0] is not None         # kline 已注入
    assert eng.seen[1] is not None         # index_kline 已注入
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd d:\cc-joesph\strategy_web && py -3.12 -m pytest tests/test_screen.py -v`
Expected: FAIL(`TypeError`/`AttributeError`——`FakeDS` 无 `get_kline_bulk`,且 `SpyEngine.compute_factors` 收不到 `kline=` 关键字)。

- [ ] **Step 3: 实现** — 改 `screen.py` 的 `run()` 两处:

(1) 在 `sector_map = self._build_sector_map(limit_ups)` 之前插入预拉:

```python
        # 预拉指数K线 + 批量涨停股K线(提速: 避免每只涨停股重复拉取)
        index_kline = None
        try:
            index_kline = self.ds.get_index_kline("000001.SH", days=30)
        except Exception:
            index_kline = None
        kline_map = {}
        if limit_ups:
            try:
                kline_map = self.ds.get_kline_bulk([u["code"] for u in limit_ups], days=250)
            except Exception:
                kline_map = {}
```

(2) 循环里 `compute_factors` 调用加关键字:

```python
                auto_raw = self.engine.compute_factors(code, tick, detail, self.ds,
                                                       sector_map, limit_ups, None,
                                                       kline=kline_map.get(code),
                                                       index_kline=index_kline)
```

(3) 更新 `test_screen.py`:

- `FakeDS` 类加方法:

```python
    def get_kline_bulk(self, codes, days=250, period="1d"):
        return {c: self.get_kline(c, days) for c in codes}
```

- `test_screen_passes_real_tick_and_detail_to_engine` 里的 `SpyEngine.compute_factors` 签名改为:

```python
        def compute_factors(self, code, tick, detail, ds, sector_map,
                            limit_ups=None, market=None, kline=None, index_kline=None):
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd d:\cc-joesph\strategy_web && py -3.12 -m pytest tests/test_screen.py -v`
Expected: 全部 PASS(含既有 screen 测试 + 新提速测试)。

- [ ] **Step 5: Commit**

```bash
git add strategy_web/screen.py strategy_web/tests/test_screen.py
git commit -m "perf(screen): 指数K线拉一次 + 涨停股K线批量拉取(提速重筛)"
```

---

### Task 4: `app.py` 三个新路由 + `.gitignore`

**Files:**
- Modify: `strategy_web/app.py`(常量 + 快照函数 + 三路由)
- Modify: `.gitignore`(Runtime data 段加 `limitup_result.json`/`.tmp`)
- Test: `strategy_web/tests/test_app.py`

**Interfaces:**
- Produces(供 Task 5 的 qmt_data 工具消费):
  - `GET /api/market/kline?codes=600000.SH,000001.SZ&days=120&period=1d` → `{"ok":true,"data":{code:{dates,open,high,low,close,volume,amount}}}`;codes 缺 → 400。
  - `GET /api/market/limitup` → 读快照;`POST /api/market/limitup` → 实时算+落快照+返回。返回 `{"ok":true,"generated_at":...,"data":[{code,name,last,last_close,up_stop_price,sealed,amount,volume,float_volume,open_date}]}`;无快照 GET → 404。
  - `GET /api/market/tick?codes=...` → `{"ok":true,"data":{code:{lastPrice,lastClose,askPrice,bidPrice,bidVol,volume,amount,high,low,open,time}}}`;codes 缺 → 400。
- Consumes: `ds_obj.get_kline_bulk`、`ds_obj.get_full_market_ticks`、`ds_obj.get_limit_up_stocks`;复用 `_fmt_date`。

- [ ] **Step 1: 写失败测试** — 在 `test_app.py` 末尾加:

```python
def test_market_kline_bulk(client, monkeypatch):
    import pandas as pd, numpy as np
    n = 120
    ts = (pd.date_range("2026-01-01", periods=n, freq="B").astype("int64") // 10**6).tolist()
    class FakeDS:
        _connected = True
        def get_kline_bulk(self, codes, days=120, period="1d"):
            return {c: pd.DataFrame({"time": ts, "open": np.full(n,10.0),
                                     "high": np.full(n,10.5), "low": np.full(n,9.5),
                                     "close": np.full(n,10.2), "volume": np.full(n,100000),
                                     "amount": np.full(n,1e6)}) for c in codes}
    monkeypatch.setattr(app_module, "ds_obj", FakeDS())
    r = client.get("/api/market/kline?codes=600000.SH,000001.SZ&days=120")
    assert r.status_code == 200
    data = r.get_json()
    assert data["ok"] is True
    assert set(data["data"].keys()) == {"600000.SH", "000001.SZ"}
    assert len(data["data"]["600000.SH"]["close"]) == 120

def test_market_kline_requires_codes(client):
    r = client.get("/api/market/kline")
    assert r.status_code == 400

def test_market_tick(client, monkeypatch):
    class FakeDS:
        _connected = True
        def get_full_market_ticks(self, codes=None):
            return {"600000.SH": {"lastPrice": 10.5, "lastClose": 10.0}}
    monkeypatch.setattr(app_module, "ds_obj", FakeDS())
    r = client.get("/api/market/tick?codes=600000.SH")
    assert r.status_code == 200
    assert r.get_json()["data"]["600000.SH"]["lastPrice"] == 10.5

def test_market_tick_requires_codes(client):
    r = client.get("/api/market/tick")
    assert r.status_code == 400

def test_market_limitup_refresh_and_read(client, tmp_path, monkeypatch):
    snap = tmp_path / "limitup_result.json"
    monkeypatch.setattr(app_module, "LIMITUP_SNAPSHOT_PATH", snap)
    class FakeDS:
        _connected = True
        def get_full_market_ticks(self, codes=None):
            return {"600000.SH": {"lastPrice": 10.5}}
        def get_limit_up_stocks(self, ticks=None):
            return [{"code": "600000.SH", "name": "浦发银行", "last": 10.5}]
    monkeypatch.setattr(app_module, "ds_obj", FakeDS())
    r = client.post("/api/market/limitup")
    assert r.status_code == 200
    assert snap.is_file()
    assert r.get_json()["data"][0]["code"] == "600000.SH"
    r2 = client.get("/api/market/limitup")
    assert r2.status_code == 200
    assert r2.get_json()["data"][0]["code"] == "600000.SH"

def test_market_limitup_no_snapshot(client, tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "LIMITUP_SNAPSHOT_PATH", tmp_path / "nope.json")
    r = client.get("/api/market/limitup")
    assert r.status_code == 404
    assert r.get_json()["ok"] is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd d:\cc-joesph\strategy_web && py -3.12 -m pytest tests/test_app.py -v`
Expected: 新测试 FAIL(404 `The requested URL was not found on the server`)。

- [ ] **Step 3: 实现** — 改 `app.py`:

(1) 在 `SNAPSHOT_PATH` 常量后加:

```python
LIMITUP_SNAPSHOT_PATH = Path(__file__).parent / "limitup_result.json"
```

(2) 在 `_save_snapshot` 后加:

```python
def _save_limitup_snapshot(limit_ups):
    """涨停股列表落盘快照(原子写), 供 GET 秒读。"""
    snapshot = {
        "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "data": limit_ups,
    }
    tmp = LIMITUP_SNAPSHOT_PATH.with_suffix(".json.tmp")
    tmp.write_text(_json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, LIMITUP_SNAPSHOT_PATH)
    return snapshot
```

(3) 在 `/api/screen/latest` 路由后加三个路由:

```python
@app.route("/api/market/kline", methods=["GET"])
def market_kline():
    if not ds_obj._connected:
        return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
    codes = [c.strip() for c in (request.args.get("codes") or "").split(",") if c.strip()]
    if not codes:
        return jsonify({"error": "缺少 codes 参数"}), 400
    days = int(request.args.get("days", 120))
    period = request.args.get("period", "1d")
    try:
        kline_map = ds_obj.get_kline_bulk(codes, days=days, period=period)
    except DataSourceError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    data = {}
    for code, df in kline_map.items():
        item = {
            "dates": [_fmt_date(t) for t in df["time"]],
            "open": [float(x) for x in df["open"]],
            "high": [float(x) for x in df["high"]],
            "low": [float(x) for x in df["low"]],
            "close": [float(x) for x in df["close"]],
            "volume": [int(v) for v in df["volume"]],
        }
        if "amount" in df.columns:
            item["amount"] = [float(a) for a in df["amount"]]
        data[code] = item
    if not data:
        return jsonify({"ok": False, "error": "无法获取任何股票的K线"}), 500
    return jsonify({"ok": True, "data": data})


@app.route("/api/market/limitup", methods=["GET", "POST"])
def market_limitup():
    if request.method == "POST":
        if not ds_obj._connected:
            return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
        try:
            ticks = ds_obj.get_full_market_ticks()
            limit_ups = ds_obj.get_limit_up_stocks(ticks)
        except DataSourceError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        snap = _save_limitup_snapshot(limit_ups)
        snap["ok"] = True
        return jsonify(snap)
    if not LIMITUP_SNAPSHOT_PATH.is_file():
        return jsonify({"ok": False, "error": "尚未刷新, 请先 POST /api/market/limitup"}), 404
    try:
        data = _json.loads(LIMITUP_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        data["ok"] = True
        return jsonify(data)
    except Exception as e:
        return jsonify({"ok": False, "error": "快照读取失败: %r" % e}), 500


@app.route("/api/market/tick", methods=["GET"])
def market_tick():
    if not ds_obj._connected:
        return jsonify({"error": "QMT未连接, 请先打开QMT并开启miniQMT"}), 400
    codes = [c.strip() for c in (request.args.get("codes") or "").split(",") if c.strip()]
    if not codes:
        return jsonify({"error": "缺少 codes 参数"}), 400
    try:
        ticks = ds_obj.get_full_market_ticks(codes)
    except DataSourceError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "data": ticks})
```

(4) `.gitignore` 的 Runtime data 段加两行(`screen_result.json` 之后):

```
limitup_result.json
limitup_result.json.tmp
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd d:\cc-joesph\strategy_web && py -3.12 -m pytest tests/test_app.py -v`
Expected: 全部 PASS(含既有 app 测试 + 6 个新测试)。

- [ ] **Step 5: Commit**

```bash
git add strategy_web/app.py strategy_web/tests/test_app.py .gitignore
git commit -m "feat(app): 新增 /api/market/kline + /limitup + /tick 三个 QMT 数据接口"
```

---

### Task 5: Vibe-Trading `qmt_data` 工具

**Files:**
- Create: `D:\Vibe-Trading\agent\src\tools\qmt_data_tool.py`
- Test: `D:\Vibe-Trading\agent\tests\test_qmt_data_tool.py`

**Interfaces:**
- Produces: `QmtDataTool(BaseTool)`,`name="qmt_data"`,`is_readonly=True`;operations `kline/limitup/limitup_refresh/tick/health`;base URL 可被 `STRATEGY_WEB_URL` 环境变量覆盖。
- Consumes: strategy_web 的 `GET /api/market/kline`、`GET/POST /api/market/limitup`、`GET /api/market/tick`、`GET /api/health`(Task 4)。

- [ ] **Step 1: 写失败测试** — 新建 `test_qmt_data_tool.py`:

```python
import json

from src.tools.qmt_data_tool import QmtDataTool


class _FakeResp:
    def __init__(self, status, body):
        self.status = status
        self._body = body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def read(self):
        return self._body.encode("utf-8")


def _patch(monkeypatch, handler):
    import src.tools.qmt_data_tool as mod
    monkeypatch.setattr(mod.urllib.request, "urlopen", handler)


def test_kline_fetches_bulk(monkeypatch):
    body = json.dumps({"ok": True, "data": {"600000.SH": {"close": [1, 2]}}})
    def _h(req, timeout=None):
        assert "codes=600000.SH" in req.full_url
        return _FakeResp(200, body)
    _patch(monkeypatch, _h)
    out = json.loads(QmtDataTool().execute(operation="kline", codes="600000.SH"))
    assert out["ok"] is True and "600000.SH" in out["data"]

def test_kline_requires_codes(monkeypatch):
    out = json.loads(QmtDataTool().execute(operation="kline"))
    assert out["ok"] is False and "codes" in out["error"]

def test_limitup_reads_snapshot(monkeypatch):
    def _h(req, timeout=None):
        assert req.full_url.endswith("/api/market/limitup")
        return _FakeResp(200, json.dumps({"ok": True, "data": [{"code": "600000.SH"}]}))
    _patch(monkeypatch, _h)
    out = json.loads(QmtDataTool().execute(operation="limitup"))
    assert out["ok"] is True and out["data"][0]["code"] == "600000.SH"

def test_limitup_refresh_posts(monkeypatch):
    def _h(req, timeout=None):
        assert req.get_method() == "POST"
        assert timeout == 180
        return _FakeResp(200, json.dumps({"ok": True, "data": []}))
    _patch(monkeypatch, _h)
    out = json.loads(QmtDataTool().execute(operation="limitup_refresh"))
    assert out["ok"] is True

def test_tick_fetches(monkeypatch):
    def _h(req, timeout=None):
        assert "codes=600000.SH" in req.full_url
        return _FakeResp(200, json.dumps({"ok": True, "data": {"600000.SH": {"lastPrice": 10.5}}}))
    _patch(monkeypatch, _h)
    out = json.loads(QmtDataTool().execute(operation="tick", codes="600000.SH"))
    assert out["data"]["600000.SH"]["lastPrice"] == 10.5

def test_tick_requires_codes(monkeypatch):
    out = json.loads(QmtDataTool().execute(operation="tick"))
    assert out["ok"] is False and "codes" in out["error"]

def test_health(monkeypatch):
    def _h(req, timeout=None):
        assert req.full_url.endswith("/api/health")
        return _FakeResp(200, json.dumps({"ok": True, "qmt_connected": True}))
    _patch(monkeypatch, _h)
    out = json.loads(QmtDataTool().execute(operation="health"))
    assert out["ok"] is True and out["qmt_connected"] is True

def test_connection_error_fail_open(monkeypatch):
    def _h(req, timeout=None):
        raise OSError("connection refused")
    _patch(monkeypatch, _h)
    out = json.loads(QmtDataTool().execute(operation="limitup"))
    assert out["ok"] is False and "数据服务" in out["error"]

def test_unknown_operation(monkeypatch):
    out = json.loads(QmtDataTool().execute(operation="nope"))
    assert out["ok"] is False and "未知操作" in out["error"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd D:\Vibe-Trading && .venv\Scripts\python.exe -m pytest agent/tests/test_qmt_data_tool.py -v`
Expected: FAIL(`ModuleNotFoundError: No module named 'src.tools.qmt_data_tool'`)。

- [ ] **Step 3: 实现** — 新建 `qmt_data_tool.py`:

```python
"""QMT 本地行情数据工具(只读)。

通过 HTTP 调 strategy_web(Flask, 127.0.0.1:5000)读取 QMT miniQMT 本地数据:
kline=批量K线; limitup=涨停股列表(读快照); limitup_refresh=涨停股列表(实时重算);
tick=指定股票实时盘口; health=查数据服务/QMT 状态。
服务不可用时 fail-open, 返回带提示的 JSON, 不抛异常。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from src.agent.tools import BaseTool

DEFAULT_BASE_URL = "http://127.0.0.1:5000"

_PARAMS = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["kline", "limitup", "limitup_refresh", "tick", "health"],
            "description": "kline=批量K线; limitup=涨停股列表(读快照秒回); limitup_refresh=涨停股列表(实时重算); tick=指定股票实时盘口; health=查服务/QMT状态",
        },
        "codes": {"type": "string", "description": "逗号分隔股票代码(kline/tick 必填), 如 '600000.SH,000001.SZ'"},
        "days": {"type": "integer", "default": 120, "description": "K线天数(kline 用)"},
        "period": {"type": "string", "default": "1d", "description": "K线周期(kline 用)"},
    },
    "required": ["operation"],
}


class QmtDataTool(BaseTool):
    name = "qmt_data"
    description = ("读取 QMT miniQMT 本地行情数据(strategy_web 中转, 只读)。"
                   "operation: kline / limitup / limitup_refresh / tick / health。"
                   "kline 批量K线(codes 必填); limitup 读涨停股快照(秒回); "
                   "limitup_refresh 实时重算涨停股; tick 指定股票实时盘口(codes 必填); "
                   "health 查服务状态。若返回服务不可用, 说明 strategy_web 尚未运行。")
    parameters = _PARAMS
    is_readonly = True

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = (base_url or os.environ.get("STRATEGY_WEB_URL")
                          or DEFAULT_BASE_URL).rstrip("/")

    def execute(self, **kwargs: object) -> str:
        try:
            return self._dispatch(str(kwargs.get("operation") or ""), kwargs)
        except Exception as exc:  # noqa: BLE001 — 工具必须 fail-open
            return json.dumps(
                {"ok": False, "error": "QMT 数据服务不可用(%r)。请先运行 d:\\cc-joesph\\strategy_web 下的 app.py。" % exc},
                ensure_ascii=False)

    def _request(self, path: str, method: str, timeout: int) -> tuple[int, str]:
        req = urllib.request.Request(self._base_url + path, data=b"" if method == "POST" else None,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def _codes_arg(self, kw: dict) -> str | None:
        return (str(kw.get("codes") or "").strip()) or None

    def _dispatch(self, op: str, kw: dict) -> str:
        if op == "kline":
            return self._kline(kw)
        if op == "limitup":
            return self._limitup()
        if op == "limitup_refresh":
            return self._limitup_refresh()
        if op == "tick":
            return self._tick(kw)
        if op == "health":
            return self._health()
        return json.dumps({"ok": False, "error": "未知操作: %s" % op}, ensure_ascii=False)

    def _kline(self, kw: dict) -> str:
        codes = self._codes_arg(kw)
        if not codes:
            return json.dumps({"ok": False, "error": "kline 需要 codes 参数"}, ensure_ascii=False)
        days = kw.get("days") or 120
        period = str(kw.get("period") or "1d")
        path = "/api/market/kline?codes=%s&days=%s&period=%s" % (
            urllib.parse.quote(codes), days, period)
        status, body = self._request(path, "GET", 180)
        data = json.loads(body)
        if status != 200:
            return json.dumps({"ok": False, "error": data.get("error", "K线获取失败")}, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)

    def _limitup(self) -> str:
        status, body = self._request("/api/market/limitup", "GET", 10)
        data = json.loads(body)
        if status != 200:
            return json.dumps({"ok": False, "error": data.get("error", "涨停股快照不可用")}, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)

    def _limitup_refresh(self) -> str:
        status, body = self._request("/api/market/limitup", "POST", 180)
        data = json.loads(body)
        if status != 200:
            return json.dumps({"ok": False, "error": data.get("error", "涨停股刷新失败")}, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)

    def _tick(self, kw: dict) -> str:
        codes = self._codes_arg(kw)
        if not codes:
            return json.dumps({"ok": False, "error": "tick 需要 codes 参数"}, ensure_ascii=False)
        path = "/api/market/tick?codes=%s" % urllib.parse.quote(codes)
        status, body = self._request(path, "GET", 10)
        data = json.loads(body)
        if status != 200:
            return json.dumps({"ok": False, "error": data.get("error", "盘口获取失败")}, ensure_ascii=False)
        return json.dumps(data, ensure_ascii=False)

    def _health(self) -> str:
        status, body = self._request("/api/health", "GET", 5)
        data = json.loads(body)
        data["ok"] = (status == 200)
        return json.dumps(data, ensure_ascii=False)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd D:\Vibe-Trading && .venv\Scripts\python.exe -m pytest agent/tests/test_qmt_data_tool.py -v`
Expected: 9 个测试全部 PASS。

- [ ] **Step 5: Commit**

```bash
cd /d/Vibe-Trading && git add agent/src/tools/qmt_data_tool.py agent/tests/test_qmt_data_tool.py
git commit -m "feat(tools): qmt_data 只读工具(kline/limitup/tick/health 走 strategy_web)"
```

---

## 完成后的全量验证

```bash
cd d:\cc-joesph\strategy_web && py -3.12 -m pytest -q
cd /d/Vibe-Trading && .venv\Scripts\python.exe -m pytest agent/tests/test_strategy_screen_tool.py agent/tests/test_qmt_data_tool.py -q
```

两个都全绿(预计 strategy_web ~106、Vibe-Trading ~16)后,进入 finishing-a-development-branch(本地合并,不 push)。
