/* Browser-local design state. No inference, polling or live API requests. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const clone = id => $(`#${id}`).content.firstElementChild.cloneNode(true);
  const put = (root, selector, value) => { root.querySelector(selector).textContent = value; };
  const nodes = [
    {id: 'front', name: 'Control node', hardware: 'CPU · 32 GiB RAM', state: 'ready'},
    {id: 'atlas', name: 'Atlas', hardware: '2 × RTX 5090 · 64 GiB VRAM', state: 'running'},
    {id: 'orion', name: 'Orion', hardware: 'RTX 4090 · 24 GiB VRAM', state: 'running'},
    {id: 'cedar', name: 'Cedar', hardware: 'CPU · 128 GiB RAM', state: 'ready'},
    {id: 'nova', name: 'Nova', hardware: 'Last known · RTX 3090', state: 'offline'},
  ];
  const models = [
    {id: 'qwen8', name: 'Qwen3 · 8B', meta: 'Q4_K_M · 8,192 context', kind: 'llm', nodes: ['atlas', 'orion', 'cedar', 'nova']},
    {id: 'gemma4', name: 'Gemma 3 · 4B', meta: 'Q4_K_M · 8,192 context', kind: 'llm', nodes: ['front', 'atlas', 'orion']},
    {id: 'embed', name: 'BGE-M3', meta: 'F16 · embedding', kind: 'embedding', nodes: ['front', 'cedar']},
    {id: 'sdxl', name: 'SDXL · base', meta: 'Q8_0 · image generation', kind: 'image', nodes: ['atlas', 'orion']},
    {id: 'kokoro', name: 'Kokoro · 82M', meta: 'F16 · speech synthesis', kind: 'audio', nodes: ['front', 'cedar']},
  ];
  const tests = {
    speed: {name: 'Generation speed', note: 'LLMs only · fixed 8,192-token context · tokens / second · higher is better', version: 'v3'},
    context: {name: 'Context capacity', note: 'LLMs only · adaptive capacity probe · tokens · higher is better', version: 'v2'},
    load: {name: 'Load time', note: 'LLM, embedding, image & audio · fresh process · seconds · lower is better', version: 'v2'},
    embedding_speed: {name: 'Embedding throughput', note: 'Embedding models only · input tokens / second · higher is better', version: 'v2'},
    extraction: {name: 'Structured extraction', note: 'LLMs only · same case set and protocol on every node · score / 100', version: 'v4'},
  };
  const initialQueue = [
    {id: 'Q-104', model: 'qwen8', node: 'cedar', test: 'speed', state: 'queued', reason: 'Waiting for the benchmark worker'},
    {id: 'Q-105', model: 'gemma4', node: 'atlas', test: 'context', state: 'paused', reason: 'Paused by you · completed samples kept'},
    {id: 'Q-106', model: 'qwen8', node: 'nova', test: 'speed', state: 'waiting', reason: 'Node unreachable · target stays Nova'},
  ];
  const running = [
    {id: 'R-102', model: 'qwen8', node: 'atlas', test: 'speed', percent: 68, samples: '34 / 50 samples', elapsed: '4m 12s elapsed', eta: 'About 2m remaining', metric: '74.2 tok/s', log: '[10:42:03] Sample 34 saved. Yielding to user requests.'},
    {id: 'R-103', model: 'gemma4', node: 'orion', test: 'extraction', percent: 42, samples: '42 / 100 cases', elapsed: '6m 05s elapsed', eta: 'About 8m remaining', metric: '86 / 100 so far', log: '[10:42:05] Case 42 graded. Partial score is provisional.'},
  ];
  const history = [
    {id: 'R-101', model: 'gemma4', node: 'atlas', test: 'speed', state: 'completed', score: '112.4 tok/s', freshness: 'Current settings', reason: '50 samples retained', time: 'Today, 10:31 PST'},
    {id: 'R-100', model: 'qwen8', node: 'orion', test: 'speed', state: 'completed', score: '48.6 tok/s', freshness: 'Settings changed', reason: 'Saved at 4,096 context; current setup uses 8,192.', time: 'Today, 10:18 PST'},
    {id: 'R-099', model: 'qwen8', node: 'cedar', test: 'context', state: 'failed', score: '—', freshness: 'No completed result', reason: 'Probe process exited: insufficient system RAM. No successful capacity measurement.', time: 'Today, 09:54 PST'},
    {id: 'R-098', model: 'embed', node: 'front', test: 'embedding_speed', state: 'completed', score: '2,840 tok/s', freshness: 'Current settings', reason: '100 embedding inputs retained', time: 'Yesterday, 17:42 PST'},
    {id: 'R-097', model: 'sdxl', node: 'atlas', test: 'load', state: 'completed', score: '6.2 s', freshness: 'Current settings', reason: '3 fresh-process loads retained', time: 'Yesterday, 16:23 PST'},
    {id: 'R-096', model: 'kokoro', node: 'cedar', test: 'load', state: 'cancelled', score: '—', freshness: 'Incomplete', reason: 'Stopped by owner; partial samples retained.', time: 'Yesterday, 15:07 PST'},
  ];
  let queue = structuredClone(initialQueue), selected = new Set(), layout = 'matrix', paused = false, batch = [], nextId = 107;
  const model = id => models.find(item => item.id === id);
  const node = id => nodes.find(item => item.id === id);
  const key = (m, n) => `${m}/${n}`;
  const scenario = () => $('#scenario').value;
  const canChange = () => !['offline', 'readonly', 'empty'].includes(scenario());
  const activeRuns = () => scenario() === 'empty' ? [] : running;
  const compatible = m => $('#benchmark').value === 'load' || ($('#benchmark').value === 'embedding_speed' ? m.kind === 'embedding' : m.kind === 'llm');
  const evidence = (m, n) => history.find(run => run.model === m.id && run.node === n.id && run.test === $('#benchmark').value && run.state === 'completed');
  const exists = (m, n) => [...activeRuns(), ...queue].some(run => run.model === m.id && run.node === n.id && run.test === $('#benchmark').value);
  function availability(m, n) {
    if (scenario() === 'offline') return 'Inventory unknown';
    if (n.state === 'offline') return 'Node unreachable';
    if (!m.nodes.includes(n.id)) return 'Not installed';
    if (!compatible(m)) return 'Test incompatible';
    if (exists(m, n)) return 'Already active';
    if (!canChange()) return 'View only';
    return '';
  }
  const notify = message => { $('#feedback').textContent = message; };
  const quantity = (count, noun) => `${count} ${noun}${count === 1 ? '' : 's'}`;
  function badge(element, state) {
    const colors = {running: 'blue', ready: 'green', completed: 'green', failed: 'red', offline: 'secondary', cancelled: 'secondary', waiting: 'orange', paused: 'orange', queued: 'blue', unknown: 'orange'};
    element.className = `badge bg-${colors[state] || 'secondary'}-lt`;
    element.textContent = state[0].toUpperCase() + state.slice(1);
  }
  function empty(root, columns, message) {
    const row = clone('empty-template');
    row.firstElementChild.colSpan = columns;
    row.firstElementChild.textContent = message;
    root.append(row);
  }
  function renderNodes() {
    $('#nodes').replaceChildren();
    nodes.forEach(n => {
      const tile = clone('node-template'), state = scenario() === 'offline' ? 'unknown' : n.state;
      tile.dataset.node = n.id; tile.dataset.status = state;
      put(tile, '[data-name]', n.name); put(tile, '[data-hardware]', n.hardware);
      badge(tile.querySelector('[data-state]'), state);
      const live = activeRuns().find(run => run.node === n.id);
      put(tile, '[data-work]', scenario() === 'empty' ? 'No models configured' : scenario() === 'offline' ? 'Snapshot retained · disconnected' : live ? `${model(live.model).name} · ${tests[live.test].name}` : n.state === 'offline' ? 'Inventory unknown · 1 run waiting' : 'Available for benchmark work');
      $('#nodes').append(tile);
    });
    $('#fleet-summary').textContent = scenario() === 'offline' ? '5 nodes visible · status unavailable' : `5 nodes visible · 4 reachable · ${activeRuns().length} running`;
  }
  function target(m, n, title) {
    const choice = clone('target-template'), box = choice.querySelector('input'), result = evidence(m, n), reason = availability(m, n);
    box.value = JSON.stringify({model: m.id, machine: n.id}); box.dataset.key = key(m.id, n.id);
    box.disabled = !!reason; box.checked = selected.has(box.dataset.key);
    box.setAttribute('aria-label', `Select ${m.name} on ${n.name}`);
    put(choice, '[data-target-title]', title || reason || (result ? result.score : 'Not run'));
    put(choice, '[data-target-note]', title ? reason || (result ? result.freshness : 'Not run') : reason ? 'Unavailable for selection' : result ? result.freshness : 'Missing evidence');
    return choice;
  }
  function visibleModels() {
    if (scenario() === 'empty') return [];
    return models.filter(m => compatible(m) && `${m.name} ${m.meta}`.toLowerCase().includes($('#search-models').value.toLowerCase()));
  }
  function renderTargets() {
    const focused = document.activeElement, focusKey = focused?.dataset.key, focusNode = focused?.dataset.selectNode;
    const root = $('#targets'), visible = visibleModels(); root.replaceChildren();
    $('#benchmark-note').textContent = tests[$('#benchmark').value].note;
    if (layout === 'lanes') {
      const groups = document.createElement('div'); groups.className = 'node-targets';
      nodes.forEach(n => {
        const group = clone('node-group-template'); group.dataset.nodeGroup = n.id;
        put(group, '[data-name]', n.name); put(group, '[data-meta]', n.hardware);
        const button = group.querySelector('[data-select-node]'); button.dataset.selectNode = n.id;
        button.disabled = !visible.some(m => !availability(m, n));
        visible.forEach(m => group.querySelector('[data-options]').append(target(m, n, m.name)));
        if (!visible.length) put(group, '[data-options]', 'No matching models.');
        groups.append(group);
      }); root.append(groups);
    } else {
      const table = document.createElement('table'); table.className = 'table target-table align-middle mb-0'; table.setAttribute('aria-label', 'Model and node benchmark targets');
      const head = table.createTHead().insertRow();
      const title = document.createElement('th'); title.scope = 'col'; title.textContent = 'Model / configuration'; head.append(title);
      nodes.forEach(n => {
        const th = document.createElement('th'); th.scope = 'col'; th.textContent = n.name;
        const label = document.createElement('label'); label.className = 'd-flex gap-1 align-items-center mt-2 node-select';
        const box = document.createElement('input'); box.type = 'checkbox'; box.className = 'form-check-input'; box.dataset.selectNode = n.id;
        box.setAttribute('aria-label', `Select eligible models on ${n.name}`);
        const eligible = visible.filter(m => !availability(m, n));
        box.disabled = !eligible.length; box.checked = !!eligible.length && eligible.every(m => selected.has(key(m.id, n.id)));
        box.indeterminate = !box.checked && eligible.some(m => selected.has(key(m.id, n.id)));
        label.append(box, 'Eligible'); th.append(label); head.append(th);
      });
      const body = table.createTBody();
      visible.forEach(m => {
        const row = clone('model-row-template'); row.dataset.model = m.id;
        put(row, '[data-name]', m.name); put(row, '[data-meta]', m.meta);
        nodes.forEach(n => { const cell = row.insertCell(); cell.dataset.label = n.name; cell.append(target(m, n)); }); body.append(row);
      });
      if (!visible.length) empty(body, 6, scenario() === 'empty' ? 'No local models configured. Add models to a compute node to begin.' : 'No matching models. Clear the search to see the catalogue.');
      root.append(table);
    }
    updateSelection();
    if (focusKey) $(`[data-target][data-key="${focusKey}"]`)?.focus({preventScroll: true});
    else if (focusNode) $(`[data-select-node="${focusNode}"]`)?.focus({preventScroll: true});
  }
  function updateSelection() {
    const pairs = [...selected].map(value => value.split('/'));
    $('#selection-count').textContent = `${quantity(pairs.length, 'run')} · ${quantity(new Set(pairs.map(p => p[0])).size, 'model')} · ${quantity(new Set(pairs.map(p => p[1])).size, 'node')}`;
    const hidden = pairs.filter(([m]) => !visibleModels().some(item => item.id === m)).length;
    $('#selection-note').textContent = hidden ? `${quantity(hidden, 'selected target')} outside this search. Review includes all selections.` : 'Exact node targets · active pairs excluded · one test per batch';
    $('#review').disabled = !pairs.length || !canChange();
    $('#select-gaps').disabled = !canChange();
  }
  function renderRunning() {
    const root = $('#running'); root.replaceChildren(); $('#running-count').textContent = `${activeRuns().length} active`;
    activeRuns().forEach(run => {
      const tile = clone('running-template');
      put(tile, '[data-model]', model(run.model).name); put(tile, '[data-node]', node(run.node).name);
      put(tile, '[data-benchmark]', `${tests[run.test].name} · ${tests[run.test].version} · ${run.id}`);
      put(tile, '[data-samples]', run.samples); put(tile, '[data-percent]', `${run.percent}%`);
      const progress = tile.querySelector('progress'); progress.value = run.percent; progress.setAttribute('aria-label', `${model(run.model).name} on ${node(run.node).name}`);
      put(tile, '[data-elapsed]', run.elapsed); put(tile, '[data-eta]', scenario() === 'offline' ? 'Last known progress' : run.eta);
      const dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = 'Latest measurement'; dd.textContent = run.metric; tile.querySelector('dl').append(dt, dd);
      put(tile, '[data-log]', run.log); root.append(tile);
    });
    if (!activeRuns().length) { const p = document.createElement('p'); p.className = 'p-3 mb-0 text-secondary'; p.textContent = 'No benchmarks running. Choose models and nodes below to prepare a run.'; root.append(p); }
  }
  function renderQueue() {
    const root = $('#queue'); root.replaceChildren();
    queue.forEach(run => {
      const row = clone('queue-row-template'); row.dataset.queueId = run.id;
      put(row, '[data-model]', model(run.model).name); put(row, '[data-id]', run.id); put(row, '[data-node]', node(run.node).name); put(row, '[data-benchmark]', tests[run.test].name);
      badge(row.querySelector('[data-state]'), paused ? 'paused' : run.state); put(row, '[data-reason]', paused ? 'Queue paused · running work continues' : run.reason);
      const toggle = row.querySelector('[data-pause]'); toggle.textContent = run.state === 'paused' ? 'Resume' : 'Pause'; toggle.dataset.pause = run.id;
      toggle.setAttribute('aria-label', `${toggle.textContent} ${run.id}`); toggle.disabled = !canChange() || paused;
      const remove = row.querySelector('[data-remove]'); remove.dataset.remove = run.id; remove.disabled = !canChange(); remove.setAttribute('aria-label', `Remove ${run.id}`);
      root.append(row);
    });
    if (!queue.length) empty(root, 5, 'Queue is clear. Reviewed runs will appear here.');
    $('#pause-all').textContent = paused ? 'Resume queue' : 'Pause queue'; $('#pause-all').disabled = !canChange() || !queue.length;
    $('#queue-summary').textContent = `${queue.length} queued targets · ${paused ? 'queue paused' : 'running work appears above'} · node identity is retained`;
  }
  function renderHistory() {
    const root = $('#history'); root.replaceChildren();
    const filtered = scenario() === 'empty' ? [] : history.filter(run => ($('#history-state').value === 'all' || run.state === $('#history-state').value) && `${model(run.model).name} ${node(run.node).name} ${tests[run.test].name} ${run.id}`.toLowerCase().includes($('#history-search').value.toLowerCase()));
    filtered.forEach(run => {
      const row = clone('history-row-template');
      const detail = row.querySelector('[data-detail]'); detail.dataset.detail = run.id; detail.textContent = model(run.model).name; detail.setAttribute('aria-label', `Open saved run ${run.id}`);
      put(row, '[data-meta]', `${run.id} · ${tests[run.test].version}`); put(row, '[data-node]', node(run.node).name); put(row, '[data-benchmark]', tests[run.test].name); put(row, '[data-score]', run.score); put(row, '[data-freshness]', run.freshness); put(row, '[data-ended]', run.time);
      badge(row.querySelector('[data-state]'), run.state);
      const rerun = row.querySelector('[data-rerun]'); rerun.dataset.rerun = run.id; rerun.disabled = !canChange(); rerun.setAttribute('aria-label', `Set up ${run.id} again`); root.append(row);
    });
    if (!filtered.length) empty(root, 6, scenario() === 'empty' ? 'No finished runs yet. Completed, failed and cancelled work will be retained here.' : 'No matching history. Clear the search or choose all outcomes.');
    $('#history-count').textContent = `${filtered.length} of ${scenario() === 'empty' ? 0 : history.length} retained runs`;
  }
  function render() {
    $('#connection-note').hidden = scenario() !== 'offline'; $('#permission-note').hidden = scenario() !== 'readonly';
    renderNodes(); renderRunning(); renderQueue(); renderHistory(); renderTargets();
  }
  function setLayout(value) {
    layout = value; $('#workspace').dataset.layout = value;
    $$('[data-design]').forEach(button => { const active = button.dataset.design === value; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
    $('#design-description').textContent = {matrix: 'Compare models across every node. Best for planning a consistent evaluation batch.', lanes: 'Follow machine workloads first. Each node keeps its own target list.', studio: 'Build a focused batch beside live work. Review its exact scope before queueing.'}[value];
    renderTargets();
  }
  function selectNode(id, checked = true) {
    visibleModels().filter(m => !availability(m, node(id))).forEach(m => checked ? selected.add(key(m.id, id)) : selected.delete(key(m.id, id)));
    renderTargets();
  }
  document.addEventListener('change', event => {
    if (event.target.matches('[data-target]')) { event.target.checked ? selected.add(event.target.dataset.key) : selected.delete(event.target.dataset.key); renderTargets(); }
    if (event.target.matches('input[data-select-node]')) selectNode(event.target.dataset.selectNode, event.target.checked);
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button) return;
    if (button.dataset.design) setLayout(button.dataset.design);
    if (button.hasAttribute('data-select-node')) selectNode(button.dataset.selectNode);
    if (button.hasAttribute('data-close')) button.closest('dialog').close();
    if (button.dataset.pause) { const run = queue.find(r => r.id === button.dataset.pause); run.state = run.state === 'paused' ? node(run.node).state === 'offline' ? 'waiting' : 'queued' : 'paused'; run.reason = run.state === 'paused' ? 'Paused by you · completed samples kept' : run.state === 'waiting' ? 'Node unreachable · target retained' : 'Waiting for the benchmark worker'; renderQueue(); }
    if (button.dataset.remove) { queue = queue.filter(r => r.id !== button.dataset.remove); renderQueue(); renderTargets(); notify(`${button.dataset.remove} removed from the sample queue.`); }
    if (button.dataset.detail) {
      const run = history.find(r => r.id === button.dataset.detail), root = $('#run-detail'); root.replaceChildren();
      Object.entries({Run: run.id, Model: model(run.model).name, Node: node(run.node).name, Test: `${tests[run.test].name} · ${tests[run.test].version}`, Outcome: run.state, Result: run.score, Settings: run.freshness, Evidence: run.reason, Finished: run.time}).forEach(([label, value]) => { const dt = document.createElement('dt'), dd = document.createElement('dd'); dt.textContent = label; dd.textContent = value; root.append(dt, dd); });
      $('#detail-dialog').showModal();
    }
    if (button.dataset.rerun) {
      const run = history.find(r => r.id === button.dataset.rerun); $('#benchmark').value = run.test; $('#search-models').value = ''; selected.clear();
      const reason = availability(model(run.model), node(run.node));
      if (!reason) selected.add(key(run.model, run.node));
      renderTargets(); $('#composer').scrollIntoView({behavior: 'smooth'}); $('#benchmark').focus({preventScroll: true});
      notify(reason ? `Cannot queue ${run.id}: ${reason}.` : `Prepared ${run.id} with current settings. Review before queueing.`);
    }
  });
  $('#benchmark').addEventListener('change', () => { selected.clear(); renderTargets(); });
  $('#search-models').addEventListener('input', renderTargets);
  $('#history-search').addEventListener('input', renderHistory); $('#history-state').addEventListener('change', renderHistory);
  $('#select-gaps').addEventListener('click', () => {
    visibleModels().forEach(m => nodes.forEach(n => { const result = evidence(m, n); if (!availability(m, n) && (!result || result.freshness !== 'Current settings')) selected.add(key(m.id, n.id)); })); renderTargets();
  });
  $('#clear-selection').addEventListener('click', () => { selected.clear(); renderTargets(); });
  $('#pause-all').addEventListener('click', () => { paused = !paused; renderQueue(); });
  $('#setup').addEventListener('submit', event => {
    event.preventDefault(); if (!canChange()) return;
    batch = [...selected].map(value => { const [m, n] = value.split('/'); return {model: m, node: n, test: $('#benchmark').value}; }).filter(run => !availability(model(run.model), node(run.node)));
    if (!batch.length) return;
    $('#review-rows').replaceChildren();
    batch.forEach(run => { const row = clone('review-row-template'); put(row, '[data-model]', model(run.model).name); put(row, '[data-node]', node(run.node).name); put(row, '[data-benchmark]', tests[run.test].name); $('#review-rows').append(row); });
    $('#review-summary').textContent = `${batch.length} runs · ${new Set(batch.map(r => r.model)).size} models · ${new Set(batch.map(r => r.node)).size} nodes · ${tests[$('#benchmark').value].version}`;
    $('#review-dialog').showModal();
  });
  $('#queue-batch').addEventListener('click', () => {
    if (!canChange()) return;
    let count = 0;
    batch.forEach(run => { if (availability(model(run.model), node(run.node))) return; queue.push({...run, id: `Q-${nextId++}`, state: 'queued', reason: 'Reviewed target · waiting for benchmark worker'}); count++; });
    selected.clear(); batch = []; $('#review-dialog').close(); render(); notify(`${count} sample runs queued. No real API was called.`);
  });
  $('#scenario').addEventListener('change', () => { selected.clear(); queue = scenario() === 'empty' ? [] : structuredClone(initialQueue); paused = false; render(); notify(''); });
  $('#reset').addEventListener('click', () => { selected.clear(); queue = structuredClone(initialQueue); paused = false; nextId = 107; $('#scenario').value = 'normal'; $('#benchmark').value = 'speed'; $('#search-models').value = ''; $('#history-search').value = ''; $('#history-state').value = 'all'; render(); notify('Demo reset.'); });
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark', control = $('[data-theme-form] [name="mode"]');
    control.value = dark ? 'dark' : 'light'; control.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = dark ? 'Light mode' : 'Dark mode';
  });
  setLayout(new URLSearchParams(location.search).get('design') === 'lanes' ? 'lanes' : new URLSearchParams(location.search).get('design') === 'studio' ? 'studio' : 'matrix'); render();
})();
