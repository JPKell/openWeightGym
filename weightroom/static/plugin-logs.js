/* Enhance server-rendered metadata; filters, selection and downloads also work without JS. */
(() => {
  'use strict';
  if (window.weightroomPluginLogs) return;
  window.weightroomPluginLogs = true;
  const wide = matchMedia('(min-width:1200px)');

  function ready(event) {
    const root = document.querySelector('[data-plugin-logs]');
    if (!root) return;
    const aside = root.querySelector('#plugin-detail');
    const dialog = root.querySelector('#plugin-detail-dialog');
    const slot = root.querySelector('#plugin-dialog-slot');
    const record = aside.querySelector('#record-detail') || slot.querySelector('#record-detail');
    if (!record) { aside.hidden = true; return; }
    const selected = record.dataset.selectedRecord;
    for (const row of root.querySelectorAll('[data-record-row]')) {
      const active = row.dataset.recordId === selected;
      row.dataset.selected = String(active);
      const link = row.querySelector('[data-open]');
      if (active) link?.setAttribute('aria-current', 'true'); else link?.removeAttribute('aria-current');
    }
    record.querySelector('#copy-record').hidden = false;
    record.querySelector('#close-detail').hidden = wide.matches;
    if (wide.matches) {
      if (dialog.open) dialog.close();
      if (!aside.contains(record)) aside.prepend(record);
      slot.replaceChildren();
      aside.hidden = false;
    } else {
      if (!slot.contains(record)) slot.replaceChildren(record);
      aside.hidden = true;
      const selectedByClick = event?.detail?.target?.id === 'plugin-detail';
      const deepLink = !event && new URLSearchParams(location.search).has('record');
      if ((selectedByClick || deepLink) && !dialog.open) dialog.showModal();
    }
  }

  document.addEventListener('htmx:afterSwap', ready);
  wide.addEventListener('change', () => ready({}));
  document.addEventListener('click', async event => {
    const root = event.target.closest('[data-plugin-logs]');
    if (!root) return;
    if (event.target.closest('#close-detail')) root.querySelector('#plugin-detail-dialog').close();
    if (event.target.closest('#copy-record')) {
      const json = root.querySelector('[data-selected-json]');
      const feedback = root.querySelector('#record-feedback');
      if (json) {
        try {
          await navigator.clipboard.writeText(JSON.stringify(JSON.parse(json.textContent), null, 2));
          feedback.textContent = 'Record JSON copied.';
        } catch { feedback.textContent = 'Copy unavailable. Use Download JSON.'; }
      }
    }
    const row = event.target.closest('[data-record-row]');
    if (row && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey &&
        !event.target.closest('a, button, input, select, label') && !window.getSelection()?.toString()) {
      row.querySelector('[data-open]')?.click();
    }
  });
  ready();
})();
