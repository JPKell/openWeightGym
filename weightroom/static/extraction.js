/* Real extraction desk. Drafts stay in page memory; sources/jobs are owner-private API records. */
(() => {
  'use strict';
  let cleanup;
  function setup() {
    const root = document.querySelector('[data-extraction-root]');
    if (!root) { if (cleanup) cleanup(); cleanup = null; return; }
    if (root.dataset.initialized) return;
    if (cleanup) cleanup();
    root.dataset.initialized = 'true';
    const $ = selector => root.querySelector(selector);
    const $$ = selector => [...root.querySelectorAll(selector)];
    const template = id => document.querySelector(id).content.cloneNode(true);
    const apiRoot = '/chat/extraction/api/';
    const records = new Map(), jobs = new Map(), inboxNodes = new Map();
    const fields = ['task', 'format', 'model', 'instructions', 'detail', 'schema', 'keep-breaks', 'mark-unclear', 'temporary'];
    let active = '', models = [], presets = [], view = 'preview', timer, alive = true, cursor = null, uploadBusy = false, noticeError = '', modelsLoading = true;
    const current = () => records.get(active);
    const terminal = job => job && ['completed', 'needs_review', 'failed', 'cancelled'].includes(job.status);
    const successful = job => job && ['completed', 'needs_review'].includes(job.status);
    const canonical = value => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
    const busy = item => Boolean(item?.submitting || (item?.job && !terminal(item.job)));
    const say = text => { $('#action-status').textContent = text; };
    const failure = error => { noticeError = error.message || String(error); $('#error-message').textContent = noticeError; $('#error-notice').hidden = false; };
    async function api(path, options = {}) {
      const response = await fetch(apiRoot + path, {credentials: 'same-origin', ...options});
      if (response.redirected && new URL(response.url).pathname === '/sign-in') throw new Error('Your session ended. Sign in again to continue.');
      if (!response.ok) {
        let message;
        try { message = (await response.json()).error?.message; } catch { /* Authentication/network errors can return HTML. */ }
        const error = new Error(message || `Request failed (HTTP ${response.status}).`); error.status = response.status; throw error;
      }
      return response.status === 204 ? null : response.json();
    }
    const send = (path, method, body, headers = {}) => api(path, {method, headers: {'content-type': 'application/json', ...headers}, body: JSON.stringify(body)});
    function settings() {
      const value = Object.fromEntries(fields.map(id => [id, $('#' + id).type === 'checkbox' ? $('#' + id).checked : $('#' + id).value]));
      return value;
    }
    let defaults = settings();
    function saveDraft() { if (current()) current().settings = settings(); else defaults = settings(); }
    function loadDraft() {
      const value = current()?.settings || defaults;
      $('#raw-editor').setCustomValidity('');
      for (const id of fields) {
        const field = $('#' + id);
        if (field.type === 'checkbox') field.checked = Boolean(value[id]);
        else if (id === 'model' && value[id] && !models.some(model => model.id === value[id])) {
          let option = field.querySelector('option[data-unavailable]');
          if (!option) { option = new Option(); option.dataset.unavailable = ''; field.add(option); }
          option.value = value[id]; option.textContent = value[id] + ' · unavailable';
          field.value = value[id];
        } else field.value = value[id] ?? '';
      }
      controls();
    }
    function specFor(item) {
      const value = item.settings;
      const spec = {asset_id: item.asset.id, task: value.task, model: value.model, output_format: value.format,
        detail: value.detail, instructions: value.instructions, keep_breaks: value['keep-breaks'],
        mark_unclear: value['mark-unclear'], temporary: value.temporary};
      if (value.task === 'custom') {
        try { spec.schema = JSON.parse(value.schema); }
        catch { throw new Error('Enter a valid JSON field schema.'); }
      }
      return spec;
    }
    function draftFrom(spec) {
      return {...defaults, task: spec.task, format: spec.output_format, model: spec.model, detail: spec.detail,
        instructions: spec.instructions, schema: JSON.stringify(spec.schema || JSON.parse(defaults.schema), null, 2),
        'keep-breaks': spec.keep_breaks, 'mark-unclear': spec.mark_unclear, temporary: spec.temporary};
    }
    function controls() {
      const task = $('#task').value;
      $('#schema-fields').hidden = task !== 'custom';
      $('#detail').disabled = task !== 'describe';
      $('#format option[value="csv"]').disabled = !['receipt', 'table'].includes(task);
      if ($('#format').value === 'csv' && !['receipt', 'table'].includes(task)) $('#format').value = 'json';
      $('#format-hint').textContent = {plain:'A Unicode string in reading order.',layout:'Approximate columns and spacing in monospace.',markdown:'Semantic headings and Markdown tables.',json:'Schema-validated fields; unknown values stay null.',csv:'Spreadsheet rows with safe quoting and formula protection.'}[$('#format').value];
      const selected = models.find(model => model.id === $('#model').value);
      $('#model-hint').textContent = selected?.role ? 'This role can use its configured local or cloud candidates.' : selected?.hosted ? 'Sent to cloud account: ' + selected.provider : 'Processed on your configured local compute nodes.';
      $('#model-notice').hidden = modelsLoading || models.length > 0;
      $('#run').disabled = !current() || !selected || busy(current()) || uploadBusy;
      $('#cancel').hidden = !current()?.job || terminal(current().job);
      $('#run').textContent = busy(current()) ? 'Extracting…' : 'Extract image';
      $('#delete-image').disabled = !current() || busy(current());
      $('#run-hint').textContent = busy(current()) ? 'You can switch images while this runs. Closing this page keeps the job running.' : 'Uploads go only to your chosen model route. Temporary sources expire after 24 hours; retained sources after 30 days.';
      $('#run-batch').disabled = uploadBusy || ![...records.values()].some(item => item.selected && !busy(item) && item.settings.model);
      $('#cancel-batch').hidden = ![...jobs.values()].some(job => !terminal(job));
    }
    function choose(id) {
      saveDraft(); active = id; noticeError = ''; loadDraft(); render();
    }
    function inbox() {
      for (const [id, row] of inboxNodes) if (!records.has(id)) { row.remove(); inboxNodes.delete(id); }
      const query = $('#inbox-search').value.toLowerCase();
      for (const item of records.values()) {
        let row = inboxNodes.get(item.asset.id);
        if (!row) {
          const fragment = template('#inbox-item-template');
          row = fragment.querySelector('.ex-inbox-item');
          row.querySelector('input').addEventListener('change', event => { records.get(item.asset.id).selected = event.target.checked; controls(); inbox(); });
          row.querySelector('button').addEventListener('click', () => choose(item.asset.id));
          inboxNodes.set(item.asset.id, row); $('#inbox-list').append(fragment);
        }
        const select = row.querySelector('input'), button = row.querySelector('button');
        row.hidden = !item.asset.name.toLowerCase().includes(query);
        select.checked = item.selected; select.setAttribute('aria-label', 'Select ' + item.asset.name);
        button.setAttribute('aria-current', String(item.asset.id === active));
        const source = apiRoot + 'assets/' + item.asset.id + '?prepared=1';
        if (row.querySelector('img').getAttribute('src') !== source) row.querySelector('img').src = source;
        row.querySelector('[data-inbox-name]').textContent = item.asset.name;
        row.querySelector('[data-inbox-detail]').textContent = item.settings.task + ' → ' + item.settings.format.toUpperCase();
        row.querySelector('[data-inbox-status]').textContent = item.submitting ? 'Submitting' : item.job?.status.replaceAll('_', ' ') || 'Ready';
      }
      $('#batch-count').textContent = [...records.values()].filter(item => item.selected).length + ' selected';
      $('#inbox-empty').hidden = $$('.ex-inbox-item').some(row => !row.hidden);
    }
    function displayRows(target, rows, header = false) {
      target.replaceChildren();
      for (const row of rows) {
        const fragment = template('#data-row-template'), tr = fragment.querySelector('tr');
        for (const value of row) { const td = document.createElement(header ? 'th' : 'td'); td.textContent = value ?? ''; if (header) td.scope = 'col'; tr.append(td); }
        target.append(fragment);
      }
    }
    function receipt(item) {
      const data = item.correction || item.job.data;
      $('#receipt-merchant').textContent = data.merchant || 'Unknown merchant';
      $('#receipt-fields').replaceChildren();
      for (const key of Object.keys(data).filter(key => key !== 'items')) {
        const fragment = template('#receipt-field-template'), input = fragment.querySelector('input'), label = fragment.querySelector('label');
        input.id = 'field-' + key; label.htmlFor = input.id; label.textContent = key.replaceAll('_', ' ');
        input.value = data[key] ?? ''; input.placeholder = 'Not readable';
        const button = fragment.querySelector('button'); button.hidden = true;
        fragment.querySelector('[data-field-note]').textContent = data[key] == null ? 'Unknown · no value inferred' : item.job.revision > 1 ? 'Correction revision ' + item.job.revision : 'Model extraction · check source';
        fragment.querySelector('.ex-receipt-field').toggleAttribute('data-unclear', data[key] == null);
        input.addEventListener('input', () => {
          item.correction = structuredClone(item.correction || item.job.data);
          item.correction[key] = input.value === '' ? null : input.value;
          item.rawDraft = null; $('#raw-editor').setCustomValidity('');
          $('#save-review').hidden = false; $('#result-state').textContent = 'Unsaved corrections';
        });
        $('#receipt-fields').append(fragment);
      }
      displayRows($('#receipt-items'), data.items.map(entry => [entry.description, entry.quantity, entry.amount]));
      if (!data.items.length) $('#receipt-items').append(template('#empty-items-template'));
      $('#reconciliation').textContent = item.job.warnings.some(warning => /mismatch/.test(warning.code)) ? 'Needs review · receipt amounts do not agree.' : item.job.warnings.some(warning => /unreadable/.test(warning.code)) ? 'Unknown fields need review. No missing amount was inferred.' : 'Line items and available totals were checked independently.';
      $('#review-toggle').textContent = item.job.reviewed ? 'Reviewed ✓' : 'Mark reviewed';
      $('#review-toggle').setAttribute('aria-pressed', String(item.job.reviewed));
    }
    function result() {
      const item = current(), job = item?.job, ready = successful(job);
      let stale = false;
      if (ready && item) {
        try { const draft = specFor(item); stale = Object.keys(draft).some(key => JSON.stringify(canonical(draft[key])) !== JSON.stringify(canonical(job.spec[key]))); }
        catch { stale = true; }
      }
      $('#result-empty').hidden = ready;
      $('#result-preview').hidden = !ready || view !== 'preview';
      $('#raw-output').hidden = !ready || view !== 'raw';
      $('#correction-editor').hidden = !ready || view !== 'raw';
      $('#copy').disabled = $('#download').disabled = !ready;
      $('#original-export').hidden = !ready;
      $('#save-review').hidden = !item?.correction;
      $('#review-general').hidden = !ready || job.task === 'receipt';
      $('#review-general').textContent = job?.reviewed ? 'Reviewed ✓' : 'Mark reviewed';
      $('#review-general').setAttribute('aria-pressed', String(Boolean(job?.reviewed)));
      $('#result-state').textContent = item?.correction ? 'Unsaved corrections' : stale ? 'Settings changed' : job?.reviewed ? 'Reviewed' : (job?.status || 'No result yet').replaceAll('_', ' ');
      $('#error-notice').hidden = job?.status !== 'failed' && !noticeError;
      if (job?.status === 'failed' || noticeError) $('#error-message').textContent = noticeError || job.error.message;
      $('#review-notice').hidden = !ready || (!job.warnings.length && !stale);
      $('#review-notice').textContent = stale ? 'Settings changed. Extract to create a new result; the current output uses the previous settings.' : (job?.warnings || []).map(warning => warning.code.replaceAll('_', ' ') + (warning.path ? ': ' + warning.path : '')).join(' · ');
      if (ready) {
        $('#raw-output').textContent = job.text;
        if (document.activeElement !== $('#raw-editor')) $('#raw-editor').value = item.rawDraft ?? JSON.stringify(item.correction || job.data, null, 2);
        $('#receipt-preview').hidden = job.task !== 'receipt';
        $('#table-preview').hidden = job.task !== 'table';
        $('#text-preview').hidden = ['receipt', 'table'].includes(job.task);
        $('#markdown-preview').hidden = job.output_format !== 'markdown' || ['receipt', 'table'].includes(job.task);
        if (!$('#markdown-preview').hidden) {
          $('#text-preview').hidden = true;
          // The server's Markdown renderer disables raw HTML and remote image loading.
          $('#markdown-preview').innerHTML = job.preview_html;
        }
        $('#text-preview').textContent = job.text || 'No visible text.'; $('#text-preview').dataset.format = job.output_format;
        if (job.task === 'receipt' && !$('#receipt-fields').contains(document.activeElement)) receipt(item);
        if (job.task === 'table') { displayRows($('#table-head'), [job.data.columns], true); displayRows($('#table-body'), job.data.rows); }
        $('#result-provenance').textContent = `${job.provenance.model || job.spec.model} · ${job.provenance.node || (job.provenance.hosted ? 'cloud' : 'local')} · revision ${job.revision} · ${job.usage.total_tokens ?? 'unreported'} tokens`;
        $('#output-type').textContent = {json:'application/json',csv:'text/csv',markdown:'text/markdown',plain:'text/plain',layout:'text/plain'}[job.output_format];
      } else $('#result-provenance').textContent = busy(item) ? 'Waiting for the actual model result…' : '';
      $$('[data-result-view]').forEach(button => { const on = button.dataset.resultView === view; button.classList.toggle('active', on); button.setAttribute('aria-pressed', String(on)); });
    }
    function render() {
      const item = current();
      $('#image-stage').hidden = Boolean(!item); $('#source-empty').hidden = Boolean(item);
      if (item) {
        const url = apiRoot + 'assets/' + item.asset.id + '?prepared=1';
        if ($('#source-image').getAttribute('src') !== url) $('#source-image').src = url;
        $('#source-image').alt = item.asset.name;
        $('#source-name').textContent = item.asset.name; $('#source-size').textContent = `${item.asset.width} × ${item.asset.height}`;
      } else { $('#source-image').removeAttribute('src'); $('#source-name').textContent = 'No image selected'; $('#source-size').textContent = ''; }
      $('#region-help').textContent = 'Compare the image directly. Qualified source coordinates are unavailable for this model.';
      $('#asset-reference').textContent = item?.asset.id || 'Upload an image first.';
      controls(); inbox(); result();
    }
    async function upload(files) {
      if (uploadBusy) return;
      const chosen = [...files];
      if (chosen.length > 20) { failure(new Error('Add at most twenty images at a time.')); return; }
      saveDraft(); uploadBusy = true; controls();
      try {
        for (const file of chosen) {
          if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type) || file.size > 5 * 1024 * 1024) throw new Error('Use PNG, JPEG or WebP images of at most 5 MiB.');
          const form = new FormData(); form.append('image', file);
          const asset = await api('assets', {method: 'POST', body: form});
          records.set(asset.id, {asset, settings: structuredClone(current()?.settings || defaults), selected: true, job: null, correction: null});
          if (!active) active = asset.id;
        }
        if (records.size > 1) toggleBatch(true);
        loadDraft(); say(`${chosen.length} image${chosen.length === 1 ? '' : 's'} uploaded. Choose a task and extract.`);
      } catch (error) { failure(error); }
      finally { uploadBusy = false; $('#image-file').value = ''; render(); }
    }
    async function submit(item) {
      if (busy(item)) return;
      item.submitting = true; render();
      try {
        const spec = specFor(item);
        if (item.job?.id) spec.retry_of = item.job.id;
        const fingerprint = JSON.stringify(canonical(spec));
        const token = item.pendingSubmission?.fingerprint === fingerprint ? item.pendingSubmission.token :
          crypto.randomUUID?.() || [...crypto.getRandomValues(new Uint8Array(24))].map(value => value.toString(16).padStart(2, '0')).join('');
        item.pendingSubmission = {fingerprint, token};
        const job = await send('jobs', 'POST', spec, {'idempotency-key': token});
        item.pendingSubmission = null;
        item.job = job; item.correction = null; item.rawDraft = null; jobs.set(job.id, job);
      } finally { item.submitting = false; }
    }
    async function run(selected) {
      saveDraft(); noticeError = ''; $('#error-notice').hidden = true; say('Submitting extraction…');
      try {
        for (const item of selected) { await submit(item); render(); }
        say('Submitted to the normal inference queue. Each image keeps its own task and result.');
      } catch (error) { failure(error); }
      render(); schedulePoll();
    }
    function schedulePoll() {
      clearTimeout(timer);
      if (alive && ([...records.values()].some(busy) || [...jobs.values()].some(job => !terminal(job)))) timer = setTimeout(poll, 900);
    }
    async function poll() {
      try {
        await Promise.all([...jobs.values()].filter(job => !terminal(job)).map(async previous => {
          const job = await api('jobs/' + previous.id); jobs.set(job.id, job);
          for (const item of records.values()) if (item.job?.id === job.id) item.job = job;
        }));
        if (alive) { render(); if (![...records.values()].some(busy)) { say('Extraction finished. Review the result against its source.'); historyRows(); } }
      } catch (error) { if (alive) failure(error); }
      schedulePoll();
    }
    async function saveReview(reviewed = false) {
      const item = current(); if (!successful(item?.job)) return;
      try {
        if (item.rawDraft != null) {
          const data = JSON.parse(item.rawDraft);
          if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error('Corrections must be a JSON object.');
          item.correction = data;
        }
        const job = await send('jobs/' + item.job.id + '/review', 'PATCH', {revision: item.job.revision,
          data: item.correction || item.job.data, reviewed});
        item.job = job; item.correction = null; item.rawDraft = null; jobs.set(job.id, job); render(); historyRows(); say('Saved correction revision ' + job.revision + '. Original output is retained.');
      } catch (error) { failure(error); }
    }
    function historyRows() {
      $('#history').replaceChildren();
      for (const job of [...jobs.values()].sort((a, b) => b.created - a.created)) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-outline-secondary text-start';
        button.textContent = `${job.asset?.name || job.asset_id} · ${job.task} · ${job.status.replaceAll('_', ' ')} · ${new Date(job.created * 1000).toLocaleString()}`;
        button.addEventListener('click', () => {
          if (!job.asset) return;
          saveDraft(); const existing = records.get(job.asset_id);
          records.set(job.asset_id, {...existing, asset: job.asset, settings: draftFrom(job.spec), job, correction: null, rawDraft: null, selected: existing?.selected ?? false});
          active = job.asset_id; loadDraft(); render(); schedulePoll();
        }); $('#history').append(button);
      }
      if (!jobs.size) $('#history').textContent = 'No retained extractions yet.';
      $('#more-history').hidden = !cursor;
    }
    async function history(older = false) {
      const listing = await api('jobs' + (older && cursor ? '?cursor=' + encodeURIComponent(cursor) : ''));
      cursor = listing.next_cursor;
      for (const job of listing.data) {
        jobs.set(job.id, job);
        if (job.asset && !records.has(job.asset_id)) records.set(job.asset_id, {asset: job.asset, settings: draftFrom(job.spec), selected: false, job, correction: null});
      }
      if (!active && records.size) { active = records.keys().next().value; loadDraft(); }
      historyRows(); render(); schedulePoll();
    }
    function toggleBatch(open = root.dataset.batch !== 'open') {
      root.dataset.batch = open ? 'open' : 'closed';
      $('#toggle-batch').setAttribute('aria-expanded', String(open));
      $('#toggle-batch').textContent = open ? 'Hide batch' : 'Batch inbox';
    }
    $('#toggle-batch').addEventListener('click', () => toggleBatch());
    $('#extraction-form').addEventListener('submit', event => { event.preventDefault(); if (!$('#run').disabled) run([current()]); });
    $('#extraction-form').addEventListener('change', () => { controls(); saveDraft(); inbox(); result(); });
    $('#raw-editor').addEventListener('input', () => {
      current().rawDraft = $('#raw-editor').value;
      try {
        const data = JSON.parse($('#raw-editor').value);
        if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error();
        current().correction = data; $('#raw-editor').setCustomValidity(''); $('#save-review').hidden = false; $('#result-state').textContent = 'Unsaved corrections';
      } catch { $('#raw-editor').setCustomValidity('Enter a valid JSON object before saving.'); }
    });
    $('#save-review').addEventListener('click', () => { if ($('#raw-editor').checkValidity()) saveReview(false); else $('#raw-editor').reportValidity(); });
    $('#review-toggle').addEventListener('click', () => saveReview(!current().job.reviewed));
    $('#review-general').addEventListener('click', () => saveReview(!current().job.reviewed));
    $('#retry').addEventListener('click', () => { if (!current() || !models.length) loadModels().then(() => history()).catch(failure); else run([current()]); });
    $('#run-batch').addEventListener('click', () => run([...records.values()].filter(item => item.selected && !busy(item))));
    $('#cancel').addEventListener('click', async () => { try { current().job = await send('jobs/' + current().job.id + '/cancel', 'POST', {}); render(); } catch (error) { failure(error); } });
    $('#cancel-batch').addEventListener('click', async () => { try { for (const previous of [...jobs.values()].filter(job => !terminal(job))) {
      const job = await send('jobs/' + previous.id + '/cancel', 'POST', {}); jobs.set(job.id, job);
      for (const item of records.values()) if (item.job?.id === job.id) item.job = job;
    } render(); say('Active extractions cancelled. Completed results are kept.'); } catch (error) { failure(error); } });
    $('#inbox-search').addEventListener('input', inbox);
    $$('[data-result-view]').forEach(button => button.addEventListener('click', () => { view = button.dataset.resultView; result(); }));
    $('#copy').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(current().job.text); say('Output copied.'); }
      catch { view = 'raw'; result(); const range = document.createRange(); range.selectNodeContents($('#raw-output')); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); $('#raw-output').focus(); say('Output selected. Press Ctrl/⌘ C to copy.'); }
    });
    $('#download').addEventListener('click', () => { const link = document.createElement('a'); link.href = apiRoot + 'jobs/' + current().job.id + '/export'; link.download = ''; link.click(); });
    $('#original-export').addEventListener('click', () => { const link = document.createElement('a'); link.href = apiRoot + 'jobs/' + current().job.id + '/export?format=json&revision=original'; link.download = ''; link.click(); });
    $('#delete-image-form').addEventListener('submit', async event => {
      event.preventDefault();
      // console.js owns the shared two-click confirmation and permits only its confirmed click.
      const item = current(); if (!item || busy(item)) return;
      try {
        const owned = []; let next = '';
        do { const listing = await api('jobs' + (next ? '?cursor=' + encodeURIComponent(next) : '')); owned.push(...listing.data.filter(job => job.asset_id === item.asset.id)); next = listing.next_cursor; } while (next);
        if (owned.some(job => !terminal(job))) throw new Error('Cancel this image’s active extractions before deleting it.');
        for (const job of owned) { await api('jobs/' + job.id, {method: 'DELETE'}); jobs.delete(job.id); }
        if (!owned.length) await api('assets/' + item.asset.id, {method: 'DELETE'});
        records.delete(item.asset.id); active = records.keys().next().value || ''; loadDraft(); render(); historyRows(); say('Deleted this image and its results.');
      } catch (error) { failure(error); }
    });
    async function loadPresets() {
      presets = (await api('presets')).presets; $('#preset').replaceChildren(new Option('Choose a preset…', ''));
      for (const preset of presets) $('#preset').add(new Option(preset.name, preset.id));
    }
    $('#save-preset').addEventListener('click', async () => {
      try { saveDraft(); const spec = specFor(current() || {asset: {id: 'preset'}, settings: defaults}); delete spec.asset_id;
        await send('presets', 'POST', {name: $('#preset-name').value, spec}); await loadPresets(); say('Extraction settings saved.'); } catch (error) { failure(error); }
    });
    $('#delete-preset').addEventListener('click', async () => { if (!$('#preset').value || !await window.suiteConfirmLoss('Delete this extraction preset.')) return; try { await api('presets/' + $('#preset').value, {method: 'DELETE'}); await loadPresets(); } catch (error) { failure(error); } });
    $('#preset').addEventListener('change', () => {
      const preset = presets.find(one => one.id === $('#preset').value); if (!preset) return;
      if (current()) current().settings = draftFrom(preset.spec); else defaults = draftFrom(preset.spec);
      loadDraft(); inbox(); say('Preset applied to this image. Extract to create a new result.');
    });
    $('#refresh-history').addEventListener('click', () => loadModels().then(() => history()).catch(failure));
    $('#more-history').addEventListener('click', () => history(true).catch(failure));
    ['#choose-image', '#empty-choose'].forEach(id => $(id).addEventListener('click', () => $('#image-file').click()));
    $('#image-file').addEventListener('change', event => upload(event.target.files));
    $('#drop-zone').addEventListener('keydown', event => { if (event.target === $('#drop-zone') && ['Enter', ' '].includes(event.key)) { event.preventDefault(); $('#image-file').click(); } });
    $('#drop-zone').addEventListener('dragover', event => { event.preventDefault(); $('#drop-zone').dataset.dragging = ''; });
    $('#drop-zone').addEventListener('dragleave', () => delete $('#drop-zone').dataset.dragging);
    $('#drop-zone').addEventListener('drop', event => { event.preventDefault(); delete $('#drop-zone').dataset.dragging; upload(event.dataTransfer.files); });
    const paste = event => { if (!alive || !root.isConnected) return; const files = [...event.clipboardData.items].filter(item => item.kind === 'file').map(item => item.getAsFile()); if (files.length) { event.preventDefault(); upload(files); } };
    document.addEventListener('paste', paste);
    cleanup = () => { alive = false; clearTimeout(timer); document.removeEventListener('paste', paste); };
    async function loadModels() {
      modelsLoading = true;
      try {
        const value = await api('models'); models = value.models;
        $('#model').replaceChildren(new Option('Choose a vision model…', ''));
        for (const model of models) $('#model').add(new Option(model.id + ' · ' + model.provider, model.id));
        defaults.model ||= models.find(model => !model.hosted && !model.role)?.id || models[0]?.id || '';
        noticeError = ''; loadDraft(); render();
      } finally { modelsLoading = false; controls(); }
    }
    Promise.all([loadModels(), loadPresets()]).then(() => history()).catch(failure);
    render();
  }
  document.addEventListener('DOMContentLoaded', setup);
  document.addEventListener('htmx:load', setup);
})();
