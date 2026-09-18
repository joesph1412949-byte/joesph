// 全局状态
const state = { screenResult: null, currentCode: null, automationPaused: false };

// ---------- 通用助手 ----------
// HTML 转义: 后端/用户数据插入 innerHTML 前统一转义(策略名可含引号/尖括号)
function escHtml(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
// 数值兜底: 空串/非法 → 默认值(显式 0 是合法输入, 不被 || 默认值吃掉)
function numOr(v, d) {
  const n = parseFloat(v);
  return isNaN(n) ? d : n;
}

// ---------- Tab 切换 ----------
function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach(p =>
    p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "compare") renderComparison(state.screenResult);
  if (name === "sector") loadSectorStage();
}

// ---------- API ----------
async function api(url, opts) {
  const res = await fetch(url, opts);
  return res.json();
}

// ---------- QMT 连接状态 ----------
async function checkHealth() {
  const el = document.getElementById("conn-status");
  try {
    const data = await api("/api/health");
    if (data.qmt_connected) {
      el.textContent = "已连接 QMT";
      el.classList.add("ok");
      el.classList.remove("fail");
    } else {
      el.textContent = "未连接";
      el.classList.add("fail");
      el.classList.remove("ok");
    }
  } catch (e) {
    el.textContent = "后端未启动";
    el.classList.add("fail");
    el.classList.remove("ok");
  }
}
checkHealth();

// ---------- 自动化开关(一键暂停) ----------
function renderAutomation(paused) {
  const el = document.getElementById("auto-status");
  const btn = document.getElementById("btn-auto");
  el.textContent = paused ? "自动化已暂停" : "自动化运行中";
  el.classList.toggle("ok", !paused);
  el.classList.toggle("fail", paused);
  btn.textContent = paused ? "恢复自动化" : "一键暂停";
  btn.classList.toggle("paused", paused);
}

async function refreshAutomation() {
  try {
    const data = await api("/api/automation");
    state.automationPaused = !!data.paused;
    renderAutomation(state.automationPaused);
  } catch (e) {
    const el = document.getElementById("auto-status");
    el.textContent = "自动化状态未知";
    el.classList.add("fail");
  }
}

async function toggleAutomation() {
  const btn = document.getElementById("btn-auto");
  btn.disabled = true;
  try {
    const data = await api("/api/automation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused: !state.automationPaused }),
    });
    state.automationPaused = !!data.paused;
    renderAutomation(state.automationPaused);
  } catch (e) {
    alert("切换自动化状态失败: " + e);
  } finally {
    btn.disabled = false;
  }
}
refreshAutomation();

// ---------- 选股 ----------
async function fetchScreen() {
  const btn = document.getElementById("btn-screen");
  btn.textContent = "选股中...";
  btn.disabled = true;
  try {
    const data = await api("/api/screen", { method: "POST" });
    if (data.error) { alert(data.error); return; }
    state.screenResult = data;
    renderMarket(data.market);
    renderCandidates(data.candidates);
    if (data.environment_ok) switchTab("candidates");
    else switchTab("market");
  } finally {
    btn.textContent = "开始选股";
    btn.disabled = false;
  }
}

// ---------- ① 市场环境 ----------
function renderMarket(market) {
  const banner = document.getElementById("market-banner");
  if (state.screenResult.environment_ok) {
    banner.className = "env-ok";
    banner.innerHTML = `✅ 市场环境达标: ${market.stage} · 节点模型 ${market.node_score}/${market.gate_total ?? 5} · 允许选股`;
  } else {
    banner.className = "env-bad";
    banner.innerHTML = `⛔ 市场环境不达标: ${market.stage} · 节点模型 ${market.node_score}/${market.gate_total ?? 5} · 空仓等待`;
  }
  // 节点因子卡片
  const cards = document.getElementById("node-cards");
  cards.innerHTML = "";
  const names = { N1:"涨停指数", N2:"情绪周期", N3:"首板溢价", N4:"连板高度", N5:"成交额",
    N6:"美股映射", N7:"美债平稳", N8:"VIX平稳" };
  for (const [f, info] of Object.entries(market.factors)) {
    const hit = info.score === 1;
    cards.insertAdjacentHTML("beforeend",
      `<div class="card"><div class="label">${names[f]||f}</div>
       <div class="value ${hit?'hit':'miss'}">${hit?"✓":"✗"}</div>
       <div style="font-size:11px;color:#aaa">${info.note||""}</div></div>`);
  }
  const s = document.getElementById("market-stats");
  s.innerHTML = `<div class="card"><div class="label">两市成交额</div>
    <div class="value">${(market.total_amount/1e12).toFixed(2)}万亿</div></div>
    <div class="card"><div class="label">今日涨停</div>
    <div class="value">${market.limit_up_count}</div></div>`;
}

