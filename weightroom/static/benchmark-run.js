/* Progressive enhancement of server-rendered, caller-authorized run evidence. */
(() => {
  'use strict';
  const armed = new WeakSet();
  const names = {'Sample': 'id', 'Context tokens': 'context_tokens', 'Probe': 'state', 'Answer': 'answer',
    'Correct checks': 'checks', 'Input tokens': 'input_tokens', 'Output tokens': 'output_tokens',
    'Read tokens/s': 'prompt_tokens_per_second', 'Write tokens/s': 'generation_tokens_per_second',
    'First token (s)': 'ttft_seconds', 'Request (s)': 'request_seconds', 'Load (s)': 'load_seconds',
    'GPU MiB': 'peak_gpu_mib', 'CPU peak RAM (MiB)': 'peak_ram_mib',
    'Input tokens/s': 'embedding_tokens_per_second', 'Embeddings/s': 'embeddings_per_second', 'Dimensions': 'embedding_dimensions'};

  function initialize() {
    const root = document.querySelector('[data-run-briefing]');
    if (!root || armed.has(root)) return;
    const payload = root.querySelector('#benchmark-run-data');
    if (!payload) return;
    const data = JSON.parse(payload.textContent);
    armed.add(root);
    const $ = selector => root.querySelector(selector), $$ = selector => [...root.querySelectorAll(selector)];
    const table = $('#sample-table'), body = table.tBodies[0];
    const records = data.rows.map((meta, i) => ({...meta, values: data.values[i], element: body.querySelector(`[data-sample-row="${meta.index}"]`)}));
    const headings = [...table.tHead.rows[0].cells];
    const fields = headings.map(th => names[th.textContent.trim()]);
    let filtered = records.slice(), selected = null, view = data.embedding ? 'Embedding receipt' : 'Graded final answer';
    let columns = 'results', sortKey = 'id', ascending = true, timer;
    const tabKey = `weightroom-run-evidence:${document.documentElement.dataset.themeUser || 'anonymous'}:${data.id}`;
    try { view = sessionStorage.getItem(tabKey) || view; } catch { /* Storage may be disabled. */ }
    const changes = new Map((data.regression?.rows || []).map(row => [row.case, row.status]));
    const clone = id => document.getElementById(id).content.firstElementChild.cloneNode(true);
    function notify(message) {
      clearTimeout(timer); $('#run-feedback').hidden = false; $('#run-feedback').textContent = message;
      timer = setTimeout(() => { if (root.isConnected) $('#run-feedback').hidden = true; }, 5000);
    }
    async function copy(value) {
      try { await navigator.clipboard.writeText(value); notify('Copied.'); }
      catch { notify('Clipboard unavailable. Select the evidence to copy it.'); }
    }
    function value(record, field) {
      if (field === 'id' || field === 'state') return record[field];
      if (field === 'peak_ram_mib') return record.cpu_ram;
      if (field === 'answer') return record.state;
      return record.values[field];
    }
    function applyColumns() {
      const basic = ['id', 'context_tokens', 'state', 'answer', 'checks'];
      const results = data.embedding ? ['input_tokens', 'embedding_tokens_per_second', 'embeddings_per_second', 'embedding_dimensions'] : ['input_tokens', 'output_tokens'];
      const timing = data.embedding ? ['embedding_tokens_per_second', 'embeddings_per_second', 'request_seconds', 'load_seconds'] : ['prompt_tokens_per_second', 'generation_tokens_per_second', 'ttft_seconds', 'request_seconds', 'load_seconds'];
      const chosen = new Set([...basic, ...(columns === 'results' ? results : columns === 'timing' ? timing : ['peak_gpu_mib', 'peak_ram_mib'])]);
      headings.forEach((th, index) => { th.hidden = columns !== 'all' && !chosen.has(fields[index]); });
      records.forEach(record => [...record.element.cells].forEach((cell, index) => { cell.hidden = headings[index].hidden; }));
      $$('[data-columns]').forEach(button => { const active = button.dataset.columns === columns; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
      const empty = $('[data-filter-empty] td, [data-empty-row] td');
      if (empty) empty.colSpan = headings.filter(th => !th.hidden).length;
    }
    function select(record, preserveView = true) {
      selected = record || null;
      records.forEach(item => item.element.setAttribute('aria-selected', String(item === selected)));
      $$('[data-sample-evidence]').forEach(panel => { panel.hidden = !selected || panel.dataset.sampleEvidence !== String(selected.index); });
      const index = filtered.indexOf(selected);
      $('[data-evidence-empty]').hidden = !!selected;
      $('[data-evidence-controls]').hidden = !selected;
      $('[data-evidence-tabs]').hidden = !selected;
      $('[data-previous-sample]').disabled = index <= 0;
      $('[data-next-sample]').disabled = index < 0 || index >= filtered.length - 1;
      const tabs = $('[data-evidence-tabs]'); tabs.replaceChildren();
      if (!selected) return;
      $('[data-selected-label]').textContent = `${selected.id} · ${index + 1} of ${filtered.length} filtered samples`;
      const card = $(`#sample-${selected.index}`); card.open = true;
      const sections = [...card.querySelectorAll('.sample-section')];
      if (!preserveView || !sections.some(section => section.querySelector('summary').textContent === view)) view = sections[0]?.querySelector('summary').textContent;
      sections.forEach((section, i) => {
        const summary = section.querySelector('summary'), title = summary.textContent, active = title === view;
        section.id = `sample-${selected.index}-section-${i}`;
        section.hidden = !active; section.open = active; summary.hidden = true;
        const button = clone('run-evidence-tab'); button.textContent = title;
        button.dataset.evidenceTab = title; button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active)); button.setAttribute('aria-controls', section.id); tabs.append(button);
      });
    }
    function render() {
      const query = $('#sample-search').value.toLocaleLowerCase().trim(), outcome = $('#outcome').value, budget = $('#budget').value;
      const state = {pass: 'passed', fail: 'failed'}[outcome] || outcome;
      filtered = records.filter(record => (record.element.dataset.search.includes(query)) &&
        (budget === 'all' || String(record.budget) === budget) &&
        (outcome === 'all' || record.state === state || outcome === 'unscored' && ['unsupported', 'unavailable'].includes(record.state) || outcome === 'regression' && changes.get(record.id) === 'new failure'));
      filtered.sort((a, b) => {
        const av = value(a, sortKey), bv = value(b, sortKey);
        if (av == null) return bv == null ? 0 : 1;
        if (bv == null) return -1;
        return (typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv), undefined, {numeric: true})) * (ascending ? 1 : -1);
      });
      const visible = new Set(filtered);
      $('[data-filter-empty]')?.remove();
      records.forEach(record => { record.element.hidden = !visible.has(record); });
      filtered.forEach(record => body.append(record.element));
      if (!filtered.length && records.length) {
        const row = document.querySelector('#empty-row-template').content.firstElementChild.cloneNode(true);
        row.dataset.filterEmpty = ''; row.cells[0].textContent = 'No matching samples. Reset filters to see all saved evidence.';
        body.append(row);
      }
      headings.forEach((th, i) => { if (fields[i] === sortKey) th.setAttribute('aria-sort', ascending ? 'ascending' : 'descending'); else th.removeAttribute('aria-sort'); });
      $('#visible-count').textContent = `${filtered.length} of ${records.length} samples`;
      $('#export-samples').disabled = !filtered.length;
      applyColumns(); select(visible.has(selected) ? selected : filtered[0]);
    }
    function reset() { $('#sample-search').value = ''; $('#outcome').value = 'all'; $('#budget').value = 'all'; }
    function anchor() {
      const match = /^#sample-(\d+)(?:-section-\d+)?$/.exec(location.hash);
      const record = match && records.find(record => record.index === Number(match[1]));
      if (record) { if (!filtered.includes(record)) { reset(); render(); } select(record); }
    }
    headings.forEach((th, index) => {
      const title = th.textContent, button = clone('run-sort-button'); button.textContent = `${title} ↕`;
      button.dataset.runSort = fields[index]; th.replaceChildren(button); th.scope = 'col';
    });
    $('[data-sample-controls]').hidden = false;
    $$('[data-inspect], [data-copy-fingerprint]').forEach(control => { control.hidden = false; });
    root.addEventListener('input', event => { if (event.target.id === 'sample-search') render(); });
    root.addEventListener('change', event => { if (['outcome', 'budget'].includes(event.target.id)) render(); });
    root.addEventListener('click', event => {
      const button = event.target.closest('button');
      const link = event.target.closest('a[href^="#sample-"]');
      if (link) { const record = records.find(r => `#sample-${r.index}` === link.getAttribute('href')); if (record) select(record); }
      if (!button) return;
      if (button.dataset.columns) { columns = button.dataset.columns; applyColumns(); }
      if (button.id === 'clear-filters') { reset(); render(); }
      if (button.dataset.runSort) { ascending = sortKey === button.dataset.runSort ? !ascending : true; sortKey = button.dataset.runSort; render(); }
      if (button.dataset.evidenceTab) { view = button.dataset.evidenceTab; try { sessionStorage.setItem(tabKey, view); } catch { /* Keep the current in-memory view. */ } select(selected); $('[data-evidence-tab][aria-pressed="true"]').focus({preventScroll: true}); }
      if (button.dataset.inspect) { reset(); $('#outcome').value = button.dataset.inspect; if (!data.quality) columns = 'timing'; render(); $('#samples-panel').scrollIntoView({block: 'start'}); $('#outcome').focus({preventScroll: true}); }
      if (button.hasAttribute('data-previous-sample') || button.hasAttribute('data-next-sample')) {
        const record = filtered[filtered.indexOf(selected) + (button.hasAttribute('data-next-sample') ? 1 : -1)];
        if (record) { select(record); history.replaceState(null, '', `#sample-${record.index}`); }
      }
      if (button.hasAttribute('data-copy-fingerprint')) copy(data.fingerprint);
      if (button.hasAttribute('data-copy-evidence') && selected) copy($(`#sample-${selected.index} .sample-section:not([hidden])`)?.innerText || '');
      if (button.id === 'export-samples') {
        const keys = ['id', 'state', ...new Set(data.values.flatMap(Object.keys)), ...(data.rows.some(row => row.cpu) && !data.values.some(values => 'peak_ram_mib' in values) ? ['peak_ram_mib'] : [])];
        const rows = [keys, ...filtered.map(record => keys.map(key => value(record, key) ?? ''))];
        const csv = rows.map(row => row.map(cell => {
          const value = typeof cell === 'object' ? JSON.stringify(cell) : String(cell);
          const safe = typeof cell === 'string' && /^[=+\-@\t\r]/.test(value) ? "'" + value : value;
          return `"${safe.replaceAll('"', '""')}"`;
        }).join(',')).join('\r\n');
        const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
        const a = document.createElement('a'); a.href = url; a.download = 'benchmark-samples.csv'; a.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000); notify(`${filtered.length} filtered samples exported.`);
      }
    });
    window.addEventListener('hashchange', () => { if (root.isConnected) anchor(); });
    root.drawRunChart = () => drawChart(root, data);
    const observer = new ResizeObserver(() => { if (root.isConnected) root.drawRunChart(); else observer.disconnect(); });
    observer.observe($('[data-budget-chart]'));
    render(); anchor(); root.drawRunChart();
  }

  function drawChart(root, data) {
    const container = root.querySelector('[data-budget-chart]');
    const points = data.chart;
    container.replaceChildren();
    if (!points.length) { container.textContent = 'No chart measurements recorded.'; return; }
    const width = Math.max(270, container.clientWidth), height = 208, ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.setAttribute('aria-hidden', 'true');
    const add = (tag, attributes, content) => { const node = document.createElementNS(ns, tag); Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, value)); if (content != null) node.textContent = content; svg.append(node); return node; };
    const format = value => !Number.isFinite(value) ? '—' : data.chart_unit === 's' && value > 0 && value < 1 ? value < .001 ? '<0.001' : value.toFixed(3) : value.toLocaleString(undefined, {maximumFractionDigits: 1});
    const finite = points.map(point => point.value).filter(Number.isFinite);
    const max = data.chart_unit === '%' ? Math.max(100, ...finite) : Math.max(1, ...finite) * 1.12;
    for (let i = 0; i <= 4; i++) { const y = 170 - i * 34;
      add('line', {x1: 44, x2: width - 20, y1: y, y2: y, class: 'br-grid-line'});
      add('text', {x: 36, y: y + 4, 'text-anchor': 'end'}, format(max * i / 4));
    }
    let segment = [];
    const finish = () => { if (segment.length) add('polyline', {points: segment.join(' '), class: 'br-trend'}); segment = []; };
    points.forEach((point, index) => {
      const x = points.length === 1 ? (width + 35) / 2 : 55 + index * (width - 85) / (points.length - 1), y = 170 - (point.value || 0) / max * 136;
      if (Number.isFinite(point.value)) segment.push(`${x},${y}`); else finish();
      if (Number.isFinite(point.value)) {
        const dot = add('circle', {cx: x, cy: y, r: 4, class: 'br-dot'});
        const title = document.createElementNS(ns, 'title'); title.textContent = `${point.budget.toLocaleString()} tokens: ${format(point.value)} ${data.chart_unit}`; dot.append(title);
        if (points.length <= 6) add('text', {x, y: y - 12, 'text-anchor': 'middle', class: 'br-point-value'}, format(point.value));
      }
      if (index === points.length - 1 || index % Math.max(1, Math.ceil(points.length / 5)) === 0) add('text', {x, y: 198, 'text-anchor': 'middle'}, point.budget >= 1024 ? `${+(point.budget / 1024).toFixed(1)}K` : point.budget);
    }); finish();
    container.setAttribute('role', 'img'); container.setAttribute('aria-label', `${data.chart_label}. ${points.map(p => `${p.budget} tokens: ${format(p.value)} ${data.chart_unit}`).join('; ')}`);
    container.append(svg);
  }
  initialize();
  document.addEventListener('htmx:afterSwap', initialize);
  document.addEventListener('weightroom:theme', () => document.querySelector('[data-run-briefing]')?.drawRunChart?.());
})();
