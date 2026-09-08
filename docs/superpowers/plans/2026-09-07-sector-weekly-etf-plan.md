# 板块周度跟踪升维 实现计划（2026-09-07）

Spec: docs/superpowers/specs/2026-09-07-sector-weekly-etf-design.md
基线: 486 绿（pt_bt167 起用）；基线 commit fb03658。

## Global Constraints

- **纯观察面板**：不碰任何因子打分/模拟盘买入/仓位代码路径（用户拍板红线）
- ponytail 阶梯：needs-to-exist→reuse→stdlib→one-line→minimal；绝不简化掉 fail-open/测试确定性/校验完整性
- sector_stage 保持纯函数（新数据全部可选参注入）；测试离线（monkeypatch，禁 xtdata/网络/真实 .pkl）
- 本地 commit 随意，**绝不 push**

## Task W1: 数据计算层（etf_map + ETF 行情 + 60日新高 + sector_table 扩展）

- `prism/sector_etf_map.py`：SECTOR_ETF_MAP 31 行业，WebSearch 补全候选 + **逐码 QMT 验证**（存在性+名称一致，验证脚本运行记录写报告）；无专属 ETF 行业留空 `{}`；锚点口径 docstring
- `prism/market_data.py`：`fetch_etf_quotes(codes)`（读日K，本地无数据**或末根早于今日**则批量下载[每码每日 memo]+单码失败跳过；09-08 终审 I-1 修正触发条件）
- `prism/sector_stage.py`：`new_high_counts()` 纯计算 + `POS_CAP` 常量 + `sector_table()` 可选参扩展（new_high/etf/pos_cap/week_rank 四字段，缺省 None 输出 None）
- TDD；测试全离线（构造 fake 缓存/映射/quotes）
- 验证: `PYTHONIOENCODING=utf-8 python -m pytest prism/tests/test_sector_stage.py prism/tests/test_sector_etf_map.py prism/tests/test_market_data.py -q --import-mode=importlib --basetemp=D:/cc-joesph/pt_bt167`（子集命令；**基线 486 为全量口径**，子集数不含其他文件，最终以全量回归为准）

## Task W2: API + GUI（组装透出 + 板块观察 tab 新列）

- `prism_web/app.py`：api_sector_stage 组装（sector_map 取缓存 map 段、zt 缓存加载+按 mtime memo、fetch_etf_quotes 注入），fail-open 结构保持
- `prism_web` 前端：板块观察 tab 新列（周排名/ETF锚点/新高家数/建议上限%），escHtml 契约，头部"纸面参考未接入"注明，默认按 week_rank 升序
- TDD（API 测试 client + DOM 测试）；全量回归

## Task W3: 收尾

- 真数据冒烟：31 行业新列齐/排行榜核对/QMT 验证记录；15:00 后顺手 `--build-sectors` 追平板块K线（乐咕延迟，非本批次依赖但顺手）
- MEMORY/台账/skill 实战案例更新；plan 勾选

## 验收标准

- 全量测试绿；真数据冒烟新列齐；无打分/买卖链路改动（diff 审查确认）
