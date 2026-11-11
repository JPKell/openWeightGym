/* Live Telemetry workspace. Server-rendered inventory; retained readings only, never sample data. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const state = { identity: null, range: null, group: null, metric: null, zoom: [0, 100], search: '', filter: 'all', sort: 'name' };
  const heatColors = [120, 90, 60, 30, 0].map(hue => `hsl(${hue},60%,42%)`);
  let root, data, groups = [], chart, expanded, observer, opener, selected, dragging = false;
  const format = (value, unit) => value == null ? '—' : `${unit === ' GB' ? value.toFixed(1) : Math.round(value)}${unit}`;
  const clone = id => $(id, root).content.firstElementChild.cloneNode(true);
  const escape = value => String(value).replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  function dispose() {
    chart?.dispose(); expanded?.dispose(); observer?.disconnect();
    chart = expanded = observer = null;
  }
  function filter() {
    state.search = $('#host-search', root).value;
    state.filter = $('#host-filter', root).value;
    state.sort = $('#host-sort', root).value;
    const query = state.search.trim().toLowerCase();
    const matches = element => element.dataset.search.includes(query) && (state.filter === 'all' ||
      (state.filter === 'attention' ? element.dataset.fresh !== 'true' : state.filter === 'reporting' ?
        element.dataset.fresh === 'true' : ['not answering', 'sleeping'].includes(element.dataset.state)));
    ['#host-list', '#host-grid'].forEach(selector => {
      const container = $(selector, root);
      if (!container) return;
      const rows = $$('[data-host]', container);
      rows.sort((a, b) => (state.sort === 'gpus' ? +b.dataset.gpus - +a.dataset.gpus : state.sort === 'attention' ?
        Number(a.dataset.fresh === 'true') - Number(b.dataset.fresh === 'true') : 0) || a.dataset.name.localeCompare(b.dataset.name));
      rows.forEach(row => { row.hidden = !matches(row); container.append(row); });
    });
    const visible = $$('#host-list [data-host]', root).filter(matches).length;
    $('#host-count', root).textContent = visible;
    $('#no-host-match', root).hidden = !!visible;
    if ($('#fleet-empty', root)) $('#fleet-empty', root).hidden = !!visible;
  }
  function prepareGroups() {
    groups = data.groups.map(group => ({ ...group, charts: group.charts.map(item => ({ ...item, lines: [{ name: group.title, ...item.told }] })) }));
    const gpus = groups.filter(group => group.id.startsWith('gpu-'));
    if (gpus.length > 1) {
      const names = [...new Set(gpus.flatMap(group => group.charts.map(item => item.title)))];
      const comparisons = names.map(title => {
        const metrics = gpus.map(group => ({ group, item: group.charts.find(item => item.title === title) })).filter(pair => pair.item);
        return { title, name: title, unit: metrics[0].item.unit, current_value: null, compare: true,
          lines: metrics.map(({ group, item }) => ({ name: group.title, ...item.told, unit: item.unit })) };
      });
      const position = groups.findIndex(group => group.id === 'storage');
      groups.splice(position < 0 ? groups.length : position, 0, { id: 'compare', title: 'Compare GPUs', charts: comparisons });
    }
    if (!groups.some(group => group.id === state.group)) state.group = data.initial_group;
    if (!groups.some(group => group.id === state.group)) state.group = groups[0]?.id;
  }
  function hardwareTabs() {
    const container = $('#viz-tabs', root);
    if (!container) return;
    container.replaceChildren();
    groups.forEach((group, index) => {
      const button = clone('#telemetry-viz-tab');
      button.id = `telemetry-tab-${group.id}`;
      button.textContent = group.title;
      button.removeAttribute('aria-pressed');
      button.setAttribute('aria-selected', String(state.group === group.id));
      button.classList.toggle('active', state.group === group.id);
      button.tabIndex = state.group === group.id ? 0 : -1;
      button.onclick = () => {
        state.group = group.id;
        hardwareTabs(); renderGraph();
        $(`#telemetry-tab-${group.id}`, root).focus({ preventScroll: true });
      };
      button.onkeydown = event => {
        if (!['Home', 'End', 'ArrowLeft', 'ArrowRight'].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? groups.length - 1 :
          (index + (event.key === 'ArrowRight' ? 1 : -1) + groups.length) % groups.length;
        $(`#telemetry-tab-${groups[next].id}`, root).click();
      };
      container.append(button);
    });
    $('#graph-panel', root).setAttribute('aria-labelledby', `telemetry-tab-${state.group}`);
    $$('[data-open-gpu]', root).forEach(button => {
      const active = button.dataset.openGpu === state.group;
      button.setAttribute('aria-pressed', String(active));
      button.closest('tr').dataset.selected = String(active);
    });
  }
  function options(metric, large = false) {
    const style = getComputedStyle(document.documentElement);
    const color = key => style.getPropertyValue(key).trim();
    const local = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    const utc = new Intl.DateTimeFormat(undefined, { dateStyle: 'short', timeStyle: 'medium', timeZone: 'UTC' });
    const tops = metric.lines.map(line => line.top).filter(value => value != null && value > 0);
    const visualMap = [];
    const series = metric.lines.map((line, index) => {
      if (line.heat) {
        const [cool, hot] = line.heat;
        visualMap.push({ show: false, type: 'continuous', dimension: 1, seriesIndex: index,
          min: Math.min(cool, hot), max: Math.max(cool, hot),
          inRange: { color: cool < hot ? heatColors : [...heatColors].reverse() } });
      }
      return { name: line.name, type: 'line', connectNulls: false, symbolSize: 3, showSymbol: true,
        symbol: ['circle', 'rect', 'triangle', 'diamond'][index % 4],
        data: (line.values || []).map((value, point) => [(data.begin + data.step * point) * 1000, value]),
        lineStyle: { width: 1.5, type: ['solid', 'dashed', 'dotted'][index % 3] },
        areaStyle: metric.lines.length === 1 ? { opacity: .18, origin: 'start' } : undefined };
    });
    return { animation: false, color: [color('--bs-primary')],
      textStyle: { color: color('--bs-secondary-color'), fontSize: large ? 12 : 10 },
      tooltip: { trigger: 'axis', confine: true, backgroundColor: color('--bs-body-bg'), borderColor: color('--bs-border-color'), textStyle: { color: color('--bs-body-color') },
        formatter: samples => {
          const points = Array.isArray(samples) ? samples : [samples];
          const at = points[0]?.value?.[0];
          return (at == null ? '' : escape(local.format(at)) + '<br>' + escape(utc.format(at)) + ' UTC<br>') +
            points.map(point => `${escape(point.seriesName)}: ${escape(format(point.value[1], metric.unit))}`).join('<br>');
        } },
      legend: { show: metric.lines.length > 1, type: 'scroll', top: 0, textStyle: { color: color('--bs-secondary-color') } },
      grid: { left: 8, right: 12, top: metric.lines.length > 1 ? 38 : 16, bottom: 46, containLabel: true },
      xAxis: { type: 'time', min: data.begin * 1000, max: data.end * 1000, axisLabel: { color: color('--bs-secondary-color'), hideOverlap: true }, splitLine: { show: false }, axisLine: { lineStyle: { color: color('--bs-border-color') } } },
      yAxis: { type: 'value', min: tops.length ? 0 : null, max: tops.length ? Math.max(...tops) : null, scale: !tops.length, splitNumber: 3,
        axisLabel: { color: color('--bs-secondary-color'), formatter: value => format(value, metric.unit) }, splitLine: { lineStyle: { color: color('--bs-border-color') } } },
      visualMap,
      dataZoom: [{ type: 'inside', filterMode: 'none', start: state.zoom[0], end: state.zoom[1] },
        { type: 'slider', filterMode: 'none', bottom: 2, height: 16, showDetail: false, start: state.zoom[0], end: state.zoom[1] }],
      series };
  }
  function draw(place, metric, large = false) {
    const result = echarts.init(place);
    result.setOption(options(metric, large));
    result.on('datazoom', event => {
      const zoom = event.batch?.[0] || event;
      if (zoom.start != null && zoom.end != null) state.zoom = [zoom.start, zoom.end];
    });
    return result;
  }
  function renderGraph() {
    chart?.dispose(); chart = null; observer?.disconnect();
    const container = $('#graph-grid', root);
    if (!container) return;
    container.replaceChildren();
    const group = groups.find(group => group.id === state.group);
    if (!group) return;
    if (!group.charts.some(metric => metric.title === state.metric)) state.metric = group.charts[0]?.title;
    const choices = $('#metric-choices', root);
    choices.replaceChildren();
    group.charts.forEach(metric => {
      const button = clone('#telemetry-metric-choice');
      button.textContent = metric.title;
      button.setAttribute('aria-pressed', String(metric.title === state.metric));
      button.classList.toggle('active', metric.title === state.metric);
      button.onclick = () => { state.metric = metric.title; renderGraph(); $$('#metric-choices button', root).find(one => one.textContent === state.metric)?.focus({ preventScroll: true }); };
      choices.append(button);
    });
    const metric = group.charts.find(metric => metric.title === state.metric);
    if (!metric) return;
    selected = metric;
    const figure = clone('#telemetry-chart-tile');
    $('[data-title]', figure).textContent = `${group.title} · ${metric.title}`;
    $('[data-current]', figure).textContent = metric.compare ? `${metric.lines.length} devices` : format(metric.current_value, metric.unit);
    const values = metric.lines.flatMap(line => line.values || []).filter(value => value != null);
    const unavailable = metric.lines.filter(line => !(line.values || []).some(value => value != null)).map(line => line.name);
    $('[data-extents]', figure).textContent = values.length ? `${format(Math.min(...values), metric.unit)} – ${format(Math.max(...values), metric.unit)}${unavailable.length ? ` · ${unavailable.join(', ')} unavailable` : ''}` : 'No history in this range';
    const place = $('.tw-plot', figure);
    place.hidden = !values.length;
    place.setAttribute('aria-label', `${group.title} ${metric.title}: current ${metric.compare ? 'multiple devices' : format(metric.current_value, metric.unit)}`);
    $('[data-no-history]', figure).hidden = !!values.length;
    const button = $('[data-expand]', figure);
    button.disabled = !values.length;
    button.setAttribute('aria-label', `Expand ${metric.title}`);
    button.onclick = () => {
      opener = button;
      $('#expanded-title', root).textContent = `${$('#host-title', root).textContent} · ${group.title} · ${metric.title}`;
      $('#expanded-chart', root).setAttribute('aria-label', $('#expanded-title', root).textContent);
      $('#graph-dialog', root).showModal();
      expanded = draw($('#expanded-chart', root), metric, true);
      $('#close-graph', root).focus();
    };
    container.append(figure);
    if (values.length) chart = draw(place, metric);
    observer = new ResizeObserver(() => { chart?.resize(); expanded?.resize(); });
    observer.observe($('.tw-history', root));
    observer.observe($('#main-view', root));
    $('#chart-context', root).textContent = `${Intl.DateTimeFormat().resolvedOptions().timeZone} · retained samples; gaps are missing readings · green to red as usage or temperature rises`;
    $('#reset-zoom', root).disabled = !values.length;
  }
  function initialize() {
    const next = $('#telemetry-workspace');
    if (!next || next === root) return;
    dispose(); root = next;
    data = JSON.parse($('#telemetry-history', root).textContent);
    if (state.identity !== data.identity) { state.group = data.initial_group; state.metric = null; state.zoom = [0, 100]; }
    if (state.range !== root.dataset.range) state.zoom = [0, 100];
    state.identity = data.identity; state.range = root.dataset.range;
    $('#host-search', root).value = state.search;
    $('#host-filter', root).value = state.filter;
    $('#host-sort', root).value = state.sort;
    $('#host-search', root).oninput = filter;
    $('#host-filter', root).onchange = filter;
    $('#host-sort', root).onchange = filter;
    filter(); prepareGroups(); hardwareTabs(); renderGraph();
    $$('[data-open-gpu]', root).forEach(button => button.onclick = () => {
      state.group = button.dataset.openGpu;
      hardwareTabs(); renderGraph();
      $(`#telemetry-tab-${state.group}`, root)?.focus({ preventScroll: true });
    });
    const preset = $('#history-preset', root);
    if (preset) preset.onchange = () => {
      if (!preset.value) return;
      $('#hours', root).value = preset.value; $('#until', root).value = '';
      $('#history-range', root).requestSubmit();
    };
    const reset = $('#reset-zoom', root);
    if (reset) reset.onclick = () => { state.zoom = [0, 100]; chart?.dispatchAction({ type: 'dataZoom', start: 0, end: 100 }); };
    $('#close-graph', root).onclick = () => $('#graph-dialog', root).close();
    $('#graph-dialog', root).addEventListener('close', () => {
      expanded?.dispose(); expanded = null;
      chart?.dispatchAction({ type: 'dataZoom', start: state.zoom[0], end: state.zoom[1] });
      opener?.focus({ preventScroll: true });
    });
  }
  const polling = detail => detail?.requestConfig?.elt?.id === 'telemetry-workspace';
  const busy = () => dragging || $('#graph-dialog', root)?.open ||
    (root?.contains(document.activeElement) && document.activeElement.matches('input, select, textarea'));
  document.addEventListener('pointerdown', event => { if (event.target.closest('.tw-plot')) dragging = true; });
  document.addEventListener('pointerup', () => { dragging = false; });
  document.addEventListener('pointercancel', () => { dragging = false; });
  document.addEventListener('htmx:beforeRequest', event => { if (polling(event.detail) && busy()) event.preventDefault(); });
  document.addEventListener('htmx:beforeSwap', event => {
    if (event.detail.target?.id !== 'telemetry-workspace') return;
    if (polling(event.detail) && busy()) { event.detail.shouldSwap = false; return; }
    if (event.detail.shouldSwap) dispose();
  });
  document.addEventListener('htmx:afterSwap', initialize);
  document.addEventListener('htmx:historyRestore', initialize);
  ['htmx:responseError', 'htmx:sendError'].forEach(name => document.addEventListener(name, event => {
    if (event.detail.target?.id === 'telemetry-workspace' || polling(event.detail)) {
      const notice = $('#telemetry-refresh-error'); if (notice) notice.hidden = false;
    }
  }));
  document.addEventListener('weightroom:theme', () => {
    if (!root?.isConnected || !selected) return;
    chart?.setOption(options(selected), true); expanded?.setOption(options(selected, true), true);
  });
  initialize();
})();
