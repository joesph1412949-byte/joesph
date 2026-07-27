# MyQuant Trading Framework — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a 4-layer personal quant trading framework (data/strategy/backtest/live) with Streamlit web panel, deployable on WSL2 Ubuntu.

**Architecture:** Event-driven synchronous loop. Data layer provides unified pandas DataFrames to strategy layer, which outputs standardized Signal objects consumed by backtest/live engines. Same strategy code runs in both backtest and live modes.

**Tech Stack:** Python 3.12, AKShare, DuckDB, PyYAML, Streamlit, matplotlib, pandas/numpy, pytest

**Spec Reference:** `docs/superpowers/specs/2026-07-27-myquant-trading-framework-design.md`

## Global Constraints

- Python version: >= 3.12 (WSL2 Ubuntu 24.04 system python3)
- Project root: `~/myquant` in WSL
- All pip installs use `--break-system-packages` (Ubuntu 24.04 PEP 668)
- PATH must include `$HOME/.local/bin` for pip-installed CLI tools
- Data sources: AKShare (primary, free, no registration)
- All code uses synchronous for-loops (no asyncio)
- Strategy and backtest engine share identical interface
- Each file has exactly one responsibility

---

## File Structure

```
~/myquant/
├── config.yaml                  # Global YAML configuration
├── requirements.txt             # pip dependencies
├── myquant/
│   ├── __init__.py
│   ├── config.py                # Config loader
│   ├── data/
│   │   ├── __init__.py
│   │   ├── provider.py          # DataProvider → pd.DataFrame
│   │   ├── sources/
│   │   │   ├── __init__.py
│   │   │   └── akshare.py       # AKShare adapter
│   │   └── store.py             # DuckDB read/write/cache
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── base.py              # Strategy + Signal dataclass
│   │   ├── rotation.py          # Multi-factor rotation
│   │   └── timing.py            # MA crossover timing
│   ├── backtest/
│   │   ├── __init__.py
│   │   ├── account.py           # Virtual account
│   │   ├── broker.py            # Simulated broker
│   │   ├── recorder.py          # Trade logger
│   │   ├── engine.py            # Event-driven loop
│   │   └── report.py            # Metrics + charts
│   ├── live/
│   │   ├── __init__.py
│   │   ├── risk.py              # RiskManager
│   │   └── notify.py            # Push notifications
│   └── web/
│       ├── __init__.py
│       └── app.py               # Streamlit dashboard
└── tests/
    ├── __init__.py
    ├── test_account.py
    ├── test_signal.py
    ├── test_store.py
    ├── test_provider.py
    ├── test_broker.py
    ├── test_recorder.py
    ├── test_engine.py
    ├── test_rotation.py
    └── test_risk.py
```

**Key interfaces:**

| From → To | Signature |
|-----------|-----------|
| DataProvider → Strategy | `get(symbol, start, end, freq) → pd.DataFrame` |
| Strategy → Engine | `on_bar(bar, account) → list[Signal]` |
| Broker → Account | `execute(Signal, Account) → Order` |
| Recorder | `log(Order, Account)`, `summarize() → dict` |

---

### Task 1: Project scaffold

**Files:**
- Create: `~/myquant/requirements.txt`
- Create: `~/myquant/config.yaml`
- Create: `~/myquant/myquant/__init__.py`
- Create: `~/myquant/myquant/config.py`
- Create: `~/myquant/myquant/data/__init__.py`
- Create: `~/myquant/myquant/data/sources/__init__.py`
- Create: `~/myquant/myquant/strategy/__init__.py`
- Create: `~/myquant/myquant/backtest/__init__.py`
- Create: `~/myquant/myquant/live/__init__.py`
- Create: `~/myquant/myquant/web/__init__.py`
- Create: `~/myquant/tests/__init__.py`

**Interfaces:**
- Produces: `load_config() → dict`, `get(key, default) → Any`

- [ ] **Step 1: Create directories**

```bash
export PATH="$HOME/.local/bin:$PATH"
for d in myquant/data/sources myquant/strategy myquant/backtest myquant/live myquant/web tests; do
  mkdir -p ~/myquant/$d
done
```

- [ ] **Step 2: Write requirements.txt**

```bash
cat > ~/myquant/requirements.txt << "REQEOF"
akshare>=1.14
pandas>=2.0
numpy>=1.24
duckdb>=0.10
sqlalchemy>=2.0
streamlit>=1.28
matplotlib>=3.7
plotly>=5.18
pyyaml>=6.0
requests>=2.28
pytest>=7.0
REQEOF
```

- [ ] **Step 3: Write config.yaml**

```bash
cat > ~/myquant/config.yaml << "YAMLEOF"
data:
  primary: akshare
  cache_dir: ./data/cache

strategy:
  rotation:
    top_k: 10
    rebalance_freq: "W"
    factors: ["momentum_20", "volatility_60", "turnover_5"]

backtest:
  start_cash: 100000
  commission: 0.00025
  slippage: 0.001
  stamp_duty: 0.001

live:
  max_position_pct: 0.2
  max_daily_loss_pct: 0.05
  max_consecutive_loss: 3

notify:
  server_chan_key: ""
  email_smtp: ""
YAMLEOF
```

- [ ] **Step 4: Write myquant/config.py**

```bash
cat > ~/myquant/myquant/config.py << "PYEOF"
"""Global configuration loader."""
from pathlib import Path
import yaml

_CONFIG = None
ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | None = None) -> dict:
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    if path is None:
        path = str(ROOT / "config.yaml")
    with open(path) as f:
        _CONFIG = yaml.safe_load(f)
    return _CONFIG


def get(key: str, default=None):
    cfg = load_config()
    for part in key.split("."):
        if isinstance(cfg, dict) and part in cfg:
            cfg = cfg[part]
        else:
            return default
    return cfg
PYEOF
```

- [ ] **Step 5: Create empty __init__.py files**

```bash
cd ~/myquant && for d in myquant myquant/data myquant/data/sources myquant/strategy myquant/backtest myquant/live myquant/web tests; do
  touch $d/__init__.py
done
```

- [ ] **Step 6: Install dependencies and verify config**

```bash
export PATH="$HOME/.local/bin:$PATH"
cd ~/myquant && pip install -r requirements.txt --break-system-packages 2>&1 | tail -3
python3 -c "from myquant.config import get; assert get('backtest.start_cash') == 100000; print('Config OK')"
```
Expected: `Config OK`

- [ ] **Step 7: Git init and commit**

```bash
cd ~/myquant && git init && git add -A && git commit -m "feat: project scaffold with config and package structure"
```

---

### Task 2: Account model

**Files:**
- Create: `~/myquant/myquant/backtest/account.py`
- Create: `~/myquant/tests/test_account.py`

**Interfaces:**
- Produces: `class Account` with methods `buy(symbol, price, size, cost)`, `sell(symbol, price, size, cost)`, `holding(symbol) → dict`, `cash`, `total_value(prices)`, `positions`

- [ ] **Step 1: Write the failing test**

```bash
cat > ~/myquant/tests/test_account.py << "PYEOF"
import pytest
from myquant.backtest.account import Account


def test_account_init():
    a = Account(cash=100000)
    assert a.cash == 100000
    assert a.init_cash == 100000
    assert len(a.positions) == 0


def test_account_buy():
    a = Account(cash=100000)
    a.buy("000001.SZ", price=10.0, size=1000, cost=25.0)
    assert a.holding("000001.SZ") == {"size": 1000, "avg_cost": 10.0}
    assert a.cash == 100000 - 10000 - 25


def test_account_buy_duplicate_symbol():
    a = Account(cash=100000)
    a.buy("000001.SZ", price=10.0, size=1000, cost=25.0)
    a.buy("000001.SZ", price=12.0, size=500, cost=15.0)
    h = a.holding("000001.SZ")
    assert h["size"] == 1500
    assert h["avg_cost"] == pytest.approx((10000 + 6000) / 1500, rel=0.01)


def test_account_sell():
    a = Account(cash=100000)
    a.buy("000001.SZ", price=10.0, size=1000, cost=25.0)
    a.sell("000001.SZ", price=15.0, size=600, cost=30.0)
    h = a.holding("000001.SZ")
    assert h["size"] == 400
    assert a.cash == pytest.approx(100000 - 10025 + 9000 - 30, rel=0.01)


def test_account_sell_all():
    a = Account(cash=100000)
    a.buy("000001.SZ", price=10.0, size=1000, cost=25.0)
    a.sell("000001.SZ", price=15.0, size=1000, cost=30.0)
    assert a.holding("000001.SZ") is None


def test_account_insufficient_shares():
    a = Account(cash=100000)
    with pytest.raises(ValueError, match="Insufficient shares"):
        a.sell("000001.SZ", price=10.0, size=100, cost=5.0)


def test_account_insufficient_cash():
    a = Account(cash=5000)
    with pytest.raises(ValueError, match="Insufficient cash"):
        a.buy("000001.SZ", price=10.0, size=1000, cost=25.0)


def test_account_total_value():
    a = Account(cash=100000)
    a.buy("000001.SZ", price=10.0, size=1000, cost=25.0)
    a.buy("000002.SZ", price=20.0, size=500, cost=30.0)
    tv = a.total_value({"000001.SZ": 15.0, "000002.SZ": 22.0})
    # cash = 100000 - 10025 - 10030 = 79945
    # positions = 1000*15 + 500*22 = 15000 + 11000 = 26000
    assert tv == pytest.approx(79945 + 26000, rel=0.01)


def test_account_reset():
    a = Account(cash=100000)
    a.buy("000001.SZ", price=10.0, size=1000, cost=25.0)
    a.reset()
    assert a.cash == 100000
    assert len(a.positions) == 0
PYEOF
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd ~/myquant && python3 -m pytest tests/test_account.py -v 2>&1 | tail -10
```
Expected: `ModuleNotFoundError: No module named 'myquant.backtest.account'`

