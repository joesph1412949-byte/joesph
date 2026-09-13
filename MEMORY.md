# MEMORY.md — 项目记忆（agent 会话开头必读，干活后主动更新）

> 维护规则：agent 每完成一个里程碑、用户每拍板一个新决策，就更新本文件对应小节。
> 本文件记"状态与偏好"；工作流程规则在 CLAUDE.md/AGENTS.md；任务细节在 .superpowers/sdd/progress.md。

## 用户合作偏好（joesph）

- **回复极简、通俗中文**：用户常用 "ok"/"继续"，直给结论不铺陈
- **push 必须每次先问**；本地 commit 随意（不用请示）
- **不要一味迎合**：有分歧直说，诚实评估优于顺从
- **ponytail 约束延续**：最懒可行方案；每个子代理 dispatch 注入 ponytail 约束（阶梯：needs-to-exist→reuse→stdlib→one-line→minimal；绝不简化掉校验完整性/原子写/指针回落/default保护；`# ponytail:` 标记）
- **默认工作方式**：superpowers SDD（子代理实现 + 独立子代理两阶段审查 + 台账记录），TDD 红绿循环

## 项目现状（截至 2026-09-13）

**prism**：A股量化系统。选股引擎（36 因子 / 3 策略 / JSON 策略文件）+ 回测 + Flask 网页 GUI（5000 端口）+ 模拟盘守护。Windows + Python 3.12 + QMT miniQMT（xtquant：`C:\Users\28037\AppData\Local\Programs\Python\Python312\Lib\site-packages`）+ **通达信 pytdx 1.72（同目录，09-05 装）**。

- **板块周度跟踪**（09-07/08，commit d0adcbc..68b3195，**纯观察面板**）：`prism/sector_etf_map.py`（31 行业 ETF 锚点映射，24 锚点+7 留空，QMT 逐码验证）+ `sector_stage.new_high_counts()`（60 日新高家数，zt 缓存直算）+ `sector_table()` 扩展四列（week_rank/etf/new_high/pos_cap 纸面上限）+ app.py 组装（**801 前置过滤=纯申万宇宙**[占比分母与旧面板有出入，已记录]、双重翻译 6位码→801→行业名、memo 双 mtime）+ 板块观察 tab 新四列（**建议上限=纸面参考未接入交易**）。周排名/新高/占比拥挤等 triage：C3 机构共识去掉、C1 惯性/C2 拥挤查重已有、状态调制暂缓（红线）。ETF 行情**按日刷新**（本地末根<今日才批量下载，memo 失败也占当日槽；09-08 终审 I-1 修冻结病）。`build_sector_cache` 尾部过期重采修复（原"已有即跳过"日期永久冻结 09-02；变短守卫防静默截断）。全量 **518 绿**
- **板块感知层**（09-06/07，commit 3b368c0..361956c，**观察模式不进打分**——用户拍板）：`prism/sector_stage.py` 纯计算（孕育期三信号：近3日≥2日跑赢上证/成交额占比MA5>MA20/站上5日线且近5日阳≥3，未启动 r5<8%；五阶段判定 退潮>高潮>主升>启动>孕育>休整；资金惯性 streak=每日净流入前3连续上榜，≥5日=系统性增配）+ market_data 新段 `flow_rank`（**东财 BK 细分行业口径自洽，勿回填 SEC3 申万体系**；CLIST f62 当日快照**前向累积**幂等、空快照不落盘）+ `benchmark`（上证日K全量替换，09-07 已落 165 日）+ CLI `--build-flow-rank`/`--build-benchmark`（**容错：一段被封不连累另一段，点名失败非零退出**）+ GUI `/api/sector_stage` +「板块观察」tab（**需重启 5000 Flask 生效**）。**升级对话 triage 沉淀为 skill `.claude/skills/prism-upgrade-triage/SKILL.md`**（三问门：数据层可办到/取数容易/实测有效 → 保留/降级/去掉 + 用户拍板；已过 RED/GREEN 子代理测试）

