// Enhance server-rendered trace links; authorization and exports stay on the server.
(() => {
  'use strict';
  let saved = null;
  let holdTableUntil = 0;
  const root = () => document.querySelector('[data-log-workspace]');
  function ready() {
    const choice = document.getElementById('log-view-choice');
    if (choice && root()) choice.value = root().dataset.layout;
    const panel = document.querySelector('[data-log-page] [data-start-collapsed]');
    if (!panel) return;
    panel.hidden = true; delete panel.dataset.startCollapsed;
    document.querySelector('[data-log-more]')?.setAttribute('aria-expanded', 'false');
  }
  const tell = message => { const el = document.getElementById('log-feedback'); if (el) el.textContent = message; };
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); tell('Copied.'); }
    catch { tell('Clipboard unavailable. Select and copy the JSON or browser address.'); }
  }
  function openRow(event) {
    if (event.target.closest('a,button,input,select,textarea,summary')) return;
    const row = event.target.closest('[data-log-table-row]');
    if (!row) return;
    event.preventDefault(); row.querySelector('[data-log-table-open]')?.click();
  }
  document.addEventListener('dblclick', openRow);
  document.addEventListener('keydown', event => { if (event.key === 'Enter') openRow(event); });
  document.addEventListener('pointerdown', event => {
    if (event.target.closest('[data-log-table-row]')) holdTableUntil = performance.now() + 600;
  });
  document.addEventListener('click', event => {
    const toggle = event.target.closest('[data-log-more]');
    if (toggle) {
      const panel = document.getElementById(toggle.getAttribute('aria-controls'));
      panel.hidden = !panel.hidden; toggle.setAttribute('aria-expanded', String(!panel.hidden));
    }
    if (event.target.closest('[data-log-copy-event]')) copy(document.getElementById('detail-json')?.textContent || '{}');
    if (event.target.closest('[data-log-copy-view]')) copy(new URL(root().dataset.viewUrl, location.origin).href);
  });
  document.addEventListener('change', event => {
    if (event.target.matches('#log-start, #log-end') && document.getElementById('log-period')) document.getElementById('log-period').value = '';
    if (event.target.matches('#log-period') && event.target.value) {
      for (const id of ['log-start', 'log-end']) { const el = document.getElementById(id); el.value = ''; el.dataset.instant = ''; }
    }
  });
  document.addEventListener('htmx:beforeRequest', event => {
    const current = root();
    if (!current || event.detail.target?.id !== 'log') return;
    if (event.detail.elt === current && performance.now() < holdTableUntil) { event.preventDefault(); return; }
    saved = {selected:current.dataset.selected,
      tableTop:document.getElementById('log-table-scroll')?.scrollTop || 0,
      tableLeft:document.getElementById('log-table-scroll')?.scrollLeft || 0,
      list:document.getElementById('trace-choices')?.scrollTop || 0,
      inspector:document.querySelector('.al-inspector-slot')?.scrollTop || 0,
      raw:document.getElementById('log-raw')?.open || false,
      focus:event.detail.elt?.matches('[data-log-select]') ?
        event.detail.elt.closest('#trace-choices') ? 'trace-timeline' : 'inspector' : null};
  });
  document.addEventListener('htmx:beforeSwap', event => {
    if (event.detail.requestConfig?.elt === root() && performance.now() < holdTableUntil) event.detail.shouldSwap = false;
  });
  document.addEventListener('htmx:afterSwap', event => {
    ready();
    const current = root();
    if (!current || event.detail.target?.id !== 'log' || !saved) return;
    const choices = document.getElementById('trace-choices'), inspector = document.querySelector('.al-inspector-slot');
    const table = document.getElementById('log-table-scroll');
    if (table) { table.scrollTop = saved.tableTop; table.scrollLeft = saved.tableLeft; }
    if (choices) choices.scrollTop = saved.list;
    if (inspector && saved.selected === current.dataset.selected) inspector.scrollTop = saved.inspector;
    if (document.getElementById('log-raw')) document.getElementById('log-raw').open = saved.raw;
    if (saved.focus) {
      const heading = saved.focus === 'inspector' ? document.getElementById('detail-title') : document.getElementById('timeline-title');
      heading?.setAttribute('tabindex', '-1'); heading?.focus({preventScroll:true});
      if (matchMedia('(max-width:850px)').matches) document.getElementById(saved.focus)?.scrollIntoView({block:'center'});
    }
    saved = null;
  });
  ready();
})();