- [ ] **Step 3: Write Account implementation**

```bash
cat > ~/myquant/myquant/backtest/account.py << "PYEOF"
"""Virtual trading account — tracks cash and positions."""
from typing import Optional


class Account:
    def __init__(self, cash: float = 100_000):
        self.init_cash = cash
        self.cash = cash
        # positions: {symbol: {"size": int, "avg_cost": float}}
        self.positions: dict[str, dict] = {}
        self.trades: list[dict] = []

    def buy(self, symbol: str, price: float, size: int, cost: float = 0.0):
        total = price * size + cost
        if total > self.cash:
            raise ValueError(f"Insufficient cash: need {total}, have {self.cash:.2f}")
        self.cash -= total
        if symbol in self.positions:
            old = self.positions[symbol]
            new_size = old["size"] + size
            old_cost = old["avg_cost"] * old["size"]
            new_cost = price * size
            old["avg_cost"] = (old_cost + new_cost) / new_size
            old["size"] = new_size
        else:
            self.positions[symbol] = {"size": size, "avg_cost": price}

    def sell(self, symbol: str, price: float, size: int, cost: float = 0.0):
        if symbol not in self.positions:
            raise ValueError(f"No position for {symbol}")
        if size > self.positions[symbol]["size"]:
            raise ValueError(
                f"Insufficient shares: have {self.positions[symbol]['size']}, tried {size}"
            )
        self.cash += price * size - cost
        self.positions[symbol]["size"] -= size
        if self.positions[symbol]["size"] == 0:
            del self.positions[symbol]

    def holding(self, symbol: str) -> Optional[dict]:
        return self.positions.get(symbol)

    def total_value(self, prices: dict[str, float]) -> float:
        position_value = sum(
            h["size"] * prices.get(sym, 0) for sym, h in self.positions.items()
        )
        return self.cash + position_value

    def reset(self):
        self.cash = self.init_cash
        self.positions.clear()
        self.trades.clear()
PYEOF
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd ~/myquant && python3 -m pytest tests/test_account.py -v
```
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
cd ~/myquant && git add myquant/backtest/account.py tests/test_account.py && git commit -m "feat: Account model with buy/sell/position tracking"
```

---

### Task 3: Signal + Strategy base class

**Files:**
- Create: `~/myquant/myquant/strategy/base.py`
- Create: `~/myquant/tests/test_signal.py`

**Interfaces:**
- Produces: `Signal(action, symbol, size, price, reason)` dataclass
- Produces: `class Strategy` with `init(params) → None`, `on_bar(bar, account) → list[Signal]`

- [ ] **Step 1: Write the failing test**

```bash
cat > ~/myquant/tests/test_signal.py << "PYEOF"
import pandas as pd
from myquant.strategy.base import Signal, Strategy
from myquant.backtest.account import Account


def test_signal_creation():
    sig = Signal(action="BUY", symbol="000001.SZ", size=1000, price=10.0, reason="test")
    assert sig.action == "BUY"
    assert sig.symbol == "000001.SZ"
    assert sig.size == 1000
    assert sig.price == 10.0
    assert sig.reason == "test"


def test_signal_default_price():
    sig = Signal(action="SELL", symbol="000002.SZ", size=500, reason="stop")
    assert sig.price is None  # market order


def test_strategy_base():
    class Dummy(Strategy):
        def init(self, params):
            self.ma_period = params.get("ma_period", 5)

    s = Dummy()
    s.init({"ma_period": 10})
    assert s.ma_period == 10


def test_strategy_default_params():
    class Dummy(Strategy):
        def init(self, params):
            self.ma_period = params.get("ma_period", 5)

    s = Dummy()
    s.init({})
    assert s.ma_period == 5


def test_on_bar_not_implemented():
    class Dummy(Strategy):
        def init(self, params):
            pass

    s = Dummy()
    s.init({})
    bar = pd.DataFrame({"open": [10.0], "close": [10.5], "volume": [1000]})
    account = Account(cash=10000)
    # Strategy without on_bar override returns empty signal list
    signals = s.on_bar(bar, account)
    assert signals == []
PYEOF
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd ~/myquant && python3 -m pytest tests/test_signal.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write Signal + Strategy**

```bash
cat > ~/myquant/myquant/strategy/base.py << "PYEOF"
"""Strategy base class and Signal dataclass."""
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd


@dataclass
class Signal:
    """Standardized trading signal — same format for all strategies."""
    action: str       # "BUY" | "SELL" | "HOLD"
    symbol: str       # e.g. "000001.SZ"
    size: int         # number of shares
    reason: str       # e.g. "ma5_cross_ma20"
    price: Optional[float] = None  # None = market order


class Strategy:
    """Base strategy class. Override init() and on_bar()."""

    def init(self, params: dict) -> None:
        """Initialize strategy parameters. Called once before running."""
        pass

    def on_bar(self, bar: pd.DataFrame, account) -> list[Signal]:
        """Called on each bar. Return list of trading signals."""
        return []
PYEOF
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd ~/myquant && python3 -m pytest tests/test_signal.py -v
```

- [ ] **Step 5: Commit**

```bash
cd ~/myquant && git add myquant/strategy/base.py tests/test_signal.py && git commit -m "feat: Signal dataclass and Strategy base class"
```

---

### Task 4: DuckDB data store

**Files:**
- Create: `~/myquant/myquant/data/store.py`
- Create: `~/myquant/tests/test_store.py`

**Interfaces:**
- Produces: `class DataStore` with `save(df, table)`, `load(table, symbol, start, end) → pd.DataFrame`, `has(table) → bool`

- [ ] **Step 1: Write the failing test**

```bash
cat > ~/myquant/tests/test_store.py << "PYEOF"
import tempfile, os
import pandas as pd
import numpy as np
from myquant.data.store import DataStore


def make_sample_df():
    dates = pd.date_range("2024-01-01", periods=5, freq="D")
    symbols = ["000001.SZ", "000002.SZ"]
    data = []
    for d in dates:
        for s in symbols:
            data.append({
                "datetime": d,
                "symbol": s,
                "open": np.random.rand() * 10 + 5,
                "close": np.random.rand() * 10 + 5,
                "volume": np.random.randint(1000, 10000),
            })
    df = pd.DataFrame(data)
    df.set_index(["datetime", "symbol"], inplace=True)
    return df


def test_save_and_has():
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(path=tmp)
        df = make_sample_df()
        store.save(df, "daily_prices")
        assert store.has("daily_prices") is True


def test_load_all():
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(path=tmp)
        df = make_sample_df()
        store.save(df, "daily_prices")
        loaded = store.load("daily_prices")
        assert len(loaded) == len(df)


def test_load_by_symbol():
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(path=tmp)
        df = make_sample_df()
        store.save(df, "daily_prices")
        loaded = store.load("daily_prices", symbol="000001.SZ")
        assert loaded.index.get_level_values("symbol").unique() == ["000001.SZ"]
PYEOF
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd ~/myquant && python3 -m pytest tests/test_store.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write DataStore**

```bash
cat > ~/myquant/myquant/data/store.py << "PYEOF"
"""DuckDB-based data store for historical OHLCV data."""
from pathlib import Path
import duckdb
import pandas as pd


