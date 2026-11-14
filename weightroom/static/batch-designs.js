/* Offline design review. All actions use sample data held in this tab. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const source = JSON.parse($('#batch-data').textContent);
  const labels = {in_progress: 'Running', completed: 'Completed', cancelled: 'Cancelled', cancelling: 'Cancelling', paused: 'Paused'};
  const descriptions = {
    ledger: 'Compare status, progress and results in one readable ledger. Recommended for everyday use.',
    board: 'Follow the flow of work. Active, completed and cancelled batches have their own lanes.',
    inspector: 'Keep the directory in sight while inspecting one batch’s progress, submission details and files.'
  };
  const date = new Intl.DateTimeFormat('en-CA', {timeZone: 'America/Vancouver', timeZoneName: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'});
  const number = value => value.toLocaleString('en-CA');
  const clone = id => document.getElementById(id).content.firstElementChild.cloneNode(true);
  const put = (node, selector, value) => { for (const el of node.querySelectorAll(selector)) el.textContent = value; };
  const name = batch => batch.metadata?.name || batch.id;
  const processed = batch => batch.request_counts.completed + batch.request_counts.failed;
  const percent = batch => batch.request_counts.total ? processed(batch) / batch.request_counts.total * 100 : 0;
  const active = batch => ['in_progress', 'paused', 'cancelling'].includes(batch.status);
  const canCancel = batch => batch.status === 'in_progress' && (batch.user === source.viewer || source.can_stop);
  let batches = [], selected = null, layout = 'ledger', currentPage = 1;
  const dialog = $('#detail-dialog');
  function announce(message) { $('#feedback').textContent = message; }
  function resetConfirmations() { document.dispatchEvent(new CustomEvent('suite:actionsChanged', {detail: {form: null}})); }
  function filtered() {
    const search = $('#search').value.trim().toLowerCase();
    const status = $('#status-filter').value, owner = $('#owner-filter').value;
    const rows = batches.filter(batch =>
      (!search || `${name(batch)} ${batch.id} ${batch.endpoint} ${batch.user}`.toLowerCase().includes(search)) &&
      (status === 'all' || status === 'errors' && batch.request_counts.failed > 0 || status === batch.status) &&
      (owner === 'all' || owner === batch.user));
    return rows.sort((a, b) => {
      const sort = $('#sort').value;
      return (sort === 'oldest' ? a.created_at - b.created_at :
        sort === 'errors' ? b.request_counts.failed - a.request_counts.failed :
        sort === 'progress' ? percent(b) - percent(a) : b.created_at - a.created_at) || b.created_at - a.created_at;
    });
  }
  function saveFile(text, filename, type) {
    const blob = new Blob([text], {type}), url = URL.createObjectURL(blob);
    const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function download(batch, kind) {
    if (batch.user !== source.viewer || !batch[kind === 'answers' ? 'output_file_id' : 'error_file_id']) return;
    const count = batch.request_counts[kind === 'answers' ? 'completed' : 'failed'];
    const lines = Array.from({length: count}, (_, index) => JSON.stringify({
      custom_id: `sample-${kind}-${index + 1}`, sample: true,
      ...(kind === 'answers' ? {response: {status_code: 200, body: {message: 'Illustrative result for this design preview.'}}} :
        {error: {code: 'sample_error', message: 'Illustrative request error for this design preview.'}})
    })).join('\n') + '\n';
    saveFile(lines, `${batch.id}-sample-${kind}.jsonl`, 'application/jsonl');
    announce(`Downloaded ${number(count)} illustrative ${kind} for ${name(batch)}. Sample data only.`);
  }
  function populate(node, batch) {
    const counts = batch.request_counts;
    node.dataset.batch = batch.id; node.dataset.selected = String(selected === batch.id);
    put(node, '[data-name]', name(batch)); put(node, '[data-id]', batch.id);
    put(node, '[data-owner]', batch.user === source.viewer ? 'You' : batch.user);
    put(node, '[data-created]', date.format(new Date(batch.created_at * 1000)));
    for (const time of node.querySelectorAll('time[data-created]')) time.dateTime = new Date(batch.created_at * 1000).toISOString();
    put(node, '[data-endpoint]', batch.endpoint);
    for (const state of node.querySelectorAll('[data-state]')) { state.textContent = labels[batch.status] || batch.status; state.dataset.state = batch.status; }
    put(node, '[data-progress-label]', `${number(processed(batch))} / ${number(counts.total)} processed`);
    put(node, '[data-percent]', `${Math.floor(percent(batch))}%`);
    put(node, '[data-success-label]', `${number(counts.completed)} successful`);
    put(node, '[data-error-label]', counts.failed ? `${number(counts.failed)} errors` : 'No errors');
    for (const el of node.querySelectorAll('[data-error-label]')) { el.classList.toggle('text-danger', counts.failed > 0); el.classList.toggle('text-secondary', !counts.failed); }
    for (const el of node.querySelectorAll('[data-progress]')) {
      el.setAttribute('aria-valuenow', String(Math.floor(percent(batch))));
      el.setAttribute('aria-valuetext', `${processed(batch)} of ${counts.total} processed: ${counts.completed} successful, ${counts.failed} errors`);
    }
    for (const [selector, count] of [['[data-completed-bar]', counts.completed], ['[data-failed-bar]', counts.failed]]) {
      for (const bar of node.querySelectorAll(selector)) bar.style.width = `${counts.total ? count / counts.total * 100 : 0}%`;
    }
    for (const button of node.querySelectorAll('[data-open]')) {
      button.dataset.batch = batch.id;
      button.setAttribute('aria-label', `Inspect ${name(batch)}`);
      button.setAttribute('aria-pressed', String(selected === batch.id));
      button.onclick = () => choose(batch.id);
    }
    const mine = batch.user === source.viewer, available = mine && (batch.output_file_id || batch.error_file_id);
    for (const button of node.querySelectorAll('[data-download]')) {
      const kind = button.dataset.download;
      button.hidden = !(mine && batch[kind === 'answers' ? 'output_file_id' : 'error_file_id']);
      button.setAttribute('aria-label', `Download sample ${kind} for ${name(batch)}`);
      button.onclick = () => download(batch, kind);
    }
    for (const form of node.querySelectorAll('[data-cancel-form]')) {
      form.hidden = !canCancel(batch); form.elements.batch.value = batch.id;
      form.querySelector('button').setAttribute('aria-label', `Cancel ${name(batch)}`);
    }
    put(node, '[data-file-note]', !mine ? 'Files visible to owner' : available ? '' : active(batch) ? 'Files available when finished' : 'No result files');
  }
  function updateInspector() {
    const batch = batches.find(item => item.id === selected);
    $('#inspector-content').hidden = !batch; $('#inspector-empty').hidden = !!batch;
    if (!batch) return;
    populate($('#inspector-content'), batch);
    $('#detail-name').textContent = name(batch);
    put($('#inspector-content'), '[data-input-file]', batch.input_file_id);
    const end = batch.completed_at || batch.cancelled_at;
    put($('#inspector-content'), '[data-finished]', end ? date.format(new Date(end * 1000)) : '—');
    put($('#inspector-content'), '[data-created]', date.format(new Date(batch.created_at * 1000)));
    put($('#inspector-content'), '[data-remaining]', number(batch.request_counts.total - processed(batch)));
    $('#detail-outcome').textContent = batch.status === 'completed' ? 'Processing finished. Errors are included in the processed count.' :
      batch.status === 'cancelled' ? 'Cancelled. Successful answers are kept; unprocessed requests will not run.' :
      batch.status === 'cancelling' ? 'Cancellation is in progress. Answers already completed will be kept.' :
      batch.status === 'paused' ? 'Paused. Existing progress is retained.' : 'Running in the background. Live requests take priority.';
    $('#batch-record').textContent = JSON.stringify(batch, null, 2);
  }
  function render() {
    resetConfirmations();
    const visible = filtered(), size = Number($('#page-size').value);
    const pages = Math.max(1, Math.ceil(visible.length / size));
    currentPage = Math.min(currentPage, pages);
    if (!visible.some(batch => batch.id === selected)) selected = visible[0]?.id || null;
    const onPage = layout === 'board' ? visible : visible.slice((currentPage - 1) * size, currentPage * size);
    $('#active-count').textContent = batches.filter(active).length;
    $('#processed-count').textContent = number(batches.reduce((sum, batch) => sum + processed(batch), 0));
    $('#processed-note').textContent = `of ${number(batches.reduce((sum, batch) => sum + batch.request_counts.total, 0))} submitted requests`;
    $('#error-count').textContent = batches.filter(batch => batch.request_counts.failed > 0).length;
    $('#ready-count').textContent = batches.filter(batch => batch.user === source.viewer && batch.output_file_id).length;
    $('#visible-count').textContent = `${visible.length} ${visible.length === 1 ? 'batch' : 'batches'}`;
    $('#scope-note').textContent = `${$('#owner-filter').value === 'all' ? 'All owners' : $('#owner-filter').selectedOptions[0].textContent} · times in America/Vancouver`;
    $('#ledger').hidden = layout !== 'ledger' || !visible.length;
    $('#board').hidden = layout !== 'board' || !visible.length;
    $('#directory').hidden = layout !== 'inspector' || !visible.length;
    $('#inspector-slot').hidden = layout !== 'inspector' || !visible.length;
    $('#empty').hidden = !!visible.length;
    $('#empty-title').textContent = batches.length ? 'No matching batches' : 'No batches yet';
    $('#empty-message').textContent = batches.length ? 'Try another name, status or owner, or clear your filters.' : 'Submit a JSONL file through the API. Your batches and results will appear here.';
    $('#empty [data-guide]').hidden = !!batches.length; $('#empty-clear').hidden = !batches.length;
    $('#stop-all-form button').disabled = !batches.some(canCancel);
    $('#advance').disabled = !batches.some(batch => ['in_progress', 'cancelling'].includes(batch.status));
    $('#export').disabled = !visible.length;
    $('#ledger-rows').replaceChildren(); $('#batch-list').replaceChildren();
    for (const lane of $$('[data-lane]')) lane.replaceChildren();
    const totals = {active: 0, completed: 0, cancelled: 0};
    for (const batch of onPage) {
      if (layout === 'ledger') { const row = clone('batch-table-template'); populate(row, batch); $('#ledger-rows').append(row); }
      else {
        const entry = clone('batch-card-template'); populate(entry, batch);
        if (layout === 'inspector') $('#batch-list').append(entry);
        else {
          const lane = active(batch) ? 'active' : batch.status === 'completed' ? 'completed' : 'cancelled';
          $(`[data-lane="${lane}"]`).append(entry); totals[lane]++;
        }
      }
    }
    for (const key of Object.keys(totals)) { $(`[data-lane-count="${key}"]`).textContent = totals[key]; $(`[data-lane-empty="${key}"]`).hidden = !!totals[key]; }
    $('#pagination').hidden = layout === 'board' || !visible.length;
    $('#page-summary').textContent = `${(currentPage - 1) * size + 1}–${Math.min(currentPage * size, visible.length)} of ${visible.length}`;
    $('#previous').disabled = currentPage <= 1; $('#next').disabled = currentPage >= pages;
    updateInspector();
  }
  function choose(id) {
    selected = id;
    for (const entry of $$('.bt-entry, #ledger-rows tr')) {
      const chosen = entry.dataset.batch === id; entry.dataset.selected = String(chosen);
      entry.querySelector('[data-open]')?.setAttribute('aria-pressed', String(chosen));
    }
    resetConfirmations(); updateInspector();
    if (layout !== 'inspector') { if (!dialog.open) dialog.showModal(); }
    else if (innerWidth <= 850) $('#inspector-slot').scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start'});
  }
  function setLayout(value) {
    if (dialog.open) dialog.close();
    layout = value; $('#workspace').dataset.layout = value;
    for (const button of $$('[data-design]')) { const chosen = button.dataset.design === value; button.classList.toggle('active', chosen); button.setAttribute('aria-pressed', String(chosen)); }
    $('#design-description').textContent = descriptions[value];
    (layout === 'inspector' ? $('#inspector-slot') : $('#dialog-slot')).append($('#batch-inspector'));
    $('#close-inspector').hidden = layout === 'inspector';
    render(); history.replaceState(null, '', `#${value}`);
  }
  function clearFilters() { $('#search').value = ''; $('#status-filter').value = 'all'; $('#owner-filter').value = 'all'; $('#sort').value = 'newest'; currentPage = 1; render(); }
  function reset() {
    if (dialog.open) dialog.close();
    batches = $('#scenario').value === 'empty' ? [] : structuredClone(source.batches);
    if ($('#scenario').value === 'attention') {
      const batch = batches[0]; batch.status = 'completed';
      batch.request_counts = {total: 1200, completed: 960, failed: 240};
      batch.output_file_id = 'file_answers_attention'; batch.error_file_id = 'file_errors_attention';
      batch.completed_at = batch.created_at + 1200;
    }
    selected = batches[0]?.id || null; clearFilters();
    announce('Actions change this preview only. Downloads contain illustrative sample records.');
  }
  document.addEventListener('submit', event => {
    if (event.target.id === 'filters') { event.preventDefault(); return; }
    if (!event.target.matches('[data-cancel-form], #stop-all-form')) return;
    event.preventDefault();
    const all = event.target.id === 'stop-all-form';
    const changed = batches.filter(batch => canCancel(batch) && (all || batch.id === event.target.elements.batch.value));
    for (const batch of changed) batch.status = 'cancelling';
    render(); announce(`${changed.length} ${changed.length === 1 ? 'batch is' : 'batches are'} cancelling in this demo. Completed answers are kept. Use Advance demo progress to finish cancellation.`);
  });
  $('#advance').addEventListener('click', () => {
    for (const batch of batches) {
      if (batch.status === 'cancelling') batch.status = 'cancelled';
      else if (batch.status === 'in_progress') {
        const counts = batch.request_counts;
        counts.completed += Math.min(counts.total - processed(batch), Math.max(1, Math.ceil(counts.total * .15)));
        if (processed(batch) === counts.total) batch.status = 'completed';
      } else continue;
      if (['completed', 'cancelled'].includes(batch.status)) {
        batch[batch.status === 'completed' ? 'completed_at' : 'cancelled_at'] = Math.floor(Date.now() / 1000);
        batch.output_file_id = batch.request_counts.completed ? `file_answers_${batch.id}` : null;
        batch.error_file_id = batch.request_counts.failed ? `file_errors_${batch.id}` : null;
      }
    }
    render(); announce('Sample progress advanced. Search, filters and the selected batch are retained.');
  });
  $('#export').addEventListener('click', () => {
    const fields = ['id', 'name', 'user', 'status', 'endpoint', 'total', 'completed', 'failed', 'created_at'];
    const quote = value => `"${String(value).replaceAll('"', '""')}"`;
    const rows = filtered().map(batch => [batch.id, name(batch), batch.user, batch.status, batch.endpoint,
      batch.request_counts.total, batch.request_counts.completed, batch.request_counts.failed, new Date(batch.created_at * 1000).toISOString()]);
    saveFile([fields, ...rows].map(row => row.map(quote).join(',')).join('\r\n'), 'sample-batches.csv', 'text/csv');
    announce(`Exported ${rows.length} matching sample batches, across all pages.`);
  });
  $('#copy-id').addEventListener('click', async () => {
    if (!selected) return;
    try { await navigator.clipboard.writeText(selected); announce(`Copied ${selected}.`); }
    catch { const selection = getSelection(), range = document.createRange(); range.selectNodeContents($('#inspector-content [data-id]')); selection.removeAllRanges(); selection.addRange(range); announce('Batch ID selected. Use your browser’s Copy command.'); }
  });
  for (const id of ['search', 'status-filter', 'owner-filter', 'sort', 'page-size']) $('#' + id).addEventListener(id === 'search' ? 'input' : 'change', () => { currentPage = 1; render(); });
  for (const button of $$('[data-design]')) button.addEventListener('click', () => setLayout(button.dataset.design));
  for (const button of $$('[data-guide]')) button.addEventListener('click', () => $('#guide-dialog').showModal());
  $('#close-guide').addEventListener('click', () => $('#guide-dialog').close());
  $('#close-inspector').addEventListener('click', () => dialog.close());
  $('#previous').addEventListener('click', () => { currentPage--; render(); });
  $('#next').addEventListener('click', () => { currentPage++; render(); });
  $('#clear').addEventListener('click', clearFilters); $('#empty-clear').addEventListener('click', clearFilters);
  $('#scenario').addEventListener('change', reset); $('#reset').addEventListener('click', reset);
  $('#theme').addEventListener('click', () => {
    const mode = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    const select = $('[data-theme-form] [name="mode"]'); select.value = mode; select.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = mode === 'dark' ? 'Light mode' : 'Dark mode';
  });
  $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  window.addEventListener('hashchange', () => { const value = location.hash.slice(1); if (Object.hasOwn(descriptions, value) && value !== layout) setLayout(value); });
  reset(); setLayout(Object.hasOwn(descriptions, location.hash.slice(1)) ? location.hash.slice(1) : 'ledger');
})();
