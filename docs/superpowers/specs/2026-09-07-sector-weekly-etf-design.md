# 板块周度跟踪升维设计（ETF 锚点 / 60日新高 / 纸面仓位列）

- 日期：2026-09-07
- 来源：用户提供的对话（"分歧在，行情就在"+信谛听行业评分器+板块四状态仓位映射），经 prism-upgrade-triage 三问门评估
- 决策记录（用户拍板）：A 周度排名+ETF锚点 / D 60日新高家数 / B 建议仓位列（纸面）三项保留，**纯观察面板，不碰打分/买卖链路**；C3 机构共识去掉（无稳定免费结构化源，龙虎榜≠机构净买入，同 09-06 事件日历理由）；进打分/资金管理维持 09-06"先观察积累样本"拍板不变

## 0. Triage 结论（背景）

| 原建议 | 结论 | 理由 |
|---|---|---|
| 周度最强板块排名+ETF 锚点 | 保留 | sector_stage 已算 r5/占比/阶段；ETF 行情 QMT 当场实测通（512800/159865），不碰东财封禁 |
| 板块状态→仓位上限（75/50/30/10） | 降级为纸面列 | 数值无回测背书；接资金管理=红线 |
| 资金惯性（3日/5日） | 已有（查重命中） | flow_rank streak 即此物，等 push2 解封积累 |
| 占比>90 分位拥挤预警 | 已有（查重命中） | STAGE_SHARE_PCT=0.90 已内嵌高潮期判定 |
| 机构共识信号 | 去掉 | 无稳定数据源 |
| 板块内 60 日新高家数 | 保留 | zt 缓存 5217 只（09-07 已刷新）+ sector_map 直接算，63MB 加载 0.3s |
| 状态调制个股置信度/真实仓位 | 暂缓 | 红线，观察样本积累后用户明示再接 |

## 1. ETF 锚点映射（prism/sector_etf_map.py）

- 常量 `SECTOR_ETF_MAP: dict[str, dict]`：申万一级行业名（与 sector_map 缓存值同口径）→ `{"code": "512800.SH", "name": "银行ETF"}`。
- 31 行业逐个核对：**每个代码必须经 QMT 验证存在且名称一致**；行业无专属/规模过小 ETF → 留空 `{}`（面板显示 "—"），不硬凑。
- 锚点口径声明（docstring + 面板注明）：ETF 跟踪指数与申万行业覆盖有偏差，取"流动性最好的代表品种"性质，非精确映射。
- 候选基准（实现者经搜索+QMT 双重核验后可修正）：银行 512800 / 传媒 512980 / 军工 512660 / 煤炭 515220 / 有色 512400 / 医药 512010 / 房地产 512200 / 通信 515880 / 钢铁 515210 / 环保 512580 / 养殖(农林牧渔) 159865 / 家电 159996 / 非银 512070 / 电子 159997 / 化工(基础化工) 159870；其余行业实现者搜索补充。

## 2. ETF 行情（prism/market_data.py）

- `fetch_etf_quotes(codes) -> dict[code, {"amount": float, "pct_chg": float}]`：
  - xtdata `get_market_data_ex` count=2（日K）取最新成交额与涨跌幅；本地无数据 → 先 `download_history_data2` 该代码（每代码每日至多一次，模块级 memo）再读。
  - 单代码失败 → 跳过该条（fail-open），不影响其余。
  - 单位与 data.py 既有口径一致（amount 元；面板自行折亿显示）。

## 3. 60 日新高家数（prism/sector_stage.py）

- `new_high_counts(sector_map, zt_cache, window=60) -> dict[sector_name, {"nh": n, "base": d}]`：
  - 纯计算：个股最新收盘 ≥ 近 window 日（含当日）最高 → 计入 nh；base=该行业有足够历史（len≥window）的股票数。
  - 代码格式：zt 缓存键（600051.SH）与 sector_map 键同带后缀格式，直接 join（F9 已验证契约）。
- `sector_table(mkt, new_high=None, etf_quotes=None, etf_map=None)` 扩展（可选参，缺省 None 时对应列输出 None——纯函数性不破坏，向后兼容）：
  - 每行新增：`"new_high": {"nh": n, "base": d}`、`"etf": {"code", "name", "amount", "pct_chg"}`、`"pos_cap": int`、`"week_rank": int`（按 r5 降序名次，r5 缺失不参与）。
- 纸面仓位常量 `POS_CAP = {"孕育期":30, "启动期":50, "主升期":75, "高潮期":50, "退潮期":10, "休整期":30}`（docstring 注明：纸面参考，无回测背书，未接入资金管理）。
- app.py api_sector_stage 组装真实数据（zt 缓存加载 0.3s 可接受；sector_map 取自 market_data 缓存 map 段）；new_high 按缓存文件 mtime+日期 memo，避免每次 API 重算。
- **红线**：以上全部只读展示；POS_CAP 不进任何买入/仓位代码路径。

## 4. GUI（板块观察 tab 扩展）

- 新列：周排名（week_rank）/ ETF 锚点（代码+名称，title 显示成交额折亿与当日涨幅）/ 60 日新高（nh/base）/ 建议上限%（pos_cap）。
- XSS 契约沿用：插值点走 escHtml 或纯数值列，与既有面板同风格。
- tab 头部注明："建议上限为纸面参考，未接入交易"。
- 排序：默认按 week_rank 升序（周度排名视图即"信谛听"式周报）。

## 5. 验收

- 测试全绿（基线 486）+ 新增用例：new_high_counts 纯计算（构造缓存/映射）、sector_table 可选参注入与缺省 None、POS_CAP 常量、ETF quotes fail-open、API 组装透出、DOM 新列。
- 真数据冒烟：31 行业新列齐、ETF 锚点无错码（QMT 验证日志留档）、排行榜与手工核对一致。
