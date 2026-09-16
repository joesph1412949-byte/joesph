# tt_solo —— 做T策略自包含化 + 仪表盘重建（设计）

- 日期：2026-09-16
- 状态：待用户评审
- 决策来源：用户于 2026-09-16 拍板「脱离 prism 依赖，做真正自包含的独立项目」+「在现有 tt_web 上重做/大幅升级成仪表盘」

---

## 1. 目标

把 `tt/` 做T策略从 A 股主项目（prism）里真正剥离出来，成为一个**能整体拷走、自给自足、独立运行**的项目 `tt_solo/`，并把 `tt_web` 监控台重建为一个**一屏决策仪表盘**。

**完成定义**：

1. `tt_solo/` 不 import 任何 `prism` / `shared` / `qmt_sync` / `backtest` / `legacy` 模块；
2. `tt_solo/` 的测试全绿，用例数 ≥ 现有 150；
3. 仪表盘在 `127.0.0.1:5011` 独立跑通；
4. 原有启动入口（.bat）全部指向新目录且可用；
5. 用户确认后，删除 `tt/` + `tt_web/`，**只留一份**。

---

## 2. 现状（实测，非推断）

### 2.1 `tt/` 已具备的结构

`tt/` 本身已是分层的独立子包，`grid.py` / `risk.py` 为零 IO 纯逻辑：

| 模块 | 行数 | 职责 |
|---|---|---|
| `grid.py` | 206 | 纯逻辑：波动率带宽、档位阶梯、开关三态、穿越判定 |
| `risk.py` | — | 风控闸门（含 `limit_ratio_for_code` 调用） |
| `state.py` | 277 | 日重置账本 + FIFO 配对盈亏 + 原子写 |
| `engine.py` | 468 | 编排：行情+网格+账本+风控 → Intent |
| `executor.py` | ~300 | miniQMT 外部直连下单 |
| `market.py` | 243 | 行情后端（xtdata / sample CSV） |
| `config.py` | 285 | 配置加载+深合并+fail-closed 校验 |
| `daemon.py` | 405 | 守护：轮询→落信号→记账→快照 |
| `arm_today.py` | — | 人工放行闸门工具 |

**结论：结构不需要大改，需要的是切断外部依赖 + 归属路径。**

### 2.2 外部依赖全清单（唯一真相，已 `grep` 实证）

| 依赖项 | 调用点 | 用途 |
|---|---|---|
| `shared.common.atomic_write` | `state.py:21`、`daemon.py:28`、`arm_today.py:28` | 原子写（mkdir+fsync+os.replace） |
| `shared.common.STATE_DIR` | `state.py:21`、`daemon.py:28` | 账本/快照目录 |
| `shared.common.limit_ratio_for_code` | `risk.py:16` | 涨跌停比例（板块判定） |
| `shared.common.is_local_request` | `tt_web/app.py:113` | 分级写护栏判据 |
| `prism.live_account.LiveAccount` | `engine.py:103` | 真实账户只读 |

**共 5 个符号，跨 6 个文件。** 依赖面很小 —— 这是本次工作量可控的根本原因。

### 2.3 已确认的环境事实

- **当前无任何 python 进程在跑**（`Get-CimInstance` 实测）→ 无新旧并发污染风险；
- `runtime/state/tt_state.json` 内容为 `{"version":1,"date":"2026-09-14","symbols":{},"events":[]}` —— **账本为空，无数据需迁移**；
- `D:/QMT_SIGNALS/real/armed.txt` 内容为 `20260914` —— 对今天（2026-09-16）**已过期**；
- 测试基线：`python -m pytest tt/tests tt_web/tests` = **150 passed**；
- `tt/` 与 `tt_web/` **均已入库**（`git ls-files` 实测），不存在未跟踪风险。

### 2.4 顺带发现的问题

- 现有 6 个做T `.bat` 文件为 **GBK 编码**，却设了 `chcp 65001`（UTF-8）→ 中文提示在控制台显示为乱码。新入口需统一编码。

---

## 3. 设计

### 3.1 目录结构

```
tt_solo/
  ttcore/                    # 策略核心
    __init__.py
    _vendor.py               # 内联底座 + 路径根（唯一真相）
    broker.py                # 自包含账户只读（吸收原 prism.live_account）
    grid.py                  # 零 IO 纯逻辑（原样迁入）
    risk.py                  # 零 IO 闸门（改 import 源）
    market.py state.py engine.py executor.py
    config.py daemon.py arm_today.py
    sample_data/*.csv
    tt_config.json
  dashboard/                 # 仪表盘
    app.py
    templates/index.html
    static/echarts.min.js
  tests/                     # 全套测试（原 tt/tests + tt_web/tests）
    conftest.py ...
  runtime/                   # 本项目的运行数据（gitignore）
    state/ log/
  README.md
  requirements.txt
```

### 3.2 依赖内联策略：`ttcore/_vendor.py`

