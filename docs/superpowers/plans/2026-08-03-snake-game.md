# 贪吃蛇游戏 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个单个 HTML 文件的贪吃蛇网页游戏，粉色系配色，支持最高分持久化、速度递增和开始/重新开始。

**Architecture:** 单个文件 `snake/index.html`，内嵌 CSS + 原生 JS IIFE。Canvas 绘制 20×20 网格；游戏逻辑用纯函数（`initGame`/`step`/`nextDirection`/`placeFood`/`draw`）组织，状态机 `idle → playing → over` 驱动。方向输入用队列防止 180° 掉头。最高分存 `localStorage`。

**Tech Stack:** 原生 HTML5 + CSS3 + JavaScript (ES6)，Canvas 2D。零依赖，无构建。

**Spec:** `docs/superpowers/specs/2026-08-03-snake-game-design.md`

## Global Constraints

- 交付物为**单个文件** `snake/index.html`，双击浏览器打开即可玩，不允许引入任何外部依赖（无 CDN、无框架、无构建）。
- 网格 20×20；初始蛇长 3，位于中部偏左，初始向右移动。
- 吃食物 +10 分；每吃 3 个食物加速一次（间隔 ×0.9），移动间隔下限 80ms，初始 200ms。
- 配色为粉色系：画布底 `#ffe0e6`、网格线 `#fff0f3`、蛇头 `#ff6b9d`、蛇身 `#ffb3c6`→`#ffd1dc`、食物 `#e63946`。
- 显示当前分数和最高分徽章；最高分通过 `localStorage`（键 `snake-high-score`）持久化。
- 撞墙或撞自身 → 游戏结束，显示「游戏结束」遮罩；有**开始**与**重新开始**按钮（共用逻辑）。
- 所有代码与注释使用英文标识符；界面文案为中文。
- 每个任务结束手动在浏览器验证（本机命令：`start snake/index.html`）。

---

## File Structure

- `snake/index.html` — 唯一交付物。内含：`<head>`（meta、标题、全部内嵌 CSS）、`<body>`（卡片布局、画布、遮罩、按钮）、`<script>`（游戏逻辑 IIFE）。结构按职责划分：
  - **CSS** — 主题变量 + 布局 + 组件样式
  - **script 常量区** — GRID / 速度参数 / 存储键
  - **script DOM 区** — 元素引用
  - **script 逻辑区** — 生命周期、食物、移动、渲染、UI、输入
- `docs/superpowers/specs/2026-08-03-snake-game-design.md` — 设计文档（已存在，仅参考，不改动）。

任务分解为 3 个：壳 + 样式 → 核心玩法 → 最高分/快捷键/响应式。

---

### Task 1: 页面壳与粉色样式

**Files:**
- Create: `snake/index.html`

**Interfaces:**
- Consumes: 无（新建文件）
- Produces: HTML 元素 id 供 Task 2/3 使用：`board`(canvas)、`score`、`highScore`、`mainBtn`、`overlay`、`overlayTitle`、`overlayText`、`newRecord`、`overlayBtn`

- [ ] **Step 1: 创建 `snake/` 目录与 `index.html`**