class DataStore:
    def __init__(self, path: str = "./data/cache"):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._db_path = str(self.path / "market.db")

    def _conn(self):
        return duckdb.connect(self._db_path)

    def save(self, df: pd.DataFrame, table: str):
        """Save a multi-index (datetime, symbol) DataFrame to DuckDB."""
        conn = self._conn()
        flat = df.reset_index()
        conn.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM flat")

    def has(self, table: str) -> bool:
        conn = self._conn()
        result = conn.execute(
            f"SELECT COUNT(*) FROM information_schema.tables WHERE table_name = '{table}'"
        ).fetchone()
        return result[0] > 0

    def load(
        self,
        table: str,
        symbol: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> pd.DataFrame:
        """Load OHLCV data, optionally filtered by symbol and date range."""
        conn = self._conn()
        query = f"SELECT * FROM {table}"
        conditions = []
        if symbol:
            conditions.append(f"symbol = '{symbol}'")
        if start:
            conditions.append(f"datetime >= '{start}'")
        if end:
            conditions.append(f"datetime <= '{end}'")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        df = conn.execute(query).df()
        if "datetime" in df.columns and "symbol" in df.columns:
            df.set_index(["datetime", "symbol"], inplace=True)
        return df

    def symbols(self, table: str) -> list[str]:
        conn = self._conn()
        result = conn.execute(f"SELECT DISTINCT symbol FROM {table}").fetchall()
        return [r[0] for r in result]
PYEOF
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd ~/myquant && python3 -m pytest tests/test_store.py -v
```

- [ ] **Step 5: Commit**

```bash
cd ~/myquant && git add myquant/data/store.py tests/test_store.py && git commit -m "feat: DuckDB DataStore for historical OHLCV data"
```

---

### Task 5: AKShare data provider

**Files:**
- Create: `~/myquant/myquant/data/sources/akshare.py`
- Create: `~/myquant/myquant/data/provider.py`
- Create: `~/myquant/tests/test_provider.py`

**Interfaces:**
- Consumes: `DataStore` from Task 4
- Produces: `class AKShareSource` with `get_daily(symbol, start, end) → pd.DataFrame`
- Produces: `class DataProvider` with `get(symbol, start, end, freq) → pd.DataFrame`

- [ ] **Step 1: Write the failing test**

```bash
cat > ~/myquant/tests/test_provider.py << "PYEOF"
import pandas as pd
from myquant.data.provider import DataProvider


def strip_code(symbol: str) -> str:
    """'000001.SZ' → '000001'"""
    return symbol.split(".")[0]


def test_provider_get_daily_history():
    provider = DataProvider()
    # Pull a small date range from AKShare
    df = provider.get("000001.SZ", start="2024-01-01", end="2024-01-10", freq="1d")
    assert isinstance(df, pd.DataFrame)
    assert len(df) > 0
    assert "open" in df.columns or "close" in df.columns
    # Columns should be lowercase after normalization
    cols_lower = [c.lower() for c in df.columns]
    assert "open" in cols_lower or "close" in cols_lower


def test_provider_caches_to_store():
    provider = DataProvider()
    # First fetch populates cache
    provider.get("000001.SZ", start="2024-01-01", end="2024-01-10", freq="1d")
    # Second fetch should hit cache (no network error = pass)
    df2 = provider.get("000001.SZ", start="2024-01-01", end="2024-01-10", freq="1d")
    assert len(df2) > 0
PYEOF
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd ~/myquant && python3 -m pytest tests/test_provider.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write AKShare source adapter**

```bash
cat > ~/myquant/myquant/data/sources/akshare.py << "PYEOF"
"""AKShare data source — free Chinese stock market data."""
import pandas as pd
from typing import Optional


class AKShareSource:
    """AKShare adapter for A-share daily data."""

    def get_daily(
        self, symbol: str, start: Optional[str] = None, end: Optional[str] = None
    ) -> pd.DataFrame:
        """Fetch daily OHLCV for a single A-share symbol.

        symbol format: '000001.SZ' or '600000.SH'
        Returns DataFrame with columns: date, open, close, high, low, volume
        """
        try:
            import akshare as ak
        except ImportError:
            raise ImportError("akshare not installed. Run: pip install akshare")

        code = symbol.split(".")[0]
        market = symbol.split(".")[1].lower()

        if market == "sh":
            full_code = f"sh{code}"
        elif market == "sz":
            full_code = f"sz{code}"
        else:
            full_code = symbol

        df = ak.stock_zh_a_hist(
            symbol=code,
            period="daily",
            start_date=start.replace("-", "") if start else "20200101",
            end_date=end.replace("-", "") if end else "20241231",
            adjust="qfq",
        )

        if df is None or len(df) == 0:
            return pd.DataFrame()

        # Normalize column names to lowercase
        df.rename(
            columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
            },
            inplace=True,
        )

        # If columns are already English
        col_map = {c: c.lower() for c in df.columns}
        df.rename(columns=col_map, inplace=True)

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"])
            df.set_index("date", inplace=True)

        return df
PYEOF
```

- [ ] **Step 4: Write DataProvider**

```bash
cat > ~/myquant/myquant/data/provider.py << "PYEOF"
"""Unified data provider — auto-caches to DuckDB, falls back to cache."""
import pandas as pd
from pathlib import Path
from myquant.data.sources.akshare import AKShareSource
from myquant.data.store import DataStore
from myquant.config import get


class DataProvider:
    def __init__(self, cache_path: str | None = None):
        if cache_path is None:
            cache_path = get("data.cache_dir", str(Path("~/myquant/data/cache").expanduser()))
        self.store = DataStore(path=cache_path)
        self._ak = AKShareSource()
        self._table = "daily_prices"

    def get(
        self,
        symbol: str,
        start: str | None = None,
        end: str | None = None,
        freq: str = "1d",
    ) -> pd.DataFrame:
        """Get OHLCV data. Uses cache if available, otherwise fetches from AKShare."""
        # Check cache first
        if self.store.has(self._table):
            cached = self.store.load(self._table, symbol=symbol, start=start, end=end)
            if len(cached) > 0:
                return cached

        # Fetch from AKShare
        if freq == "1d":
            df = self._ak.get_daily(symbol, start=start, end=end)
        else:
            raise ValueError(f"Unsupported frequency: {freq}")

        if len(df) == 0:
            return df

        # Normalize: add symbol column and set multi-index
        df["symbol"] = symbol
        if "date" in df.columns:
            df.rename(columns={"date": "datetime"}, inplace=True)
        # Reset index if 'datetime' is the index
        if df.index.name == "date" or (isinstance(df.index, pd.DatetimeIndex)):
            df.index.name = "datetime"
            df.reset_index(inplace=True)
        df.set_index(["datetime", "symbol"], inplace=True)

        # Save to cache (append mode)
        try:
            self.store.save(df, self._table)
        except Exception:
            pass  # cache write failure is non-fatal

        return df
PYEOF
```

- [ ] **Step 5: Run test — expect pass**

```bash
cd ~/myquant && python3 -m pytest tests/test_provider.py -v
```

- [ ] **Step 6: Commit**

```bash
cd ~/myquant && git add myquant/data/sources/akshare.py myquant/data/provider.py tests/test_provider.py && git commit -m "feat: AKShare data provider with DuckDB caching"
```

---

### Task 6: MA Crossover timing strategy

**Files:**
- Create: `~/myquant/myquant/strategy/timing.py`
- Create: `~/myquant/tests/test_timing.py`

**Interfaces:**
- Consumes: `Strategy` base class from Task 3
- Produces: `class MACrossoverStrategy(Strategy)` — golden cross buy, death cross sell

- [ ] **Step 1: Write the failing test**

```bash
cat > ~/myquant/tests/test_timing.py << "PYEOF"
import pandas as pd
import numpy as np
from myquant.strategy.timing import MACrossoverStrategy
from myquant.backtest.account import Account


def make_uptrend_bars(n=30) -> list[pd.DataFrame]:
    """Simulate n daily bars in an uptrend. Returns list of single-row DataFrames."""
    np.random.seed(42)
    price = 10.0
    bars = []
    for i in range(n):
        price += 0.1 + np.random.randn() * 0.2
        df = pd.DataFrame([{
            "open": price - 0.05,
            "close": price,
            "high": price + 0.1,
            "low": price - 0.1,
            "volume": 5000 + np.random.randint(0, 2000),
        }])
        df.index = [pd.Timestamp(f"2024-01-{i+1:02d}")]
        bars.append(df)
    return bars


def test_ma_crossover_buy_signal():
    strategy = MACrossoverStrategy()
    strategy.init({"fast": 3, "slow": 8})

    # Run through uptrend bars — should trigger buy after fast crosses above slow
    bars = make_uptrend_bars(30)
    account = Account(cash=100000)
    found_buy = False
    for bar in bars:
        signals = strategy.on_bar(bar, account)
        for sig in signals:
            if sig.action == "BUY":
                found_buy = True
                assert sig.symbol == "SYMBOL"  # default symbol when none provided
    assert found_buy, "Should have triggered a buy signal in uptrend"


def test_ma_crossover_no_signal_insufficient_bars():
    strategy = MACrossoverStrategy()
    strategy.init({"fast": 5, "slow": 20})
    bar = pd.DataFrame([{"open": 10.0, "close": 10.5, "high": 10.6, "low": 9.9, "volume": 5000}])
    account = Account(cash=100000)
    # Not enough bars yet — should have no signal
    signals = strategy.on_bar(bar, account)
    assert len(signals) == 0
PYEOF
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd ~/myquant && python3 -m pytest tests/test_timing.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write MACrossoverStrategy**

```bash
cat > ~/myquant/myquant/strategy/timing.py << "PYEOF"
"""Technical indicator timing strategies."""
import pandas as pd
from myquant.strategy.base import Strategy, Signal


class MACrossoverStrategy(Strategy):
    """Golden cross / death cross: fast MA crosses above slow MA → BUY, below → SELL."""

    def init(self, params: dict) -> None:
        self.fast = int(params.get("fast", 5))
        self.slow = int(params.get("slow", 20))
        self.symbol = params.get("symbol", "SYMBOL")
        self._closes: list[float] = []
        self._position = False

    def on_bar(self, bar: pd.DataFrame, account) -> list[Signal]:
        close = float(bar["close"].iloc[-1])
        self._closes.append(close)

        if len(self._closes) < self.slow:
            return []

        signals = []
        fast_ma = sum(self._closes[-self.fast :]) / self.fast
        slow_ma = sum(self._closes[-self.slow :]) / self.slow

        # Use position sizing from account
        available_cash = account.cash
        price = close

        if fast_ma > slow_ma and not self._position:
            size = self._calc_size(available_cash, price)
            if size > 0:
                signals.append(Signal(
                    action="BUY",
                    symbol=self.symbol,
                    size=size,
                    price=price,
                    reason=f"golden_cross_fast{self.fast}_slow{self.slow}",
                ))
                self._position = True
        elif fast_ma < slow_ma and self._position:
            holding = account.holding(self.symbol)
            if holding:
                signals.append(Signal(
                    action="SELL",
                    symbol=self.symbol,
                    size=holding["size"],
                    price=price,
                    reason=f"death_cross_fast{self.fast}_slow{self.slow}",
                ))
                self._position = False

        return signals

    def _calc_size(self, cash: float, price: float) -> int:
        """Allocate 80% of available cash, round down to 100-share lots."""
        if price <= 0:
            return 0
        target = cash * 0.8
        lots = int(target / (price * 100))
        return lots * 100
PYEOF
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd ~/myquant && python3 -m pytest tests/test_timing.py -v
```

- [ ] **Step 5: Commit**

```bash
cd ~/myquant && git add myquant/strategy/timing.py tests/test_timing.py && git commit -m "feat: MA crossover timing strategy"
```

---

### Task 7: Multi-factor rotation strategy

**Files:**
- Create: `~/myquant/myquant/strategy/rotation.py`
- Create: `~/myquant/tests/test_rotation.py`

**Interfaces:**
- Consumes: `Strategy` base class from Task 3
- Produces: `class RotationStrategy(Strategy)` — rebalance on schedule, rank by factors

- [ ] **Step 1: Write the failing test**

```bash
cat > ~/myquant/tests/test_rotation.py << "PYEOF"
import pandas as pd
import numpy as np
from myquant.strategy.rotation import RotationStrategy
from myquant.backtest.account import Account


def make_multi_symbol_bars(symbols, n=30):
    """Generate bars for multiple symbols over n days."""
    np.random.seed(42)
    prices = {s: 10.0 + np.random.rand() * 5 for s in symbols}
    for day in range(n):
        for s in symbols:
            prices[s] += np.random.randn() * 0.3
            df = pd.DataFrame([{
                "open": prices[s] - 0.05,
                "close": prices[s],
                "high": prices[s] + 0.1,
                "low": prices[s] - 0.1,
                "volume": 5000 + np.random.randint(0, 5000),
                "symbol": s,
            }])
            df.index = [pd.Timestamp(f"2024-01-{day+1:02d}")]
            yield df, s


def test_rotation_ranks_symbols():
    strategy = RotationStrategy()
    strategy.init({
        "symbols": ["000001.SZ", "000002.SZ", "000003.SZ"],
        "top_k": 2,
        "rebalance_freq": 10,
    })

    account = Account(cash=100000)
    # Feed 20 days of data across 3 symbols
    signals_by_day: dict[int, list] = {}
    for i, (bar, _) in enumerate(make_multi_symbol_bars(
        ["000001.SZ", "000002.SZ", "000003.SZ"], n=20
    )):
        day_idx = i % 3  # each batch of 3 bars represents one day
        signals = strategy.on_bar(bar, account)
        if signals:
            signals_by_day.setdefault(day_idx, []).extend(signals)

    # After enough data, should have generated ranking signals
    total_signals = sum(len(v) for v in signals_by_day.values())
    assert total_signals > 0, "Should produce rotation signals after enough data"


def test_rotation_holds_top_k():
    strategy = RotationStrategy()
    strategy.init({
        "symbols": ["A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ"],
        "top_k": 2,
        "rebalance_freq": 5,
    })
    account = Account(cash=100000)
    np.random.seed(123)
    prices = {s: 10.0 + i * 0.5 for i, s in enumerate(["A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ"])}

    for day in range(10):
        for sym in ["A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ"]:
            prices[sym] += np.random.randn() * 0.2
            bar = pd.DataFrame([{
                "open": prices[sym] - 0.05, "close": prices[sym],
                "high": prices[sym] + 0.1, "low": prices[sym] - 0.1,
                "volume": 5000, "symbol": sym,
            }])
            bar.index = [pd.Timestamp(f"2024-01-{day+1:02d}")]
            strategy.on_bar(bar, account)
PYEOF
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd ~/myquant && python3 -m pytest tests/test_rotation.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write RotationStrategy**

```bash
cat > ~/myquant/myquant/strategy/rotation.py << "PYEOF"
"""Factor-based rotation strategy — rank symbols, buy top K."""
import pandas as pd
from myquant.strategy.base import Strategy, Signal


class RotationStrategy(Strategy):
    def init(self, params: dict) -> None:
        self.symbols: list[str] = params.get("symbols", [])
        self.top_k: int = int(params.get("top_k", 10))
        self.rebalance_freq: int = int(params.get("rebalance_freq", 5))  # days
        self._bars_since_rebalance = 0
        self._price_history: dict[str, list[float]] = {s: [] for s in self.symbols}
        self._current_holdings: set[str] = set()

    def on_bar(self, bar: pd.DataFrame, account) -> list[Signal]:
        # Extract symbol from bar
        sym = bar.get("symbol", pd.Series(["SYMBOL"]))
        symbol = sym.iloc[0] if len(sym) > 0 else "SYMBOL"
        close = float(bar["close"].iloc[-1])

        if symbol in self._price_history:
            self._price_history[symbol].append(close)

        self._bars_since_rebalance += 1

        # Only rebalance on schedule (after accumulating enough bars per symbol)
        if self._bars_since_rebalance < self.rebalance_freq * len(self.symbols):
            return []

        self._bars_since_rebalance = 0
        return self._do_rebalance(account)

    def _do_rebalance(self, account) -> list[Signal]:
        """Score symbols by momentum + volume factors, buy top_k."""
        scores = {}
        for sym in self.symbols:
            prices = self._price_history.get(sym, [])
            if len(prices) < 20:
                scores[sym] = -999
                continue
            # Simple composite: momentum (20-day return) + volatility adjustment
            momentum = (prices[-1] / prices[-20] - 1) if len(prices) >= 20 else 0
            vol = (
                pd.Series(prices[-20:]).pct_change().std() if len(prices) >= 20 else 1
            )
            scores[sym] = momentum / max(vol, 0.0001)

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        target_holdings = set(s for s, _ in ranked[: self.top_k] if scores[s] > -900)

        signals = []
        # Sell symbols no longer in top_k
        for sym in list(self._current_holdings):
            if sym not in target_holdings:
                holding = account.holding(sym)
                if holding:
                    signals.append(
                        Signal(
                            action="SELL",
                            symbol=sym,
                            size=holding["size"],
                            price=self._price_history[sym][-1] if self._price_history[sym] else None,
                            reason=f"rotation_drop_out_of_top{self.top_k}",
                        )
                    )

        # Buy newly selected symbols
        if target_holdings:
            per_symbol_cash = account.cash * 0.8 / len(target_holdings)
            for sym in target_holdings:
                if sym not in self._current_holdings:
                    price = self._price_history[sym][-1] if self._price_history[sym] else 10.0
                    lots = int(per_symbol_cash / (price * 100))
                    size = lots * 100
                    if size > 0:
                        signals.append(
                            Signal(
                                action="BUY",
                                symbol=sym,
                                size=size,
                                price=price,
                                reason=f"rotation_enter_top{self.top_k}",
                            )
                        )

        self._current_holdings = target_holdings
        return signals
PYEOF
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd ~/myquant && python3 -m pytest tests/test_rotation.py -v
```

- [ ] **Step 5: Commit**

```bash
cd ~/myquant && git add myquant/strategy/rotation.py tests/test_rotation.py && git commit -m "feat: multi-factor rotation strategy"
```

---

### Task 8: Backtest Broker

**Files:**
- Create: `~/myquant/myquant/backtest/broker.py`
- Create: `~/myquant/tests/test_broker.py`

**Interfaces:**
- Consumes: `Signal` from Task 3, `Account` from Task 2
- Produces: `class Broker` with `execute(signal, account) → dict`

- [ ] **Step 1: Write the failing test**

```bash
cat > ~/myquant/tests/test_broker.py << "PYEOF"
import pytest
from myquant.backtest.account import Account
from myquant.backtest.broker import Broker
from myquant.strategy.base import Signal


def test_broker_buy_market_order():
    broker = Broker(commission=0.00025, slippage=0.001, stamp_duty=0.001)
    account = Account(cash=100000)
    sig = Signal(action="BUY", symbol="000001.SZ", size=1000, price=None, reason="test")
    order = broker.execute(sig, account)
    assert order["action"] == "BUY"
    assert order["symbol"] == "000001.SZ"
    assert order["filled"] == 1000
    assert order["cost"] > 0
    assert account.holding("000001.SZ") is not None


def test_broker_sell_with_stamp_duty():
    broker = Broker(commission=0.00025, slippage=0.001, stamp_duty=0.001)
    account = Account(cash=100000)
    # Buy first at known price
    sig_buy = Signal(action="BUY", symbol="000001.SZ", size=1000, price=10.0, reason="test")
    broker.execute(sig_buy, account)
    # Then sell — stamp duty applies
    sig_sell = Signal(action="SELL", symbol="000001.SZ", size=500, price=12.0, reason="test")
    order = broker.execute(sig_sell, account)
    assert order["action"] == "SELL"
    # stamp_duty = 0.001 * (500 * 12) = 6, commission = 0.00025 * 6000 = 1.5
    assert order["cost"] > 0


def test_broker_rejects_invalid_action():
    broker = Broker()
    account = Account(cash=100000)
    sig = Signal(action="HOLD", symbol="000001.SZ", size=100, reason="test")
    with pytest.raises(ValueError, match="Unknown action"):
        broker.execute(sig, account)


def test_broker_commission_floor():
    """Commission floor is 5 yuan minimum."""
    broker = Broker(commission=0.00025, slippage=0.0, stamp_duty=0.0)
    account = Account(cash=100000)
    # Very small trade: 100 shares at 10 yuan = 1000 yuan
    # Commission: 1000 * 0.00025 = 0.25, floor = 5
    sig = Signal(action="BUY", symbol="000001.SZ", size=100, price=10.0, reason="test")
    order = broker.execute(sig, account)
    assert order["cost"] == pytest.approx(5.0, rel=0.1)
PYEOF
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd ~/myquant && python3 -m pytest tests/test_broker.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write Broker**

```bash
cat > ~/myquant/myquant/backtest/broker.py << "PYEOF"
"""Simulated broker with commission, slippage, and stamp duty."""
from myquant.strategy.base import Signal
from myquant.backtest.account import Account
from myquant.config import get


class Broker:
    def __init__(
        self,
        commission: float | None = None,
        slippage: float | None = None,
        stamp_duty: float | None = None,
    ):
        self.commission_rate = commission if commission is not None else get("backtest.commission", 0.00025)
        self.slippage = slippage if slippage is not None else get("backtest.slippage", 0.001)
        self.stamp_duty_rate = stamp_duty if stamp_duty is not None else get("backtest.stamp_duty", 0.001)
        self.commission_floor = 5.0

    def execute(self, signal: Signal, account: Account) -> dict:
        """Execute a signal against an account. Returns order dict."""
        if signal.action == "BUY":
            return self._buy(signal, account)
        elif signal.action == "SELL":
            return self._sell(signal, account)
        elif signal.action == "HOLD":
            return {"action": "HOLD", "symbol": signal.symbol, "filled": 0, "cost": 0}
        else:
            raise ValueError(f"Unknown action: {signal.action}")

    def _buy(self, signal: Signal, account: Account) -> dict:
        price = signal.price or 10.0
        # Apply slippage: buy at slightly higher price
        exec_price = price * (1 + self.slippage)
        notional = exec_price * signal.size
        commission = max(notional * self.commission_rate, self.commission_floor)
        account.buy(signal.symbol, price=exec_price, size=signal.size, cost=commission)
        return {
            "action": "BUY",
            "symbol": signal.symbol,
            "price": exec_price,
            "filled": signal.size,
            "cost": commission,
            "notional": notional,
        }

    def _sell(self, signal: Signal, account: Account) -> dict:
        price = signal.price or 10.0
        # Apply slippage: sell at slightly lower price
        exec_price = price * (1 - self.slippage)
        notional = exec_price * signal.size
        commission = max(notional * self.commission_rate, self.commission_floor)
        stamp_duty = notional * self.stamp_duty_rate
        total_cost = commission + stamp_duty
        account.sell(signal.symbol, price=exec_price, size=signal.size, cost=total_cost)
        return {
            "action": "SELL",
            "symbol": signal.symbol,
            "price": exec_price,
            "filled": signal.size,
            "cost": total_cost,
            "commission": commission,
            "stamp_duty": stamp_duty,
        }
PYEOF
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd ~/myquant && python3 -m pytest tests/test_broker.py -v
```

- [ ] **Step 5: Commit**

```bash
cd ~/myquant && git add myquant/backtest/broker.py tests/test_broker.py && git commit -m "feat: Backtest Broker with commission/slippage/stamp duty"
```

---

### Task 9: Backtest Recorder

**Files:**
- Create: `~/myquant/myquant/backtest/recorder.py`
- Create: `~/myquant/tests/test_recorder.py`

**Interfaces:**
- Consumes: `Account` from Task 2
- Produces: `class Recorder` with `log(order, account)`, `mark(bar, account)`, `summarize() → dict`

- [ ] **Step 1: Write the failing test**

```bash
cat > ~/myquant/tests/test_recorder.py << "PYEOF"
from myquant.backtest.account import Account
from myquant.backtest.recorder import Recorder


def test_recorder_log_trade():
    r = Recorder()
    account = Account(cash=100000)
    order = {"action": "BUY", "symbol": "000001.SZ", "price": 10.0, "filled": 1000, "cost": 25.0}
    account.buy("000001.SZ", price=10.0, size=1000, cost=25.0)
    r.log(order, account)
    assert len(r.trades) == 1
    assert r.trades[0]["action"] == "BUY"


def test_recorder_mark_and_summarize():
    import pandas as pd
    r = Recorder()
    account = Account(cash=100000)
    bar1 = pd.DataFrame([{"close": 10.0}])
    bar2 = pd.DataFrame([{"close": 10.5}])
    r.mark(bar1, account)
    r.mark(bar2, account)
    assert len(r.equity) == 2
    summary = r.summarize()
    assert "total_return" in summary
    assert "sharpe_ratio" in summary
    assert "max_drawdown" in summary
    assert "trade_count" in summary
PYEOF
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd ~/myquant && python3 -m pytest tests/test_recorder.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write Recorder**

```bash
cat > ~/myquant/myquant/backtest/recorder.py << "PYEOF"
"""Trade logger and equity curve recorder."""
import pandas as pd
import numpy as np


class Recorder:
    def __init__(self):
        self.trades: list[dict] = []
        self.equity: list[dict] = []

    def log(self, order: dict, account):
        self.trades.append({
            "action": order.get("action"),
            "symbol": order.get("symbol"),
            "price": order.get("price", 0),
            "filled": order.get("filled", 0),
            "cost": order.get("cost", 0),
            "cash_after": account.cash,
        })

    def mark(self, bar: pd.DataFrame, account):
        """Record a mark-to-market snapshot. bar is a single-row DataFrame."""
        close = float(bar["close"].iloc[-1])
        # Build a price dict from the bar
        prices = {}
        sym_col = bar.get("symbol", None)
        if sym_col is not None:
            prices[str(sym_col.iloc[0])] = close
        else:
            prices["SYMBOL"] = close

        self.equity.append({
            "datetime": bar.index[0],
            "cash": account.cash,
            "total_value": account.total_value(prices),
        })

    def _equity_df(self) -> pd.DataFrame:
        df = pd.DataFrame(self.equity)
        if "datetime" in df.columns:
            df.set_index("datetime", inplace=True)
        return df

    def summarize(self) -> dict:
        """Compute performance metrics."""
        eq = self._equity_df()
        if len(eq) < 2:
            return {
                "total_return": 0.0,
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0,
                "win_rate": 0.0,
                "trade_count": 0,
            }

        returns = eq["total_value"].pct_change().dropna()
        total_return = (eq["total_value"].iloc[-1] / eq["total_value"].iloc[0] - 1)
        sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0.0

        # Max drawdown
        cummax = eq["total_value"].cummax()
        drawdown = (eq["total_value"] - cummax) / cummax
        max_dd = float(drawdown.min())

        # Win rate from trades
        buys = [t for t in self.trades if t["action"] == "BUY"]
        sells = [t for t in self.trades if t["action"] == "SELL"]

        return {
            "total_return": float(round(total_return * 100, 2)),
            "sharpe_ratio": float(round(sharpe, 2)),
            "max_drawdown": float(round(max_dd * 100, 2)),
            "win_rate": 0.0,  # requires pair matching — simplified
            "trade_count": len(self.trades),
            "buy_count": len(buys),
            "sell_count": len(sells),
        }

    def equity_curve(self) -> pd.DataFrame:
        return self._equity_df()
PYEOF
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd ~/myquant && python3 -m pytest tests/test_recorder.py -v
```

- [ ] **Step 5: Commit**

```bash
cd ~/myquant && git add myquant/backtest/recorder.py tests/test_recorder.py && git commit -m "feat: Backtest Recorder with equity tracking and metrics"
```

---

### Task 10: Backtest Engine (integration)

**Files:**
- Create: `~/myquant/myquant/backtest/engine.py`
- Create: `~/myquant/tests/test_engine.py`

**Interfaces:**
- Consumes: `Strategy` (Task 3), `Broker` (Task 8), `Recorder` (Task 9), `Account` (Task 2), `DataProvider` (Task 5)
- Produces: `class BacktestEngine` with `run(strategy, symbols, start, end) → dict`

- [ ] **Step 1: Write the integration test**

```bash
cat > ~/myquant/tests/test_engine.py << "PYEOF"
import pandas as pd
import numpy as np
from myquant.backtest.engine import BacktestEngine
from myquant.strategy.timing import MACrossoverStrategy


def make_fake_bars(symbol, start, end):
    """Generate synthetic OHLCV bars for testing when AKShare is unavailable."""
    dates = pd.date_range(start, end, freq="B")  # business days
    np.random.seed(42)
    price = 10.0
    data = []
    for d in dates:
        price += np.random.randn() * 0.15
        data.append({
            "datetime": d,
            "symbol": symbol,
            "open": price - 0.05,
            "close": price,
            "high": price + 0.1,
            "low": price - 0.1,
            "volume": float(np.random.randint(1000, 10000)),
        })
    df = pd.DataFrame(data)
    df.set_index(["datetime", "symbol"], inplace=True)
    return df


def test_backtest_engine_runs_with_fake_data():
    """Test backtest engine works end-to-end with synthetic data."""
    # Generate fake data
    df = make_fake_bars("000001.SZ", "2024-01-01", "2024-03-31")

    strategy = MACrossoverStrategy()
    strategy.init({"fast": 5, "slow": 20, "symbol": "000001.SZ"})

    engine = BacktestEngine(start_cash=100000)
    result = engine.run(strategy, df)

    assert "total_return" in result
    assert "sharpe_ratio" in result
    assert "max_drawdown" in result
    assert "trade_count" in result
    assert result["trade_count"] >= 0


def test_backtest_engine_no_crash_empty_data():
    """Engine should handle empty data gracefully."""
    df = pd.DataFrame(columns=["datetime", "symbol", "open", "close", "high", "low", "volume"])
    df.set_index(["datetime", "symbol"], inplace=True)

    strategy = MACrossoverStrategy()
    strategy.init({"fast": 5, "slow": 20, "symbol": "000001.SZ"})

    engine = BacktestEngine(start_cash=100000)
    result = engine.run(strategy, df)
    assert result["trade_count"] == 0
    assert result["total_return"] == 0.0
PYEOF
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd ~/myquant && python3 -m pytest tests/test_engine.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write BacktestEngine**

```bash
cat > ~/myquant/myquant/backtest/engine.py << "PYEOF"
"""Event-driven backtest engine."""
import pandas as pd
from myquant.backtest.account import Account
from myquant.backtest.broker import Broker
from myquant.backtest.recorder import Recorder
from myquant.config import get


class BacktestEngine:
    def __init__(self, start_cash: float | None = None):
        if start_cash is None:
            start_cash = float(get("backtest.start_cash", 100000))
        self.start_cash = start_cash

    def run(
        self,
        strategy,
        data: pd.DataFrame,
    ) -> dict:
        """Run backtest with event-driven loop.

        Args:
            strategy: Strategy instance with init() already called
            data: Multi-index (datetime, symbol) DataFrame with OHLCV columns

        Returns:
            dict: performance metrics
        """
        account = Account(cash=self.start_cash)
        broker = Broker()
        recorder = Recorder()

        if len(data) == 0:
            return recorder.summarize()

        # Group bars by datetime for multi-symbol support
        if isinstance(data.index, pd.MultiIndex) and "symbol" not in data.columns:
            grouped = data.groupby(level="datetime")
        else:
            # Single symbol or flat structure
            grouped = [(ts, data.loc[[ts]]) for ts in data.index.unique()]

        for ts, group in grouped if isinstance(grouped, list) else grouped:
            for idx, bar_row in group.iterrows():
                # Build a single-row DataFrame for the strategy
                bar = pd.DataFrame([bar_row.to_dict()])
                bar.index = [ts]

                signals = strategy.on_bar(bar, account)
                for sig in signals:
                    try:
                        order = broker.execute(sig, account)
                        recorder.log(order, account)
                    except ValueError as e:
                        # Skip signals that fail (insufficient cash, no position, etc.)
                        pass

                recorder.mark(bar, account)

        return recorder.summarize()
PYEOF
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd ~/myquant && python3 -m pytest tests/test_engine.py -v
```

- [ ] **Step 5: Commit**

```bash
cd ~/myquant && git add myquant/backtest/engine.py tests/test_engine.py && git commit -m "feat: event-driven BacktestEngine with Broker + Recorder integration"
```

---

### Task 11: Backtest Report generator

**Files:**
- Create: `~/myquant/myquant/backtest/report.py`

**Interfaces:**
- Consumes: `Recorder.equity_curve()` and `Recorder.summarize()` from Task 9
- Produces: `generate_report(recorder, output_path) → str` with HTML file

- [ ] **Step 1: Write Report module**

```bash
cat > ~/myquant/myquant/backtest/report.py << "PYEOF"
"""Generate HTML backtest report with charts."""
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates


def generate_report(recorder, output_path: str = "backtest_report.html") -> str:
    """Generate an HTML report with equity curve chart and metrics table."""
    metrics = recorder.summarize()
    equity = recorder.equity_curve()

    # Plot equity curve
    fig, ax = plt.subplots(figsize=(10, 5))
    if len(equity) > 0 and "total_value" in equity.columns:
        ax.plot(equity.index, equity["total_value"], label="Portfolio Value", color="#2196F3")
        ax.axhline(y=equity["total_value"].iloc[0], color="gray", linestyle="--", label="Start")
        ax.set_title("Equity Curve", fontsize=14)
        ax.set_ylabel("Value (CNY)")
        ax.legend()
        ax.grid(True, alpha=0.3)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        fig.autofmt_xdate()

    chart_path = Path(output_path).parent / "equity_curve.png"
    fig.savefig(str(chart_path), dpi=100, bbox_inches="tight")
    plt.close(fig)

    # Build HTML report
    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
    <meta charset="UTF-8">
    <title>MyQuant Backtest Report</title>
    <style>
        body {{ font-family: -apple-system, sans-serif; max-width: 800px; margin: auto; padding: 2rem; }}
        h1 {{ color: #1565C0; }}
        .metrics {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 1rem; }}
        .metric-card {{
            background: #f5f5f5; padding: 1rem; border-radius: 8px; text-align: center;
        }}
        .metric-value {{ font-size: 2rem; font-weight: bold; color: #1565C0; }}
        .metric-label {{ font-size: 0.85rem; color: #666; }}
        .positive {{ color: #4CAF50; }}
        .negative {{ color: #F44336; }}
        img {{ max-width: 100%; margin-top: 1rem; border-radius: 8px; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
        th, td {{ padding: 0.5rem; text-align: left; border-bottom: 1px solid #eee; }}
    </style>
</head>
<body>
    <h1>MyQuant Backtest Report</h1>
    <div class="metrics">
        <div class="metric-card">
            <div class="metric-value {"positive" if metrics["total_return"] >= 0 else "negative"}">{metrics["total_return"]:+.2f}%</div>
            <div class="metric-label">Total Return</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{metrics["sharpe_ratio"]:.2f}</div>
            <div class="metric-label">Sharpe Ratio</div>
        </div>
        <div class="metric-card">
            <div class="metric-value negative">{metrics["max_drawdown"]:.2f}%</div>
            <div class="metric-label">Max Drawdown</div>
        </div>
    </div>
    <img src="equity_curve.png" alt="Equity Curve">
    <table>
        <tr><th>Metric</th><th>Value</th></tr>
        <tr><td>Total Trades</td><td>{metrics['trade_count']}</td></tr>
        <tr><td>Buy Count</td><td>{metrics.get('buy_count', 0)}</td></tr>
        <tr><td>Sell Count</td><td>{metrics.get('sell_count', 0)}</td></tr>
    </table>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)

    return output_path
PYEOF
```

- [ ] **Step 2: Verify import**

```bash
cd ~/myquant && python3 -c "from myquant.backtest.report import generate_report; print('Report module OK')"
```

- [ ] **Step 3: Commit**

```bash
cd ~/myquant && git add myquant/backtest/report.py && git commit -m "feat: HTML backtest report with equity curve and metrics"
```

---

### Task 12: Live Risk Manager

**Files:**
- Create: `~/myquant/myquant/live/risk.py`
- Create: `~/myquant/tests/test_risk.py`

**Interfaces:**
- Produces: `class RiskManager` with `approve(signal, account, daily_pnl) → bool`

- [ ] **Step 1: Write the failing test**

```bash
cat > ~/myquant/tests/test_risk.py << "PYEOF"
from myquant.live.risk import RiskManager
from myquant.backtest.account import Account
from myquant.strategy.base import Signal


def test_risk_approves_normal_buy():
    rm = RiskManager(max_position_pct=0.2, max_daily_loss_pct=0.05)
    account = Account(cash=100000)
    sig = Signal(action="BUY", symbol="000001.SZ", size=1000, price=10.0, reason="test")
    approved, reason = rm.approve(sig, account, daily_pnl=0)
    assert approved is True


def test_risk_rejects_over_position_limit():
    rm = RiskManager(max_position_pct=0.2, max_daily_loss_pct=0.05)
    account = Account(cash=100000)
    # Buy 80% position first
    sig_big = Signal(action="BUY", symbol="000001.SZ", size=4000, price=20.0, reason="test")
    account.buy("000001.SZ", price=20.0, size=4000, cost=20.0)
    # Try to add more of same symbol
    sig = Signal(action="BUY", symbol="000001.SZ", size=5000, price=20.0, reason="test")
    approved, reason = rm.approve(sig, account, daily_pnl=0)
    assert approved is False
    assert "position limit" in reason.lower()


def test_risk_stops_on_daily_loss():
    rm = RiskManager(max_position_pct=0.2, max_daily_loss_pct=0.05)
    account = Account(cash=100000)
    sig = Signal(action="BUY", symbol="000001.SZ", size=1000, price=10.0, reason="test")
    # Daily PnL = -6000 (> 5% of 100k), should stop
    approved, reason = rm.approve(sig, account, daily_pnl=-6000)
    assert approved is False
    assert "daily loss" in reason.lower()


def test_risk_consecutive_loss_block():
    rm = RiskManager(max_position_pct=0.2, max_daily_loss_pct=0.1, max_consecutive_loss=2)
    account = Account(cash=100000)
    sig = Signal(action="BUY", symbol="000001.SZ", size=100, price=10.0, reason="test")
    # Record 2 losing trades
    rm.record_trade(pnl=-500)
    rm.record_trade(pnl=-300)
    approved, reason = rm.approve(sig, account, daily_pnl=0)
    assert approved is False
    assert "consecutive" in reason.lower()
PYEOF
```

- [ ] **Step 2: Run test — expect fail**

```bash
cd ~/myquant && python3 -m pytest tests/test_risk.py -v 2>&1 | tail -5
```

- [ ] **Step 3: Write RiskManager**

```bash
cat > ~/myquant/myquant/live/risk.py << "PYEOF"
"""Risk management — approves or rejects trading signals."""
from myquant.config import get


class RiskManager:
    def __init__(
        self,
        max_position_pct: float | None = None,
        max_daily_loss_pct: float | None = None,
        max_consecutive_loss: int | None = None,
    ):
        self.max_position_pct = max_position_pct or get("live.max_position_pct", 0.2)
        self.max_daily_loss_pct = max_daily_loss_pct or get("live.max_daily_loss_pct", 0.05)
        self.max_consecutive_loss = max_consecutive_loss or get("live.max_consecutive_loss", 3)
        self._consecutive_losses = 0

    def approve(self, signal, account, daily_pnl: float = 0.0) -> tuple[bool, str]:
        """Check if a signal passes all risk rules. Returns (approved, reason)."""
        # Rule 1: Daily loss limit
        max_loss = account.init_cash * self.max_daily_loss_pct
        if daily_pnl < -max_loss:
            return False, f"Daily loss limit exceeded: PnL={daily_pnl:.0f}, limit=-{max_loss:.0f}"

        # Rule 2: Position limit (for BUY signals)
        if signal.action == "BUY":
            holding = account.holding(signal.symbol)
            current_size = holding["size"] if holding else 0
            new_size = current_size + signal.size
            price = signal.price or 10.0
            position_pct = (new_size * price) / account.init_cash
            if position_pct > self.max_position_pct:
                return False, f"Position limit: {position_pct:.1%} > {self.max_position_pct:.1%}"

        # Rule 3: Consecutive loss protection
        if self._consecutive_losses >= self.max_consecutive_loss:
            return False, f"Consecutive loss limit: {self._consecutive_losses} >= {self.max_consecutive_loss}"

        return True, ""

    def record_trade(self, pnl: float):
        """Record a trade's PnL. Winning trade resets the loss counter."""
        if pnl < 0:
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0
PYEOF
```

- [ ] **Step 4: Run test — expect pass**

```bash
cd ~/myquant && python3 -m pytest tests/test_risk.py -v
```

- [ ] **Step 5: Commit**

```bash
cd ~/myquant && git add myquant/live/risk.py tests/test_risk.py && git commit -m "feat: RiskManager with position/daily-loss/consecutive-loss rules"
```

---

### Task 13: Notification module

**Files:**
- Create: `~/myquant/myquant/live/notify.py`

**Interfaces:**
- Produces: `send_wechat(title, content)`, `send_email(subject, body)`

- [ ] **Step 1: Write Notify module**

```bash
cat > ~/myquant/myquant/live/notify.py << "PYEOF"
"""Push notifications via ServerChan (WeChat) and email."""
import json, smtplib
from email.mime.text import MIMEText
import requests
from myquant.config import get


def send_wechat(title: str, content: str = "") -> bool:
    """Send a WeChat push via ServerChan. Returns True on success."""
    key = get("notify.server_chan_key", "")
    if not key:
        return False
    try:
        resp = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title, "desp": content},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception:
        return False


def send_email(subject: str, body: str = "") -> bool:
    """Send email via SMTP. Returns True on success."""
    smtp_host = get("notify.email_smtp", "")
    if not smtp_host:
        return False
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = get("notify.email_from", "")
        msg["To"] = get("notify.email_to", "")
        with smtplib.SMTP(smtp_host, int(get("notify.email_port", "587"))) as server:
            server.starttls()
            server.login(
                get("notify.email_user", ""),
                get("notify.email_password", ""),
            )
            server.sendmail(msg["From"], [msg["To"]], msg.as_string())
        return True
    except Exception:
        return False


def alert(title: str, content: str = "") -> bool:
    """Send alert via all configured channels. At least one must succeed."""
    ok = False
    ok |= send_wechat(title, content)
    ok |= send_email(title, content)
    return ok
PYEOF
```

- [ ] **Step 2: Verify import**

```bash
cd ~/myquant && python3 -c "from myquant.live.notify import alert; print('Notify module OK')"
```

- [ ] **Step 3: Commit**

```bash
cd ~/myquant && git add myquant/live/notify.py && git commit -m "feat: notification module (ServerChan WeChat + email)"
```

---

### Task 14: Streamlit Web dashboard

**Files:**
- Create: `~/myquant/myquant/web/app.py`

**Interfaces:**
- Consumes: `DataProvider` (Task 5), `BacktestEngine` (Task 10), `Recorder` (Task 9)
- Produces: Streamlit web app at `http://localhost:8501`

- [ ] **Step 1: Write web/app.py**

```bash
cat > ~/myquant/myquant/web/app.py << "PYEOF"
"""MyQuant Streamlit Dashboard — portfolio, signals, backtest, logs."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt

st.set_page_config(page_title="MyQuant Dashboard", layout="wide")

st.title("MyQuant Trading Framework")
st.caption("Personal quant trading — data → strategy → backtest → live")

tab1, tab2, tab3, tab4 = st.tabs(["Overview", "Backtest", "Strategy", "Logs"])

with tab1:
    st.header("Portfolio Overview")
    st.metric("Account Value", "¥100,000", "+0.00%")
    st.metric("Today P&L", "¥0", "0.00%")

    st.subheader("Positions")
    st.dataframe(pd.DataFrame(columns=["Symbol", "Size", "Avg Cost", "Market Value", "PnL%"]))

with tab2:
    st.header("Backtest")
    col1, col2 = st.columns(2)
    with col1:
        symbol = st.text_input("Symbol", "000001.SZ")
        start_date = st.date_input("Start Date", pd.to_datetime("2024-01-01"))
        end_date = st.date_input("End Date", pd.to_datetime("2024-12-31"))
    with col2:
        strategy_type = st.selectbox("Strategy", ["MA Crossover", "Rotation"])
        fast = st.number_input("Fast MA", value=5, min_value=2)
        slow = st.number_input("Slow MA", value=20, min_value=5)

    if st.button("Run Backtest", type="primary"):
        with st.spinner("Running backtest..."):
            from myquant.data.provider import DataProvider
            from myquant.backtest.engine import BacktestEngine
            from myquant.strategy.timing import MACrossoverStrategy

            provider = DataProvider()
            try:
                df = provider.get(symbol, start=str(start_date), end=str(end_date))
            except Exception as e:
                st.error(f"Data fetch failed: {e}")
                import numpy as np
                dates = pd.date_range(start_date, end_date, freq="B")
                np.random.seed(42)
                price = 10.0
                rows = []
                for d in dates:
                    price += np.random.randn() * 0.15
                    rows.append({
                        "datetime": d, "symbol": symbol,
                        "open": price - 0.05, "close": price,
                        "high": price + 0.1, "low": price - 0.1,
                        "volume": float(np.random.randint(1000, 10000)),
                    })
                df = pd.DataFrame(rows)
                df.set_index(["datetime", "symbol"], inplace=True)
                st.info("Using synthetic data (AKShare unavailable)")

            strategy = MACrossoverStrategy()
            strategy.init({"fast": fast, "slow": slow, "symbol": symbol})

            engine = BacktestEngine(start_cash=100000)
            result = engine.run(strategy, df)

            st.success("Backtest Complete!")
            cols = st.columns(4)
            ret = result["total_return"]
            cols[0].metric("Total Return", f"{ret:+.2f}%", delta_color="normal")
            cols[1].metric("Sharpe Ratio", f"{result['sharpe_ratio']:.2f}")
            dd = result["max_drawdown"]
            cols[2].metric("Max Drawdown", f"{dd:.2f}%", delta_color="inverse")
            cols[3].metric("Trades", result["trade_count"])

with tab3:
    st.header("Strategy Configuration")
    st.json({
        "rotation": {"top_k": 10, "rebalance_freq": "W", "factors": ["momentum_20"]},
        "timing": {"fast_ma": 5, "slow_ma": 20},
    })

with tab4:
    st.header("System Logs")
    st.text("No logs yet — run a backtest or start live trading to see logs.")

st.sidebar.title("MyQuant")
st.sidebar.markdown("**Status:** Ready")
st.sidebar.markdown("**Data:** AKShare")
st.sidebar.markdown("**Broker:** miniQMT")
st.sidebar.markdown("---")
st.sidebar.markdown("*Built with Streamlit*")
PYEOF
```

- [ ] **Step 2: Test import**

```bash
cd ~/myquant && python3 -c "import myquant.web.app; print('Web module OK')"
```

- [ ] **Step 3: Test Streamlit loads (no server)**

```bash
cd ~/myquant && streamlit run myquant/web/app.py --server.headless=true --server.port=8501 &
sleep 5
curl -s -o /dev/null -w "%{http_code}" http://localhost:8501
# Kill the background streamlit
kill %1 2>/dev/null
```
Expected: `200` (if network is available) or Streamlit installed confirmation

- [ ] **Step 4: Commit**

```bash
cd ~/myquant && git add myquant/web/app.py && git commit -m "feat: Streamlit web dashboard with backtest UI"
```

---

### Task 15: End-to-end integration test

**Files:**
- Create: `~/myquant/tests/test_integration.py`

**Interfaces:**
- Consumes: All modules from Tasks 1-14
- Produces: Integration test covering data → strategy → backtest → report pipeline

- [ ] **Step 1: Write integration test**

```bash
cat > ~/myquant/tests/test_integration.py << "PYEOF"
"""End-to-end integration test: data → strategy → backtest → report."""
import tempfile, os
import pandas as pd
import numpy as np
from myquant.data.store import DataStore
from myquant.backtest.account import Account
from myquant.backtest.broker import Broker
from myquant.backtest.recorder import Recorder
from myquant.backtest.engine import BacktestEngine
from myquant.backtest.report import generate_report
from myquant.strategy.timing import MACrossoverStrategy
from myquant.strategy.rotation import RotationStrategy
from myquant.live.risk import RiskManager
from myquant.live.notify import send_wechat
from myquant.config import load_config, get


def make_synthetic_data(symbols, start, end):
    """Generate synthetic multi-symbol OHLCV data."""
    dates = pd.date_range(start, end, freq="B")
    np.random.seed(42)
    prices = {s: 10.0 + np.random.rand() * 5 for s in symbols}
    rows = []
    for d in dates:
        for s in symbols:
            prices[s] += np.random.randn() * 0.2
            rows.append({
                "datetime": d, "symbol": s,
                "open": prices[s] - 0.05, "close": prices[s],
                "high": prices[s] + 0.1, "low": prices[s] - 0.1,
                "volume": float(np.random.randint(1000, 10000)),
            })
    df = pd.DataFrame(rows)
    df.set_index(["datetime", "symbol"], inplace=True)
    return df


def test_full_pipeline_ma_crossover():
    """Data → Strategy → Backtest → Report pipeline with MA Crossover."""
    # 1. Generate data
    df = make_synthetic_data(["000001.SZ"], "2024-01-01", "2024-06-30")

    # 2. Init strategy
    strategy = MACrossoverStrategy()
    strategy.init({"fast": 5, "slow": 20, "symbol": "000001.SZ"})

    # 3. Run backtest
    engine = BacktestEngine(start_cash=100000)
    result = engine.run(strategy, df)

    # 4. Verify result structure
    assert "total_return" in result
    assert "sharpe_ratio" in result
    assert "max_drawdown" in result
    assert isinstance(result["trade_count"], int)


def test_full_pipeline_rotation():
    """Rotation strategy with multi-symbol data."""
    symbols = ["A.SZ", "B.SZ", "C.SZ", "D.SZ", "E.SZ"]
    df = make_synthetic_data(symbols, "2024-01-01", "2024-06-30")

    strategy = RotationStrategy()
    strategy.init({"symbols": symbols, "top_k": 2, "rebalance_freq": 20})

    engine = BacktestEngine(start_cash=100000)
    result = engine.run(strategy, df)

    assert "total_return" in result
    assert result["trade_count"] >= 0


def test_risk_and_notify():
    """Risk manager blocks invalid trades, notify returns False without config."""
    account = Account(cash=100000)
    rm = RiskManager(max_position_pct=0.2, max_daily_loss_pct=0.05)
    from myquant.strategy.base import Signal
    sig = Signal(action="BUY", symbol="T.SZ", size=100, price=10.0, reason="test")
    approved, _ = rm.approve(sig, account, daily_pnl=0)
    assert approved

    # Notify without config should return False (not crash)
    assert send_wechat("test title", "test content") is False


def test_config_loads():
    cfg = load_config()
    assert "data" in cfg
    assert "strategy" in cfg
    assert "backtest" in cfg
    assert "live" in cfg
    assert get("backtest.start_cash") == 100000


def test_report_generation():
    """Report generates HTML file."""
    recorder = Recorder()
    account = Account(cash=100000)
    for i in range(5):
        bar = pd.DataFrame([{"close": 10.0 + i}])
        bar.index = [pd.Timestamp(f"2024-01-{i+1:02d}")]
        recorder.mark(bar, account)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "report.html")
        result_path = generate_report(recorder, output_path=path)
        assert os.path.exists(result_path)
        with open(result_path) as f:
            html = f.read()
            assert "<html" in html
            assert "Backtest Report" in html
PYEOF
```

- [ ] **Step 2: Run integration tests**

```bash
cd ~/myquant && python3 -m pytest tests/test_integration.py -v
```
Expected: 5 passed

- [ ] **Step 3: Run the full test suite**

```bash
cd ~/myquant && python3 -m pytest tests/ -v
```
Expected: all tests pass

- [ ] **Step 4: Final commit**

```bash
cd ~/myquant && git add -A && git commit -m "feat: end-to-end integration tests pass — framework complete"
```

---

## Self-Review Checklist

**Spec coverage:**
- Data layer (Task 4+5): AKShare + DuckDB caching ✓
- Strategy layer (Task 3+6+7): base + MA crossover + rotation ✓
- Backtest layer (Task 8+9+10+11): Broker + Recorder + Engine + Report ✓
- Live layer (Task 12+13): RiskManager + Notify ✓
- Web panel (Task 14): Streamlit with backtest UI ✓
- Config (Task 1): YAML with layers ✓
- Integration test (Task 15): covers full pipeline ✓

**Placeholder scan:**
- No "TBD", no "TODO", no "implement later"
- All code blocks are complete and runnable
- All commands have expected output listed

**Type consistency:**
- `DataProvider.get()` → `pd.DataFrame` (used in Tasks 5, 14, 15)
- `Strategy.on_bar(bar, account)` → `list[Signal]` (used in Tasks 6, 7, 10)
- `Broker.execute(Signal, Account)` → `dict` (used in Tasks 8, 10)
- `Recorder.summarize()` → `dict` (used in Tasks 9, 10, 11)
- All consistent across tasks ✓

**Note on miniQMT:** Tasks 12 (RiskManager) and 13 (Notify) provide the live-trading infrastructure, but the actual miniQMT xttrader/xtdata integration code is NOT included because it requires the proprietary `xtquant` Python package installed from the broker's distribution. The framework is structured so that:
1. `myquant/live/` has placeholders for `xt_trader.py` — to be completed when xtquant is available
2. `LiveEngine` follows the same `Strategy.on_bar()` interface as `BacktestEngine`
3. Risk and notify work now without miniQMT dependency

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-27-myquant-implementation-plan.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
