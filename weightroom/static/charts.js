// Draws the charts on a page. ECharts (echarts.min.js, not ours) does the drawing; this says what to draw.
//
// The page holds one <div id="charts" data-times='[...]'>, the moment each step begins, in words.
// Inside it, every <div class="plot" data-chart='{...}'> is one line chart:
//   values  one number for each step, or null where nothing was read: the line bridges missing samples
//   unit    what is written after the number: "%", "°", " W", " GB"
//   top     the most the reading can be, where that is known: the chart is drawn from nought up to it
//   heat    the two values at which the line is fully green and fully red, or null for a line of one colour
//
// When htmx puts a fresh copy of the charts in, for another stretch of time, they are drawn again and the old ones let go of.
let drawn = [];
/** Create or refresh the charts declared by the current page. */
function draw() {
  const holder = document.getElementById("charts");
  if (!holder || !window.echarts) return;
  drawn = drawn.filter((chart) => chart.getDom().isConnected || chart.dispose()); // dispose gives nothing back, so a chart off the page is dropped
  const sourceTimes = JSON.parse(holder.dataset.times);
  const instants = holder.dataset.timeStart && holder.dataset.timeStep ? sourceTimes.map((_, index) =>
    new Date((Number(holder.dataset.timeStart) + Number(holder.dataset.timeStep) * index) * 1000)) : [];
  const options = {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false};
  const local = new Intl.DateTimeFormat(undefined, options);
  const utc = new Intl.DateTimeFormat(undefined, {...options, year: 'numeric', timeZone: 'UTC'});
  const times = instants.length ? instants.map(date => local.format(date)) : sourceTimes;
  const escape = value => String(value).replace(/[&<>"']/g, char =>
    ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  if (instants.length && !holder.querySelector('[data-chart-time-zone]')) {
    const label = document.createElement('p');
    label.dataset.chartTimeZone = '';
    label.className = 'small text-secondary';
    label.textContent = 'Chart times: ' + Intl.DateTimeFormat().resolvedOptions().timeZone;
    label.title = instants[0].toISOString() + ' to ' + instants.at(-1).toISOString();
    holder.prepend(label);
  }
  // Green when low, through amber, to red when high: hues from 120 down to 0, as on the strip's bars.
  const HEAT = [120, 90, 60, 30, 0].map((hue) => `hsl(${hue},60%,42%)`);
  // The page's own colours, so that the charts are right in the light and in the dark.
  const page = getComputedStyle(document.documentElement);
  /** Read a named chart color from the active page theme. */
  const colour = (name) => page.getPropertyValue(name).trim();
  const faint = colour("--bs-secondary-color"), rule = colour("--bs-border-color");

  /** Format a chart measurement with its declared units. */
  const worded = (value, unit) => (value == null ? "—" : (unit === " GB" ? value.toFixed(1) : Math.round(value)) + unit);

  for (const place of holder.querySelectorAll(".plot")) {
    if (echarts.getInstanceByDom(place)) continue; // drawn already: something else on the page was swapped
    const told = JSON.parse(place.dataset.chart);
    const chart = echarts.init(place);
    drawn.push(chart);
    const option = {
      animation: false,
      grid: { left: 4, right: 18, top: 10, bottom: 38, containLabel: true },
      tooltip: {
        trigger: "axis",
        confine: true,
        valueFormatter: (value) => worded(value, told.unit),
        formatter: samples => {
          const points = Array.isArray(samples) ? samples : [samples];
          const index = points[0]?.dataIndex, instant = instants[index];
          const heading = escape(times[index] || '') + (instant ? '<br>' + escape(utc.format(instant)) + ' UTC' : '');
          return heading + '<br>' + points.map(point => escape(worded(point.value, told.unit))).join('<br>');
        },
        backgroundColor: colour("--bs-body-bg"),
        borderColor: rule,
        textStyle: { color: colour("--bs-body-color") },
      },
      xAxis: {
        type: "category",
        data: times,
        boundaryGap: false,
        axisTick: { show: false },
        axisLine: { lineStyle: { color: rule } },
        axisLabel: { color: faint, hideOverlap: true },
      },
      yAxis: {
        type: "value",
        min: told.top ? 0 : null,
        max: told.top || null,
        scale: !told.top, // with no top, from just under the least to just over the most
        splitNumber: 2,
        axisLabel: { color: faint, formatter: (value) => worded(value, told.unit) },
        splitLine: { lineStyle: { color: rule } },
      },
      dataZoom: [
        { type: "inside", filterMode: "none" },
        { type: "slider", bottom: 2, height: 18, filterMode: "none" },
      ],
      // Bridge missed samples so sparse telemetry remains a line, and shade the area down to the chart baseline.
      series: [{ type: "line", data: told.values, connectNulls: true, symbolSize: 2, showAllSymbol: true,
        lineStyle: { width: 1.5 }, areaStyle: { opacity: 0.18, origin: "start" }, color: colour("--bs-primary") }],
    };
    if (told.heat) {
      const [cool, hot] = told.heat; // cool above hot means the reading is the other way up, as room on a drive is
      option.visualMap = { show: false, type: "continuous", dimension: 1, min: Math.min(cool, hot), max: Math.max(cool, hot), inRange: { color: cool < hot ? HEAT : [...HEAT].reverse() } };
    }
    chart.setOption(option);
    new ResizeObserver(() => chart.resize()).observe(place);
  }
}
draw();
document.addEventListener("htmx:afterSwap", draw);
