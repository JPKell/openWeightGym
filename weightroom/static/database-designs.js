/* Local design-review state. Never connects to a database or application endpoint. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const nodes = [
    {id: 'front', label: 'Front', role: 'Console & accounts'},
    {id: 'compute', label: 'Compute 01', role: 'Primary GPU'},
    {id: 'front-cpu', label: 'Front CPU', role: 'CPU worker'},
    {id: 'remote-cpu', label: 'Remote CPU', role: 'CPU worker'},
    {id: 'compute-04', label: 'Compute 04', role: 'Additional worker'},
  ];
  const descriptions = {
    ledger: 'One compact ledger: scan every database, then expand only the rows you need.',
    nodes: 'The same compact rows, grouped by node so missing coverage is easy to spot.',
    desk: 'Inventory beside an operations panel for repeated export and maintenance work.',
  };
  const names = {
    accounts: ['users', 'groups', 'keys', 'events', 'settings'],
    roles: ['roles', 'model_roles', 'measurements'],
    chat: ['conversations', 'messages', 'collections', 'workflow_runs'],
    benchmarks: ['benchmark_runs', 'benchmark_samples', 'benchmark_datasets'],
    plugins: ['plugin_uses', 'plugin_settings'],
    alerts: ['alerts', 'acknowledgements'],
    schedules: ['schedules', 'occurrences'],
    loadcoach: ['models', 'events', 'batches', 'model_loadouts'],
    spotter: ['readings'],
  };
  const owners = {
    accounts: 'Accounts · access · settings', roles: 'Roles · model placement', chat: 'Chats · collections · workflows',
    benchmarks: 'Benchmark plans & evidence', plugins: 'Plugin activity', alerts: 'Alerts & acknowledgements',
    schedules: 'Recurring tasks', loadcoach: 'Models · requests · settings', spotter: 'Telemetry history',
  };
  const number = value => value.toLocaleString('en-US');
  const size = mb => mb >= 1024 ? `${(mb / 1024).toFixed(2)} GiB` : `${mb.toFixed(1)} MiB`;
  let databases, selected = new Set(), expanded = new Set(), design = 'ledger', scenario = 'healthy';
  let exportScope = [], exportTable = null, cleanupScope = [], purgePlan = [], allExpanded = false;

  function seed() {
    let index = 0;
    return nodes.flatMap(node => (node.id === 'front' ? Object.keys(owners).slice(0, 7) : ['loadcoach', 'spotter']).map(kind => {
      const i = index++, engine = node.id.includes('cpu') ? 'SQLite' : 'PostgreSQL';
      const tables = names[kind].map((name, ti) => {
        const records = kind === 'spotter' ? 428400 + i * 1200 : (ti + 1) * (i + 1) * 137;
        return {name: (engine === 'PostgreSQL' ? 'public.' : '') + name, records,
          mb: kind === 'spotter' ? 460 + i * 14 : (ti + 1) * (i + 1) * 1.8,
          indexes: ti % 3 + 1, purge: kind === 'spotter',
          buckets: kind === 'spotter' ? [
            {date: '2026-09-01', count: 90000}, {date: '2026-10-01', count: 45000},
            {date: '2026-11-01', count: 12000},
          ] : []};
      });
      const mb = tables.reduce((sum, table) => sum + table.mb, 0) + 20;
      return {id: `${node.id}/${kind}`, kind, node: node.id, engine,
        name: engine === 'SQLite' ? `${kind}-${node.id}.db` : `owg_${kind}`,
        owner: owners[kind], tables, mb, reusable: engine === 'SQLite' ? 18 : null,
        backup: i === 10 ? '27h ago · stale' : '2h ago · verified',
        path: engine === 'SQLite' ? `/srv/owg/${node.id}/${kind}-${node.id}.db` : `${node.id}-pg / owg_${kind}`,
        maintenanceDone: false};
    }));
  }
  const nodeName = id => nodes.find(node => node.id === id).label;
  const offline = db => scenario === 'offline' && db.node === 'compute-04';
  const mismatch = db => scenario === 'mismatch' && db.id === 'compute/loadcoach';
  const available = db => !offline(db) && !mismatch(db);
  const active = () => scenario === 'empty' ? [] : databases;
  const selectedDatabases = () => active().filter(db => selected.has(db.id));
  const visible = () => active().filter(db => {
    const query = $('search').value.trim().toLowerCase();
    return ($('node-filter').value === 'all' || db.node === $('node-filter').value)
      && ($('engine-filter').value === 'all' || db.engine === $('engine-filter').value)
      && `${db.name} ${db.owner} ${db.tables.map(table => table.name).join(' ')}`.toLowerCase().includes(query);
  });
  const setText = (row, selector, value) => { row.querySelector(selector).textContent = value; };

  function render() {
    const data = active(), filtered = visible();
    if ($('workspace').dataset.layout !== design) {
      $('operations-card').open = $('jobs-card').open = design === 'desk';
    }
    $('workspace').dataset.layout = design;
    $('design-description').textContent = descriptions[design];
    document.querySelectorAll('[data-design]').forEach(button => {
      button.classList.toggle('active', button.dataset.design === design);
      button.setAttribute('aria-pressed', button.dataset.design === design);
    });
    $('coverage').textContent = `${scenario === 'offline' ? 3 : 4} / 4`;
    $('totals').textContent = data.length;
    $('disk-total').textContent = size(data.reduce((sum, db) => sum + db.mb, 0));
    $('disk-note').textContent = `${data.reduce((sum, db) => sum + db.tables.length, 0)} tables${scenario === 'offline' ? ' · last known' : ''}`;
    $('coverage-note').hidden = !['offline', 'mismatch'].includes(scenario);
    $('coverage-message').textContent = scenario === 'offline'
      ? 'Compute 04 is unreachable. Its rows are last-known sample values; the snapshot is incomplete. Dump all is blocked until every database is reachable. Export reachable databases individually or by selection.'
      : 'Compute 01 / owg_loadcoach has a binding mismatch: PostgreSQL is configured, but a legacy SQLite source was reported. Inspect the binding before export or cleanup. Other PostgreSQL and SQLite stores are valid.';
    $('export-all').disabled = !data.length || data.some(db => !available(db));
    document.querySelector('[data-all-export]').disabled = $('export-all').disabled;
    $('node-strip').replaceChildren(...nodes.map(node => {
      const button = document.createElement('button');
      button.className = `btn btn-sm ${$('node-filter').value === node.id ? 'btn-primary' : 'btn-outline-secondary'}`;
      button.textContent = `${node.label} · ${node.id === 'compute-04' && scenario === 'offline' ? 'unreachable' : `${data.filter(db => db.node === node.id).length} DB`}`;
      button.setAttribute('aria-pressed', $('node-filter').value === node.id);
      button.onclick = () => { $('node-filter').value = $('node-filter').value === node.id ? 'all' : node.id; render(); };
      return button;
    }));
    const body = $('databases');
    body.replaceChildren();
    let lastNode;
    filtered.forEach(db => {
      if (design === 'nodes' && lastNode !== db.node) {
        const group = clone('node-group');
        setText(group, '[data-group-label]', `${nodeName(db.node)} / ${nodes.find(node => node.id === db.node).role} · ${filtered.filter(item => item.node === db.node).length} databases`);
        body.append(group);
        lastNode = db.node;
      }
      const row = clone('database-row');
      row.dataset.database = db.id;
      setText(row, '[data-name]', db.name);
      setText(row, '[data-owner]', db.owner);
      setText(row, '[data-node]', nodeName(db.node));
      setText(row, '[data-engine]', mismatch(db) ? 'SQLite ≠ PostgreSQL' : db.engine);
      setText(row, '[data-tables]', db.tables.length);
      setText(row, '[data-records]', number(db.tables.reduce((sum, table) => sum + table.records, 0)));
      setText(row, '[data-size]', size(db.mb));
      setText(row, '[data-health]', offline(db) ? 'Unreachable · cached' : mismatch(db) ? 'Binding mismatch' : '● Reachable');
      row.querySelector('[data-health]').className = available(db) ? 'text-success' : 'text-danger';
      setText(row, '[data-backup]', db.backup);
      const check = row.querySelector('[data-select]');
      check.checked = selected.has(db.id);
      check.disabled = !available(db);
      check.setAttribute('aria-label', `Select ${db.name} on ${nodeName(db.node)}`);
      check.onchange = () => { check.checked ? selected.add(db.id) : selected.delete(db.id); render(); };
      const expand = row.querySelector('[data-expand]');
      expand.setAttribute('aria-expanded', expanded.has(db.id));
      expand.setAttribute('aria-controls', `detail-${db.id.replace('/', '-')}`);
      expand.setAttribute('aria-label', `Show tables for ${db.name} on ${nodeName(db.node)}`);
      expand.title = db.owner;
      setText(row, '[data-chevron]', expanded.has(db.id) ? '▾' : '▸');
      expand.onclick = () => { expanded.has(db.id) ? expanded.delete(db.id) : expanded.add(db.id); render();
        document.querySelector(`[data-database="${db.id}"] [data-expand]`).focus(); };
      for (const [selector, action] of [['[data-dump]', () => openExport([db])], ['[data-maintain]', () => openCleanup([db])]]) {
        const button = row.querySelector(selector);
        button.disabled = !available(db);
        button.onclick = action;
      }
      body.append(row);
      if (expanded.has(db.id)) body.append(detail(db));
    });
    if (!filtered.length) body.append(clone('empty-databases'));
    $('visible-count').textContent = `${filtered.length} of ${data.length} databases shown · counts & sizes are sample values`;
    const chosen = selectedDatabases();
    $('selection').textContent = chosen.length ? `${chosen.length} selected across all filters` : 'No databases selected';
    const blocked = !chosen.length || chosen.some(db => !available(db));
    for (const id of ['export-selected', 'purge-selected', 'reclaim-selected']) $(id).disabled = blocked;
    const selectable = filtered.filter(available);
    $('select-all').checked = !!selectable.length && selectable.every(db => selected.has(db.id));
    $('select-all').indeterminate = selectable.some(db => selected.has(db.id)) && !$('select-all').checked;
    $('select-all').disabled = !selectable.length;
    $('tools').replaceChildren(...nodes.map(node => {
      const row = document.createElement('tr');
      const engine = node.id.includes('cpu') ? 'SQLite' : 'PostgreSQL';
      for (const value of [nodeName(node.id), engine, node.id === 'compute-04' && scenario === 'offline' ? 'Unknown · unreachable' : engine === 'SQLite' ? 'Python sqlite3 fallback' : 'pg_dump · plain SQL', engine === 'SQLite' ? 'SQLite import' : 'psql / pg_restore available']) {
        const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
      }
      return row;
    }));
  }

  function detail(db) {
    const row = clone('database-detail');
    row.id = `detail-${db.id.replace('/', '-')}`;
    setText(row, '[data-detail-name]', `${db.name} / ${nodeName(db.node)}`);
    setText(row, '[data-detail-owner]', db.owner);
    setText(row, '[data-path]', db.path);
    setText(row, '[data-bindings]', `Bound services: ${db.kind === 'spotter' ? 'Spotter history' : db.node === 'front' ? 'WeightRoom / front repositories' : 'LoadCoach / Spotter identity'}`);
    setText(row, '[data-storage]', db.engine === 'SQLite'
      ? `Main file ${size(db.mb - 2)} · WAL + SHM 2.0 MiB · reusable pages ${size(db.reusable)} · SQLite 3.50 (sample)`
      : `Database ${size(db.mb)} · PostgreSQL 18 (sample) · free space requires analysis · pg_dump plain SQL`);
    const table = row.querySelector('[data-table-list]');
    table.setAttribute('aria-label', `Tables in ${db.name} on ${nodeName(db.node)}`);
    db.tables.forEach(item => {
      const tr = clone('table-row');
      setText(tr, '[data-table-name]', item.name);
      setText(tr, '[data-records]', number(item.records));
      setText(tr, '[data-size]', size(item.mb));
      setText(tr, '[data-indexes]', item.indexes);
      setText(tr, '[data-retention]', item.purge ? 'Eligible · readings.at' : 'Protected / policy required');
      tr.querySelector('[data-table-dump]').disabled = !available(db);
      tr.querySelector('[data-table-dump]').onclick = () => openExport([db], item);
      row.querySelector('[data-table-rows]').append(tr);
    });
    return row;
  }

  function openExport(scope, table = null) {
    if (!scope.length || scope.some(db => !available(db))) return;
    exportScope = scope; exportTable = table;
    $('export-summary').textContent = table ? `One table from ${scope[0].name} on ${nodeName(scope[0].node)}` : `${scope.length} database${scope.length === 1 ? '' : 's'} · ${scope.length === 1 ? 'SQL file' : 'one ZIP, one SQL file per database + manifest'}`;
    $('export-list').replaceChildren(...scope.map(db => {
      const li = document.createElement('li'); li.textContent = `${nodeName(db.node)} / ${db.name} · ${db.engine}${table ? ` / ${table.name}` : ''}`; return li;
    }));
    $('export-consistency').textContent = table
      ? 'A single-table dump may depend on other tables. Use the complete database export for recovery.'
      : 'Each database has its own consistent snapshot; a batch is not one simultaneous cluster-wide snapshot. This sample contains illustrative SQL only.';
    $('download-export').textContent = scope.length > 1 ? 'Download sample ZIP' : 'Download sample SQL';
    $('export-dialog').showModal();
  }

  function job(text) {
    const li = document.createElement('li'); li.className = 'py-2 border-top';
    li.textContent = `Simulated · ${text}`; $('jobs').prepend(li); $('status').textContent = li.textContent;
    $('jobs-card').open = true;
  }
  function sampleSQL(db, table) {
    const quote = value => `"${value.replaceAll('"', '""')}"`;
    return `-- DESIGN PREVIEW ONLY: illustrative SQL, not a production backup.\n-- ${nodeName(db.node)} / ${db.name} / ${db.engine}\nBEGIN;\n`
      + (table ? [table] : db.tables).map(item => `CREATE TABLE ${item.name.split('.').map(quote).join('.')} (preview_id INTEGER, preview_note TEXT);\n`).join('') + 'COMMIT;\n';
  }
  function download(blob, name) {
    const url = URL.createObjectURL(blob), a = document.createElement('a');
    a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  // ZIP STORE records keep the standalone sample dependency-free, including file:// use.
  function zip(files) {
    const encoder = new TextEncoder(), parts = [], central = []; let offset = 0, centralSize = 0;
    const header = (length, signature) => { const bytes = new Uint8Array(length); new DataView(bytes.buffer).setUint32(0, signature, true); return bytes; };
    const put = (bytes, at, value, width = 4) => new DataView(bytes.buffer)[width === 2 ? 'setUint16' : 'setUint32'](at, value, true);
    for (const file of files) {
      const name = encoder.encode(file.name), data = encoder.encode(file.content);
      let crc = 0xffffffff;
      for (const byte of data) { crc ^= byte; for (let i = 0; i < 8; i++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0); }
      crc = (crc ^ 0xffffffff) >>> 0;
      const local = header(30, 0x04034b50); put(local, 4, 20, 2); put(local, 14, crc);
      put(local, 18, data.length); put(local, 22, data.length); put(local, 26, name.length, 2);
      parts.push(local, name, data);
      const entry = header(46, 0x02014b50); put(entry, 4, 20, 2); put(entry, 6, 20, 2); put(entry, 16, crc);
      put(entry, 20, data.length); put(entry, 24, data.length); put(entry, 28, name.length, 2); put(entry, 42, offset);
      central.push(entry, name); centralSize += entry.length + name.length; offset += local.length + name.length + data.length;
    }
    const end = header(22, 0x06054b50); put(end, 8, files.length, 2); put(end, 10, files.length, 2);
    put(end, 12, centralSize); put(end, 16, offset);
    return new Blob([...parts, ...central, end], {type: 'application/zip'});
  }
  $('download-export').onclick = () => {
    const files = exportScope.map(db => ({name: `${db.node}/${db.name}${exportTable ? '-' + exportTable.name : ''}.sql`, content: sampleSQL(db, exportTable)}));
    if (files.length === 1) download(new Blob([files[0].content], {type: 'application/sql'}), files[0].name.replaceAll('/', '-'));
    else {
      files.push({name: 'manifest.json', content: JSON.stringify({sample: true, consistency: 'per-database', databases: exportScope.map(db => ({node: db.node, database: db.name, engine: db.engine, state: 'sample-only'}))}, null, 2)});
      download(zip(files), 'owg-databases-SAMPLE.zip');
    }
    job(`${exportScope.length} database${exportScope.length === 1 ? '' : 's'} exported${exportTable ? ' (one table)' : ''}; sample download created.`);
    $('export-dialog').close();
  };

  function invalidatePurge() {
    purgePlan = []; $('purge-preview').hidden = true; $('confirm-purge').checked = false; $('apply-purge').disabled = true;
  }
  function openCleanup(scope) {
    if (!scope.length || scope.some(db => !available(db))) return;
    cleanupScope = scope; invalidatePurge();
    $('cleanup-scope').textContent = scope.map(db => `${nodeName(db.node)} / ${db.name}`).join(' · ');
    $('confirm-reclaim').checked = false; $('apply-reclaim').disabled = true; $('cleanup-result').textContent = '';
    const engines = new Set(scope.map(db => db.engine));
    $('reclaim-impact').textContent = [
      engines.has('PostgreSQL') ? 'PostgreSQL: routine VACUUM primarily makes space reusable. Returning disk to the OS can require VACUUM FULL, an exclusive lock and rewrite headroom.' : '',
      engines.has('SQLite') ? 'SQLite: VACUUM compacts the file; writer coordination and up to twice the original database size in free space may be required. Checkpoint WAL when safe.' : '',
      'The real job must check locks, verified backup and free space first; bytes reclaimed are measured after completion.',
    ].filter(Boolean).join(' ');
    $('cleanup-dialog').showModal();
  }
  $('preview-purge').onclick = () => {
    const date = $('cutoff').value;
    if (!date || !$('cutoff').checkValidity()) { $('cutoff').reportValidity(); return; }
    purgePlan = cleanupScope.flatMap(db => db.tables.map(table => ({db, table,
      count: table.purge ? table.buckets.filter(bucket => bucket.date < date).reduce((sum, bucket) => sum + bucket.count, 0) : 0})));
    $('purge-rows').replaceChildren(...purgePlan.map(item => {
      const tr = document.createElement('tr');
      for (const value of [`${nodeName(item.db.node)} / ${item.db.name} / ${item.table.name}`, number(item.count), item.table.purge ? 'Telemetry history · at < cutoff' : 'Excluded · application retention policy required']) {
        const td = document.createElement('td'); td.textContent = value; tr.append(td);
      }
      return tr;
    }));
    $('purge-impact').textContent = `${number(purgePlan.reduce((sum, item) => sum + item.count, 0))} sample records strictly before ${date} 00:00 UTC. No disk savings are promised by deletion. Only telemetry readings are eligible in this sample; event chains, active work and private content need their own policies.`;
    $('purge-preview').hidden = false; $('confirm-purge').checked = false; $('apply-purge').disabled = true;
  };
  $('confirm-purge').onchange = () => { $('apply-purge').disabled = !$('confirm-purge').checked || !purgePlan.some(item => item.count > 0); };
  $('cutoff').oninput = invalidatePurge;
  $('apply-purge').onclick = () => {
    if ($('apply-purge').disabled) return;
    const count = purgePlan.reduce((sum, item) => sum + item.count, 0);
    purgePlan.filter(item => item.count).forEach(({db, table, count: removed}) => {
      if (db.reusable !== null) db.reusable += table.mb * removed / table.records;
      table.records -= removed; table.buckets = table.buckets.filter(bucket => bucket.date >= $('cutoff').value);
      db.maintenanceDone = false;
    });
    invalidatePurge(); render();
    $('cleanup-result').textContent = `${number(count)} sample records deleted. Allocated database size is unchanged until disk reclamation.`;
    job(`${number(count)} sample records purged before ${$('cutoff').value}.`);
  };
  $('confirm-reclaim').onchange = () => { $('apply-reclaim').disabled = !$('confirm-reclaim').checked; };
  $('apply-reclaim').onclick = () => {
    if ($('apply-reclaim').disabled) return;
    let reclaimed = 0;
    cleanupScope.forEach(db => {
      const freed = db.maintenanceDone ? 0 : db.reusable === null ? db.mb * .08 : db.reusable;
      const tableScale = (db.mb - freed - 20) / (db.mb - 20);
      db.tables.forEach(table => { table.mb *= tableScale; });
      db.mb -= freed; reclaimed += freed; db.maintenanceDone = true;
      if (db.reusable !== null) db.reusable = 0;
    });
    $('confirm-reclaim').checked = false; $('apply-reclaim').disabled = true; invalidatePurge(); render();
    $('cleanup-result').textContent = `Simulated completed maintenance: ${size(reclaimed)} returned to disk (illustrative measurement).`;
    job(`${size(reclaimed)} disk space reclaimed across ${cleanupScope.length} sample databases.`);
  };
  $('cleanup-form').onsubmit = event => event.preventDefault();

  for (const node of nodes) { const option = document.createElement('option'); option.value = node.id; option.textContent = node.label; $('node-filter').append(option); }
  document.querySelectorAll('[data-design]').forEach(button => { button.onclick = () => { design = button.dataset.design; render(); }; });
  for (const id of ['search', 'node-filter', 'engine-filter']) $(id).addEventListener(id === 'search' ? 'input' : 'change', render);
  $('scenario').onchange = () => { scenario = $('scenario').value; selected.clear(); expanded.clear(); render(); };
  $('select-all').onchange = () => { visible().filter(available).forEach(db => $('select-all').checked ? selected.add(db.id) : selected.delete(db.id)); render(); };
  $('expand-all').onclick = () => { allExpanded = !allExpanded; visible().forEach(db => allExpanded ? expanded.add(db.id) : expanded.delete(db.id)); $('expand-all').textContent = allExpanded ? 'Collapse visible' : 'Expand visible'; render(); };
  $('export-all').onclick = () => openExport(active());
  document.querySelector('[data-all-export]').onclick = () => openExport(active());
  $('export-selected').onclick = () => openExport(selectedDatabases());
  $('purge-selected').onclick = $('reclaim-selected').onclick = () => openCleanup(selectedDatabases());
  $('refresh').onclick = () => { $('snapshot-time').textContent = 'Sample snapshot refreshed · ' + new Date().toLocaleTimeString('en-GB', {timeZone: 'UTC'}) + ' UTC'; $('status').textContent = 'Sample snapshot refreshed. No live database was queried.'; render(); };
  document.querySelectorAll('[data-close]').forEach(button => { button.onclick = () => button.closest('dialog').close(); });
  $('theme').onclick = () => {
    const mode = document.querySelector('[data-theme-form] select[name="mode"]');
    mode.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    mode.dispatchEvent(new Event('change', {bubbles: true}));
    $('theme').textContent = mode.value === 'dark' ? 'Light mode' : 'Dark mode';
  };
  $('reset').onclick = () => {
    databases = seed(); selected.clear(); expanded.clear(); scenario = 'healthy'; allExpanded = false;
    $('scenario').value = 'healthy'; $('search').value = ''; $('node-filter').value = $('engine-filter').value = 'all';
    $('cutoff').value = '2026-10-01'; $('expand-all').textContent = 'Expand visible';
    $('jobs').replaceChildren(); $('status').textContent = 'Sample data reset.'; render();
  };
  databases = seed(); render();
})();
