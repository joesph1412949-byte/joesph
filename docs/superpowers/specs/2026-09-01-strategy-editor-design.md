# 网页策略编辑器 + 默认策略切换 设计规格

日期：2026-09-01 ｜ 状态：已获用户批准（brainstorming 完成）
上游：44 因子库（42 存量 + F8 + F9）、现有策略格式（load_strategy 兼容）、模拟盘热重载

## 1. 目标与用户决策记录

在网页"策略"页新增**可视化策略编辑器**：用户（量门外汉）自己勾选因子、调权重、设门槛与卖出规则，保存即成为可用策略；并提供**设为默认**按钮，一处切换同时生效于选股默认、回测可选、模拟盘运行中策略。

用户已确认的决策：
- 范围 = **编辑器基础版 + 设为默认按钮**；**不做一键回测按钮**（用户明示"每次调整都回测太麻烦"——回测仍走现有回测页手动触发）
- 生效范围 = **选股 + 回测 + 模拟盘**（一处切换处处生效；模拟盘**热生效不重启守护**）
- 因子**内部参数不可改**（如 F8 的 3% 阈值）——编辑器只管"用不用/权重多少"；参数化因子阈值作为后续独立项目
- MVP 不支持原地编辑已有策略文件：新建 + "复制现有策略为底稿"；`default.json` 永不可编辑/覆盖（既有快照保护）

## 2. 系统组件

| 组件 | 位置 | 职责 |
|---|---|---|
| 编辑面板 | `prism_web/templates/index.html`（策略页追加）+ `static/app.js`（追加） | 新建按钮、复制底稿、分组因子勾选、权重/门槛/卖出输入、保存 |
| 创建端点 | `prism_web/app.py` 追加 `POST /api/strategies/create` | 组装 JSON → 校验 → 写 `prism/strategies/{id}.json` |
| 激活端点 | `prism_web/app.py` 追加 `POST /api/strategies/<sid>/activate` | 校验策略存在 → 写默认指针 |
| 默认指针 | `prism/strategies/.active.json`：`{"id": "...", "updated": "..."}` | 唯一真相源；gitignored（新增 .gitignore 条目） |
| 指针读取 | `prism/engine.py` 新增 `active_strategy_id() -> str` | 读指针；缺文件/损坏 → 回落 `"first_board_v04"`（fail-closed 到已知好策略） |
| 校验器 | `prism/engine.py` 新增 `validate_strategy_payload(payload) -> (ok, errors, strategy_dict)` | 全部合法性规则（见 §4）+ `load_strategy` 试载 |
| 列表增强 | 现有 `GET /api/strategies` 响应加 `"active": <当前默认id>` | 前端显示当前默认徽标 |

## 3. 编辑器输入与产出 JSON

前端提交 payload（扁平、人类可填）：
```json
{"name": "我的组合",
 "models": [{"id": "first_board", "name": "首板", "weight": 0.6,
             "factors": ["F1", "F8"], "weights": [1.0, 1.0]},
            {"id": "monster", "name": "妖股", "weight": 0.4,
             "factors": ["Y1", "Y2"], "weights": [1, 2]}],
 "gate_factors": ["N1"], "gate_threshold": 1,
 "candidate_min_model": 3,
 "sell": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05, "max_hold_days": 5}}
```

产出 `prism/strategies/{id}.json`（与 default/v04 同构，`load_strategy` 直接可载）：
```json
{"id": "<自动生成>", "name": "我的组合",
 "description": "网页编辑器生成 <ISO时间>",
 "market_gate": {"model": "node", "threshold": 1, "factors": ["N1"]},
 "scoring_models": [{"id": "first_board", "name": "首板", "weight": 0.6,
   "factors": ["F1", "F8"], "weights": [1.0, 1.0]},
  {"id": "monster", "name": "妖股", "weight": 0.4,
   "factors": ["Y1", "Y2"], "weights": [1, 2]}],
 "composite": {"mode": "top3_weighted", "weights": [0.6, 0.4], "cap": <自动>},
 "filters": {"candidate_min_model": 3, "environment_threshold": 1},
 "sell_rules": {"take_profit_pct": 0.08, "stop_loss_pct": 0.05, "max_hold_days": 5}}
```

**id 自动生成**：`custom_%Y%m%d_%H%M%S`（同秒冲突加 `_2` 序号）；用户只起显示名，不碰 id。
**cap 自动计算**：`cap = Σ(模型权重 × 该模型因子权重之和)`——复算校验：default = (0.6+0.25+0.15)×7 = 7.0 ✓；v04 = 1.0×9 = 9.0 ✓（两例全对，公式成立）。

## 4. 校验规则（validate_strategy_payload，全过才写盘）

