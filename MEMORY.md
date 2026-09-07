# MEMORY.md — 项目记忆（agent 会话开头必读，干活后主动更新）

> 维护规则：agent 每完成一个里程碑、用户每拍板一个新决策，就更新本文件对应小节。
> 本文件记"状态与偏好"；工作流程规则在 CLAUDE.md/AGENTS.md；任务细节在 .superpowers/sdd/progress.md。

## 用户合作偏好（joesph）

- **回复极简、通俗中文**：用户常用 "ok"/"继续"，直给结论不铺陈
- **push 必须每次先问**；本地 commit 随意（不用请示）
- **不要一味迎合**：有分歧直说，诚实评估优于顺从
- **ponytail 约束延续**：最懒可行方案；每个子代理 dispatch 注入 ponytail 约束（阶梯：needs-to-exist→reuse→stdlib→one-line→minimal；绝不简化掉校验完整性/原子写/指针回落/default保护；`# ponytail:` 标记）
- **默认工作方式**：superpowers SDD（子代理实现 + 独立子代理两阶段审查 + 台账记录），TDD 红绿循环

## 项目现状（截至 2026-09-07）

**prism**：A股量化系统。选股引擎（36 因子 / 3 策略 / JSON 策略文件）+ 回测 + Flask 网页 GUI（5000 端口）+ 模拟盘守护。Windows + Python 3.12 + QMT miniQMT（xtquant：`C:\Users\28037\AppData\Local\Programs\Python\Python312\Lib\site-packages`）+ **通达信 pytdx 1.72（同目录，09-05 装）**。

- **板块感知层**（09-06/07，commit 3b368c0..361956c，**观察模式不进打分**——用户拍板）：`prism/sector_stage.py` 纯计算（孕育期三信号：近3日≥2日跑赢上证/成交额占比MA5>MA20/站上5日线且近5日阳≥3，未启动 r5<8%；五阶段判定 退潮>高潮>主升>启动>孕育>休整；资金惯性 streak=每日净流入前3连续上榜，≥5日=系统性增配）+ market_data 新段 `flow_rank`（**东财 BK 细分行业口径自洽，勿回填 SEC3 申万体系**；CLIST f62 当日快照**前向累积**幂等、空快照不落盘）+ `benchmark`（上证日K全量替换，09-07 已落 165 日）+ CLI `--build-flow-rank`/`--build-benchmark`（**容错：一段被封不连累另一段，点名失败非零退出**）+ GUI `/api/sector_stage` +「板块观察」tab（**需重启 5000 Flask 生效**）。**升级对话 triage 沉淀为 skill `.claude/skills/prism-upgrade-triage/SKILL.md`**（三问门：数据层可办到/取数容易/实测有效 → 保留/降级/去掉 + 用户拍板；已过 RED/GREEN 子代理测试）

