# sector_momentum v5 — 板块综合评分链 设计文档

日期: 2026-08-31
状态: 已与用户确认设计方向（个股板块分 / 独立评分模块 / 引擎两处挂钩）

## 1. 背景与目标

sector_momentum v4 已实现: 移动止盈 trailing[8,5] + 环境门槛 threshold=3（N1+≥2宏观）。
OOS 验证显示切换期（7-8月科技→医药）亏损从 -17.76% 收窄到 -10.83%，但仍是亏损。

《8.23更新内容》文档核心遗留项——板块综合评分（35/25/30/10 权重）、方向确认信号
（综合强度>75）、仓位联动（评分每高10分+5%，设上限）——本轮落地为 v5。

**目标**: 用"个股板块综合分"同时回答两个问题——这只股该不该买（门槛）、买多重（仓位），
进一步降低风格切换期的亏损，同时不牺牲样本内收益。

## 2. 用户已确认的决策

| 决策点 | 选择 |
|---|---|
| 评分作用方式 | **个股板块分**: 候选股看自己所属板块的评分，>75 才买；无板块过线自然空仓 |
| 实现方案 | **方案一**: 独立评分模块 `prism/sector_score.py` + 引擎两处挂钩（非因子、非特判） |
| 现有宏观门槛 | 保持不变（N1 + ≥2 宏观计数门），评分链是额外一层 |
| 缺数据处理 | 分项缺数据 → 权重按比例归一化到剩余分项；完全无数据 → 不打分 → 不买（fail-closed）|

## 3. 评分定义（每板块 0-100）

综合分 = Σ(分项分 × 权重)，权重: 动量 35% / 资金流 25% / 拥挤度 30% / 宏观 10%。

### 3.1 动量分（35%）— 来源 mkt.sector K线

- r5 = 板块近5日涨幅(%), r10 = 近10日涨幅(%)
- `score = 0.5 × clamp(r5/6×100, 0, 100) + 0.5 × clamp(r10/10×100, 0, 100)`
- 语义: 5日涨6% 或 10日涨10% 即该子项满分; 下跌记 0。

### 3.2 资金流分（25%）— 来源 mkt.sector_flow（东财 fflow, secid=90.<801码>）

- 近5日主力净流入合计（main_net_in 求和, 元→亿元）
- `score = clamp(50 + 10 × 亿元, 0, 100)`
- 语义: 净流入 5亿 → 满分; 持平 → 50; 净流出 5亿 → 0。

### 3.3 拥挤度分（30%）— 来源 mkt.sector amount

- ratio = 近5日日均成交额 ÷ 近240日日均成交额
- `score = clamp(100 − max(0, ratio−1) × 40, 0, 100)`
- 语义: **反向指标**——缩量(≤1倍均量) 100 分; 放量 2 倍 → 60; 放量 3.5 倍 → 0。
  与 SEC4 的 ≤2.0 通过线自洽（2.0 倍对应 60 分, 中性偏安全）。

### 3.4 宏观分（10%）— 全市场共用（mkt.global）

- 纳指分 = `clamp(50 + 昨日纳指涨幅(%) × 50, 0, 100)`（+1% → 100, −1% → 0）
- 美债分 = `clamp(100 − max(0, 20日变化(百分点) − 0.3) × 100, 0, 100)`（≤0.3 → 100, ≥1.3 → 0）
- VIX分 = `clamp(100 − max(0, VIX − 20) × 20, 0, 100)`（≤20 → 100, ≥25 → 0）
- 宏观分 = 三者算术平均。注意: 美债变化是百分点差值，**不要 ×100**（v4 踩过的坑）。

### 3.5 缺数据降级

- 某分项数据不足（如资金流缺失、K线不足240日按可用天数算但<10日视为缺失）:
  该分项剔除，剩余分项权重归一化（如缺资金流 → 动量 35/75、拥挤 30/75、宏观 10/75）。
- 板块完全无 K 线数据 → 返回 None（无评分）→ 引擎不买该板块个股（fail-closed）。
- 宏观数据缺失（FRED/新浪全挂）→ 宏观分项剔除，权重归一化。

## 4. 引擎接入

