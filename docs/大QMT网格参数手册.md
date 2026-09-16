# 网格（做T）策略 · 大 QMT 三种跑法参数手册

> 生成 2026-09-14 ｜ 适用 `tt` 做T网格（日重置、前收定中枢）
> 结论：**大 QMT 完全能跑**，且比外部直连省两件事（不用管 Python 环境、不用管放行条文件）

---

## 〇、先澄清一个常见误解：桥为谁而存在

很多人以为「miniQMT 才用外部直连、大 QMT 才需要信号桥」——**前半句对，后半句的归因错了**。

真正决定走哪种通道的，是**两个互相独立的维度**：

1. **策略代码住哪儿** —— 外部 Python 进程？还是住进 QMT 里？
2. **单子怎么进柜台** —— `order_stock`（xtquant 外部 API）？还是 `passorder`（QMT 内策略 API）？

两两组合：

| 组合 | 是否顺路 | 说明 |
|---|---|---|
| miniQMT + 外部直连 | ✅ 天作之合 | miniQMT 本就是「砍掉 GUI、只留数据+交易服务」的产物；`XtQuantTrader` 连的正是它的 `userdata_mini` |
| 大 QMT + 策略内嵌 | ✅ 原生 | 策略住在 QMT 里，自己调 `passorder`，**这种组合根本不需要桥** |
| 大 QMT + 外部信号 + 内嵌下单 | ⚠️ 才需要桥 | 信号在进程 A 算，下单必须在进程 B 发生，两边无法直接调用 → 用文件当传话筒 |

**结论**：桥存在的唯一理由是「**信号在外、下单在内**」这个混搭，**与终端是 mini 还是完整版无关**。
如果把策略整体搬进大 QMT 用 `passorder` 重写，那就是第二行，**桥可以直接拆掉**。

> 本机实测：`bin.x64` 下 `XtItClient.exe`（大 QMT 完整版）与 `XtMiniQmt.exe`（miniQMT 极简版）**同时存在**，
> `userdata` 与 `userdata_mini` 两套数据目录也都在 —— 三条路都能走，选哪条只看你想让策略住哪儿。

**两个必须记住的前提**：

- 直连能否连上，取决于 **QMT 极速交易服务是否已登录**（`XtMiniQmt.exe` 在跑，或 `XtItClient` 以极速模式登录）。没登录 = `connect()` 失败，**不是代码问题**。
- 在 A2 与 B 之间切换时，**必须关掉另一条通道**（B 要去掉 `--direct`），否则同一信号会被下两次单。

---

## 一、先搞清楚：三种跑法

| 跑法 | 谁在下单 | 代码在哪 | 参数填在哪 |
|---|---|---|---|
| **A1** QMT 内原生写网格 | QMT `passorder` | QMT 策略编辑器 | QMT 策略参数 + 脚本常量 |
| **A2** QMT 跑桥、Python 算信号 ⭐ | QMT `passorder` | `qmt/bridge/signal_bridge_real.py` | 桥脚本头部 6 个常量 + `tt_config.json` |
| **B** 外部直连（当前在用） | Python `order_stock` | `tt/executor.py` | 只填 `tt_config.json` |

**A2 是最省事的"大 QMT 跑法"**：桥脚本已经写好、调试过，你只要把它粘进 QMT 就行。

---

## 二、A2 填法（QMT 跑桥）

### 2.1 粘进 QMT

1. QMT 终端 → **策略交易** → 新建策略
2. 模型类型选 **Python**
3. 把 `qmt/bridge/signal_bridge_real.py` **全文** 粘进编辑器
4. 保存 → **运行**（不要只"编译"）
5. 看日志出现 `[SignalBridge] REAL MODE env=real` 即成功

> ⚠️ **编码**：QMT 内是 **GBK**。这个脚本**刻意全用英文注释**，中文会乱码。你别顺手改成中文。

### 2.2 桥脚本头部 6 个参数

```python
SIGNAL_ROOT   = r"D:/QMT_SIGNALS"   # 信号根目录，与 tt 侧一致
ENVIRONMENT   = "real"              # real=实盘 / sim=模拟盘
DRY_RUN       = False               # True=只打印不下单；首次务必先 True 试
SCAN_INTERVAL = 2                   # 扫描间隔(秒)。做T够用
FILE_MIN_AGE  = 1.0                 # 文件落地多久才处理(秒) → 建议 0.2
FIXED_ACCOUNT = ""                  # 留空 = 自动用 QMT 当前登录账号 ⭐推荐
DEDUP_ENABLED = True                # 日去重，按 order_id（已修好）保持 True
MAX_ORDER_PRICE = 100000.0          # 胖手指保护：价格异常直接拒
```

**逐项说明**：

