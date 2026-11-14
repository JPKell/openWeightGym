/* Sample-only review: no fetches, storage, inference or live mutations. */
(() => {
  'use strict';
  const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
  const original = JSON.parse($('#reliability-sample').textContent);
  const descriptions = {
    briefing: 'A clear daily overview: spot issues, compare outcomes, then inspect the evidence. Recommended.',
    ledger: 'A comparison workspace: exact counts, recent feedback and routing reasons in one sortable ledger.',
    desk: 'A focused investigation: browse candidates on the left and review evidence and recovery on the right.'
  };
  let rows, quality, selected, layout = 'briefing', confirmation = '', returnFocus;
  const dialog = $('#evidence-dialog'), inspector = $('#inspector');
  function clone(id) { return document.getElementById(id).content.firstElementChild.cloneNode(true); }
  function put(node, selector, value) { node.querySelector(selector).textContent = value; }
  function announce(message) { $('#feedback').textContent = message; }
  function faults(row) { return row.counts.error + row.counts.timeout + row.counts.empty; }
  function total(row) { return Object.values(row.counts).reduce((a, b) => a + b, 0); }
  function rate(row) { return total(row) ? 100 * row.counts.success / total(row) : null; }
  function percent(value) { return value === null ? '—' : `${value.toFixed(1)}%`; }
  function priority(row) { return (row.state !== 'eligible' ? 100 : 0) + (row.regression ? 50 : 0) + faults(row); }
  function operator() { return $('#access').value === 'operator'; }
  function offline() { return $('#scenario').value === 'offline'; }
  function activeRows() {
    if ($('#scenario').value === 'empty') return [];
    return rows.map(row => {
      const r = structuredClone(row);
      if (!operator()) { r.counts = r.own_counts; r.ignored = r.own_ignored; r.positive = Math.floor(r.positive / 3); r.negative = Math.floor(r.negative / 3); r.last_request = r.own_reset ? null : r.last_request; }
      return r;
    });
  }
  function visibleRows() {
    const query = $('#search').value.trim().toLowerCase();
    return activeRows().filter(r => (!query || `${r.model} ${r.role || 'direct'} ${r.fingerprint}`.toLowerCase().includes(query)) &&
      (!$('#model').value || r.model === $('#model').value) && (!$('#role').value || (r.role || 'direct') === $('#role').value) &&
      ($('#fingerprint').value === 'all' || r.current === ($('#fingerprint').value === 'current')) && (!$('#issues').checked || faults(r) > 0))
      .sort((a, b) => $('#sort').value === 'model' ? a.model.localeCompare(b.model) : $('#sort').value === 'faults' ? faults(b) - faults(a) :
        $('#sort').value === 'rate' ? (rate(a) ?? 101) - (rate(b) ?? 101) : priority(b) - priority(a));
  }
  function badge(node, row) {
    const target = node.querySelector('[data-state]');
    target.textContent = offline() ? 'State unavailable' : row.state;
    target.className = `badge ${offline() ? 'bg-secondary-lt' : row.state === 'eligible' ? 'bg-success-lt' : row.state === 'cooldown' ? 'bg-warning-lt' : 'bg-danger-lt'}`;
  }
  function identity(node, row) {
    put(node, '[data-model]', row.model);
    put(node, '[data-identity]', `${row.fingerprint.slice(0, 12)} · ${row.current ? 'current' : 'historical'}`);
    node.querySelector('[data-identity]').title = row.fingerprint;
  }
  function bindOpen(button, row) {
    button.dataset.candidate = row.id;
    button.setAttribute('aria-label', `Inspect ${row.model}, ${row.role || 'direct request'}, ${row.current ? 'current' : 'historical'} fingerprint ${row.fingerprint.slice(0, 12)}`);
    button.addEventListener('click', () => choose(row.id, button));
  }
  function populate(node, row) {
    identity(node, row); badge(node, row);
    put(node, '[data-role]', row.role || 'Direct model request');
    put(node, '[data-rate]', percent(rate(row)));
    put(node, '[data-factor]', offline() ? '—' : `×${row.factor.toFixed(3)}`);
    node.querySelector('[data-regression]').hidden = !row.regression;
    bindOpen(node.matches('[data-open]') ? node : node.querySelector('[data-open]'), row);
    return node;
  }
  function choice(row) {
    const node = populate(clone('candidate-choice'), row);
    put(node, '[data-faults]', `${faults(row)} faults / ${total(row)} scored`);
    node.setAttribute('aria-pressed', String(selected === row.id));
    return node;
  }
  function renderOverview(visible) {
    const successes = visible.reduce((n, r) => n + r.counts.success, 0), scored = visible.reduce((n, r) => n + total(r), 0);
    $('#kpi-success').textContent = percent(scored ? 100 * successes / scored : null);
    $('#kpi-success-note').textContent = `${successes} of ${scored} scored outcomes`;
    $('#kpi-faults').textContent = visible.reduce((n, r) => n + faults(r), 0);
    $('#kpi-fault-note').textContent = ['error', 'timeout', 'empty'].map(k => `${visible.reduce((n, r) => n + r.counts[k], 0)} ${k === 'error' ? 'errors' : k === 'timeout' ? 'timeouts' : 'empty'}`).join(' · ');
    $('#kpi-excluded').textContent = offline() ? '—' : visible.filter(r => r.current && r.state !== 'eligible').length;
    $('#kpi-regressions').textContent = visible.filter(r => r.current && r.regression).length;
    $('#result-count').textContent = `${visible.length} candidates · ${visible.filter(r => r.current).length} current · ${visible.filter(r => !r.current).length} historical · ${scored} scored outcomes`;
    $('#scope-label').textContent = operator() ? 'all users' : 'your requests';
    $('#offline-note').hidden = !offline();
  }
  function renderAttention(visible) {
    const attention = visible.filter(r => r.current && (r.state !== 'eligible' || r.regression));
    $('#attention-list').replaceChildren(...attention.map(choice));
    $('#attention-count').textContent = offline() ? 'Saved evidence' : `${attention.length} candidates`;
    if (!attention.length) {
      const p = document.createElement('p'); p.className = 'p-3 mb-0 text-secondary small';
      p.textContent = visible.length ? 'No exclusions or regression warnings in this view. Candidates may still have isolated faults.' : 'No evidence in this view. Clear filters or select a populated preview state.';
      $('#attention-list').append(p);
    }
  }
  function renderTable(visible) {
    $('#candidate-rows').replaceChildren(); $('#desk-list').replaceChildren();
    for (const row of visible) {
      const node = populate(clone('candidate-row'), row);
      for (const key of ['success', 'error', 'timeout', 'empty']) put(node, `[data-count="${key}"]`, row.counts[key]);
      put(node, '[data-feedback]', `${row.positive} / ${row.negative}`);
      put(node, '[data-reason]', row.reason);
      bindOpen(node.querySelector('[data-inspect]'), row);
      $('#candidate-rows').append(node); $('#desk-list').append(choice(row));
    }
    if (!visible.length) {
      $('#candidate-rows').append(clone('empty-table'));
      const p = document.createElement('p'); p.className = 'p-3 text-secondary'; p.textContent = 'No candidates match. Clear filters or try another preview state.'; $('#desk-list').append(p);
    }
  }
  function renderChart(visible) {
    $('#outcome-list').replaceChildren();
    for (const row of visible) {
      const node = clone('outcome-row');
      put(node, '[data-model]', row.model); put(node, '[data-chart-role]', `${row.role || 'Direct'} · ${row.current ? 'current' : 'historical'} · n=${total(row)}`);
      put(node, '[data-rate]', percent(rate(row)));
      const label = ['success', 'error', 'timeout', 'empty'].map(k => `${row.counts[k]} ${k}`).join(', ');
      node.querySelector('[role="img"]').setAttribute('aria-label', label); node.querySelector('[role="img"]').title = label;
      for (const key of ['success', 'error', 'timeout', 'empty']) node.querySelector(`[data-segment="${key}"]`).style.width = `${total(row) ? row.counts[key] / total(row) * 100 : 0}%`;
      bindOpen(node.querySelector('[data-open]'), row); $('#outcome-list').append(node);
    }
    if (!visible.length) $('#outcome-list').textContent = 'No scored outcomes to compare.';
  }
  function record(request) {
    $('#event-heading').textContent = `Sample request #${request}`;
    $('#event-text').textContent = `In the live console this opens /requests/${request}. This preview contains aggregate fixtures only; it does not invent request content or contact the service.`;
    $('#event-dialog').showModal();
  }
  function renderQuality() {
    const filtered = $('#scenario').value === 'empty' ? [] : quality.filter(r => (!$('#model').value || r.model === $('#model').value) &&
      ($('#fingerprint').value === 'all' || r.current === ($('#fingerprint').value === 'current')));
    $('#quality-rows').replaceChildren();
    for (const row of filtered) {
      const node = clone('quality-row'); identity(node, row);
      const good = operator() ? row.good : Math.floor(row.good / 3), bad = operator() ? row.bad : Math.floor(row.bad / 3);
      put(node, '[data-good]', good); put(node, '[data-bad]', bad); put(node, '[data-score]', percent(good + bad ? 100 * good / (good + bad) : null));
      if (!operator()) put(node, '[data-explanations]', "Reading explanations needs the ability to read everyone's log.");
      else for (const explanation of row.explanations) {
        const item = clone('explanation'); put(item, '[data-request]', `#${explanation.request}`);
        put(item, '[data-rating]', `${explanation.value > 0 ? 'good' : 'bad'} · ${explanation.rater}: ${explanation.explanation}`);
        item.querySelector('[data-request]').addEventListener('click', () => record(explanation.request)); node.querySelector('[data-explanations]').append(item);
      }
      $('#quality-rows').append(node);
    }
    if (!filtered.length) $('#quality-rows').append(clone('empty-quality'));
  }
  function renderDetail() {
    confirmation = ''; $('#reset-candidate').textContent = 'Reset reliability'; $('#recover').textContent = 'Run recovery';
    const row = visibleRows().find(r => r.id === selected);
    $('#detail-empty').hidden = !!row; $('#detail-content').hidden = !row;
    if (!row) return;
    identity(inspector, row); badge(inspector, row);
    $('#detail-role').textContent = row.role || 'Direct model request';
    $('#detail-reason').textContent = row.reason;
    $('#detail-rate').textContent = percent(rate(row)); $('#detail-factor').textContent = offline() ? 'Unavailable' : `×${row.factor.toFixed(3)}`;
    for (const [id, key] of [['success', 'success'], ['error', 'error'], ['timeout', 'timeout'], ['empty-count', 'empty']]) $(`#detail-${id}`).textContent = row.counts[key];
    $('#detail-feedback').textContent = `${row.positive} positive / ${row.negative} negative`;
    $('#detail-ignored').textContent = Object.entries(row.ignored).map(([k, v]) => `${k}: ${v}`).join(' · ') || 'None';
    $('#detail-request').textContent = row.last_request ? `#${row.last_request} ↗` : 'No recent request'; $('#detail-request').disabled = !row.last_request;
    $('#detail-request').onclick = () => row.last_request && record(row.last_request);
    $('#detail-at').textContent = row.last_at; $('#detail-fingerprint').textContent = row.fingerprint; $('#detail-regression').hidden = !row.regression;
    const canManage = operator() && row.may_manage && !offline();
    $('#manage-actions').hidden = !canManage;
    $('#recover').hidden = row.state !== 'recovery required';
    $('#manage-note').textContent = offline() ? 'Recovery controls are unavailable while compute is not answering.' : !operator() ? 'Model management permission is required for recovery and reset.' : !row.current ? 'Historical fingerprint. Recovery controls are available only for the current candidate.' : row.state === 'cooldown' ? 'Five-minute cooldown in progress. Recovery becomes available when the cooldown ends.' : row.state === 'recovery required' ? 'This candidate is excluded from this role. It is ready for a controlled recovery check.' : 'This candidate is eligible. Reset only when you want to start a fresh scoring window.';
  }
  function render() {
    const visible = visibleRows();
    if (!visible.some(r => r.id === selected)) selected = visible[0]?.id;
    renderOverview(visible); renderAttention(visible); renderTable(visible); renderChart(visible); renderQuality(); renderDetail();
  }
  function choose(id, trigger) {
    selected = id; returnFocus = trigger; renderDetail();
    $$('#desk-list [data-open], #attention-list [data-open]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.candidate === id)));
    if (layout !== 'desk') { $('#dialog-slot').append(inspector); if (!dialog.open) dialog.showModal(); }
    else if (innerWidth <= 800) $('#inspector-slot').scrollIntoView({behavior: 'smooth', block: 'start'});
  }
  function closeDetail() { dialog.close(); $('#inspector-slot').append(inspector); if (returnFocus?.isConnected) returnFocus.focus(); }
  function switchLayout(value) {
    if (dialog.open) closeDetail();
    layout = value; $('#workspace').dataset.layout = value;
    if (location.hash !== `#${value}`) history.replaceState(null, '', `#${value}`);
    $$('#table-wrap, #desk-list').forEach(n => n.hidden = n.id === 'table-wrap' ? value === 'desk' : value !== 'desk');
    $$('[data-design]').forEach(b => { const active = b.dataset.design === value; b.classList.toggle('active', active); b.setAttribute('aria-pressed', String(active)); });
    $('#design-description').textContent = descriptions[value]; render();
  }
  function resetData() {
    rows = structuredClone(original.rows); quality = structuredClone(original.quality); selected = 'candidate-1';
    if ($('#scenario').value === 'healthy') for (const row of rows) {
      row.counts = {success: total(row), error: 0, timeout: 0, empty: 0};
      row.own_counts = {...row.counts, success: Math.floor(row.counts.success / 3)};
      row.state = 'eligible'; row.regression = false; row.factor = 1;
      row.positive = 0; row.negative = 0;
      row.reason = 'No recent model faults; eligible for this role.';
    }
  }
  function act(kind) {
    const row = visibleRows().find(r => r.id === selected);
    if (!row || !operator() || !row.may_manage || offline() || (kind === 'recover' && row.state !== 'recovery required')) return;
    if (confirmation !== kind) {
      confirmation = kind;
      $('#recover').textContent = 'Run recovery'; $('#reset-candidate').textContent = 'Reset reliability';
      $(kind === 'recover' ? '#recover' : '#reset-candidate').textContent = kind === 'recover' ? 'Confirm sample recovery' : 'Confirm sample reset';
      announce(kind === 'recover' ? 'Confirm to simulate a successful recovery. A live check may load the model or incur hosted cost.' : 'Confirm to start a fresh sample scoring window. Lifetime ratings and retained history are kept.'); return;
    }
    const target = rows.find(r => r.id === row.id);
    target.state = 'eligible'; target.regression = false;
    if (kind === 'reset') { target.counts = {success: 0, error: 0, timeout: 0, empty: 0}; target.own_counts = {...target.counts}; target.factor = 1; target.positive = 0; target.negative = 0; target.ignored = {}; target.own_ignored = {}; target.last_request = null; target.own_reset = true; target.reason = 'Fresh scoring window. Retained history and lifetime quality ratings are unchanged.'; }
    else target.reason = `${target.reason.split(';')[0]}; sample recovery succeeded and restored eligibility. Recovery checks are not ordinary scored outcomes.`;
    render(); announce(kind === 'recover' ? 'Sample recovery succeeded. Candidate is eligible; ordinary outcome counts are unchanged.' : 'Sample reliability reset. Scoring window is empty; lifetime answer quality is retained.');
  }
  $('#filters').addEventListener('submit', e => e.preventDefault());
  $$('#filters input, #filters select, #sort, #access').forEach(n => n.addEventListener(n.type === 'search' ? 'input' : 'change', render));
  $('#scenario').addEventListener('change', () => { resetData(); render(); announce('Preview state changed. All data and actions are illustrative.'); });
  $$('[data-design]').forEach(b => b.addEventListener('click', () => switchLayout(b.dataset.design)));
  function clear() { $('#filters').reset(); render(); }
  $('#clear').addEventListener('click', clear);
  $('#reset-demo').addEventListener('click', () => { $('#scenario').value = 'normal'; resetData(); $('#access').value = 'operator'; $('#sort').value = 'attention'; clear(); announce('Demo reset. Actions are simulated in this tab.'); });
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark';
    const mode = $('[data-theme-form] [name="mode"]'); mode.value = dark ? 'dark' : 'light'; mode.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = dark ? 'Light mode' : 'Dark mode';
  });
  $('#recover').addEventListener('click', () => act('recover')); $('#reset-candidate').addEventListener('click', () => act('reset'));
  $('[data-close]').addEventListener('click', closeDetail);
  dialog.addEventListener('cancel', e => { e.preventDefault(); closeDetail(); });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') { confirmation = ''; $('#reset-candidate').textContent = 'Reset reliability'; $('#recover').textContent = 'Run recovery'; } });
  $('#audit').addEventListener('click', () => { $('#event-heading').textContent = 'Reliability events'; $('#event-text').textContent = 'In the live console this opens AI Logs at /log?kind=reliability. This sample contains no live audit records.'; $('#event-dialog').showModal(); });
  $('#close-event').addEventListener('click', () => $('#event-dialog').close());
  $('#export').addEventListener('click', () => {
    const columns = ['model', 'fingerprint', 'current', 'role', 'success', 'error', 'timeout', 'empty', 'success_rate', 'positive', 'negative', 'factor', 'state', 'regression', 'reason', 'ignored', 'last_request', 'last_at'];
    const csv = [columns, ...visibleRows().map(r => columns.map(k => k in r.counts ? r.counts[k] : k === 'success_rate' ? rate(r) ?? '' : k === 'ignored' ? JSON.stringify(r.ignored) : offline() && ['factor', 'state'].includes(k) ? 'unavailable' : r[k] ?? ''))]
      .map(line => line.map(value => `"${String(value).replace(/"/g, '""')}"`).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'})), link = document.createElement('a'); link.href = url; link.download = 'reliability-sample.csv'; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); announce('Exported the filtered sample reliability evidence as CSV.');
  });
  for (const [selector, values] of [['#model', [...new Set(original.rows.map(r => r.model))].sort()], ['#role', [...new Set(original.rows.map(r => r.role).filter(Boolean))].sort()]]) {
    for (const value of values) { const option = document.createElement('option'); option.value = value; option.textContent = value; $(selector).append(option); }
  }
  resetData();
  window.addEventListener('hashchange', () => {
    const next = location.hash.slice(1);
    if (Object.hasOwn(descriptions, next)) switchLayout(next);
  });
  const initial = location.hash.slice(1);
  switchLayout(Object.hasOwn(descriptions, initial) ? initial : 'briefing');
})();