### 4.1 策略 JSON 配置块（strategies/sector_momentum.json v5）

```json
"sector_score": {
  "enabled": true,
  "threshold": 75,
  "position": {"step": 0.05, "cap_ratio": 0.45}
}
```

- `threshold`: 严格大于才通过（score > 75）。
- `step`: 分数每高 10 分, 仓位比例相对 +5%。
- `cap_ratio`: 单笔仓位比例绝对上限。

### 4.2 回测引擎（prism/backtest.py）

- `run()` 每个交易日预计算一次全板块评分 dict（防未来: 只用 ≤asof 数据,
  复用 `_slice_mkt` 切片结果）, 传入 `_pick`。
- `_pick(...)`: 候选股经 sector_map 找到板块 → 分数 ≤75 或 None → 剔除;
  通过的交易 dict 记 `sector_score` 数值与 `pos_mult` 乘数, note 附"板块分XX"。
- 仓位乘数: `pos_mult = min(1 + (score − threshold)/10 × step, cap_ratio/position_ratio)`
  （position_ratio 现默认 0.30, 即 base）。`_simulate_equity` 对每笔用
  `day_nav × position_ratio × pos_mult`, 其余规则不变。
- 评分模块对引擎零侵入: backtest 只 import `sector_score.compute_scores`。

### 4.3 实盘/信号侧（prism/trader.py）

- 本轮仅做 **note 透传**: 信号备注附板块分（用户在 QMT 信号里可见）。
- 实盘下单量的评分联动不在本轮范围（trader 无独立仓位逻辑, 量在 QMT 侧）,
  留待回测验证有效后再接。

## 5. 验证方法

- 同区间对比 v4 vs v5:
  - 全区间 2026-01-01 ~ zt 缓存覆盖末日;
  - OOS 切分（沿用 run_oos: 4-6月样本内 / 7-8月样本外）。
- 消融: v5a（enabled + step=0, 只门槛）vs v5b（门槛+仓位联动）——分清各自贡献。
- 通过标准: OOS 切换期收益/回撤不差于 v4, 样本内收益不明显变差（>70% 保持）;
  不达标则调 threshold/权重/step 重测（参数全在 JSON, 无需改码）。

## 6. 数据前置（已并行进行中）

8/30 会话清理删掉了全部运行时缓存, 本轮已启动后台重建:
- `--build-sectors --beg 20250101 --source sw --rebuild`（31 板块K线 ✅ 6444天索引）
- `--build-sector-map`（✅ 2836 只; 20 个行业成分股接口偶发空表 → 补采脚本重试）
- `--build-global --source sina` + `--source fred`（✅ NDX/SPX/DJIA 415天 + US10Y 414 + VIX 427）
- `python -m prism.zt_history --build`（QMT 5216 只, 30-60 分钟, 进行中）
- **资金流注意**: SW 源主采集不含 fflow（`hasattr(feed,"fetch_sector_flow")` 为假）,
  需用 `EastMoneyProbe.fetch_sector_flow("90.<码>")` 单独补采 31 板块
  （pt_collect_flow.py, 8/31 已验证 fflow 解封）。评分模块只读 main_net_in。
- beg 取 20250101: 保证 SEC4/拥挤度的 240 日窗口完整。

## 7. 测试计划

- `prism/tests/test_sector_score.py`（新）:
  各分项边界（0分/满分/负值 clamp）、缺数据权重归一化、完全缺→None、
  宏观三项独立打分、防未来（只用 ≤asof 数据）。
- `prism/tests/test_backtest_mkt.py`（扩展）: 门槛过滤生效、note 记录、
  pos_mult 计算/封顶、step=0 时仓位不变。
- `prism/tests/test_m67_strategy.py`（更新）: v5 配置块断言（fids 不变 + sector_score 配置）。
- 全量回归（prism/prism_web/strategy_web, 删 paused 后跑）预期 402+ 全绿。

## 8. 不做的事（YAGNI）

- 不做全市场总分/双层控制（用户已选个股板块分）。
- 不做竞价模块、ETF 因子（后续方向）。
- 不动卖出席位逻辑（trailing/止损/持有期维持 v4）。
- 实盘下单量联动暂不做。
