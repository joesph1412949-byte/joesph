# 选股策略 ↔ Vibe-Trading agent 桥接 — 设计文档

> 日期: 2026-08-13
> 状态: 待用户审阅

## 目标

把 strategy_web(量化选股网页)与 Vibe-Trading agent 打通:agent 能读到选股结果快照 + 主动触发重新选股,结合已有的 qmt_account 实盘账户数据,给出分析与操作建议。并用一个一键启动脚本把所有服务拉起,日常**不依赖 Claude Code**,开 QMT + 双击脚本 + 打开网页即可。

## 架构与数据流

```
QMT miniQMT ──xtdata──▶ strategy_web(Flask, Py3.7, 127.0.0.1:5000)
                              │  POST /api/screen   跑完落盘快照 screen_result.json
                              │  GET  /api/screen/latest   读快照(秒回)
                              │  GET  /api/health          查 QMT 连接状态
                              ▲
                              │ urllib(标准库, 无 xtquant)
Vibe-Trading agent ──strategy_screen 工具──┤
                              │
         (已有) qmt_account 工具 ──▶ D:\QMT_SYNC\qmt_sync.db(实盘账户)
```

- strategy_web 与 Vibe-Trading 是两个独立进程,仅通过 HTTP(127.0.0.1)通信。
- agent 侧工具用标准库 `urllib.request`,不 import xtquant,不 import flask。
- 所有服务只监听 127.0.0.1,数据不离开本机。

## 环境约束(必须遵守)

| 组件 | 解释器 | 关键依赖 |
|------|--------|----------|
| qmt_sync | Python 3.7.4 | xtquant(cp37 pyd), `PYTHONPATH=D:\QMT\bin.x64\Lib\site-packages` |
| strategy_web | Python 3.7.4 | xtdata(cp37 pyd) + **flask**(需安装) |
| Vibe-Trading 后端/agent | `.venv`(Python 3.12) | 无 xtquant 依赖,工具用标准库 urllib |

- xtquant 只带 cp36–cp311 的 `.pyd`;Python 3.12 无法 import xtdata(无 cp312)。
- Anaconda 3.9 是 32 位,加载不了 64 位 pyd,不可用。
- 因此 strategy_web 必须在 Python 3.7 跑,并安装 flask(及现有 requirements)。

## 数据契约

### 选股结果(screen.py `run()` 的返回,已存在,不改)

```json
{
  "market": {
    "node_score": 3,
    "stage": "冰点期",
    "factors": { "N1": {"score": 0}, "...": "..." },
    "total_amount": 123456789.0,
    "limit_up_count": 60
  },
  "environment_ok": true,
  "candidates": [
    {
      "code": "600000.SH", "name": "浦发银行",
      "last": 10.5, "up_stop_price": 10.55,
      "sealed": true, "float_mv": 123456789.0,
      "scores": {
        "first_board": 5, "monster": 4, "momentum": 3, "node": 3,
        "composite": 3.85, "grade": "B",
        "strength": "强", "position": "仓位上限50%"
      },
      "factors": { "F1": 1, "...": 0 },
      "auto_manual": { "F1": "auto", "S5": "manual", "...": "fundamental" }
    }
  ],
  "summary": { "candidate_count": 12, "a_count": 1, "b_count": 3, "c_count": 5, "d_count": 3 }
}
```

- `scores.grade` ∈ A/B/C/D/E;`scores.strength` ∈ 极强/强/中等/弱;`scores.position` ∈ 仓位上限75%/50%/30% 或 观察/空仓。

### 快照文件 `strategy_web/screen_result.json`

```json
{
  "generated_at": "2026-08-13 10:00:00",
  "market": { "...": "同 screen 结果" },
  "environment_ok": true,
  "candidates": [ "..." ],
  "summary": { "..." }
}
```

即 screen 结果扁平化 + 顶层 `generated_at`。原子写入:先写 `screen_result.json.tmp` 再 `os.replace`。

## 改动点

### ① strategy_web(修改 app.py + 新增测试)

1. `/api/screen`(POST,已有)跑成功后,把结果加 `generated_at` 写入 `screen_result.json`(原子写)。
2. 新增 `GET /api/screen/latest`:
   - 有快照 → `{"ok": true, "generated_at": ..., "market": ..., "environment_ok": ..., "summary": ..., "candidates": [...]}` (200)
   - 无快照 → `{"ok": false, "error": "尚未选股, 请先调用 /api/screen"}` (404)
