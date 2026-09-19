/* ==========================================================================
   prism-theme.js — Prism 视觉层增强 (presentation only)
   --------------------------------------------------------------------------
   本文件只做三件与业务无关的事, 且任何一处失败都必须静默降级、绝不影响主流程:

     1) 把 ECharts 默认的浅色主题换成 Prism 深色演播室主题
        —— 通过包装 echarts.init / instance.setOption 实现, 因此 app.js
           一行都不用改; 只重写「颜色」, 不动任何数据、系列与配置语义。
     2) 标签页的「光谱墨条」—— 用 MutationObserver 跟随 .active 变化滑动,
           不介入 switchTab 的实现。
     3) 图表容器尺寸变化时自动 resize —— 修正切页/缩放后画布被压扁的问题。

   加载顺序必须是: echarts.min.js → prism-theme.js → app.js
   若 echarts 未就绪, 本文件整体退化为空操作。
   ========================================================================== */
(function () {
  "use strict";

  /* ======================================================================
     1. ECharts 深色主题
     ====================================================================== */
  var ECHARTS_THEME = "prism-dark";
  var FONT = '-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", ' +
             '"Helvetica Neue", "PingFang SC", "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif';

  /* 调色板单一真源在 CSS 自定义属性里 —— 换主题只需换一份 CSS。
     读取失败(或宿主未定义)时回落到 Prism 深色默认值, 行为与从前完全一致。 */
  function cssVar(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name);
      v = (v || "").trim();
      return v || fallback;
    } catch (e) { return fallback; }
  }

  var C = {
    fg:      cssVar("--txt-0",   "#f5f5f7"),
    txt1:    cssVar("--txt-1",   "#a1a1a6"),
    txt2:    cssVar("--txt-2",   "#86868b"),
    line:    cssVar("--chart-line",    "rgba(255,255,255,0.06)"),
    axis:    cssVar("--chart-axis",    "rgba(255,255,255,0.13)"),
    surface: cssVar("--chart-surface", "rgba(18,18,22,0.88)"),
    edge:    cssVar("--chart-edge",    "rgba(255,255,255,0.13)"),
    labelBg: cssVar("--chart-label-bg","#1f1f24"),
    onBar:   cssVar("--chart-on-bar", "#08080a"),
    ink:     cssVar("--chart-ink",    "255,255,255"),
    accentFaint: cssVar("--chart-accent-faint", "rgba(10,132,255,0.12)"),
    accentSoft:  cssVar("--chart-accent-soft",  "rgba(10,132,255,0.20)"),
    red:     cssVar("--sp-red",    "#ff453a"),
    orange:  cssVar("--sp-orange", "#ff9f0a"),
    yellow:  cssVar("--sp-yellow", "#ffd60a"),
    green:   cssVar("--sp-green",  "#30d158"),
    teal:    cssVar("--sp-teal",   "#40cbe0"),
    blue:    cssVar("--sp-blue",   "#0a84ff"),
    purple:  cssVar("--sp-purple", "#bf5af2")
  };

  /* 用中性基色合成叠加色 —— 这是整套主题里唯一需要"翻转"的东西:
     深色主题白上加白, 浅色主题黑上加黑。 */
  function ink(a) { return "rgba(" + C.ink + "," + a + ")"; }

  /* 旧调色板 → Prism 调色板。
     仅做色相规整: 红仍是红(涨), 绿仍是绿(跌), 语义不变。 */
  var LEGACY_COLORS = {
    "#2980b9": C.blue,
    "#e74c3c": C.red,
    "#27ae60": C.green,
    "#e67e22": C.orange,
    "#2c3e50": C.fg,
    "#333333": C.txt2,
    "#999999": C.txt2,
    "#cccccc": C.edge,
    "#eee":    C.line,
    "#ffffff": C.fg
  };

  var hasEcharts = (typeof window.echarts !== "undefined" && window.echarts);

  if (hasEcharts) {
    try {
      window.echarts.registerTheme(ECHARTS_THEME, {
        color: [C.blue, C.teal, C.green, C.yellow, C.orange, C.red, C.purple],
        backgroundColor: "transparent",
        textStyle: { fontFamily: FONT, color: C.txt1 },

        title: {
          left: 2, top: 2,
          textStyle: { color: C.fg, fontSize: 13, fontWeight: 600, fontFamily: FONT },
          subtextStyle: { color: C.txt2, fontSize: 11, fontFamily: FONT }
        },

        legend: {
          textStyle: { color: C.txt1, fontFamily: FONT, fontSize: 11.5 },
          inactiveColor: ink(0.22)
        },

        tooltip: {
          backgroundColor: C.surface,
          borderColor: C.edge,
          borderWidth: 1,
          padding: [10, 14],
          textStyle: { color: C.fg, fontFamily: FONT, fontSize: 12 },
          axisPointer: {
            lineStyle: { color: ink(0.22) },
            crossStyle: { color: ink(0.22) },
            label: { backgroundColor: C.labelBg, borderColor: C.edge, color: C.fg }
          },
          extraCssText: "border-radius:12px;box-shadow:0 16px 48px rgba(0,0,0,.62);" +
                        "-webkit-backdrop-filter:blur(20px) saturate(180%);" +
                        "backdrop-filter:blur(20px) saturate(180%);"
        },

        grid: { left: 58, right: 26, top: 50, bottom: 54, containLabel: false },

        categoryAxis: {
          axisLine: { lineStyle: { color: C.axis } },
          axisTick: { show: false },
          axisLabel: { color: C.txt2, fontFamily: FONT, fontSize: 10.5 },
          splitLine: { show: false },
          splitArea: { show: false }
        },
        valueAxis: {
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { color: C.txt2, fontFamily: FONT, fontSize: 10.5 },
          splitLine: { lineStyle: { color: C.line, type: "solid" } },
          splitArea: { show: false }
        },
        logAxis: {
          axisLine: { lineStyle: { color: C.axis } },
          axisTick: { show: false },
          axisLabel: { color: C.txt2, fontFamily: FONT, fontSize: 10.5 },
          splitLine: { lineStyle: { color: C.line } }
        },

        radar: {
          /* 默认雷达图偏小, 在一整块面板里显得单薄 —— 放大并略下移中心 */
          radius: "68%",
          center: ["50%", "52%"],
          axisName: { color: C.txt1, fontFamily: FONT, fontSize: 11.5 },
          axisLine: { lineStyle: { color: C.line } },
          splitLine: { lineStyle: { color: C.line } },
          splitArea: { areaStyle: { color: [ink(0.012), ink(0.028)] } }
        },

        dataZoom: {
          backgroundColor: "transparent",
          borderColor: "transparent",
          fillerColor: C.accentFaint,
          handleStyle: { color: ink(0.34), borderColor: "transparent" },
          moveHandleStyle: { color: ink(0.18) },
          emphasis: {
            handleStyle: { color: ink(0.6) },
            moveHandleStyle: { color: ink(0.28) }
          },
          textStyle: { color: C.txt2, fontFamily: FONT, fontSize: 10.5 },
          dataBackground: {
            lineStyle: { color: ink(0.20) },
            areaStyle: { color: ink(0.06) }
          },
          selectedDataBackground: {
            lineStyle: { color: C.blue },
            areaStyle: { color: C.accentSoft }
          }
        },

        line: { symbolSize: 5, smooth: false },
        bar: { itemStyle: { borderRadius: [3, 3, 0, 0] } },
        candlestick: {
          itemStyle: {
            color: C.red, color0: C.green,
            borderColor: C.red, borderColor0: C.green,
            borderWidth: 1
          }
        },
        markPoint: {
          label: { color: C.onBar, fontFamily: FONT, fontSize: 11, fontWeight: 600 }
        },
        markLine: {
          label: { color: C.txt1, fontFamily: FONT, fontSize: 10.5 },
          lineStyle: { width: 1 }
        }
      });
    } catch (e) {
      /* 主题注册失败 → 保持 ECharts 默认外观, 功能不受影响 */
    }

    /* ---- 递归改写选项里的旧色值 (只碰字符串色值, 不碰数据/结构) ---- */
    function remapColors(node, depth) {
      if (!node || depth > 7) return node;
      if (typeof node === "string") {
        var hit = LEGACY_COLORS[node.toLowerCase()];
        return hit ? hit : node;
      }
      if (typeof node !== "object") return node;
      if (Array.isArray(node)) {
        for (var i = 0; i < node.length; i++) node[i] = remapColors(node[i], depth + 1);
        return node;
      }
      for (var k in node) {
        if (!Object.prototype.hasOwnProperty.call(node, k)) continue;
        var v = node[k];
        var t = typeof v;
        if (t === "string" || (t === "object" && v !== null)) {
          node[k] = remapColors(v, depth + 1);
        }
      }
      return node;
    }

    /* ---- 补齐「仅视觉」的默认值 ----
       只在 app.js 未显式指定时补, 不覆盖任何既有设定, 不触碰数据。 */
    function rgbaOf(hex, alpha) {
      var m = /^#([0-9a-f]{6})$/i.exec(String(hex));
      if (!m) return hex;
      var n = parseInt(m[1], 16);
      return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + alpha + ")";
    }

    function enrichOption(option) {
      if (!option || typeof option !== "object") return;

      /* 雷达坐标系: 放到接近面板满幅 (仅当调用方未指定) */
      var rd = option.radar;
      if (rd && !Array.isArray(rd) && typeof rd === "object") {
        if (rd.radius === undefined) rd.radius = "74%";
        if (rd.center === undefined) rd.center = ["50%", "53%"];
      }

      var series = option.series;
      if (!series) return;
      if (!Array.isArray(series)) series = [series];

      for (var i = 0; i < series.length; i++) {
        var s = series[i];
        if (!s || typeof s !== "object") continue;

        if (s.type === "radar") {
          if (!s.lineStyle) s.lineStyle = { width: 2, color: C.blue };
          if (!s.itemStyle) s.itemStyle = { color: C.blue };
          if (!s.areaStyle) s.areaStyle = { color: C.accentSoft };
          if (s.symbolSize === undefined) s.symbolSize = 6;
          continue;
        }

        /* 柱体加一层自上而下的微渐变 —— 平涂的柱子显得廉价 */
        if (s.type === "bar" && window.echarts.graphic &&
            window.echarts.graphic.LinearGradient) {
          if (!s.itemStyle) s.itemStyle = {};
          var col = s.itemStyle.color;
          if (typeof col === "string" && col.charAt(0) === "#") {
            try {
              s.itemStyle.color = new window.echarts.graphic.LinearGradient(
                0, 0, 0, 1,
                [{ offset: 0, color: col }, { offset: 1, color: rgbaOf(col, 0.45) }]
              );
            } catch (e) { /* 保持原色 */ }
          }
        }
      }
    }

    /* ---- 包装 setOption / init ---- */
    function wrapInstance(inst) {
      if (!inst || inst.__prismWrapped) return inst;
      try {
        var origSetOption = inst.setOption;
        inst.setOption = function (option, notMerge, lazyUpdate) {
          try { remapColors(option, 0); } catch (e) { /* 改写失败则原样交给 ECharts */ }
          try { enrichOption(option); } catch (e) { /* 忽略 */ }
          return origSetOption.call(inst, option, notMerge, lazyUpdate);
        };
        try {
          Object.defineProperty(inst, "__prismWrapped", { value: true, enumerable: false });
        } catch (e) {
          inst.__prismWrapped = true;
        }
      } catch (e) { /* 包装失败 → 直接返回原实例 */ }
      return inst;
    }

    try {
      var _init = window.echarts.init;
      window.echarts.init = function (dom, theme, opts) {
        /* app.js 只用单参形式; 其余调用形式原样透传, 保持兼容 */
        if (arguments.length <= 1 || theme === undefined || theme === null) {
          return wrapInstance(_init.call(window.echarts, dom, ECHARTS_THEME, opts));
        }
        return wrapInstance(_init.call(window.echarts, dom, theme, opts));
      };
    } catch (e) { /* 包装失败 → ECharts 原样可用 */ }
  }

  /* ======================================================================
     2. 标签页「光谱墨条」
     ====================================================================== */
  function initTabInk() {
    var tabs = document.getElementById("tabs");
    if (!tabs) return;

    var ink = document.createElement("span");
    ink.className = "tab-ink";
    ink.setAttribute("aria-hidden", "true");
    tabs.appendChild(ink);

    var activeTab = function () {
      return tabs.querySelector(".tab.active");
    };

    var place = function () {
      var t = activeTab();
      if (!t) { ink.style.width = "0px"; return; }
      var r = t.getBoundingClientRect();
      var p = tabs.getBoundingClientRect();
      if (!r.width) { return; }
      /* 定位到当前标签所在「行」的下沿, 换行排布时也不会跑到别的行下面 */
      var x = r.left - p.left;
      var y = t.offsetTop + t.offsetHeight + 7;
      ink.style.width = r.width + "px";
      ink.style.transform = "translate(" + x + "px," + y + "px)";
    };

    /* 先无条件摆一次, 再启用滑动过渡, 避免首帧从 (0,0) 滑入 */
    ink.style.transition = "none";
    place();
    requestAnimationFrame(function () {
      ink.style.transition = "";
    });

    tabs.classList.add("js-ink");

    if (typeof MutationObserver !== "undefined") {
      var mo = new MutationObserver(place);
      mo.observe(tabs, { subtree: true, attributes: true, attributeFilter: ["class"] });
    }
    if (typeof ResizeObserver !== "undefined") {
      try { new ResizeObserver(place).observe(tabs); } catch (e) { /* 忽略 */ }
    }
    window.addEventListener("resize", place, { passive: true });
    window.addEventListener("load", place);
    if (document.fonts && document.fonts.ready && document.fonts.ready.then) {
      document.fonts.ready.then(place).catch(function () {});
    }
  }

  /* ======================================================================
     3. 图表随容器尺寸自适应
     ====================================================================== */
  function initChartResize() {
    if (!hasEcharts || typeof echarts.getInstanceByDom !== "function") return;

    var pending = false;
    function flush() {
      pending = false;
      var nodes = document.querySelectorAll("[_echarts_instance_]");
      for (var i = 0; i < nodes.length; i++) {
        try {
          var inst = echarts.getInstanceByDom(nodes[i]);
          if (inst && !inst.isDisposed() && nodes[i].clientWidth > 0) inst.resize();
        } catch (e) { /* 单个图表失败不影响其他 */ }
      }
    }
    function schedule() {
      if (pending) return;
      pending = true;
      requestAnimationFrame(flush);
    }

    window.addEventListener("resize", schedule, { passive: true });

    /* ECharts 初始化时会往容器写 _echarts_instance_, 据此发现新图表 */
    if (typeof ResizeObserver !== "undefined" && typeof MutationObserver !== "undefined") {
      var ro;
      try { ro = new ResizeObserver(schedule); } catch (e) { ro = null; }
      if (ro) {
        try {
          new MutationObserver(function (muts) {
            for (var i = 0; i < muts.length; i++) {
              var el = muts[i].target;
              if (el && el.getAttribute && el.getAttribute("_echarts_instance_")) {
                try { ro.observe(el); } catch (e) { /* 忽略 */ }
              }
            }
          }).observe(document.documentElement, {
            subtree: true, attributes: true, attributeFilter: ["_echarts_instance_"]
          });
        } catch (e) { /* 忽略 */ }
      }
    }
  }

  /* ======================================================================
     启动 (DOM 已就绪: 本脚本置于 body 末尾)
     ====================================================================== */
  function boot() {
    try { initTabInk(); } catch (e) { /* 墨条失败 → 静态下划线仍在 */ }
    try { initChartResize(); } catch (e) { /* 忽略 */ }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
