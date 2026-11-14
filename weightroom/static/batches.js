// The server owns filtering and pagination; delegated controls survive polling swaps.
(() => {
  'use strict';
  let searchTimer, scrollLeft = 0;
  const filtersInFlight = new WeakMap();
  const filterValues = form => new URLSearchParams(new FormData(form)).toString();
  const dialog = () => document.getElementById('batch-detail-dialog');
  function initialize() {
    const root = document.getElementById('batch-workspace');
    if (!root) return;
    window.SuiteTime?.localize(root);
    root.querySelectorAll('[data-local-zone]').forEach(node => {
      node.textContent = `times in ${Intl.DateTimeFormat().resolvedOptions().timeZone}`;
    });
    const table = root.querySelector('#batches .table-responsive');
    if (table) table.scrollLeft = scrollLeft;
  }
  document.addEventListener('click', event => {
    const open = event.target.closest('[data-batch-open]');
    if (open) {
      const template = document.getElementById(open.dataset.batchOpen), panel = dialog();
      if (!template || !panel) return;
      panel.replaceChildren(template.content.cloneNode(true)); panel.showModal();
      document.dispatchEvent(new CustomEvent('htmx:afterSwap', {detail: {target: panel}}));
    }
    if (event.target.closest('[data-batch-close]')) dialog()?.close();
    if (event.target.closest('[data-batch-guide]')) {
      const guide = document.getElementById('batch-submission');
      if (guide) { guide.open = true; guide.scrollIntoView({block:'start'}); }
    }
  });
  document.addEventListener('input', event => {
    if (event.target.id !== 'batch-search') return;
    clearTimeout(searchTimer);
    const form = event.target.form;
    searchTimer = setTimeout(() => { if (form.isConnected) form.requestSubmit(); }, 350);
  });
  document.addEventListener('change', event => {
    if (event.target.matches('#batch-filters select')) {
      clearTimeout(searchTimer);
      const form = event.target.form;
      searchTimer = setTimeout(() => { if (form.isConnected) form.requestSubmit(); }, 150);
    }
  });
  document.addEventListener('submit', event => {
    if (event.target.id === 'batch-filters') clearTimeout(searchTimer);
  });
  for (const type of ['htmx:beforeRequest', 'htmx:beforeSwap']) document.addEventListener(type, event => {
    const detail = event.detail, source = detail.requestConfig?.elt;
    if (source?.id === 'batch-filters') {
      const current = document.getElementById('batch-filters');
      if (type === 'htmx:beforeRequest') filtersInFlight.set(detail.xhr, filterValues(source));
      else if (current && filtersInFlight.get(detail.xhr) !== filterValues(current)) {
        detail.shouldSwap = false;
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => { if (current.isConnected) current.requestSubmit(); }, 350);
      }
      return;
    }
    if (source?.id !== 'batches') return;
    const held = dialog()?.open || source.querySelector('[data-confirm-two][aria-label^="Are you sure?"]') ||
      source.querySelector('select:focus');
    if (held) { if (type === 'htmx:beforeSwap') detail.shouldSwap = false; else event.preventDefault(); }
    if (type === 'htmx:beforeSwap') scrollLeft = source.querySelector('.table-responsive')?.scrollLeft || 0;
  });
  document.addEventListener('htmx:afterSwap', initialize);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {once:true});
  else initialize();
})();