// ---------- ② 候选列表 ----------
function renderCandidates(candidates) {
  const tbody = document.querySelector("#cand-table tbody");
  tbody.innerHTML = "";
  if (!candidates || candidates.length === 0) {
    tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:#999">无候选股</td></tr>';
    return;
  }
  for (const c of candidates) {
    const s = c.scores;
    const tr = document.createElement("tr");
    tr.className = "grade-" + s.grade;
    // 综合分(排名依据)醒目列: 加粗放大 + 进度条(相对上限7.0) + 按等级着色
    const pct = Math.max(0, Math.min(100, (s.composite / 7.0) * 100));
    const gc = "grade-" + s.grade;
    tr.innerHTML = `<td>${c.code}</td><td>${c.name}</td>
      <td>${s.first_board}</td><td>${s.monster}</td><td>${s.momentum}</td>
      <td class="composite-cell">
        <div class="composite-value ${gc}">${s.composite}<span class="c-max">/7</span></div>
        <div class="composite-bar"><div class="composite-bar-fill ${gc}" style="width:${pct}%"></div></div>
      </td>
      <td><span class="g-badge ${gc}">${s.grade}</span></td>
      <td>${s.strength}${s.position ? `<br><span class="pos-hint">${s.position}</span>` : ""}</td>`;
    tr.style.cursor = "pointer";
    tr.onclick = () => showStockDetail(c.code);
    tbody.appendChild(tr);
  }
}

// ---------- ③ 模型对比 ----------
function renderComparison(data) {
  if (!data || !data.candidates || data.candidates.length === 0) return;
  const c = data.candidates;
  // 池内 A/B/C/D 等级分布 (spec §4④, 后端已算 summary.a/b/c/d_count)
  const sum = data.summary || {};
  const tiles = document.getElementById("class-tiles");
  tiles.innerHTML = "";
  for (const [g, n] of [["A", sum.a_count], ["B", sum.b_count],
                        ["C", sum.c_count], ["D", sum.d_count]]) {
    tiles.insertAdjacentHTML("beforeend",
      `<div class="tile grade-${g}"><div class="label">${g}级 · 候选</div>
       <div class="value">${n}</div></div>`);
  }
  const radar = echarts.init(document.getElementById("radar-chart"));
  radar.setOption({
    title: { text: "三模型评分对比 (池内均值)" },
    radar: { indicator: [
      { name: "首板", max: 7 }, { name: "妖股", max: 7 }, { name: "势能", max: 7 },
    ]},
    series: [{
      type: "radar",
      data: [{
        name: "池内均值",
        value: [
          c.reduce((a, x) => a + x.scores.first_board, 0) / c.length,
          c.reduce((a, x) => a + x.scores.monster, 0) / c.length,
          c.reduce((a, x) => a + x.scores.momentum, 0) / c.length,
        ],
      }],
    }],
  });
  // 因子命中柱状图
  const bar = echarts.init(document.getElementById("bar-chart"));
  const counts = {};
  for (const x of c) for (const [f, v] of Object.entries(x.factors)) {
    if (v === 1) counts[f] = (counts[f] || 0) + 1;
  }
  bar.setOption({
    title: { text: "因子命中数 (池内)" },
    xAxis: { type: "category", data: Object.keys(counts) },
    yAxis: { type: "value" },
    series: [{ type: "bar", data: Object.values(counts), itemStyle: { color: "#2980b9" } }],
  });
}

// ---------- ④ 单股详情 ----------
async function showStockDetail(code) {
  state.currentCode = code;
  switchTab("detail");
  document.getElementById("detail-title").textContent = "股票 " + code;
  try {
    const [kl, manual] = await Promise.all([
      api("/api/stock/" + code + "/kline"),
      api("/api/stock/" + code + "/manual"),
    ]);
    if (kl.error) { alert("K线加载失败: " + kl.error); return; }
    renderKline(kl);
    renderFactors(state.screenResult, code, manual);
  } catch (e) { alert("加载失败: " + e); }
}

function renderKline(k) {
  const chart = echarts.init(document.getElementById("kline-chart"));
  const n = k.closes.length;
  const lastIdx = n - 1;
  const upStop = k.up_stop;                 // 涨停价
  // 突破位 = 今日之前的最高价 (当日突破历史高点才有意义)
  const priorHigh = n > 1 ? Math.max.apply(null, k.highs.slice(0, lastIdx)) : null;
  const atLimit = upStop > 0 && k.closes[lastIdx] >= upStop - 0.01;  // 今日封板

  // ECharts 蜡烛图数据顺序: [open, close, low, high]
  const ohlc = k.dates.map((_, i) =>
    [k.opens[i], k.closes[i], k.lows[i], k.highs[i]]);

  const markLines = [];
  if (upStop > 0) {
    markLines.push({ yAxis: upStop, name: "涨停价",
      lineStyle: { color: "#e74c3c", type: "dashed" } });
  }
  if (priorHigh !== null) {
    markLines.push({ yAxis: priorHigh, name: "突破位",
      lineStyle: { color: "#2980b9", type: "dashed" } });
  }

  chart.setOption({
    title: { text: "日线K线 (近120日)" },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
    grid: { left: 64, right: 24, top: 48, bottom: 64 },
    xAxis: { type: "category", data: k.dates, boundaryGap: true },
    yAxis: { type: "value", scale: true },
    dataZoom: [
      { type: "inside", start: 40, end: 100 },
      { type: "slider", start: 40, end: 100, height: 20, bottom: 10 },
    ],
    series: [
      {
        name: "K线", type: "candlestick", data: ohlc,
        itemStyle: {
          color: "#e74c3c", color0: "#27ae60",
          borderColor: "#e74c3c", borderColor0: "#27ae60",
        },
        markPoint: atLimit ? {
          symbol: "pin", symbolSize: 48,
          label: { color: "#fff" },
          data: [{ coord: [lastIdx, k.highs[lastIdx]], value: "涨停",
                   itemStyle: { color: "#e74c3c" } }],
        } : {},
        markLine: {
          symbol: "none", label: { formatter: "{b}" },
          data: markLines,
        },
      },
      { name: "MA60", type: "line", data: k.ma60, showSymbol: false,
        lineStyle: { color: "#e67e22", width: 1 } },
    ],
  });
}