3. 现有 `GET /api/health` 保持不变(返回 `qmt_connected`)。

### ② Vibe-Trading 新工具 `strategy_screen`(新建 `src/tools/strategy_screen_tool.py`)

仿 `qmt_account_tool.py` 的只读 + fail-open 模式。

- `name = "strategy_screen"`
- `is_readonly = True`
- 参数 schema:
  ```json
  {
    "type": "object",
    "properties": {
      "operation": {
        "type": "string",
        "enum": ["latest", "run", "health"],
        "description": "latest=读最近选股快照(秒回); run=重新选股(耗时1-2分钟); health=查选股服务/QMT状态"
      },
      "top_n": { "type": "integer", "description": "只返回综合分前 N 个候选, 默认全部" }
    },
    "required": ["operation"]
  }
  ```
- 实现:用 `urllib.request` 调 `http://127.0.0.1:5000`。
  - `latest` → GET `/api/screen/latest`,超时 5s;可选按 `top_n` 截断 candidates(按 composite 已排序)。
  - `run` → POST `/api/screen`,超时 180s(选股首次 1-2 分钟)。
  - `health` → GET `/api/health`,超时 5s。
- 返回:统一 `{"ok": ...}` JSON,`ensure_ascii=False`。
- fail-open:连接失败 / 超时 / 非 200 → 返回 `{"ok": false, "error": "选股服务不可用, 请先运行 strategy_web(app.py): ..."}`,**绝不抛异常**。
- base URL 可被环境变量 `STRATEGY_WEB_URL` 覆盖(默认 `http://127.0.0.1:5000`),便于测试注入。

### ③ 一键启动脚本(新建 `start_all.bat`,放 d:\cc-joesph 根)

用 `start` 后台拉起四进程,各自指定正确解释器与工作目录:

1. qmt_sync(watch 模式):`Python37\python.exe -m qmt_sync --account-id 88869979`,cwd `d:\cc-joesph`,env `PYTHONPATH=D:\QMT\bin.x64\Lib\site-packages`
2. strategy_web:`Python37\python.exe app.py`,cwd `d:\cc-joesph\strategy_web`,env 同上 + `APP_DEBUG=0`
3. Vibe-Trading 后端:`D:\Vibe-Trading\.venv\Scripts\vibe-trading.exe serve --port 8899`,cwd `D:\Vibe-Trading`
4. Vibe-Trading 前端:`npm run dev`,cwd `D:\Vibe-Trading\frontend`

- 各进程 stdout/stderr 重定向到 `D:\QMT_SYNC\logs\<name>.log`(启动前 `mkdir` 日志目录)。
- 脚本末尾 `start http://localhost:5899` 打开浏览器。
- 账号 `88869979` 作为 `--account-id` 参数写在脚本内(与现有运行方式一致;qmt_sync.conf 默认不存在)。

## 错误处理与边界

- agent 工具任何异常 → fail-open 返回带提示 JSON,不让 agent 循环崩。
- 选股依赖 QMT 已登录:未连接时 `/api/screen` 返回 400(已有),agent `run`/`health` 会如实透出。
- 只读:agent 侧只 GET/POST 选股(选股本身只读行情、不下单),无实盘下单路径。
- 数据不外发:所有服务 127.0.0.1 监听。

## 测试策略

- **strategy_web**(沿用现有 pytest,离线 mock xtdata):新增
  - 快照落盘:mock `ScreenRunner.run`,断言 `screen_result.json` 生成 + `generated_at` 存在 + 原子写不残留 tmp。
  - `/api/screen/latest`:有快照返回 200 + 结构正确;无快照返回 404。
- **Vibe-Trading 工具**:monkeypatch 注入 fake HTTP 客户端(或 monkeypatch `urllib.request.urlopen`),测 latest/run/health 正常路径 + fail-open(连接失败、非 200、超时)。
- 现有 95(strategy_web)+ 27(qmt_sync)测试保持通过。

## 全局约束

- 只读、本地、无下单、无外发。
- 不 push 到任何 remote(本地工作流)。
- Python 版本严格按环境约束表。
- agent 工具遵循 qmt_account_tool 的既有模式(BaseTool 子类、`execute(**kwargs)`、fail-open)。
