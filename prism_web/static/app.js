// 全局状态
const state = { screenResult: null, currentCode: null, automationPaused: false };

// ---------- Tab 切换 ----------
function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t =>
    t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tab-panel").forEach(p =>
    p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "compare") renderComparison(state.screenResult);
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
    banner.innerHTML = `✅ 市场环境达标: ${market.stage} · 节点模型 ${market.node_score}/5 · 允许选股`;
  } else {
    banner.className = "env-bad";
    banner.innerHTML = `⛔ 市场环境不达标: ${market.stage} · 节点模型 ${market.node_score}/5 · 空仓等待`;
  }
  // 节点因子卡片
  const cards = document.getElementById("node-cards");
  cards.innerHTML = "";
  const names = { N1:"涨停指数", N2:"情绪周期", N3:"首板溢价", N4:"连板高度", N5:"成交额" };
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
    F6:"大盘配合",F7:"题材新颖",Y1:"小市值",Y2:"筹码干净",Y3:"倍量突破",Y4:"均线多头",
    Y5:"多概念",Y6:"事件催化",Y7:"游资现身",S1:"产业趋势",S2:"量价堆积密度",S3:"最小阻力",
    S4:"均线系统",S5:"机构流入",S6:"板块共振强度",S7:"基本面催化",
    N1:"涨停指数",N2:"情绪周期",N3:"首板溢价",N4:"连板高度",N5:"成交额" };
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
  // 手动因子总输入（覆盖所有可手填项）
  const MANUAL_ALL = ["S1","S5","S7"];
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
  const MANUAL_ALL = ["S1","S5","S7"];
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
