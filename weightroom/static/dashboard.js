// Personal token totals from the same authorized report as the Usage page.
(() => {
  const data = document.getElementById('home-usage-data');
  const place = document.getElementById('home-usage-chart');
  if (!data || !place || !window.echarts) return;
  const report = JSON.parse(data.textContent);
  const points = report.bucket === 'hour' ? report.hourly : report.daily;
  const labels = points.map(point => report.bucket === 'hour' ? point.label : point.date);
  const chart = echarts.init(place, null, {renderer: 'svg'});
  /** Render the dashboard's retained metrics using the current theme. */
  function draw() {
    const style = getComputedStyle(document.documentElement);
    /** Read a dashboard color from the active CSS palette. */
    const color = name => style.getPropertyValue(name).trim();
    chart.setOption({
      animation: false,
      color: [color('--bs-primary'), color('--bs-success')],
      textStyle: {color: color('--bs-body-color')},
      legend: {top: 0, textStyle: {color: color('--bs-body-color')}},
      tooltip: {trigger: 'axis', renderMode: 'richText', confine: true,
        valueFormatter: value => Number(value).toLocaleString() + ' tokens'},
      grid: {left: 12, right: 16, top: 45, bottom: 52, containLabel: true},
      xAxis: {type: 'category', data: labels, boundaryGap: false,
        axisLabel: {color: color('--bs-secondary-color'), hideOverlap: true}},
      yAxis: {type: 'value', min: 0,
        axisLabel: {color: color('--bs-secondary-color'), formatter: value =>
          Intl.NumberFormat(undefined, {notation: 'compact'}).format(value)},
        splitLine: {lineStyle: {color: color('--bs-border-color')}}},
      dataZoom: [{type: 'inside', filterMode: 'none'},
        {type: 'slider', bottom: 4, height: 18, filterMode: 'none'}],
      series: [['input_tokens', 'Input tokens'], ['output_tokens', 'Output tokens']].map(([key, name]) => ({
        name, type: 'line', showSymbol: points.length < 60,
        data: points.map(point => point[key]),
      })),
    }, true);
  }
  draw();
  new ResizeObserver(() => chart.resize()).observe(place);
  document.addEventListener('weightroom:theme', draw);
})();
