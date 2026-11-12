(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('measurement-data').textContent);
  const $ = selector => document.querySelector(selector);
  const all = selector => [...document.querySelectorAll(selector)];
  const clone = id => $(`#${id}`).content.firstElementChild.cloneNode(true);
  const put = (root, selector, value) => { root.querySelector(selector).textContent = value; };
  const names = {ledger: 'Recommended for daily use. Every node, then its complete model inventory and measurements.',
    matrix: 'Compare one measurement across the fleet. Frozen model names keep wide comparisons readable.',
    lens: 'Start with a model. Compare its capacity, startup and speed on every configured node.'};
  const metrics = {
    context: {name: 'Max context', unit: 'tokens', scoreUnit: 'tokens', group: 'context', higher: true,
      note: 'Largest context that processed a full prompt and generated an answer. Higher is better.'},
    load: {name: 'Load time', unit: 's', scoreUnit: '1/s', group: 'load', higher: false,
      note: 'Median startup at the largest successfully loaded context. Lower seconds are better; the score is 1 ÷ seconds.'},
    speed: {name: 'Generation speed', unit: 'tok/s', scoreUnit: 'tokens/s', group: 'speed', higher: true,
      note: 'Median output speed at the selected sample input, usually 2,048 tokens. Higher is better.'},
    prefill: {name: 'Prompt speed', unit: 'tok/s', group: 'speed', higher: true},
    ttft: {name: 'First token', unit: 's', group: 'speed', higher: false},
  };
  let layout = Object.hasOwn(names, location.hash.slice(1)) ? location.hash.slice(1) : 'ledger';
  let kind = 'llm', nodeId = 'atlas', modelId = 'llm-0', metric = 'context';
  let scenario = 'normal', search = '', filter = 'all', order = 'name', display = 'values';
  const activeModels = () => scenario === 'empty' ? [] : data.models.filter(model => model.kind === kind);
  const primaryMetrics = () => ['image', 'audio'].includes(kind) ? ['load'] : ['context', 'load', 'speed'];
  const tableMetrics = () => kind === 'llm' ? [...primaryMetrics(), 'prefill', 'ttft'] : primaryMetrics();
  const nodeById = id => data.nodes.find(node => node.id === id);
  const number = value => new Intl.NumberFormat('en-US', {maximumFractionDigits: 2}).format(value);
  const metricName = key => key === 'speed' && kind === 'embedding' ? 'Embedding throughput' : metrics[key].name;
  const metricNote = () => kind === 'embedding' && metric === 'speed' ? 'Median input throughput, usually at 512 input tokens, including receipt of the embedding vector. Higher is better.' : kind === 'embedding' && metric === 'context' ? 'Largest successfully processed embedding input. This tests input capacity, with no generated answer.' : metrics[metric].note;
  const meta = model => `${model.format} · ${model.kind === 'llm' ? `${model.size}B · ` : ''}${model.placements.length} nodes`;
  function result(model, node, key = metric) {
    const record = data.results[`${model.id}/${node.id}`];
    if (!record) return {state: 'absent'};
    const test = record.measurements[metrics[key].group];
    if (!test) return {state: 'unsupported'};
    if (scenario === 'unmeasured') return {state: 'missing'};
    return {...test, record, value: record.values[key]};
  }
  const hasValue = item => ['current', 'stale'].includes(item.state);
  function valueOf(item, key = metric, mode = display) {
    return mode === 'scores' && key === 'load' ? 1 / item.value : item.value;
  }
  function formatted(item, key = metric, mode = display) {
    const value = valueOf(item, key, mode);
    return mode === 'scores' && key === 'load' ? value.toFixed(4) : number(value);
  }
  const unit = key => display === 'scores' && metrics[key].scoreUnit ? metrics[key].scoreUnit : metrics[key].unit;
  function counts(models, node, keys = primaryMetrics()) {
    const tests = models.filter(model => model.placements.includes(node.id)).flatMap(model => keys.map(key => result(model, node, key)));
    return {current: tests.filter(test => test.state === 'current').length, total: tests.length};
  }
  function relevant(model) {
    if (!`${model.name} ${model.size}B ${model.format}`.toLowerCase().includes(search)) return false;
    const nodes = layout === 'ledger' ? [nodeById(nodeId)] : data.nodes;
    if (layout === 'ledger' && !model.placements.includes(nodeId)) return false;
    if (filter === 'all') return true;
    return nodes.some(node => {
      const test = result(model, node);
      return filter === 'gaps' ? ['missing', 'failed'].includes(test.state) : test.state === filter;
    });
  }
  function sortValue(model) {
    const nodes = layout === 'ledger' ? [nodeById(nodeId)] : data.nodes;
    const values = nodes.map(node => result(model, node)).filter(item => item.state === 'current').map(item => item.value);
    if (!values.length) return -Infinity;
    return metrics[metric].higher ? Math.max(...values) : -Math.min(...values);
  }
  function visibleModels() {
    return activeModels().filter(relevant).sort((a, b) =>
      (order === 'best' ? sortValue(b) - sortValue(a) : 0) || a.name.localeCompare(b.name));
  }
  function chooseLayout(next) {
    layout = next;
    history.replaceState(null, '', `#${layout}`);
    render();
  }
  function chooseNode(id) {
    nodeId = id;
    if (layout !== 'ledger') chooseLayout('ledger'); else render();
  }
  function renderNodes() {
    const models = activeModels();
    $('#node-total').textContent = data.nodes.length;
    $('#node-count').textContent = data.nodes.length;
    $('#node-list').replaceChildren(...data.nodes.map(node => {
      const el = clone('measurement-node-template');
      const count = models.filter(model => model.placements.includes(node.id)).length;
      const coverage = counts(models, node);
      el.dataset.node = node.id;
      el.setAttribute('aria-pressed', String(layout === 'ledger' && nodeId === node.id));
      el.setAttribute('aria-label', `${node.id}, ${node.state}, ${count} models, ${coverage.current} of ${coverage.total} current tests`);
      put(el, '[data-name]', node.id);
      put(el, '[data-state]', node.state);
      put(el, '[data-hardware]', node.hardware);
      put(el, '[data-count]', `${count} models`);
      put(el, '[data-coverage]', `${coverage.current}/${coverage.total} current`);
      el.querySelector('progress').value = coverage.total ? coverage.current / coverage.total * 100 : 0;
      el.querySelector('progress').setAttribute('aria-label', `Current evidence on ${node.id}`);
      el.addEventListener('click', () => chooseNode(node.id));
      return el;
    }));
    const totals = data.nodes.map(node => counts(models, node));
    const current = totals.reduce((sum, one) => sum + one.current, 0);
    const total = totals.reduce((sum, one) => sum + one.total, 0);
    $('#model-total').textContent = models.length;
    $('#coverage-total').textContent = total ? `${Math.round(current / total * 100)}%` : '—';
    $('#coverage-total').title = `${current} of ${total} model + node + test combinations have current evidence`;
  }
  function cell(model, node, key = metric) {
    const el = clone('measurement-cell-template');
    const item = result(model, node, key);
    el.dataset.metric = key;
    el.dataset.state = item.state;
    const button = el.querySelector('[data-evidence]');
    if (hasValue(item) || item.state === 'failed') {
      button.dataset.status = item.state;
      const label = item.state === 'failed' ? 'Failed' : formatted(item, key);
      put(el, '[data-value]', label + (item.state === 'stale' ? ' †' : ''));
      put(el, '[data-note]', item.state === 'failed' ? 'View failure' : `${unit(key)}${item.state === 'stale' ? ' · settings changed' : ''}`);
      button.setAttribute('aria-label', `${model.name} on ${node.id}, ${metricName(key)}: ${label}${item.state === 'stale' ? ', settings changed' : ''}. View evidence`);
      button.addEventListener('click', () => showEvidence(model, node, key));
    } else {
      button.remove();
      const unavailable = el.querySelector('[data-unavailable]');
      unavailable.hidden = false;
      unavailable.textContent = {absent: 'Not on node', missing: 'Not run', unsupported: 'Not applicable'}[item.state];
    }
    return el;
  }
  function heading(text, note = '') {
    const th = document.createElement('th'); th.scope = 'col'; th.textContent = text;
    if (note) { const small = document.createElement('small'); small.className = 'd-block text-secondary fw-normal'; small.textContent = note; th.append(small); }
    return th;
  }
  function subjectRow(name, note, click) {
    const row = clone('measurement-row-template');
    put(row, '[data-subject]', name); put(row, '[data-meta]', note);
    row.querySelector('[data-subject]').addEventListener('click', click);
    return row;
  }
  function renderTable(models) {
    const head = document.createElement('tr');
    head.append(heading(layout === 'lens' ? 'Compute node' : 'Model'));
    if (layout === 'matrix') {
      data.nodes.forEach(node => {
        const th = clone('measurement-node-heading-template');
        put(th, '[data-node]', node.id); put(th, '[data-meta]', node.state);
        th.querySelector('button').addEventListener('click', () => chooseNode(node.id));
        head.append(th);
      });
    } else tableMetrics().forEach(key => head.append(heading(metricName(key), unit(key))));
    $('#measurement-head').replaceChildren(head);
    const chosen = models.find(model => model.id === modelId);
    const rows = layout === 'lens' ? (chosen ? data.nodes.map(node => {
      const row = subjectRow(node.id, node.state, () => chooseNode(node.id));
      row.dataset.nodeRow = node.id;
      tableMetrics().forEach(key => row.append(cell(chosen, node, key)));
      return row;
    }) : []) : models.map(model => {
      const row = subjectRow(model.name, meta(model), () => { modelId = model.id; chooseLayout('lens'); });
      row.dataset.modelRow = model.id;
      if (layout === 'matrix') {
        const values = data.nodes.map(node => result(model, node)).filter(item => item.state === 'current').map(item => item.value);
        const best = metrics[metric].higher ? Math.max(...values) : Math.min(...values);
        data.nodes.forEach(node => {
          const td = cell(model, node), item = result(model, node);
          if (item.state === 'current' && item.value === best) {
            td.dataset.best = '';
            td.querySelector('[data-note]').textContent += ' · best';
            td.querySelector('button').setAttribute('aria-label', td.querySelector('button').getAttribute('aria-label') + '. Best current result for this model.');
          }
          row.append(td);
        });
      }
      else tableMetrics().forEach(key => row.append(cell(model, nodeById(nodeId), key)));
      return row;
    });
    if (!rows.length) {
      const empty = clone('measurement-empty-template');
      empty.querySelector('td').colSpan = head.children.length;
      const noModels = !activeModels().length;
      const noLocal = layout === 'ledger' && !activeModels().some(model => model.placements.includes(nodeId));
      put(empty, '[data-empty-title]', noModels ? 'No models available' : noLocal ? `No ${kind === 'llm' ? 'LLM' : kind} models on ${nodeId}` : 'No matching models');
      put(empty, '[data-empty-note]', noModels || noLocal ? 'Choose another node or model type. All configured nodes remain in view.' : 'Clear the search or choose All evidence to see more models.');
      rows.push(empty);
    }
    $('#measurement-rows').replaceChildren(...rows);
    $('#row-count').textContent = layout === 'lens' ? `${chosen ? data.nodes.length : 0} nodes` : `${models.length} models`;
    $('#table-range').textContent = layout === 'lens' ? 'All configured nodes' : 'All matching rows · scroll to browse';
    $('#table-caption').textContent = layout === 'ledger' ? `Recorded measurements on ${nodeId}` : layout === 'lens' ? `Recorded measurements for ${chosen?.name || 'selected model'} on all nodes` : `${metricName(metric)} on every configured node`;
  }
  function renderLens(models) {
    if (!models.some(model => model.id === modelId)) modelId = models[0]?.id;
    const chosen = models.find(model => model.id === modelId);
    $('#model-list').replaceChildren(...models.map(model => {
      const el = clone('measurement-model-template');
      put(el, '[data-name]', model.name); put(el, '[data-meta]', meta(model));
      el.dataset.model = model.id; el.setAttribute('aria-pressed', String(model.id === modelId));
      el.addEventListener('click', () => { modelId = model.id; render(); });
      return el;
    }));
    if (!models.length) $('#model-list').textContent = 'No matching models. Clear the filters to browse.';
    const selected = $('#model-list [aria-pressed="true"]'), list = $('#model-list');
    if (selected) {
      const top = selected.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop;
      if (top < list.scrollTop || top + selected.offsetHeight > list.scrollTop + list.clientHeight) {
        list.scrollTop = Math.max(0, top - list.clientHeight / 2 + selected.offsetHeight / 2);
      }
    }
    $('#lens-model-name').textContent = chosen ? `${chosen.name} / ${metricName(metric)}` : 'No model selected';
    const current = chosen ? data.nodes.map(node => ({node, item: result(chosen, node)})).filter(one => one.item.state === 'current') : [];
    const max = Math.max(...current.map(one => valueOf(one.item)), 0);
    $('#lens-bars').replaceChildren(...current.map(({node, item}) => {
      const bar = clone('measurement-bar-template');
      put(bar, '[data-name]', node.id);
      put(bar, '[data-value]', `${formatted(item)} ${unit(metric)}`);
      bar.querySelector('[data-bar]').style.width = `${max ? valueOf(item) / max * 100 : 0}%`;
      bar.title = `${node.state} · ${metrics[metric].name}: ${formatted(item)} ${unit(metric)}`;
      return bar;
    }));
    if (!current.length) $('#lens-bars').textContent = 'No current measurements for this model and test.';
  }
  function render() {
    $('#workspace').dataset.layout = layout;
    $('#design-description').textContent = names[layout];
    all('[data-design]').forEach(button => {
      button.classList.toggle('active', button.dataset.design === layout);
      button.setAttribute('aria-pressed', String(button.dataset.design === layout));
    });
    all('[data-kind]').forEach(button => {
      button.classList.toggle('active', button.dataset.kind === kind);
      button.setAttribute('aria-pressed', String(button.dataset.kind === kind));
    });
    all('[data-metric]').forEach(button => {
      button.hidden = !primaryMetrics().includes(button.dataset.metric);
      button.textContent = metricName(button.dataset.metric);
      button.classList.toggle('active', button.dataset.metric === metric);
      button.setAttribute('aria-pressed', String(button.dataset.metric === metric));
    });
    $('#type-note').textContent = ['image', 'audio'].includes(kind) ? 'Load-time evidence only. Token context and token speed are not applicable.' : 'Context, load and speed tests · every configured node stays visible.';
    $('#node-navigation-note').textContent = layout === 'ledger' ? 'Choose a node to inspect its models.' : 'Every configured node. Open one to inspect its inventory.';
    $('#connection-note').hidden = scenario !== 'offline';
    $('#metric-note').textContent = metricNote() + (display === 'scores' ? ' Scores use each benchmark’s default formula.' : '') + (layout === 'matrix' ? ' Highlighted cells are the best current result for that model.' : ' The selected metric controls sorting and evidence filters.');
    $('#measurement-methods').textContent = (kind === 'llm' ? 'Capacity means a full prompt passed the Context test. ' : kind === 'embedding' ? 'Embedding capacity is measured from successfully processed input. ' : '') + 'Load time measures process readiness. Saved results remain available on unreachable or disabled nodes.';
    renderNodes();
    const models = visibleModels();
    $('#model-lens').hidden = layout !== 'lens';
    if (layout === 'lens') renderLens(models);
    $('#context-eyebrow').textContent = layout === 'ledger' ? `NODE / ${nodeId.toUpperCase()}` : layout === 'matrix' ? 'FLEET / ONE TEST, EVERY NODE' : 'MODEL / PLACEMENT EXPLORER';
    $('#context-title').textContent = layout === 'ledger' ? `Models on ${nodeId}` : layout === 'matrix' ? `${metricName(metric)} across nodes` : 'Where should this model run?';
    $('#context-note').textContent = layout === 'ledger' ? `${nodeById(nodeId).hardware} · ${nodeById(nodeId).state}${['Unreachable', 'Disabled'].includes(nodeById(nodeId).state) ? ' · saved evidence retained' : ''}` : layout === 'matrix' ? 'One row per model. One column per configured node, including nodes with no matching models.' : 'Select from the catalogue; inspect all node results and the settings behind them.';
    $('#context-count').textContent = layout === 'ledger' ? `${activeModels().filter(model => model.placements.includes(nodeId)).length} models` : `${data.nodes.length} configured nodes`;
    renderTable(models);
  }
  function showEvidence(model, node, key) {
    const item = result(model, node, key);
    const group = metrics[key].group;
    const el = clone('measurement-detail-template');
    $('#evidence-title').textContent = `${model.name} / ${node.id}`;
    $('#evidence-subtitle').textContent = `${node.hardware} · ${node.state}`;
    put(el, '[data-test]', `${metricName(group)} benchmark`);
    put(el, '[data-state]', {current: 'Current evidence', stale: 'Settings changed', failed: 'Failed test'}[item.state]);
    el.querySelector('[data-state]').classList.add(item.state === 'failed' ? 'bg-red-lt' : item.state === 'stale' ? 'bg-orange-lt' : 'bg-blue-lt');
    put(el, '[data-value]', hasValue(item) ? `${formatted(item, key, 'values')} ${metrics[key].unit}` : 'No score');
    const scoreItem = {...item, value: item.record.values[group]};
    put(el, '[data-score]', hasValue(item) ? `Benchmark score: ${formatted(scoreItem, group, 'scores')} ${metrics[group].scoreUnit} · formula: ${group === 'load' ? '1 ÷ load_seconds' : group === 'context' ? 'max_context_tokens' : kind === 'embedding' ? 'embedding_tokens_per_second' : 'generation_tokens_per_second'}.` : 'The probe failed. A failed or unavailable measurement is never treated as zero.');
    const facts = [
      ['Run', item.run], ['Recorded', item.at], ['Runtime', `${item.record.engine} · ${item.record.backend} · sample build`],
      ['Model format', model.format], ['Saved context', ['image', 'audio'].includes(kind) ? 'Not applicable' : `${number(item.record.values.context)} tokens`],
      ['Sample group', group === 'context' ? (kind === 'embedding' ? 'Largest input probe that passed' : 'Largest full-prompt probe that passed') : group === 'load' ? 'Median of 3 starts at the largest loaded context' : `Median of 3 repetitions at ${kind === 'embedding' ? '512' : '2,048'} input tokens`],
      ['Test version', `${kind === 'embedding' ? 'embedding_' : ''}${group} · sample version 1`],
      ['Configuration', item.state === 'stale' ? 'Context or model settings changed after this run. The saved value belongs to its recorded configuration.' : 'Saved model settings and environment match this sample run.'],
      ['Comparison', 'Review saved context, format, runtime and sample group before comparing nodes. Their configurations can differ.'],
    ];
    if (item.state === 'failed') facts.push(['Failure', 'Sample probe exited before a valid measurement was recorded. Inspect node capacity and runtime logs before retrying.']);
    if (group === 'context' && hasValue(item)) facts.push(['Recommended context', `${number(Math.max(128, item.value - 1024))} tokens (1,024 tokens of headroom)`]);
    facts.forEach(([name, value]) => {
      const dt = document.createElement('dt'), dd = document.createElement('dd');
      dt.textContent = name; dd.textContent = value; el.querySelector('dl').append(dt, dd);
    });
    $('#evidence-body').replaceChildren(el);
    $('#evidence-dialog').showModal();
  }
  all('[data-design]').forEach(button => button.addEventListener('click', () => chooseLayout(button.dataset.design)));
  all('[data-kind]').forEach(button => button.addEventListener('click', () => {
    kind = button.dataset.kind;
    if (!primaryMetrics().includes(metric)) metric = 'load';
    modelId = activeModels()[0]?.id; render();
  }));
  all('[data-metric]').forEach(button => button.addEventListener('click', () => { metric = button.dataset.metric; render(); }));
  $('#model-search').addEventListener('input', event => { search = event.target.value.trim().toLowerCase(); render(); });
  $('#evidence-filter').addEventListener('change', event => { filter = event.target.value; render(); });
  $('#sort').addEventListener('change', event => { order = event.target.value; render(); });
  $('#display').addEventListener('change', event => { display = event.target.value; render(); });
  $('#scenario').addEventListener('change', event => { scenario = event.target.value; render(); });
  $('#close-evidence').addEventListener('click', () => $('#evidence-dialog').close());
  $('#evidence-dialog').addEventListener('click', event => { if (event.target === event.currentTarget) event.currentTarget.close(); });
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark';
    const control = $('[data-theme-form] [name="mode"]');
    control.value = dark ? 'dark' : 'light'; control.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = dark ? 'Light mode' : 'Dark mode';
  });
  window.addEventListener('hashchange', () => { const next = location.hash.slice(1); if (Object.hasOwn(names, next)) {layout = next; render();} });
  $('#export').addEventListener('click', () => {
    const rows = [['Model', 'Node', 'Metric', 'Value', 'Unit', 'State', 'Run', 'Recorded']];
    const models = visibleModels().filter(model => layout !== 'lens' || model.id === modelId);
    models.forEach(model => (layout === 'ledger' ? [nodeById(nodeId)] : data.nodes).forEach(node => {
      (layout === 'matrix' ? [metric] : tableMetrics()).forEach(key => {
        const item = result(model, node, key);
        rows.push([model.name, node.id, metricName(key), hasValue(item) ? formatted(item, key) : '', unit(key), item.state, item.run || '', item.at || '']);
      });
    }));
    const csv = rows.map(row => row.map(value => `"${String(value).replaceAll('"', '""')}"`).join(',')).join('\r\n');
    const link = document.createElement('a'), url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
    link.href = url; link.download = `sample-node-measurements-${layout}.csv`; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $('#feedback').textContent = `Exported the sample ${layout} view with evidence states and run references.`;
  });
  render();
})();