function renderFactors(screenResult, code, manual) {
  const box = document.getElementById("factor-table");
  const cand = (screenResult?.candidates || []).find(c => c.code === code);
  if (!cand) { box.innerHTML = "该股不在当前候选池，先点击开始选股。"; return; }
  const factors = cand.factors;
  const src = cand.auto_manual || {};
  const names = { F1:"首板确认",F2:"早封板",F3:"封单强度",F4:"板块共振",F5:"量价堆积",
    F6:"大盘配合",F7:"题材新颖",F8:"行业边际",F9:"板块延展",
    Y1:"小市值",Y2:"筹码干净",Y3:"倍量突破",Y4:"均线多头",
    Y5:"多概念",Y6:"事件催化",Y7:"游资现身",Y8:"中市值启动",
    S2:"量价堆积密度",S3:"最小阻力",S4:"均线系统",S6:"板块共振强度",
    M6:"横盘突破",M7:"年线企稳",
    SEC1:"板块连阳",SEC2:"板块10日涨幅",SEC3:"板块资金流入",SEC4:"板块不拥挤",SEC6:"行业共振",
    N1:"涨停指数",N2:"情绪周期",N3:"首板溢价",N4:"连板高度",N5:"成交额",
    N6:"美股映射",N7:"美债平稳",N8:"VIX平稳" };
  let html = "";
  for (const [f, v] of Object.entries(factors)) {
    const src_ = src[f] || "auto";
    const badge = src_ === "fundamental" ? '<span class="src-tag">东财</span>'
                 : src_ === "manual" ? '<span class="src-tag src-manual">手填</span>'
                 : '<span class="src-tag src-auto">QMT</span>';
    html += `<div class="factor-row ${src_==='manual'?'manual':''}">
      <span class="fname">${f}</span><span>${names[f]||f}</span>
      ${badge}<span>${v===1?'✓':'✗'}</span>
    </div>`;
  }
  // 手动因子总输入(手填因子已全部删除, 2026-09-03 → 空集合)
  const MANUAL_ALL = [];
  html += `<div style="padding:8px 0;margin-top:8px;border-top:1px solid #eee">
    <b>手填因子</b>`;
  for (const f of MANUAL_ALL) {
    const cur = manual[f] ?? 0;
    html += `<div class="factor-row manual"><span class="fname">${f}</span>
      <span>${names[f]||f}</span>
      <input type="number" id="man-${f}" min="0" max="1" step="1" value="${cur}"></div>`;
  }
  html += `<button onclick="saveManual('${code}')" style="padding:8px 16px;margin-top:8px;
    background:#27ae60;color:#fff;border:none;border-radius:6px;cursor:pointer">保存手填因子并重算</button></div>`;
  box.innerHTML = html;
}

