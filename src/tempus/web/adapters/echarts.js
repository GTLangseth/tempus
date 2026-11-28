// Panel payload → ECharts, styled per docs/DESIGN.md (Neon Notebook).
// One function per panel type; the only file that knows ECharts.
export const fmt = (v) => (v == null ? "—" : Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2));

const LAG_UNIT = { D: "days", W: "weeks", M: "months", Q: "quarters", Y: "years" };
const activeCharts = new Map(); // chart instance -> panel payload

function base(theme) {
  const c = theme.color;
  return {
    backgroundColor: "transparent",
    color: c.series,
    animationDuration: 300,
    textStyle: { color: c.ink, fontFamily: theme.font.body },
    grid: { left: 52, right: 20, top: 28, bottom: 64 },
    legend: { show: false }, // legends are HTML chips next to the chart
    tooltip: {
      trigger: "axis", backgroundColor: c["tooltip-bg"] ?? c.ink, borderWidth: 0, padding: [6, 10],
      textStyle: { color: c["tooltip-fg"] ?? c.paper, fontFamily: theme.font.mono, fontSize: 11 },
      valueFormatter: fmt,
    },
  };
}

function axis(theme, extra) {
  const c = theme.color;
  return {
    axisLine: { lineStyle: { color: c.hairline } },
    axisTick: { show: false },
    splitLine: { lineStyle: { color: c.grid } },
    axisLabel: { color: c.pencil, fontFamily: theme.font.mono, fontSize: 10 },
    nameTextStyle: { color: c.pencil, fontFamily: theme.font.mono, fontSize: 10 },
    ...extra,
  };
}

function lastPoint(points) {
  for (let i = points.length - 1; i >= 0; i--) if (points[i][1] != null) return points[i];
  return null;
}

const OPTIONS = {
  timeseries(p, theme) {
    const c = theme.color;
    const lines = p.series.map((s, i) => ({
      name: s.label, type: "line", showSymbol: false, connectNulls: false, data: s.points,
      lineStyle: { width: 2, cap: "round", join: "round" },
      itemStyle: { color: c.series[i % c.series.length] },
      emphasis: { disabled: true },
    }));
    // Points are drawn only at the last value: a neon dot with a crisp ring.
    const dots = p.series.map((s, i) => ({
      type: "scatter", silent: true, symbolSize: 8, tooltip: { show: false }, z: 3,
      data: lastPoint(s.points) ? [lastPoint(s.points)] : [],
      itemStyle: { color: c.series[i % c.series.length], borderColor: c["dot-ring"] ?? c.ink, borderWidth: 1.5 },
    }));
    const units = p.series[0]?.units;
    return {
      ...base(theme),
      tooltip: { ...base(theme).tooltip,
                 axisPointer: { type: "line", lineStyle: { color: c.pencil, type: "dashed" } } },
      xAxis: axis(theme, { type: "time", splitLine: { show: false } }),
      yAxis: axis(theme, { type: "value", scale: true, name: units ? units.toUpperCase() : "",
                           nameLocation: "end", nameGap: 12 }),
      dataZoom: [
        { type: "inside" },
        { type: "slider", height: 18, bottom: 14, borderColor: c.hairline,
          backgroundColor: "transparent", fillerColor: c["chart-filler"] ?? "rgba(28, 27, 25, 0.06)",
          dataBackground: { lineStyle: { color: c.pencil, width: 1 }, areaStyle: { color: c.grid } },
          selectedDataBackground: { lineStyle: { color: c.ink, width: 1 }, areaStyle: { color: c.hairline } },
          handleStyle: { color: c.paper, borderColor: c["border-card"] ?? c.ink },
          moveHandleStyle: { color: c["border-card"] ?? c.ink },
          textStyle: { color: c.pencil, fontFamily: theme.font.mono, fontSize: 10 } },
      ],
      series: [...lines, ...dots],
    };
  },

  lagcorr(p, theme) {
    const c = theme.color;
    const peak = p.annotations.peak;
    const rows = p.table.rows;
    const unit = LAG_UNIT[p.annotations.freq] ?? "periods";
    return {
      ...base(theme),
      grid: { left: 52, right: 20, top: 20, bottom: 44 },
      tooltip: { ...base(theme).tooltip, axisPointer: { type: "shadow",
                 shadowStyle: { color: c["chart-shadow"] ?? "rgba(28, 27, 25, 0.05)" } } },
      xAxis: axis(theme, { type: "category", data: rows.map((r) => r.lag),
                           name: `LAG (${unit.toUpperCase()}) · X LEADS Y`,
                           nameLocation: "middle", nameGap: 28, splitLine: { show: false } }),
      yAxis: axis(theme, { type: "value", min: -1, max: 1, name: "R",
                           nameLocation: "middle", nameGap: 36 }),
      series: [{
        type: "bar", barMaxWidth: 28,
        data: rows.map((r) => ({
          value: r.r,
          itemStyle: {
            color: peak && r.lag === peak.lag ? c.primary : c.hairline,
            borderRadius: r.r >= 0 ? [2, 2, 0, 0] : [0, 0, 2, 2],
          },
        })),
      }],
    };
  },

  sparkline(p, theme) {
    const c = theme.color;
    const pts = p.series[0].points;
    return {
      grid: { left: 0, right: 6, top: 6, bottom: 6 },
      xAxis: { type: "time", show: false }, yAxis: { type: "value", show: false, scale: true },
      series: [
        { type: "line", showSymbol: false, data: pts, silent: true,
          lineStyle: { color: c.ink, width: 1.5, cap: "round", join: "round" } },
        { type: "scatter", silent: true, symbolSize: 6, data: lastPoint(pts) ? [lastPoint(pts)] : [],
          itemStyle: { color: c.ink } },
      ],
    };
  },
};

export function renderPanel(el, payload, theme) {
  if (payload.type === "table") {
    const t = payload.table;
    el.innerHTML = `<table><tr>${t.columns.map((c) => `<th>${c}</th>`).join("")}</tr>${
      t.rows.map((r) => `<tr>${t.columns.map((c) => `<td>${
        typeof r[c] === "number" ? fmt(r[c]) : r[c] ?? "—"}</td>`).join("")}</tr>`).join("")
    }</table>`;
    return null;
  }
  if (payload.type === "stat") {
    const s = payload.stat;
    if (!s) return null;
    const arrow = s.change == null ? "" : s.change >= 0 ? "↑ " : "↓ ";
    const change = s.change == null ? "" : ` · ${arrow}${fmt(Math.abs(s.change))}${
      s.change_kind === "yoy" ? "% yoy" : " vs prior"}`;
    // Stat panels are set in ink; the card's single neon figure lives in its header stat row.
    el.innerHTML = `<b>${fmt(s.value)}</b><span class="label">${s.date}${change}</span>
      <div class="chart"></div>`;
    el = el.querySelector(".chart");
    payload = { ...payload, type: "sparkline" };
  }
  const chart = echarts.init(el, null, { renderer: "canvas" });
  chart.setOption(OPTIONS[payload.type](payload, theme));
  const ro = new ResizeObserver(() => chart.resize());
  ro.observe(el);
  activeCharts.set(chart, payload);
  const origDispose = chart.dispose.bind(chart);
  chart.dispose = () => {
    ro.disconnect();
    activeCharts.delete(chart);
    origDispose();
  };
  return chart;
}

export function rethemeCharts(theme) {
  for (const [chart, payload] of activeCharts) {
    if (chart.isDisposed?.()) {
      activeCharts.delete(chart);
      continue;
    }
    chart.setOption(OPTIONS[payload.type](payload, theme), { notMerge: true });
  }
}
