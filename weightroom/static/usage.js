// Draw authorized counts, costs by source or an all-mock comparison. Chart toggles need no request.
(() => {
  const data = document.getElementById("usage-data");
  if (!data || !window.echarts) return;
  const report = JSON.parse(data.textContent);
  const style = getComputedStyle(document.documentElement);
  /** Read a usage-chart color from the active theme. */
  const colour = name => style.getPropertyValue(name).trim();
  /** Format a numeric usage value for compact chart labels. */
  const compact = value => {
    let suffix = "";
    for (const next of ["K", "M", "B", "T"]) {
      if (Math.abs(value) < 999.95) break;
      value /= 1000; suffix = next;
    }
    return suffix ? `${value.toFixed(1)}${suffix}` : String(value);
  };
  const base = {
    animation: false,
    color: [colour("--bs-primary"), "#41b89a", "#d98e32", "#9478d1", "#d76f88", "#46a8cc", "#88ad4e", "#b3805c", "#708cbb", "#aa7ba2", "#5eac91", "#c3aa52"],
    textStyle: { color: colour("--bs-body-color") },
    legend: { type: "scroll", top: 14, textStyle: { color: colour("--bs-body-color") } },
    tooltip: { trigger: "axis", renderMode: "richText", confine: true,
      valueFormatter: value => Number(value).toLocaleString() },
  };
  const axisLabel = { color: colour("--bs-secondary-color") };
  const valuesAxis = { type: "value", min: 0, splitNumber: 4, axisLabel: { ...axisLabel, formatter: compact, hideOverlap: true },
    splitLine: { lineStyle: { color: colour("--bs-border-color") } } };
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
      const tr = document.createElement('tr'), td = document.createElement('td');
      td.colSpan = 10; td.textContent = 'No usage in this range.'; td.className = 'text-secondary'; tr.append(td); table.tBodies[0].append(tr);
    }
    table.tFoot.replaceChildren(cells(report.totals, 'Total'));
    document.getElementById('usage-range-heading').textContent = `Selected range by ${group}`;
    if (typeof sortTable === 'function') sortTable(table);
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
  new ResizeObserver(() => chart.resize()).observe(place);
})();