- **全项目体检 + miniQMT 实盘就绪评估**（09-13，报告 `项目体检报告_未完成任务与miniQMT实盘就绪_20260913.md`）：①修**测试隔离真缺陷**——`test_backtest`/`test_registry` 的 autouse fixture `reg.reset()` 有可能先于 conftest 快照执行，使还原快照取到"残缺状态"(1~2 个测试因子)，把真实 36 因子库从整个 session 抹掉 → `test_sector_factors` 整片 `UnknownFactorError`（沙箱下稳定 21 例复现）。修法双层：conftest 加会话级干净基线 + `SEC1` 哨兵自愈；`test_sector_factors` 加 autouse fixture 按需 force 重扫（自足化，根治）。②修 **`registry._clear_factor_bytecode` 健壮性**：只吞 `OSError`，而沙箱 safe-delete 抛 `SystemExit`(BaseException) 会穿透 → 缓存清理失败中断因子注册；改捕获 `BaseException`（KeyboardInterrupt 除外）。③新增 **`qmt_live_check.py`**：实盘只读就绪自检（接口/数据格式/配置/运行 四类 32 项，只 query + 方法存在性，绝不下单；`--quiet` 只看问题；退出码 0/1/2）。④数据缓存停更已刷新：板块K线 09-04→**09-11**、NDX/SPX/DJIA 09-04→**09-11**（东财封→新浪源）、US10Y/VIX 09-03→**09-10**（FRED）。⑤**miniQMT 实测就绪**（只读探测）：`connect()=0`、账号 `88869979`、总资产 274,783.61、持仓 1 只；`order_stock/order_stock_async/cancel_order_stock*` **均在**——**外部 Python 可直接下单，无需把桥脚本粘进 QMT**（现有代码只用了它的查询能力）。
- **通达信数据层**（09-05，commit 2f46e51）：`prism/tdx_source.py` —— pytdx 直连，**补充源定位**（QMT 优先，取不到时降级顶上；板块成分股/证券列表仍走 QMT）。服务器白名单 4 台（123.125.108.14 是残废已剔除）；**板块/指数 K 线必须走 get_index_bars**（get_security_bars 返回乱码内存）；连接会被巨量请求污染 → 每调用前健康检查+换机重连；`tdx_source.set_enabled(False)` 全局开关（测试 conftest 已默认禁用）。自检：`python -m prism.tdx_source`。pytdx 无美股/宏观——NDX/US10Y/VIX 仍走 akshare/FRED
- **数据层遗留修复批次**（09-07，ac5ba0b..2007be4）：④zt_history 刷新链路——停更 08-31 根因=`build_cache` 增量只补新股、存量 K 线永不更新（重跑 --build 也没用）；新增 `refresh_cache`（存量尾部续传 90 天窗+新股只进尾部+落盘全改原子写 tmp+os.replace）+ CLI `--refresh` + `prev_day_pool` 陈旧保护（>10 自然日 fail-closed 空+warning）+ 守护自动刷新钩子（启动连接成功后+收盘选股后各一次，6h 节流，`zt_refresh_fn` 可注入）。已实跑回补：索引 399→404 天（09-01..09-07 齐；09-04 池 41 只），今日 15:05 选股 F9 首次拿到真"昨日池"。**注意 09-07 条目是 13:13 盘中临时值，收盘后刷新一次才作数**。⑤Y1/Y8 float_mv 兜底——`data.py` QMT FloatVolume 缺失/0 时降级 `tdx_source.float_shares`（**liutongguben 单位=股已实测**，茅台 12.5 亿股对上）；float_mv 统一=股本×现价均正才算否则 None；fund_feed 吃到兜底值；QMT 正常时零 tdx 调用（有否定用例）。测试 470→486（+16）
- **因子断链修复**（09-05 同 commit）：①F6 恒 0 根因=指数K线从未回填个股 ctx **且** index_kline 字段被 N1(涨停指数880368,≥6根)与 F6(上证指数,≥21根)语义冲突共用 → 新增独立字段 `sh_index_kline`（build_market_context 抓 QMT 上证K线，失败降级通达信）②M6/M7 实盘恒 0=K线拉 250 根但因子要 251 根算 MA250 → 改 260 ③回测量能：kline_feed 升级 OHLCV 契约（向后兼容三档：2/3/6 元组按长度识别），东财/腾讯源给真实成交量，mkt 注入改默认开启（`--no-market-data` 关闭）
- **factor_check 升级**（09-05）：除"空上下文不崩"外，新增 5 个数据齐全合成场景（首板封板/放量突破/均线多头/市场门控/妖股）跑命中率，全 0 的标 ZERO-HIT。**36 因子已全部场景命中**——证明恒 0 全是数据供给问题非因子逻辑。坑：因子里 `ctx.get(x) or y` 遇 DataFrame 真值测试抛 ValueError 被 except 吞成恒 0（factor_check 实际抓到过一次这种回归）
- **模拟盘**：100 万 paper trading；守护由**用户双击桌面 `PRISM.bat`** 启动（2026-09-04 起——09-04 会话故障曾带走守护两天半，教训：守护不寄生 agent 会话；PRISM.bat=守护+网页 GUI 双防重复启动；仓库内 `启动模拟盘.bat` 保留为守护单启动）；当前激活策略 = **full_factor_v1**（09-04 起主力；default 已删除——净值归因切换点 09-03，账本不清零）
- **交易节奏**（09-04 起，spec 2026-09-04-open-top5）：15:05 收盘选股（门禁 3/8 → 三层**等权**打分 → 前 5 存 planned_buys）→ 次日 09:26-09:35 开盘买入窗口（每只净值 **15%**；开盘价直接买；一字板/开盘即板 → 按真实涨停价转排队；跌停开盘跳过；非当日行情快照 fail-closed 跳过）→ 盘中每 5 秒排队检查+卖出（止盈 **15%**/止损 5%/持有 5 日，跌停顺延）→ 15:00 结算。**盘中 10:00/13:30 排队时点已移除**，打板机制仅作一字板替补
- **排板成交模型**（09-04 修正）：两路径——钉板吃穿全队列成交 + **炸板吃穿初始队列成交**（真实打板主通道，09-03/04 九委托零成交的根因=旧条件结构性近不可满足）；撤单流水带 dvol_shares 事后归因；半残 tick（lastVolume=0）不建委托
- **全因子四层策略**（09-03）：36 因子（门控 N1-N8 / 首板 F1-F9 w0.60 / 妖股 Y1-Y8 w0.25 / 势能板块 M6·M7·S2-S6·SEC1-4·SEC6 w0.15）；M1-M5 与 S1/S5/S7 已删；策略库仅存 v04/v03/full_factor_v1
- **策略编辑器**（09-01 完成）：网页新建策略/设为默认/复制底稿；指针 `prism/strategies/.active.json` 热重载（选股+回测+模拟盘处处生效）
- **排板队列状态机**（09-03 完成，089ea9d）：买入排队制（成交=新增成交量穿越前方封单+本单且仍封板；开板撤单当日禁排；收盘作废；资金冻结；跌停卖出顺延）。423 测试全绿。**待下一交易日真实验收**（13:30 时点排队第一现场/T+1/hold_expire 对照）

