// Enhance server-rendered trace links; authorization and exports stay on the server.
(() => {
  'use strict';
  let saved = null;
  const root = () => document.querySelector('[data-log-workspace]');
  function ready() {
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
    saved = {selected:current.dataset.selected,
      list:document.getElementById('trace-choices')?.scrollTop || 0,
      inspector:document.querySelector('.al-inspector-slot')?.scrollTop || 0,
      raw:document.getElementById('log-raw')?.open || false,
      focus:event.detail.elt?.matches('[data-log-select]') ?
        event.detail.elt.closest('#trace-choices') ? 'trace-timeline' : 'inspector' : null};
  });
  document.addEventListener('htmx:afterSwap', event => {
    ready();
    const current = root();
    if (!current || event.detail.target?.id !== 'log' || !saved) return;
    const choices = document.getElementById('trace-choices'), inspector = document.querySelector('.al-inspector-slot');
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
