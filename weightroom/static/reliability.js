// The table remains usable without JavaScript. Charts contain authorized aggregates only.
(() => {
  /** Render retained reliability evidence with the selected model and role filters. */
  function draw() {
    const place = document.getElementById('reliability-chart');
    const data = document.getElementById('reliability-data');
    if (!place || !data || !window.echarts) return;
    const rows = JSON.parse(data.textContent);
    const css = getComputedStyle(document.documentElement);
    const text = css.getPropertyValue('--bs-body-color').trim();
    const chart = echarts.getInstanceByDom(place) || echarts.init(place, null, {renderer: 'svg'});
    chart.setOption({animation: false,
      color: ['#41b89a', '#d76f88', '#d98e32', '#9478d1'],
      textStyle: {color: text},
      tooltip: {trigger: 'axis', renderMode: 'richText', confine: true},
      legend: {top: 0, textStyle: {color: text}},
      grid: {left: 15, right: 15, top: 45, bottom: 70, containLabel: true},
      xAxis: {type: 'category', data: rows.map(r => `${r.model} · ${r.role || 'direct'} · ${r.fingerprint.slice(0, 8)}`), axisLabel: {color: text, width: 130, overflow: 'truncate'}},
      yAxis: {type: 'value', min: 0, minInterval: 1, axisLabel: {color: text}},
      dataZoom: [{type: 'slider', bottom: 0, startValue: 0, endValue: Math.min(rows.length - 1, 5)}, {type: 'inside'}],
      series: [['success', 'Successful'], ['error', 'Errors'], ['timeout', 'Timeouts'], ['empty', 'Empty answers']].map(([key, name]) =>
        ({name, type: 'bar', stack: 'outcomes', barMaxWidth: 70, data: rows.map(r => r.counts[key])}))
    });
  }
  draw();
  document.addEventListener('htmx:afterSettle', event => {
    const place = document.getElementById('reliability-chart');
    if (place && event.detail?.elt?.contains(place)) draw();
  });
  document.addEventListener('mw:theme', draw);
  new MutationObserver(draw).observe(document.documentElement, {attributes: true, attributeFilter: ['data-bs-theme']});
  window.addEventListener('resize', () => {
    const place = document.getElementById('reliability-chart');
    if (place && window.echarts) echarts.getInstanceByDom(place)?.resize();
  });
})();