- **F3 封单强度 ×100 修复**（09-03，ded9e1a）：bidVol/lastVolume 均为手（xtdata 官方示例 L1492 实证）；回测对比零影响（回测无实时盘口，F3 在回测恒 0——v04 九因子回测实际 8 个生效）；修复意义在实盘/模拟盘真实盘口
- **未推 GitHub**：**6 个 commit 未推**（Tailscale 私享分享批次 `d20c8dd..4737713`，09-09 产生）+ 09-13 修复批次待提交——下次 push 前仍要问
  - 09-13 已重启守护（PRISM.bat，15:12）：paper_daemon + prism_web 在跑；zt 缓存 15:19 自动刷新
  - **模拟盘 09-08 午后~09-13 空窗**：账本最后选股 `2026-09-07T15:05`，守护 09-08 午后停摆 → 09-08/09/10/11 四个交易日无选股无成交（非代码问题，进程没在跑）；现金仍 1,000,000、无持仓（09-03/04 九委托零成交的已知结果）
  - **发现重复 web 进程**：`prism_web\app.py` 两个实例（14248 占 5000 / 16604 冗余），启动器只查端口监听，双开仍可能漏网
- **守护重启待办**：现跑的守护是 09-07 12:28 启动的旧代码，**无自动刷新钩子**；且 app.py/静态资源 09-07/08 有改动（板块观察 tab 四新列）——**重启 PRISM.bat 一次**同时激活两件事；启动即补当日真实收盘涨停池（旧盘中临时值由陈旧保护兜底，但重启更干净）

## 环境备忘

