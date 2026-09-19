# 首板盘后深度拆解模块 实施计划（2026-09-18）

> 关联 spec：`docs/superpowers/specs/2026-09-18-first-board-review-design.md`
> 目标模块：`prism/first_board_review.py`
> 测试：`prism/tests/test_first_board_review.py`

---

## 批次 1：分析层核心（纯函数，零 IO）★ 先做

**为什么先做**：分析层是纯函数，不依赖任何数据源，TDD 最快闭环，也是整个模块的价值所在。

### 1.1 常量与数据结构
- `WEIGHTS = {"seal": 0.35, "sector": 0.25, "volume": 0.20, "fund": 0.15, "industry": 0.05}`
  - 注释标明：初值，可校准（沿用 F3 `BT_SEAL_RATIO_T` 做法）
- `BoardRecord`：用 plain dict（项目惯例，见 factor_check / bt_intraday，不引 dataclass 增负担）
  字段契约：
  ```
  code, name, seal_time, open_times, one_word, on_board_amt,
  float_mv, amount, turnover, vol_ratio, prev5_avg_amt,
  sector_name, sector_zt_count, sector_is_top, top_score,
  seal_amount,  # 封单金额，常为 None
  sources: {field: "auto"|"manual"|"unknown"}
  ```

### 1.2 五维评分函数（每个独立可测）
| 函数 | 入参 | 评分口径 |
|---|---|---|
| `score_seal(r)` | 首封时间/开板次数/一字板/板上额占比 | 越早封、越少开板、一字 = 越高。09:35 前 90+，10:00 后递减，开板一次 -15 |
| `score_sector(r)` | 板块内涨停家数/是否最强 | 家数分档：≥6 只 90+，3-5 只 70-85，2 只 55，1 只 30（跟风） |
| `score_volume(r)` | 量比/前5日均量对比 | 量比 1.5-3 最佳 80-95；<1 缩量 40；>5 过度放量 45 |
| `score_fund(r)` | 封单金额/板上量额 | 有封单金额 → 按占流通市值比；无 → 用板上量额代理并**降权** |
| `score_industry(r)` | 板块阶段/top_score | 有则用，无则给中性 60 |

### 1.3 汇总
- `analyze(record) -> dict`：五维 = {score, evidence: [str], available: bool}
- `total_score(dims) -> (int, str)`：
  - **缺失维权重从分母剔除，其余归一**（核心，防 0 分冒充）
  - 置信度：≥75 高 / 55-75 中 / 35-55 低 / 板块共振 <40 → 跟风脉冲

### 1.4 测试（先写）
- `test_score_seal_*`：早封高分 / 一字板满分 / 多次开板扣分
- `test_score_sector_*`：家数分档边界
- `test_analyze_missing_dim`：**封单缺失 → 权重归一化正确，且 available=False，score 不为 0**
- `test_total_score_confidence`：四档标签边界
- `test_analyze_no_division_by_zero`：全缺失时不出异常

---

## 批次 2：采集层（接线现有数据源）

### 2.1 `collect(day, deps=None)` 
`deps` 注入式（便于测试造假）：
```
deps = {"zt_feed": zt_history.qmt_zt_feed, "intraday": bt_intraday.features_for,
        "sector_map": ..., "kline": ..., "snapshot": ...}
```
流程：
1. `zt_feed(day)` → 涨停池
2. 筛 `boards == 1` → 首板清单
3. 逐只填字段，每个字段写 `sources[field]`
4. 板块内涨停家数：用涨停池 + sector_map 自算

### 2.2 缺失处理（不造假）
- 1m 特征缺失 → `seal_time=None, open_times=None`，sources 标 unknown
- 封单金额 → 永远尝试 snapshot，无 → None

### 2.3 测试
- `test_collect_filters_first_board`：boards==2 的必须被剔除
- `test_collect_sector_count`：同板块计数正确
- `test_collect_missing_intraday`：特征缺失不炸，字段标 unknown
- 全部用假 deps，**不触网**

---

## 批次 3：覆盖层 + 报告 + CLI

### 3.1 `apply_manual(records, manual)`
- manual 非必需；手填优先，source 标 manual

### 3.2 `render_report(day, records) -> str`
- 每只一段：总评 + 五维表 + 拆解 + 存疑
- 缺失字段明写"未知（原因）"

### 3.3 CLI `__main__`
```
--date YYYYMMDD  --report  --manual PATH
```
- JSON → `runtime/state/first_board_review_YYYYMMDD.json`
- MD → `docs/reports/首板拆解_YYYYMMDD.md`

### 3.4 测试
- `test_apply_manual_override`
- `test_render_report_contains_fields`
- `test_render_report_marks_unknown`

---

## 验证清单（每批完成必跑）
- [ ] `python -m pytest prism/tests/test_first_board_review.py -q`
- [ ] 完成后跑全量 `prism/tests` 确认无回归
- [ ] 报告人工目视一份（构造数据）

## 明确不做
- 不触网（测试）、不接实时、不进因子打分、不推测缺失值