- **通达信数据层**（09-05，commit 2f46e51）：`prism/tdx_source.py` —— pytdx 直连，**补充源定位**（QMT 优先，取不到时降级顶上；板块成分股/证券列表仍走 QMT）。服务器白名单 4 台（123.125.108.14 是残废已剔除）；**板块/指数 K 线必须走 get_index_bars**（get_security_bars 返回乱码内存）；连接会被巨量请求污染 → 每调用前健康检查+换机重连；`tdx_source.set_enabled(False)` 全局开关（测试 conftest 已默认禁用）。自检：`python -m prism.tdx_source`。pytdx 无美股/宏观——NDX/US10Y/VIX 仍走 akshare/FRED
- **因子断链修复**（09-05 同 commit）：①F6 恒 0 根因=指数K线从未回填个股 ctx **且** index_kline 字段被 N1(涨停指数880368,≥6根)与 F6(上证指数,≥21根)语义冲突共用 → 新增独立字段 `sh_index_kline`（build_market_context 抓 QMT 上证K线，失败降级通达信）②M6/M7 实盘恒 0=K线拉 250 根但因子要 251 根算 MA250 → 改 260 ③回测量能：kline_feed 升级 OHLCV 契约（向后兼容三档：2/3/6 元组按长度识别），东财/腾讯源给真实成交量，mkt 注入改默认开启（`--no-market-data` 关闭）
- **factor_check 升级**（09-05）：除"空上下文不崩"外，新增 5 个数据齐全合成场景（首板封板/放量突破/均线多头/市场门控/妖股）跑命中率，全 0 的标 ZERO-HIT。**36 因子已全部场景命中**——证明恒 0 全是数据供给问题非因子逻辑。坑：因子里 `ctx.get(x) or y` 遇 DataFrame 真值测试抛 ValueError 被 except 吞成恒 0（factor_check 实际抓到过一次这种回归）
- **模拟盘**：100 万 paper trading；守护由**用户双击桌面 `PRISM.bat`** 启动（2026-09-04 起——09-04 会话故障曾带走守护两天半，教训：守护不寄生 agent 会话；PRISM.bat=守护+网页 GUI 双防重复启动；仓库内 `启动模拟盘.bat` 保留为守护单启动）；当前激活策略 = **full_factor_v1**（09-04 起主力；default 已删除——净值归因切换点 09-03，账本不清零）
- **交易节奏**（09-04 起，spec 2026-09-04-open-top5）：15:05 收盘选股（门禁 3/8 → 三层**等权**打分 → 前 5 存 planned_buys）→ 次日 09:26-09:35 开盘买入窗口（每只净值 **15%**；开盘价直接买；一字板/开盘即板 → 按真实涨停价转排队；跌停开盘跳过；非当日行情快照 fail-closed 跳过）→ 盘中每 5 秒排队检查+卖出（止盈 **15%**/止损 5%/持有 5 日，跌停顺延）→ 15:00 结算。**盘中 10:00/13:30 排队时点已移除**，打板机制仅作一字板替补
- **排板成交模型**（09-04 修正）：两路径——钉板吃穿全队列成交 + **炸板吃穿初始队列成交**（真实打板主通道，09-03/04 九委托零成交的根因=旧条件结构性近不可满足）；撤单流水带 dvol_shares 事后归因；半残 tick（lastVolume=0）不建委托
- **全因子四层策略**（09-03）：36 因子（门控 N1-N8 / 首板 F1-F9 w0.60 / 妖股 Y1-Y8 w0.25 / 势能板块 M6·M7·S2-S6·SEC1-4·SEC6 w0.15）；M1-M5 与 S1/S5/S7 已删；策略库仅存 v04/v03/full_factor_v1
- **策略编辑器**（09-01 完成）：网页新建策略/设为默认/复制底稿；指针 `prism/strategies/.active.json` 热重载（选股+回测+模拟盘处处生效）
- **排板队列状态机**（09-03 完成，089ea9d）：买入排队制（成交=新增成交量穿越前方封单+本单且仍封板；开板撤单当日禁排；收盘作废；资金冻结；跌停卖出顺延）。423 测试全绿。**待下一交易日真实验收**（13:30 时点排队第一现场/T+1/hold_expire 对照）

- **F3 封单强度 ×100 修复**（09-03，ded9e1a）：bidVol/lastVolume 均为手（xtdata 官方示例 L1492 实证）；回测对比零影响（回测无实时盘口，F3 在回测恒 0——v04 九因子回测实际 8 个生效）；修复意义在实盘/模拟盘真实盘口
- **未推 GitHub**：无（2026-09-04 已全部推送至 340160d）——下次 push 前仍要问

## 环境备忘

- 测试：`$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_btNN`（NN 递增，下一个 **164**；当前基线 **468 绿**——09-07 板块感知层后全量复跑；历史环境失败 test_automation_pause_roundtrip 近两轮未复现）。09-05 起 conftest 全局禁用通达信取数（离线确定性）；test_market_data 的 _ws_tmp 用唯一临时目录（固定目录+沙箱清理失败=跨轮缓存污染假失败）
- xtquant 直连探测：`from xtquant import xtdata; xtdata.connect()`（系统 python 即可）
- tdx 自检：`python -m prism.tdx_source`（6 项：连接/个股日K/大盘指数/板块指数/快照/流通股本）
- QMT 数据/守护可并发读；守护日志看 job_output

## 因子管理惯例（2026-09-03 起）

- **新增因子默认组合进 full_factor_v1**：按四层归位——环境/情绪类→market_gate（N 系）；首板确认类→first_board 模型（F 系）；妖股类→monster 模型（Y 系）；形态/板块类→momentum 模型（M/S/SEC 系）。加入策略 JSON 对应层 factors 数组并**重算 composite.cap**（Σ 模型权重×该模型因子数）。特殊要求（如仅供实验/仅供回测）才不入，需用户明说

## 关键决策史

