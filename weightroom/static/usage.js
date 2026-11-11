// Draw authorized counts, costs by source or an all-mock comparison. Chart toggles need no request.
/** Initialize controls after the initial document or an independently loaded section arrives. */
function initializeUsage() {
  const data = document.getElementById("usage-data");
  if (!data || !window.echarts || data.dataset.initialized) return;
  data.dataset.initialized = "true";
  const report = JSON.parse(data.textContent);
  /** Read a usage-chart color from the active theme. */
  const colour = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  /** Format a numeric usage value for compact chart labels. */
  const compact = value => {
    let suffix = "";
    for (const next of ["K", "M", "B", "T"]) {
      if (Math.abs(value) < 999.95) break;
      value /= 1000; suffix = next;
    }
    return suffix ? `${value.toFixed(1)}${suffix}` : String(value);
  };
  let base, axisLabel, valuesAxis;
  /** Take the chart's colors from the theme in use; again whenever the theme changes. */
  function paint() {
  base = {
    animation: false,
    color: [colour("--bs-primary"), "#41b89a", "#d98e32", "#9478d1", "#d76f88", "#46a8cc", "#88ad4e", "#b3805c", "#708cbb", "#aa7ba2", "#5eac91", "#c3aa52"],
    textStyle: { color: colour("--bs-body-color") },
    legend: { type: "scroll", top: 14, textStyle: { color: colour("--bs-body-color") } },
    tooltip: { trigger: "axis", renderMode: "richText", confine: true,
      valueFormatter: value => Number(value).toLocaleString() },
  };
  axisLabel = { color: colour("--bs-secondary-color") };
  valuesAxis = { type: "value", min: 0, splitNumber: 4, axisLabel: { ...axisLabel, formatter: compact, hideOverlap: true },
    splitLine: { lineStyle: { color: colour("--bs-border-color") } } };
  }
  paint();
  const place = document.getElementById("usage-chart");
  const chart = echarts.init(place, null, { renderer: "svg" });
  let view = "trend";
  /** Read the selected token or cost reporting metric. */
  const metric = () => document.getElementById("usage-metric").value;
  /** Return whether the selected metric uses currency units. */
  const isCost = () => metric() !== "tokens";
  /** Choose the retained input/output fields for the selected metric. */
  const seriesFields = () => metric() === "cost"
    ? [["recorded_cost", "Recorded"], ["model_cost", "Model-rate estimate"], ["fallback_cost", "Mock fallback"]]
    : metric() === "mock" ? [["input_cost", "Mock input cost"], ["output_cost", "Mock output cost"]]
    : [["input_tokens", "Input tokens"], ["output_tokens", "Output tokens"]];
  /** Format a finite cost value in dollars with useful precision. */
  const money = value => "$" + Number(value).toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 6 });
  const tableFields = ['requests', 'input_tokens', 'output_tokens', 'total_tokens', 'recorded_cost', 'model_cost', 'fallback_cost', 'effective_cost', 'missing_usage'];
  /** Render exact usage cells for the selected report row. */
  function cells(row, name) {
    const tr = document.createElement('tr'), heading = document.createElement('th');
    heading.scope = 'row'; heading.textContent = name; tr.append(heading);
    for (const key of tableFields) {
      const td = document.createElement('td'); td.className = 'text-end'; td.dataset.value = row[key];
      td.textContent = key.endsWith('_cost') ? money(row[key]) : Number(row[key]).toLocaleString();
      if (key.endsWith('_cost')) td.title = `$${Number(row[key]).toFixed(6)} USD`;
      tr.append(td);
    }
    return tr;
  }
  /** Update the exact usage table for the selected metric. */
  function table() {
    const group = document.getElementById('usage-group-by').value;
    const table = document.getElementById('usage-breakdown'), rows = report.breakdowns[group];
    table.tHead.rows[0].cells[0].textContent = group[0].toUpperCase() + group.slice(1);
    table.tBodies[0].replaceChildren(...rows.map(row => cells(row, row.name)));
    if (!rows.length) {
      const tr = document.getElementById('empty-row-template').content.firstElementChild.cloneNode(true);
      tr.cells[0].colSpan = 10; tr.cells[0].textContent = 'No usage in this range.'; table.tBodies[0].append(tr);
    }
    table.tFoot.replaceChildren(cells(report.totals, 'Total'));
    document.getElementById('usage-range-heading').textContent = `Selected range by ${group}`;
    if (typeof sortTable === 'function') sortTable(table);
    shares();
  }
  /** Shorten a count to a whole number or 1.2K, 3.4M. */
  const short = value => Math.abs(value) < 999.95 ? String(Math.round(value)) : compact(value);
  const percent = (part, whole) => whole ? `${(100 * part / whole).toFixed(part / whole < 0.1 ? 1 : 0)}%` : "—";
  /** A filled trend line of one daily figure, drawn into an inline SVG. */
  function spark(svg, values) {
    const top = Math.max(...values, 1), step = values.length > 1 ? 120 / (values.length - 1) : 120;
    const points = values.map((value, index) => `${(index * step).toFixed(2)},${(30 - 28 * value / top).toFixed(2)}`);
    const ns = "http://www.w3.org/2000/svg", area = document.createElementNS(ns, "path"), line = document.createElementNS(ns, "polyline");
    area.setAttribute("d", `M0,32 L${points.join(" L")} L120,32 Z`); area.setAttribute("class", "usage-spark-area");
    line.setAttribute("points", points.join(" ")); line.setAttribute("class", "usage-spark-line");
    svg.replaceChildren(area, line);
  }
  /** Fill a bar with one segment per part, each as wide as its share. */
  function segments(bar, parts) {
    const whole = parts.reduce((sum, part) => sum + part.value, 0);
    bar.replaceChildren(...parts.filter(part => part.value > 0).map(part => {
      const segment = document.getElementById("usage-segment-template").content.firstElementChild.cloneNode(true);
      segment.dataset.source = part.key; segment.style.inlineSize = `${100 * part.value / whole}%`;
      segment.title = `${part.label}: ${part.text} · ${percent(part.value, whole)}`;
      return segment;
    }));
    bar.setAttribute("aria-label", whole ? parts.map(part => `${part.label} ${percent(part.value, whole)}`).join(", ") : "Nothing to divide in this range");
  }
  /** What each figure means per request or per day, its trend, the share of each cost source, and how many filters are on. */
  function figures() {
    const totals = report.totals, days = report.daily.length || 1;
    const perRequest = key => totals.requests ? short(totals[key] / totals.requests) : "—";
    const said = {
      input_tokens: `${perRequest("input_tokens")} per request`,
      output_tokens: `${perRequest("output_tokens")} per request · ${percent(totals.output_tokens, totals.total_tokens)} of tokens`,
      total_tokens: `${short(totals.total_tokens / days)} a day on average`,
      requests: `${short(totals.requests / days)} a day · ${percent(totals.missing_usage, totals.requests)} missing counts`,
    };
    for (const element of document.querySelectorAll("[data-derived]")) element.textContent = said[element.dataset.derived] ?? "";
    for (const element of document.querySelectorAll("[data-spark]")) spark(element, report.daily.map(day => day[element.dataset.spark]));
    for (const element of document.querySelectorAll("[data-share]")) element.textContent = ` ${percent(totals[element.dataset.share], totals.effective_cost)} of total`;
    const dollars = value => "$" + Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    for (const bar of document.querySelectorAll("[data-composition]")) segments(bar, [["recorded", "Recorded"], ["model", "Model-rate estimate"], ["fallback", "Mock fallback"]]
      .map(([key, label]) => ({ key, label, value: totals[`${key}_cost`], text: dollars(totals[`${key}_cost`]) })));
    for (const bar of document.querySelectorAll("[data-split]")) segments(bar, [
      { key: "input", label: "Input", value: totals.input_tokens, text: short(totals.input_tokens) },
      { key: "output", label: "Output", value: totals.output_tokens, text: short(totals.output_tokens) }]);
    // Someone who sees only their own usage is not filtering by user.
    const used = Object.entries(report.filters).filter(([key, value]) => value && !(key === "user" && !report.everyone)).length;
    for (const badge of document.querySelectorAll("[data-filter-count]")) badge.textContent = used ? `${used} on` : "None";
  }
  /** Each breakdown row's share of all tokens, as a thin bar under its name. */
  function shares() {
    const table = document.querySelector("#usage-breakdown.usage-table-shares");
    if (!table) return;
    for (const row of table.tBodies[0].rows) {
      const name = row.cells[0];
      if (row.hasAttribute("data-empty-row") || name.querySelector(".usage-share")) continue;
      const bar = document.createElement("span"); bar.className = "usage-share";
      const total = Number(row.cells[4]?.dataset.value || 0), whole = report.totals.total_tokens;
      bar.style.setProperty("--share", whole ? `${100 * total / whole}%` : "0");
      name.append(bar);
    }
  }
  /** Preserve current usage filters and metric in the page URL. */
  function bookmark() {
    const url = new URL(location.href);
    for (const key of ['input_rate', 'output_rate', 'group_by']) url.searchParams.set(key, document.querySelector(`[name="${key}"]`).value);
    history.replaceState(null, '', url);
  }
  /** Apply the selected mock pricing rates to the comparison view. */
  function rates() {
    const input = document.getElementById('usage-input-rate'), output = document.getElementById('usage-output-rate');
    if (!input.checkValidity() || !output.checkValidity()) return;
    const inputRate = Number(input.value), outputRate = Number(output.value);
    const all = [report.totals, ...report.daily, ...Object.values(report.breakdowns).flat(), ...report.user_models];
    for (const row of all) {
      row.input_cost = row.input_tokens * inputRate / 1e6;
      row.output_cost = row.output_tokens * outputRate / 1e6;
      row.total_cost = row.input_cost + row.output_cost;
      row.fallback_cost = row.fallback_input_tokens * inputRate / 1e6 + row.fallback_output_tokens * outputRate / 1e6;
      row.effective_cost = row.recorded_cost + row.model_cost + row.fallback_cost;
    }
    for (const element of document.querySelectorAll('[data-usage-total]')) {
      const key = element.dataset.usageTotal;
      if (key.endsWith('_cost')) {element.textContent = money(report.totals[key]); element.title = `$${Number(report.totals[key]).toFixed(6)} USD`;}
    }
    table();
    document.getElementById('usage-daily').tBodies[0].replaceChildren(...report.daily.map(row => cells(row, row.date)));
    bookmark(); draw();
  }
  document.getElementById('usage-group-by').addEventListener('change', () => {table(); bookmark();});
  for (const side of ['input', 'output']) document.getElementById(`usage-${side}-rate`).addEventListener('input', rates);
  /** Build shared usage-chart options with the active tooltip formatter. */
  const chartBase = () => ({ ...base, tooltip: { ...base.tooltip,
    valueFormatter: value => isCost() ? money(value) + " USD" : Number(value).toLocaleString() } });
  /** Build an axis formatter appropriate to tokens or dollars. */
  const metricAxis = () => ({ ...valuesAxis, axisLabel: { ...valuesAxis.axisLabel,
    formatter: value => isCost() ? money(value) : compact(value) } });
  /** Return the operator-visible title for the selected usage metric. */
  const metricTitle = () => metric() === "cost" ? "Cost (USD)" : metric() === "mock" ? "All-mock comparison (USD)" : "Tokens";

  /** Refresh usage charts and exact tables from retained report data. */
  function draw() {
    const trend = view === "trend";
    document.getElementById("usage-lines-control").classList.toggle("d-none", !trend || isCost());
    document.getElementById("usage-mode-control").classList.toggle("d-none", !trend);
    document.getElementById("usage-stack-control").classList.toggle("d-none", view !== "user");
    if (trend) drawTrend(); else drawBars();
    chart.resize();
    figures();
    // A page can show more of the same report beside the chart, kept current with it and with the mock rates.
    place.dispatchEvent(new CustomEvent("weightroom:usage", {bubbles: true, detail: {report, view, metric: metric()}}));
  }

  /** Render daily or cumulative usage trends for the selected metric. */
  function drawTrend() {
    place.style.height = "340px";
    const cumulative = document.getElementById("usage-mode").value === "cumulative";
    const total = !isCost() && document.getElementById("usage-lines").value === "total";
    const lines = total ? [["total_tokens", "Total tokens"]] : seriesFields();
    chart.setOption({
      ...chartBase(), grid: { left: isCost() ? 95 : 65, right: 20, top: 55, bottom: 70 },
      xAxis: { type: "category", data: report.daily.map(day => day.date), boundaryGap: false, axisLabel },
      yAxis: metricAxis(),
      dataZoom: [{ type: "inside", filterMode: "none" },
        { type: "slider", bottom: 5, height: 18, filterMode: "none" }],
      series: lines.map(([key, name]) => {
        let sum = 0;
        return { name, type: "line", showSymbol: report.daily.length < 60,
          data: report.daily.map(day => { sum += day[key]; return cumulative ? sum : day[key]; }) };
      }),
    }, true);
    document.getElementById("usage-chart-heading").textContent = `${metricTitle()} over time`;
    place.setAttribute("aria-label",
      `${cumulative ? "Cumulative" : "Daily"} ${isCost() ? "costs in USD" : total ? "total tokens" : "input and output tokens"}.`);
  }

  /** Render ranked usage breakdown bars for the selected dimension. */
  function drawBars() {
    const key = metric() === "cost" ? "effective_cost" : metric() === "mock" ? "total_cost" : "total_tokens";
    const rows = [...report.breakdowns[view]].sort((a, b) => b[key] - a[key] || a.name.localeCompare(b.name));
    const stacked = view === "user" && document.getElementById("usage-stack").checked;
    let series;
    if (stacked) {
      const models = report.breakdowns.model.map(row => row.name);
      const byUser = new Map();
      for (const row of report.user_models) {
        if (!byUser.has(row.user)) byUser.set(row.user, new Map());
        byUser.get(row.user).set(row.model, row[key]);
      }
      series = models.map(model => ({ name: model, type: "bar", stack: "usage",
        data: rows.map(row => byUser.get(row.name)?.get(model) ?? 0), barMaxWidth: 24 }));
    } else {
      const fields = isCost() ? seriesFields() : [["total_tokens", "Total tokens"]];
      series = fields.map(([field, name]) => ({ name, type: "bar", stack: "usage",
        data: rows.map(row => row[field]), barMaxWidth: 24 }));
    }
    place.style.height = `${Math.max(340, Math.min(640, rows.length * 32 + 90))}px`;
    chart.setOption({
      ...chartBase(), grid: { left: 12, right: 45, top: 55, bottom: 25, containLabel: true },
      xAxis: metricAxis(),
      yAxis: { type: "category", inverse: true, data: rows.map(row => row.name),
        axisLabel: { ...axisLabel, width: 180, overflow: "truncate" } },
      dataZoom: rows.length > 15
        ? [{ type: "slider", yAxisIndex: 0, filterMode: "empty", startValue: 0, endValue: 14, width: 15, right: 5 }] : [],
      series,
    }, true);
    const title = `${metricTitle()} by ${view}${stacked ? " · stacked by model" : ""}`;
    document.getElementById("usage-chart-heading").textContent = title;
    place.setAttribute("aria-label", `${title} for the selected date range.`);
  }

  for (const button of document.querySelectorAll("[data-usage-view]")) {
    button.addEventListener("click", () => {
      view = button.dataset.usageView;
      for (const pill of document.querySelectorAll("[data-usage-view]")) {
        const selected = pill === button;
        pill.classList.toggle("active", selected);
        pill.setAttribute("aria-pressed", String(selected));
      }
      draw();
    });
  }
  for (const id of ["usage-mode", "usage-lines", "usage-metric", "usage-stack"]) {
    document.getElementById(id).addEventListener("change", draw);
  }
  draw();
  shares();
  new ResizeObserver(() => chart.resize()).observe(place);
  document.addEventListener("weightroom:theme", () => { if (place.isConnected) { paint(); draw(); } });
}
initializeUsage();
document.addEventListener('htmx:afterSwap', initializeUsage);