**取舍：内联单文件，而非再造一个 `shared` 包。**

理由：5 个符号合计约 60 行。为 60 行代码造一层包结构 + 独立版本管理，是过度设计。内联到单文件，每个函数顶部标注来源与日期，保留回溯能力。

```python
# ttcore/_vendor.py
# ponytail: vendored from shared/common.py @2026-09-16 -- tt_solo 自包含化
# 刻意不 import shared: 本项目能被整体拷走、独立运行。
```

内联内容：

| 符号 | 处理 |
|---|---|
| `atomic_write(path, text)` | 原样内联（mkdir → 同目录 .tmp → flush → **fsync** → `os.replace`）。fsync 必须保留：它是断电存活的关键，不是可选项。 |
| `limit_ratio_for_code(code)` | 原样内联（BJ 30% / 创业板+科创 20% / 主板 10%） |
| `is_local_request(cf_ip, remote_addr)` | 原样内联含 `_is_rfc1918`（分级写护栏判据） |
| `PROJECT_ROOT` / `STATE_DIR` / `LOG_DIR` | **新定义**（见 3.3） |

### 3.3 路径归属：自带 `runtime/`

**决策：`tt_solo/` 自带 `runtime/`，与主项目 `runtime/state/` 分家。**

这是"真自包含"的必然代价 —— 做T的账本/快照/日志不再与 prism 同目录。用户已确认接受（"独立性 > 目录统一"）。

```python
# ttcore/_vendor.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent   # -> tt_solo/
RUNTIME_DIR  = Path(os.environ.get("TT_RUNTIME_DIR") or PROJECT_ROOT / "runtime")
STATE_DIR    = RUNTIME_DIR / "state"
LOG_DIR      = RUNTIME_DIR / "log"
```

- 环境变量 `TT_RUNTIME_DIR` 可覆盖（演练/测试隔离用）；
- 老 `runtime/state/tt_state.json` 内容为空 → **无数据迁移**；`tt/` 删除时一并清理其残留；
- `TT_SIGNAL_ROOT` 语义不变（默认 `D:/QMT_SIGNALS`）—— 信号目录是**与外部 QMT 的契约**，不属于本项目数据，保持原样。

### 3.4 `ttcore/broker.py`

吸收原 `prism/live_account.py`（`_QmtBackend` + `LiveAccount` 两个类，约 160 行），去掉 prism 前缀，保持**只读**语义。

```python
class LiveAccount:      # 只读：asset() / positions() / total_asset() / available_cash() / can_use_map()
class _QmtBackend:      # xtquant 连接层
def calc_buy_volume(price, total_asset, position_ratio=0.15, lot=100)
```

`engine._real_account_state()` 的 import 改指 `ttcore.broker`。**保留原有的 try/except 降级链**：账户取不到 → 纸面推演（`source="paper"`），这是既有行为，不改变。

### 3.5 仪表盘重建

#### 后端：接口清单

保留已验证的 6 个（不改签名，避免破坏既有测试）：

| 接口 | 作用 |
|---|---|
| `GET /api/status` | 主快照（守护快照优先，过期则只读重算） |
| `GET /api/config` | 脱敏配置 |
| `GET /api/kline/<code>` | 日K（画走势+档位） |
| `POST /api/pause` | 急停（需 `confirm=true`） |
| `POST /api/arm` | 当日放行（需 `confirm=true`） |
| `GET /api/health` | 存活探测 |

**新增 2 个**：

| 新接口 | 返回 | 用途 |
|---|---|---|
| `GET /api/ledger/history?days=20` | 逐日账本序列 | 收益曲线（往返次数/实现盈亏/净敞口趋势） |
| `GET /api/rejections` | 按 `reject_code` 聚合计数 + 明细 | 「为什么没成交」归因排行 |

**新增接口必须沿用现有安全约束**：只读、不发单、异常不白屏（catch → `{ok:false,error}` + 500）。

#### 前端：一屏决策面板

替换现有 27KB 单页。五个区块，自上而下即决策顺序：

1. **状态条（最重要）** —— 「今天到底能不能做T」
   三道闸门串联可视化：`dry_run` → `paused` → `armed`，加环境（real/sim）+ 行情源 + 快照新鲜度。
   **必须一眼可判**：任一闸门关闭 → 整条红色并标出被拦在哪一道。
2. **账户卡** —— 总资产 / 现金 / 市值 / 持仓明细（标注 `source`：qmt 还是 paper 推演，不可混同）。
3. **档位阶梯图** —— 每只票竖向阶梯：ref 中枢线、当前价、买卖各档、已成交档位高亮、开关状态（ENABLED/HALF/DISABLED）。
4. **今日战果** —— 往返次数 / 实现盈亏 / 净敞口 / 当日笔数 vs 上限 / 熔断状态。
5. **被拦原因排行** —— 条形图按原因码聚合，可下钻明细（含每条的具体文案）。