创建 `d:\cc-joesph\snake\index.html`，写入以下完整内容（script 为占位，仅初始化画布并显示空棋盘）：

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>贪吃蛇</title>
<style>
  :root {
    --bg: #1a1a2e;
    --card: #16213e;
    --board: #ffe0e6;
    --snake-head: #ff6b9d;
    --snake-body: #ffb3c6;
    --snake-body-2: #ffd1dc;
    --food: #e63946;
    --text: #f8f9fa;
    --shadow: rgba(0, 0, 0, 0.45);
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: radial-gradient(circle at 30% 20%, #232342, var(--bg) 70%);
    font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    color: var(--text);
    padding: 20px;
  }

  .card {
    background: var(--card);
    border-radius: 20px;
    padding: 24px;
    box-shadow: 0 16px 40px var(--shadow);
    display: flex;
    flex-direction: column;
    gap: 16px;
    align-items: center;
    max-width: 100%;
  }

  .header {
    display: flex;
    align-items: center;
    gap: 16px;
    width: 100%;
  }

  h1 {
    font-size: 26px;
    letter-spacing: 2px;
    background: linear-gradient(90deg, var(--snake-head), var(--food));
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
  }

  .badges { display: flex; gap: 10px; margin-left: auto; }

  .badge {
    background: rgba(255, 107, 157, 0.15);
    border: 1px solid rgba(255, 107, 157, 0.4);
    border-radius: 999px;
    padding: 6px 14px;
    font-size: 14px;
    font-weight: 600;
  }

  .badge span { color: var(--snake-head); margin-left: 4px; }

  .board-wrap {
    position: relative;
    background: var(--board);
    border-radius: 14px;
    box-shadow: inset 0 0 12px rgba(230, 57, 70, 0.12);
  }

  canvas {
    display: block;
    width: 480px;
    height: 480px;
    border-radius: 14px;
  }

  .overlay {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    background: rgba(26, 26, 46, 0.78);
    border-radius: 14px;
    opacity: 0;
    pointer-events: none;
    transition: opacity 0.25s ease;
  }

  .overlay.show { opacity: 1; pointer-events: auto; }

  .overlay h2 { font-size: 34px; letter-spacing: 4px; }
  .overlay p { font-size: 16px; opacity: 0.9; }

  .overlay .new-record { color: var(--snake-head); font-weight: 700; }

  .controls { display: flex; gap: 12px; }

  .btn {
    border: none;
    cursor: pointer;
    font-size: 16px;
    font-weight: 700;
    padding: 12px 32px;
    border-radius: 999px;
    background: linear-gradient(135deg, var(--snake-head), #ff4d6d);
    color: #fff;
    box-shadow: 0 6px 18px rgba(255, 77, 109, 0.4);
    transition: transform 0.12s ease, box-shadow 0.12s ease;
  }

  .btn:hover { transform: translateY(-2px); box-shadow: 0 10px 24px rgba(255, 77, 109, 0.5); }
  .btn:active { transform: translateY(0); }

  .hint { font-size: 13px; opacity: 0.6; }
</style>
</head>
<body>
  <div class="card">
    <div class="header">
      <h1>🐍 贪吃蛇</h1>
      <div class="badges">
        <div class="badge">分数 <span id="score">0</span></div>
        <div class="badge">最高 <span id="highScore">0</span></div>
      </div>
    </div>

    <div class="board-wrap">
      <canvas id="board" width="480" height="480"></canvas>
      <div class="overlay" id="overlay">
        <h2 id="overlayTitle">游戏结束</h2>
        <p id="overlayText">本次得分 0</p>
        <p class="new-record" id="newRecord" hidden>🎉 新纪录！</p>
        <button class="btn" id="overlayBtn">重新开始</button>
      </div>
    </div>

    <div class="controls">
      <button class="btn" id="mainBtn">开始</button>
    </div>
    <p class="hint">方向键控制移动 · 空格 / 回车 开始或重新开始</p>
  </div>

<script>
(() => {
  'use strict';

  const GRID = 20;
  const canvas = document.getElementById('board');
  const ctx = canvas.getContext('2d');
  const CELL = canvas.width / GRID;

  function drawEmptyBoard() {
    ctx.fillStyle = '#ffe0e6';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = '#fff0f3';
    ctx.lineWidth = 1;
    for (let i = 1; i < GRID; i++) {
      ctx.beginPath();
      ctx.moveTo(i * CELL, 0);
      ctx.lineTo(i * CELL, canvas.height);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(0, i * CELL);
      ctx.lineTo(canvas.width, i * CELL);
      ctx.stroke();
    }
  }

  drawEmptyBoard();
})();
</script>
</body>
</html>
```

- [ ] **Step 2: 浏览器验证视觉**

运行：`start snake/index.html`
期望：浏览器打开，居中粉色主题卡片；标题「🐍 贪吃蛇」、两个徽章（分数 0 / 最高 0）、粉色空棋盘（含浅粉网格线）、底部「开始」按钮、快捷键提示。无报错（F12 Console 为空）。

- [ ] **Step 3: 提交**

```bash
git add snake/index.html
git commit -m "feat: add snake game shell with pink theme"
```

---

### Task 2: 核心玩法（移动、吃食、得分、加速、碰撞、结束）

**Files:**
- Modify: `snake/index.html`（替换 `<script>...</script>` 整块为下方内容）

**Interfaces:**
- Consumes: Task 1 的元素 id：`board`、`score`、`mainBtn`、`overlay`、`overlayTitle`、`overlayText`、`overlayBtn`（本任务尚未使用 `highScore`、`newRecord`，Task 3 使用）
- Produces: 供 Task 3 复用的函数与状态：`state.status`（`'idle'|'playing'|'over'`）、`gameOver()`、`initGame()`、`state.foodsEaten`、`state.score`、`currentInterval()`

- [ ] **Step 1: 替换 script 为完整游戏逻辑**

删除 Task 1 中 `<script>...</script>` 整块，替换为以下内容：

```html
<script>
(() => {
  'use strict';

  // ---------- Constants ----------
  const GRID = 20;
  const BASE_INTERVAL = 200;       // 初始移动间隔 (ms)
  const MIN_INTERVAL = 80;         // 最快间隔
  const SPEED_STEP_EVERY = 3;      // 每吃 3 个食物加速一次
  const SPEED_FACTOR = 0.9;        // 每次加速系数
  const POINTS_PER_FOOD = 10;

  // ---------- DOM ----------
  const canvas = document.getElementById('board');
  const ctx = canvas.getContext('2d');
  const scoreEl = document.getElementById('score');
  const mainBtn = document.getElementById('mainBtn');
  const overlay = document.getElementById('overlay');
  const overlayTitle = document.getElementById('overlayTitle');
  const overlayText = document.getElementById('overlayText');
  const overlayBtn = document.getElementById('overlayBtn');

  const CELL = canvas.width / GRID;

  // ---------- State ----------
  const state = {
    snake: [],
    dir: { x: 1, y: 0 },   // 当前方向
    queue: [],             // 方向输入队列
    food: { x: 0, y: 0 },
    score: 0,
    foodsEaten: 0,
    status: 'idle',        // 'idle' | 'playing' | 'over'
    timer: null,
  };

  const OPPOSITE = { up: 'down', down: 'up', left: 'right', right: 'left' };
  const KEY_TO_DIR = {
    ArrowUp:    { key: 'up',    x: 0,  y: -1 },
    ArrowDown:  { key: 'down',  x: 0,  y: 1 },
    ArrowLeft:  { key: 'left',  x: -1, y: 0 },
    ArrowRight: { key: 'right', x: 1,  y: 0 },
  };

  // ---------- Lifecycle ----------
  function initGame() {
    state.snake = [{ x: 8, y: 10 }, { x: 7, y: 10 }, { x: 6, y: 10 }];
    state.dir = { x: 1, y: 0 };
    state.queue = [];
    state.score = 0;
    state.foodsEaten = 0;
    placeFood();
    scoreEl.textContent = '0';
    hideOverlay();
    draw();
  }

  function startGame() {
    clearTimer();
    initGame();
    state.status = 'playing';
    mainBtn.textContent = '重新开始';
    state.timer = setInterval(step, currentInterval());
  }

  function gameOver() {
    clearTimer();
    state.status = 'over';
    mainBtn.textContent = '重新开始';
    showOverlay('游戏结束', `本次得分 ${state.score}`);
    draw();
  }

  function currentInterval() {
    const factor = Math.pow(SPEED_FACTOR, Math.floor(state.foodsEaten / SPEED_STEP_EVERY));
    return Math.max(MIN_INTERVAL, Math.round(BASE_INTERVAL * factor));
  }

  // ---------- Food ----------
  function placeFood() {
    const free = [];
    for (let y = 0; y < GRID; y++) {
      for (let x = 0; x < GRID; x++) {
        if (!state.snake.some(s => s.x === x && s.y === y)) free.push({ x, y });
      }
    }
    if (!free.length) return; // 蛇占满棋盘（实际不可达）
    state.food = free[Math.floor(Math.random() * free.length)];
  }

  // ---------- Movement ----------
  function nextDirection() {
    let dir = state.dir;
    while (state.queue.length) {
      const candidate = state.queue.shift();
      if (OPPOSITE[dir.key] !== candidate.key) { dir = candidate; break; }
    }
    state.dir = dir;
    return dir;
  }

  function step() {
    const dir = nextDirection();
    const head = state.snake[0];
    const newHead = { x: head.x + dir.x, y: head.y + dir.y };

    // 撞墙
    if (newHead.x < 0 || newHead.x >= GRID || newHead.y < 0 || newHead.y >= GRID) {
      gameOver();
      return;
    }

    const eats = newHead.x === state.food.x && newHead.y === state.food.y;
    // 不吃时尾巴会移走，检测时排除尾巴
    const bodyToCheck = eats ? state.snake : state.snake.slice(0, -1);

    // 撞自己
    if (bodyToCheck.some(s => s.x === newHead.x && s.y === newHead.y)) {
      gameOver();
      return;
    }

    state.snake.unshift(newHead);
    if (eats) {
      state.foodsEaten++;
      state.score += POINTS_PER_FOOD;
      scoreEl.textContent = state.score;
      placeFood();
      // 吃食后重新计时以应用加速
      clearTimer();
      state.timer = setInterval(step, currentInterval());
    } else {
      state.snake.pop();
    }
    draw();
  }

  // ---------- Rendering ----------
  function draw() {
    ctx.fillStyle = '#ffe0e6';
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    ctx.strokeStyle = '#fff0f3';
    ctx.lineWidth = 1;
    for (let i = 1; i < GRID; i++) {
      ctx.beginPath();
      ctx.moveTo(i * CELL, 0);
      ctx.lineTo(i * CELL, canvas.height);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(0, i * CELL);
      ctx.lineTo(canvas.width, i * CELL);
      ctx.stroke();
    }

    // 食物（玫红圆点 + 高光）
    const fx = state.food.x * CELL + CELL / 2;
    const fy = state.food.y * CELL + CELL / 2;
    ctx.fillStyle = '#e63946';
    ctx.beginPath();
    ctx.arc(fx, fy, CELL * 0.34, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = '#ff8fa3';
    ctx.beginPath();
    ctx.arc(fx - CELL * 0.08, fy - CELL * 0.08, CELL * 0.12, 0, Math.PI * 2);
    ctx.fill();

    // 蛇：头粉色、身渐变
    state.snake.forEach((seg, i) => {
      const isHead = i === 0;
      const t = i / Math.max(1, state.snake.length - 1);
      ctx.fillStyle = isHead ? '#ff6b9d'
        : t < 0.5 ? '#ffb3c6' : '#ffd1dc';
      const pad = isHead ? 1 : 2.5;
      roundRect(seg.x * CELL + pad, seg.y * CELL + pad, CELL - pad * 2, CELL - pad * 2, isHead ? 6 : 5);
      ctx.fill();
    });
  }

  function roundRect(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // ---------- UI ----------
  function showOverlay(title, text) {
    overlayTitle.textContent = title;
    overlayText.textContent = text;
    overlay.classList.add('show');
  }

  function hideOverlay() {
    overlay.classList.remove('show');
  }

  function clearTimer() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  // ---------- Input ----------
  document.addEventListener('keydown', (e) => {
    if (e.key in KEY_TO_DIR) {
      e.preventDefault();
      if (state.status !== 'playing') return;
      const d = KEY_TO_DIR[e.key];
      const last = state.queue[state.queue.length - 1] || state.dir;
      if (OPPOSITE[last.key] !== d.key) state.queue.push(d);
    }
  });

  mainBtn.addEventListener('click', startGame);
  overlayBtn.addEventListener('click', startGame);

  // ---------- Boot ----------
  initGame();
})();
</script>
```

- [ ] **Step 2: 浏览器验证完整玩法**

运行：`start snake/index.html`
验证清单：
1. 点击「开始」→ 蛇向右自动移动
2. 按方向键（↑↓←→）蛇转向；快速连按反方向不会瞬间 180° 掉头自杀（输入队列生效）
3. 吃到食物 → 长度 +1、分数 +10；吃满 3 个后移动明显变快
4. 撞墙 → 弹出「游戏结束」遮罩 + 本次得分
5. 重新开始时绕圈撞到自己 → 同样游戏结束
6. 遮罩上「重新开始」按钮与底部「重新开始」按钮均可重新开局
7. F12 Console 无报错

- [ ] **Step 3: 提交**

```bash
git add snake/index.html
git commit -m "feat: implement core snake gameplay with collision and game over"
```

---

### Task 3: 最高分持久化、快捷键、响应式缩放

**Files:**
- Modify: `snake/index.html`（在 Task 2 基础上做以下 5 处编辑）

**Interfaces:**
- Consumes: Task 2 的 `state`、`gameOver()`、`initGame()`、`state.status`；Task 1 的 `highScore`、`newRecord` 元素
- Produces: 无（收尾）

- [ ] **Step 1: 添加最高分常量与 DOM 引用**

在 `const POINTS_PER_FOOD = 10;` 之后加一行：

```js
  const HIGH_SCORE_KEY = 'snake-high-score';
```

在 `const overlayBtn = document.getElementById('overlayBtn');` 之后加两行：

```js
  const highScoreEl = document.getElementById('highScore');
  const newRecordEl = document.getElementById('newRecord');
```

- [ ] **Step 2: 加入最高分读写函数，并让 UI 走统一更新**

在 `currentInterval()` 函数之后新增以下两个函数：

```js
  const memHighScore = { value: 0 };

  function getHighScore() {
    try {
      return parseInt(localStorage.getItem(HIGH_SCORE_KEY) || '0', 10);
    } catch (e) {
      return memHighScore.value;
    }
  }

  function updateHighScore() {
    if (state.score > getHighScore()) {
      try {
        localStorage.setItem(HIGH_SCORE_KEY, String(state.score));
      } catch (e) {
        memHighScore.value = state.score;
      }
      newRecordEl.hidden = false;
    } else {
      newRecordEl.hidden = true;
    }
    highScoreEl.textContent = getHighScore();
  }
```

> **注意（最终审查修订）：** localStorage 访问用 try/catch 防护（存储被禁用/隐私模式下会抛 SecurityError，直接读取会在 `initGame()` 里中止页面导致白屏），并加内存回退。

将 `initGame()` 中的 `scoreEl.textContent = '0';` 替换为：

```js
    scoreEl.textContent = '0';
    highScoreEl.textContent = getHighScore();
    newRecordEl.hidden = true;
```

将 `gameOver()` 中 `showOverlay(...)` 之后、`draw()` 之前插入：

```js
    updateHighScore();
```

即 `gameOver()` 变为：

```js
  function gameOver() {
    clearTimer();
    state.status = 'over';
    mainBtn.textContent = '重新开始';
    showOverlay('游戏结束', `本次得分 ${state.score}`);
    updateHighScore();
    draw();
  }
```

- [ ] **Step 3: 加入空格 / 回车快捷键**

将整个 `document.addEventListener('keydown', ...)` 块替换为：

```js
  document.addEventListener('keydown', (e) => {
    if (e.key in KEY_TO_DIR) {
      e.preventDefault();
      if (state.status !== 'playing') return;
      const d = KEY_TO_DIR[e.key];
      const last = state.queue[state.queue.length - 1] || state.dir;
      if (OPPOSITE[last.key] !== d.key) state.queue.push(d);
    } else if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault();
      if (state.status !== 'playing') startGame();
    }
  });
```

> **注意（最终审查修订）：** `preventDefault()` 必须对 Space/Enter **无条件**调用（即使在游戏中），否则当玩家点击「开始」按钮开局后按钮保持聚焦，游戏中按空格/回车会触发浏览器原生的按钮激活 → 静默重启游戏丢失进度。

- [ ] **Step 4: 加入响应式缩放**

在 `mainBtn.addEventListener('click', startGame);` 之前新增：

```js
  function fitCanvas() {
    const maxSize = Math.min(480, window.innerWidth - 90);
    canvas.style.width = maxSize + 'px';
    canvas.style.height = maxSize + 'px';
  }
  window.addEventListener('resize', fitCanvas);
  fitCanvas();
```

- [ ] **Step 5: 浏览器验证**

运行：`start snake/index.html`
验证清单：
1. 打一局拿到分数后撞墙 → 遮罩出现「🎉 新纪录！」，最高徽章更新为本次得分
2. 刷新页面 → 最高分保留；再次开局得分低于最高分时**不**显示「新纪录」
3. 游戏结束/待开始状态按**空格**或**回车** → 直接开始新局
4. 拖动窗口缩小 → 棋盘随窗口缩小，不出现横向滚动条
5. F12 Console 无报错

- [ ] **Step 6: 提交**

```bash
git add snake/index.html
git commit -m "feat: persist high score, add keyboard shortcuts, responsive board"
```

---

## 验收汇总（全部任务完成后）

对照 `docs/superpowers/specs/2026-08-03-snake-game-design.md` 逐条验证：
- [ ] 方向键控制移动 ✅（Task 2）
- [ ] 吃食物变长、+10 分、每 3 个加速 ✅（Task 2）
- [ ] 撞墙/撞自己游戏结束 + 遮罩 ✅（Task 2）
- [ ] 开始 / 重新开始按钮 ✅（Task 1/2）
- [ ] 最高分显示 + localStorage 持久化 + 新纪录提示 ✅（Task 3）
- [ ] 空格/回车快捷键 ✅（Task 3）
- [ ] 响应式缩放 ✅（Task 3）
- [ ] 粉色系简洁界面 ✅（Task 1）
