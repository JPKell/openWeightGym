// Scoring studio. Drafts live in page memory and saves use the existing protected form route.
(() => {
  'use strict';
  const drafts = new Map(), views = new Map();
  let initialized, owner, saved = new Map(), pending = new WeakMap();
  const view = {search: '', catalogueScroll: 0};
  const root = () => document.querySelector('[data-benchmark-manage]');
  const formOf = page => page.querySelector('[data-settings-form]');
  const idOf = form => form?.elements.benchmark.value;
  const values = form => ({formula: form.elements.formula.value, green: form.elements.green.value,
    enabled: form.elements.enabled.checked, ...(form.elements.server ? {server: form.elements.server.value} : {})});
  const baseline = bench => ({formula: bench.formula, green: bench.green, enabled: bench.enabled,
    ...(bench.quality ? {server: bench.server} : {})});
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  function remember(page) {
    const form = formOf(page);
    if (!form) return;
    const id = idOf(form), now = values(form);
    if (same(now, saved.get(id))) drafts.delete(id); else drafts.set(id, now);
    views.set(id, {search: page.querySelector('#value-search').value,
      scroll: page.querySelector('.values-scroll').scrollTop});
    view.catalogueScroll = page.querySelector('.benchmark-library').scrollTop;
  }

  function marks(page) {
    page.querySelectorAll('[data-benchmark]').forEach(link => {
      link.querySelector('[data-draft]').hidden = !drafts.has(link.dataset.benchmark);
    });
    const form = formOf(page);
    if (!form) return;
    const changed = drafts.has(idOf(form));
    page.querySelector('#save-state').textContent = changed ? 'Unsaved changes' : 'All changes saved';
    page.querySelector('#discard').hidden = false;
    page.querySelector('#discard').disabled = !changed;
    // The shared submit controller owns the button while a request is active.
    if (form.getAttribute('aria-busy') !== 'true') page.querySelector('#save').disabled = !changed;
  }

  function filterBenchmarks(page) {
    const term = view.search.trim().toLowerCase();
    const choices = [...page.querySelectorAll('[data-benchmark]')];
    choices.forEach(link => { link.hidden = !link.dataset.benchmarkSearch.toLowerCase().includes(term); });
    page.querySelectorAll('[data-benchmark-group]').forEach(group => {
      group.hidden = ![...group.querySelectorAll('[data-benchmark]')].some(link => !link.hidden);
    });
    const empty = page.querySelector('#no-matches');
    if (empty) empty.hidden = choices.some(link => !link.hidden);
  }

  function filterValues(page) {
    const input = page.querySelector('#value-search');
    if (!input) return;
    const term = input.value.trim().toLowerCase(), rows = [...page.querySelectorAll('#values-panel tbody tr')];
    rows.forEach(row => { row.hidden = !row.textContent.toLowerCase().includes(term); });
    const count = rows.filter(row => !row.hidden).length;
    page.querySelector('#value-count').textContent = count ? `${count} of ${rows.length} values shown` :
      'No matching values. Clear the search to see all measurements.';
  }

  function setup() {
    const page = root();
    if (!page || page === initialized) return;
    initialized = page;
    if (owner !== page.dataset.owner) {
      owner = page.dataset.owner; drafts.clear(); views.clear();
      view.search = ''; view.catalogueScroll = 0;
    }
    saved = new Map(JSON.parse(page.querySelector('#manage-definitions').textContent).map(b => [b.id, baseline(b)]));
    for (const id of drafts.keys()) if (!saved.has(id)) { drafts.delete(id); views.delete(id); }
    const form = formOf(page);
    if (!form) return;
    const id = idOf(form), draft = drafts.get(id);
    if (draft) for (const [name, value] of Object.entries(draft)) {
      if (name === 'enabled') form.elements[name].checked = value; else form.elements[name].value = value;
    }
    page.querySelector('#benchmark-search').value = view.search;
    filterBenchmarks(page);
    page.querySelector('.benchmark-library').scrollTop = view.catalogueScroll;
    page.querySelectorAll('#values-panel tbody th code, #values-panel tbody td:first-child code').forEach(code => {
      const button = page.querySelector('#insert-value-template').content.firstElementChild.cloneNode(true);
      button.dataset.insert = code.textContent;
      button.append(code.cloneNode(true)); code.replaceWith(button);
    });
    const previous = views.get(id);
    if (previous) page.querySelector('#value-search').value = previous.search;
    filterValues(page);
    if (previous) page.querySelector('.values-scroll').scrollTop = previous.scroll;
    remember(page); marks(page);
  }

  function edited(event) {
    const page = root();
    if (!page?.contains(event.target)) return;
    if (event.target.id === 'benchmark-search') {
      view.search = event.target.value; filterBenchmarks(page);
    } else {
      if (event.target.id === 'value-search') filterValues(page);
      remember(page); marks(page);
    }
  }
  document.addEventListener('input', edited);
  document.addEventListener('change', edited);
  document.addEventListener('click', event => {
    const page = root();
    if (!page?.contains(event.target)) return;
    const form = formOf(page), button = event.target.closest('[data-insert]');
    if (button && form) {
      const field = form.elements.formula;
      field.setRangeText(button.dataset.insert, field.selectionStart, field.selectionEnd, 'end');
      field.focus(); remember(page); marks(page);
    }
    if (event.target.closest('#discard') && form) {
      for (const [name, value] of Object.entries(saved.get(idOf(form)))) {
        if (name === 'enabled') form.elements[name].checked = value; else form.elements[name].value = value;
      }
      remember(page); marks(page);
      form.elements.formula.focus();
    }
  });
  document.addEventListener('htmx:beforeRequest', event => {
    const page = root(), form = event.detail.elt?.closest('[data-settings-form]');
    if (!page?.contains(form) || event.detail.requestConfig?.verb !== 'post') return;
    remember(page);
    pending.set(event.detail.xhr, {id: idOf(form), values: values(form)});
  });
  document.addEventListener('htmx:beforeSwap', event => {
    const page = root();
    if (!page || !['benchmark-definitions', 'page-content'].includes(event.detail.target?.id)) return;
    remember(page);
    const submitted = pending.get(event.detail.xhr);
    if (!submitted) return;
    const response = new DOMParser().parseFromString(event.detail.xhr.responseText, 'text/html');
    const next = response.querySelector('[data-benchmark-manage]');
    if (!next || next.dataset.saveFailed === 'true' || next.dataset.owner !== owner) return;
    const bench = JSON.parse(next.querySelector('#manage-definitions').textContent).find(b => b.id === submitted.id);
    if (bench && same(baseline(bench), submitted.values) && same(drafts.get(submitted.id), submitted.values)) {
      drafts.delete(submitted.id);
    }
  });
  document.addEventListener('htmx:afterSwap', setup);
  document.addEventListener('htmx:historyRestore', setup);
  document.addEventListener('htmx:afterRequest', () => {
    const page = root(); if (page) marks(page);
  });
  setup();
})();
