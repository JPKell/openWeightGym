/* Standalone proposal client: all data and actions remain browser-local. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const source = JSON.parse($('#log-data').textContent);
  const fixedNow = Date.parse(source.now);
  const sampleOffset = source.now.slice(-6);
  const offsetMs = (sampleOffset[0] === '-' ? -1 : 1) * (Number(sampleOffset.slice(1,3))*60 + Number(sampleOffset.slice(4))) * 60000;
  const descriptions = {
    explorer: 'Recommended · Scan a clear event table, then inspect one record without losing your place.',
    incidents: 'Start with failure patterns, narrow by time or node, then investigate the underlying evidence.',
    traces: 'Follow an exact trace chronologically across nodes. Keep the event evidence beside the timeline.',
  };
  const labels = {q:'Search', range:'Time', node:'Node', model:'Model', kind:'Kind', what:'What happened',
    user:'User', group:'Group', trace:'Trace', start:'From', end:'Through', quick:'Quick view', code:'Error code'};
  const controls = {q:'#search', range:'#time-range', node:'#log-node', model:'#log-model', kind:'#log-kind',
    what:'#log-what', user:'#log-user', group:'#log-group', trace:'#log-trace', start:'#log-start', end:'#log-end'};
  const coreFields = new Set(['id','at','kind','what','user','caller','machine','model','trace','level']);
  let layout = 'explorer', quick = 'all', errorCode = '', selected = null, page = 0;
  let sort = 'at', ascending = false, matched = [], events = source.events, invalidRange = false;
  const timeFormat = new Intl.DateTimeFormat('en-CA', {timeZone:source.timezone, hour:'2-digit', minute:'2-digit', second:'2-digit', hourCycle:'h23'});
  const dateFormat = new Intl.DateTimeFormat('en-CA', {timeZone:source.timezone, day:'numeric', month:'short'});
  const title = event => `${event.kind} / ${event.what}`;
  const outcome = event => event.what === 'failed' ? 'Failed' : event.what === 'denied' ? 'Denied' :
    ['done','created','loaded','signed-in'].includes(event.what) ? 'Completed' : 'Activity';
  const badgeClass = event => `badge bg-${event.what === 'failed' ? 'red' : event.what === 'denied' ? 'orange' : outcome(event) === 'Completed' ? 'green' : 'secondary'}-lt`;
  const summary = event => event.error || event.reason || (event.of ? `Request #${event.of}` : `Event #${event.id}`) +
    (typeof event.duration === 'number' ? ` · ${event.duration.toLocaleString(undefined, {maximumFractionDigits:3})} s` : '');
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  function fill(root, field, value) { const el = root.querySelector(`[data-field="${field}"]`); if (el) el.textContent = value ?? '—'; }
  function announce(message) { $('#feedback').textContent = message; }
  function filters() { return Object.fromEntries(Object.entries(controls).map(([key,selector]) => [key,$(selector).value])); }
  function localInstant(value) { return value ? Date.parse(value + sampleOffset) : null; } // This snapshot covers one Vancouver date.
  function populate() {
    for (const key of ['model','kind','what']) {
      for (const value of [...new Set(source.events.map(event => event[key]).filter(Boolean))].sort()) {
        $(controls[key]).add(new Option(value, value));
      }
    }
  }
  function getMatched() {
    const f = filters(), from = localInstant(f.start), through = localInstant(f.end);
    invalidRange = f.range === 'custom' && ((f.start && !Number.isFinite(from)) ||
      (f.end && !Number.isFinite(through)) || (f.start && f.end && from > through));
    $('#filter-error').hidden = !invalidRange;
    $('#filter-error').textContent = invalidRange ? 'Choose a From time before the Through time.' : '';
    return invalidRange ? [] : events.filter(event => {
      const at = Date.parse(event.at);
      if (f.range !== 'all' && f.range !== 'custom' && at < fixedNow - Number(f.range) * 60000) return false;
      if (f.range === 'custom' && ((from !== null && at < from) || (through !== null && at > through))) return false;
      if (['model','kind','what','user','trace'].some(key => f[key] && event[key] !== f[key])) return false;
      if (f.node && event.machine !== f.node) return false;
      if (f.group && !(source.user_groups[event.user] || []).includes(f.group)) return false;
      if (f.q && !JSON.stringify(event).toLowerCase().includes(f.q.toLowerCase())) return false;
      if (quick === 'failed' || quick === 'denied') { if (event.what !== quick) return false; }
      if ((quick === 'request' || quick === 'model') && event.kind !== quick) return false;
      return !errorCode || event.code === errorCode;
    }).sort((a,b) => {
      const aValue = sort === 'at' ? Date.parse(a.at) : String(a[sort] || '');
      const bValue = sort === 'at' ? Date.parse(b.at) : String(b[sort] || '');
      const order = typeof aValue === 'number' ? aValue - bValue : aValue.localeCompare(bValue);
      return (ascending ? 1 : -1) * (order || a.id - b.id);
    });
  }
  function syncURL() {
    const params = new URLSearchParams({design:layout});
    for (const [key,value] of Object.entries(filters())) if (value && !(key === 'range' && value === '60')) params.set(key,value);
    if (quick !== 'all') params.set('quick',quick);
    if (errorCode) params.set('code',errorCode);
    if (selected !== null) params.set('event',selected);
    params.set('sort',sort); params.set('direction',ascending ? 'asc' : 'desc');
    params.set('limit',$('#log-limit').value); params.set('page',page + 1);
    history.replaceState(null,'','#' + params);
  }
  function updateChips() {
    const f = filters();
    const active = Object.entries({...f, quick:quick === 'all' ? '' : quick, code:errorCode})
      .filter(([key,value]) => value && !(key === 'range' && value === '60') && !(['start','end'].includes(key) && f.range !== 'custom'));
    $('#active-filters').replaceChildren();
    for (const [key,value] of active) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'al-filter-chip';
      const display = key === 'range' ? ({15:'Last 15 minutes', all:'All sample activity', custom:'Custom range'}[value] || value) : value;
      button.textContent = `${labels[key]}: ${display} ×`; button.setAttribute('aria-label',`Remove ${labels[key]} filter: ${display}`);
      button.addEventListener('click',() => {
        if (key === 'quick') quick = 'all'; else if (key === 'code') errorCode = '';
        else $(controls[key]).value = key === 'range' ? '60' : '';
        page = 0; render();
      });
      $('#active-filters').append(button);
    }
    for (const button of $$('[data-quick]')) {
      const active = button.dataset.quick === quick;
      button.className = `btn btn-sm ${active ? 'btn-primary' : 'btn-outline-secondary'}`;
      button.setAttribute('aria-pressed',String(active));
    }
    const count = ['kind','what','user','group','trace'].filter(key => f[key]).length;
    $('#more-filters').textContent = `More filters${count ? ` (${count})` : ''}`;
  }
  function renderTable() {
    const limit = Number($('#log-limit').value), total = matched.length;
    page = Math.max(0,Math.min(page,Math.ceil(total / limit) - 1));
    const visible = matched.slice(page * limit,(page + 1) * limit);
    $('#event-rows').replaceChildren();
    for (const event of visible) {
      const row = clone('#event-row-template'); row.dataset.id = event.id;
      row.setAttribute('aria-selected',String(event.id === selected));
      fill(row,'time',timeFormat.format(new Date(event.at))); fill(row,'date',dateFormat.format(new Date(event.at)));
      row.querySelector('time').dateTime = event.at;
      fill(row,'title',title(event)); fill(row,'summary',summary(event));
      row.querySelector('[data-field="summary"]').title = summary(event);
      fill(row,'outcome',outcome(event)); row.querySelector('[data-field="outcome"]').className = badgeClass(event);
      fill(row,'model',event.model || 'No model'); fill(row,'node',event.machine); fill(row,'user',event.user);
      row.querySelector('[data-open]').addEventListener('click',() => choose(event.id,true));
      const trace = row.querySelector('[data-trace]'); trace.hidden = !event.trace;
      row.querySelector('[data-no-trace]').hidden = !!event.trace;
      trace.textContent = event.trace || ''; trace.addEventListener('click',() => { selected = event.id; setLayout('traces'); });
      $('#event-rows').append(row);
    }
    if (!total) $('#event-rows').append(clone('#empty-row-template'));
    $('#result-count').textContent = `${total} matching events`;
    $('#page-summary').textContent = total ? `${page * limit + 1}–${Math.min((page + 1) * limit,total)} of ${total} events` : '0 events';
    $('#previous').disabled = page === 0; $('#next').disabled = (page + 1) * limit >= total;
    for (const button of $$('[data-sort]')) {
      button.closest('th').setAttribute('aria-sort',button.dataset.sort === sort ? ascending ? 'ascending' : 'descending' : 'none');
      button.querySelector('[data-sort-indicator]').textContent = button.dataset.sort === sort ? ascending ? '↑' : '↓' : '↕';
    }
  }
  function related(event) {
    return events.filter(other => event.trace ? other.trace === event.trace : other.id === event.id)
      .sort((a,b) => Date.parse(a.at) - Date.parse(b.at) || a.id - b.id);
  }
  function addFact(target,label,value) {
    const row = clone('#fact-template'); fill(row,'label',label); fill(row,'value',value); target.append(row);
  }
  function renderInspector() {
    const event = events.find(event => event.id === selected);
    $('#inspector-content').hidden = !event; $('#inspector-empty').hidden = !!event;
    if (!event) return;
    $('#detail-title').textContent = title(event); $('#detail-summary').textContent = summary(event);
    $('#detail-id').textContent = `#${event.id}`; $('#detail-outcome').textContent = outcome(event); $('#detail-outcome').className = badgeClass(event);
    $('#detail-facts').replaceChildren();
    for (const [label,value] of [['Recorded at',`${dateFormat.format(new Date(event.at))}, ${timeFormat.format(new Date(event.at))} Vancouver`],
      ['Node',event.machine],['User',event.user],['Model',event.model || 'Not recorded'],['Caller',event.caller],
      ['Trace ID',event.trace || 'Not recorded'],['Request reference',event.of ? `#${event.of}` : event.kind === 'request' && event.what === 'asked' ? `#${event.id}` : 'Not recorded'],
      ['Sensitivity',event.level || 'Not recorded']]) addFact($('#detail-facts'),label,value);
    $('#detail-payload').replaceChildren();
    const details = Object.entries(event).filter(([key]) => !coreFields.has(key));
    for (const [key,value] of details) addFact($('#detail-payload'),key,typeof value === 'object' ? JSON.stringify(value) : String(value));
    if (!details.length) addFact($('#detail-payload'),'Details','No additional fields recorded');
    $('#detail-json').textContent = JSON.stringify(event,null,2);
    $('#inspect-trace').hidden = !event.trace;
    $('#related-note').textContent = event.trace ? 'Exact trace match · all events in this sample trace, including those outside your filters.' : 'No trace ID was recorded. This event is shown on its own.';
    $('#related-events').replaceChildren();
    for (const other of related(event)) {
      const li = document.createElement('li'), button = document.createElement('button'); button.type = 'button';
      button.textContent = `${timeFormat.format(new Date(other.at))} · ${title(other)} · ${other.machine}`;
      button.addEventListener('click',() => choose(other.id,true)); li.append(button); $('#related-events').append(li);
    }
  }
  function renderTraces() {
    const groups = new Map();
    for (const event of matched) {
      const key = event.trace || `event-${event.id}`;
      if (!groups.has(key)) groups.set(key,[]); groups.get(key).push(event);
    }
    const chosen = events.find(event => event.id === selected), activeKey = chosen && (chosen.trace || `event-${chosen.id}`);
    $('#trace-choices').replaceChildren();
    const traced = [...groups.keys()].filter(key => !key.startsWith('event-')).length;
    const untraced = groups.size - traced;
    $('#trace-count').textContent = `${traced} trace${traced === 1 ? '' : 's'} · ${untraced} untraced event${untraced === 1 ? '' : 's'}`;
    for (const [key,group] of groups) {
      const row = clone('#trace-choice-template');
      const representative = group.find(event => event.what === 'failed') || group.find(event => event.what === 'denied') || group[0];
      fill(row,'trace',representative.trace || `Event #${representative.id}`); fill(row,'outcome',outcome(representative));
      row.querySelector('[data-field="outcome"]').className = badgeClass(representative);
      fill(row,'model',representative.model || `${representative.kind} activity`);
      fill(row,'summary',`${group.length} matching event${group.length === 1 ? '' : 's'} · ${representative.user}`);
      row.setAttribute('aria-pressed',String(key === activeKey));
      row.addEventListener('click',() => choose(representative.id,false)); $('#trace-choices').append(row);
    }
    if (!groups.size) { const message = document.createElement('p'); message.className = 'text-secondary p-3'; message.textContent = 'No traces or events match. Clear filters to explore the sample.'; $('#trace-choices').append(message); }
    $('#timeline-title').textContent = chosen ? chosen.trace || `Untraced event #${chosen.id}` : 'Select a trace';
    $('#timeline-events').replaceChildren();
    const chain = chosen ? related(chosen) : [];
    $('#timeline-note').textContent = chosen ? `${chain.length} recorded event${chain.length === 1 ? '' : 's'} · ${chain.filter(event => matched.some(match => match.id === event.id)).length} match your filters · oldest first · Vancouver` : 'Choose a trace or an untraced event from the list.';
    for (const event of chain) {
      const row = clone('#timeline-template'), button = row.querySelector('[data-open]');
      fill(row,'title',title(event)); fill(row,'summary',summary(event)); fill(row,'time',timeFormat.format(new Date(event.at)));
      row.querySelector('time').dateTime = event.at; fill(row,'outcome',outcome(event)); fill(row,'node',event.machine);
      row.querySelector('[data-field="outcome"]').className = badgeClass(event);
      button.setAttribute('aria-pressed',String(event.id === selected)); button.addEventListener('click',() => choose(event.id,true));
      $('#timeline-events').append(row);
    }
  }
  function renderIncidents() {
    const failures = matched.filter(event => event.what === 'failed'), groups = new Map();
    for (const event of failures) { const key = event.code || 'unclassified'; if (!groups.has(key)) groups.set(key,[]); groups.get(key).push(event); }
    $('#issue-list').replaceChildren();
    const names = {out_of_memory:'GPU memory exhausted', tool_timeout:'Knowledge tool timed out', file_missing:'Model file unavailable'};
    for (const [code,group] of [...groups].sort((a,b) => b[1].length - a[1].length).slice(0,3)) {
      const issue = clone('#issue-template'); fill(issue,'outcome','Failed'); fill(issue,'count',`${group.length} event${group.length === 1 ? '' : 's'}`);
      fill(issue,'title',names[code] || code.replaceAll('_',' '));
      fill(issue,'summary',`${[...new Set(group.map(event => event.machine))].join(', ')} · ${code}`);
      issue.addEventListener('click',() => { errorCode = code; quick = 'failed'; page = 0; render(); $('#event-list').scrollIntoView({block:'start'}); });
      $('#issue-list').append(issue);
    }
    if (!groups.size) { const text = document.createElement('p'); text.className = 'text-secondary py-3'; text.textContent = 'No failed events in this selection. Other outcomes may still need attention.'; $('#issue-list').append(text); }
    const bins = Array.from({length:12},(_,i) => ({start:fixedNow - (12-i)*300000, total:0, failed:0}));
    for (const event of matched) { const index = Math.min(11,Math.floor((Date.parse(event.at) - bins[0].start)/300000)); if (index >= 0 && Date.parse(event.at) <= fixedNow) { bins[index].total++; if (event.what === 'failed') bins[index].failed++; } }
    const max = Math.max(1,...bins.map(bin => bin.total)); $('#activity-chart').replaceChildren();
    for (const bin of bins) {
      const button = document.createElement('button'); button.type = 'button'; button.className = 'al-chart-bar';
      button.setAttribute('aria-label',`${timeFormat.format(new Date(bin.start))} to ${timeFormat.format(new Date(bin.start+300000))}: ${bin.total} events, ${bin.failed} failed`); button.title = button.getAttribute('aria-label');
      const count = document.createElement('span'); count.className = 'al-chart-count'; count.textContent = bin.total;
      const bar = document.createElement('span'); bar.className = 'al-chart-volume'; bar.style.height = `${bin.total/max*83}%`;
      const failed = document.createElement('span'); failed.className = 'al-chart-failed'; failed.style.height = `${bin.failed/max*83}%`;
      button.append(count,bar,failed); button.addEventListener('click',() => {
        // End at the final whole second before the next bucket.
        const local = instant => new Date(instant+offsetMs).toISOString().slice(0,19);
        $('#time-range').value = 'custom'; $('#log-start').value = local(bin.start); $('#log-end').value = local(bin.start+299000);
        setAdvanced(true); page = 0; render();
      }); $('#activity-chart').append(button);
    }
    $('#chart-range').textContent = '09:42 — 10:42 Vancouver · 13 November · sample hour';
    $('#node-breakdown').replaceChildren();
    for (const node of [...new Set(source.events.map(event => event.machine))].sort()) {
      const count = failures.filter(event => event.machine === node).length;
      const button = document.createElement('button'); button.type = 'button'; button.className = 'al-node-row';
      const name = document.createElement('span'); name.textContent = node;
      const bar = document.createElement('progress'); bar.max = Math.max(1,failures.length); bar.value = count; bar.setAttribute('aria-label',`${node}: ${count} failed events`);
      const amount = document.createElement('strong'); amount.textContent = count;
      button.append(name,bar,amount); button.addEventListener('click',() => { $('#log-node').value = node; quick = 'failed'; page = 0; render(); });
      $('#node-breakdown').append(button);
    }
  }
  function render(preserveSelection = false) {
    const failed = $('#scenario').value === 'failure';
    $('#load-error').hidden = !failed; $('#loaded-content').hidden = failed;
    events = $('#scenario').value === 'empty' || failed ? [] : source.events;
    const group = $('#log-group').value;
    for (const option of [...$('#log-user').options]) option.hidden = !!(option.value && group && !source.user_groups[option.value]?.includes(group));
    if ($('#log-user').selectedOptions[0]?.hidden) $('#log-user').value = '';
    matched = getMatched();
    if (!events.some(event => event.id === selected) || (!preserveSelection && !matched.some(event => event.id === selected))) selected = matched.find(event => event.what === 'failed')?.id || matched[0]?.id || null;
    $('#metric-events').textContent = failed ? '—' : matched.length;
    $('#metric-failed').textContent = failed ? '—' : matched.filter(event => event.what === 'failed').length;
    $('#metric-denied').textContent = failed ? '—' : matched.filter(event => event.what === 'denied').length;
    $('#metric-traces').textContent = failed ? '—' : new Set(matched.map(event => event.trace).filter(Boolean)).size;
    $('#export').disabled = !matched.length || failed; $('#copy-view').disabled = invalidRange;
    updateChips(); renderTable(); renderInspector(); renderTraces(); renderIncidents(); syncURL();
  }
  function choose(id,focus) {
    selected = id; render(true);
    if (focus && matchMedia('(max-width:850px)').matches) { $('#inspector').scrollIntoView({block:'start'}); $('#detail-title').focus({preventScroll:true}); }
  }
  function setLayout(value) {
    layout = Object.hasOwn(descriptions,value) ? value : 'explorer';
    $('#workspace').dataset.layout = layout; $('#design-description').textContent = descriptions[layout];
    $('#incident-overview').hidden = layout !== 'incidents';
    for (const button of $$('[data-design]')) { const active = button.dataset.design === layout; button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active)); }
    render(true);
  }
  function setAdvanced(open) { $('#advanced-filters').hidden = !open; $('#more-filters').setAttribute('aria-expanded',String(open)); }
  function clearFilters() {
    for (const [key,selector] of Object.entries(controls)) $(selector).value = key === 'range' ? '60' : '';
    quick = 'all'; errorCode = ''; page = 0; render();
  }
  async function copy(text,message) {
    try { await navigator.clipboard.writeText(text); announce(message); }
    catch { announce('Clipboard unavailable here. Select and copy the JSON or address from your browser.'); }
  }
  function download(text,type,name) {
    const url = URL.createObjectURL(new Blob([text],{type})), link = document.createElement('a');
    link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
  }
  function restore() {
    const params = new URLSearchParams(location.hash.slice(1));
    for (const [key,selector] of Object.entries(controls)) {
      $(selector).value = key === 'range' ? '60' : '';
      const value = params.get(key); if (value === null) continue;
      if ($(selector).tagName !== 'SELECT' || [...$(selector).options].some(option => option.value === value)) $(selector).value = value;
    }
    quick = ['all','failed','denied','request','model'].includes(params.get('quick')) ? params.get('quick') : 'all';
    errorCode = params.get('code') || '';
    sort = ['at','kind','what','model','user','trace'].includes(params.get('sort')) ? params.get('sort') : 'at';
    ascending = params.get('direction') === 'asc'; selected = Number(params.get('event')) || null;
    page = Math.max(0,Number(params.get('page')) - 1 || 0);
    if (['12','24','100'].includes(params.get('limit'))) $('#log-limit').value = params.get('limit');
    if (['kind','what','user','group','trace','start','end'].some(key => params.get(key))) setAdvanced(true);
    setLayout(params.get('design')); // All state is applied before rendering and serializing the view.
  }
  populate();
  $('#filters').addEventListener('submit',event => { event.preventDefault(); page = 0; render(); });
  for (const [key,selector] of Object.entries(controls)) $(selector).addEventListener(['q','trace'].includes(key) ? 'input' : 'change',() => {
    if (key === 'start' || key === 'end') $('#time-range').value = 'custom';
    if (key === 'range' && $('#time-range').value === 'custom') setAdvanced(true);
    page = 0; render();
  });
  document.addEventListener('click',event => { if (event.target.closest('[data-clear]')) clearFilters(); });
  for (const button of $$('[data-design]')) button.addEventListener('click',() => setLayout(button.dataset.design));
  for (const button of $$('[data-quick]')) button.addEventListener('click',() => { quick = button.dataset.quick; errorCode = ''; page = 0; render(); });
  for (const button of $$('[data-sort]')) button.addEventListener('click',() => { ascending = sort === button.dataset.sort ? !ascending : true; sort = button.dataset.sort; page = 0; render(true); });
  $('#more-filters').addEventListener('click',() => setAdvanced($('#advanced-filters').hidden));
  $('#previous').addEventListener('click',() => { page--; render(true); });
  $('#next').addEventListener('click',() => { page++; render(true); });
  $('#log-limit').addEventListener('change',() => { page = 0; render(true); });
  $('#all-failures').addEventListener('click',() => { quick = 'failed'; errorCode = ''; page = 0; render(); });
  $('#inspect-trace').addEventListener('click',() => setLayout('traces'));
  $('#copy-view').addEventListener('click',() => copy(location.href,'View link copied. It includes the design, filters and selected event.'));
  $('#copy-event').addEventListener('click',() => copy($('#detail-json').textContent,'Event JSON copied.'));
  $('#download-event').addEventListener('click',() => download($('#detail-json').textContent,'application/json',`ai-event-${selected}.json`));
  $('#export').addEventListener('click',() => {
    const columns = ['id','at','machine','kind','what','user','model','trace','details'];
    const cell = value => '"' + String(value ?? '').replace(/^[=+@\-\t\r]/,"'$&").replaceAll('"','""') + '"';
    const rows = matched.map(event => columns.map(key => cell(key === 'details' ? JSON.stringify(Object.fromEntries(Object.entries(event).filter(([name]) => !coreFields.has(name)))) : event[key])).join(','));
    download([columns.join(','),...rows].join('\r\n'),'text/csv;charset=utf-8','ai-logs-filtered.csv');
    announce(`Exported all ${matched.length} matching events, across every page.`);
  });
  $('#scenario').addEventListener('change',() => { selected = null; page = 0; render(); announce($('#scenario').value === 'normal' ? 'Sample activity restored. Filters kept.' : 'Preview state changed. Filters kept.'); });
  $('#retry').addEventListener('click',() => { $('#scenario').value = 'normal'; render(); announce('Sample activity loaded. Your filters were kept.'); });
  $('#reset-demo').addEventListener('click',() => { $('#scenario').value = 'normal'; sort = 'at'; ascending = false; $('#log-limit').value = '12'; setAdvanced(false); clearFilters(); setLayout('explorer'); announce('Sample data reset. All interactions stay in this preview.'); });
  $('#theme').addEventListener('click',() => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark', control = $('[data-theme-form] [name="mode"]');
    control.value = dark ? 'dark' : 'light'; control.dispatchEvent(new Event('change',{bubbles:true}));
    $('#theme').textContent = dark ? 'Light mode' : 'Dark mode';
  });
  $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  window.addEventListener('hashchange',restore);
  restore();
})();
