(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const saved = JSON.parse($('#sample-data').textContent);
  const clone = id => document.getElementById(id).content.firstElementChild.cloneNode(true);
  const text = (root, selector, value) => { root.querySelector(selector).textContent = value; };
  const fmt = value => Number.isFinite(value) ? value.toLocaleString('en-US', {maximumFractionDigits: 1}) : '—';
  const seconds = value => Number.isFinite(value) ? value > 0 && value < 1 ? value < .001 ? '<0.001' : value.toFixed(3) : value.toFixed(2) : '—';
  const percent = value => Number.isFinite(value) ? `${value.toFixed(1)}%` : '—';
  const median = values => { const a = values.filter(Number.isFinite).sort((a, b) => a - b); return a.length ? (a[Math.floor((a.length - 1) / 2)] + a[Math.floor(a.length / 2)]) / 2 : null; };
  const profiles = {
    quality: {name: 'Structured extraction', score: 'Correct fields', key: 'accuracy', unit: '%', time: 'Median first token'},
    speed: {name: 'Generation speed', score: 'Median write speed', key: 'generation_tokens_per_second', unit: 'tokens/s', time: 'Median first token'},
    embedding: {name: 'Embedding throughput', score: 'Median input throughput', key: 'embedding_tokens_per_second', unit: 'tokens/s', time: 'Median request time'},
  };
  let layout = 'briefing', profile = 'quality', scenario = 'normal', columns = 'results';
  let selected = 'S043', evidenceView = 'answer', sortKey = 'id', sortDirection = 1, compared = 'previous', feedbackTimer;
  let samples = [], visible = [];
  const quality = () => profile === 'quality';
  const embedding = () => profile === 'embedding';
  const modelName = () => embedding() ? 'Qwen3 Embedding 8B' : saved.model;
  const available = sample => ['pass', 'fail'].includes(sample.state);
  function notify(message) {
    clearTimeout(feedbackTimer); $('#feedback').hidden = false; $('#feedback').textContent = message;
    feedbackTimer = setTimeout(() => { $('#feedback').hidden = true; }, 5000);
  }
  function metric(items) {
    if (!quality()) return median(items.map(s => s.values[profiles[profile].key]));
    const supported = items.filter(s => s.state !== 'unsupported');
    return supported.length ? 100 * supported.reduce((n, s) => n + s.values.correct, 0) / (supported.length * 4) : null;
  }
  const metricText = items => quality() ? percent(metric(items)) : fmt(metric(items));
  function badge(element, sample) {
    const state = typeof sample === 'string' ? sample : sample.state;
    const label = {pass: quality() ? 'Pass' : 'Available', fail: 'Fail', unsupported: 'Unsupported', unavailable: 'Unavailable', completed: 'Completed', failed: 'Failed'}[state];
    element.className = `badge bg-${{pass: 'green', fail: 'red', unsupported: 'secondary', unavailable: 'orange', completed: 'green', failed: 'red'}[state]}-lt`;
    element.textContent = label;
  }
  function prepare() {
    samples = scenario === 'empty' ? [] : structuredClone(saved.samples).slice(0, scenario === 'failed' ? 19 : 48);
    if (!quality()) samples.forEach(s => {
      if (s.state === 'fail') s.state = 'pass';
      s.category = embedding() ? 'Document vectors' : 'Prompt throughput';
      s.title = `${s.category} / probe ${s.id.slice(1)}`;
      if (embedding()) {
        s.values.request_seconds = available(s) ? s.values.input_tokens / s.values.embedding_tokens_per_second : null;
        s.values.embeddings_per_second = available(s) ? 1 / s.values.request_seconds : null;
        ['output_tokens', 'generation_tokens_per_second', 'prompt_tokens_per_second', 'ttft_seconds'].forEach(key => { delete s.values[key]; });
      }
    });
    if (!samples.some(s => s.id === selected)) selected = samples[0]?.id || '';
  }
  function compareState(sample) {
    if (!quality() || compared !== 'previous') return '—';
    if (sample.state === 'unsupported') return 'Unsupported';
    if (sample.state === 'unavailable') return 'Unavailable';
    if (sample.baseline === 'pass' && sample.state === 'fail') return 'New failure';
    if (sample.baseline === 'fail' && sample.state === 'pass') return 'Improved';
    return sample.state === 'pass' ? 'Still passing' : 'Still failing';
  }
  function renderPassport() {
    const facts = {Node: saved.node, Host: saved.host, Engine: saved.engine, Build: saved.engine_hash,
      Hardware: saved.hardware, Context: '32,768 tokens', Benchmark: `v${saved.version}`, Reason: 'Manual request',
      'Started by': 'jordan', Finished: scenario === 'failed' ? '10:44:18 PST · stopped' : saved.ended};
    $('#passport-facts').replaceChildren();
    Object.entries(facts).forEach(([label, value]) => { const row = clone('fact-template'); text(row, '[data-fact-label]', label); text(row, '[data-fact-value]', value); $('#passport-facts').append(row); });
    $('#recorded-settings').textContent = JSON.stringify({subject: {...saved.subject,
      protocol: {...saved.subject.protocol, dataset: quality() ? 'extraction-v3' : embedding() ? 'embedding-throughput-v1' : 'speed-v1', embedding: embedding()}}, environment: saved.environment}, null, 2);
    const messages = scenario === 'empty' ? [] : scenario === 'failed' ? [...saved.messages.slice(0, 3), {at: '10:44:18', text: 'Worker connection closed · run failed · 19 partial samples retained'}] : saved.messages;
    $('#run-log').textContent = messages.length ? messages.map(item => `${item.at}  ${item.text}`).join('\n') : 'No log messages recorded.';
  }
  function renderSummary() {
    const executed = samples.filter(available), passed = samples.filter(s => s.state === 'pass');
    const unsupported = samples.filter(s => s.state === 'unsupported').length;
    const unavailable = samples.filter(s => s.state === 'unavailable').length;
    const title = !samples.length ? 'No measurements recorded.' : scenario === 'failed' ? 'Partial evidence. No final score.' :
      quality() ? 'Accuracy softens at longer contexts.' : embedding() ? 'Throughput falls as inputs grow.' : 'Generation stays steady as inputs grow.';
    $('#verdict-title').textContent = title;
    $('#verdict-note').textContent = !samples.length ? 'The run record is retained. There is no sample evidence to inspect yet.' : quality() ?
      `${passed.length} of ${executed.length} executed cases passed every check. Coverage and unavailable attempts are reported separately below.` :
      `${executed.length} successful probes across four input budgets. Read individual measurements before comparing different contexts.`;
    $('#benchmark-name').textContent = profiles[profile].name;
    $('#run-heading .page-title').textContent = modelName();
    badge($('#run-state'), scenario === 'failed' ? 'failed' : 'completed');
    $('#score-label').textContent = profiles[profile].score;
    $('#score').textContent = scenario === 'failed' ? '—' : metricText(samples);
    $('#score-note').textContent = scenario === 'failed' ? 'run did not complete' : quality() ? 'formula: accuracy · higher is better' : `${profiles[profile].unit} · higher is better`;
    $('#pass-label').textContent = quality() ? 'All-correct cases' : 'Successful probes';
    $('#pass-count').textContent = samples.length ? `${passed.length} / ${executed.length}` : '—';
    $('#pass-note').textContent = quality() ? 'among executed cases' : 'received a complete response';
    $('#sample-count').textContent = `${samples.length} / 48`;
    $('#sample-note').textContent = `${executed.length} executed · ${unsupported} unsupported · ${unavailable} unavailable`;
    $('#time-label').textContent = profiles[profile].time;
    $('#median-time').textContent = samples.length ? `${seconds(median(executed.map(s => s.values[embedding() ? 'request_seconds' : 'ttft_seconds'])))} s` : '—';
    $('#time-note').textContent = embedding() ? 'local vector request · excludes startup' : 'after model loading';
    $('#finding-title').textContent = quality() ? `${samples.filter(s => s.state === 'fail').length} cases contain failed checks` : 'Read throughput with its input size';
    $('#finding-note').textContent = quality() ? 'The answer can be valid JSON and still contain an incorrect field. Review the expected and actual values.' : 'A pooled median is a summary. Per-input measurements show how this configuration handles longer prompts.';
    $('#show-failures').textContent = quality() ? 'Inspect failing cases →' : 'Inspect timing measurements →';
    $('#coverage-title').textContent = `${unsupported + unavailable} samples have no scored response`;
    $('#coverage-note').textContent = `${unsupported} unsupported configurations; ${unavailable} requests unavailable. These are distinct from failed answer checks.`;
    $('#formula-note').textContent = quality() ? 'Score = accuracy = correct field checks / supported field checks × 100. Unsupported attempts are excluded; unavailable attempts keep their checks and receive no credit. The all-correct count above uses executed cases only; coverage shows every recorded attempt.' :
      `This proposal summarizes the median of successful ${embedding() ? 'input-token throughput measurements' : 'generation-token throughput measurements'}. Load, tokenization and warm-up are excluded from embedding request throughput. Sample JSON retains every original value.`;
    $('#state-note').hidden = !['stale', 'failed', 'empty'].includes(scenario);
    $('#state-message').textContent = {stale: 'Settings changed since this run. These results use the recorded 32,768-token configuration. No automatic rerun has occurred.', failed: 'Worker connection closed. Partial samples are retained for investigation; a final run score is unavailable.', empty: 'This saved run has no sample receipts. Settings and run identity remain available.'}[scenario] || '';
    $('#breakdown').hidden = !quality();
    $('#comparison-panel').hidden = !quality();
    $('#show-failures').disabled = !samples.length;
    $('#show-unavailable').disabled = unsupported + unavailable === 0;
  }
  function renderGroups() {
    $('#group-rows').replaceChildren();
    const groups = new Map();
    samples.forEach(s => { const key = `${s.budget}/${s.category}`; if (!groups.has(key)) groups.set(key, []); groups.get(key).push(s); });
    groups.forEach(items => {
      const row = clone('group-row-template'); text(row, '[data-group-name]', items[0].category); text(row, '[data-group-budget]', fmt(items[0].budget));
      text(row, '[data-group-accuracy]', metricText(items)); row.querySelector('[data-group-bar]').style.width = `${metric(items) || 0}%`;
      text(row, '[data-group-passed]', `${items.filter(s => s.state === 'pass').length} / ${items.length}`);
      text(row, '[data-group-executed]', `${items.filter(available).length} / ${items.length}`);
      text(row, '[data-group-unsupported]', items.filter(s => s.state === 'unsupported').length); $('#group-rows').append(row);
    });
    if (!groups.size) empty($('#group-rows'), 6, 'No category results recorded.');
  }
  function empty(root, span, message) {
    const row = clone('empty-template'); row.firstElementChild.colSpan = span; row.firstElementChild.textContent = message; root.append(row);
  }
  function renderComparison() {
    const compatible = compared === 'previous';
    $('#comparison-note').textContent = compatible ? 'Same frozen dataset, protocol and grader. Counts include only the recorded cases in this run.' : compared === 'none' ? 'Choose a compatible baseline to compare frozen cases.' : 'Comparison unavailable: dataset or grader differs. Scores and case changes are not treated as comparable.';
    $('#comparison-counts').replaceChildren();
    if (compatible) {
      const counts = new Map(); samples.forEach(s => { const key = compareState(s); counts.set(key, (counts.get(key) || 0) + 1); });
      counts.forEach((count, label) => { const span = document.createElement('span'); span.className = `badge bg-${label === 'New failure' ? 'red' : label === 'Improved' ? 'green' : 'secondary'}-lt`; span.textContent = `${label}: ${count}`; $('#comparison-counts').append(span); });
    }
    $('#show-regressions').disabled = !compatible || !samples.some(s => compareState(s) === 'New failure');
    $('#outcome option[value="regression"]').disabled = !compatible || !quality();
  }
  function columnKeys() {
    const base = ['id', 'context_tokens', 'outcome'];
    const results = embedding() ? ['input_tokens', 'embedding_tokens_per_second', 'embeddings_per_second', 'embedding_dimensions'] : ['input_tokens', 'output_tokens'];
    const timing = embedding() ? ['embedding_tokens_per_second', 'embeddings_per_second', 'request_seconds', 'load_seconds'] : ['prompt_tokens_per_second', 'generation_tokens_per_second', 'ttft_seconds', 'request_seconds', 'load_seconds'];
    const memory = ['peak_gpu_mib', 'peak_ram_mib'];
    return [...base, ...(columns === 'results' ? results : columns === 'timing' ? timing : columns === 'memory' ? memory : [...new Set([...results, ...timing, ...memory])]), ...(quality() && compared === 'previous' ? ['change'] : [])];
  }
  function filtered() {
    const query = $('#sample-search').value.toLowerCase(), outcome = $('#outcome').value, budget = $('#budget').value;
    return samples.filter(s => `${s.id} ${s.case} ${s.title} ${s.category}`.toLowerCase().includes(query) &&
      (budget === 'all' || String(s.budget) === budget) && (outcome === 'all' || outcome === s.state ||
        outcome === 'unscored' && !available(s) || outcome === 'regression' && compareState(s) === 'New failure'))
      .sort((a, b) => {
        const av = sortKey === 'id' ? a.id : sortKey === 'outcome' ? a.state : a.values[sortKey];
        const bv = sortKey === 'id' ? b.id : sortKey === 'outcome' ? b.state : b.values[sortKey];
        if (av == null) return bv == null ? 0 : 1;
        if (bv == null) return -1;
        return (typeof av === 'number' ? av - bv : String(av).localeCompare(String(bv))) * sortDirection;
      });
  }
  function renderSamples() {
    visible = filtered(); const keys = columnKeys(); $('#sample-rows').replaceChildren();
    if (!visible.some(s => s.id === selected)) selected = visible[0]?.id || '';
    visible.forEach(s => {
      const row = clone('sample-row-template'); row.dataset.sampleRow = s.id; row.setAttribute('aria-selected', String(s.id === selected));
      row.querySelector('[data-open-sample]').dataset.openSample = s.id;
      row.querySelector('[data-open-sample]').setAttribute('aria-label', `Inspect ${s.id}: ${s.title}`);
      text(row, '[data-sample-id]', s.id); text(row, '[data-sample-title]', s.title); badge(row.querySelector('[data-outcome]'), s);
      text(row, '[data-checks]', quality() && available(s) ? `${s.values.correct} / ${s.values.checks} checks` : '');
      row.querySelectorAll('[data-cell]').forEach(cell => { const key = cell.dataset.cell; cell.hidden = !keys.includes(key);
        cell.textContent = key.endsWith('_seconds') ? seconds(s.values[key]) : key === 'embeddings_per_second' ? (Number.isFinite(s.values[key]) ? s.values[key].toFixed(3) : '—') : fmt(s.values[key]); });
      row.querySelector('[data-change]').hidden = !keys.includes('change'); text(row, '[data-change]', compareState(s));
      $('#sample-rows').append(row);
    });
    $$('#sample-table [data-column]').forEach(th => { th.hidden = !keys.includes(th.dataset.column); });
    $$('#sample-table thead th').forEach(th => { const active = th.querySelector('[data-sort]')?.dataset.sort === sortKey; if (active) th.setAttribute('aria-sort', sortDirection === 1 ? 'ascending' : 'descending'); else th.removeAttribute('aria-sort'); });
    if (!visible.length) empty($('#sample-rows'), keys.length, samples.length ? 'No matching samples. Reset filters to see all saved evidence.' : 'No samples recorded for this run.');
    $('#visible-count').textContent = `${visible.length} of ${samples.length} samples`;
    $('#export-samples').disabled = !visible.length;
    $('#table-note').textContent = embedding() ? 'Context is allocated capacity. Input tokens form one pooled vector; request time excludes tokenization and startup.' : 'Context is allocated capacity; input and output are actual token counts.';
    $$('[data-columns]').forEach(button => { const active = button.dataset.columns === columns; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
    renderEvidence();
    requestAnimationFrame(keepSelectionVisible);
  }
  function keepSelectionVisible() {
    const scroller = $('.br-sample-scroll'), row = $('#sample-rows tr[aria-selected="true"]');
    if (!row) return;
    const box = scroller.getBoundingClientRect(), selectedBox = row.getBoundingClientRect();
    const header = $('#sample-table thead').getBoundingClientRect().height;
    if (selectedBox.top < box.top + header) scroller.scrollTop += selectedBox.top - box.top - header;
    else if (selectedBox.bottom > box.bottom) scroller.scrollTop += selectedBox.bottom - box.bottom;
  }
  function evidence(sample, view) {
    if (view === 'input') return `${sample.evidence.prompt}\n\nToken digest: ${sample.evidence.prompt_tokens_sha256}`;
    if (view === 'measurements') return JSON.stringify(sample.values, null, 2);
    if (view === 'server') return sample.log;
    if (view === 'receipt') return JSON.stringify(embedding() ? {request: {input_tokens: sample.values.input_tokens}, dimensions: sample.values.embedding_dimensions,
      vector: available(sample) ? '[Illustrative receipt; vector values omitted.]' : null, resource: sample.evidence.resource,
      prompt_tokens_sha256: sample.evidence.prompt_tokens_sha256} : {case: sample.case, usage: {input_tokens: sample.values.input_tokens, output_tokens: sample.values.output_tokens},
      timings: {request_seconds: sample.values.request_seconds}, resource: sample.evidence.resource}, null, 2);
    if (view === 'checks') return JSON.stringify(sample.evidence.case, null, 2);
    return available(sample) ? sample.evidence[view === 'answer' ? 'answer' : 'output'] || sample.output : 'No answer recorded for this attempt.';
  }
  function renderEvidence() {
    const sample = samples.find(s => s.id === selected); $('#evidence-empty').hidden = !!sample; $('#evidence-content').hidden = !sample;
    const index = visible.findIndex(s => s.id === selected);
    $('#previous-sample').disabled = !sample || index <= 0; $('#next-sample').disabled = !sample || index >= visible.length - 1;
    if (!sample) return;
    $('#selected-id').textContent = `${sample.id} / ${index + 1} of ${visible.length} filtered samples`;
    $('#selected-title').textContent = sample.title;
    $('#selected-meta').textContent = `${fmt(sample.budget)} context tokens · ${sample.case} · ${saved.node}`;
    badge($('#selected-outcome'), sample); $('#sample-error').hidden = !sample.values.error; $('#sample-error').textContent = sample.values.error || '';
    $('#check-rows').replaceChildren();
    (available(sample) ? sample.evidence.checks : []).forEach(check => {
      const row = clone('check-template'); text(row, '[data-field]', check.field); text(row, '[data-expected]', JSON.stringify(check.expected)); text(row, '[data-actual]', JSON.stringify(check.actual)); badge(row.querySelector('[data-result]'), check.passed ? 'pass' : 'fail'); $('#check-rows').append(row);
    });
    if (!available(sample)) empty($('#check-rows'), 4, 'No graded answer is available.');
    if (embedding() && ['answer', 'checks', 'raw'].includes(evidenceView) || !quality() && evidenceView === 'checks') evidenceView = 'receipt';
    $$('[data-evidence]').forEach(button => {
      button.hidden = embedding() && ['answer', 'checks', 'raw'].includes(button.dataset.evidence) || !quality() && button.dataset.evidence === 'checks';
      const active = button.dataset.evidence === evidenceView; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    });
    $$('[data-evidence-panel]').forEach(panel => { const key = panel.dataset.evidencePanel; panel.hidden = key !== evidenceView; panel.querySelector('[data-evidence-text]').textContent = evidence(sample, key); });
    $('#evidence-caption').textContent = `${sample.id} · saved ${evidenceView === 'checks' ? 'checks and answer key' : evidenceView} · illustrative fixture`;
  }
  function drawChart() {
    const root = $('#budget-chart'); root.replaceChildren();
    const budgets = [2048, 8192, 16384, 32768], values = budgets.map(budget => metric(samples.filter(s => s.budget === budget)));
    const svgNS = 'http://www.w3.org/2000/svg', width = Math.max(285, root.clientWidth), height = 208;
    const svg = document.createElementNS(svgNS, 'svg'); svg.setAttribute('viewBox', `0 0 ${width} ${height}`); svg.setAttribute('aria-hidden', 'true');
    const element = (tag, attrs, content) => { const el = document.createElementNS(svgNS, tag); Object.entries(attrs).forEach(([k, v]) => el.setAttribute(k, v)); if (content != null) el.textContent = content; svg.append(el); return el; };
    const max = quality() ? 100 : Math.ceil(Math.max(...values.filter(Number.isFinite), 1) / (embedding() ? 500 : 20)) * (embedding() ? 500 : 20);
    for (let i = 0; i <= 4; i++) { const y = 170 - i * 34; element('line', {x1: 40, x2: width - 22, y1: y, y2: y, class: 'br-grid-line'}); element('text', {x: 32, y: y + 4, 'text-anchor': 'end'}, fmt(max * i / 4)); }
    const points = values.map((value, i) => ({value, x: 55 + i * (width - 90) / 3, y: 170 - (value || 0) / max * 136}));
    let segment = [];
    const finish = () => { if (segment.length) element('polyline', {points: segment.map(p => `${p.x},${p.y}`).join(' '), class: 'br-trend'}); segment = []; };
    points.forEach(p => { if (Number.isFinite(p.value)) segment.push(p); else finish(); }); finish();
    points.forEach((p, i) => {
      element('text', {x: p.x, y: 199, 'text-anchor': 'middle'}, `${budgets[i] / 1024}K`);
      if (Number.isFinite(p.value)) { element('circle', {cx: p.x, cy: p.y, r: 4.5, class: 'br-dot'}); element('text', {x: p.x, y: p.y - 13, 'text-anchor': 'middle', class: 'br-point-value'}, quality() ? percent(p.value) : fmt(p.value)); }
    }); root.append(svg);
    const summary = budgets.map((budget, i) => `${fmt(budget)} tokens: ${quality() ? percent(values[i]) : fmt(values[i])}`).join('; ');
    root.setAttribute('aria-label', `${profiles[profile].score} by input budget. ${summary}`);
    $('#chart-unit').textContent = `${profiles[profile].score}${quality() ? ' (%)' : ' (tokens/s)'}`;
    $('#chart-description').textContent = samples.length ? 'Unsupported configurations are excluded. Missing measurements never become zero.' : 'No chart measurements recorded. Input budgets are shown for reference.';
  }
  function setLayout(value) {
    layout = ['briefing', 'ledger', 'explorer'].includes(value) ? value : 'briefing'; $('#workspace').dataset.layout = layout;
    $$('[data-design]').forEach(button => { const active = button.dataset.design === layout; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
    $('#design-description').textContent = {briefing: 'Recommended · A concise result, the context-size trend and the evidence that deserves attention.', ledger: 'For comparison work · A fixed run passport beside a dense, sortable measurement ledger.', explorer: 'For investigation · A sample browser beside its answer, checks and complete measurement receipt.'}[layout];
    const url = new URL(location.href); url.searchParams.set('design', layout); history.replaceState(null, '', url);
    requestAnimationFrame(() => { drawChart(); keepSelectionVisible(); });
  }
  function render() { prepare(); $('#workspace').dataset.profile = profile; renderPassport(); renderSummary(); renderGroups(); renderComparison(); renderSamples(); requestAnimationFrame(drawChart); }
  function clearFilters() { $('#sample-search').value = ''; $('#outcome').value = 'all'; $('#budget').value = 'all'; }
  function focusSamples(outcome) { clearFilters(); $('#outcome').value = outcome; renderSamples(); $('#samples-panel').scrollIntoView({behavior: 'instant', block: 'start'}); $('#outcome').focus({preventScroll: true}); }
  async function copy(value, message) { try { await navigator.clipboard.writeText(value); notify(message); } catch { const input = document.createElement('textarea'); input.value = value; input.style.position = 'fixed'; input.style.opacity = '0'; document.body.append(input); input.select(); const done = document.execCommand('copy'); input.remove(); notify(done ? message : 'Clipboard unavailable. Use Export JSON to save this evidence.'); } }
  function download(name, content, type) { const url = URL.createObjectURL(new Blob([content], {type})); const a = document.createElement('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
  document.addEventListener('click', event => {
    const button = event.target.closest('button'); if (!button) return;
    if (button.dataset.design) setLayout(button.dataset.design);
    if (button.dataset.columns) { columns = button.dataset.columns; renderSamples(); }
    if (button.dataset.openSample) { selected = button.dataset.openSample; renderSamples(); $('#evidence-panel').scrollIntoView({behavior: 'instant', block: 'nearest'}); }
    if (button.dataset.evidence) { evidenceView = button.dataset.evidence; renderEvidence(); }
    if (button.dataset.sort) { sortDirection = sortKey === button.dataset.sort ? -sortDirection : 1; sortKey = button.dataset.sort; renderSamples(); }
  });
  $('#sample-search').addEventListener('input', renderSamples);
  $('#outcome').addEventListener('change', renderSamples); $('#budget').addEventListener('change', renderSamples);
  $('#clear-filters').addEventListener('click', () => { clearFilters(); renderSamples(); });
  $('#profile').addEventListener('change', () => { profile = $('#profile').value; clearFilters(); evidenceView = embedding() ? 'receipt' : 'answer'; render(); });
  $('#scenario').addEventListener('change', () => { scenario = $('#scenario').value; clearFilters(); render(); });
  $('#show-failures').addEventListener('click', () => { if (!quality()) columns = 'timing'; focusSamples(quality() ? 'fail' : 'all'); });
  $('#show-unavailable').addEventListener('click', () => focusSamples('unscored'));
  $('#show-regressions').addEventListener('click', () => focusSamples('regression'));
  $('#compare').addEventListener('click', () => { compared = $('#baseline').value; if (compared !== 'previous' && $('#outcome').value === 'regression') $('#outcome').value = 'all'; renderComparison(); renderSamples(); notify(compared === 'previous' ? 'Matching frozen cases compared.' : compared === 'none' ? 'Baseline cleared.' : 'Comparison unavailable: the frozen dataset or grader differs.'); });
  ['previous', 'next'].forEach(direction => $(`#${direction}-sample`).addEventListener('click', () => { const i = visible.findIndex(s => s.id === selected); selected = visible[i + (direction === 'next' ? 1 : -1)]?.id || selected; renderSamples(); }));
  $('#copy-fingerprint').addEventListener('click', () => copy(saved.fingerprint, 'Fingerprint copied.'));
  $('#copy-summary').addEventListener('click', () => copy(`${modelName()} · ${profiles[profile].name} v${saved.version}\n${saved.node} · ${saved.id}\n${$('#score-label').textContent}: ${$('#score').textContent}\n${$('#sample-note').textContent}\nFingerprint: ${saved.fingerprint}`, 'Run summary copied.'));
  $('#copy-evidence').addEventListener('click', () => { const sample = samples.find(s => s.id === selected); if (sample) copy(evidence(sample, evidenceView), `${sample.id} evidence copied.`); });
  $('#export-run').addEventListener('click', () => { download(`${saved.id}.json`, JSON.stringify({...saved, model: modelName(), profile, scenario, score: scenario === 'failed' ? null : metric(samples), samples}, null, 2), 'application/json'); notify('Sample run exported as JSON.'); });
  $('#export-samples').addEventListener('click', () => { const keys = ['id', 'case', 'state', ...columnKeys().filter(k => !['id', 'outcome', 'change'].includes(k))];
    const csv = [keys, ...visible.map(s => keys.map(key => s[key] ?? s.values[key] ?? ''))].map(row => row.map(v => `"${String(v).replaceAll('"', '""')}"`).join(',')).join('\r\n');
    download(`${saved.id}-samples.csv`, csv, 'text/csv'); notify(`${visible.length} filtered sample rows exported.`);
  });
  $('#theme').addEventListener('click', () => { const dark = document.documentElement.dataset.bsTheme !== 'dark', control = $('[data-theme-form] [name="mode"]'); control.value = dark ? 'dark' : 'light'; control.dispatchEvent(new Event('change', {bubbles: true})); $('#theme').textContent = dark ? 'Light mode' : 'Dark mode'; });
  document.addEventListener('weightroom:theme', drawChart); window.addEventListener('resize', drawChart);
  setLayout(new URLSearchParams(location.search).get('design')); render();
})();
