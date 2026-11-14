// Enhance the existing journal and local-model form; fetches still use /output and /ask.
(() => {
  'use strict';
  if (window.SystemLogExplorer) { window.SystemLogExplorer.init(); return; }
  const remembered = new Map();
  const pageSize = 20;
  let capture = () => {};
  function init() {
    const root = document.getElementById('system-log-explorer');
    if (!root || root.dataset.ready) return;
    root.dataset.ready = 'true';
    const $ = id => root.querySelector(`#${id}`);
    // The enhanced results bar already labels the browser's timezone once.
    $('printed').caption?.remove();
    const entries = JSON.parse($('journal-data').textContent).entries;
    const key = JSON.stringify([document.documentElement.dataset.themeUser, root.dataset.node, root.dataset.source]);
    const state = remembered.get(key) || {search:'', level:'all', order:'newest', wrap:true, page:0, selected:null};
    let filtered = [], page = state.page, selected = state.selected;
    const clock = new Intl.DateTimeFormat(undefined, {hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
    const fullTime = new Intl.DateTimeFormat(undefined, {year:'numeric', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit', second:'2-digit', timeZoneName:'short'});
    const clockText = row => row.epoch === null ? '—' : clock.format(new Date(row.epoch * 1000));
    const levelText = level => level[0].toUpperCase() + level.slice(1);
    const words = () => $('search').value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    const selectedEntry = () => entries.find(row => row.id === selected);
    const tell = message => { $('journal-feedback').hidden = false; $('journal-feedback').textContent = message; };
    const setLevel = (element, value) => { element.textContent = levelText(value); element.dataset.level = value; };
    const scroll = () => $('journal-scroll');
    const localInput = epoch => {
      const parts = new Intl.DateTimeFormat('en-CA', {year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(new Date(epoch * 1000));
      const values = Object.fromEntries(parts.map(part => [part.type,part.value]));
      return `${values.year}-${values.month}-${values.day}T${values.hour}:${values.minute}:${values.second}`;
    };
    function save() {
      if (!root.isConnected) return;
      Object.assign(state, {search:$('search').value, level:$('level').value, order:$('sort').value,
        wrap:$('wrap').checked, page, selected, top:scroll().scrollTop,
        question:$('words')?.value || '', model:$('ask-model')?.value || '',
        selection:root.querySelector('[data-log-selection]')?.value || ''});
      remembered.delete(key); remembered.set(key,state);
      if (remembered.size > 8) remembered.delete(remembered.keys().next().value);
    }
    capture = save;
    function highlight(element, value) {
      const terms = words();
      if (!terms.length) { element.textContent = value; return; }
      const expression = new RegExp(terms.map(word => word.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')).join('|'),'gi');
      let previous = 0;
      for (const match of value.matchAll(expression)) {
        element.append(document.createTextNode(value.slice(previous,match.index)));
        const mark = document.createElement('mark'); mark.textContent = match[0]; element.append(mark);
        previous = match.index + match[0].length;
      }
      element.append(document.createTextNode(value.slice(previous)));
    }
    function details() {
      const row = selectedEntry();
      $('detail-content').hidden = !row; $('detail-empty').hidden = !!row;
      if (!row) return;
      setLevel($('detail-level'),row.level);
      $('detail-id').textContent = `Line ${row.index + 1}`;
      $('detail-message').textContent = row.message;
      $('detail-at').textContent = row.epoch === null ? 'Not recorded' : fullTime.format(new Date(row.epoch * 1000));
      $('detail-node').textContent = row.node; $('detail-source').textContent = row.source;
      $('detail-raw').textContent = row.raw;
      $('around-entry').disabled = row.epoch === null || root.dataset.offline === 'true';
      const chronological = [...entries].sort((a,b) => {
        if (a.epoch !== null && b.epoch !== null) return a.epoch - b.epoch || a.index - b.index;
        if (a.epoch === null && b.epoch === null) return a.index - b.index;
        return a.epoch === null ? 1 : -1;
      });
      const position = chronological.findIndex(entry => entry.id === row.id);
      $('entry-context').replaceChildren();
      chronological.slice(Math.max(0,position - 2), position + 3).forEach(entry => {
        const button = $('log-context-template').content.firstElementChild.cloneNode(true);
        button.dataset.id = entry.id; button.setAttribute('aria-current',String(entry.id === selected));
        button.querySelector('[data-field="time"]').textContent = clockText(entry);
        button.querySelector('[data-field="message"]').textContent = entry.message;
        $('entry-context').append(button);
      });
    }
    function draw() {
      const body = $('printed').tBodies[0]; body.replaceChildren();
      filtered.slice(page * pageSize,(page + 1) * pageSize).forEach(row => {
        const button = $('log-entry-template').content.firstElementChild.cloneNode(true);
        button.dataset.id = row.id; button.setAttribute('aria-pressed',String(row.id === selected));
        const field = name => button.querySelector(`[data-field="${name}"]`);
        field('number').textContent = row.index + 1;
        field('time').textContent = clockText(row);
        if (row.at) field('time').dateTime = row.at;
        setLevel(field('level'),row.level);
        field('node').textContent = row.node; field('source').textContent = row.source;
        highlight(field('message'),row.message);
        button.setAttribute('aria-label',`${clockText(row)}, ${levelText(row.level)}, ${row.source} on ${row.node}: ${row.message}`);
        const tr = document.createElement('tr'), td = document.createElement('td');
        tr.dataset.filterText = row.raw; td.append(button); tr.append(td); body.append(tr);
      });
      $('empty-state').hidden = !!filtered.length;
      $('empty-title').textContent = entries.length ? 'No matching entries' : root.dataset.offline === 'true' ? 'No saved journal entries' : 'No journal entries';
      $('empty-description').textContent = entries.length ? 'Try another phrase or choose a different level.' : 'Choose another source or time window.';
      $('empty-reset').hidden = !entries.length;
      $('visible-count').textContent = (filtered.length === entries.length ? `${entries.length} entries` : `${filtered.length} / ${entries.length} entries`) + ` · ${Intl.DateTimeFormat().resolvedOptions().timeZone}`;
      $('page-summary').textContent = filtered.length ? `${page * pageSize + 1}–${Math.min((page + 1) * pageSize,filtered.length)} of ${filtered.length}` : '0 entries';
      $('previous').disabled = page === 0; $('next').disabled = (page + 1) * pageSize >= filtered.length;
      $('export').disabled = !filtered.length;
      details(); save();
    }
    function render(resetPage = true) {
      const terms = words(), level = $('level').value;
      filtered = entries.filter(row => (level === 'all' || (level === 'attention' ? ['error','warning'].includes(row.level) : row.level === level)) && terms.every(word => row.raw.toLocaleLowerCase().includes(word)));
      const direction = $('sort').value === 'newest' ? -1 : 1;
      filtered.sort((a,b) => {
        if (a.epoch === null || b.epoch === null) return a.epoch === b.epoch ? direction * (a.index - b.index) : a.epoch === null ? 1 : -1;
        return direction * (a.epoch - b.epoch || a.index - b.index);
      });
      if (resetPage) page = 0;
      page = Math.max(0,Math.min(page,Math.ceil(filtered.length / pageSize) - 1));
      if (!filtered.some(row => row.id === selected)) selected = filtered[page * pageSize]?.id || null;
      root.dataset.wrap = String($('wrap').checked);
      scroll().scrollTop = 0; draw();
    }
    function select(id, focus = false) {
      const index = filtered.findIndex(row => row.id === id); if (index < 0) return;
      const top = scroll().scrollTop;
      selected = id; page = Math.floor(index / pageSize); draw(); scroll().scrollTop = top;
      if (focus) $('printed').querySelector(`[data-id="${id}"]`)?.focus({preventScroll:true});
    }
    function useWindow(epoch, before = 60, after = 60) {
      if (root.dataset.offline === 'true') return;
      $('at').value = localInput(epoch); $('before').value = before; $('after').value = after;
      // console.js serializes this browser-local value in the existing server timezone.
      $('journal-filters').requestSubmit();
    }
    function chart() {
      const rows = entries.filter(row => row.epoch !== null).sort((a,b) => a.epoch - b.epoch);
      $('volume-chart').replaceChildren();
      if (!rows.length) { $('volume-chart').textContent = 'No timestamped output'; return; }
      const first = rows[0].epoch, last = rows.at(-1).epoch, span = Math.max(1,last - first + 1);
      const buckets = Array.from({length:24},() => []);
      rows.forEach(row => buckets[Math.min(23,Math.floor((row.epoch - first) / span * 24))].push(row));
      const max = Math.max(...buckets.map(bucket => bucket.length));
      buckets.forEach((bucket,index) => {
        const button = $('log-bucket-template').content.firstElementChild.cloneNode(true);
        const start = first + Math.ceil(index * span / 24), end = Math.max(start,first + Math.ceil((index + 1) * span / 24) - 1);
        button.dataset.start = start; button.dataset.end = end;
        button.disabled = !bucket.length || root.dataset.offline === 'true';
        const counts = {error:0, warning:0, other:0};
        bucket.forEach(row => counts[row.level === 'error' || row.level === 'warning' ? row.level : 'other']++);
        for (const [kind,count] of Object.entries(counts)) button.querySelector(`[data-bar="${kind}"]`).style.height = `${count / max * 38}px`;
        button.title = `${clockText({epoch:start})}–${clockText({epoch:end})}: ${bucket.length} entries, ${counts.error} errors, ${counts.warning} warnings`;
        button.setAttribute('aria-label',button.title); $('volume-chart').append(button);
      });
      $('chart-start').textContent = clockText(rows[0]); $('chart-end').textContent = clockText(rows.at(-1));
      $('time-range-note').textContent = `${clockText(rows[0])}–${clockText(rows.at(-1))}`;
      $('volume-chart').title = `Times in ${Intl.DateTimeFormat().resolvedOptions().timeZone}`;
    }
    $('search').value = state.search; $('level').value = state.level;
    $('sort').value = state.order; $('wrap').checked = state.wrap;
    if ($('words')) $('words').value = state.question || '';
    if ($('ask-model') && [...$('ask-model').options].some(option => option.value === state.model)) $('ask-model').value = state.model;
    const selection = root.querySelector('[data-log-selection]');
    if (selection && state.selection && entries.some(row => row.raw.includes(state.selection))) {
      selection.value = state.selection;
      root.querySelector('[data-log-selection-note]').textContent = `${selection.value.length.toLocaleString()} selected characters will be sent.`;
      root.querySelector('[data-whole-log]').hidden = false;
    }
    chart(); const initialTop = state.top || 0; render(false); scroll().scrollTop = initialTop;
    // Own this filter before sort.js handles the shared data-filter contract.
    $('search').addEventListener('input',event => { event.stopPropagation(); render(); });
    $('level').addEventListener('change',() => render()); $('sort').addEventListener('change',() => render());
    $('wrap').addEventListener('change',() => { root.dataset.wrap = String($('wrap').checked); save(); });
    for (const id of ['clear-search','empty-reset']) $(id).addEventListener('click',() => { $('search').value = ''; $('level').value = 'all'; render(); });
    $('clear-window').addEventListener('click',() => { $('at').value = ''; $('journal-filters').requestSubmit(); });
    $('printed').addEventListener('click',event => {
      if (getSelection()?.toString()) return;
      const button = event.target.closest('[data-entry]'); if (!button) return;
      select(button.dataset.id,true);
      if (matchMedia('(max-width: 900px)').matches) $('detail-panel').scrollIntoView({block:'start'});
    });
    $('printed').addEventListener('keydown',event => {
      if (!['ArrowDown','ArrowUp'].includes(event.key)) return;
      const button = event.target.closest('[data-entry]'); if (!button) return;
      event.preventDefault();
      const index = filtered.findIndex(row => row.id === button.dataset.id) + (event.key === 'ArrowDown' ? 1 : -1);
      if (filtered[index]) { select(filtered[index].id,true); $('printed').querySelector(`[data-id="${selected}"]`).scrollIntoView({block:'nearest'}); }
    });
    $('entry-context').addEventListener('click',event => {
      const button = event.target.closest('[data-context]'); if (!button) return;
      $('search').value = ''; $('level').value = 'all'; render(); select(button.dataset.id);
    });
    $('volume-chart').addEventListener('click',event => { const button = event.target.closest('[data-bucket]'); if (button) useWindow(Number(button.dataset.start),0,Number(button.dataset.end) - Number(button.dataset.start)); });
    $('around-entry').addEventListener('click',() => { const row = selectedEntry(); if (row && row.epoch !== null) useWindow(row.epoch); });
    for (const [id,delta] of [['previous',-1],['next',1]]) $(id).addEventListener('click',() => { page += delta; scroll().scrollTop = 0; selected = filtered[page * pageSize]?.id || null; draw(); });
    $('copy-entry').addEventListener('click',async () => {
      const row = selectedEntry(); if (!row) return;
      try { await navigator.clipboard.writeText(row.raw); tell('Original entry copied.'); }
      catch { const range = document.createRange(); range.selectNodeContents($('detail-raw')); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); tell('Original output selected. Press Ctrl+C or Command+C to copy.'); }
    });
    $('export').addEventListener('click',() => {
      const url = URL.createObjectURL(new Blob([filtered.map(row => row.raw).join('\n') + '\n'], {type:'text/plain;charset=utf-8'}));
      const link = document.createElement('a'); link.href = url; link.download = `system-logs-${root.dataset.node}-${root.dataset.source}.txt`.replace(/[^\w.-]/g,'_'); link.click();
      setTimeout(() => URL.revokeObjectURL(url),1000); tell(`Exported ${filtered.length} entries.`);
    });
    $('ask-entry').addEventListener('click',() => {
      const row = selectedEntry(), field = root.querySelector('[data-log-selection]'); if (!row || !field) return;
      getSelection()?.removeAllRanges(); field.value = row.raw;
      root.querySelector('[data-log-selection-note]').textContent = 'Selected entry will be sent.';
      root.querySelector('[data-whole-log]').hidden = false;
      $('analysis-panel').querySelector('details').open = true;
      $('words').focus(); save();
    });
    root.addEventListener('input',save); root.addEventListener('change',save);
  }
  document.addEventListener('htmx:beforeRequest',() => capture());
  document.addEventListener('htmx:beforeSwap',() => capture());
  document.addEventListener('htmx:afterSwap',init);
  window.SystemLogExplorer = {init};
  init();
})();
