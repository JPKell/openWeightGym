// Preserve evidence navigation across live fragment swaps; content and permissions stay server-owned.
(() => {
  'use strict';
  const selections = new Map();
  const panels = ['diagnostics', 'content', 'activity', 'raw'];
  function choose(root, name) {
    const selected = panels.includes(name) ? name : 'diagnostics';
    selections.set(root.dataset.requestId, selected);
    root.querySelectorAll('[data-request-panel]').forEach(panel => { panel.hidden = panel.dataset.requestPanel !== selected; });
    root.querySelectorAll('[data-request-tab]').forEach(button => {
      const active = button.dataset.requestTab === selected;
      button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    });
  }
  function initialize() {
    const root = document.querySelector('#request[data-request-id]');
    const anchorTab = location.hash === '#request-related' || location.hash === '#request-activity-panel' ? 'activity' : location.hash === '#request-content-panel' ? 'content' : location.hash === '#request-raw-panel' ? 'raw' : 'diagnostics';
    if (root) choose(root, selections.get(root.dataset.requestId) || anchorTab);
    const back = document.querySelector('[data-request-back]');
    try {
      const saved = sessionStorage.getItem('weightroom-requests-return');
      if (back && (saved === '/requests' || saved?.startsWith('/requests?'))) back.href = saved;
    } catch { /* The unfiltered return link remains usable when browser storage is unavailable. */ }
  }
  document.addEventListener('click', async event => {
    const tab = event.target.closest('[data-request-tab]');
    if (tab) { choose(tab.closest('#request'), tab.dataset.requestTab); return; }
    const section = event.target.closest('[data-open-request-tab]');
    if (section) choose(section.closest('#request'), section.dataset.openRequestTab);
    const record = event.target.closest('#recent-requests a[href^="/requests/"]');
    if (record) try { sessionStorage.setItem('weightroom-requests-return', location.pathname + location.search); } catch { /* Browser storage is optional. */ }
    const button = event.target.closest('[data-request-copy]');
    if (!button) return;
    const root = button.closest('#request'), feedback = root.querySelector('[data-request-feedback]');
    try {
      await navigator.clipboard.writeText(button.dataset.requestCopy === 'link' ? location.href : root.querySelector('#request-raw-metadata').textContent);
      feedback.textContent = button.dataset.requestCopy === 'link' ? 'Request link copied.' : 'Request metadata copied.';
    } catch { feedback.textContent = 'Clipboard unavailable. Use Export metadata to save the evidence.'; }
  });
  document.addEventListener('htmx:load', initialize);
  document.addEventListener('DOMContentLoaded', initialize);
  initialize();
})();