**技术选型：Flask + 原生 JS + ECharts（已 vendored）**。
不引入 React/Vite —— 与项目现有风格冲突且引入构建链，收益不成比例。

#### 端口与安全

- 端口 **5011**（`TT_WEB_PORT` 可覆盖）—— 不抢 `tt_web` 的 5010；
- **只监听 `127.0.0.1`**（硬约束，不对外）；
- 分级写护栏沿用：`pause` / `arm` 仅限本机，远程 403；
- 面板**只能关闸，不能开单** —— 安全红线，不因"仪表盘"而放宽。

### 3.6 启动入口

更新 6 个 `.bat` 指向新路径，并**统一修复编码**（实测现存文件为 GBK + `chcp 65001` → 乱码）：

| 文件 | 变更 |
|---|---|
| `启动做T守护.bat` | `python -m tt.daemon` → working dir 改 `tt_solo` |
| `启动做T直连守护.bat` | 同上（`--direct`） |
| `启动做T实盘直连.bat` | 同上（`--direct --live`） |
| `启动做T模拟守护.bat` | 同上（`--env sim`） |
| `做T-今日放行.bat` | 路径改 `tt_solo\ttcore\arm_today.py` |
| `启动做T监控台.bat` | 改指仪表盘，端口提示 5011 |

编码统一为 **UTF-8 无 BOM + `chcp 65001`**，中文提示不得乱码。

---

## 4. 不做什么（YAGNI）

| 不做 | 理由 |
|---|---|
| React / Vite / 构建链 | 与项目 Flask+原生 JS 风格冲突，现有 ECharts 已够用 |
| WebSocket 实时推送 | 5 秒轮询对日内做T完全足够 |
| 面板下单按钮 | 安全红线：面板只能关闸。不因"仪表盘"放宽 |
| 新建 `shared` 包或发布 PyPI 包 | 60 行代码不值得造包结构 |
| 改策略逻辑（band/档位/风控口径） | 本次是**搬家 + 换皮**，不是改策略。逻辑变更会污染"搬家无回归"的验证 |
| 多账户 / 多策略并行 | 无需求 |

---

## 5. 实施顺序（含删除顺序）

**关键：不能先删 `tt/`** —— 会丢参照物且不可回退。

1. **建 `tt_solo/`** —— 拷贝 `tt/` + `tt_web/`，改 import，写 `_vendor.py` / `broker.py`
2. **测试迁移** —— 改测试 import，目标全绿且 ≥150
3. **仪表盘** —— 新 `dashboard/`，5011 冒烟通过
4. **入口更新** —— 6 个 `.bat` 指向新路径 + 修复编码
5. **双跑对照验证** —— 新旧各跑一次 `--sample` / 只读 plan，逐字段比对决策输出一致（证明"搬家无回归"）
6. **【用户确认点】** —— 再问一次用户，确认后才删 `tt/` + `tt_web/` + 老 `runtime/state` 残留

第 6 步是**独立的人工确认点**，不自动执行（删除不可逆）。

---

## 6. 验证标准

| 项 | 通过条件 |
|---|---|
| 依赖剥离 | `rg "^(from\|import) (prism\|shared\|qmt_sync\|backtest\|legacy)" tt_solo/` 无输出（ripgrep/Rust 正则：`\|` 即或运算） |
| 测试 | `pytest tt_solo/tests` 全绿，用例数 ≥ 150 |
| 无回归 | 步骤 5 双跑对照：同输入下 Intent 列表逐字段一致 |
| 仪表盘 | 6+2 个接口全 200；`pause`/`arm` 无 `confirm` 返 400、带 `confirm` 生效；远程请求返 403 |
| 编码 | 新 `.bat` 中文提示不乱码 |
| 路径独立 | 跑一轮后 `tt_solo/runtime/state/` 生成文件，主 `runtime/state/tt_state.json` **不被触碰** |

---

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| `LiveAccount` 吸收后连接行为漂移 | `broker.py` 逐行搬移，不改逻辑；双跑对照验证账户读取结果一致 |
| 测试 import 改动引入假绿 | 逐文件改 import 后**先跑基线**确认行为不变，再改代码 |
| 仪表盘重建丢失现有功能 | 现有 index.html 保留为参照；新页面逐项对照 5 个区块是否覆盖 |
| 删除 `tt/` 后才发现遗漏引用 | 步骤 6 前先 `grep` 全仓引用 `tt\.` / `tt_web`，逐个确认已迁移 |
| 沙箱内 pytest 假失败 | 验证一律脱沙箱跑，避免把环境问题误判为代码问题 |

---

## 8. 待用户确认

1. 本设计整体是否认可？
2. `.bat` 入口名是否保持现状（`启动做T*.bat`）？还是希望改名为 `启动TT*.bat` 之类？
3. 步骤 5「双跑对照验证」是否要做？（做的话更稳，多花一点时间）