- 2026-09-06：**升级对话 triage**（DeepSeek 四模块建议，用户拍板）：事件日历**去掉**（无结构化源/不可回测/Y6 手填已有入口）；资金惯性**换源**（fflow 历史封死 → CLIST f62 当日快照前向累积，实测通；东财 BK 口径自洽不回填 SEC3）；孕育期**保留但降级**（个股底部放量→板块级量价，全市场K线缓存停更）；**全部观察模式不进因子打分**（先积累样本）。方法论沉淀 = prism-upgrade-triage skill
- 2026-09-05：通达信接入形态选**pytdx 原生直连进数据层**（用户要"不开会话也能用"，MCP 只能在 agent 会话里调被排除）；定位**补充源**非替换（QMT 主链路不动，风险最低）；**全面修复**因子断链（F6/M6M7/回测量能/回测mkt/体检）
- 2026-09-05：**git 对象库曾部分损坏**（约 09-05 前发生：340160d 及更早对象丢失、reflog 大量坏条目、refs/remotes/origin/worktree-strategy-web 指向坏对象，fetch 协商被卡死）。修复路径：①`git ls-files | git hash-object -w` 重写工作区内容补回同 sha 的丢失 blob ②坏 remote 引用 `git update-ref -d` ③清 reflog ④clone --bare 救援副本**物理拷贝 pack/loose 对象**进 .git/objects（fetch 协商卡死时的终极手段）。教训：对象库损坏先 fsck 定位，fetch 不通就 clone 拷对象，本地未改文件 hash-object -w 能无损补缺
- 2026-09-01：策略编辑器选**网页版**；**拒绝一键回测按钮**（"每次调整都要回测太麻烦"）；生效范围=选股+回测+模拟盘一处切换
- 2026-09-02：排板模拟选**方案 B 排队状态机**（vs 轻量过滤）；用户自切默认策略 default（编辑器首次真实使用）
- 2026-09-03：F3 修复+回测对比（用户拍板"修 F3 + 回测对比"）；记忆系统选**文件记忆分立**（CLAUDE.md=流程 / MEMORY.md=状态，不上 mem0 等向量方案）

## 观察项 / 遗留

- `D:\cc-joesph\.tdx_probe\`（09-05 探测用 venv，6397 文件）沙箱批量删除需确认，留待用户自行删
- 排板真实验收（下一交易日）；守护断连重试需人工重启（60×10s）
- triage Minor 列表见 `.superpowers/sdd/progress.md`（编辑器 M1-M9、模拟盘 T2/M-d/M-f/M-i/M-j 等，均非阻塞）
- **因子数据遗留**（09-05 审计；09-06 大修，剩 2 项）：~~①fundamental 未来函数~~ ✅ 已修（`_ref(asof)` 全路由、窗口双向 [cutoff, ref]、缓存按 asof 分日；Backtester 可注 fund_feed，Y5/Y2 快照类回测剔除，commit 3dba8f6 已推送）~~③sector_map 6 行业零覆盖~~ ✅ 已补齐（5220 只，F8 煤炭/石油石化映射恢复）②**SEC3 资金流仍缺 15/31 板块**（东财 fflow 端点对 801120/801720/801890/801950 等持续封禁+部分 EMPTY，09-05/06 多轮 30s 间隔重试 0 成功；SEC3 fail-open 得 0；恢复手段：`python -m prism.market_data --build-sectors`（增量只补缺的）或等东财解封；勿用东财 BK 码回填——口径不同会污染申万体系）④zt_history_index 缓存停更 08-31，无刷新调度（通达信历史涨停池可按日期查询，可作回补源：tdx_screener "2026年9月3日涨停" 实测可用）⑤Y1/Y8 float_mv 依赖实时 tick，可用 tdx get_finance_info liutongguben 兜底（tdx_source.float_shares 已备好未接线）
- **市场数据缓存基线**（09-06）：板块K线 31 行业到 09-02（申万源乐咕自身延迟，`--build-sectors --source sw` 下一交易日收盘后追平）；global NDX/SPX/DJIA/UDI 09-04（新浪源）、US10Y/VIX 09-03（FRED）；fundamental_cache.json 按 asof 分日后旧条目仍兼容（key 含日期段）
- **东财 push2(clist) 09-07 起封禁中**（RemoteDisconnected，裸 requests 同样失败；push2his kline 主机存活——封禁按主机/天漂移）。后果：资金惯性 flow_rank 暂 0 天，**等解封后每日盘后跑 `--build-flow-rank` 逐日累积**（≥5 日才亮"系统性增配"）；benchmark 走 push2his 不受影响。惯性表口径=东财 BK 细分行业（~90 个），与申万 31 一级不同体系，面板已注明