| 参数 | 怎么填 | 为什么 |
|---|---|---|
| `SIGNAL_ROOT` | `D:/QMT_SIGNALS` | 必须与 `tt` 侧写信号的目录**完全一致**，否则收不到 |
| `ENVIRONMENT` | `real` | 改成 `sim` 只读 `sim/` 目录，与实盘隔离 |
| `DRY_RUN` | 先 `True`，确认无误改 `False` | **改 False 才开始真下单** |
| `SCAN_INTERVAL` | `2` | 每 2 秒扫一次。做T不需要更快 |
| `FILE_MIN_AGE` | **`0.2`** | 原值 1.0 是防"文件写一半"。但 `tt` 侧已用 `os.replace` 原子写 → 1 秒纯延迟。**做T对延迟敏感，建议 0.2** |
| `FIXED_ACCOUNT` | `""`（留空） | 留空自动用 QMT 登录账号。填死在别的终端登录会挂错账户 |
| `DEDUP_ENABLED` | `True` | 防止信号文件重发/重启重扫。**已改为按 `order_id` 去重**（见下） |

### 2.3 ⚠️ 一个已修的致命坑（你原来看不了）

原桥的日去重键是 **股票代码** → **每只票每天只放行 1 单**。
做T要一只票一天下好几单（每档一单、买卖双向）→ **第 2 单起全被 `DUPLICATE` 拒掉，整套阶梯废掉**。

**已修**：`_dedup_key()` 改为优先用 `order_id`（`TT_日期_代码_方向_档位`，天然唯一），
没有 `order_id` 的手写信号才退回用代码。防重目的不变（重发/重启仍被拦），但不再误伤第 2 单。

---

## 三、A1 填法（QMT 内原生写网格）

如果你想全在 QMT 里闭环，**不该照抄 `tt` 的配置**（那是外部 Python 的），
而要填 QMT 策略编辑器的参数。核心对应关系：

| tt 配置项 | QMT 内等价写法 | 说明 |
|---|---|---|
| `ref_mode: prev_close` | `ContextInfo.get_market_data(...).iloc[-1]` 取前收 | QMT 里就是 **昨收** |
| `band_pct` | 直接写常数，如 `0.0165` | 海油 = 1.65% |
| `n_units × max_units` | 两个整数，如 `5` 和 `3` | 分母 5、实际做 3 档 |
| `weight` | `passorder` 里的 `volume` 自己算 | QMT 不给自动算金额 |
| `max_price_deviation_pct: 0.05` | `if abs(price/ref-1) > 0.05: return` | 自己写判断 |
| 三道闸门 | **建议仍保留 armed 文件** | QMT 内反而更需要，误点运行就真下单 |
| `dry_run` | `DRY_RUN = True` 常量 | 同上 |

**passorder 的坑**（从运行时报错逆推出来的，血的教训）：

```python
passorder(
    0,            # opType: 0=买 1=卖  ← 不是 23/24!
    0,            # orderType: 0=限价
    account_id,   # 账号字符串
    "600900.SH",  # 代码（必须带 .SH/.SZ 后缀）
    0,            # prType: 0=限价 2=对手价
    28.60,        # 价格
    300,          # 股数
    ContextInfo,  # ⚠️ 必须是策略上下文对象，传 int/str 会崩
)
```

**最容易犯的错**：`opType` 用 0/1，但外部 API 用 `STOCK_BUY=23 / STOCK_SELL=24` —— **两套数值，混用会下反单**。

---

## 四、A2 vs B 怎么选

| 维度 | A2（QMT 跑桥） | B（外部直连，当前） |
|---|---|---|
| Python 环境 | QMT 自带（3.6.8），**不用管** | 系统 3.12，需装 xtquant |
| 放行条文件 | **同样需要**（桥自己查 armed） | 需要 |
| 调试 | 看 QMT 日志，**不能跑 pytest** | **能跑 140 个单测** |
| 编码约束 | **GBK**，中文注释会乱码 | UTF-8，随便写 |
| 进程管理 | QMT 开着就在跑 | 要自己看着守护进程 |
| 断线恢复 | QMT 自己重连 | 需自己处理 |
| 出错排查 | 只能靠 print | 有完整日志 + 测试 |

**结论**：
- 想**省心、图形化、不怕断线** → **A2**
- 想**可测试、可回滚、能改逻辑** → **B**（当前方案已跑通）

两者**共用同一份 `tt_config.json`**，所以随时能切，不用改策略参数。

---

## 五、切换到 A2 的操作清单

```
1. 停掉外部直连守护（关掉 tifosi 高级选项 1 开出的那个守护窗口）
2. QMT 终端 → 策略交易 → 新建 Python 策略
3. 粘贴 qmt/bridge/signal_bridge_real.py 全文
4. 改 FILE_MIN_AGE = 0.2
5. DRY_RUN = True 先跑一天，看日志
6. 无误后改 DRY_RUN = False
7. 启动策略；每天开盘前仍要写 armed.txt
   外部侧改跑：cd tt_solo 后 python -m ttcore.daemon（不加 --direct）
   （或直接跑 tifosi 主菜单 1）
   → 信号落到 D:/QMT_SIGNALS/real/pending/，桥自动消费
```

> ⚠️ **两条通道不能同时开**：`--direct` 和桥同时跑会**双重下单**。
> 切到 A2 就必须去掉 `--direct`。

---

## 六、改动记录

- `qmt/bridge/signal_bridge_real.py`：日去重键 `stock_code` → **`order_id`**（新增 `_dedup_key()`）。
  修掉"每票每天只放行一单"的致命缺陷，使桥能支持做T多档多单。
  影响面：`strategy_web/tests/test_bridge_safety.py` 等 173 例测试通过。
