// Portable sample archive: no application requests, writes, or external dependencies.
(() => {
  'use strict';
  const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
  const records = JSON.parse($('#sample-data').textContent);
  const layouts = {
    registry: 'Recommended · A calm, spacious archive paired with a complete record dossier.',
    ledger: 'For investigation · Persistent filters, compact rows, and settings beside file evidence.',
    explorer: 'For discovery · A table with a quick preview, paired with a document-style record.',
  };
  let layout = 'registry', view = 'directory', selected = records[0], page = 0, sort = 'first', ascending = false;
  let filtered = records, lastLink = null;
  const number = n => Number(n).toLocaleString('en-US');
  const size = n => n >= 1024**3 ? `${(n / 1024**3).toFixed(1)} GiB` : `${(n / 1024**2).toFixed(0)} MiB`;
  const shown = v => typeof v === 'object' ? JSON.stringify(v) : String(v);
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const tell = message => { $('#feedback').textContent = message; };
  function fact(container, name, value) {
    const row = clone('#fact-row');
    row.querySelector('dt').textContent = name;
    row.querySelector('dd').textContent = value;
    container.append(row);
  }
  function dates(record) {
    const date = new Date(record.first);
    return [date.toLocaleDateString('en-GB', {day:'2-digit', month:'short', year:'numeric'}),
      date.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit', timeZoneName:'short'})];
  }
  function route(nextView = view, record = selected) {
    const params = new URLSearchParams({design:layout, view:nextView});
    if (nextView === 'detail') params.set('id', record.fingerprint);
    if ($('#start').value) params.set('q', $('#start').value);
    if ($('#model-filter').value) params.set('model', $('#model-filter').value);
    if ($('#context-filter').value) params.set('context', $('#context-filter').value);
    return `#${params}`;
  }
  function saveRoute() { history.replaceState(null, '', route()); }
  function mosaic() {
    $$('[data-mosaic]').forEach(el => {
      el.replaceChildren();
      [...selected.fingerprint].forEach(char => {
        const cell = document.createElement('span');
        cell.dataset.lit = String(parseInt(char, 16) > 7);
        el.append(cell);
      });
    });
  }
  function peek() {
    $('#peek-hash').textContent = selected.fingerprint;
    $('#peek-model').textContent = selected.model;
    $('#peek-facts').replaceChildren();
    fact($('#peek-facts'), 'First seen', dates(selected)[0]);
    fact($('#peek-facts'), 'Context', `${number(selected.settings.flags['ctx-size'])} tokens`);
    fact($('#peek-facts'), 'Files', Object.keys(selected.files).length);
    const weights = $('#compact-file-record').content.cloneNode(true);
    fillFile(weights, selected.files.model, 'model');
    $('#peek-weights').replaceChildren(weights);
    mosaic();
  }
  function fillFile(card, file, role) {
    card.querySelector('[data-file-role]').textContent = role === 'model' ? 'Model weights' : 'Projector';
    card.querySelector('[data-file-size]').textContent = size(file.size);
    card.querySelector('[data-file-name]').textContent = file.path.split('/').pop();
    const facts = card.querySelector('[data-file-facts]');
    fact(facts, 'Recorded path', file.path);
    fact(facts, 'Exact size', `${number(file.size)} bytes`);
    Object.entries(file.says || {}).filter(([,value]) => value !== null).forEach(([name, value]) => fact(facts, name.replaceAll('_', ' '), shown(value)));
    card.querySelector('[data-file-hash]').textContent = file.sha256;
    card.querySelector('[data-copy-file]').addEventListener('click', () => copy(file.sha256, 'File hash copied.'));
  }
  function renderRows() {
    const query = $('#start').value.trim().toLowerCase();
    const model = $('#model-filter').value, context = $('#context-filter').value;
    filtered = records.filter(r => (!query || r.fingerprint.startsWith(query) || r.model.toLowerCase().includes(query) || r.files.model.path.toLowerCase().includes(query)) &&
      (!model || r.model === model) && (!context || r.settings.flags['ctx-size'] === Number(context)));
    const key = r => ({fingerprint:r.fingerprint, model:r.model, first:r.first, file:r.files.model.path, context:r.settings.flags['ctx-size']})[sort];
    filtered.sort((a, b) => typeof key(a) === 'number' ? (key(a) - key(b)) * (ascending ? 1 : -1) :
      String(key(a)).localeCompare(String(key(b)), undefined, {numeric:true}) * (ascending ? 1 : -1));
    const limit = Number($('#page-size').value);
    page = Math.min(page, Math.max(0, Math.ceil(filtered.length / limit) - 1));
    const tbody = $('#fingerprint-rows'), empty = $('#no-records');
    tbody.replaceChildren(empty);
    empty.hidden = filtered.length > 0;
    filtered.slice(page * limit, (page + 1) * limit).forEach(record => {
      const row = clone('#fingerprint-row'), file = record.files.model;
      const [day, time] = dates(record);
      const fields = {fingerprint:record.fingerprint.slice(0, 12), model:record.model, quantization:file.says.quantization,
        first:day, time, filename:file.path.split('/').pop(), size:size(file.size), context:number(record.settings.flags['ctx-size'])};
      Object.entries(fields).forEach(([name, value]) => { row.querySelector(`[data-field="${name}"]`).textContent = value; });
      row.querySelector('[data-field="first"]').setAttribute('data-utc', record.first);
      const link = row.querySelector('[data-open]');
      link.href = route('detail', record);
      link.setAttribute('aria-label', `Inspect fingerprint ${record.fingerprint.slice(0, 12)} for ${record.model}`);
      row.dataset.selected = String(record.fingerprint === selected.fingerprint);
      link.addEventListener('click', event => {
        if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); lastLink = record.fingerprint;
        selected = record;
        if (layout === 'explorer' && $('#record-peek').getBoundingClientRect().width) {
          peek(); renderRows();
          $$('#fingerprint-rows [data-open]').find(el => el.hash.includes(selected.fingerprint))?.focus({preventScroll:true});
          tell('Record selected. Open the complete record from At a glance.');
        }
        else setView('detail', true);
      });
      tbody.append(row);
    });
    $('#match-count').textContent = `${number(filtered.length)} of ${records.length} records`;
    $('#page-summary').textContent = filtered.length ? `Showing ${page * limit + 1}–${Math.min((page + 1) * limit, filtered.length)} of ${filtered.length}` : '0 results';
    $('#previous').disabled = page === 0;
    $('#next').disabled = (page + 1) * limit >= filtered.length;
    $('#export-directory').disabled = !filtered.length;
    $$('[data-sort]').forEach(button => {
      const active = button.dataset.sort === sort;
      button.closest('th').setAttribute('aria-sort', active ? (ascending ? 'ascending' : 'descending') : 'none');
      button.querySelector('span').textContent = active ? (ascending ? '↑' : '↓') : '↕';
    });
  }
  function detail() {
    $('#record-model').textContent = selected.model;
    $('#record-hash').textContent = selected.fingerprint;
    const index = records.indexOf(selected);
    $('#record-position').textContent = `Record ${index + 1} of ${records.length}`;
    $('#previous-record').disabled = index === 0;
    $('#next-record').disabled = index === records.length - 1;
    $('#identity-facts').replaceChildren();
    fact($('#identity-facts'), 'First seen', dates(selected).join(' · '));
    fact($('#identity-facts'), 'Context', `${number(selected.settings.flags['ctx-size'])} tokens`);
    fact($('#identity-facts'), 'Recorded files', `${Object.keys(selected.files).length} · ${selected.files.mmproj ? 'model + projector' : 'model weights'}`);
    $('#file-records').replaceChildren();
    Object.entries(selected.files).forEach(([role, file]) => {
      const card = clone('#file-record');
      fillFile(card, file, role);
      $('#file-records').append(card);
    });
    const settings = Object.entries(selected.settings).filter(([name]) => name !== 'flags')
      .concat(Object.entries(selected.settings.flags).filter(([name]) => !(name in selected.files)));
    $('#setting-rows').replaceChildren();
    settings.forEach(([name, value]) => {
      const row = clone('#setting-row');
      row.querySelector('th').textContent = name;
      row.querySelector('code').textContent = shown(value);
      $('#setting-rows').append(row);
    });
    filterSettings(); peek();
  }
  function filterSettings() {
    const query = $('#setting-search').value.toLowerCase().trim();
    const rows = $$('#setting-rows tr');
    rows.forEach(row => { row.hidden = !row.textContent.toLowerCase().includes(query); });
    $('#no-settings').hidden = rows.some(row => !row.hidden);
  }
  function setView(next, focus = false, persist = true) {
    view = next;
    $('#workspace').dataset.view = view;
    $('#directory-view').hidden = view !== 'directory';
    $('#detail-view').hidden = view !== 'detail';
    $$('button[data-view]').forEach(button => {
      const active = button.dataset.view === view;
      button.setAttribute('aria-pressed', String(active)); button.classList.toggle('active', active);
    });
    if (view === 'detail') detail(); else renderRows();
    if (persist) saveRoute();
    if (focus) {
      if (view === 'detail') $('#record-model').focus({preventScroll:true});
      else {
        const link = $$('#fingerprint-rows [data-open]').find(el => el.hash.includes(lastLink || selected.fingerprint));
        (link || $('#start')).focus({preventScroll:true});
      }
      window.scrollTo({top:0, behavior:'instant'});
    }
  }
  function setDesign(value, persist = true) {
    layout = value; $('#workspace').dataset.layout = layout;
    $('#design-description').textContent = layouts[layout];
    $$('[data-design]').forEach(button => {
      const active = button.dataset.design === layout;
      button.setAttribute('aria-pressed', String(active)); button.classList.toggle('active', active);
    });
    renderRows(); peek();
    if (persist) saveRoute();
  }
  async function copy(value, message) {
    try { await navigator.clipboard.writeText(value); tell(message); }
    catch {
      const field = document.createElement('textarea');
      field.value = value; field.setAttribute('aria-label', 'Text to copy'); document.body.append(field); field.select();
      const copied = document.execCommand('copy'); field.remove();
      tell(copied ? message : 'Copy is unavailable in this browser. Select and copy the value shown on the page.');
    }
  }
  function download(value, filename) {
    const blob = new Blob([JSON.stringify(value, null, 2)], {type:'application/json'});
    const url = URL.createObjectURL(blob), link = document.createElement('a');
    link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    tell(`Downloaded ${filename}.`);
  }
  function readRoute() {
    const params = new URLSearchParams(location.hash.slice(1));
    $('#start').value = params.get('q') || '';
    $('#model-filter').value = params.get('model') || '';
    $('#context-filter').value = params.get('context') || '';
    const record = records.find(r => r.fingerprint === params.get('id'));
    const missing = !!params.get('id') && !record;
    $('#missing-record').hidden = !missing;
    selected = record || selected;
    setDesign(params.get('design') in layouts ? params.get('design') : 'registry', false);
    setView(params.get('view') === 'detail' && !missing ? 'detail' : 'directory', false, false);
  }
  [...new Set(records.map(r => r.model))].sort().forEach(name => {
    const option = document.createElement('option'); option.value = name; option.textContent = name; $('#model-filter').append(option);
  });
  $('#total-count').textContent = records.length;
  $('#model-count').textContent = new Set(records.map(r => r.model)).size;
  $('#file-count').textContent = new Set(records.flatMap(r => Object.values(r.files).map(f => f.sha256))).size;
  $('#latest-seen').textContent = new Date(records[0].first).toLocaleDateString('en-GB', {day:'2-digit', month:'short'});
  $$('[data-design]').forEach(button => button.addEventListener('click', () => { tell(''); setDesign(button.dataset.design); }));
  $$('button[data-view]').forEach(button => button.addEventListener('click', () => setView(button.dataset.view, true)));
  $('#lookup-form').addEventListener('submit', event => { event.preventDefault(); page = 0; renderRows(); saveRoute(); });
  ['#start', '#model-filter', '#context-filter'].forEach(selector => $(selector).addEventListener(selector === '#start' ? 'input' : 'change', () => { page = 0; renderRows(); saveRoute(); }));
  $$('[data-clear]').forEach(button => button.addEventListener('click', () => {
    $('#lookup-form').reset(); page = 0; renderRows(); saveRoute(); $('#start').focus();
  }));
  $$('[data-sort]').forEach(button => button.addEventListener('click', () => {
    ascending = sort === button.dataset.sort ? !ascending : true;
    sort = button.dataset.sort; page = 0; renderRows();
  }));
  $('#page-size').addEventListener('change', () => { page = 0; renderRows(); });
  $('#previous').addEventListener('click', () => { page--; renderRows(); });
  $('#next').addEventListener('click', () => { page++; renderRows(); });
  $('#back').addEventListener('click', () => setView('directory', true));
  $('#brand').addEventListener('click', event => { event.preventDefault(); setView('directory', true); });
  $('#open-peek').addEventListener('click', () => setView('detail', true));
  ['previous', 'next'].forEach(direction => $(`#${direction}-record`).addEventListener('click', () => {
    selected = records[records.indexOf(selected) + (direction === 'next' ? 1 : -1)]; setView('detail', true);
  }));
  $('#setting-search').addEventListener('input', filterSettings);
  $('#copy-hash').addEventListener('click', () => copy(selected.fingerprint, 'Fingerprint copied.'));
  $('#copy-link').addEventListener('click', () => copy(`${location.href.split('#')[0]}${route('detail')}`, 'Record link copied.'));
  $('#export-record').addEventListener('click', () => download(selected, `fingerprint-${selected.fingerprint.slice(0, 12)}.json`));
  $('#export-directory').addEventListener('click', () => download(filtered, 'fingerprints.json'));
  $('#theme').addEventListener('click', () => {
    const choice = $('[data-theme-form] [name=mode]');
    choice.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    choice.dispatchEvent(new Event('change', {bubbles:true}));
    $('#theme').textContent = choice.value === 'dark' ? 'Light mode' : 'Dark mode';
  });
  $$('[data-anchor]').forEach(link => link.addEventListener('click', event => {
    event.preventDefault(); const target = $(`#${link.dataset.anchor}`);
    target.scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'});
    target.tabIndex = -1; target.focus({preventScroll:true});
  }));
  window.addEventListener('hashchange', readRoute);
  $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  readRoute();
})();