1. `name` 非空且 ≤40 字符
2. `models` 非空（1-3 个）；每模型 `factors` 非空且**全部已注册**（`registry` 扫描后核对）；因子不得跨模型重复
3. 因子权重缺省 1.0、必须为正数；模型权重为正数（不强制和为 1——top3_weighted 内部按权重占比归一）
4. `gate_factors` ⊆ **node 类**因子（N1-N8），**可为空**（空 = 关闭市场环境门，任何环境都选股）；`gate_threshold` 为 ≥0 整数
5. `candidate_min_model` 为 ≥1 整数
6. `sell`：`0 < take_profit_pct ≤ 0.5`、`0 < stop_loss_pct ≤ 0.5`、`1 ≤ max_hold_days ≤ 30`
7. 终极校验：组装后的 dict 过 `load_strategy()` 试载（因子存在性/结构完整性由引擎把关）；试载失败 → 拒绝写盘并返回具体错误
8. 任何一条不过 → **零写入**，400 返回 `{"ok": false, "errors": [...]}`（每条错误指向具体字段，用户能看懂）

## 5. 默认指针机制（三处消费）

- **选股默认**：`/api/screen` 的 fallback `or "first_board_v04"` 改为 `or active_strategy_id()`
- **回测**：回测页下拉列表不变（显式选择）；`active` 字段仅用于前端徽标展示
- **模拟盘热重载**：`PaperAccount.strategy` getter 改为**动态读指针**——每次访问比对 `active_strategy_id()` 与已载策略的 id，不一致才重新 `load_strategy`（缓存单文件读取成本可忽略；守护 5 秒轮询零感知）→ 设为默认后**下一时点选股自动用新策略**，账本/持仓不动
- **激活端点**：校验目标策略文件存在（404 否则）→ 原子写指针（temp+replace，同账本纪律）→ 返回 `{"ok": true, "active": sid}`

## 6. 前端交互（策略页追加，纯增量）

- 策略列表每项：既有详情展示 + 新增两个动作——**「设为默认」**按钮与**「复制为底稿」**按钮；当前默认项显示绿色徽标
- 「➕ 新建策略」按钮 → 编辑面板（tab 内展开 section）：
  - 名称输入框
  - 模型栏（默认 1 栏，可加到 3 栏）：模型名 + 因子勾选（按 5 类分组 checkbox，title 悬浮显示因子说明，数据源=现有 `GET /api/factors`）+ 因子权重数字输入（默认 1）+ 模型权重输入
  - 门槛区：N 系因子勾选 + 门槛线数字 + 候选资质线数字
  - 卖出区：止盈%/止损%/持有天数 三个数字（百分比展示，提交前 ÷100）
  - 「保存」→ 成功提示 + 刷新策略列表 + 更新默认徽标；失败 → 逐条显示后端返回的 errors
- 复制底稿：把选中策略的结构填入编辑面板（id/name 清空，因子与数值保留）

## 7. 安全边界

1. 写盘仅限**新建** `prism/strategies/{新id}.json`；不覆盖任何已存在文件（default.json 快照保护天然成立）
2. 校验不过零写入；写盘用原子写（temp+replace）
3. 指针文件损坏/缺失 → 所有消费方回落 `first_board_v04`，绝不崩溃
4. 端点无删除策略能力（MVP 不做）
5. 编辑器改动不影响模拟盘账本（策略热重载只换评分规则，持仓与流水不变）
6. GUI 保持既有只读语义之外的新增写端点仅两个（create/activate），且都写受控目录

## 8. 披露限制（交付时向用户说明）

- 因子内部参数（阈值/窗口等）不可改——只控制"用不用/权重"
- 编辑器只管组合合法性，不保证组合有效性——大改后建议手动回测对比
- MVP 不支持原地编辑既有策略（新建+复制底稿；后续需要再加"编辑"模式）

## 9. 测试计划

- 校验器单测：合法 payload 全过；各非法路径（因子不存在/跨模型重复/gate 非 node/卖出越界/试载失败）逐条拒绝且零写入
- 端点测试：create 成功写文件且 JSON 可被 load_strategy 加载；activate 写指针 + 404 路径；`/api/strategies` 含 active 字段
- 指针测试：缺文件/损坏 → active_strategy_id 回落 first_board_v04
- 热重载测试：写指针改变 id → PaperAccount.strategy 下次访问换策略（账本不变）
- 回归：default.json 快照不变；现有 367 测试全绿
- GUI 冒烟：面板 DOM 结构 + create/activate 按钮契约

## 10. 风险与预案

| 风险 | 预案 |
|---|---|
| 用户配出恒 0 候选的组合（门槛过严/因子互斥） | 校验只保证合法不保证有效；交付时说明"建议先回测"；模拟盘空仓状态可辨识 |
| 指针与策略文件不同步（策略被手删） | active_strategy_id 读取后即时校验文件存在，不存在回落 first_board_v04 |
| 模拟盘热重载时策略 JSON 被写坏 | load_strategy 抛异常 → getter 保留旧策略并记日志（绝不因坏文件中断交易循环） |
| 用户把 default 设为默认 | 允许（切回老算法正是用户诉求之一） |
