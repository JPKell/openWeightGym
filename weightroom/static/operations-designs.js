/* Sample-only operations design review. No network calls or real node actions. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const state = { design: 'ledger', page: 'status', size: 4, scenario: 'normal',
    selected: null, pageIndex: 0, compared: new Set(), tick: 0, nodes: [], hosts: [] };
  const pageSize = 12;
  const notes = {
    ledger: 'Fleet ledger · Scan capacity and exceptions in one compact table. Best for a growing fleet.',
    grid: 'Compact grid · Recognize machines at a glance. Best for a small to medium mixed fleet.',
    focus: 'Focus workspace · Keep the fleet nearby while investigating one node or host.'
  };
  const labels = { ready: 'Available', pressure: 'High load', draining: 'Draining',
    offline: 'Offline', disabled: 'Disabled', stale: 'Stale', unknown: 'Unknown' };
  const colors = { ready: 'green', pressure: 'orange', draining: 'blue', offline: 'red',
    disabled: 'secondary', stale: 'yellow', unknown: 'secondary' };
  const metrics = { cpu: 'CPU load', ram: 'RAM used', gpu: 'GPU load',
    disk: 'Disk used', temp: 'GPU temperature', power: 'GPU power' };
  const now = Date.now();
  let detailChart, compareChart;
  const usable = item => !['offline', 'disabled', 'stale', 'unknown'].includes(item.state);
  const attention = item => ['pressure', 'offline', 'stale', 'unknown'].includes(item.state);
  const unit = key => key === 'temp' ? '°C' : key === 'power' ? ' W' : '%';
  const maximum = key => key === 'power' ? 600 : 100;
  const fmt = (value, key = 'cpu') => value == null ? '—' : `${Math.round(value)}${unit(key)}`;
  const hostOf = item => state.hosts.find(host => host.id === (item.hostId || item.id));
  const current = (item, key) => {
    if (!usable(item)) return null;
    if (['gpu', 'temp', 'power'].includes(key) && !item.gpuCount) return null;
    return item[key];
  };
  function makeFleet() {
    state.hosts = [];
    state.nodes = [];
    state.compared.clear();
    state.selected = null;
    state.pageIndex = 0;
    for (let i = 0; i < state.size; i++) {
      // Two services share the first host; physical capacities are never added twice.
      const hostIndex = i === 1 ? 0 : i === 0 ? 0 : i - 1;
      const hostId = `host-${hostIndex}`;
      let host = state.hosts.find(item => item.id === hostId);
      if (!host) {
        const names = ['atlas', 'orion', 'nova', 'cedar', 'lyra', 'vega'];
        const name = names[hostIndex] || `worker-${String(hostIndex + 1).padStart(2, '0')}`;
        const gpuCount = hostIndex % 5 === 3 ? 0 : hostIndex % 3 === 0 ? 2 : 1;
        let status = hostIndex === 1 ? 'pressure' : hostIndex === 2 ? 'offline' :
          hostIndex > 3 && hostIndex % 7 === 0 ? 'disabled' : 'ready';
        if (state.scenario === 'healthy') status = 'ready';
        if (state.scenario === 'stale' && status !== 'disabled') status = 'stale';
        if (state.scenario === 'outage' && status !== 'disabled') status = 'unknown';
        host = { id: hostId, name, state: status, gpuCount, cores: hostIndex % 2 ? 32 : 24,
          ramGB: hostIndex % 2 ? 128 : 64, diskTB: 2, cpu: status === 'pressure' ? 94 : 22 + hostIndex * 13 % 49,
          ram: status === 'pressure' ? 89 : 38 + hostIndex * 7 % 34,
          gpu: status === 'pressure' ? 98 : 46 + hostIndex * 9 % 38,
          disk: 42 + hostIndex * 7 % 48, temp: 58 + hostIndex * 3 % 22,
          power: 190 + hostIndex * 19 % 140, vramFree: status === 'pressure' ? 2 : 18,
          vramTotal: gpuCount * 24, services: [], running: 0, waiting: 0 };
        state.hosts.push(host);
      }
      const cpuOnly = i === 1 || !host.gpuCount;
      const node = { ...host, id: `node-${i}`, hostId, name: `${host.name}-${cpuOnly ? 'cpu' : 'gpu'}`,
        kind: cpuOnly ? 'CPU' : 'GPU', gpuCount: cpuOnly ? 0 : host.gpuCount,
        vramFree: cpuOnly ? 0 : host.vramFree, vramTotal: cpuOnly ? 0 : host.vramTotal,
        running: usable(host) ? (i === 1 ? 1 : i % 3 + 1) : null,
        waiting: usable(host) ? (host.state === 'pressure' ? 3 : 0) : null,
        models: usable(host) ? [{ name: cpuOnly ? 'Qwen3 · 8B' : i % 2 ? 'Mistral Small · 24B' : 'Qwen3 · 32B', locked: false }] : [] };
      state.nodes.push(node);
      host.services.push(node);
      host.running += node.running || 0;
      host.waiting += node.waiting || 0;
    }
    // Telemetry includes the control-plane host, even before any workers are connected.
    state.hosts.push({ id: 'front', name: 'Control node', state: ['outage', 'stale'].includes(state.scenario) ?
      (state.scenario === 'stale' ? 'stale' : 'unknown') : 'ready', role: 'Control plane',
      gpuCount: 0, cores: 8, ramGB: 16, diskTB: 1, cpu: 12, ram: 32, disk: 28,
      vramTotal: 0, vramFree: 0, services: [], running: 0, waiting: 0 });
  }
  const items = () => state.page === 'status' ? state.nodes : state.hosts;
  function filtered() {
    const search = $('#search').value.trim().toLowerCase();
    const filter = $('#filter').value;
    const rank = { unknown: 0, offline: 1, stale: 2, pressure: 3, draining: 4, ready: 5, disabled: 6 };
    return items().filter(item => {
      const words = [item.name, hostOf(item)?.name, ...(item.models || []).map(model => model.name),
        ...(item.services || []).map(node => `${node.name} ${node.models.map(model => model.name).join(' ')}`)].join(' ').toLowerCase();
      return words.includes(search) && (filter === 'all' ||
        (filter === 'attention' ? attention(item) : filter === 'ready' ? usable(item) && item.state !== 'draining' : item.state === filter));
    }).sort((a, b) => $('#sort').value === 'name' ? a.name.localeCompare(b.name) :
      $('#sort').value === 'load' ? (current(b, 'cpu') ?? -1) - (current(a, 'cpu') ?? -1) :
        rank[a.state] - rank[b.state] || a.name.localeCompare(b.name));
  }
  function badge(element, status) {
    element.textContent = labels[status];
    element.className = `badge bg-${colors[status]}-lt`;
  }
  const age = item => item.state === 'disabled' ? 'Not polled' :
    ['offline', 'stale', 'unknown'].includes(item.state) ? 'Last seen 7m ago' : 'Sample · 4s ago';
  const work = item => !usable(item) ? 'Work unknown' : `${item.running} running · ${item.waiting} queued`;
  const subtitle = item => state.page === 'status' ? `${hostOf(item).name} · ${item.kind} service` :
    `${item.role || `${item.services.length} compute service${item.services.length === 1 ? '' : 's'}`} · ${item.cores} cores`;
  function values(root, item) {
    $$('[data-value]', root).forEach(el => {
      const key = el.dataset.value;
      el.textContent = key === 'gpu' && !item.gpuCount ? 'No GPU' : fmt(current(item, key), key);
    });
    $$('[data-bar]', root).forEach(el => {
      const value = current(item, el.dataset.bar);
      el.style.width = `${value ?? 0}%`;
      el.classList.toggle('bg-orange', value >= 85);
      const progress = el.parentElement;
      if (progress.hasAttribute('role')) {
        if (value == null) progress.removeAttribute('aria-valuenow');
        else progress.setAttribute('aria-valuenow', Math.round(value));
        progress.setAttribute('aria-valuetext', value == null ? 'Unavailable' : `${Math.round(value)}%`);
      }
    });
  }
  function history(item, key, hours) {
    if (['gpu', 'temp', 'power'].includes(key) && !item.gpuCount) return [];
    if (item.state === 'disabled') return [];
    const base = item[key] ?? 0;
    return Array.from({ length: 49 }, (_, i) => {
      const timestamp = now - (48 - i) / 48 * hours * 3600000;
      const gap = i === 19 || i === 20 || (!usable(item) && timestamp > now - 7 * 60000);
      return [timestamp, gap ? null : i === 48 ? base :
        Math.round(Math.max(0, Math.min(maximum(key), base + Math.sin(i * .7 + item.name.length) * (key === 'power' ? 24 : 9))))];
    });
  }
  function spark(el, item) {
    const key = $('#metric').value;
    const samples = history(item, key, +$('#range').value);
    el.replaceChildren();
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 100 28');
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', `${item.name}: ${metrics[key]} history${samples.length ? ', gaps indicate missing samples' : ' unavailable'}`);
    let points = [];
    const segment = () => {
      if (!points.length) return;
      const line = document.createElementNS(svg.namespaceURI, 'polyline');
      line.setAttribute('points', points.join(' '));
      line.setAttribute('stroke', 'currentColor');
      line.setAttribute('stroke-width', '1.5');
      line.setAttribute('fill', 'none');
      svg.append(line);
      points = [];
    };
    samples.forEach((sample, i) => {
      if (sample[1] == null) segment();
      else points.push(`${i / 48 * 100},${26 - sample[1] / maximum(key) * 24}`);
    });
    segment();
    if (samples.length) el.append(svg);
    else el.textContent = '—';
  }
  function message(text) { $('#ops-message').textContent = text; }
  function compare(id) {
    if (state.compared.has(id)) state.compared.delete(id);
    else if (state.compared.size < 3) state.compared.add(id);
    else message('Compare up to 3 physical hosts. Remove one to add another.');
    render();
  }
  function fillItem(element, item) {
    const open = element.matches('[data-open]') ? element : $('[data-open]', element);
    const name = $('[data-name]', element);
    if (name) name.textContent = item.name;
    else open.textContent = item.name;
    open.setAttribute('aria-pressed', String(state.selected === item.id));
    open.setAttribute('aria-label', `Inspect ${item.name}`);
    element.dataset.selected = String(state.selected === item.id);
    open.onclick = () => { state.selected = item.id; render(); };
    $('[data-subtitle]', element).textContent = subtitle(item);
    badge($('[data-state]', element), item.state);
    $('[data-work]', element).textContent = work(item);
    $('[data-age]', element).textContent = age(item);
    const vram = $('[data-vram]', element);
    if (vram) vram.textContent = !item.gpuCount ? 'CPU only' : usable(item) ? `${item.vramFree} / ${item.vramTotal} GiB` : '—';
    values(element, item);
    const trend = $('[data-spark]', element);
    if (trend) spark(trend, item);
    const checkbox = $('[data-compare]', element);
    if (checkbox) {
      checkbox.checked = state.compared.has(item.id);
      checkbox.setAttribute('aria-label', `Compare ${item.name}`);
      checkbox.onchange = () => compare(item.id);
    }
  }
  function renderStats() {
    const list = items();
    const known = list.filter(usable);
    const status = state.page === 'status';
    const stats = status ? [
      ['Available', known.filter(item => item.state !== 'draining').length, `of ${list.length} compute services`],
      ['Running', known.reduce((sum, item) => sum + item.running, 0), 'Requests · fresh reports'],
      ['Waiting', known.reduce((sum, item) => sum + item.waiting, 0), 'Requests · fresh reports'],
      ['Needs attention', list.filter(attention).length, 'Offline, stale, or high load']
    ] : [
      ['Reporting hosts', known.length, `of ${list.length} physical hosts`],
      ['GPU devices', known.reduce((sum, item) => sum + item.gpuCount, 0), 'On reporting hosts · deduplicated'],
      ['RAM installed', `${known.reduce((sum, item) => sum + item.ramGB, 0)} GiB`, 'On reporting hosts · deduplicated'],
      ['Needs attention', list.filter(attention).length, 'Offline, stale, or high load']
    ];
    $$('.ops-stat').forEach((el, i) => {
      el.children[0].textContent = stats[i][0];
      el.children[1].textContent = i !== 3 && !known.length && list.length ? '—' : stats[i][1];
      el.children[2].textContent = stats[i][2];
    });
  }
  function renderDetails(item) {
    $('#inspector').hidden = !item;
    if (!item) return;
    const status = state.page === 'status';
    $('#detail-eyebrow').textContent = status ? 'COMPUTE SERVICE' : 'PHYSICAL HOST';
    $('#detail-title').textContent = item.name;
    $('#detail-subtitle').textContent = subtitle(item);
    badge($('#detail-state'), item.state);
    values($('#detail-content'), item);
    $('#model-section').hidden = !status;
    $('#service-section').hidden = status;
    $('#devices').replaceChildren();
    const devices = [];
    if (item.gpuCount) devices.push({ name: `${item.gpuCount} × RTX 4090`,
      capacity: usable(item) ? `${item.vramFree} / ${item.vramTotal} GiB` : `— / ${item.vramTotal} GiB`,
      note: 'GPU memory · shared host capacity' });
    devices.push({ name: `${item.cores} CPU cores`, capacity: usable(item) ?
      `${Math.round(item.ramGB * (1 - item.ram / 100))} / ${item.ramGB} GiB` : `— / ${item.ramGB} GiB`, note: 'System RAM · physical host' });
    devices.push({ name: 'Storage', capacity: usable(item) ?
      `${(item.diskTB * (1 - item.disk / 100)).toFixed(1)} / ${item.diskTB} TiB` : `— / ${item.diskTB} TiB`, note: 'Host disk · free / total' });
    devices.forEach(device => {
      const el = clone('#device-row');
      $('[data-device]', el).textContent = device.name;
      $('[data-capacity]', el).textContent = device.capacity;
      $('[data-device-note]', el).textContent = device.note;
      $('#devices').append(el);
    });
    $('#models').replaceChildren();
    (item.models || []).forEach(model => {
      const el = clone('#model-row');
      $('[data-model]', el).textContent = model.name;
      $('[data-device]', el).textContent = `${item.kind} · ${model.locked ? 'Kept loaded' : 'Automatic unloading'}`;
      const button = $('[data-lock]', el);
      button.textContent = model.locked ? 'Unlock' : 'Lock';
      button.setAttribute('aria-label', `${model.locked ? 'Unlock' : 'Lock'} ${model.name}`);
      button.setAttribute('aria-pressed', String(model.locked));
      button.onclick = () => { model.locked = !model.locked; message(`Demo: ${model.name} ${model.locked ? 'locked in memory' : 'unlocked'} on ${item.name}.`); renderDetails(item); };
      $('#models').append(el);
    });
    if (!(item.models || []).length) $('#models').textContent = usable(item) ? 'No models loaded.' : 'Loaded models unknown.';
    $('#services').replaceChildren();
    (item.services || []).forEach(node => {
      const button = document.createElement('button');
      button.className = 'btn btn-outline-secondary btn-sm me-2 mb-2';
      button.textContent = `${node.name} · ${labels[node.state]}`;
      button.onclick = () => switchPage('status', node.id);
      $('#services').append(button);
    });
    if (!(item.services || []).length) $('#services').textContent = 'Control plane only · no compute services.';
    const action = $('#node-action');
    action.hidden = !status;
    action.disabled = !usable(item);
    action.textContent = item.state === 'draining' ? 'Resume node' : 'Drain node';
    action.onclick = () => {
      item.state = item.state === 'draining' ? hostOf(item).state : 'draining';
      message(`Demo: ${item.name} ${item.state === 'draining' ? 'is draining. Running requests continue; new work is paused' : 'accepts new work again'}.`);
      render();
    };
    $('#switch-detail').textContent = status ? 'View host telemetry' : 'View compute services';
    $('#switch-detail').disabled = !status && !item.services.length;
    $('#switch-detail').onclick = () => switchPage(status ? 'telemetry' : 'status', status ? item.hostId : item.services[0].id);
    $('#compare-selected').hidden = status;
    $('#compare-selected').textContent = state.compared.has(item.id) ? 'Remove comparison' : 'Compare host';
    $('#compare-selected').onclick = () => compare(item.id);
    $('#action-note').textContent = status ? 'Drain affects this service only. Active requests finish; other services on the host keep running.' :
      'One record per physical host. CPU, RAM and disk readings are shared by its services.';
  }
  function chartOptions(series, key) {
    const style = getComputedStyle(document.documentElement);
    const ink = style.getPropertyValue('--bs-secondary-color').trim() || '#73849a';
    const border = style.getPropertyValue('--bs-border-color').trim() || '#d7dfe8';
    return { animation: false, color: ['#4b87dc', '#26a69a', '#cc8751'],
      textStyle: { color: ink, fontFamily: 'inherit', fontSize: 11 },
      tooltip: { trigger: 'axis', confine: true, valueFormatter: value => fmt(value, key) },
      legend: { show: series.length > 1, bottom: 0, textStyle: { color: ink } },
      grid: { left: 43, right: 12, top: 12, bottom: series.length > 1 ? 55 : 28 },
      xAxis: { type: 'time', axisLabel: { color: ink, hideOverlap: true }, axisLine: { lineStyle: { color: border } }, splitLine: { show: false } },
      yAxis: { type: 'value', min: 0, max: maximum(key), splitNumber: 2, axisLabel: { color: ink, formatter: value => `${value}${unit(key)}` }, splitLine: { lineStyle: { color: border, type: 'dashed' } } },
      series: series.map(item => ({ name: item.name, data: history(item, key, state.page === 'status' ? 1 : +$('#range').value),
        type: 'line', connectNulls: false, showSymbol: false, lineStyle: { width: 2 }, areaStyle: series.length === 1 ? { opacity: .06 } : undefined })) };
  }
  function renderCharts(item) {
    const key = state.page === 'status' ? 'cpu' : $('#metric').value;
    if (item) {
      const unavailable = ['gpu', 'temp', 'power'].includes(key) && !item.gpuCount;
      $('#plot-label').textContent = `${metrics[key]} · ${state.page === 'status' ? '1 hour' : $('#range').selectedOptions[0].textContent}`;
      $('#plot-current').textContent = fmt(current(item, key), key);
      $('#chart-note').textContent = unavailable ? 'No GPU installed or assigned. GPU history is unavailable.' :
        item.state === 'disabled' ? 'Polling disabled. No history in this sample.' : 'Sample history · gaps are missing readings · local time';
      $('#detail-chart').setAttribute('aria-label', `${item.name} ${metrics[key]} history, current ${fmt(current(item, key), key)}`);
      detailChart ??= echarts.init($('#detail-chart'));
      detailChart.setOption(chartOptions([item], key), true);
      detailChart.resize();
    }
    const selected = state.hosts.filter(host => state.compared.has(host.id));
    $('#compare-panel').hidden = state.page !== 'telemetry' || !selected.length;
    if (!$('#compare-panel').hidden) {
      $('#compare-description').textContent = `${metrics[key]} · ${$('#range').selectedOptions[0].textContent} · ${selected.map(host => host.name).join(', ')}. Missing readings remain gaps; hosts without this metric have no line.`;
      compareChart ??= echarts.init($('#compare-chart'));
      compareChart.setOption(chartOptions(selected, key), true);
      compareChart.resize();
    }
  }
  function renderRequests(list) {
    $('#requests-panel').hidden = state.page !== 'status';
    $('#requests').replaceChildren();
    let count = 0;
    list.filter(usable).forEach(node => {
      for (let i = 0; i < node.running && count < 8; i++) {
        const el = clone('#request-row');
        $('[data-request]', el).textContent = `req-${node.id.slice(5)}${i + 1}27`;
        $('[data-node]', el).textContent = node.name;
        $('[data-model]', el).textContent = node.models[0]?.name || '—';
        $('[data-state]', el).textContent = 'Running';
        $('[data-state]', el).className = 'badge bg-blue-lt';
        $('[data-elapsed]', el).textContent = `${12 + i * 16}s`;
        $('#requests').append(el);
        count++;
      }
    });
    if (!count) {
      const el = clone('#empty-requests');
      if (list.some(item => !usable(item))) el.firstElementChild.textContent = 'No fresh active-request reports. Work on offline or stale nodes is unknown.';
      $('#requests').append(el);
    }
    $('#request-caption').textContent = `Showing ${count} of ${list.filter(usable).reduce((sum, node) => sum + node.running, 0)} known running requests in the filtered fleet.`;
  }
  function render() {
    const status = state.page === 'status';
    $('#workspace').dataset.design = state.design;
    $('#workspace').dataset.page = state.page;
    $('#design-note').textContent = notes[state.design];
    $('#ops-heading .page-title').textContent = status ? 'Status' : 'Telemetry';
    $('#fleet-title').textContent = status ? 'Compute nodes' : 'Physical hosts';
    $('#fleet-description').textContent = `${state.nodes.length} compute services · ${state.hosts.length} physical hosts including the control node · ${status ? 'Availability and active work' : 'Host resources and retained history'}`;
    $('#monitor-warning').hidden = state.scenario !== 'outage';
    $('#history-controls').hidden = status;
    $$('button[data-design]').forEach(button => { const active = button.dataset.design === state.design; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
    $$('button[data-page]').forEach(button => { const active = button.dataset.page === state.page; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
    renderStats();
    const list = filtered();
    state.pageIndex = Math.min(state.pageIndex, Math.max(0, Math.ceil(list.length / pageSize) - 1));
    const displayed = list.slice(state.pageIndex * pageSize, (state.pageIndex + 1) * pageSize);
    if (!list.some(item => item.id === state.selected)) state.selected = displayed[0]?.id || null;
    const selected = list.find(item => item.id === state.selected);
    $('#fleet-rows').replaceChildren(); $('#fleet-grid').replaceChildren(); $('#fleet-list').replaceChildren();
    $('#fleet-table-wrap').hidden = state.design !== 'ledger' || !list.length;
    $('#fleet-grid').hidden = state.design !== 'grid' || !list.length;
    $('#fleet-list').hidden = state.design !== 'focus' || !list.length;
    const template = state.design === 'ledger' ? '#fleet-row' : state.design === 'grid' ? '#fleet-tile' : '#fleet-list-item';
    const container = state.design === 'ledger' ? '#fleet-rows' : state.design === 'grid' ? '#fleet-grid' : '#fleet-list';
    displayed.forEach(item => { const el = clone(template); fillItem(el, item); $(container).append(el); });
    $('#empty-fleet').hidden = !!list.length;
    $('#empty-title').textContent = items().length ? 'No matching nodes or hosts' : 'No compute nodes yet';
    $('#empty-description').textContent = items().length ? 'Try a different search or state filter.' : 'Connect a compute node to see availability and workloads. Control node is already visible in Telemetry.';
    $('#clear-filters').hidden = !items().length;
    $('#page-count').textContent = list.length ? `${state.pageIndex * pageSize + 1}–${state.pageIndex * pageSize + displayed.length} of ${list.length} ${status ? 'nodes' : 'hosts'}` : '0 results';
    $('#previous').disabled = !state.pageIndex;
    $('#next').disabled = (state.pageIndex + 1) * pageSize >= list.length;
    renderDetails(selected);
    if (status) renderRequests(list); else $('#requests-panel').hidden = true;
    requestAnimationFrame(() => renderCharts(selected));
  }
  function switchPage(page, id) {
    state.page = page; state.selected = id || null; state.pageIndex = 0;
    $('#search').value = ''; $('#filter').value = 'all';
    render();
  }
  $$('button[data-design]').forEach(button => button.onclick = () => { state.design = button.dataset.design; $('#device-section details').open = state.design === 'focus'; render(); });
  $$('button[data-page]').forEach(button => button.onclick = () => switchPage(button.dataset.page));
  $('#fleet-size').onchange = event => { state.size = +event.target.value; makeFleet(); message(''); render(); };
  $('#scenario').onchange = event => { state.scenario = event.target.value; makeFleet(); message(''); render(); };
  ['search', 'filter', 'sort'].forEach(id => $(`#${id}`).addEventListener('input', () => { state.pageIndex = 0; render(); }));
  ['metric', 'range'].forEach(id => $(`#${id}`).onchange = render);
  $('#previous').onclick = () => { state.pageIndex--; state.selected = null; render(); };
  $('#next').onclick = () => { state.pageIndex++; state.selected = null; render(); };
  $('#clear-filters').onclick = () => { $('#search').value = ''; $('#filter').value = 'all'; render(); };
  $('#clear-compare').onclick = () => { state.compared.clear(); render(); };
  $('#retry').onclick = () => { state.scenario = 'normal'; $('#scenario').value = 'normal'; makeFleet(); message('Demo monitoring connection restored.'); render(); };
  $('#refresh').onclick = () => {
    state.tick++;
    $('#sample-age').textContent = `Sample snapshot refreshed · ${state.tick}`;
    message(state.scenario === 'outage' ? 'Demo refresh failed: monitoring is disconnected. Current values remain unknown.' : 'Sample snapshot refreshed. This preview does not contact your servers.');
    render();
  };
  $('#theme').onclick = () => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark';
    const mode = $('[data-theme-form] select[name="mode"]');
    mode.value = dark ? 'dark' : 'light';
    mode.dispatchEvent(new Event('change', { bubbles: true }));
    $('#theme').textContent = dark ? 'Light mode' : 'Dark mode';
    render();
  };
  $('#reset').onclick = () => {
    state.size = 4; state.scenario = 'normal'; state.design = 'ledger'; state.tick = 0;
    $('#fleet-size').value = '4'; $('#scenario').value = 'normal'; $('#sort').value = 'attention';
    $('#metric').value = 'cpu'; $('#range').value = '1'; $('#sample-age').textContent = 'Snapshot · sample data';
    $('#device-section details').open = false;
    makeFleet(); message(''); switchPage('status');
  };
  new ResizeObserver(() => { detailChart?.resize(); compareChart?.resize(); }).observe($('#workspace'));
  makeFleet(); render();
})();
