/* Isolated Status design review. No API calls or live control actions. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const text = (root, selector, value) => $(selector, root).textContent = value;
  const notes = { focus: 'Telemetry companion · current work above a focused node workspace. Recommended.', ledger: 'Dense overview · scan every request, node and loaded model without selecting a node.', lanes: 'Dispatch view · running and queued work side by side, with capacity grouped into node cards.' };
  let design = ['focus', 'ledger', 'lanes'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'focus';
  let nodes = [], requests = [], selected = 0, pending, opener;
  const state = node => !node.online ? 'Offline' : node.paused ? 'Paused' : 'Accepting';
  function badge(element, label) {
    element.textContent = label;
    element.className = 'badge ' + (['Accepting', 'Processing', 'In use'].includes(label) ? 'bg-green-lt' : ['Queued', 'Paused', 'Unload queued'].includes(label) ? 'bg-orange-lt' : 'bg-secondary-lt');
  }
  const work = (node, status) => requests.filter(request => request.node === node.id && (!status || request.state === status));
  const scoped = () => nodes.filter(node => $('#control-scope').value === 'all' || String(node.id) === $('#control-scope').value);
  const gib = value => value == null ? '—' : value.toFixed(1) + ' GiB';
  function seed() {
    const scenario = $('#scenario').value;
    nodes = Array.from({ length: +$('#fleet-size').value }, (_, i) => {
      const cpu = i % 4 === 2, offline = scenario === 'offline' || (scenario === 'mixed' && i % 4 === 3);
      const models = cpu ? [{ name: 'BGE-M3', cards: [], vram: 0, rss: 2.4 }] : i % 4 === 0 ?
        [{ name: 'Qwen3 14B', cards: [0], vram: 10.4, rss: 1.8 }, { name: 'Gemma 3 12B', cards: [1], vram: 9.2, rss: 1.4 }] :
        [{ name: 'Qwen3 30B-A3B', cards: [0, 1], vram: 32.4, rss: 3.6 }];
      return { id: i, name: ['atlas-gpu', 'orion-gpu', 'cedar-cpu', 'nova-gpu'][i] || `worker-${String(i + 1).padStart(2, '0')}`,
        online: !offline, paused: false, unloading: false, cpu: cpu ? 28 : 17 + i % 3 * 13,
        ram: cpu ? 64 : 128, used: cpu ? 12.4 : 24.6, gpuTotal: cpu ? 0 : i % 4 === 0 ? 16 : 24,
        models: offline ? [] : models, received: scenario === 'idle' ? [0, 12, 69] : [18 + i * 3, 52 + i * 7, 190 + i * 13] };
    });
    requests = [];
    if (scenario === 'mixed') nodes.filter(node => node.online).forEach(node => {
      const cpu = !node.gpuTotal;
      const states = cpu ? ['Processing'] : node.id % 4 === 0 ? ['Processing', 'Processing', 'Queued'] : ['Processing', 'Processing', 'Queued', 'Queued'];
      states.forEach((state, i) => requests.push({ id: 8241 + requests.length, node: node.id,
        model: node.models[i % node.models.length].name, user: ['jules', 'batch / evaluation', 'morgan'][i % 3],
        state, elapsed: [42, 18, 9, 6][i] }));
    });
    selected = nodes[0]?.id;
    const scope = $('#control-scope'); scope.replaceChildren(new Option('All nodes', 'all'));
    nodes.forEach(node => scope.add(new Option(node.name, node.id)));
    $('#search').value = '';
    $('#feedback').textContent = 'Fleet totals stay visible when filtering. Rolling counts are requests received, including those still active.';
    render();
  }
  function showRequests() {
    const query = $('#search').value.toLowerCase();
    const filtered = requests.filter(request => [request.id, request.model, request.user, nodes[request.node].name].join(' ').toLowerCase().includes(query));
    $$('[data-requests]').forEach(body => {
      body.replaceChildren();
      const rows = filtered.filter(request => body.dataset.requests === 'all' || request.state === (body.dataset.requests === 'running' ? 'Processing' : 'Queued'));
      rows.forEach(request => {
        const row = clone('#sd-request-row');
        text(row, '[data-id]', '#' + request.id); text(row, '[data-user]', request.user);
        text(row, '[data-model]', request.model); text(row, '[data-node]', nodes[request.node].name); text(row, '[data-request-node]', nodes[request.node].name);
        badge($('[data-state]', row), request.state); text(row, '[data-elapsed]', request.elapsed + ' s');
        const stop = $('[data-stop]', row); stop.textContent = request.state === 'Queued' ? 'Cancel' : 'Stop';
        stop.setAttribute('aria-label', `${stop.textContent} request ${request.id}`);
        stop.onclick = () => confirm('Stop request #' + request.id, 'This sample request will be removed. Other requests continue.', () => {
          requests = requests.filter(item => item.id !== request.id); finishUnloads();
        }, stop);
        body.append(row);
      });
      if (!rows.length) body.append(clone('#sd-empty-requests'));
    });
    $('#request-total').textContent = `${filtered.length} visible · ${requests.filter(r => r.state === 'Processing').length} processing · ${requests.filter(r => r.state === 'Queued').length} queued`;
  }
  function modelRows(body, list) {
    body.replaceChildren();
    list.forEach(node => node.models.forEach(model => {
      const row = clone('#sd-model-row');
      text(row, '[data-model]', model.name); text(row, '[data-node]', node.name);
      text(row, '[data-device]', model.cards.length ? 'GPU ' + model.cards.join(' + ') : 'CPU');
      text(row, '[data-vram]', model.cards.length ? gib(model.vram) : '—'); text(row, '[data-rss]', gib(model.rss));
      badge($('[data-work]', row), node.unloading ? 'Unload queued' : work(node).some(r => r.model === model.name) ? 'In use' : 'Idle');
      body.append(row);
    }));
    if (!body.children.length) {
      const row = clone('#sd-empty-models');
      if (list.some(node => !node.online)) row.firstElementChild.textContent = 'Loaded model readings unavailable.';
      body.append(row);
    }
  }
  function capacity(node) {
    const tile = clone('#sd-capacity'); text(tile, '[data-name]', node.name); badge($('[data-state]', tile), state(node));
    const devices = $('[data-devices]', tile);
    const meter = (label, total, allocated, other, note) => {
      const row = clone('#sd-meter'); text(row, '[data-label]', label);
      text(row, '[data-value]', node.online ? `${gib(total - allocated - other)} / ${total} GiB free` : 'Reading unavailable');
      const bar = $('[role="progressbar"]', row);
      bar.setAttribute('aria-label', `${node.name} ${label} used`);
      if (node.online) bar.setAttribute('aria-valuenow', Math.round((allocated + other) / total * 100));
      $('[data-model-bar]', row).style.width = (node.online ? allocated / total * 100 : 0) + '%';
      $('[data-other-bar]', row).style.width = (node.online ? other / total * 100 : 0) + '%';
      text(row, '[data-note]', node.online ? note : 'Last contact 12 min ago · controls unavailable'); devices.append(row);
    };
    if (node.gpuTotal) for (let i = 0; i < 2; i++) {
      const allocation = node.models.filter(model => model.cards.includes(i)).reduce((sum, model) => sum + model.vram / model.cards.length, 0);
      meter('GPU ' + i + ' · VRAM', node.gpuTotal, allocation, 1.2, `${gib(allocation)} models · 1.2 GiB other`);
    }
    meter('Host RAM', node.ram, 0, node.used, 'System memory · includes model processes');
    text(tile, '[data-cpu]', node.online ? `CPU ${node.cpu}% · ${work(node, 'Processing').length} processing · ${work(node, 'Queued').length} queued` : 'CPU and request counts unknown');
    return tile;
  }
  function render() {
    $('#sd-workspace').dataset.design = design; $('#design-note').textContent = notes[design];
    $$('[data-design]').filter(button => button.tagName === 'BUTTON').forEach(button => {
      button.classList.toggle('active', button.dataset.design === design); button.setAttribute('aria-pressed', String(button.dataset.design === design));
    });
    const online = nodes.filter(node => node.online), unavailable = nodes.some(node => !node.online);
    const suffix = unavailable && online.length ? '+' : '';
    ['running', 'queued'].forEach((key, i) => $(`[data-kpi="${key}"]`).textContent = !online.length && nodes.length ? '—' : requests.filter(request => request.state === (i ? 'Queued' : 'Processing')).length + suffix);
    ['five', 'fifteen', 'sixty'].forEach((key, i) => $(`[data-kpi="${key}"]`).textContent = !online.length && nodes.length ? '—' : online.reduce((sum, node) => sum + node.received[i], 0) + suffix);
    $('#capacity-summary').textContent = `${online.length} / ${nodes.length} nodes reporting · ${online.reduce((sum,node) => sum + node.models.length, 0)} known loaded models${unavailable ? ' · + indicates a partial count' : ''}`;
    showRequests();
    $('#request-ledger').hidden = design === 'lanes'; $('#request-lanes').hidden = design !== 'lanes';
    $('#node-navigator').hidden = $('#focus-panel').hidden = design !== 'focus';
    $('#capacity-ledger').hidden = design !== 'ledger'; $('#capacity-grid').hidden = design !== 'lanes';
    const query = $('#search').value.toLowerCase();
    const visible = nodes.filter(node => [node.name, ...node.models.map(model => model.name), ...work(node).map(r => `${r.id} ${r.user}`)].join(' ').toLowerCase().includes(query));
    if (!visible.some(node => node.id === selected)) selected = visible[0]?.id;
    $('#empty-nodes').hidden = !!visible.length;
    if (!visible.length) $('#focus-panel').hidden = true;
    $('#node-list').replaceChildren(); $('#capacity-list').replaceChildren(); $('#capacity-grid').replaceChildren();
    visible.forEach(node => {
      const button = clone('#sd-node-link'); text(button, '[data-name]', node.name); badge($('[data-state]', button), state(node));
      text(button, '[data-work]', node.online ? `${work(node, 'Processing').length} processing · ${work(node, 'Queued').length} queued` : 'Readings unavailable');
      button.setAttribute('aria-pressed', String(node.id === selected));
      button.onclick = () => { selected = node.id; $('#control-scope').value = node.id; render(); };
      $('#node-list').append(button); $('#capacity-list').append(capacity(node));
      const tile = clone('#sd-node-card'); $('[data-capacity]', tile).append(capacity(node)); modelRows($('[data-models]', tile), [node]);
      const pause = $('[data-node-pause]', tile); pause.textContent = node.paused ? 'Resume intake' : 'Pause intake'; pause.disabled = !node.online;
      pause.onclick = () => action('pause', [node], pause);
      $('[data-node-control]', tile).onclick = () => { $('#control-scope').value = node.id; selected = node.id; render(); $('#pause').focus(); };
      $('#capacity-grid').append(tile);
    });
    modelRows($('#capacity-ledger [data-models]'), visible);
    const node = nodes.find(item => item.id === selected);
    if (node) {
      $('#focus-title').textContent = node.name; $('#focus-subtitle').textContent = node.gpuTotal ? 'GPU compute service · 2 GPU devices' : 'CPU compute service · 24 cores';
      badge($('#focus-state'), state(node)); $('#focus-capacity').replaceChildren(capacity(node)); modelRows($('#focus-panel [data-models]'), [node]);
    }
    const targets = scoped().filter(node => node.online);
    $('#pause').disabled = !targets.length; $('#pause').textContent = targets.length && targets.every(node => node.paused) ? 'Resume intake' : 'Pause intake';
    $('#clear').disabled = !targets.some(node => work(node, 'Queued').length);
    $('#unload').disabled = !targets.some(node => node.models.length && !node.unloading);
  }
  function finishUnloads() {
    nodes.filter(node => node.unloading && !work(node).length).forEach(node => { node.used = Math.max(0, node.used - node.models.reduce((sum, model) => sum + model.rss, 0)); node.models = []; node.unloading = false; });
  }
  function confirm(title, description, effect, button) {
    opener = button; pending = effect; $('#action-title').textContent = title; $('#action-description').textContent = description;
    $('#action-dialog').showModal(); $('#cancel-action').focus();
  }
  function action(kind, targets, button) {
    targets = targets.filter(node => node.online);
    const names = targets.map(node => node.name).join(', ');
    if (kind === 'pause') {
      const resume = targets.every(node => node.paused);
      confirm(`${resume ? 'Resume' : 'Pause'} intake`, `${names}: ${resume ? 'accept new requests again.' : 'reject new arrivals; running and queued requests finish normally.'}`, () => targets.forEach(node => node.paused = !resume), button);
    } else if (kind === 'clear') {
      const count = targets.reduce((sum, node) => sum + work(node, 'Queued').length, 0);
      confirm(`Clear ${count} queued requests`, `${names}: remove waiting requests. Running requests continue.`, () => { requests = requests.filter(request => request.state !== 'Queued' || !targets.some(node => node.id === request.node)); finishUnloads(); }, button);
    } else {
      confirm('Unload models from nodes', `${names}: unload all resident models after existing work finishes. Intake remains ${targets.every(node => node.paused) ? 'paused' : 'unchanged'}.`, () => {
        targets.forEach(node => node.unloading = true); finishUnloads();
      }, button);
    }
  }
  $('#confirm-action').onclick = () => { pending?.(); $('#action-dialog').close(); render(); $('#feedback').textContent = 'Sample action applied. No live service was contacted.'; };
  $('#cancel-action').onclick = () => $('#action-dialog').close();
  $('#action-dialog').addEventListener('close', () => opener?.focus());
  ['pause', 'clear', 'unload'].forEach(kind => $('#' + kind).onclick = () => action(kind, scoped(), $('#' + kind)));
  $$('button[data-design]').forEach(button => button.onclick = () => { design = button.dataset.design; history.replaceState(null, '', '#' + design); render(); });
  $('#search').oninput = render; $('#control-scope').onchange = render;
  $('#fleet-size').onchange = seed; $('#scenario').onchange = seed; $('#reset').onclick = seed;
  function theme(mode) { const select = $('[data-theme-form] select[name="mode"]'); select.value = mode; select.dispatchEvent(new Event('change', { bubbles: true })); $('#theme').textContent = mode === 'dark' ? 'Light mode' : 'Dark mode'; }
  $('#theme').onclick = () => theme(document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark');
  $$('a[href^="/"]').forEach(link => link.onclick = event => { event.preventDefault(); $('#feedback').textContent = 'Application navigation is shown for context. This preview stays on Status.'; });
  $$('form[action]').forEach(form => form.onsubmit = event => event.preventDefault());
  const header = $('body > header'); new ResizeObserver(() => document.documentElement.style.setProperty('--top', `${header.offsetHeight}px`)).observe(header);
  document.addEventListener('DOMContentLoaded', () => theme('dark'), { once: true });
  seed();
})();
