// Panel payload → ECharts. One function per panel type; the only file that knows ECharts.
const fmt = (v) => (v == null ? "—" : Math.abs(v) >= 100 ? v.toFixed(0) : v.toFixed(2));

function base(theme) {
  const c = theme.color;
  return {
    backgroundColor: "transparent",
    color: c.series,
    textStyle: { color: c.text, fontFamily: theme.font.family },
    grid: { left: 56, right: 24, top: 32, bottom: 64 },
    tooltip: { trigger: "axis", backgroundColor: c.panel, borderColor: c.border,
               textStyle: { color: c.text }, valueFormatter: fmt },
    legend: { top: 0, textStyle: { color: c.muted } },
  };
}

const axis = (theme, extra) => ({
  axisLine: { lineStyle: { color: theme.color.grid } },
  splitLine: { lineStyle: { color: theme.color.grid } },
  axisLabel: { color: theme.color.muted },
  ...extra,
});

const OPTIONS = {
  timeseries(p, theme) {
    return {
      ...base(theme),
      tooltip: { ...base(theme).tooltip, axisPointer: { type: "cross" } },
      xAxis: axis(theme, { type: "time" }),
      yAxis: axis(theme, { type: "value", scale: true }),
      dataZoom: [{ type: "inside" }, { type: "slider", height: 20, bottom: 16 }],
      series: p.series.map((s) => ({
        name: s.label, type: "line", showSymbol: false, connectNulls: false,
        data: s.points,
      })),
    };
  },
  lagcorr(p, theme) {
    const peak = p.annotations.peak;
    const rows = p.table.rows;
    return {
      ...base(theme),
      legend: { show: false },
      xAxis: axis(theme, { type: "category", name: "lag", data: rows.map((r) => r.lag) }),
      yAxis: axis(theme, { type: "value", name: "r", min: -1, max: 1 }),
      series: [{
        type: "bar",
        data: rows.map((r) => ({
          value: r.r,
          itemStyle: { color: peak && r.lag === peak.lag ? theme.color.accent : theme.color.grid },
        })),
      }],
    };
  },
  sparkline(p, theme) {
    return {
      grid: { left: 0, right: 0, top: 4, bottom: 4 },
      xAxis: { type: "time", show: false }, yAxis: { type: "value", show: false, scale: true },
      series: [{ type: "line", showSymbol: false, data: p.series[0].points,
                 lineStyle: { color: theme.color.accent } }],
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
    el.classList.add("stat");
    el.innerHTML = s
      ? `<div class="value">${fmt(s.value)}</div><div class="sub">${s.date}${
          s.change == null ? "" : ` · ${s.change >= 0 ? "+" : ""}${fmt(s.change)}${
            s.change_kind === "yoy" ? "% YoY" : " vs prior"}`}</div><div class="chart"></div>`
      : "";
    if (!s) return null;
    el = el.querySelector(".chart");
    payload = { ...payload, type: "sparkline" };
  }
  const chart = echarts.init(el, null, { renderer: "canvas" });
  chart.setOption(OPTIONS[payload.type](payload, theme));
  new ResizeObserver(() => chart.resize()).observe(el);
  return chart;
}
