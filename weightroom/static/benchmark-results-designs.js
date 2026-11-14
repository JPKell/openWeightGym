// Portable proposals share one state and the live page's server-rendered evidence tables.
(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('results-data').textContent);
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const page = $('#results');
  const benches = new Map(data.benchmarks.map(b => [b.id, b]));
  const models = new Map(data.models.map(m => [m.id, m]));
  const state = { layout: 'briefing', model: 'Qwen3-32B', benchmark: 'json_schema',
    baseline: 'Qwen3-8B', query: '', evidence: 'all', scenario: 'normal' };
  const descriptions = {
    briefing: 'Full benchmark tables with grouped columns and pinned model names.',
    atlas: 'A metric ledger: all measured fields down the page, models across columns. Recommended for dense benchmarks.',
    explorer: 'Inspect every measured field for one model beside a baseline, with the full table below.',
  };
  const fmt = value => Number.isFinite(value) ? value.toLocaleString('en-CA', {maximumFractionDigits: 1, minimumFractionDigits: 1}) : '—';
  const metricFormat = (value, unit) => {
    if (!Number.isFinite(value)) return '—';
    const text = unit === 'count' || unit === 'tokens' || unit === ''
      ? value.toLocaleString('en-CA', { maximumFractionDigits: 0 }) : fmt(value);
    return `${text}${unit === '%' ? '%' : unit && unit !== 'count' ? ` ${unit}` : ''}`;
  };
  const percentage = value => `${fmt(value)}%`;
  const result = (model, benchmark = state.benchmark) => state.scenario === 'normal' ? data.latest[model]?.[benchmark] : undefined;
  const scored = run => Number.isFinite(run?.score);
  const comparable = (run, benchmark = state.benchmark) => scored(run) && run.version === benches.get(benchmark).version;
  const mark = run => run?.status === 'settings' ? '†' : run?.status === 'protocol' ? '*' : '';
  const delta = (run, benchmark = state.benchmark) => {
    const base = result(state.baseline, benchmark);
    return comparable(run, benchmark) && comparable(base, benchmark) ? run.score - base.score : null;
  };
  const deltaText = value => value === null ? '—' : `${value > 0 ? '+' : ''}${fmt(value)} pp`;
  const announce = message => { $('#feedback').textContent = message; };
  const heatRows = $$('#benchmark-score-table tbody tr[data-model]');
  const originalCells = new Map($$('#benchmark-score-table .score-cell').map(cell => [cell, cell.cloneNode(true)]));
  const allMeasurementRows = $$('#measurement-tables tr[data-model]');
  const originalMeasurements = new Map(allMeasurementRows.map(row => [row, [...row.children].map(cell => cell.cloneNode(true))]));

  let heatmapScenario = null;
  const ledgerCells = new Map($$('#metric-ledgers td').map(cell => [cell, cell.cloneNode(true)]));

  function visibleModels() {
    if (state.scenario === 'empty') return [];
    return data.models.filter(model => {
      if (!`${model.id} ${model.family}`.toLowerCase().includes(state.query)) return false;
      const run = result(model.id);
      return state.evidence === 'all'
        || state.evidence === 'current' && run?.status === 'current'
        || state.evidence === 'review' && ['settings', 'protocol'].includes(run?.status)
        || state.evidence === 'gaps' && !scored(run);
    });
  }

  function orderedModels(visible) {
    return [...visible].sort((a, b) => {
      const left = result(a.id), right = result(b.id);
      if (!scored(left) || !scored(right)) return Number(!scored(left)) - Number(!scored(right)) || a.id.localeCompare(b.id);
      return right.score - left.score || a.id.localeCompare(b.id);
    });
  }

  function renderOverview() {
    const inventory = state.scenario === 'empty' ? [] : data.models;
    const all = inventory.flatMap(model => data.benchmarks.map(b => result(model.id, b.id)));
    const count = all.filter(scored).length;
    $('#kpi-models').textContent = inventory.length;
    $('#kpi-benchmarks').textContent = data.benchmarks.length;
    $('#kpi-coverage').textContent = all.length ? `${Math.round(count / all.length * 100)}%` : '—';
    $('#coverage-note').textContent = `${count} of ${all.length} model / benchmark pairs`;
    $('#kpi-review').textContent = all.filter(run => ['settings', 'protocol'].includes(run?.status)).length;
  }

  function renderInsights(visible) {
    const benchmark = benches.get(state.benchmark);
    const leaders = orderedModels(visible).filter(model => comparable(result(model.id)));
    const first = leaders[0], run = first && result(first.id);
    $('#insight-focus-value').textContent = first ? `${fmt(run.score)}%` : 'No final scores';
    $('#insight-focus-note').textContent = first ? `${first.id}${mark(run)} · ${benchmark.name}` : 'Choose a benchmark with completed evidence.';
    const reasoning = [...visible].filter(model => comparable(result(model.id, 'reasoning'), 'reasoning'))
      .sort((a, b) => result(b.id, 'reasoning').score - result(a.id, 'reasoning').score)[0];
    $('#insight-specialist-value').textContent = reasoning ? percentage(result(reasoning.id, 'reasoning').score) : 'Not measured';
    $('#insight-specialist-note').textContent = reasoning ? `${reasoning.id} · Multi-step reasoning` : 'Reasoning results will appear after a completed run.';
    const gaps = visible.reduce((sum, model) => sum + data.benchmarks.filter(b => !scored(result(model.id, b.id))).length, 0);
    $('#insight-coverage-value').textContent = `${gaps} unscored pairs`;
    $('#insight-coverage-note').textContent = 'Missing, partial and unsupported results stay visible. No missing value is treated as zero.';
  }

  function renderHeatmap() {
    // Measurement search, benchmark selection and evidence filters never alter this dataset.
    if (heatmapScenario === state.scenario) return;
    heatmapScenario = state.scenario;
    for (const row of heatRows) {
      row.hidden = state.scenario === 'empty';
      for (const cell of row.querySelectorAll('.score-cell')) {
        const original = originalCells.get(cell);
        if (state.scenario === 'normal') {
          cell.replaceChildren(...[...original.childNodes].map(n => n.cloneNode(true)));
          for (const attr of ['data-score', 'style', 'class', 'aria-label', 'title']) {
            if (original.hasAttribute(attr)) cell.setAttribute(attr, original.getAttribute(attr));
            else cell.removeAttribute(attr);
          }
        } else {
          cell.textContent = '—'; cell.dataset.score = '';
          cell.removeAttribute('style'); cell.classList.add('score-missing');
          cell.setAttribute('aria-label', `${row.dataset.model}, ${benches.get(cell.dataset.resultBench).name}: Not run`);
          cell.title = 'Not run';
        }
      }
    }
    $('#heatmap-count').textContent = `${state.scenario === 'empty' ? 0 : data.models.length} models · ${data.benchmarks.length} benchmarks`;
    $('#heatmap-empty').hidden = state.scenario !== 'empty';
    $('#benchmark-heatmap-scroll').hidden = state.scenario === 'empty';
    $('#benchmark-score-table table').dispatchEvent(new Event('weightroom:benchmark-refresh'));
  }

  function renderRanking(visible) {
    const b = benches.get(state.benchmark);
    $('#ranking-title').textContent = b.name;
    $('#ranking-subtitle').textContent = `${b.score_label} · protocol v${b.version}`;
    const list = $('#ranking');
    list.replaceChildren();
    const eligible = orderedModels(visible).filter(model => comparable(result(model.id)));
    let prior = null, rank = 0;
    eligible.forEach((model, index) => {
      const run = result(model.id);
      if (run.score !== prior) rank = index + 1;
      prior = run.score;
      const row = $('#ranking-row').content.cloneNode(true);
      const button = row.querySelector('button');
      button.dataset.resultModel = model.id;
      button.dataset.resultBench = state.benchmark;
      if (model.id === state.baseline) button.setAttribute('data-baseline', '');
      row.querySelector('.leader-rank').textContent = String(rank).padStart(2, '0');
      row.querySelector('.leader-model strong').textContent = `${model.id}${mark(run)}`;
      row.querySelector('.leader-track > span').style.width = `${run.score}%`;
      row.querySelector('.leader-value').textContent = fmt(run.score);
      row.querySelector('.leader-delta').textContent = deltaText(delta(run));
      button.setAttribute('aria-label', `${model.id}: ${percentage(run.score)}. ${run.status_label}. Difference from baseline: ${deltaText(delta(run))}`);
      list.append(row);
    });
    $('#ranking-empty').hidden = eligible.length > 0;
    const base = result(state.baseline);
    $('#baseline-note').textContent = state.baseline
      ? `Baseline: ${state.baseline}. ${comparable(base) ? 'Deltas in percentage points (pp).' : 'No current-protocol baseline score; deltas unavailable.'} Older protocols are excluded from ranks. † uses earlier settings.`
      : 'Choose a baseline to see percentage-point differences. Older protocols are excluded from ranks; † retains earlier model settings.';
  }

  function renderMeasurements(visible) {
    const b = benches.get(state.benchmark), ids = new Set(visible.map(m => m.id));
    $$('[data-measurement]').forEach(panel => { panel.hidden = panel.dataset.measurement !== state.benchmark; });
    $('#measurement-title').textContent = b.name;
    $('#measurement-score-label').textContent = `Score: ${b.score_label.toLowerCase()} (%) · higher is better`;
    $('#measurement-version').textContent = `Protocol v${b.version}`;
    $('#benchmark-method').textContent = b.method;
    $('#measurements-empty').hidden = visible.length > 0;
    const panel = $$('[data-measurement]').find(p => p.dataset.measurement === state.benchmark);
    const rows = [...panel.querySelectorAll('tbody tr[data-model]')];
    for (const row of rows) {
      row.hidden = !ids.has(row.dataset.model);
      row.classList.toggle('selected-model', row.dataset.model === state.model);
      row.replaceChildren(...originalMeasurements.get(row).map(c => c.cloneNode(true)));
      if (state.scenario !== 'normal') {
        [...row.cells].slice(1).forEach((cell, index) => {
          cell.textContent = index === row.cells.length - 2 ? 'Not run' : '—';
          cell.dataset.value = '';
        });
        const modelCell = row.cells[0];
        modelCell.textContent = row.dataset.model;
        row.removeAttribute('data-result-link');
      } else if (result(row.dataset.model)) row.setAttribute('data-result-link', '');
    }
    for (const model of orderedModels(visible)) {
      const row = rows.find(r => r.dataset.model === model.id);
      row.parentNode.append(row);
    }
    $('#metric-count').textContent = `${b.kpis.length} metrics · ${visible.length} models`;
    $('#measurement-tables').hidden = state.layout === 'atlas';
    $('#metric-ledgers').hidden = state.layout !== 'atlas';
    $$('[data-ledger]').forEach(ledger => { ledger.hidden = ledger.dataset.ledger !== state.benchmark; });
    const ledger = $$('[data-ledger]').find(p => p.dataset.ledger === state.benchmark);
    ledger.querySelectorAll('[data-model-column]').forEach(cell => {
      cell.hidden = !ids.has(cell.dataset.modelColumn);
      cell.classList.toggle('baseline-column', cell.dataset.modelColumn === state.baseline);
      if (cell.tagName === 'TD' && !cell.parentNode.classList.contains('ledger-section')) {
        const original = ledgerCells.get(cell);
        cell.replaceChildren(...[...original.childNodes].map(n => n.cloneNode(true)));
        if (state.scenario !== 'normal') cell.textContent = '—';
      }
    });
    const sections = $('#metric-sections');
    sections.replaceChildren();
    for (const section of b.metric_sections) {
      const button = $('#metric-group-button').content.cloneNode(true);
      button.querySelector('button').textContent = `${section.name} · ${section.metrics.length}`;
      button.querySelector('button').dataset.scrollMetric = section.metrics[0][0];
      sections.append(button);
    }
  }

  function renderProfile(visible) {
    const visibleIds = new Set(visible.map(m => m.id));
    if (!visibleIds.has(state.model)) state.model = visible[0]?.id || '';
    $$('#model-choices .model-choice').forEach(button => {
      const id = button.dataset.focusModel;
      button.hidden = !visibleIds.has(id);
      button.setAttribute('aria-pressed', String(id === state.model));
      button.querySelector('[data-model-coverage]').textContent = `${data.benchmarks.filter(b => scored(result(id, b.id))).length} / ${data.benchmarks.length} scored`;
    });
    $('#model-nav-empty').hidden = visible.length > 0;
    const model = models.get(state.model);
    $('#profile-title').textContent = model?.id || 'No model selected';
    $('#profile-subtitle').textContent = model ? `${model.family} · ${model.parameters} · ${model.quant} · recorded configurations` : 'Reset the filters to explore the library.';
    const benchmark = benches.get(state.benchmark);
    $('#profile-coverage').textContent = `${benchmark.kpis.length} metrics`;
    $('#profile-baseline').textContent = `${benchmark.name} · Baseline: ${state.baseline || 'none selected'}`;
    const body = $('#profile-scores');
    body.replaceChildren();
    if (!model) return;
    const run = result(model.id), base = result(state.baseline);
    for (const [key, label, unit] of benchmark.kpis) {
      const row = $('#profile-row').content.cloneNode(true);
      row.querySelector('th').textContent = label;
      const selectedValue = run?.metrics[key], baselineValue = base?.metrics[key];
      row.querySelector('[data-selected]').textContent = metricFormat(selectedValue, unit);
      row.querySelector('[data-baseline]').textContent = metricFormat(baselineValue, unit);
      const compatible = comparable(run) && comparable(base) && Number.isFinite(selectedValue) && Number.isFinite(baselineValue);
      const difference = compatible ? selectedValue - baselineValue : null;
      row.querySelector('[data-difference]').textContent = difference === null ? '—'
        : `${difference > 0 ? '+' : ''}${metricFormat(difference, unit === '%' ? 'pp' : unit)}`;
      body.append(row);
    }
  }

  function render() {
    const visible = visibleModels();
    page.dataset.layout = state.layout;
    $('#design-description').textContent = descriptions[state.layout];
    $$('[data-design]').forEach(button => {
      const active = button.dataset.design === state.layout;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    $('#model-navigator').hidden = state.layout !== 'explorer';
    $('#profile-panel').hidden = state.layout !== 'explorer';
    renderProfile(visible);
    renderOverview();
    renderInsights(visible);
    renderHeatmap();
    renderRanking(visible);
    renderMeasurements(visible);
  }

  function openEvidence(model, benchmark) {
    state.model = model;
    const b = benches.get(benchmark), run = result(model, benchmark);
    $('#evidence-benchmark').textContent = `${b.name} · ${b.group}`;
    $('#evidence-model').textContent = model;
    $('#evidence-status').textContent = run?.status_label || 'Not run';
    $('#evidence-score').textContent = scored(run) ? percentage(run.score) : '—';
    $('#evidence-score-label').textContent = b.score_label;
    $('#evidence-delta').textContent = deltaText(delta(run, benchmark));
    $('#evidence-baseline').textContent = state.baseline || 'No baseline selected';
    $('#evidence-note').textContent = !run ? 'No saved run for this model and benchmark. It remains unmeasured.'
      : run.status === 'settings' ? 'Model settings changed. This score belongs to the recorded configuration and is retained with †.'
      : run.status === 'protocol' ? 'This run used an older benchmark protocol. Its saved score is retained, and excluded from the current ranking and baseline deltas.'
      : run.status === 'unsupported' ? 'This configuration did not support the required native tool workflow. Unsupported cases have no score.'
      : run.status === 'partial' ? 'Only part of the planned work has finished. Available measurements are retained; a final score is unavailable.'
      : 'Completed evidence for the current model settings and benchmark protocol.';
    const facts = $('#evidence-facts');
    facts.replaceChildren();
    const pairs = [['Run', run?.id || 'No saved run'], ['Node', run?.node || '—'],
      ['Recorded context', run ? `${run.context.toLocaleString('en-CA')} tokens` : '—'],
      ['Protocol', run ? `v${run.version} · latest v${b.version}` : `Latest v${b.version}`],
      ['Dataset', b.dataset], ['Formula', b.formula],
      ['Recorded', run ? new Intl.DateTimeFormat('en-CA', {timeZone: 'America/Vancouver', dateStyle: 'medium', timeStyle: 'short'}).format(new Date(run.ended * 1000)) + ' PST' : '—']];
    for (const [key, value] of pairs) {
      const term = document.createElement('dt'), definition = document.createElement('dd');
      term.textContent = key; definition.textContent = value; facts.append(term, definition);
    }
    const metrics = $('#evidence-metrics');
    metrics.replaceChildren();
    for (const [key, label, unit] of b.kpis) {
      const row = $('#metric-row').content.cloneNode(true);
      row.querySelector('th').textContent = label;
      row.querySelector('td').textContent = metricFormat(run?.metrics[key], unit);
      metrics.append(row);
    }
    $('#evidence-dialog').showModal();
  }

  function reset() {
    state.query = ''; state.evidence = 'all'; state.scenario = 'normal';
    $('#search-models').value = ''; $('#evidence-filter').value = 'all'; $('#scenario').value = 'normal';
    render(); announce('Search and evidence filters reset.');
  }

  document.addEventListener('click', event => {
    const section = event.target.closest('[data-scroll-metric]');
    if (section) {
      const isLedger = state.layout === 'atlas';
      const panel = document.querySelector(`[data-${isLedger ? 'ledger' : 'measurement'}="${state.benchmark}"]`);
      const target = panel.querySelector(`[data-metric="${section.dataset.scrollMetric}"]`);
      const scroll = panel.querySelector('.table-responsive');
      if (isLedger) scroll.scrollTo({top: target.offsetTop - 80, behavior: 'smooth'});
      else scroll.scrollTo({left: Math.max(0, target.offsetLeft - 280), behavior: 'smooth'});
      return;
    }
    const proposal = event.target.closest('[data-design]');
    if (proposal) { state.layout = proposal.dataset.design; render(); return; }
    const score = event.target.closest('[data-result-model]');
    if (score) { event.preventDefault(); openEvidence(score.dataset.resultModel, score.dataset.resultBench); return; }
    const column = event.target.closest('#benchmark-score-table thead th[data-bench]');
    const focus = event.target.closest('[data-focus-bench]');
    if (column || focus) {
      event.preventDefault(); state.benchmark = column?.dataset.bench || focus.dataset.focusBench;
      $('#benchmark').value = state.benchmark; render(); announce(`Ranked by ${benches.get(state.benchmark).name}.`); return;
    }
    const model = event.target.closest('[data-focus-model]');
    if (model) {
      state.model = model.dataset.focusModel;
      if (state.layout === 'explorer') render();
      else openEvidence(state.model, state.benchmark);
      return;
    }
    const measureRow = event.target.closest('[data-measurement] tr[data-result-link]');
    if (measureRow) { openEvidence(measureRow.dataset.model, state.benchmark); return; }
    const sortHeading = event.target.closest('[data-measurement] th[data-sort]');
    if (sortHeading) {
      const table = sortHeading.closest('table'), index = sortHeading.cellIndex;
      const descending = sortHeading.getAttribute('aria-sort') !== 'descending';
      const rows = [...table.tBodies[0].rows];
      rows.sort((a, b) => {
        const x = a.cells[index], y = b.cells[index];
        if (sortHeading.dataset.sort === 'text') return (descending ? -1 : 1) * x.textContent.localeCompare(y.textContent);
        const first = x.dataset.value, second = y.dataset.value;
        if (!first || !second) return Number(!first) - Number(!second);
        return (descending ? -1 : 1) * (Number(first) - Number(second));
      });
      table.querySelectorAll('thead th').forEach(th => th.removeAttribute('aria-sort'));
      sortHeading.setAttribute('aria-sort', descending ? 'descending' : 'ascending');
      rows.forEach(row => table.tBodies[0].append(row));
      announce(`Measurements sorted by ${sortHeading.textContent.trim()}, ${descending ? 'descending' : 'ascending'}.`);
    }
    if (event.target.closest('[data-reset]')) reset();
  });
  // Keep table header sorting reachable without a pointer.
  $$('[data-measurement] th[data-sort]').forEach(th => {
    th.tabIndex = 0;
    th.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); th.click(); }
    });
  });
  $('#search-models').addEventListener('input', event => { state.query = event.target.value.trim().toLowerCase(); render(); });
  $('#benchmark').addEventListener('change', event => { state.benchmark = event.target.value; render(); });
  $('#baseline').addEventListener('change', event => { state.baseline = event.target.value; render(); });
  $('#evidence-filter').addEventListener('change', event => { state.evidence = event.target.value; render(); });
  $('#scenario').addEventListener('change', event => { state.scenario = event.target.value; render(); });
  $('#reset').addEventListener('click', reset);
  $('#keyboard-grid').addEventListener('click', event => {
    const expanded = event.currentTarget.getAttribute('aria-expanded') !== 'true';
    event.currentTarget.setAttribute('aria-expanded', String(expanded));
    $('#benchmark-score-table').classList.toggle('show-score-table', expanded);
  });
  $('#close-evidence').addEventListener('click', () => { $('#evidence-dialog').close(); render(); });
  $('#evidence-dialog').addEventListener('cancel', () => { render(); });
  $('#theme').addEventListener('click', event => {
    const select = document.querySelector('[data-theme-form] [name="mode"]');
    const dark = document.documentElement.dataset.bsTheme !== 'dark';
    select.value = dark ? 'dark' : 'light'; select.dispatchEvent(new Event('change', {bubbles: true}));
    event.currentTarget.textContent = dark ? 'Light mode' : 'Dark mode';
  });
  document.addEventListener('submit', event => {
    if (event.target.matches('[data-preview-results-filters]')) event.preventDefault();
  });
  render();
})();
