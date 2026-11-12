// Schedule Studio. Drafts and filters stay in page memory across server-rendered selections.
(() => {
  'use strict';
  const drafts = new Map(), baselines = new WeakMap(), savedFields = new Map(), needsPreview = new WeakSet();
  const view = {search: '', status: 'all', kind: 'all'};
  let initialized = null, pending = null;
  const root = () => document.querySelector('[data-schedule-studio]');
  const formOf = page => page.querySelector('[data-schedule-form]');
  const fields = form => [...form.elements].filter(field => field.name && !['hidden', 'submit', 'button', 'reset', 'fieldset'].includes(field.type));
  const valueOf = field => field.multiple ? [...field.selectedOptions].map(option => option.value) : ['checkbox', 'radio'].includes(field.type) ? field.checked : field.value;
  function restore(field, value) {
    if (field.multiple) for (const option of field.options) option.selected = value.includes(option.value);
    else if (['checkbox', 'radio'].includes(field.type)) field.checked = value;
    else field.value = value;
  }
  function remember(page) {
    const form = formOf(page);
    if (form.dataset.canEdit !== 'yes') return;
    const edits = fields(form).map((field, index) => ({field, index})).filter(({field}) => JSON.stringify(valueOf(field)) !== baselines.get(field))
      .map(({field, index}) => ({index, name: field.name, type: field.type, value: valueOf(field)}));
    if (edits.length) drafts.set(page.dataset.selected, edits); else drafts.delete(page.dataset.selected);
  }
  function showDrafts(page) {
    const dirty = drafts.has(page.dataset.selected);
    page.querySelector('#schedule-draft-status').textContent = dirty ? 'Unsaved changes' : page.dataset.selected === 'new' ? 'Draft' : 'Saved';
    page.querySelector('[data-discard-schedule]').hidden = !dirty;
    for (const row of page.querySelectorAll('[data-schedule-row]')) row.querySelector('[data-draft-mark]').hidden = !drafts.has(row.dataset.scheduleRow);
  }
  function filter(page) {
    const rows = [...page.querySelectorAll('[data-schedule-row]')];
    for (const row of rows) row.hidden = !row.dataset.search.toLowerCase().includes(view.search.trim().toLowerCase()) ||
      (view.kind !== 'all' && row.dataset.kind !== view.kind) || (view.status === 'active' && row.dataset.enabled !== 'yes') ||
      (view.status === 'paused' && row.dataset.enabled !== 'no') || (view.status === 'attention' && row.dataset.attention !== 'yes');
    const count = rows.filter(row => !row.hidden).length;
    page.querySelector('#schedule-count').textContent = `${count} of ${rows.length} schedules`;
    page.querySelector('#schedule-no-matches').hidden = !rows.length || count > 0;
  }
  function showScope(page) {
    const form = formOf(page), kind = form.elements.kind.value, readonly = form.dataset.canEdit !== 'yes';
    for (const group of form.querySelectorAll('[data-schedule-kind]')) {
      group.hidden = !group.dataset.scheduleKind.split(' ').includes(kind);
      for (const input of group.querySelectorAll('input,select')) input.disabled = readonly || group.hidden;
    }
    const node = form.elements.machine.value;
    const catalogue = JSON.parse(page.querySelector('#schedule-catalogue').textContent);
    const selected = catalogue.find(row => row.model_id === form.elements.model.value), locations = selected?.locations || [];
    const target = form.querySelector('[data-schedule-target]');
    target.textContent = 'Uses this node when available; a verified missing model can fall back. Unknown nodes defer the occurrence.';
    if (kind === 'benchmark' && locations.length) {
      const chosen = locations.find(one => one.machine === node) || locations[0];
      target.textContent = `Target: ${chosen.machine}${chosen.machine !== node ? ` · Model absent on ${node}; fallback shown here.` : '.'}`;
    }
    const days = [...form.querySelectorAll('[name=weekdays]:checked')].map(input => Number(input.value));
    form.querySelector('#repeat').value = days.length === 7 ? 'daily' : days.join() === '0,1,2,3,4' ? 'weekdays' : 'custom';
  }
  function preview(page) {
    const control = page.querySelector('[data-schedule-preview]');
    if (formOf(page).dataset.canEdit === 'yes' && control && window.htmx) htmx.trigger(control, 'schedule:timing');
  }
  function setup() {
    const page = root();
    if (!page || initialized === page) return;
    initialized = page;
    const failed = page.dataset.saveFailed === 'yes' || Boolean(page.querySelector('.alert-danger'));
    if (pending && !failed) drafts.delete(pending.key);
    pending = null;
    const form = formOf(page), controls = fields(form);
    // Failed responses have their raw form values refilled by the shared middleware.
    // Retain the previous saved baseline so Discard still restores saved settings.
    const previous = failed ? savedFields.get(page.dataset.selected) : null;
    controls.forEach((field, index) => {
      const saved = previous?.[index];
      baselines.set(field, saved && saved.name === field.name && saved.type === field.type ? saved.value : JSON.stringify(valueOf(field)));
    });
    savedFields.set(page.dataset.selected, controls.map(field => ({name: field.name, type: field.type, value: baselines.get(field)})));
    for (const edit of drafts.get(page.dataset.selected) || []) {
      const field = controls[edit.index];
      if (field && field.name === edit.name && field.type === edit.type) restore(field, edit.value);
    }
    page.querySelector('#schedule-search').value = view.search;
    page.querySelector('#schedule-status-filter').value = view.status;
    page.querySelector('#schedule-kind-filter').value = view.kind;
    showScope(page); showDrafts(page); filter(page);
    if (drafts.has(page.dataset.selected) || failed) needsPreview.add(page);
  }
  document.addEventListener('input', event => {
    const page = root();
    if (!page || !page.contains(event.target)) return;
    if (event.target.id === 'schedule-search') { view.search = event.target.value; filter(page); }
    if (event.target.closest('[data-schedule-form]')) {
      remember(page); showDrafts(page);
      if (['time', 'timezone'].includes(event.target.name)) preview(page);
    }
  });
  document.addEventListener('change', event => {
    const page = root();
    if (!page || !page.contains(event.target)) return;
    if (event.target.id === 'schedule-status-filter') { view.status = event.target.value; return filter(page); }
    if (event.target.id === 'schedule-kind-filter') { view.kind = event.target.value; return filter(page); }
    if (!event.target.closest('[data-schedule-form]')) return;
    if (event.target.id === 'repeat' && event.target.value !== 'custom') {
      for (const input of formOf(page).querySelectorAll('[name=weekdays]')) input.checked = event.target.value === 'daily' || Number(input.value) < 5;
    }
    const repeat = event.target.id === 'repeat' ? event.target.value : null;
    showScope(page);
    if (repeat === 'custom') page.querySelector('#repeat').value = 'custom';
    remember(page); showDrafts(page);
    if (['time', 'timezone', 'weekdays'].includes(event.target.name) || repeat) preview(page);
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('[data-discard-schedule]')) return;
    const page = root();
    for (const field of fields(formOf(page))) restore(field, JSON.parse(baselines.get(field)));
    drafts.delete(page.dataset.selected); showScope(page); showDrafts(page); preview(page);
  });
  document.addEventListener('htmx:beforeRequest', event => {
    const page = root(), elt = event.detail.elt;
    if (!page || !elt) return;
    if (elt.matches?.('[data-schedule-form]')) pending = {key: page.dataset.selected};
    else if (elt.matches?.('[data-schedule-delete]')) pending = {key: elt.dataset.scheduleDelete};
  });
  document.addEventListener('htmx:beforeSwap', event => {
    const page = root();
    if (page && event.detail.target?.id === 'page-content') remember(page);
  });
  document.addEventListener('htmx:afterRequest', event => {
    if (event.detail.failed && event.detail.elt?.matches?.('[data-schedule-form],[data-schedule-delete]')) pending = null;
  });
  document.addEventListener('htmx:afterSwap', setup);
  document.addEventListener('htmx:historyRestore', setup);
  function refreshRestoredTiming() {
    const page = root();
    if (page && needsPreview.has(page)) { needsPreview.delete(page); preview(page); }
  }
  // HTMX finishes initializing restored attributes during settle, after afterSwap.
  document.addEventListener('htmx:afterSettle', refreshRestoredTiming);
  document.addEventListener('htmx:load', refreshRestoredTiming);
  window.addEventListener('beforeunload', event => {
    if (drafts.size) { event.preventDefault(); event.returnValue = ''; }
  });
  setup();
})();
