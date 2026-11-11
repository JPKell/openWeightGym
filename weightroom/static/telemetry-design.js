/* Refined Telemetry proposal. All inventory and history are simulated, with no API calls. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const state = { hosts: [], selected: null, tab: 'processor', metric: null, hours: 1, zoom: [0, 100] };
  const now = Date.now();
  const palette = ['#6399ec', '#39b8a5', '#e9ac52', '#bf8de7', '#e67895', '#7fc269', '#89cde0', '#c8b77a'];
  let charts = [], expanded, expandedSpec, opener;
  const fresh = host => host.state === 'reporting';
  const read = (host, value) => fresh(host) ? value : null;
  const fmt = (value, unit = '%') => value == null ? '—' : `${unit === ' GiB' ? value.toFixed(1) : Math.round(value)}${unit}`;
  const statusText = { reporting: 'Reporting', offline: 'Offline', stale: 'Stale' };
  const hostAge = host => fresh(host) ? 'Sample · 4s ago' : host.state === 'offline' ? 'Last seen 12m ago' : 'Last seen 7m ago';
  const selectedHost = () => state.hosts.find(host => host.id === state.selected);
  function makeFleet() {
    const count = +$('#fleet-size').value;
    const scenario = $('#scenario').value;
    state.hosts = Array.from({ length: count }, (_, index) => {
      const front = count > 1 && index === count - 1;
      const name = front ? 'front' : ['atlas', 'orion', 'nova', 'cedar', 'lyra', 'vega'][index] || `worker-${String(index + 1).padStart(2, '0')}`;
      const gpuCount = front || index % 7 === 3 ? 0 : scenario === 'dense' ? 8 : index === 1 ? 4 : 2;
      const gpuModel = index === 0 ? 'RTX A4000' : 'RTX 4090';
      const total = index === 0 ? 16 : 24;
      return { id: `host-${index}`, name, cores: front ? 8 : index % 2 ? 32 : 24,
        services: front ? ['Front'] : index === 0 ? ['GPU compute', 'CPU compute'] : ['Compute'],
        state: scenario === 'stale' ? 'stale' : scenario === 'healthy' ? 'reporting' : index === 2 ? 'offline' : 'reporting',
        cpu: front ? 12 : index === 0 ? 1 : 31 + index * 13 % 49,
        cpuTemp: front ? null : index === 0 ? 39 : 48 + index * 4 % 25,
        ram: front ? 5.1 : index === 0 ? 12.7 : 42 + index * 7 % 46,
        ramTotal: front ? 16 : index === 0 ? 32 : 128,
        disks: [{ name: 'Root · /', used: 124, total: 512 }, ...(front ? [] : [{ name: 'Library · /models', used: 740 + index * 33, total: 2048 }])],
        gpus: Array.from({ length: gpuCount }, (_, gpuIndex) => ({
          slot: index === 4 ? gpuIndex * 2 : gpuIndex, name: gpuModel, total,
          memory: index === 0 ? 13.7 - gpuIndex * .5 : Math.min(total, 9.2 + gpuIndex * 3.2),
          load: index === 0 ? 0 : (52 + index * 11 + gpuIndex * 13) % 100,
          temp: index === 0 ? 30 + gpuIndex : 54 + gpuIndex * 7,
          power: index === 0 ? 6 - gpuIndex * 2 : 188 + gpuIndex * 31,
          powerLimit: index === 0 ? 140 : 450,
          fan: index === 0 ? 0 : gpuIndex === 3 ? null : 35 + gpuIndex * 8
        })) };
    });
    state.selected = null;
    state.tab = 'processor';
    state.zoom = [0, 100];
    render();
  }
  function badge(element, host) {
    element.textContent = statusText[host.state];
    element.className = `badge bg-${fresh(host) ? 'green' : host.state === 'offline' ? 'red' : 'yellow'}-lt`;
  }
  function totals(container, entries) {
    container.replaceChildren();
    entries.forEach(([label, value, note]) => {
      const element = clone('#summary-value');
      $('[data-label]', element).textContent = label;
      $('[data-value]', element).textContent = value;
      $('[data-note]', element).textContent = note;
      container.append(element);
    });
  }
  function gpuRows(container, host) {
    container.replaceChildren();
    host.gpus.forEach(gpu => {
      const element = clone('#gpu-row');
      element.dataset.device = `${host.id}-gpu-${gpu.slot}`;
      element.dataset.selected = String(host.id === state.selected && state.tab === `gpu-${gpu.slot}`);
      const open = $('[data-gpu-open]', element);
      open.textContent = `GPU ${gpu.slot}`;
      open.setAttribute('aria-label', `Inspect ${host.name} GPU ${gpu.slot}`);
      open.onclick = () => selectHost(host.id, `gpu-${gpu.slot}`);
      $('[data-gpu-name]', element).textContent = gpu.name;
      $('[data-gpu-load]', element).textContent = fmt(read(host, gpu.load));
      const bar = $('[data-gpu-bar]', element);
      bar.style.width = `${read(host, gpu.load) ?? 0}%`;
      bar.classList.toggle('bg-orange', gpu.load >= 85 && fresh(host));
      $('[data-gpu-memory]', element).textContent = fmt(read(host, gpu.memory), ' GiB');
      $('[data-gpu-capacity]', element).textContent = `/ ${gpu.total} GiB`;
      $('[data-gpu-temp]', element).textContent = fmt(read(host, gpu.temp), '°C');
      container.append(element);
    });
  }
  function inventory() {
    const query = $('#host-search').value.trim().toLowerCase();
    const filter = $('#host-filter').value;
    const hosts = state.hosts.filter(host => `${host.name} ${host.services.join(' ')} ${host.gpus.map(gpu => `GPU ${gpu.slot} ${gpu.name}`).join(' ')}`.toLowerCase().includes(query) &&
      (filter === 'all' || (filter === 'attention' ? !fresh(host) : host.state === filter)))
      .sort((a, b) => ($('#host-sort').value === 'attention' ? Number(fresh(a)) - Number(fresh(b)) :
        $('#host-sort').value === 'gpus' ? b.gpus.length - a.gpus.length : 0) || a.name.localeCompare(b.name));
    const devices = hosts.flatMap(host => host.gpus);
    $('#host-total').textContent = state.hosts.length;
    $('#host-count').textContent = hosts.length === state.hosts.length ? String(hosts.length) : `${hosts.length}/${state.hosts.length}`;
    $('#inventory-total').textContent = `${hosts.length} hosts · ${devices.length} GPUs`;
    $('#all-hosts').setAttribute('aria-pressed', String(state.selected == null));
    $('#host-list').replaceChildren();
    $('#host-grid').replaceChildren();
    hosts.forEach(host => {
      const link = clone('#host-link');
      link.setAttribute('aria-pressed', String(host.id === state.selected));
      link.setAttribute('aria-label', `Inspect host ${host.name}, ${statusText[host.state]}, ${host.gpus.length} GPUs`);
      $('[data-name]', link).textContent = host.name;
      $('[data-subtitle]', link).textContent = `${host.gpus.length ? `${host.gpus.length} GPUs` : 'CPU only'} · ${statusText[host.state]}`;
      $('[data-indicator]', link).dataset.state = host.state;
      link.onclick = () => selectHost(host.id);
      $('#host-list').append(link);
      const tile = clone('#host-card');
      tile.dataset.host = host.id;
      $('[data-name]', tile).textContent = host.name;
      $('[data-host-open]', tile).setAttribute('aria-label', `Open ${host.name} telemetry`);
      $('[data-host-open]', tile).onclick = () => selectHost(host.id);
      $('[data-subtitle]', tile).textContent = `${host.cores} cores · ${host.ramTotal} GiB RAM`;
      badge($('[data-state]', tile), host);
      const diskUsed = host.disks.reduce((sum, disk) => sum + disk.used, 0);
      const diskTotal = host.disks.reduce((sum, disk) => sum + disk.total, 0);
      const fields = [['CPU', fmt(read(host, host.cpu))], ['RAM', fmt(read(host, host.ram), ' GiB')], ['Disk', fmt(read(host, 100 * diskUsed / diskTotal))]];
      fields.forEach(([label, value]) => { const part = document.createElement('span'); part.textContent = `${label} ${value}`; $('[data-host-summary]', tile).append(part); });
      gpuRows($('[data-gpu-rows]', tile), host);
      $('table', tile).hidden = !host.gpus.length;
      $('[data-no-gpu]', tile).hidden = !!host.gpus.length;
      $('[data-count]', tile).textContent = `${host.gpus.length} GPUs · ${host.services.length} service${host.services.length === 1 ? '' : 's'}`;
      $('[data-age]', tile).textContent = hostAge(host);
      $('#host-grid').append(tile);
    });
    const allDevices = state.hosts.flatMap(host => host.gpus);
    totals($('#fleet-totals'), [
      ['Hosts reporting', `${state.hosts.filter(fresh).length} / ${state.hosts.length}`, 'Fleet · fresh telemetry'],
      ['GPU devices', allDevices.length, 'Fleet · all discovered devices'],
      ['VRAM installed', `${allDevices.reduce((sum, gpu) => sum + gpu.total, 0)} GiB`, 'Includes offline inventory'],
      ['Needs attention', state.hosts.filter(host => !fresh(host)).length, 'Offline or stale hosts']
    ]);
    $('#no-host-match').hidden = !!hosts.length;
    $('#fleet-empty').hidden = !!hosts.length;
    $('#empty-title').textContent = state.hosts.length ? 'No matching hosts' : 'No hosts registered';
    $('#empty-note').textContent = state.hosts.length ? 'Try a different host name or GPU model.' : 'Hardware will appear here when a host reports telemetry.';
    $('#inventory-note').textContent = 'One entry per physical host';
  }
  function selectHost(id, tab = 'processor') {
    state.selected = id;
    state.tab = tab;
    state.metric = null;
    state.zoom = [0, 100];
    render();
    if (innerWidth <= 600) $('#main-view').scrollIntoView({ block: 'start' });
  }
  function tabs(host) {
    const choices = [['processor', 'Processor & memory'], ...host.gpus.map(gpu => [`gpu-${gpu.slot}`, `GPU ${gpu.slot}`]),
      ...(host.gpus.length > 1 ? [['compare', 'Compare GPUs']] : []), ['storage', 'Storage']];
    $('#viz-tabs').replaceChildren();
    choices.forEach(([key, label]) => {
      const button = clone('#viz-tab');
      button.id = `tab-${key}`;
      button.dataset.tab = key;
      button.textContent = label;
      button.removeAttribute('aria-pressed');
      const active = state.tab === key;
      button.classList.toggle('active', active);
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
      button.onclick = () => {
        state.tab = key;
        tabs(host);
        gpuRows($('#host-view [data-gpu-rows]'), host);
        renderGraphs();
        $(`#tab-${key}`).focus({ preventScroll: true });
      };
      button.onkeydown = event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const index = choices.findIndex(([value]) => value === key);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? choices.length - 1 :
          (index + (event.key === 'ArrowRight' ? 1 : -1) + choices.length) % choices.length;
        $(`#tab-${choices[next][0]}`).click();
      };
      $('#viz-tabs').append(button);
    });
    $('#graph-panel').setAttribute('aria-labelledby', `tab-${state.tab}`);
  }
  function hostDetails(host) {
    $('#host-title').textContent = host.name;
    $('#host-subtitle').textContent = `${host.cores} CPU cores · ${host.ramTotal} GiB RAM · ${host.gpus.length} GPUs · ${host.services.join(' + ')}`;
    badge($('#host-state'), host);
    $('#stale-notice').hidden = fresh(host);
    $('#stale-note').textContent = `${hostAge(host)}. Current readings are unknown; retained history and device inventory remain visible.`;
    const diskTotal = host.disks.reduce((sum, disk) => sum + disk.total, 0);
    const diskUsed = host.disks.reduce((sum, disk) => sum + disk.used, 0);
    totals($('#host-totals'), [
      ['CPU load', fmt(read(host, host.cpu)), `${host.cores} cores`],
      ['CPU temperature', fmt(read(host, host.cpuTemp), '°C'), host.cpuTemp == null ? 'Sensor unavailable' : 'Processor sensor'],
      ['Memory used', fmt(read(host, host.ram), ' GiB'), `of ${host.ramTotal} GiB`],
      ['Disk used', fmt(read(host, 100 * diskUsed / diskTotal)), `${host.disks.length} volumes · ${(diskTotal / 1024).toFixed(1)} TiB total`]
    ]);
    const percentages = [host.cpu, host.cpuTemp == null ? null : host.cpuTemp / 110 * 100, host.ram / host.ramTotal * 100, diskUsed / diskTotal * 100];
    $$('#host-totals .tw-total').forEach((element, index) => {
      const progress = $('[data-reading-progress]', element);
      const value = read(host, percentages[index]);
      progress.hidden = false;
      progress.setAttribute('aria-label', $('[data-label]', element).textContent);
      progress.setAttribute('aria-valuetext', $('[data-value]', element).textContent);
      if (value != null) progress.setAttribute('aria-valuenow', Math.round(value));
      progress.firstElementChild.style.width = `${value ?? 0}%`;
    });
    $('#gpu-total').textContent = `${host.gpus.length} devices · ${host.gpus.reduce((sum, gpu) => sum + gpu.total, 0)} GiB total VRAM`;
    gpuRows($('#host-view [data-gpu-rows]'), host);
    $('#host-view .tw-gpus').hidden = !host.gpus.length;
    $('#cpu-only').hidden = !!host.gpus.length;
    $('#capacity-list').replaceChildren();
    const capacities = [[`${host.cores} CPU cores`, `${host.ramTotal} GiB system RAM`],
      ...host.disks.map(disk => [disk.name, `${fresh(host) ? (disk.total - disk.used).toFixed(0) : '—'} / ${disk.total} GiB free`])];
    capacities.forEach(([name, value]) => {
      const element = clone('#capacity-item');
      $('[data-capacity-name]', element).textContent = name;
      $('[data-capacity-value]', element).textContent = value;
      $('#capacity-list').append(element);
    });
    tabs(host);
  }
  function series(name, value, seed = 1) { return { name, value, seed }; }
  function specs(host) {
    if (state.tab === 'processor') return [
      { title: 'Processor load', unit: '%', max: 100, series: [series('CPU', host.cpu)] },
      { title: 'Processor temperature', unit: '°C', max: 110, series: [series('CPU', host.cpuTemp)] },
      { title: 'Memory used', unit: ' GiB', max: host.ramTotal, series: [series('RAM', host.ram)] }
    ];
    if (state.tab === 'storage') return host.disks.map((disk, i) => ({ title: disk.name, unit: ' GiB', max: disk.total, series: [series('Used', disk.used, i)] }));
    const gpus = state.tab === 'compare' ? host.gpus : host.gpus.filter(gpu => `gpu-${gpu.slot}` === state.tab);
    return [['memory', 'Memory used', ' GiB'], ['load', 'Load', '%'], ['temp', 'Temperature', '°C'], ['power', 'Power', ' W'], ['fan', 'Fan', '%']].map(([key, title, unit]) => ({
      title, unit, max: key === 'memory' ? Math.max(...gpus.map(gpu => gpu.total)) : key === 'power' ? Math.max(...gpus.map(gpu => gpu.powerLimit)) : key === 'temp' ? 110 : 100,
      series: gpus.map(gpu => series(`GPU ${gpu.slot}`, gpu[key], gpu.slot + 1))
    }));
  }
  function samples(host, spec, line) {
    return Array.from({ length: 97 }, (_, index) => {
      const timestamp = now - (96 - index) / 96 * state.hours * 3600000;
      const lastSeen = host.state === 'offline' ? 12 : 7;
      const missing = line.value == null || index === 38 || index === 39 || (!fresh(host) && timestamp > now - lastSeen * 60000);
      const amplitude = line.value === 0 ? 0 : spec.unit === ' GiB' ? spec.max * .035 : spec.unit === ' W' ? 15 : 7;
      const wave = Math.sin(index * .21 + line.seed) * amplitude + Math.sin(index * .59 + line.seed) * amplitude * .25;
      const value = index === 96 ? line.value : Math.max(0, Math.min(spec.max, line.value + wave));
      return [timestamp, missing ? null : Math.round(value * 10) / 10];
    });
  }
  function option(host, spec, large = false) {
    const style = getComputedStyle(document.documentElement);
    const color = name => style.getPropertyValue(name).trim();
    return { animation: false, color: palette,
      textStyle: { color: color('--bs-secondary-color'), fontSize: large ? 12 : 10 },
      tooltip: { trigger: 'axis', confine: true, valueFormatter: value => fmt(value, spec.unit),
        backgroundColor: color('--tblr-bg-surface'), borderColor: color('--bs-border-color'), textStyle: { color: color('--bs-body-color') } },
      legend: { show: spec.series.length > 1, type: 'scroll', top: 0, textStyle: { color: color('--bs-secondary-color'), fontSize: 10 }, pageIconColor: color('--bs-secondary-color') },
      grid: { left: 4, right: 8, top: spec.series.length > 1 ? 32 : 14, bottom: 40, containLabel: true },
      xAxis: { type: 'time', min: now - state.hours * 3600000, max: now, axisLabel: { color: color('--bs-secondary-color'), hideOverlap: true, fontSize: large ? 11 : 9 }, axisLine: { lineStyle: { color: color('--bs-border-color') } }, splitLine: { show: false } },
      yAxis: { type: 'value', min: 0, max: spec.max, splitNumber: 2, axisLabel: { color: color('--bs-secondary-color'), fontSize: large ? 11 : 9, formatter: value => `${Math.round(value)}${spec.unit}` }, splitLine: { lineStyle: { color: color('--bs-border-color') } } },
      dataZoom: [{ type: 'inside', filterMode: 'none', start: state.zoom[0], end: state.zoom[1] },
        { type: 'slider', filterMode: 'none', bottom: 2, height: 14, start: state.zoom[0], end: state.zoom[1], showDetail: false, borderColor: color('--bs-border-color'), textStyle: { color: color('--bs-secondary-color') } }],
      series: spec.series.map(line => ({ name: line.name, type: 'line', data: samples(host, spec, line), connectNulls: false,
        showSymbol: false, lineStyle: { width: 1.5 }, areaStyle: spec.series.length === 1 ? { opacity: .1 } : undefined })) };
  }
  function teardown() {
    charts.forEach(chart => chart.dispose());
    charts = [];
  }
  function draw(place, host, spec, large = false) {
    const chart = echarts.init(place);
    chart.setOption(option(host, spec, large));
    chart.on('datazoom', event => {
      const zoom = event.batch?.[0] || event;
      if (zoom.start != null && zoom.end != null) state.zoom = [zoom.start, zoom.end];
    });
    return chart;
  }
  function expand(spec, button) {
    expandedSpec = spec;
    opener = button;
    const host = selectedHost();
    $('#expanded-title').textContent = `${host.name} · ${spec.series.map(line => line.name).join(', ')} · ${spec.title}`;
    $('#expanded-note').textContent = $('#chart-context').textContent;
    $('#expanded-chart').setAttribute('aria-label', $('#expanded-title').textContent);
    $('#graph-dialog').showModal();
    expanded = draw($('#expanded-chart'), host, spec, true);
    $('#close-graph').focus();
  }
  function renderGraphs() {
    teardown();
    $('#graph-grid').replaceChildren();
    const host = selectedHost();
    if (!host) return;
    const gpu = host.gpus.find(gpu => `gpu-${gpu.slot}` === state.tab);
    $('#graph-title').textContent = gpu ? `GPU ${gpu.slot} · ${gpu.name} · ${gpu.total} GiB` :
      state.tab === 'compare' ? `All ${host.gpus.length} GPUs · one line per device` : state.tab === 'storage' ? 'Storage volumes' : 'Processor and memory';
    $('#graph-note').textContent = fresh(host) ? 'Current readings in each graph header' : 'Current values unknown · retained samples shown';
    $('#chart-context').textContent = `${$('#range').selectedOptions[0].textContent} · ${Intl.DateTimeFormat().resolvedOptions().timeZone} · gaps indicate missing samples · drag a graph to zoom`;
    const options = specs(host);
    if (!options.some(spec => spec.title === state.metric)) state.metric = options[0].title;
    $('#metric-choices').replaceChildren();
    options.forEach(spec => {
      const button = clone('#metric-choice');
      button.textContent = spec.title;
      const active = spec.title === state.metric;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
      button.onclick = () => {
        state.metric = spec.title;
        renderGraphs();
        $$('#metric-choices button').find(item => item.textContent === spec.title)?.focus({ preventScroll: true });
      };
      $('#metric-choices').append(button);
    });
    options.filter(spec => spec.title === state.metric).forEach(spec => {
      const element = clone('#chart-tile');
      $('[data-title]', element).textContent = spec.title;
      $('[data-current]', element).textContent = spec.series.length === 1 ? fmt(read(host, spec.series[0].value), spec.unit) : `${spec.series.length} devices`;
      const valid = spec.series.flatMap(line => samples(host, spec, line).map(point => point[1])).filter(value => value != null);
      const unavailable = spec.series.filter(line => line.value == null).map(line => line.name);
      $('[data-extents]', element).textContent = valid.length ? `${fmt(Math.min(...valid), spec.unit)} – ${fmt(Math.max(...valid), spec.unit)}${unavailable.length ? ` · ${unavailable.join(', ')} unavailable` : ''}` : 'Sensor unavailable';
      const plot = $('.tw-plot', element);
      plot.setAttribute('aria-label', `${host.name} ${spec.series.map(line => line.name).join(', ')} ${spec.title}: ${$('[data-current]', element).textContent}`);
      const button = $('[data-expand]', element);
      button.setAttribute('aria-label', `Expand ${spec.title}`);
      button.onclick = () => expand(spec, button);
      $('#graph-grid').append(element);
      charts.push(draw(plot, host, spec));
    });
  }
  function render() {
    inventory();
    const host = selectedHost();
    $('#fleet-view').hidden = !!host;
    $('#host-view').hidden = !host;
    if (host) { hostDetails(host); renderGraphs(); }
    else teardown();
  }
  function allHosts() { state.selected = null; render(); }
  $('#all-hosts').onclick = allHosts;
  $('#back-to-grid').onclick = () => { allHosts(); $('#all-hosts').focus({ preventScroll: true }); };
  $('#host-search').oninput = inventory;
  $('#host-filter').onchange = inventory;
  $('#host-sort').onchange = inventory;
  $('#fleet-size').onchange = makeFleet;
  $('#scenario').onchange = makeFleet;
  $('#range').onchange = () => { state.hours = +$('#range').value; state.zoom = [0, 100]; renderGraphs(); };
  $('#reset-zoom').onclick = () => { state.zoom = [0, 100]; charts.forEach(chart => chart.dispatchAction({ type: 'dataZoom', start: 0, end: 100 })); };
  $('#close-graph').onclick = () => $('#graph-dialog').close();
  $('#graph-dialog').addEventListener('close', () => {
    expanded?.dispose(); expanded = null; expandedSpec = null;
    charts.forEach(chart => chart.dispatchAction({ type: 'dataZoom', start: state.zoom[0], end: state.zoom[1] }));
    opener?.focus({ preventScroll: true });
  });
  function setTheme(mode) {
    const select = $('[data-theme-form] select[name="mode"]');
    select.value = mode;
    select.dispatchEvent(new Event('change', { bubbles: true }));
    $('#theme').textContent = mode === 'dark' ? 'Light mode' : 'Dark mode';
    if (selectedHost()) renderGraphs();
    if (expanded) expanded.setOption(option(selectedHost(), expandedSpec, true), true);
  }
  $('#theme').onclick = () => setTheme(document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark');
  const resize = new ResizeObserver(() => { charts.forEach(chart => chart.resize()); expanded?.resize(); });
  resize.observe($('#main-view'));
  resize.observe($('.tw-history'));
  const shellHeader = $('body > header');
  new ResizeObserver(() => document.documentElement.style.setProperty('--top', `${shellHeader.offsetHeight}px`)).observe(shellHeader);
  // Keep application navigation reviewable without leaving the sample or submitting forms.
  $$('a[href^="/"]').forEach(link => link.addEventListener('click', event => {
    event.preventDefault();
    if (link.getAttribute('href') === '/telemetry') {
      $('#mobile-navigation')?.close();
      allHosts();
    } else $('#preview-note').textContent = `${link.textContent.trim()} is shown for navigation context. This preview stays on Telemetry.`;
  }));
  $$('form[action]').forEach(form => form.addEventListener('submit', event => event.preventDefault()));
  // Set the review appearance without saving account preferences or contacting services.
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => setTheme('dark'), { once: true });
  else setTheme('dark');
  makeFleet();
})();
