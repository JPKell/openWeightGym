/* Portable design review. All mutations affect memory only; no live requests. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const source = JSON.parse($('#schedule-data').textContent);
  const DISPLAY_ZONE = 'America/Vancouver';
  const names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  const functions = {backup: 'Paired backup', benchmark: 'Benchmark rerun', outstanding: 'Missing / outdated benchmark'};
  const models = {'qwen3-32b': 'Qwen3 32B', 'llama-3.3-70b': 'Llama 3.3 70B', 'gemma3-27b': 'Gemma 3 27B'};
  const labels = {completed: 'Completed', failed: 'Failed', deferred: 'Waiting for node', paused: 'Paused', active: 'Active', new: 'New schedule'};
  const descriptions = {
    studio: 'A calm split view. Browse schedules on the left; fine-tune one on the right. Recommended.',
    planner: 'A week at a glance. See the rhythm of recurring work and open any run to edit its schedule.',
    ledger: 'A precise operations view. Scan timing, cron equivalents and outcomes across a larger collection.'
  };
  const drafts = new Map(), formatters = new Map();
  const NOW = localDate(source.now, DISPLAY_ZONE);
  for (const job of source.jobs) job.at = localDate(job.at, job.timezone).toISOString();
  let jobs, history, selected = 'nightly', layout = 'studio', sequence = 0;
  const form = $('#schedule-form'), dialog = $('#editor-dialog');

  function clone(id) { return document.getElementById(id).content.firstElementChild.cloneNode(true); }
  function put(node, selector, text) { node.querySelector(selector).textContent = text; }
  function announce(text) { $('#feedback').textContent = text; }
  function field(name) { return form.elements.namedItem(name); }
  function clockParts(date, zone) {
    if (!formatters.has(zone)) formatters.set(zone, new Intl.DateTimeFormat('en-CA', {
      timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    }));
    return Object.fromEntries(formatters.get(zone).formatToParts(date).filter(p => p.type !== 'literal').map(p => [p.type, Number(p.value)]));
  }
  function stamp(date, zone, options) { return new Intl.DateTimeFormat('en-GB', {timeZone: zone, ...options}).format(date); }
  function localDate(value, zone) { return new Date(wallTime(...value.split(/[-T:]/).map(Number), zone)); }
  function daysText(days) {
    if (days.length === 7) return 'Every day';
    if (days.join() === '0,1,2,3,4') return 'Weekdays';
    if (days.join() === '5,6') return 'Weekends';
    return days.map(day => names[day]).join(', ');
  }
  function cron(job) {
    const [hour, minute] = job.time.split(':').map(Number);
    const days = job.weekdays.length === 7 ? '*' : job.weekdays.join() === '0,1,2,3,4' ? '1-5' : job.weekdays.map(d => (d + 1) % 7).sort().join(',');
    return `${minute} ${hour} * * ${days}`;
  }
  // Wall-clock candidates are round-tripped through the zone. Clock gaps disappear;
  // both sides of a repeated hour are considered so its first occurrence wins.
  function wallTime(year, month, day, hour, minute, zone) {
    const nominal = Date.UTC(year, month - 1, day, hour, minute);
    let guess = nominal;
    for (let attempt = 0; attempt < 3; attempt++) {
      const p = clockParts(new Date(guess), zone);
      guess += nominal - Date.UTC(p.year, p.month - 1, p.day, p.hour, p.minute);
    }
    return [guess - 3600000, guess, guess + 3600000].sort((a, b) => a - b).find(candidate => {
      const p = clockParts(new Date(candidate), zone);
      return p.year === year && p.month === month && p.day === day && p.hour === hour && p.minute === minute;
    });
  }
  function occurrences(job, after = NOW.getTime(), count = 3) {
    if (!job.weekdays.length || !/^\d{2}:\d{2}$/.test(job.time)) return [];
    const p = clockParts(new Date(after), job.timezone), dates = [];
    const [hour, minute] = job.time.split(':').map(Number);
    for (let i = 0; i < 24 && dates.length < count; i++) {
      const day = new Date(Date.UTC(p.year, p.month - 1, p.day + i));
      if (!job.weekdays.includes((day.getUTCDay() + 6) % 7)) continue;
      const at = wallTime(day.getUTCFullYear(), day.getUTCMonth() + 1, day.getUTCDate(), hour, minute, job.timezone);
      if (at !== undefined && at > after) dates.push(new Date(at));
    }
    return dates;
  }
  function relativeDay(date, zone) {
    const p = clockParts(date, zone), n = clockParts(NOW, zone);
    const diff = (Date.UTC(p.year, p.month - 1, p.day) - Date.UTC(n.year, n.month - 1, n.day)) / 86400000;
    return diff === 0 ? 'Today' : diff === 1 ? 'Tomorrow' : stamp(date, zone, {weekday: 'short', day: 'numeric', month: 'short'});
  }
  function work(job) { return job.kind === 'benchmark' ? `${models[job.model]} · ${job.benchmark.toUpperCase()}` : job.kind === 'outstanding' ? `One eligible pair · ${job.machine}` : `Recovery sets · retain ${job.retention || 'all'}`; }
  function state(node, value) { node.textContent = labels[value] || value; node.dataset.tone = value; }
  function jobState(job) { return job.enabled ? job.last || 'active' : 'paused'; }
  function filtered() {
    const search = $('#search').value.toLowerCase().trim(), status = $('#status-filter').value, kind = $('#kind-filter').value;
    return jobs.filter(job => (!search || `${job.name} ${functions[job.kind]} ${work(job)} ${job.owner}`.toLowerCase().includes(search)) &&
      (kind === 'all' || kind === job.kind) && (status === 'all' || status === 'active' && job.enabled || status === 'paused' && !job.enabled || status === 'attention' && ['failed', 'deferred'].includes(job.last)));
  }
  function wireOpen(node, job, modal = true) {
    const button = node.matches('[data-open]') ? node : node.querySelector('[data-open]');
    button.dataset.schedule = job.id;
    button.setAttribute('aria-label', `Edit ${job.name}`);
    button.addEventListener('click', () => choose(job.id, modal));
  }
  function toggle(job) {
    job.enabled = !job.enabled;
    if (drafts.has(job.id)) drafts.get(job.id).enabled = job.enabled;
    render();
    if (selected === job.id) updateEditor();
    announce(`${job.enabled ? 'Resumed' : 'Paused'} ${job.name} in this demo. ${job.enabled ? 'The next future time is shown.' : 'Future runs are stopped; claimed work continues.'}`);
  }
  function renderInventory(visible) {
    $('#schedule-list').replaceChildren(); $('#ledger-rows').replaceChildren();
    for (const job of visible) {
      const next = job.enabled ? occurrences(job)[0] : null;
      const row = clone('schedule-choice-template'), table = clone('schedule-ledger-template');
      for (const node of [row, table]) {
        node.dataset.kind = job.kind; node.dataset.selected = String(selected === job.id);
        put(node, '[data-name]', job.name); put(node, '[data-work]', work(job));
        put(node, '[data-kind-mark]', {backup: '▤', benchmark: '↗', outstanding: '✧'}[job.kind]);
        put(node, '[data-next-time]', next ? stamp(next, job.timezone, {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}) : '—');
        put(node, '[data-next-day]', next ? relativeDay(next, job.timezone) : 'Paused');
        state(node.querySelector('[data-state]'), jobState(job));
        const action = node.querySelector('[data-toggle]'); action.textContent = job.enabled ? 'Pause' : 'Resume';
        action.setAttribute('aria-label', `${job.enabled ? 'Pause' : 'Resume'} ${job.name}`);
        action.addEventListener('click', () => toggle(job)); wireOpen(node, job);
      }
      row.querySelector('[data-open]').setAttribute('aria-pressed', String(selected === job.id));
      put(row, '[data-recurrence]', `${daysText(job.weekdays)} · ${job.time} · ${job.timezone.split('/').pop().replaceAll('_', ' ')}`);
      put(table, '[data-clock]', `${job.time} · ${job.timezone.split('/').pop().replaceAll('_', ' ')}`);
      put(table, '[data-days]', daysText(job.weekdays)); put(table, '[data-cron]', cron(job));
      put(table, '[data-last-time]', job.at ? stamp(new Date(job.at), job.timezone, {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}) : 'No runs yet');
      $('#schedule-list').append(row); $('#ledger-rows').append(table);
    }
    if (!visible.length) {
      const empty = clone('schedule-empty-template');
      if (jobs.length) put(empty, '[data-empty-text]', 'No schedules match. Try another search or clear the filters.');
      empty.querySelector('[data-new]').addEventListener('click', newSchedule);
      $('#schedule-list').append(empty); $('#ledger-rows').append(clone('schedule-empty-row'));
    }
  }
  function renderCalendar(visible) {
    $('#week-calendar').replaceChildren();
    for (let i = 0; i < 7; i++) {
      const start = wallTime(2026, 11, 9 + i, 0, 0, DISPLAY_ZONE);
      const end = wallTime(2026, 11, 10 + i, 0, 0, DISPLAY_ZONE), date = new Date(start);
      const cell = clone('schedule-day-template'); cell.dataset.today = String(i === 2);
      cell.setAttribute('aria-label', stamp(date, DISPLAY_ZONE, {weekday: 'long', month: 'long', day: 'numeric'}));
      put(cell, '[data-day-name]', names[i]); put(cell, '[data-day-number]', 9 + i);
      cell.querySelector('.sc-today-label').hidden = i !== 2;
      const events = visible.filter(job => job.enabled).flatMap(job => occurrences(job, start - 1, 1).filter(at => at.getTime() < end).map(at => ({job, at}))).sort((a, b) => a.at - b.at);
      for (const {job, at} of events) {
        const event = clone('schedule-event-template'); event.dataset.kind = job.kind;
        event.setAttribute('aria-pressed', String(selected === job.id));
        put(event, '[data-name]', job.name);
        put(event, '[data-event-detail]', `${stamp(at, DISPLAY_ZONE, {hour: '2-digit', minute: '2-digit', hourCycle: 'h23'})} · ${job.kind === 'backup' ? 'Backup' : job.kind === 'benchmark' ? job.benchmark.toUpperCase() : 'Upkeep'}`);
        wireOpen(event, job); cell.querySelector('.sc-day-events').append(event);
      }
      cell.querySelector('.sc-free-day').hidden = events.length > 0;
      $('#week-calendar').append(cell);
    }
  }
  function renderHistory(visible) {
    $('#history-list').replaceChildren();
    const ids = new Set(visible.map(job => job.id));
    const runs = history.filter(run => ids.has(run.id) || !jobs.some(job => job.id === run.id) && $('#status-filter').value === 'all' && $('#kind-filter').value === 'all' && !$('#search').value).slice(0, 6);
    $('#history-scope').textContent = `${runs.length} latest outcomes`;
    for (const run of runs) {
      const row = clone('schedule-history-template'); row.dataset.tone = run.last;
      put(row, '[data-name]', run.name);
      put(row, '[data-history-time]', stamp(new Date(run.at), DISPLAY_ZONE, {day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZoneName: 'short'}));
      state(row.querySelector('[data-state]'), run.last); put(row, '[data-message]', run.message);
      const job = jobs.find(item => item.id === run.id);
      if (job) wireOpen(row, job); else row.querySelector('[data-open]').hidden = true;
      $('#history-list').append(row);
    }
    if (!runs.length) { const note = document.createElement('p'); note.className = 'text-secondary small p-4 mb-0'; note.textContent = 'No runs yet. Your first outcome will appear here.'; $('#history-list').append(note); }
  }
  function render() {
    const visible = filtered();
    $('#visible-count').textContent = `${visible.length} of ${jobs.length} schedules`;
    $('#enabled-count').textContent = jobs.filter(job => job.enabled).length;
    $('#paused-count').textContent = jobs.filter(job => !job.enabled).length;
    $('#attention-count').textContent = jobs.filter(job => ['failed','deferred'].includes(job.last)).length;
    $('#overview-deferred').textContent = `${jobs.filter(job => job.last === 'failed').length} failed · ${jobs.filter(job => job.last === 'deferred').length} waiting for a node`;
    const upcoming = jobs.filter(job => job.enabled).map(job => ({job, next: occurrences(job)[0]})).filter(row => row.next).sort((a, b) => a.next - b.next)[0];
    $('#overview-next').textContent = upcoming ? `${upcoming.job.name} · ${relativeDay(upcoming.next, upcoming.job.timezone)} · ${upcoming.job.timezone}` : 'Nothing scheduled';
    $('#overview-next-time').textContent = upcoming ? upcoming.job.time : '—';
    renderInventory(visible); renderCalendar(visible); renderHistory(visible);
    $('#inspector-slot').hidden = layout !== 'studio';
    $('#schedule-list').hidden = layout === 'ledger'; $('#schedule-ledger').hidden = layout !== 'ledger';
  }
  function defaults() { return {...source.jobs[0], id: 'new', name: '', last: '', at: '', message: '', enabled: true}; }
  function savedJob() { return jobs.find(job => job.id === selected) || defaults(); }
  function current() { return drafts.get(selected) || savedJob(); }
  function readForm() {
    return {...current(), name: field('name').value.trim(), kind: field('kind').value, time: field('time').value,
      timezone: field('timezone').value.trim(), model: field('model').value, benchmark: field('benchmark').value,
      machine: field('machine').value, missed: field('missed').value, retention: Number(field('retention').value),
      weekdays: $$('[name="weekdays"]:checked').map(input => Number(input.value)),
      model_ids: [...field('model_ids').selectedOptions].map(option => option.value),
      benchmarks: [...field('benchmarks').selectedOptions].map(option => option.value)};
  }
  function updateEditor() {
    const job = current(), fresh = selected === 'new';
    $('#editor-title').textContent = fresh ? 'New schedule' : savedJob().name;
    $('#editor-eyebrow').textContent = fresh ? 'MAKE TIME FOR THE ROUTINE' : 'SCHEDULE DETAILS';
    $('#editor-owner').textContent = `Owned by ${job.owner}`;
    state($('#editor-state'), fresh ? 'new' : job.enabled ? 'active' : 'paused');
    $('#draft-state').textContent = drafts.has(selected) ? 'Unsaved changes' : fresh ? 'Draft' : 'Saved';
    $('#save-schedule').textContent = fresh ? 'Create schedule' : 'Save changes';
    $('#delete-schedule').hidden = fresh;
    for (const group of $$('[data-for-kind]')) {
      group.hidden = !group.dataset.forKind.split(' ').includes(job.kind);
      for (const input of group.querySelectorAll('input,select')) input.disabled = group.hidden;
    }
    $('#form-error').hidden = true;
    $('#next-times').replaceChildren();
    try {
      if (!job.weekdays.length) throw new Error('Choose at least one weekday.');
      if (!job.time) throw new Error('Choose a time.');
      const next = occurrences(job);
      $('#cron-value').textContent = cron(job);
      $('#timing-summary').textContent = `${daysText(job.weekdays)} at ${job.time}, ${job.timezone}.`;
      $('#next-zone').textContent = `· ${job.timezone}`;
      if (!job.enabled) {
        const note = clone('schedule-next-template'); note.textContent = 'Paused. Resume to start at the next future time.'; $('#next-times').append(note);
      } else for (const at of next) {
        const li = clone('schedule-next-template'); li.textContent = stamp(at, job.timezone, {weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'}); $('#next-times').append(li);
      }
    } catch (error) {
      $('#cron-value').textContent = '—'; $('#timing-summary').textContent = error instanceof RangeError ? 'Enter a valid IANA timezone, such as America/Vancouver.' : error.message;
      $('#next-zone').textContent = '';
    }
  }
  function fillEditor() {
    const job = current();
    for (const name of ['name', 'kind', 'time', 'timezone', 'model', 'benchmark', 'machine', 'missed', 'retention']) field(name).value = job[name];
    for (const input of $$('[name="weekdays"]')) input.checked = job.weekdays.includes(Number(input.value));
    for (const name of ['model_ids', 'benchmarks']) for (const option of field(name).options) option.selected = job[name].includes(option.value);
    $('#repeat').value = job.weekdays.length === 7 ? 'daily' : job.weekdays.join() === '0,1,2,3,4' ? 'weekdays' : 'custom';
    updateEditor();
  }
  function choose(id, open = true) {
    selected = id; fillEditor(); render();
    if (layout !== 'studio' && open) { if (!dialog.open) dialog.showModal(); }
    else if (open && innerWidth <= 800) $('#schedule-inspector').scrollIntoView({behavior: 'smooth', block: 'start'});
  }
  function newSchedule() { choose('new'); field('name').focus({preventScroll: true}); }
  function setLayout(value) {
    if (dialog.open) dialog.close();
    layout = value; $('#workspace').dataset.layout = value;
    for (const button of $$('[data-design]')) { const active = button.dataset.design === value; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); }
    $('#design-description').textContent = descriptions[value];
    (value === 'studio' ? $('#inspector-slot') : $('#dialog-slot')).append($('#schedule-inspector'));
    $('#close-editor').hidden = value === 'studio';
    render();
    location.hash = value;
  }
  function remember() { drafts.set(selected, readForm()); updateEditor(); }
  form.addEventListener('input', remember);
  form.addEventListener('change', event => {
    if (event.target.id === 'repeat') {
      if (event.target.value !== 'custom') for (const input of $$('[name="weekdays"]')) input.checked = event.target.value === 'daily' || Number(input.value) < 5;
      remember();
    } else if (event.target.name === 'weekdays') {
      const days = readForm().weekdays;
      $('#repeat').value = days.length === 7 ? 'daily' : days.join() === '0,1,2,3,4' ? 'weekdays' : 'custom';
      remember();
    } else remember();
  });
  form.addEventListener('submit', event => {
    event.preventDefault(); const job = readForm();
    let error = '';
    if (!job.weekdays.length) error = 'Select at least one weekday.';
    try { clockParts(NOW, job.timezone); } catch { error = 'Enter a valid IANA timezone, such as America/Vancouver.'; }
    if (error) { $('#form-error').textContent = error; $('#form-error').hidden = false; return; }
    if (selected === 'new') { job.id = `sample-${++sequence}`; jobs.push(job); drafts.delete('new'); selected = job.id; }
    else { const index = jobs.findIndex(item => item.id === selected); jobs[index] = job; }
    drafts.delete(selected); fillEditor(); render();
    announce(`Saved ${job.name} in this demo. ${job.enabled ? 'The next three run times are shown in the editor.' : 'It remains paused.'}`);
  });
  $('#discard').addEventListener('click', () => { drafts.delete(selected); fillEditor(); announce('Draft discarded. Saved settings restored.'); });
  $('#close-editor').addEventListener('click', () => dialog.close());
  $('#delete-schedule').addEventListener('click', () => {
    const confirmation = $('#danger-confirmation');
    confirmation.returnValue = '';
    $('#danger-confirmation-check').checked = false;
    $('#danger-confirmation-check').labels[0].textContent = `Delete ${savedJob().name} from this demo. History is kept.`;
    confirmation.showModal();
  });
  $('#danger-confirmation').addEventListener('close', event => {
    if (event.target.returnValue !== 'confirm') return;
    const name = savedJob().name; jobs = jobs.filter(job => job.id !== selected); drafts.delete(selected);
    if (dialog.open) dialog.close(); choose(jobs[0]?.id || 'new', false); announce(`Deleted ${name} in this demo. Existing run history is kept.`);
  });
  for (const button of $$('[data-design]')) button.addEventListener('click', () => setLayout(button.dataset.design));
  for (const button of $$('[data-new]')) button.addEventListener('click', newSchedule);
  for (const id of ['search', 'status-filter', 'kind-filter']) $( '#' + id).addEventListener(id === 'search' ? 'input' : 'change', render);
  function reset() {
    if (dialog.open) dialog.close();
    const scenario = $('#scenario').value;
    jobs = scenario === 'empty' ? [] : structuredClone(source.jobs);
    if (scenario === 'attention') { jobs[0].last = 'failed'; jobs[0].message = 'Backup could not complete: the compute node is unreachable. Existing recovery sets are intact.'; }
    history = jobs.filter(job => job.at).map(job => ({...job})).sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
    drafts.clear(); $('#search').value = ''; $('#status-filter').value = 'all'; $('#kind-filter').value = 'all';
    choose(jobs[0]?.id || 'new', false); announce('Sample data · changes and actions are simulated.');
  }
  $('#scenario').addEventListener('change', reset); $('#reset').addEventListener('click', reset);
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark';
    const control = $('[data-theme-form] [name="mode"]');
    control.value = dark ? 'dark' : 'light'; control.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = dark ? 'Light mode' : 'Dark mode';
  });
  $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  window.addEventListener('hashchange', () => {
    const next = location.hash.slice(1);
    if (Object.hasOwn(descriptions, next) && next !== layout) setLayout(next);
  });
  reset();
  setLayout(Object.hasOwn(descriptions, location.hash.slice(1)) ? location.hash.slice(1) : 'studio');
})();