async function saveManual(code) {
  const MANUAL_ALL = [];
  const payload = {};
  for (const f of MANUAL_ALL) {
    const el = document.getElementById("man-" + f);
    if (el) payload[f] = parseInt(el.value) || 0;
  }
  const res = await api("/api/stock/" + code + "/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.error) { alert(res.error); return; }
  // 重新选股以反映手填（简化：提示用户重新点选股）
  alert("手填已保存: " + JSON.stringify(res.factors) + "\n点击「开始选股」重新计算综合分。");
}

// ==================== Prism 新功能: 因子库 / 策略 / 回测 ====================

// ---------- Tab 切换时懒加载 ----------
const origSwitchTab = switchTab;
switchTab = function(name) {
  origSwitchTab(name);
  if (name === "factors") loadFactors();
  if (name === "strategies") loadStrategies();
  if (name === "backtest") loadBacktestForm();
};

// ---------- 因子库 ----------
const FACTOR_CATEGORIES = [
  ["first_board", "首板"], ["monster", "妖股"], ["momentum", "势能"],
  ["node", "节点"], ["通用", "通用"],
];

async function loadFactors() {
  const grid = document.getElementById("factor-grid");
  const filters = document.getElementById("factor-filters");
  let factors = [];
  try {
    const data = await api("/api/factors");
    factors = data.factors || [];
  } catch (e) {
    grid.innerHTML = `<div class="error">因子库加载失败: ${e}</div>`;
    return;
  }
  // 筛选按钮
  let filterHtml = `<button class="fbtn active" data-cat="" onclick="filterFactors(this)">全部 (${factors.length})</button>`;
  for (const [cat, label] of FACTOR_CATEGORIES) {
    const n = factors.filter(f => f.category === cat).length;
    filterHtml += `<button class="fbtn" data-cat="${cat}" onclick="filterFactors(this)">${label} (${n})</button>`;
  }
  filters.innerHTML = filterHtml;
  renderFactorCards(factors);
}

function renderFactorCards(factors) {
  const grid = document.getElementById("factor-grid");
  if (!factors.length) { grid.innerHTML = `<div class="hint">暂无因子</div>`; return; }
  grid.innerHTML = factors.map(f => `
    <div class="factor-card cat-${f.category}">
      <div class="factor-card-head">
        <span class="factor-id">${f.id}</span>
        <span class="factor-cat">${f.category}</span>
      </div>
      <div class="factor-name">${f.name}</div>
      <div class="factor-desc">${f.description || ""}</div>
    </div>`).join("");
}

function filterFactors(btn) {
  document.querySelectorAll("#factor-filters .fbtn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  const cat = btn.dataset.cat;
  api("/api/factors").then(data => {
    let list = data.factors || [];
    if (cat) list = list.filter(f => f.category === cat);
    renderFactorCards(list);
  });
}

// ---------- 策略 ----------
async function loadStrategies() {
  const list = document.getElementById("strategy-list");
  try {
    const data = await api("/api/strategies");
    const strategies = data.strategies || [];
    const activeId = data.active;
    const badge = document.getElementById("active-badge");
    const a = strategies.find(s => s.id === activeId);
    badge.textContent = a ? `当前默认: ${a.name || activeId}` : "";
    list.innerHTML = strategies.map(s => `
      <div class="strategy-item" onclick="showStrategy('${escHtml(s.id)}')">
        <div class="strategy-item-name">${s.id === activeId ? "★ " : ""}${escHtml(s.name || s.id)}</div>
        <div class="strategy-item-id">${escHtml(s.id)}</div>
        <div style="margin-top:6px;">
          <button onclick="event.stopPropagation(); activateStrategy('${escHtml(s.id)}')"${s.id === activeId ? " disabled" : ""}>设为默认</button>
          <button onclick="event.stopPropagation(); copyToEditor('${escHtml(s.id)}')">复制为底稿</button>
        </div>
      </div>`).join("") || `<div class="hint">暂无策略</div>`;
    if (strategies.length) showStrategy(strategies[0].id);
  } catch (e) {
    list.innerHTML = `<div class="error">策略加载失败: ${e}</div>`;
  }
}

async function showStrategy(id) {
  document.querySelectorAll("#strategy-list .strategy-item").forEach(el => {
    el.classList.toggle("active", el.textContent.includes(id));
  });
  try {
    const data = await api("/api/strategy/" + id);
    if (!data.ok) { alert(data.error); return; }
    const s = data.strategy;
    document.getElementById("strategy-name").textContent = s.name || s.id;
    document.getElementById("strategy-desc").textContent = s.description || "";
    document.getElementById("strategy-json").textContent = JSON.stringify(s, null, 2);
  } catch (e) {
    document.getElementById("strategy-json").textContent = "加载失败: " + e;
  }
}

// ---------- 回测 ----------
function dateToStr(d) {
  const p = n => String(n).padStart(2, "0");
  return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate());
}

async function loadBacktestForm() {
  const sel = document.getElementById("bt-strategy");
  if (sel.options.length) return;   // 已加载
  try {
    const data = await api("/api/strategies");
    (data.strategies || []).forEach(s => {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.name || s.id;
      sel.appendChild(opt);
    });
  } catch (e) { /* 策略列表不可用则留空 */ }
  const today = new Date();
  const start = new Date();
  start.setDate(today.getDate() - 15);
  document.getElementById("bt-start").value =
    start.toISOString().slice(0, 10);
  document.getElementById("bt-end").value =
    today.toISOString().slice(0, 10);
}

async function runBacktest() {
  const btn = document.getElementById("btn-backtest");
  const box = document.getElementById("backtest-result");
  const strategy = document.getElementById("bt-strategy").value;
  const start = document.getElementById("bt-start").value.replace(/-/g, "");
  const end = document.getElementById("bt-end").value.replace(/-/g, "");
  if (!start || !end || start > end) { alert("请填写有效的起止日期"); return; }
  btn.disabled = true;
  box.innerHTML = `<div class="hint">回测进行中…(QMT本地K线, 请稍候)</div>`;
  try {
    const data = await api(`/api/backtest?strategy=${strategy}&start=${start}&end=${end}`);
    if (!data.ok) { box.innerHTML = `<div class="error">回测失败: ${data.error || "未知错误"}</div>`; return; }
    renderBacktest(data.report);
  } catch (e) {
    box.innerHTML = `<div class="error">回测请求失败: ${e}</div>`;
  } finally {
    btn.disabled = false;
  }
}

function renderBacktest(r) {
  const box = document.getElementById("backtest-result");
  if (!r || r.trades === 0 || r.trades == null) {
    // 零交易归因(2026-09-04): 不再只给模糊提示——门控/过滤/数据缺失摊开
    const fs = (r && r.filter_stats) || {};
    const notes = ((r && r.gate_notes) || [])
      .map(n => `<div>· ${n}</div>`).join("");
    const why = `<div>· 门控未过关 ${fs.gate_blocked_days || 0} 天 · 候选 ${fs.candidates || 0} 只`
      + ` · 被"模型分不足"过滤 ${fs.filtered_min_model || 0} 只`
      + ` · 被板块分过滤 ${fs.filtered_sector || 0} 只</div>`;
    const warn = (r && r.market_data === false)
      ? `<div style="color:#e67e22">· ⚠ 市场数据未注入(板块/全球指数缓存为空): N6-N8/F8/F9/SEC 共 10 个因子失效, 结果不可用于评估它们</div>`
      : "";
    box.innerHTML = `<div class="hint">回测完成: 无交易。<div style="margin-top:6px;font-size:12px">${warn}${why}${notes}</div>
      <br>提示: 东财历史涨停池约保留最近 20 个交易日, 可尝试更近的日期；<br>
      若"被模型分不足过滤"占多数, 说明该策略依赖实时盘口因子, 回测内核里算不出来(如 v03 对照组)。</div>`
      + renderFactorHits(r) + renderDataNotes(r);
    return;
  }
  const pct = v => v == null ? "-" : (v * 100).toFixed(1) + "%";
  const cards = [
    ["交易笔数", r.trades],
    ["胜率", pct(r.win_rate)],
    ["平均收益/笔", (r.avg_return_pct == null ? "-" : r.avg_return_pct + "%")],
    ["盈亏比", r.profit_loss_ratio == null ? "-" : r.profit_loss_ratio],
    ["夏普比", r.sharpe_ratio == null ? "-" : r.sharpe_ratio],
    ["最大回撤", r.max_drawdown_pct == null ? "-" : r.max_drawdown_pct + "%"],
    ["总收益(累加)", r.total_return_pct == null ? "-" : r.total_return_pct + "%"],
    ["平均成本/笔", r.avg_cost_pct == null ? "-" : r.avg_cost_pct + "%"],
  ];
  // 交易日志表(按日期降序, 最新在前)
  // 成本列 = 比例成本(cost_pct) + 资金层按本笔名义额扣的 ¥5 下限差额
  // (floor_cost_pct, Task 4) —— 旧报告没有 floor_cost_pct 时退化为原值。
  const costPct = t => (t.cost_pct == null && t.floor_cost_pct == null) ? null
    : +((t.cost_pct || 0) + (t.floor_cost_pct || 0)).toFixed(3);
  const log = (r.trade_log || []).map(t => `
    <tr class="bt-ret-${t.return_pct >= 0 ? "pos" : "neg"}">
      <td>${t.date}</td>
      <td>${t.code}</td>
      <td>${t.theme || "-"}</td>
      <td>${t.composite == null ? "-" : t.composite}</td>
      <td>${t.entry == null ? "-" : t.entry}</td>
      <td>${t.exit == null ? "-" : t.exit}</td>
      <td>${costPct(t) == null ? "-" : costPct(t) + "%"}</td>
      <td class="bt-ret">${t.return_pct >= 0 ? "+" : ""}${t.return_pct}%</td>
    </tr>`).join("");
  box.innerHTML = `
    <div class="cards">
      ${cards.map(([k, v]) => `
        <div class="stat-card"><div class="stat-value">${v}</div>
        <div class="stat-label">${k}</div></div>`).join("")}
    </div>
    <h3 style="margin-top:18px">交易日志(${r.trade_log ? r.trade_log.length : 0} 笔)</h3>
    <div class="bt-log-wrap">
      <table id="bt-log-table">
        <thead><tr>
          <th>日期</th><th>代码</th><th>题材</th><th>综合分</th>
          <th>买入价</th><th>卖出价</th><th>成本</th><th>收益率</th>
        </tr></thead>
        <tbody>${log || `<tr><td colspan="8" class="hint">无明细</td></tr>`}</tbody>
      </table>
    </div>
    <div class="hint" style="margin-top:10px">注: 真实交易成本模型(佣金万2.5双向 + 印花税0.05%卖出 + 过户费万0.1 + 滑点0.1%,
      成本列含 ¥5 最低佣金下限差额); 夏普比按逐日净值年化(252 交易日);
      数据源优先 QMT 本地K线。</div>`
    + renderFactorHits(r) + renderDataNotes(r);
}

// 因子存活率表(Task 5 §8): 哪些因子在真实打分、哪些恒 0、哪些一次没被评估。
// 存在的意义就是杜绝"跑的是残废版策略"再次发生 —— 有交易/零交易两个分支都要显示
// (零交易时恰恰最需要看: 是门控永不过, 还是候选全被过滤)。
function renderFactorHits(r) {
  const fh = (r && r.factor_hits) || {};
  const ids = Object.keys(fh);
  if (!ids.length) return "";
  const rows = ids.map(fid => {
    const x = fh[fid] || {};
    const rate = x.rate == null ? "-" : (x.rate * 100).toFixed(1) + "%";
    // rate==1.0 = 常数因子(每次都命中 → 对区分候选没有贡献), 必须标注
    const rt = rate + (x.rate === 1 ? " 常数" : "");
    // 非"实算"的状态(代理/恒0/未评估)标橙: 报告里最该被看见的就是这些
    const odd = x.status && x.status !== "实算";
    return `<tr>
      <td>${escHtml(fid)}</td>
      <td>${x.kind === "gate" ? "门控" : "评分"}</td>
      <td>${x.hits == null ? "-" : x.hits}/${x.evals == null ? "-" : x.evals}</td>
      <td>${rt}</td>
      <td${odd ? ' style="color:var(--sp-orange)"' : ""}>${escHtml(x.status || "-")}</td>
    </tr>`;
  }).join("");
  return `
    <h3 style="margin-top:18px">因子存活率(${ids.length} 个)</h3>
    <div class="bt-log-wrap">
      <table id="bt-factor-table">
        <thead><tr>
          <th>因子</th><th>类型</th><th>命中/评估</th><th>命中率</th><th>状态</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <div class="hint" style="margin-top:6px">
      状态: <b>实算</b> = 真实打分命中过 · <b>代理</b> = 回测只能走代理口径(实盘数据不可得)
      · <b>恒0</b> = 评估过但从未命中 · <b>未评估</b> = 一次都没算(如门控永不过, 评分因子根本没跑);
      命中率标"常数"= 每次都命中, 对区分候选没有贡献。
      门控按交易日计一次评估, 评分因子按候选股计一次。
    </div>`;
}

// 数据说明区(Task 5 §8): 不可得/被代理的数据必须公示, 不许静默 fail-open 0。
function renderDataNotes(r) {
  const notes = (r && r.data_notes) || [];
  if (!notes.length) return "";
  return `
    <h3 style="margin-top:18px">数据说明(${notes.length} 条)</h3>
    <div class="hint">${notes.map(n => `<div>· ${escHtml(n)}</div>`).join("")}</div>`;
}

// ---------- 模拟盘面板 ----------
async function refreshPaper() {
  try {
    const sum = await api("/api/paper/summary");
    const det = sum.exists ? await api("/api/paper/detail") : null;
    renderPaper(sum, det);
  } catch (e) { /* 后端未启动时静默 */ }
}

function renderPaper(sum, det) {
  const box = document.getElementById("paper-summary");
  if (!box) return;
  if (!sum.exists) {
    box.innerHTML = "<div class='hint'>模拟盘未初始化 — 运行 " +
      "<code>python -m prism.paper --init</code> 后由守护进程接管</div>";
    return;
  }
  const ret = sum.total_return_pct;
  const cls = ret >= 0 ? "ok" : "fail";
  box.innerHTML =
    `<div class="card"><b>总收益</b> <span class="badge ${cls}">${ret}%</span></div>` +
    `<div class="card"><b>当前净值</b> ${sum.nav.toLocaleString()}</div>` +
    `<div class="card"><b>现金</b> ${sum.cash.toLocaleString()}</div>` +
    `<div class="card"><b>持仓</b> ${sum.holdings_count}/5</div>` +
    `<div class="card"><b>记账天数</b> ${sum.nav_points ?? 0}</div>`;
  const hb = document.querySelector("#paper-holdings tbody");
  if (hb) hb.innerHTML = (det && det.holdings || []).map(h =>
    `<tr><td>${h.code}</td><td>${h.shares}</td><td>${h.cost}</td>` +
    `<td>${h.buy_date}</td></tr>`).join("") ||
    "<tr><td colspan=4 class='hint'>空仓等待信号</td></tr>";
  // 排板排队区(Q4): pending_buys 只读展示 — queued_shares 存股, 显示折回手
  const pb = document.getElementById("paper-pending");
  const pc = document.getElementById("paper-pending-count");
  const pend = (det && det.pending) || [];
  if (pc) pc.textContent = pend.length ? `（${pend.length} 笔排队中）` : "";
  if (pb) pb.innerHTML = pend.length ?
    `<table><thead><tr><th>代码</th><th>委托价</th><th>股数</th>` +
    `<th>前方封单(手)</th><th>已排队(分)</th></tr></thead><tbody>` +
    pend.map(p => {
      const t = new Date(p.created).getTime();
      const mins = isFinite(t) ? Math.max(0, Math.round((Date.now() - t) / 60000)) : "-";
      return `<tr><td>${escHtml(p.code)}</td><td>${p.price}</td><td>${p.shares}</td>` +
        `<td>${p.queued_shares / 100}</td><td>${mins}</td></tr>`;
    }).join("") + `</tbody></table>`
    : `<div class="hint">无排队委托</div>`;
  const tb = document.querySelector("#paper-trades tbody");
  if (tb) tb.innerHTML = (det && det.trades || []).map(t =>
    `<tr><td>${t.ts}</td><td>${t.side}</td><td>${t.code}</td>` +
    `<td>${t.price}</td><td>${t.shares}</td><td>${t.reason}</td></tr>`).join("")
    || "<tr><td colspan=6 class='hint'>暂无交易</td></tr>";
  const nb = document.querySelector("#paper-nav tbody");
  if (nb) nb.innerHTML = (det && det.nav_history || []).map(n =>
    `<tr><td>${n.date}</td><td>${n.nav.toLocaleString()}</td></tr>`).join("")
    || "<tr><td colspan=2 class='hint'>暂无净值记录</td></tr>";
}
refreshPaper();
setInterval(refreshPaper, 30000);

// ---------- 策略编辑器 ----------
let _factorCache = null;   // 供 ＋加模型 按钮(addEditorModel 无参调用)兜底

async function openEditor(template) {
  document.getElementById("strategy-editor").style.display = "block";
  document.getElementById("ed-errors").textContent = "";
  const factors = (await api("/api/factors")).factors || [];
  _factorCache = factors;
  const gateBox = document.getElementById("ed-gate");
  gateBox.innerHTML = factors.filter(f => f.category === "node").map(f =>
    `<label style="margin-right:10px;"><input type="checkbox" data-gate="${f.id}"` +
    `${template && (template.gate_factors || []).includes(f.id) ? " checked" : ""}> ${f.id} ${f.name || ""}</label>`).join("");
  const box = document.getElementById("ed-models");
  box.innerHTML = "";
  const models = (template && template.models) || [null];
  for (const m of models) addEditorModel(m, factors);
  document.getElementById("ed-name").value = "";
  if (template) {
    document.getElementById("ed-gate-threshold").value = template.gate_threshold ?? 1;
    document.getElementById("ed-min-model").value = template.candidate_min_model ?? 3;
    document.getElementById("ed-tp").value = ((template.sell || {}).take_profit_pct ?? 0.08) * 100;
    document.getElementById("ed-sl").value = ((template.sell || {}).stop_loss_pct ?? 0.05) * 100;
    document.getElementById("ed-hold").value = (template.sell || {}).max_hold_days ?? 5;
  } else {
    // M1: 新建(无底稿)时数值输入复位默认, 避免上次编辑残留
    document.getElementById("ed-gate-threshold").value = 1;
    document.getElementById("ed-min-model").value = 3;
    document.getElementById("ed-tp").value = 8;
    document.getElementById("ed-sl").value = 5;
    document.getElementById("ed-hold").value = 5;
  }
}

function addEditorModel(m, factors) {
  factors = factors || _factorCache || [];   // C1: ＋加模型按钮无参调用 → 兜底用已载入因子库, 不再产出无因子可勾的死卡片
  const box = document.getElementById("ed-models");
  const div = document.createElement("div");
  div.className = "ed-model";
  const cats = {};
  factors.forEach(f => (cats[f.category] = cats[f.category] || []).push(f));
  // I3: 因子权重对位初值 — m.weights[i] ↔ m.factors[i](与引擎按位语义一致), 缺失/越界/非法 → 1.0
  const factorW = f => {
    const p = m ? (m.factors || []).indexOf(f.id) : -1;
    const w = p >= 0 && m.weights ? parseFloat(m.weights[p]) : NaN;
    return (isFinite(w) && w > 0) ? w : 1.0;
  };
  div.innerHTML =
    `<h4>评分模型 <input class="ed-mname" placeholder="模型名" value="${m ? (m.name || "").replace(/"/g, "&quot;") : ""}">` +
    ` 权重 <input class="ed-mweight" type="number" step="0.05" value="${m ? m.weight : 1.0}" style="width:60px"></h4>` +
    Object.keys(cats).sort().map(cat =>
      `<div><b>${cat}</b> ` + cats[cat].map(f =>
        `<label style="margin-right:8px;" title="${(f.description || "").replace(/"/g, "&quot;")}">` +
        `<input type="checkbox" data-fid="${f.id}"${m && (m.factors || []).includes(f.id) ? " checked" : ""}> ${f.id}` +
        ` 权重 <input type="number" data-fw value="${factorW(f)}" step="0.05" min="0.05" style="width:46px"></label>`).join("") + `</div>`).join("<br>");
  box.appendChild(div);
}

async function copyToEditor(sid) {
  const data = await api("/api/strategy/" + sid);
  if (!data.ok) { alert(data.error); return; }
  const s = data.strategy;
  await openEditor({
    models: (s.scoring_models || []).map(m => ({
      name: m.name, weight: m.weight,
      factors: (m.factors || []).map(x => typeof x === "string" ? x : x.id),
      weights: (m.factors || []).map(x => typeof x === "string" ? 1.0 : (x.weight || 1.0)),
    })),
    gate_factors: (s.market_gate || {}).factors || [],
    gate_threshold: (s.market_gate || {}).threshold,
    candidate_min_model: (s.filters || {}).candidate_min_model,
    sell: s.sell_rules || {},
  });
  alert("已载入「" + (s.name || sid) + "」为底稿, 改名后保存为新策略。组合方式将按 top3_weighted 保存");
}

function collectEditorPayload() {
  const models = [];
  document.querySelectorAll("#ed-models .ed-model").forEach(div => {
    const checked = [...div.querySelectorAll("input[data-fid]:checked")];
    if (!checked.length) return;
    // I3: 每个勾选因子的权重取同 label 内 data-fw 输入值(非法/≤0 → 1.0 前端兜底, 后端仍校验)
    const fws = checked.map(i => {
      const w = parseFloat(i.closest("label")?.querySelector("[data-fw]")?.value);
      return (isFinite(w) && w > 0) ? w : 1.0;
    });
    models.push({id: "", name: div.querySelector(".ed-mname").value || ("模型" + (models.length + 1)),
                 weight: parseFloat(div.querySelector(".ed-mweight").value) || 1.0,
                 factors: checked.map(i => i.dataset.fid), weights: fws});
  });
  const gate = [...document.querySelectorAll("input[data-gate]:checked")].map(i => i.dataset.gate);
  return {name: document.getElementById("ed-name").value.trim(),
          models, gate_factors: gate,
          gate_threshold: parseInt(document.getElementById("ed-gate-threshold").value) || 0,
          candidate_min_model: parseInt(document.getElementById("ed-min-model").value) || 3,
          // M3: numOr — 显式 0 是合法输入, 空串/非法才落默认(后端 0<tp≤0.5 会再拦)
          sell: {take_profit_pct: numOr(document.getElementById("ed-tp").value, 8) / 100,
                 stop_loss_pct: numOr(document.getElementById("ed-sl").value, 5) / 100,
                 max_hold_days: numOr(document.getElementById("ed-hold").value, 5)}};
}

async function saveStrategy() {
  const btn = document.getElementById("btn-save-strategy");
  const errBox = document.getElementById("ed-errors");
  btn.disabled = true;
  errBox.textContent = "";
  try {
    const res = await api("/api/strategies/create", {method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(collectEditorPayload())});
    if (!res.ok) {
      // M1: 后端错误文案含用户输入/策略名 → 转义后再入 innerHTML
      errBox.innerHTML = (res.errors || [res.error || "未知错误"])
        .map(e => `<div>${escHtml(e)}</div>`).join("");
      return;
    }
    alert("已保存: " + res.id + " (可在列表点「设为默认」启用)");   // M2
    closeEditor();
    await loadStrategies();
  } catch (e) { errBox.textContent = "保存失败: " + e; }
  finally { btn.disabled = false; }
}

function closeEditor() {
  document.getElementById("strategy-editor").style.display = "none";
}

async function activateStrategy(sid) {
  const res = await api(`/api/strategies/${sid}/activate`, {method: "POST"});
  if (!res.ok) alert(res.error || "切换失败");
  if (res.ok) await loadStrategies();
}
// # ponytail: 不做拖拽排序/因子搜索框/模板预置库(YAGNI, 复制底稿够用); 编辑已有策略 = 新建+复制底稿(spec §8)

// ---------- 板块观察 ----------
const STAGE_BADGE = {
  "孕育期": "stage-gestation", "启动期": "stage-start",
  "主升期": "stage-main", "高潮期": "stage-peak",
  "退潮期": "stage-ebb", "休整": "", "数据不足": ""
};

async function loadSectorStage() {
  try {
    const data = await api("/api/sector_stage");
    document.getElementById("sector-date").textContent = data.date || "";
    document.getElementById("sector-flow-days").textContent = data.flow_days ?? 0;
    const tb1 = document.querySelector("#sector-inertia tbody");
    tb1.innerHTML = (data.inertia || []).map(r =>
      `<tr><td>${escHtml(r.name || r.code)}</td><td>${r.streak}</td>` +
      `<td>${r.last_net_in == null ? "-" : (r.last_net_in / 1e8).toFixed(2)}</td>` +
      `<td>${r.systematic ? '<span class="badge stage-main">系统性增配</span>' : ""}</td></tr>`
    ).join("") || `<tr><td colspan="4" class="hint">暂无数据(盘后跑 python -m prism.market_data --build-flow-rank 积累)</td></tr>`;
    // 周报视图: 默认按周排名升序(无排名的行垫底)
    const rows = (data.sectors || [])
      .slice()
      .sort((a, b) => (a.week_rank ?? 1e9) - (b.week_rank ?? 1e9));
    const tb2 = document.querySelector("#sector-stage tbody");
    tb2.innerHTML = rows.map(r => {
      const etf = (r.etf && r.etf.code) ? r.etf : null;
      // title=锚点行情明细(成交额折亿 + 当日涨幅); 数值列均非插值注入
      const etfTitle = etf
        ? `${escHtml(etf.code)} ${escHtml(etf.name || "")} ` +
          `成交额${etf.amount == null ? "-" : (etf.amount / 1e8).toFixed(2)}亿 ` +
          `涨跌${etf.pct_chg == null ? "-" : etf.pct_chg.toFixed(2)}%`
        : "";
      return `<tr><td>${r.week_rank ?? "-"}</td>` +
      `<td>${escHtml(r.name || r.code)}</td>` +
      `<td><span class="badge ${STAGE_BADGE[r.stage] || ""}">${escHtml(r.stage)}</span></td>` +
      `<td class="hint">${escHtml(r.note || "")}</td>` +
      `<td>${r.r5 == null ? "-" : r.r5.toFixed(1)}</td>` +
      `<td>${r.share_chg == null ? "-" : r.share_chg.toFixed(4)}</td>` +
      `<td>${r.hits ?? 0}</td>` +
      (etf ? `<td><span title="${etfTitle}">${escHtml(etf.code)} ${escHtml(etf.name || "")}</span></td>`
           : "<td>—</td>") +
      `<td>${r.new_high ? `${r.new_high.nh}/${r.new_high.base}` : "-"}</td>` +
      `<td>${r.pos_cap ?? "-"}</td></tr>`;
    }).join("");
  } catch (e) { /* fail-open: 面板留空 */ }
}