- 测试：`$env:PYTHONIOENCODING='utf-8'; python -m pytest prism/tests prism_web/tests strategy_web/tests -q --import-mode=importlib --basetemp=D:\cc-joesph\pt_btNN`（NN 递增，下一个 **192**；当前基线 **681 绿**（三套件）/ prism+prism_web 两套件 518 绿——09-13 体检后）。**注意沙箱**：后台运行的命令在沙箱内跑，会因①safe-delete 批量删除守卫（teardown 清理 700+ 临时文件）产生假 ERROR + SystemExit；②网络被拦（market_data 相关断言失败）→ 表现为 21~22 failed/15~177 errors，**不是代码问题**；脱沙箱（escalation）即 681 全绿。诊断时优先用非后台调用。
- **诊断技巧**：pytest 全量跑出现"整片同类失败"时，用 `@pytest.fixture`/hook 打印状态边界（如注册表 `len(reg.FACTORS)`）比逐个二分快得多；autouse fixture 实例化顺序可能导致"取快照晚于污染"这类隐蔽 bug
- xtquant 直连探测：`from xtquant import xtdata; xtdata.connect()`（系统 python 即可）
- tdx 自检：`python -m prism.tdx_source`（6 项：连接/个股日K/大盘指数/板块指数/快照/流通股本）
- QMT 数据/守护可并发读；守护日志看 job_output
- **Tailscale 私享分享**（09-09）：朋友经 tailnet **全功能**访问 5000（装客户端+邀请即可；用户拍板不上护栏不上 ACL——"我这边什么样朋友见什么样"；远程只读护栏 26b648f 已撤销）；你关机=朋友不可用（已接受）；免费档 3 用户/100 设备
- **DSH × OpenCode Go（09-08）**：opencode.ai/zen/go 网关 09-05 起强制 `x-opencode-session` 头，缺失返 400 MissingSessionID（"Console Go"）；DSH 官方已知问题（discussion 5495 未修）。本机已修：`C:\Users\28037\.dsh\settings.yaml` → `llm-pi-ai.providers` 6 个 opencode 供应商补 `headers: { 'x-opencode-session': 'dsh-opencode-go-joesph' }`（备份 .bak-20260908；llm-pi-ai 适配器逐请求读配置，通常免重启）。**09-08 当天实测生效**（下一条消息即不再 400）。若 aux 路径仍 400 需动 deepseek-harness 仓库 llm-pi-ai 代码（重建 profile）

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
- **因子数据遗留**（09-05 审计；09-06 大修、09-07 数据层批次后**剩 1 项**）：~~①fundamental 未来函数~~ ✅ 已修（`_ref(asof)` 全路由、窗口双向 [cutoff, ref]、缓存按 asof 分日；Backtester 可注 fund_feed，Y5/Y2 快照类回测剔除，commit 3dba8f6 已推送）~~③sector_map 6 行业零覆盖~~ ✅ 已补齐（5220 只，F8 煤炭/石油石化映射恢复）~~④zt_history_index 停更无调度~~ ✅ 已修+已回补（09-07 刷新链路：refresh_cache 尾部续传+--refresh+陈旧保护+守护钩子；停更根因=增量只补新股）~~⑤Y1/Y8 float_mv 靠实时 tick~~ ✅ 已接线（tdx float_shares 兜底，单位=股实测）②**SEC3 资金流仍缺 15/31 板块**（东财 fflow 端点对 801120/801720/801890/801950 等持续封禁+部分 EMPTY，09-05/06 多轮 30s 间隔重试 0 成功；SEC3 fail-open 得 0；恢复手段：`python -m prism.market_data --build-sectors`（增量只补缺的）或等东财解封；勿用东财 BK 码回填——口径不同会污染申万体系）
- **市场数据缓存基线**（09-06）：板块K线 31 行业到 09-02（申万源乐咕自身延迟，`--build-sectors --source sw` 下一交易日收盘后追平）；global NDX/SPX/DJIA/UDI 09-04（新浪源）、US10Y/VIX 09-03（FRED）；fundamental_cache.json 按 asof 分日后旧条目仍兼容（key 含日期段）
- **东财 push2(clist) 09-07 起封禁中**（RemoteDisconnected，裸 requests 同样失败）。**09-13 复查：`push2his` 也已封**（全球指数/上证基准采集全部 RemoteDisconnected；而 09-06 时它还活着）→ 后果：benchmark 停更 09-07、flow_rank 仍 0 天、flow 仍 16/31、UDI 停 09-04（新浪/FRED 均无美元指数序列）。**benchmark 的可行替代=复用已建好的 `tdx_source`（通达信 get_index_bars 取上证指数）**，待接线
- **miniQMT 实盘接入缺口**（09-13 评估，详见体检报告 §3.2）：①**新引擎→实盘出口缺失**（`trader.run_daily` 无人调度；`paper_daemon` 明确不写 QMT_SIGNALS；网页只接暂停开关；唯一入口是 v04 时代 `strategy_close_pick.py`，其 `close_pick_state.json` 停在 08-11）②仓位=100 股占位非净值比例 ③卖出链路割裂（`positions.json` 不存在，与 `exit_rules.py` 不共享）④成交回报未回写 prism 账本 ⑤风控硬约束缺失（无日内亏损/持仓上限/跌停保护）⑥信号幂等不足（prism 侧重启会重发）⑦无节假日日历 ⑧卖出未校验 T+1 `can_use_volume`。**上线三步验收（DRY_RUN→sim→小额真实单）一步未做**
