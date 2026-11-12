/* The live operations desk. All HTML comes from the shared database DOM templates. */
(() => {
  'use strict';
  function initialize() {
    const root = document.querySelector('[data-database-console]');
    if (!root || root.dataset.initialized) return;
    root.dataset.initialized = 'true';
    const $ = id => root.querySelector(`#${id}`);
    const clone = id => $(id).content.firstElementChild.cloneNode(true);
    const text = (element, selector, value) => { element.querySelector(selector).textContent = value; };
    const number = value => value == null ? '—' : Number(value).toLocaleString();
    const size = value => value == null ? 'Unknown' : value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB` : value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MiB` : `${(value / 1024).toFixed(1)} KiB`;
    const when = value => new Date(value * 1000).toLocaleString();
    const state = window.databaseDeskState ||= {selected: new Set(), expanded: new Set(), search: '', node: 'all', engine: 'all'};
    let snapshot = JSON.parse($('database-snapshot').textContent), details = new Map(), pending = new Set();
    let exportScope, cleanupKeys = [], review = null, refreshing = false, jobsBusy = false;
    const jobStates = new Map();
    let needsRefresh = false;
    const api = '/databases/api/';
    const selected = () => snapshot.databases.filter(db => state.selected.has(db.key));
    const visible = () => snapshot.databases.filter(db => (state.node === 'all' || db.consumers.some(route => route.machine === state.node))
      && (state.engine === 'all' || state.engine === db.engine)
      && `${db.name} ${db.owner} ${db.bindings.join(' ')}`.toLowerCase().includes(state.search.toLowerCase().trim()));
    async function request(url, body) {
      const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store', ...(body === undefined ? {} : {method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)})});
      let result;
      try { result = await response.json(); } catch { throw new Error('The server did not return a database response. Refresh your sign-in and retry.'); }
      if (!response.ok) throw new Error(result.error?.message || 'Database operation failed.');
      return result;
    }
    function notice(error) { $('status').textContent = error.message || error; }
    async function busy(button, work) {
      button.disabled = true;
      try { await work(); } catch (error) { notice(error); $('cleanup-result').textContent = error.message; }
      finally { button.disabled = button.id === 'apply-maintenance' ? !review || !$('confirm-maintenance').checked : false; }
    }
    function render() {
      const activeRow = document.activeElement?.closest('[data-database]');
      const activeControl = activeRow && (document.activeElement.matches('[data-expand]') ? '[data-expand]' : document.activeElement.matches('[data-select]') ? '[data-select]' : null);
      const activeKey = activeRow?.dataset.database;
      const nodes = snapshot.nodes.filter(node => node.name !== 'front' && node.lifecycle !== 'retired' && node.configured !== false);
      $('coverage').textContent = `${nodes.filter(node => node.answering).length} / ${nodes.length}`;
      const total = snapshot.databases.reduce((sum, db) => sum + (db.size || 0), 0);
      $('totals').textContent = snapshot.databases.length;
      $('disk-total').textContent = size(total);
      $('disk-note').textContent = snapshot.complete ? '' : 'Incomplete inventory';
      $('snapshot-time').textContent = `Snapshot · ${when(snapshot.checked_at)}`;
      $('coverage-note').hidden = snapshot.complete;
      $('coverage-message').textContent = snapshot.inventory_error || snapshot.nodes.filter(node => !node.answering && node.lifecycle !== 'retired').map(node => node.error).join(' ') || 'One or more configured stores are unavailable. Complete export is blocked; inspect the affected node and binding.';
      $('export-all').disabled = !snapshot.databases.length;
      root.querySelector('[data-all-export]').disabled = $('export-all').disabled;
      const nodeFilter = $('node-filter');
      nodeFilter.replaceChildren(new Option('All nodes', 'all'), ...snapshot.nodes.map(node => new Option(node.name, node.name)));
      if (![...nodeFilter.options].some(option => option.value === state.node)) state.node = 'all';
      nodeFilter.value = state.node;
      $('node-strip').replaceChildren(...snapshot.nodes.map(node => {
        const button = document.createElement('button'); button.type = 'button';
        button.className = `btn btn-sm ${state.node === node.name ? 'btn-primary' : 'btn-outline-secondary'}`;
        const count = snapshot.databases.filter(db => db.consumers.some(route => route.machine === node.name)).length;
        button.textContent = `${node.name} · ${node.lifecycle === 'retired' ? 'retired' : node.answering ? `${count} DB` : 'unreachable'}`;
        button.setAttribute('aria-pressed', state.node === node.name);
        button.onclick = () => { state.node = state.node === node.name ? 'all' : node.name; render(); };
        return button;
      }));
      const filtered = visible(), body = $('databases'); body.replaceChildren();
      for (const db of filtered) {
        const row = clone('database-row'); row.dataset.database = db.key;
        text(row, '[data-name]', db.name); text(row, '[data-owner]', db.owner);
        text(row, '[data-node]', db.consumers.map(route => route.machine).filter((name, i, all) => all.indexOf(name) === i).join(', '));
        text(row, '[data-engine]', db.engine); text(row, '[data-tables]', number(db.table_count));
        text(row, '[data-records]', (db.records_estimated && db.records != null ? '≈ ' : '') + number(db.records));
        text(row, '[data-size]', size(db.size));
        text(row, '[data-health]', db.error ? 'Unavailable' : '● Reachable');
        row.querySelector('[data-health]').className = db.error ? 'text-danger' : 'text-success';
        const backup = db.backup;
        text(row, '[data-backup]', backup?.age_seconds != null ? `${(backup.age_seconds / 3600).toFixed(1)}h · ${backup.state} backup` : `${backup?.state || 'unknown'} backup`);
        const check = row.querySelector('[data-select]');
        check.checked = state.selected.has(db.key); check.disabled = !!db.error;
        check.setAttribute('aria-label', `Select ${db.name} on ${db.machine}`);
        check.onchange = () => { check.checked ? state.selected.add(db.key) : state.selected.delete(db.key); render();
          body.querySelector(`[data-database="${db.key}"] [data-select]`)?.focus(); };
        const expand = row.querySelector('[data-expand]'); expand.title = db.owner;
        expand.setAttribute('aria-expanded', state.expanded.has(db.key));
        expand.setAttribute('aria-label', `Show tables for ${db.name} on ${db.machine}`);
        expand.setAttribute('aria-controls', `detail-${db.key}`);
        text(row, '[data-chevron]', state.expanded.has(db.key) ? '▾' : '▸');
        expand.onclick = () => { state.expanded.has(db.key) ? state.expanded.delete(db.key) : state.expanded.add(db.key); render();
          body.querySelector(`[data-database="${db.key}"] [data-expand]`)?.focus(); };
        row.querySelector('[data-dump]').disabled = !!db.error;
        row.querySelector('[data-dump]').onclick = () => { location.href = db.dump_url; };
        row.querySelector('[data-maintain]').disabled = !!db.error;
        row.querySelector('[data-maintain]').onclick = () => openCleanup([db.key]);
        body.append(row);
        if (state.expanded.has(db.key)) body.append(detailRow(db));
      }
      if (!filtered.length) body.append(clone('empty-databases'));
      $('visible-count').textContent = `${filtered.length} of ${snapshot.databases.length} databases shown`;
      const chosen = selected();
      $('selection').textContent = chosen.length ? `${chosen.length} selected across all filters` : 'No databases selected';
      for (const id of ['export-selected', 'purge-selected', 'reclaim-selected']) $(id).disabled = !chosen.length || chosen.some(db => db.error);
      const selectable = filtered.filter(db => !db.error);
      $('select-all').checked = !!selectable.length && selectable.every(db => state.selected.has(db.key));
      $('select-all').indeterminate = selectable.some(db => state.selected.has(db.key)) && !$('select-all').checked;
      $('select-all').disabled = !selectable.length;
      $('discovered').replaceChildren(...snapshot.discovered.map(db => {
        const row = clone('discovered-row'); text(row, '[data-name]', `${db.machine} / ${db.name}`); text(row, '[data-path]', db.path); text(row, '[data-size]', size(db.size));
        row.querySelector('[data-dump]').disabled = !!db.error;
        row.querySelector('[data-dump]').onclick = () => { location.href = db.dump_url; }; return row;
      }));
      if (!snapshot.discovered.length) $('discovered').append(clone('empty-discovered'));
      if (activeControl) body.querySelector(`[data-database="${activeKey}"] ${activeControl}`)?.focus();
    }
    function detailRow(db) {
      const row = clone('database-detail'); row.id = `detail-${db.key}`;
      text(row, '[data-detail-name]', `${db.name} / ${db.machine}`);
      text(row, '[data-detail-owner]', db.owner); text(row, '[data-path]', db.path);
      text(row, '[data-bindings]', `Bindings: ${db.bindings.join(', ')}`);
      const detail = details.get(db.key), table = row.querySelector('[data-table-list]');
      table.setAttribute('aria-label', `Tables in ${db.name} on ${db.machine}`);
      const note = row.querySelector('.detail-content > p');
      if (!detail) {
        note.textContent = 'Loading exact table counts…'; table.hidden = true;
        if (!pending.has(db.key)) loadDetails(db);
      } else if (detail.error) {
        note.textContent = detail.error; table.hidden = true;
        const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'btn btn-outline-secondary btn-sm mt-2'; retry.textContent = 'Retry table inventory';
        retry.onclick = () => { details.delete(db.key); render(); }; note.append(document.createElement('br'), retry);
      } else {
        text(row, '[data-storage]', `${detail.server_version || detail.engine} · ${detail.dump_method} · ${detail.engine === 'SQLite' ? `main ${size(detail.main_size)} / reusable ${size(detail.free)} / WAL + SHM ${size((detail.size || 0) - (detail.main_size || 0))}` : `database ${size(detail.size)}`}`);
        note.textContent = `Exact counts checked ${when(detail.loaded_at)}. Table sizes include indexes. Table SQL may depend on other tables; use the whole database for recovery.`;
        for (const item of detail.tables) {
          const tr = clone('table-row'); text(tr, '[data-table-name]', item.name); text(tr, '[data-records]', number(item.records));
          text(tr, '[data-size]', size(item.size)); text(tr, '[data-indexes]', number(item.indexes));
          text(tr, '[data-retention]', db.bindings.includes('spotter.history') && ['readings', 'public.readings'].includes(item.name) ? 'Eligible · readings.at' : 'Protected / policy required');
          tr.querySelector('[data-table-dump]').onclick = () => { location.href = db.dump_url + '?table=' + encodeURIComponent(item.name); };
          row.querySelector('[data-table-rows]').append(tr);
        }
      }
      return row;
    }
    async function loadDetails(db) {
      pending.add(db.key);
      try { const detail = await request(db.detail_url); if (detail.identity && detail.identity !== db.identity) throw new Error('Store identity changed. Refresh snapshot.'); details.set(db.key, {...detail, loaded_at: Date.now() / 1000}); }
      catch (error) { details.set(db.key, {error: error.message}); }
      finally { pending.delete(db.key); if (root.isConnected) render(); }
    }
    async function refresh() {
      if (refreshing) return;
      refreshing = true;
      try {
        snapshot = await request(api + 'snapshot'); details.clear();
        const keys = new Set(snapshot.databases.map(db => db.key));
        state.selected.forEach(key => { if (!keys.has(key)) state.selected.delete(key); });
        render(); notice('Snapshot refreshed. Expanded table counts are reloaded.');
      } catch (error) { notice(error); }
      finally { refreshing = false; }
    }
    function openExport(scope) {
      exportScope = scope;
      const stores = scope === 'all' ? snapshot.databases : selected();
      $('export-summary').textContent = `${stores.length} database${stores.length === 1 ? '' : 's'} · ZIP with SQL dumps and manifest`;
      $('export-list').replaceChildren(...stores.map(db => { const li = document.createElement('li'); li.textContent = `${db.machine} / ${db.name} · ${db.engine}${db.error ? ' · unavailable' : ''}`; return li; }));
      $('partial-option').hidden = scope !== 'all' || snapshot.complete; $('allow-partial').checked = false;
      $('download-export').disabled = scope === 'all' && !snapshot.complete;
      $('export-dialog').showModal();
    }
    function invalidateReview() { review = null; $('maintenance-preview').hidden = true; $('confirm-maintenance').checked = false; $('apply-maintenance').disabled = true; }
    function openCleanup(keys) {
      cleanupKeys = keys; invalidateReview(); $('cleanup-result').textContent = '';
      $('cleanup-scope').textContent = snapshot.databases.filter(db => keys.includes(db.key)).map(db => `${db.machine} / ${db.name}`).join(' · ');
      $('cleanup-dialog').showModal();
    }
    async function preview(action) {
      invalidateReview();
      const result = await request(api + 'preview', {scope: cleanupKeys, action, cutoff: $('cutoff').value, mode: $('reclaim-mode').value});
      review = result;
      $('maintenance-rows').replaceChildren(...result.stores.map(store => {
        const tr = document.createElement('tr');
        for (const value of [`${store.machine} / ${store.id}`, action === 'purge' ? number(store.count) : store.method,
          action === 'purge' ? store.reason : store.headroom ? `${size(store.headroom.free_bytes)} free / ${size(store.headroom.required_bytes)} required` : 'Routine reuse; disk shrink is not promised']) {
          const td = document.createElement('td'); td.textContent = value; tr.append(td);
        }
        return tr;
      }));
      $('maintenance-notice').textContent = `Review expires ${when(result.expires_at)}. A verified recovery copy is created before changing each store. Purge keeps the cutoff date; disk reclamation is separate. Completed batches cannot be cancelled or rolled back automatically.`;
      $('maintenance-preview').hidden = false;
    }
    async function refreshJobs() {
      if (jobsBusy || !root.isConnected) return;
      jobsBusy = true;
      try {
        const result = await request(api + 'jobs');
        for (const job of result.jobs) {
          const key = `${job.machine}/${job.type}/${job.id}`;
          if (['running', 'queued'].includes(jobStates.get(key)) && !['running', 'queued', 'cancelling'].includes(job.state)) needsRefresh = true;
          jobStates.set(key, job.state);
        }
        if (needsRefresh && !root.querySelector('dialog[open]') && !window.getSelection()?.toString()) { needsRefresh = false; await refresh(); }
        if ($('jobs').contains(document.activeElement)) return;
        $('jobs').replaceChildren(...result.jobs.map(job => {
          const row = clone('database-job'); text(row, '[data-job-title]', `${job.kind} · ${job.machine}`);
          text(row, '[data-job-state]', `${job.state} · ${when(job.created_at)}${job.error ? ' · ' + job.error : ''}`);
          const outcomes = row.querySelector('[data-job-outcomes]');
          for (const outcome of job.outcomes || []) {
            const line = document.createElement('div');
            line.textContent = `${outcome.machine || outcome.id || outcome.database}: ${outcome.state}${outcome.records_deleted != null ? ` · ${number(outcome.records_deleted)} deleted` : ''}${outcome.reclaimed_bytes != null ? ` · ${size(outcome.reclaimed_bytes)} returned to disk` : ''}${outcome.error ? ' · ' + outcome.error : ''}`;
            outcomes.append(line);
          }
          const actions = row.querySelector('[data-job-actions]');
          const base = job.type === 'maintenance' ? `/databases/${encodeURIComponent(job.machine)}/operation/jobs/${job.id}` : api + 'jobs/' + job.id;
          if (job.download && job.download_expires_at > Date.now() / 1000) { const a = document.createElement('a'); a.className = 'btn btn-outline-primary btn-sm'; a.href = base + '/download'; a.textContent = 'Download ZIP'; actions.append(a); }
          if (job.type === 'maintenance' && (job.outcomes || []).some(outcome => outcome.recovery)) { const a = document.createElement('a'); a.className = 'btn btn-outline-secondary btn-sm'; a.href = base + '/recovery'; a.textContent = 'Recovery receipt'; actions.append(a); }
          if (['queued', 'running', 'cancelling'].includes(job.state)) { const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-outline-secondary btn-sm'; button.textContent = 'Cancel remaining'; button.onclick = () => busy(button, async () => { await request(base + '/cancel', {}); notice('Cancellation requested. The current transaction may finish.'); await refreshJobs(); }); actions.append(button); }
          return row;
        }));
        if (!result.jobs.length) { const li = document.createElement('li'); li.textContent = 'No database operations recorded for your account.'; $('jobs').append(li); }
      } catch (error) { $('jobs').textContent = error.message; }
      finally { jobsBusy = false; }
    }
    $('search').value = state.search; $('engine-filter').value = state.engine;
    $('search').oninput = () => { state.search = $('search').value; render(); };
    $('engine-filter').onchange = () => { state.engine = $('engine-filter').value; render(); };
    $('node-filter').onchange = () => { state.node = $('node-filter').value; render(); };
    $('select-all').onchange = () => { visible().filter(db => !db.error).forEach(db => $('select-all').checked ? state.selected.add(db.key) : state.selected.delete(db.key)); render(); };
    $('clear-selection').onclick = () => { state.selected.clear(); render(); };
    $('expand-all').onclick = () => { const rows = visible(), collapse = rows.every(db => state.expanded.has(db.key)); rows.forEach(db => collapse ? state.expanded.delete(db.key) : state.expanded.add(db.key)); render(); };
    $('refresh').onclick = refresh;
    $('export-all').onclick = root.querySelector('[data-all-export]').onclick = () => openExport('all');
    $('export-selected').onclick = () => openExport([...state.selected]);
    $('allow-partial').onchange = () => { $('download-export').disabled = !$('allow-partial').checked; };
    $('download-export').onclick = () => busy($('download-export'), async () => { const job = await request(api + 'export', {scope: exportScope, partial: $('allow-partial').checked}); $('export-dialog').close(); notice(`Export job ${job.id} started.`); await refreshJobs(); });
    $('purge-selected').onclick = $('reclaim-selected').onclick = () => openCleanup([...state.selected]);
    $('cutoff').max = new Date().toISOString().slice(0, 10);
    $('cutoff').value = new Date(Date.now() - 30 * 86400000).toISOString().slice(0, 10);
    $('cutoff').oninput = $('reclaim-mode').onchange = invalidateReview;
    $('preview-purge').onclick = () => busy($('preview-purge'), () => preview('purge'));
    $('preview-reclaim').onclick = () => busy($('preview-reclaim'), () => preview('reclaim'));
    $('confirm-maintenance').onchange = () => { $('apply-maintenance').disabled = !review || !$('confirm-maintenance').checked; };
    $('apply-maintenance').onclick = () => busy($('apply-maintenance'), async () => {
      if (!review || !$('confirm-maintenance').checked) return;
      const job = await request(api + 'execute', {review: review.review, confirmed: true}); invalidateReview();
      $('cleanup-result').textContent = `Maintenance job ${job.id} started. Progress and recovery copies appear in Recent operations. Refresh the snapshot after completion.`;
      await refreshJobs();
    });
    $('cleanup-form').onsubmit = event => event.preventDefault();
    root.querySelectorAll('[data-close]').forEach(button => { button.onclick = () => button.closest('dialog').close(); });
    root.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('close', () => { if (needsRefresh) { needsRefresh = false; refresh(); } }));
    $('refresh-jobs').onclick = refreshJobs;
    render(); refreshJobs();
    const timer = setInterval(() => { if (!root.isConnected) clearInterval(timer); else if (!document.hidden) refreshJobs(); }, 3000);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize); else initialize();
  document.addEventListener('htmx:load', initialize);
})();
