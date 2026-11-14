(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('system-logs-data').textContent);
  const $ = id => document.getElementById(id);
  const descriptions = {
    explorer: 'Recommended · Scan a structured log, then inspect an entry without losing your place.',
    investigation: 'Follow a problem through time, with persistent filters and surrounding output beside the timeline.',
    console: 'Read dense program output with line numbers, wrapping controls and an inspector below the stream.',
  };
  const timeFormatter = new Intl.DateTimeFormat('en-GB', {timeZone:data.timezone, hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const wallFormatter = new Intl.DateTimeFormat('en-CA', {timeZone:data.timezone, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const offsetFormatter = new Intl.DateTimeFormat('en', {timeZone:data.timezone, timeZoneName:'longOffset'});
  const narrowViewport = matchMedia('(max-width: 600px)');
  function sizeFilters() { $('filters-panel').open = !narrowViewport.matches; }
  narrowViewport.addEventListener('change', sizeFilters);
  sizeFilters();
  let logs = structuredClone(data.logs), scope = [], filtered = [], selected = null, page = 0, applied = null;
  let snapshot = null, refreshCount = 0;
  const pageSize = 20;
  const time = row => timeFormatter.format(new Date(row.epoch * 1000));
  const offset = epoch => offsetFormatter.formatToParts(new Date(epoch * 1000)).find(part => part.type === 'timeZoneName').value.replace('GMT','UTC');
  const levelLabel = level => level === 'unclassified' ? 'Unclassified' : level[0].toUpperCase() + level.slice(1);
  const parts = epoch => Object.fromEntries(wallFormatter.formatToParts(new Date(epoch * 1000)).map(part => [part.type, part.value]));
  function localInput(epoch) {
    const p = parts(epoch);
    return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}`;
  }
  // Convert the chosen wall time using the displayed zone, independent of the browser's zone.
  function fromLocal(value) {
    const desired = Date.parse(`${value.length === 16 ? value + ':00' : value}Z`) / 1000;
    if (!Number.isFinite(desired)) return NaN;
    let epoch = desired;
    for (let i = 0; i < 3; i++) {
      const correction = desired - Date.parse(localInput(epoch) + 'Z') / 1000;
      if (!correction) break;
      epoch += correction;
    }
    return localInput(epoch) === (value.length === 16 ? value + ':00' : value) ? epoch : NaN;
  }
  function feedback(message) { $('feedback').textContent = message; }
  function queryKey() { return JSON.stringify([$('output-node').value, $('source').value, $('lines').value, applied]); }
  function journalCommand() {
    return `journalctl ${$('source').value === 'kernel' ? '-k' : '--unit=' + $('source').value} --lines=${$('lines').value}${applied ? ` --since=@${applied.start} --until=@${applied.end}` : ''} --no-pager`;
  }
  function setLevel(element, level) { element.textContent = levelLabel(level); element.dataset.level = level; }
  function tokens() { return $('search').value.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean); }
  function highlight(element, value) {
    element.replaceChildren();
    const words = tokens();
    if (!words.length) { element.textContent = value; return; }
    const expression = new RegExp(words.map(word => word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gi');
    let last = 0;
    for (const match of value.matchAll(expression)) {
      element.append(document.createTextNode(value.slice(last, match.index)));
      const mark = document.createElement('mark'); mark.textContent = match[0]; element.append(mark);
      last = match.index + match[0].length;
    }
    element.append(document.createTextNode(value.slice(last)));
  }
  function getScope() {
    const scenario = $('scenario').value;
    if (scenario === 'empty' || scenario === 'failure') return [];
    if (scenario === 'offline') return snapshot?.key === queryKey() ? snapshot.rows : [];
    return logs.filter(row => row.node === $('output-node').value && row.source === $('source').value &&
      (!applied || (row.epoch >= applied.start && row.epoch <= applied.end)))
      .sort((a,b) => b.epoch - a.epoch).slice(0, Number($('lines').value));
  }
  function drawChart() {
    $('volume-chart').replaceChildren();
    $('chart-start').textContent = ''; $('chart-end').textContent = '';
    if (!scope.length) return;
    const ordered = [...scope].sort((a,b) => a.epoch - b.epoch);
    const start = ordered[0].epoch, end = ordered.at(-1).epoch;
    const span = Math.max(1, end - start + 1), buckets = Array.from({length:24}, () => []);
    ordered.forEach(row => buckets[Math.min(23, Math.floor((row.epoch - start) / span * 24))].push(row));
    const max = Math.max(...buckets.map(rows => rows.length), 1);
    buckets.forEach((rows,index) => {
      const button = $('log-bucket-template').content.firstElementChild.cloneNode(true);
      const from = start + Math.ceil(index * span / 24), until = start + Math.ceil((index + 1) * span / 24) - 1;
      button.dataset.start = from; button.dataset.end = Math.max(from, until);
      button.disabled = !rows.length;
      const errors = rows.filter(row => row.level === 'error').length, warnings = rows.filter(row => row.level === 'warning').length;
      const label = `${time({epoch:from})}–${time({epoch:Math.max(from,until)})}: ${rows.length} entries, ${errors} errors, ${warnings} warnings`;
      button.title = label; button.setAttribute('aria-label', label);
      for (const [kind,count] of [['error', errors], ['warning', warnings], ['other', rows.length - errors - warnings]])
        button.querySelector(`[data-bar="${kind}"]`).style.height = `${count / max * 38}px`;
      $('volume-chart').append(button);
    });
    $('chart-start').textContent = time(ordered[0]); $('chart-end').textContent = time(ordered.at(-1));
  }
  function drawEntries() {
    const list = $('printed'); list.replaceChildren();
    const shown = filtered.slice(page * pageSize, (page + 1) * pageSize);
    shown.forEach((row,index) => {
      const button = $('log-entry-template').content.firstElementChild.cloneNode(true);
      button.dataset.id = row.id; button.setAttribute('aria-pressed', String(row.id === selected));
      button.setAttribute('aria-label', `${time(row)}, ${row.level}, ${row.source} on ${row.node}: ${row.message}`);
      const field = name => button.querySelector(`[data-field="${name}"]`);
      field('number').textContent = page * pageSize + index + 1;
      field('time').textContent = time(row); field('time').dateTime = row.at;
      setLevel(field('level'), row.level);
      field('source').textContent = row.source; field('node').textContent = row.node;
      const body = $('workspace').dataset.layout === 'console' ? row.message + (row.raw.includes('\n') ? row.raw.slice(row.raw.indexOf('\n')) : '') : row.message;
      highlight(field('message'), body);
      if ($('workspace').dataset.layout === 'console') field('message').style.whiteSpace = $('wrap').checked ? 'pre-wrap' : '';
      list.append(button);
    });
    $('empty-state').hidden = !!filtered.length;
    $('previous').disabled = page === 0;
    $('next').disabled = (page + 1) * pageSize >= filtered.length;
    $('page-summary').textContent = filtered.length ? `${page * pageSize + 1}–${Math.min((page + 1) * pageSize, filtered.length)} of ${filtered.length} entries` : '0 entries';
    $('visible-count').textContent = `${filtered.length} matching / ${scope.length} fetched`;
    $('export').disabled = !filtered.length;
    const noSnapshot = $('scenario').value === 'offline' && snapshot?.key !== queryKey();
    $('empty-title').textContent = $('scenario').value === 'failure' ? 'Journal unavailable' : noSnapshot ? 'No saved snapshot for these filters' : !scope.length ? 'No journal entries' : 'No matching entries';
    $('empty-description').textContent = $('scenario').value === 'failure' ? 'Retry the sample read to return to the journal.' : noSnapshot ? 'Restore the saved filters or switch to the Available preview state.' : !scope.length ? 'Choose another source or widen the time window.' : 'Try another phrase or choose a different level.';
    $('empty-reset').textContent = $('scenario').value === 'failure' ? 'Retry sample' : noSnapshot ? 'Restore saved filters' : 'Reset filters';
    drawDetails();
  }
  function drawDetails() {
    const row = filtered.find(entry => entry.id === selected);
    $('detail-empty').hidden = !!row; $('detail-content').hidden = !row;
    if (!row) { $('entry-context').replaceChildren(); return; }
    setLevel($('detail-level'), row.level);
    $('detail-id').textContent = `#${row.id}`; $('detail-message').textContent = row.message;
    $('detail-at').textContent = `${localInput(row.epoch).replace('T', ' ')} · ${offset(row.epoch)}`;
    $('detail-node').textContent = row.node; $('detail-source').textContent = row.source;
    $('detail-raw').textContent = row.raw;
    const ordered = [...scope].sort((a,b) => a.epoch - b.epoch), index = ordered.findIndex(entry => entry.id === row.id);
    $('entry-context').replaceChildren();
    ordered.slice(Math.max(0,index - 2),index + 3).forEach(entry => {
      const button = $('log-context-template').content.firstElementChild.cloneNode(true);
      button.dataset.id = entry.id;
      button.setAttribute('aria-current', String(entry.id === selected));
      button.querySelector('[data-field="time"]').textContent = time(entry);
      button.querySelector('[data-field="message"]').textContent = entry.message;
      $('entry-context').append(button);
    });
  }
  function render(resetPage = true) {
    scope = getScope();
    const words = tokens(), level = $('level').value;
    filtered = scope.filter(row => (level === 'all' || (level === 'attention' ? ['error','warning'].includes(row.level) : row.level === level)) && words.every(word => row.raw.toLocaleLowerCase().includes(word)));
    filtered.sort((a,b) => $('sort').value === 'newest' ? b.epoch - a.epoch : a.epoch - b.epoch);
    if (resetPage) page = 0;
    page = Math.max(0, Math.min(page, Math.ceil(filtered.length / pageSize) - 1));
    if (!filtered.some(row => row.id === selected)) selected = filtered[0]?.id ?? null;
    $('total-count').textContent = scope.length;
    $('error-count').textContent = scope.filter(row => row.level === 'error').length;
    $('warning-count').textContent = scope.filter(row => row.level === 'warning').length;
    const ordered = [...scope].sort((a,b) => a.epoch - b.epoch);
    const seconds = scope.length ? ordered.at(-1).epoch - ordered[0].epoch : 0;
    $('time-range').textContent = scope.length ? seconds < 60 ? `${seconds} s` : `${Math.floor(seconds / 60)} min` : '—';
    $('time-range-note').textContent = scope.length ? `${time(ordered[0])}–${time(ordered.at(-1))}` : 'Fetched journal · local time';
    const scenario = $('scenario').value;
    $('offline-notice').hidden = scenario !== 'offline'; $('failure-notice').hidden = scenario !== 'failure';
    $('offline-notice').querySelector('strong').textContent = `${$('output-node').value} is unavailable.`;
    const fetchedTime = time({epoch:Date.parse(data.now) / 1000 + refreshCount * 5});
    $('offline-message').textContent = snapshot?.key === queryKey() ? `Showing the saved sample snapshot from ${fetchedTime}. These lines may be out of date.` : 'No snapshot is saved for this exact node, source and time window.';
    $('refresh').disabled = scenario === 'offline';
    $('refresh').textContent = scenario === 'failure' ? 'Retry sample' : 'Refresh sample';
    $('snapshot-label').textContent = `${scenario === 'offline' ? 'Saved sample' : 'Sample snapshot'} · 13 Nov 2026, ${fetchedTime}`;
    $('snapshot-zone').textContent = `${data.timezone} · ${offset(Date.parse(data.now) / 1000)}`;
    $('workspace').querySelector('.sl-status-dot').style.background = scenario === 'normal' ? 'var(--tblr-green)' : 'var(--tblr-orange)';
    $('command-label').textContent = `Command on ${$('output-node').value}`;
    $('journal-command').textContent = journalCommand();
    drawChart(); drawEntries();
    document.querySelector('#analysis-panel textarea[name="log"]').value = filtered.map(row => row.raw).join('\n');
    $('analysis-panel').querySelector('form').querySelector('button[type="submit"], button:not([type])').disabled = !filtered.length || scenario === 'offline';
    $('printed').scrollTop = 0;
  }
  function selectEntry(id, focus = false) {
    selected = id;
    const index = filtered.findIndex(row => row.id === id);
    if (index < 0) return;
    page = Math.floor(index / pageSize); drawEntries();
    if (focus) $('printed').querySelector(`[data-id="${id}"]`)?.focus({preventScroll:true});
  }
  function clearWindow() { applied = null; $('at').value = ''; $('before').value = '60'; $('after').value = '15'; $('filter-error').hidden = true; }
  function clearSearch() { $('search').value = ''; $('level').value = 'all'; render(); }
  function applyWindow() {
    if (!$('journal-filters').reportValidity()) return;
    const before = Number($('before').value), after = Number($('after').value), at = $('at').value ? fromLocal($('at').value) : null;
    if (![before,after].every(value => Number.isFinite(value) && value >= 0) || (at !== null && !Number.isFinite(at))) {
      $('filter-error').textContent = 'Choose a valid local time and nonnegative seconds before and after.'; $('filter-error').hidden = false; return;
    }
    applied = at === null ? null : {start:at - before, end:at + after};
    $('filter-error').hidden = true; render(); feedback(applied ? 'Time window applied in America/Vancouver.' : 'Showing the latest sample output.');
  }
  function useWindow(epoch, before = 60, after = 60) {
    $('at').value = localInput(epoch); $('before').value = before; $('after').value = after;
    applyWindow();
  }
  function reset() {
    logs = structuredClone(data.logs); refreshCount = 0; snapshot = null; selected = null;
    $('scenario').value = 'normal'; $('journal-filters').reset(); clearWindow();
    $('search').value = ''; $('level').value = 'all'; $('sort').value = 'newest';
    $('wrap').checked = true; $('workspace').dataset.wrap = 'true';
    $('answer').replaceChildren(); document.querySelector('#analysis-panel textarea[name="selection"]').value = '';
    $('words').value = ''; $('analysis-panel').querySelector('details').open = false;
    render(); feedback('Interactive sample · no services or models are called.');
  }
  document.querySelectorAll('[data-design]').forEach(button => button.addEventListener('click', () => {
    $('workspace').dataset.layout = button.dataset.design;
    document.querySelectorAll('[data-design]').forEach(choice => { const active = choice === button; choice.classList.toggle('active', active); choice.setAttribute('aria-pressed', String(active)); });
    $('design-description').textContent = descriptions[button.dataset.design]; drawEntries();
  }));
  $('journal-filters').addEventListener('submit', event => { event.preventDefault(); applyWindow(); });
  ['output-node','source','lines'].forEach(id => $(id).addEventListener('change', () => { render(); feedback('Sample journal updated.'); }));
  $('search').addEventListener('input', () => render()); $('level').addEventListener('change', () => render());
  $('sort').addEventListener('change', () => render());
  $('clear-window').addEventListener('click', () => { clearWindow(); render(); });
  $('clear-search').addEventListener('click', clearSearch);
  $('wrap').addEventListener('change', () => { $('workspace').dataset.wrap = String($('wrap').checked); drawEntries(); });
  $('printed').addEventListener('click', event => { const button = event.target.closest('[data-entry]'); if (button) { selectEntry(button.dataset.id, true); if (matchMedia('(max-width: 900px)').matches) $('detail-panel').scrollIntoView({block:'start'}); } });
  $('printed').addEventListener('keydown', event => {
    if (!['ArrowDown','ArrowUp'].includes(event.key)) return;
    const row = event.target.closest('[data-entry]'); if (!row) return;
    event.preventDefault(); const index = filtered.findIndex(entry => entry.id === row.dataset.id) + (event.key === 'ArrowDown' ? 1 : -1);
    if (filtered[index]) { selectEntry(filtered[index].id, true); $('printed').querySelector(`[data-id="${selected}"]`).scrollIntoView({block:'nearest'}); }
  });
  $('entry-context').addEventListener('click', event => {
    const button = event.target.closest('[data-context]'); if (!button) return;
    $('search').value = ''; $('level').value = 'all'; render(); selectEntry(button.dataset.id);
  });
  $('volume-chart').addEventListener('click', event => {
    const button = event.target.closest('[data-bucket]'); if (!button) return;
    const start = Number(button.dataset.start), end = Number(button.dataset.end);
    useWindow(start,0,end - start); feedback('Selected chart window applied. Clear the window to see the full journal.');
  });
  $('around-entry').addEventListener('click', () => { const row = filtered.find(entry => entry.id === selected); if (row) { $('search').value = ''; $('level').value = 'all'; useWindow(row.epoch); } });
  for (const [id,delta] of [['previous',-1], ['next',1]]) $(id).addEventListener('click', () => { page += delta; drawEntries(); $('printed').scrollTop = 0; });
  document.querySelectorAll('[data-quick]').forEach(button => button.addEventListener('click', () => { $('level').value = button.dataset.quick; $('search').value = ''; render(); }));
  $('scenario').addEventListener('change', () => {
    if ($('scenario').value === 'offline') snapshot = {key:queryKey(), rows:structuredClone(scope), node:$('output-node').value, source:$('source').value, lines:$('lines').value, applied:structuredClone(applied)};
    render(); feedback('Preview state changed. All data and actions remain local to this page.');
  });
  $('empty-reset').addEventListener('click', () => {
    if ($('scenario').value === 'failure') { $('scenario').value = 'normal'; render(); return; }
    if ($('scenario').value === 'offline' && snapshot) {
      $('output-node').value = snapshot.node; $('source').value = snapshot.source; $('lines').value = snapshot.lines; applied = structuredClone(snapshot.applied);
      $('at').value = applied ? localInput(applied.start) : ''; $('before').value = 0; $('after').value = applied ? applied.end - applied.start : 15;
    } else clearWindow();
    clearSearch();
  });
  function refresh() {
    if ($('scenario').value === 'failure') { $('scenario').value = 'normal'; render(); feedback('Sample journal read succeeded.'); return; }
    if ($('scenario').value === 'empty') { feedback('Sample journal checked. No output has been recorded.'); return; }
    if ($('scenario').value === 'offline') return;
    refreshCount++;
    const epoch = Date.parse(data.now) / 1000 + refreshCount * 5, at = new Date(epoch * 1000).toISOString();
    const node = $('output-node').value, source = $('source').value, message = `Sample refresh ${refreshCount}: service health check passed`;
    logs.push({id:`refresh-${refreshCount}`, at, epoch, node, source, level:'info', message, raw:`${at} ${node} ${source}[4210]: INFO ${message}`});
    render(); feedback(applied ? 'A new sample entry was added; the time window remains applied.' : 'One new sample entry loaded. Search, selection and filters were retained.');
  }
  $('refresh').addEventListener('click', refresh); $('retry').addEventListener('click', refresh);
  $('copy-entry').addEventListener('click', async () => {
    const raw = filtered.find(entry => entry.id === selected)?.raw; if (!raw) return;
    try { await navigator.clipboard.writeText(raw); feedback('Original entry copied.'); }
    catch { const selection = getSelection(), range = document.createRange(); range.selectNodeContents($('detail-raw')); selection.removeAllRanges(); selection.addRange(range); feedback('Clipboard unavailable. Original output selected; press Ctrl+C or Command+C to copy.'); }
  });
  $('export').addEventListener('click', () => {
    const blob = new Blob([filtered.map(row => row.raw).join('\n') + '\n'], {type:'text/plain;charset=utf-8'}), url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = `system-logs-${$('output-node').value}-${$('source').value}.txt`; a.click();
    setTimeout(() => URL.revokeObjectURL(url),1000); feedback(`Exported ${filtered.length} matching entries in the displayed order.`);
  });
  $('ask-entry').addEventListener('click', () => {
    const row = filtered.find(entry => entry.id === selected); if (!row) return;
    $('analysis-panel').querySelector('details').open = true;
    document.querySelector('#analysis-panel textarea[name="selection"]').value = row.raw;
    document.querySelector('[data-log-selection-note]').textContent = `Selected entry #${row.id} will be used for the sample answer.`;
    document.querySelector('[data-whole-log]').hidden = false;
    $('analysis-panel').scrollIntoView({block:'start'}); $('words').focus({preventScroll:true});
  });
  document.querySelector('[data-whole-log]').addEventListener('click', () => { document.querySelector('#analysis-panel textarea[name="selection"]').value = ''; document.querySelector('[data-log-selection-note]').textContent = 'All matching entries will be used for the sample answer.'; document.querySelector('[data-whole-log]').hidden = true; });
  $('analysis-panel').querySelector('form').addEventListener('submit', event => {
    event.preventDefault();
    const selectedLog = document.querySelector('#analysis-panel textarea[name="selection"]').value;
    const count = selectedLog ? 1 : filtered.length;
    $('answer').textContent = `Illustrative answer · ${count} sample ${count === 1 ? 'entry' : 'entries'} · no model was called. Check the original output and surrounding entries to establish what happened. For a worker disconnect, inspect the worker journal at the same timestamp; for an allocation failure, compare available device memory with the requested allocation. This text is a preview, not a diagnosis.`;
    feedback('Illustrative answer displayed. No log data left this page.');
  });
  $('theme').addEventListener('click', () => {
    const select = document.querySelector('[data-theme-form] [name="mode"]'); select.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark'; select.dispatchEvent(new Event('change',{bubbles:true}));
    $('theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  });
  $('reset').addEventListener('click', reset);
  $('theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  reset();
})();
