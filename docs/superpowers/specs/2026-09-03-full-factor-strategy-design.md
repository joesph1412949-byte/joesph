# 全因子四层策略 + 因子库清理 设计规格

日期：2026-09-03 ｜ 状态：用户已批准设计方向（"ok"）
上游：策略编辑器（已交付）、模拟盘守护（热重载）、因子库 44 因子

## 1. 目标与动因

用户拍板：删除 M1-M5（五因子回测批）与手填因子（S1/S5/S7），剩余全部因子按原有四层框架（环境门控 + 首板 + 妖股 + 势能）组成一个新策略，作为今后唯一主力策略；**以后新增因子若无特殊要求，默认归层组合进该策略**（写入 MEMORY.md 惯例）。断链老策略全删。

已确认的关键认知（向用户披露过的）：回测里仅 K 线可算因子（~12/36）有效，其余因缺数据 fail-open 恒 0——同轮回测对全体候选一致稀释，排序不变，回测仍可比；实盘/模拟盘全因子生效。回测验 K 线内核、模拟盘验全因子的分工不变。

## 2. 删除清单

**因子（8 个文件，prism/factors/）**：
- `factor_m1_momentum.py`、`factor_m2_volume_surge.py`、`factor_m3_ma_bullish.py`、`factor_m4_breakout.py`、`factor_m5_low_volatility.py`（五因子回测批）
- `factor_s1_manual.py`、`factor_s5_financing.py`、`factor_s7_manual.py`（三个手填实现——S5 名"机构流入"但实现同为 ctx.manual 手填，不填恒 0，一并删）

**策略（5 个文件，prism/strategies/）**：
- `default.json`（势能层引用 M1/M2/M5；引擎配套历史基准，git 可溯）
- `five_factor.json`（整只即 M1-M5）
- `sector_momentum.json`、`sector_momentum_v4_base.json`、`sector_momentum_v5a_gateonly.json`（引用 M1）

**保留**：`first_board_v04.json`、`first_board_v03.json`（F 系因子，不受影响）+ 新策略。

删除后 `scan_factors` 总数：44 − 8 = **36**。

## 3. 新策略 `full_factor_v1`（全因子四层 v1）

```json
{
  "id": "full_factor_v1",
  "name": "全因子四层 v1",
  "description": "36因子四层框架(环境门控+首板+妖股+势能板块)。惯例: 今后新增因子默认归入对应层组合进本策略(特殊要求除外)。2026-09-03 起为模拟盘主力策略。",
  "market_gate": {"model": "node", "threshold": 3,
                  "factors": ["N1","N2","N3","N4","N5","N6","N7","N8"]},
  "scoring_models": [
    {"id": "first_board", "name": "首板", "weight": 0.60,
     "factors": ["F1","F2","F3","F4","F5","F6","F7","F8","F9"]},
    {"id": "monster", "name": "妖股", "weight": 0.25,
     "factors": ["Y1","Y2","Y3","Y4","Y5","Y6","Y7","Y8"]},
    {"id": "momentum", "name": "势能板块", "weight": 0.15,
     "factors": ["M6","M7","S2","S3","S4","S6",
                 "SEC1","SEC2","SEC3","SEC4","SEC6"]}
  ],
  "composite": {"mode": "top3_weighted", "weights": [0.60, 0.25, 0.15],
                "cap": 9.05},
  "filters": {"candidate_min_model": 3, "environment_threshold": 3},
  "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05,
                 "max_hold_days": 5}
}
```

设计依据：
- 四层 = default 原骨架（环境门控 N + 首板 F + 妖股 Y + 势能）；F8/F9 归首板层沿用 v04 先例；SEC 系归势能板块层（不新增第五层，尊重"四个层面"）
- 权重 0.60/0.25/0.15 沿用 default 原比例；cap = Σ(权重×因子数) = 0.60×9 + 0.25×8 + 0.15×11 = 9.05
- 门槛 threshold 3/8：N6-N8 回测 fail-open 恒 0 → 回测实质仍是 N1-N5 里取 3（与 default 行为一致）；实盘有注入时 N6-N8 参与加分
- 卖出规则与 default 一致（0.08/0.05/5 天）
- 落盘方式：直接写 JSON 文件 + `engine.load_strategy` 试载验证（编辑器 3 模型上限满足，但手写 JSON 更精确可重复）

## 4. 配套动作

1. **指针切换**：新策略落盘后经 `engine.set_active_strategy("full_factor_v1")`（或网页"设为默认"）切指针——**不手工改 .active.json**。时序安全：删 default 后指针所指文件暂缺 → 既有回落逻辑接管（→ first_board_v04），守护热重载到 v04；随即激活新策略再次热重载。无缝。
2. **MEMORY.md 更新**：①新增"因子管理惯例"条目（新因子默认归层入 full_factor_v1，四层=门控N/首板F/妖股Y/势能板块M·S·SEC）；②模拟盘归因切换点（2026-09-03 起 default→full_factor_v1，账本不清零）；③项目现状（策略数、因子数 36+保留 v04/v03）。
3. **测试同步**：引用被删策略/因子的测试全部更新（`test_engine_weights.py` 的 default 基线改用 v04/v03 或内联夹具；`test_app.py` 若有 five_factor 底稿用例改用 v04 或内联 dict 夹具）；新增：36 因子数断言、新策略 load 试载断言、无残留引用 grep 断言（可选脚本化）。

## 5. 行为变化披露（已向用户确认）

1. 模拟盘**换引擎**：净值曲线连续但归因切换（09-03 前 default、之后 full_factor_v1），账本不清零
2. 回测分数绝对值被缺数据因子稀释（排序不受影响）；SEC/N6-N8 回测恒 0
3. "历史基准"default 移除，策略间对比靠 v04 + 回测记录
4. Level-2（盘口 10 档/逐笔/委托队列）为**独立后续决策**：直接受益 F2/F3 精度 + 排板队列真实化；其余 ~20 个缺数据因子需的是东财/板块/全球数据管道（多为免费），与 L2 无关。先跑模拟盘攒证据再决定。

## 6. 测试计划

- 因子库：scan 后 36 个；M1-M5/S1/S5/S7 不可注册（文件已删）
- 新策略：`load_strategy("full_factor_v1")` 通过 validator；run_screen 冒烟一次（mock 数据，四层各出分）
- 断链清零：`grep -r "M[1-5]|S1|S5|S7"` 在 prism/factors、prism/strategies、测试中无存活引用（测试夹具内联字符串除外，需逐个核对语义）
- 指针：activate 后 `.active.json` 指向 full_factor_v1；回落路径测试仍绿（指针坏→v04）
- 回归：全量 pytest 绿（当前基线 423，随测试增删变化，以实际为准）

## 7. 风险与边界

- **测试引用面**：default/five_factor 在测试中的引用需逐个清点（实现时发现一处改一处，禁止跳过 fail-skip）
- 编辑器"复制为底稿"对 dict 形态已支持（I-F2 修复），新策略为字符串形态，无兼容问题
- 未来新因子入策略 = 手工编辑本策略 JSON（加进对应层 + cap 重算）——不建自动化（YAGNI），MEMORY.md 惯例约束即可
