# MEMORY.md — 项目记忆（agent 会话开头必读，干活后主动更新）

> 维护规则：agent 每完成一个里程碑、用户每拍板一个新决策，就更新本文件对应小节。
> 本文件记"状态与偏好"；工作流程规则在 CLAUDE.md/AGENTS.md；任务细节在 .superpowers/sdd/progress.md。

## 用户合作偏好（joesph）

- **回复极简、通俗中文**：用户常用 "ok"/"继续"，直给结论不铺陈
- **push 必须每次先问**；本地 commit 随意（不用请示）
- **不要一味迎合**：有分歧直说，诚实评估优于顺从
- **ponytail 约束延续**：最懒可行方案；每个子代理 dispatch 注入 ponytail 约束（阶梯：needs-to-exist→reuse→stdlib→one-line→minimal；绝不简化掉校验完整性/原子写/指针回落/default保护；`# ponytail:` 标记）
- **默认工作方式**：superpowers SDD（子代理实现 + 独立子代理两阶段审查 + 台账记录），TDD 红绿循环

## 项目现状（截至 2026-09-03）

**prism**：A股量化系统。选股引擎（36 因子 / 3 策略 / JSON 策略文件）+ 回测 + Flask 网页 GUI（5000 端口）+ 模拟盘守护。Windows + Python 3.12 + QMT miniQMT（xtquant：`C:\Users\28037\AppData\Local\Programs\Python\Python312\Lib\site-packages`）。

- **模拟盘**：100 万 paper trading；守护由**用户双击 `启动模拟盘.bat`** 启动（2026-09-04 起——09-04 会话故障曾带走守护两天半，教训：守护不寄生 agent 会话）；当前激活策略 = **full_factor_v1**（09-04 起主力；default 已删除——净值归因切换点 09-03，账本不清零）
- **排板成交模型**（09-04 修正）：两路径——钉板吃穿全队列成交 + **炸板吃穿初始队列成交**（真实打板主通道，09-03/04 九委托零成交的根因=旧条件结构性近不可满足）；撤单流水带 dvol_shares 事后归因；半残 tick（lastVolume=0）不建委托
- **全因子四层策略**（09-03）：36 因子（门控 N1-N8 / 首板 F1-F9 w0.60 / 妖股 Y1-Y8 w0.25 / 势能板块 M6·M7·S2-S6·SEC1-4·SEC6 w0.15）；M1-M5 与 S1/S5/S7 已删；策略库仅存 v04/v03/full_factor_v1
- **策略编辑器**（09-01 完成）：网页新建策略/设为默认/复制底稿；指针 `prism/strategies/.active.json` 热重载（选股+回测+模拟盘处处生效）
- **排板队列状态机**（09-03 完成，089ea9d）：买入排队制（成交=新增成交量穿越前方封单+本单且仍封板；开板撤单当日禁排；收盘作废；资金冻结；跌停卖出顺延）。423 测试全绿。**待下一交易日真实验收**（13:30 时点排队第一现场/T+1/hold_expire 对照）
- **F3 封单强度 ×100 修复**（09-03，ded9e1a）：bidVol/lastVolume 均为手（xtdata 官方示例 L1492 实证）；回测对比零影响（回测无实时盘口，F3 在回测恒 0——v04 九因子回测实际 8 个生效）；修复意义在实盘/模拟盘真实盘口
- **未推 GitHub**：3 commit（6a6a200 排板成交模型修正 / da9f288 环境界面+启动脚本 / f0e7d2c 文档）——push 前要问

## 环境备忘

- 测试：`$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-josesph\pt_btNN`（NN 递增，下一个 **123**）
- xtquant 直连探测：`from xtquant import xtdata; xtdata.connect()`（系统 python 即可）
- QMT 数据/守护可并发读；守护日志看 job_output

## 因子管理惯例（2026-09-03 起）

- **新增因子默认组合进 full_factor_v1**：按四层归位——环境/情绪类→market_gate（N 系）；首板确认类→first_board 模型（F 系）；妖股类→monster 模型（Y 系）；形态/板块类→momentum 模型（M/S/SEC 系）。加入策略 JSON 对应层 factors 数组并**重算 composite.cap**（Σ 模型权重×该模型因子数）。特殊要求（如仅供实验/仅供回测）才不入，需用户明说

## 关键决策史

- 2026-09-01：策略编辑器选**网页版**；**拒绝一键回测按钮**（"每次调整都要回测太麻烦"）；生效范围=选股+回测+模拟盘一处切换
- 2026-09-02：排板模拟选**方案 B 排队状态机**（vs 轻量过滤）；用户自切默认策略 default（编辑器首次真实使用）
- 2026-09-03：F3 修复+回测对比（用户拍板"修 F3 + 回测对比"）；记忆系统选**文件记忆分立**（CLAUDE.md=流程 / MEMORY.md=状态，不上 mem0 等向量方案）

## 观察项 / 遗留

- 排板真实验收（下一交易日）；守护断连重试需人工重启（60×10s）
- triage Minor 列表见 `.superpowers/sdd/progress.md`（编辑器 M1-M9、模拟盘 T2/M-d/M-f/M-i/M-j 等，均非阻塞）
- 资金流 15 板块重跑 `pt_collect_flow.py`（待东财限流解除）
