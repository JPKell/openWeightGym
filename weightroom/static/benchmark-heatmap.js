// Enhance each server-rendered score grid with the suite's existing ECharts library.
// The exact tables remain the source of labels, colours, authorised links and keyboard access.
(() => {
  const maps = [
    { table: "#benchmark-score-table table", chart: "benchmark-heatmap",
      scroll: "benchmark-heatmap-scroll", fallback: "benchmark-score-table" },
  ];

  /** Render an interactive heatmap from the retained score table. */
  function drawMap(config) {
    const table = document.querySelector(config.table);
    const place = document.getElementById(config.chart);
    if (!table || !place || !window.echarts) return;
    const rows = [...table.tBodies[0].rows].filter(row => row.querySelector(".score-cell"));
    if (!rows.length) return;
    const headings = [...table.tHead.rows[0].cells].slice(1);
    /** Read the explicit heatmap label or the table heading text. */
    const headingName = heading => heading.dataset.label || heading.textContent.trim();
    let models = rows.map(row => row.cells[0].textContent.trim());
    const page = getComputedStyle(document.documentElement);
    /** Read a palette color from the current page theme. */
    const colour = name => page.getPropertyValue(name).trim();
    const points = rows.flatMap((row, y) => [...row.querySelectorAll(".score-cell")].map((link, x) => {
      const missing = link.dataset.score === "";
      return {
        value: [x, y, missing ? 0 : Number(link.dataset.score)],
        labelText: link.textContent.trim(), score: missing ? "Not measured" : link.dataset.score,
        href: link.getAttribute("href"),
        rerun: [...link.querySelectorAll("[title]")].map(note => note.title).join(" · "),
        // The server scales each column independently. Missing scores remain grey, even beside zero.
        itemStyle: { color: missing ? colour("--bs-tertiary-bg") : link.style.backgroundColor },
        label: { color: missing ? colour("--bs-secondary-color") : "#101a13" },
      };
    }));
    document.getElementById(config.scroll).hidden = false;
    place.style.minWidth = `${200 + headings.length * 75}px`;
    place.style.height = `${Math.max(1, rows.length) * 26 + 85}px`;
    const chart = echarts.init(place, null, { renderer: "svg" });
    chart.setOption({
      animation: false,
      grid: { left: 200, right: 12, top: 65, bottom: 20 },
      xAxis: {
        type: "category", position: "top", triggerEvent: true,
        data: headings.map(headingName),
        axisLabel: { color: colour("--bs-secondary-color"), interval: 0, fontSize: 11, lineHeight: 12,
          formatter: (name, x) => `${name.replaceAll(" ", "\n")}\n${headings[x].dataset.unit || ""}` },
        axisLine: { show: false }, axisTick: { show: false }, splitArea: { show: false },
      },
      yAxis: {
        type: "category", inverse: true, data: models,
        axisLabel: { color: colour("--bs-body-color"), fontSize: 11, interval: 0, width: 180, overflow: "truncate" },
        axisLine: { show: false }, axisTick: { show: false }, splitArea: { show: false },
      },
      tooltip: {
        confine: true, backgroundColor: colour("--bs-body-bg"), borderColor: colour("--bs-border-color"),
        textStyle: { color: colour("--bs-body-color") },
        formatter: params => {
          const point = params.data, [x, y] = point.value;
          const box = document.createElement("div");
          for (const text of [models[y], headingName(headings[x]), `Score: ${point.score}`,
            `Green: ${headings[x].dataset.green === "low" ? "lower" : "higher"} values`,
            point.rerun, "Click to view run details"].filter(Boolean)) {
            const line = document.createElement("div");
            line.textContent = text;
            box.append(line);
          }
          return box;
        },
      },
      series: [{
        type: "heatmap", data: points,
        label: { show: true, fontWeight: 600, formatter: params => params.data.labelText },
        itemStyle: { borderColor: colour("--bs-body-bg"), borderWidth: 3, borderRadius: 4 },
        emphasis: { itemStyle: { borderColor: colour("--bs-primary"), borderWidth: 2 } },
      }],
    });

    /** Reorder heatmap rows by the selected retained metric. */
    function sortByColumn(column) {
      /** Read the selected row score while preserving unavailable values. */
      const score = index => {
        const raw = rows[index].querySelectorAll(".score-cell")[column].dataset.score;
        return raw === "" ? null : Number(raw);
      };
      const order = rows.map((_, index) => index).sort((a, b) => {
        const left = score(a), right = score(b);
        if (left === null || right === null) return (left === null) - (right === null);
        return headings[column].dataset.green === "low" ? left - right : right - left;
      });
      models = order.map(index => rows[index].cells[0].textContent.trim());
      const positions = new Map(order.map((original, position) => [original, position]));
      chart.setOption({
        xAxis: { data: headings.map(headingName) }, yAxis: { data: models },
        series: [{ data: points.map(point => ({
          ...point, value: [point.value[0], positions.get(point.value[1]), point.value[2]],
        })) }],
      });
      for (const index of order) table.tBodies[0].append(rows[index]);
      headings.forEach((heading, index) => heading.setAttribute("aria-sort", index === column
        ? (heading.dataset.green === "low" ? "ascending" : "descending") : "none"));
    }
    const controls = headings.map(heading => heading.querySelector("a") || heading);
    controls.forEach((control, column) => {
      control.setAttribute("role", "button");
      if (control === headings[column]) control.tabIndex = 0;
      control.title = `Sort ${headingName(headings[column])} best to worst`;
      control.addEventListener("click", event => { event.preventDefault(); sortByColumn(column); });
      control.addEventListener("keydown", event => {
        if (event.key === " " || event.key === "Enter") { event.preventDefault(); control.click(); }
      });
    });
    chart.on("click", params => {
      if (params.componentType === "series") window.location.assign(params.data.href);
      if (params.componentType === "xAxis") {
        const column = headings.findIndex(heading => headingName(heading) === params.value);
        if (column >= 0) controls[column].click();
      }
    });
    new ResizeObserver(() => chart.resize()).observe(place);
    document.addEventListener('weightroom:theme', () => {
      const current = chart.getOption().series[0].data;
      chart.setOption({
        xAxis: {axisLabel: {color: colour('--bs-secondary-color')}},
        yAxis: {axisLabel: {color: colour('--bs-body-color')}},
        tooltip: {backgroundColor: colour('--bs-body-bg'), borderColor: colour('--bs-border-color'),
          textStyle: {color: colour('--bs-body-color')}},
        series: [{
          data: current.map(point => point.score === 'Not measured' ? {...point,
            itemStyle: {color: colour('--bs-tertiary-bg')}, label: {color: colour('--bs-secondary-color')}} : point),
          itemStyle: {borderColor: colour('--bs-body-bg')},
          emphasis: {itemStyle: {borderColor: colour('--bs-primary')}},
        }],
      });
    });
    document.getElementById(config.fallback).classList.add("score-keyboard-grid");
  }

  for (const config of maps) drawMap(config);
})();
