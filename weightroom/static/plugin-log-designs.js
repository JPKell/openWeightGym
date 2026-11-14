/* Standalone proposals: retained metadata only, with no live service requests. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const source = JSON.parse($('#plugin-log-data').textContent);
  const descriptions = {
    desk: 'Scan every request, then inspect its routing and identifiers alongside the table. Recommended.',
    trail: 'Follow a conversation through its recorded requests. Sessions on the left; an ordered trail on the right.',
    ledger: 'Compare outcomes across plugin instances, then narrow the ledger to the route that needs attention.'
  };
  const dialog = $('#detail-dialog'), detail = $('#record-detail');
  const wide = matchMedia('(min-width:1051px)');
  const labels = {completed:'Completed', failed:'Failed', interrupted:'Interrupted'};
  let rows = source.rows, layout = 'desk', selected = null, session = null, page = 1, sort = 'at', direction = -1;
  function clone(id) { return document.getElementById(id).content.firstElementChild.cloneNode(true); }
  function put(node, selector, value) { node.querySelector(selector).textContent = value; }
  function stamp(row, options) { return new Intl.DateTimeFormat('en-GB', {timeZone:source.zone, ...options}).format(new Date(row.at * 1000)); }
  function date(row) { return stamp(row, {day:'numeric', month:'short', year:'numeric'}); }
  function time(row) { return stamp(row, {hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'}); }
  function state(node, value) { node.textContent = labels[value] || value; node.dataset.tone = value; }
  function announce(message) { $('#feedback').textContent = message; }
  function filtered(ignoreInstance = false) {
    const query = $('#search').value.trim().toLowerCase(), hours = Number($('#period').value);
    return rows.filter(row => (!query || Object.values(row).some(value => String(value ?? '').toLowerCase().includes(query))) &&
      (!hours || row.at >= source.now - hours * 3600) &&
      (!$('#user-filter').value || row.user === $('#user-filter').value) &&
      (!$('#outcome-filter').value || row.outcome === $('#outcome-filter').value) &&
      (ignoreInstance || !$('#instance-filter').value || row.instance === $('#instance-filter').value));
  }
  function sorted(list) {
    return [...list].sort((a,b) => direction * (sort === 'at' ? a.at - b.at : String(a[sort]).localeCompare(String(b[sort]))) || b.id - a.id);
  }
  function counts(list) { return Object.fromEntries(Object.keys(labels).map(value => [value, list.filter(row => row.outcome === value).length])); }
  function outcomes(list) { const c = counts(list); return `${c.completed} completed · ${c.failed} failed · ${c.interrupted} interrupted`; }
  function selectRecord(row, open = true) {
    selected = row.id;
    updateDetail();
    $$('[data-record-row], .pl-trail-record').forEach(node => node.dataset.selected = String(Number(node.dataset.id) === selected));
    if (open && (layout !== 'desk' || !wide.matches) && !dialog.open) dialog.showModal();
  }
  function updateDetail() {
    const row = rows.find(row => row.id === selected);
    detail.hidden = !row;
    if (!row) return;
    put(detail, '[data-record-number]', `Record #${row.id}`);
    state(detail.querySelector('[data-outcome]'), row.outcome);
    for (const node of detail.querySelectorAll('[data-fact]')) {
      node.textContent = node.dataset.fact === 'at' ? `${date(row)} · ${time(row)} · ${source.zone}` : row[node.dataset.fact] || 'Not recorded';
    }
    $('#follow-session').disabled = !row.session;
    $('#follow-session').title = row.session ? 'Show this session’s requests' : 'No session ID was retained';
  }
  function openButton(node, row) {
    const button = node.querySelector('[data-open]');
    button.setAttribute('aria-label', `Inspect record ${row.id}, ${labels[row.outcome]}, ${row.user}`);
    button.addEventListener('click', () => selectRecord(row));
    node.dataset.id = row.id; node.dataset.selected = String(selected === row.id);
  }
  function renderTable(list) {
    const size = Number($('#page-size').value), pages = Math.max(1, Math.ceil(list.length / size));
    page = Math.min(page, pages);
    const visible = sorted(list).slice((page-1)*size, page*size);
    $('#log-rows').replaceChildren();
    for (const row of visible) {
      const node = clone('log-row-template');
      put(node, '[data-at]', time(row)); node.querySelector('[data-at]').dateTime = new Date(row.at * 1000).toISOString();
      put(node, '[data-date]', date(row));
      for (const field of ['user','model','plugin','instance','profile']) put(node, `[data-${field}]`, row[field]);
      state(node.querySelector('[data-outcome]'), row.outcome); openButton(node, row); $('#log-rows').append(node);
    }
    if (!list.length) {
      const emptyRow = clone('log-empty-row');
      if (!rows.length) emptyRow.querySelector('td').textContent = 'No plugin requests recorded yet. Activity will appear after a request is retained.';
      $('#log-rows').append(emptyRow);
    }
    $('#page-summary').textContent = list.length ? `${(page-1)*size+1}–${Math.min(page*size,list.length)} of ${list.length} requests · Page ${page} of ${pages}` : '0 requests';
    $('#previous').disabled = page <= 1; $('#next').disabled = page >= pages;
    for (const th of $$('[data-sort-column]')) {
      const active = th.dataset.sortColumn === sort;
      if (active) th.setAttribute('aria-sort', direction === 1 ? 'ascending' : 'descending'); else th.removeAttribute('aria-sort');
      put(th, '[data-sort-mark]', active ? direction === 1 ? '↑' : '↓' : '↕');
    }
  }
  function renderSessions(list) {
    const groups = new Map();
    for (const row of [...list].sort((a,b) => b.at-a.at)) {
      // A missing session is its own ungrouped record, never a fabricated conversation.
      const key = row.session || `unrecorded-${row.id}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(row);
    }
    if (!groups.has(session)) session = groups.keys().next().value || null;
    $('#sessions').replaceChildren();
    for (const [key, group] of groups) {
      const node = clone('session-choice-template'), newest = group[0];
      put(node, '[data-session-name]', newest.session || 'No session ID');
      put(node, '[data-session-count]', group.length);
      put(node, '[data-session-meta]', `${newest.user} · ${date(newest)} · ${time(newest)}`);
      put(node, '[data-session-outcomes]', outcomes(group));
      node.setAttribute('aria-pressed', String(session === key));
      node.addEventListener('click', () => { session = key; selected = group[0].id; render(); });
      $('#sessions').append(node);
    }
    const group = groups.get(session) || [];
    $('#trail-title').textContent = group[0]?.session || (group.length ? 'Request without a session ID' : 'No matching sessions');
    $('#trail-subtitle').textContent = group.length ? `${group[0].user} · ${group[0].plugin} / ${group[0].instance} · ${group.length} matching requests · ${source.zone}` : 'Adjust your filters to find a retained conversation.';
    $('#trail-records').replaceChildren(); $('#trail-empty').replaceChildren();
    if (!group.length) { $('#sessions').append(empty()); $('#trail-empty').append(empty()); }
    const ascending = $('#trail-order').value === 'asc';
    for (const row of [...group].sort((a,b) => (ascending ? 1 : -1) * (a.at-b.at))) {
      const node = clone('trail-record-template');
      put(node, '[data-model]', row.model);
      put(node, '[data-trail-meta]', `${date(row)} · ${time(row)} · ${row.profile}`);
      put(node, '[data-request]', row.request || 'Request ID not recorded');
      state(node.querySelector('[data-outcome]'), row.outcome); openButton(node,row); $('#trail-records').append(node);
    }
  }
  function renderInstances() {
    $('#instances').replaceChildren();
    const base = filtered(true);
    const all = clone('instance-choice-template');
    fillInstance(all, '', base); $('#instances').append(all);
    for (const name of [...new Set(rows.map(row => row.instance))].sort()) {
      const node = clone('instance-choice-template'); fillInstance(node, name, base.filter(row => row.instance === name)); $('#instances').append(node);
    }
  }
  function fillInstance(node, name, list) {
    put(node, '[data-instance-name]', name || 'All instances');
    put(node, '[data-instance-count]', list.length);
    put(node, '[data-instance-meta]', name ? `hermes / ${name}` : 'All retained plugin routes');
    put(node, '[data-instance-outcomes]', outcomes(list));
    const c = counts(list);
    for (const value of Object.keys(labels)) node.querySelector(`[data-${value}-bar]`).style.flexGrow = c[value];
    node.setAttribute('aria-pressed', String($('#instance-filter').value === name));
    node.addEventListener('click', () => { $('#instance-filter').value = name; page=1; render(); });
  }
  function empty() {
    const node = clone('log-empty-template');
    if (rows.length) put(node, '[data-empty-message]', 'No records match these filters. Clear filters to start again.');
    return node;
  }
  function positionDetail() {
    const inline = layout === 'desk' && wide.matches;
    if (dialog.open) dialog.close();
    (inline ? $('#detail-slot') : $('#dialog-slot')).append(detail);
    $('#detail-slot').hidden = !inline || selected === null;
    $('#close-detail').hidden = inline;
  }
  function render() {
    const failed = $('#scenario').value === 'error', list = filtered();
    $('#load-error').hidden = !failed; $('#log-body').hidden = failed;
    $('#export').disabled = failed || !list.length;
    $('#filter-summary').textContent = failed ? 'Activity unavailable' : `${list.length} of ${rows.length} retained requests · ${list.length === rows.length ? 'All records' : 'Filters applied'}`;
    const c = counts(list);
    $('#total-count').textContent = failed ? '—' : list.length;
    for (const value of Object.keys(labels)) $(`#${value}-count`).textContent = failed ? '—' : c[value];
    if (!list.some(row => row.id === selected)) selected = sorted(list)[0]?.id ?? null;
    if (dialog.open && selected === null) dialog.close();
    renderTable(list); renderSessions(list); renderInstances(); updateDetail();
    $('#detail-slot').hidden = layout !== 'desk' || !wide.matches || selected === null;
    $('#table-panel').hidden = layout === 'trail'; $('#trail-panel').hidden = layout !== 'trail';
    $('#browse-panel').hidden = layout === 'desk'; $('#session-browser').hidden = layout !== 'trail'; $('#instance-browser').hidden = layout !== 'ledger';
  }
  function setLayout(value) {
    layout = value; $('#workspace').dataset.layout = value;
    for (const button of $$('[data-design]')) {
      const active = button.dataset.design === value; button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active));
    }
    $('#design-description').textContent = descriptions[value]; positionDetail(); render();
    if (location.hash.slice(1) !== value) location.hash = value;
  }
  function clearFilters() { $('#filters').reset(); page=1; session=null; render(); }
  function reset() {
    if (dialog.open) dialog.close();
    rows = $('#scenario').value === 'empty' ? [] : source.rows;
    selected=null; sort='at'; direction=-1; clearFilters(); announce('');
  }
  function download(text, filename, type) {
    const url = URL.createObjectURL(new Blob([text],{type})), link = document.createElement('a');
    link.href=url; link.download=filename; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
  }
  // Prefix spreadsheet formulas before CSV quoting, including values from future datasets.
  function csv(value) { let text=String(value ?? ''); if (/^[\s]*[=+@-]/.test(text)) text="'"+text; return '"'+text.replaceAll('"','""')+'"'; }
  $('#export').addEventListener('click', () => {
    const fields=['id','at','user','model','plugin','instance','profile','outcome','request','session'];
    const list=sorted(filtered());
    const text=[fields.join(','),...list.map(row => fields.map(field => csv(field === 'at' ? new Date(row.at*1000).toISOString() : row[field])).join(','))].join('\r\n');
    download(text,'plugin-logs-filtered.csv','text/csv;charset=utf-8'); announce(`Exported ${list.length} filtered requests across all pages.`);
  });
  $('#copy-record').addEventListener('click', async () => {
    const row=rows.find(row => row.id===selected); if (!row) return;
    try { await navigator.clipboard.writeText(JSON.stringify(row,null,2)); announce(`Copied record #${row.id}.`); }
    catch { download(JSON.stringify(row,null,2),'plugin-record.json','application/json'); announce('Clipboard unavailable. Downloaded the record JSON instead.'); }
  });
  $('#follow-session').addEventListener('click', () => { const row=rows.find(row=>row.id===selected); if (!row?.session) return; session=row.session; setLayout('trail'); announce(`Following ${session}. Current filters are retained.`); });
  $('#close-detail').addEventListener('click', () => dialog.close());
  $('#filters').addEventListener('submit', event => event.preventDefault());
  for (const input of $('#filters').querySelectorAll('input,select')) input.addEventListener(input.type==='search'?'input':'change',()=>{page=1;render();});
  $('#clear').addEventListener('click',clearFilters);
  $('#page-size').addEventListener('change',()=>{page=1;render();});
  $('#previous').addEventListener('click',()=>{page--;render();}); $('#next').addEventListener('click',()=>{page++;render();});
  $('#trail-order').addEventListener('change',render);
  for (const button of $$('[data-sort]')) button.addEventListener('click',()=>{direction=sort===button.dataset.sort?-direction:1;sort=button.dataset.sort;page=1;render();});
  for (const button of $$('[data-design]')) button.addEventListener('click',()=>setLayout(button.dataset.design));
  $('#scenario').addEventListener('change',reset);
  $('#reset').addEventListener('click',()=>{$('#scenario').value='normal';$('#page-size').value='10';$('#trail-order').value='asc';reset();});
  $('#retry').addEventListener('click',()=>{$('#scenario').value='normal';reset();announce('Sample activity loaded.');});
  function themeLabel() { $('#theme').textContent=document.documentElement.dataset.bsTheme==='dark'?'Light mode':'Dark mode'; }
  $('#theme').addEventListener('click',()=>{const input=$('[data-theme-form] [name="mode"]');input.value=document.documentElement.dataset.bsTheme==='dark'?'light':'dark';input.dispatchEvent(new Event('change',{bubbles:true}));themeLabel();});
  document.addEventListener('weightroom:theme',themeLabel);
  wide.addEventListener('change',positionDetail);
  window.addEventListener('hashchange',()=>{const next=location.hash.slice(1);if(Object.hasOwn(descriptions,next)&&next!==layout)setLayout(next);});
  for (const [field,target] of [['user','#user-filter'],['instance','#instance-filter']]) for (const value of [...new Set(source.rows.map(row=>row[field]))].sort()) {
    $(target).add(new Option(value,value));
  }
  reset();setLayout(Object.hasOwn(descriptions,location.hash.slice(1))?location.hash.slice(1):'desk');themeLabel();
})();
