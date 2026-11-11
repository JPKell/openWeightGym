// Node navigation keeps drafts in this page's memory; every save uses its existing server form.
(() => {
  'use strict';
  const drafts = new Map();
  const submitted = new WeakMap();
  const baselines = new WeakMap();
  const serverBaselines = new Map();
  let initializedPage = null;
  let search = '', filter = 'all', pairingOpen = false, pairingContent = null;
  const root = () => document.querySelector('[data-compute-nodes]');
  const forms = page => [...page.querySelectorAll('form[data-node-draft]')];
  const controls = form => [...form.elements].filter(field =>
    field.name && !['hidden', 'submit', 'button', 'reset'].includes(field.type));
  const key = form => JSON.stringify([new URL(form.action).pathname,
    form.querySelector('[name=engine]')?.value || '', controls(form).map(field => field.name)]);
  const changed = field => JSON.stringify({value: field.value, checked: field.checked}) !== baselines.get(field);
  const values = form => controls(form).map((field, index) => ({index, name: field.name, type: field.type, value: field.value, checked: field.checked}));
  function remember(form) {
    const page = form.closest('[data-compute-nodes]');
    if (!page || !form.matches('[data-node-draft]')) return;
    const name = page.dataset.machine;
    if (!drafts.has(name)) drafts.set(name, new Map());
    const edits = controls(form).map((field, index) => ({field, index})).filter(({field}) => changed(field))
      .map(({field, index}) => ({index, name: field.name, type: field.type, value: field.value, checked: field.checked}));
    if (edits.length) drafts.get(name).set(key(form), edits);
    else drafts.get(name).delete(key(form));
  }
  function showDraftState(page) {
    for (const link of page.querySelectorAll('[data-node-choice]')) {
      link.querySelector('[data-draft-mark]').hidden = !drafts.get(link.dataset.nodeChoice)?.size;
    }
    const count = drafts.get(page.dataset.machine)?.size || 0;
    const status = page.querySelector('[data-node-draft-status]');
    if (status) status.textContent = count ? `Unsent changes in ${count} group${count === 1 ? '' : 's'}` : 'No unsent changes';
  }
  function filterNodes(page) {
    const links = [...page.querySelectorAll('[data-node-choice]')];
    for (const link of links) {
      const state = link.dataset.nodeState;
      const matches = filter === 'all' || (filter === 'available' && state === 'ready') ||
        (filter === 'attention' && !['ready', 'retired'].includes(state)) || (filter === 'retired' && state === 'retired');
      link.hidden = !matches || !link.dataset.nodeSearch.toLowerCase().includes(search.trim().toLowerCase());
    }
    const count = links.filter(link => !link.hidden).length;
    const counter = page.querySelector('[data-node-count]'), empty = page.querySelector('[data-node-empty]');
    if (counter) counter.textContent = count;
    if (empty) empty.hidden = count > 0;
  }
  function showPairing(page, open, focus = false) {
    const panel = page.querySelector('#add-node');
    if (!panel) return;
    pairingOpen = open; panel.hidden = !open;
    page.querySelector('[data-toggle-pairing]')?.setAttribute('aria-expanded', String(open));
    if (focus) (open ? panel.querySelector('button') : page.querySelector('[data-toggle-pairing]'))?.focus();
  }
  function setup() {
    const page = root();
    if (!page || page === initializedPage) return;
    initializedPage = page;
    const machine = page.dataset.machine;
    if (!serverBaselines.has(machine)) serverBaselines.set(machine, new Map());
    const previous = serverBaselines.get(machine), failed = page.querySelector('.said-box.alert-danger');
    for (const form of forms(page)) {
      const fields = controls(form);
      // A rejected POST echoes the typed values; those are still drafts, not a new baseline.
      const baseline = failed && previous.get(key(form)) || fields.map(field => JSON.stringify({value: field.value, checked: field.checked}));
      previous.set(key(form), baseline);
      fields.forEach((field, index) => baselines.set(field, baseline[index]));
      for (const edit of drafts.get(machine)?.get(key(form)) || []) {
        const field = fields[edit.index];
        if (!field || field.name !== edit.name || field.type !== edit.type) continue;
        if (['checkbox', 'radio'].includes(field.type)) field.checked = edit.checked;
        else field.value = edit.value;
        // Drafts should be immediately discoverable inside native disclosures.
        for (let section = field.closest('details'); section; section = section.parentElement.closest('details')) section.open = true;
      }
    }
    const input = page.querySelector('#node-search'), select = page.querySelector('#node-filter');
    if (input) input.value = search;
    if (select) select.value = filter;
    const panel = page.querySelector('#add-node');
    if (panel) {
      if (panel.querySelector('#pairing-command')) {
        pairingContent = panel.querySelector('.compute-pairing-layout').cloneNode(true);
        pairingOpen = true;
      } else if (pairingContent) panel.querySelector('.compute-pairing-layout').replaceWith(pairingContent.cloneNode(true));
      const field = panel.querySelector('[name=machine]');
      if (field) { field.value = machine; field.defaultValue = machine; }
      showPairing(page, pairingOpen || !panel.hidden);
    }
    filterNodes(page); showDraftState(page);
  }
  document.addEventListener('input', event => {
    const page = root();
    if (!page || !page.contains(event.target)) return;
    if (event.target.id === 'node-search') { search = event.target.value; filterNodes(page); return; }
    const form = event.target.closest('form[data-node-draft]');
    if (form) { remember(form); showDraftState(page); }
  });
  document.addEventListener('change', event => {
    const page = root();
    if (!page || !page.contains(event.target)) return;
    if (event.target.matches('.compute-runtime [name=enabled]')) {
      event.target.form.querySelector('[data-runtime-options]').open = true;
    }
    if (event.target.id === 'node-filter') { filter = event.target.value; filterNodes(page); }
  });
  document.addEventListener('click', event => {
    const page = root();
    if (!page) return;
    if (event.target.closest('[data-toggle-pairing]')) { event.preventDefault(); showPairing(page, page.querySelector('#add-node').hidden, true); }
    if (event.target.closest('[data-close-pairing]')) showPairing(page, false, true);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && root() && !document.querySelector('dialog[open]') && pairingOpen) showPairing(root(), false, true);
  });
  document.addEventListener('htmx:beforeRequest', event => {
    const form = event.detail.elt?.closest('form[data-node-draft]');
    if (form) submitted.set(form, values(form));
  });
  document.addEventListener('htmx:beforeSwap', event => {
    const page = root(), detail = event.detail;
    if (!page || detail.target?.id !== 'page-content') return;
    forms(page).forEach(remember);
    const form = detail.requestConfig?.elt?.closest('form[data-node-draft]');
    if (detail.requestConfig?.verb !== 'post' || !form || detail.xhr.status >= 400) return;
    const response = new DOMParser().parseFromString(detail.xhr.responseText, 'text/html');
    if (response.querySelector('[data-compute-nodes]') && !response.querySelector('.said-box.alert-danger')) {
      const sent = submitted.get(form) || [], latest = values(form);
      const lateEdits = latest.filter((value, index) => JSON.stringify(value) !== JSON.stringify(sent[index]));
      if (lateEdits.length) drafts.get(page.dataset.machine)?.set(key(form), lateEdits);
      else drafts.get(page.dataset.machine)?.delete(key(form));
    }
  });
  document.addEventListener('htmx:afterSwap', setup);
  document.addEventListener('htmx:historyRestore', setup);
  setup();
})();
