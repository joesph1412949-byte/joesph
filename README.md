# cc-joesph — 策略可视化选股网页 (strategy visual web)

基于 **4 模型 / 24 因子** 量化打分系统构建的本地可视化选股网页。启动 QMT + miniQMT 后,网页实时读取全市场行情与涨停池,输出候选股评分清单、K线详情与模型对比。

> 仅限本机 localhost 使用,不连接任何外部服务,无实盘下单路径。

## 功能

- **四个页面模块**:市场环境仪表盘 / 候选股评分列表 / 单股 K线+因子详情 / 模型对比
- **市场环境门槛**:节点模型先判情绪环境(`ENV_THRESHOLD=3`),达标才选股,否则提示空仓等待;节点不参与个股综合分
- **三模型个股评分**:首板 / 妖股 / 势能,综合分排序 + 绝对阈值 A–E 分级;`CANDIDATE_MIN_MODEL=3` 候选过滤
- **东财自动因子**:`fundamental.py` 从东方财富自动抓取 6 个个股因子(Y1 小市值 / Y5 多概念 / F7 题材新颖 / Y7 游资现身 / Y6 事件催化 / Y2 筹码干净),逐因子失败自动跳过(fail-open),按日缓存秒回
- **手填因子**:受限因子收敛为 `S1` / `S5`(机构流入,东财不可用) / `S7`,持久化到 `manual_factors.json`,重新选股自动合并
- **K线详情**:ECharts 蜡烛图 + 涨停日标注 + 60日均线 + 突破位

## 评分模型

| 模型 | 权重 | 因子 |
|------|------|------|
| 首板 first_board | 30% | F1–F7 |
| 妖股 monster | 30% | Y1–Y7 |
| 势能 momentum | 25% | S1–S7 |
| 节点 node | 市场闸门(不参与综合分) | N1–N5 |

**综合分** = 首板×0.30 + 妖股×0.30 + 势能×0.25(上限 = 0.30×7 + 0.30×7 + 0.25×7 = 5.95)。

**A–E 绝对阈值分级**(`models.py`,`best`=最强模型分,`second`=次强):
- A:`best ≥ 6`
- B:`best ≥ 5` 且 `second ≥ 3`
- C:`best ≥ 4`
- D:`best ≥ 3`
- E:其余(不展示)

**强弱区间**(无节点):`best ≥ 6` 极强 / `≥ 5` 强 / `≥ 4` 中等 / 其余弱。

**情绪阶段**(`classify_market`):≥5 高潮期 / 4 回暖期 / 3 冰点期 / <3 退潮期。

## 环境要求

- **Windows** + 已安装 **QMT**(迅投)
- **Python 3.12**(QMT / miniQMT 自带,或本机任意 3.8+)
- `xtquant` **不要用 pip 安装** —— 它来自 QMT 的 Python 环境,运行前确保 `import xtquant` 可用(本机默认 `python` 即指向带 xtquant 的解释器)

pip 依赖(`strategy_web/requirements.txt`):

```bash
pip install flask requests numpy pandas
```

## 运行

以后每次启动就三步:

**1. 先开 QMT**(必需,否则选股取不到行情)
- 打开 QMT 交易终端并登录
- 启用 **miniQMT**(极简模式,行情端口 58610)

**2. 启动网页服务**

```bash
cd strategy_web
python app.py
```

看到 `Running on http://127.0.0.1:5000` 即启动成功。

**3. 浏览器访问**

打开 **http://localhost:5000**,点"选股"按钮。

### 注意事项

- 启动时若 QMT 未连接,页面仍可访问,但 `/api/screen` 返回 400(预期行为),页面会提示先开 QMT
- 服务只监听 `127.0.0.1`,不对外暴露
- **首次选股较慢**(每股约 4-5 次东财接口请求,涨停池 60 只约 1-2 分钟);**当天第二次点选股走缓存秒回**(缓存文件 `fundamental_cache.json`)
- **端口冲突**:5000 被占用时,用 `netstat -ano | grep 5000` 找到占用 PID 后 `taskkill //F //PID <pid>`
- 服务后台持续运行,关掉浏览器不影响服务

## API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/` | GET | 网页主页 |
| `/api/health` | GET | 健康检查,`qmt_connected` 字段 |
| `/api/screen` | POST | 跑完整选股流程,返回市场环境 + 候选清单 |
| `/api/stock/<code>/kline` | GET | 个股 120 根 K线(OHLC + ma60 + 涨停价),如 `/api/stock/600519.SH/kline` |
| `/api/stock/<code>/manual` | GET/POST | 读写手动填写的受限因子 |

## 测试

全部离线(无真实网络、无需 QMT):

```bash
cd strategy_web
python -m pytest tests/ -q    # 95 passed
```

## 说明

- 本应用**不做真实下单**。用户明确不使用 `.REAL_ARMED` 安全臂机制,依赖 QMT 自带安全保护。
- 数据源:个股K线 / 全市场盘口 / 行业板块来自 xtquant(miniQMT);N1/N3/N4 来自东方财富公开涨停池接口。
- 手动因子存于 `strategy_web/manual_factors.json`;损坏的 JSON 会被保留为 `manual_factors.json.corrupt-<时间戳>` 并告警,不静默覆盖。
