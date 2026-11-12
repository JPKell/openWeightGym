// The server renders immutable evidence. Enhance native links, sorting, and fragment swaps.
(() => {
  'use strict';
  const $ = selector => document.querySelector(`[data-fingerprint-page] ${selector}`);
  const $$ = selector => [...document.querySelectorAll(`[data-fingerprint-page] ${selector}`)];
  let currentPage = 0;
  function paginate(reset = false) {
    const select = $('#page-size');
    if (!select) return;
    const rows = $$('#fingerprint-rows [data-record]');
    const size = select.value === 'all' ? Math.max(1, rows.length) : Number(select.value);
    if (reset) currentPage = 0;
    currentPage = Math.max(0, Math.min(currentPage, Math.ceil(rows.length / size) - 1));
    rows.forEach((row, index) => { row.hidden = index < currentPage * size || index >= (currentPage + 1) * size; });
    $('#page-summary').textContent = rows.length ? `Showing ${currentPage * size + 1}–${Math.min((currentPage + 1) * size, rows.length)} of ${rows.length}` : '0 results';
    $$('[data-fingerprint-page-step]').forEach(button => {
      button.disabled = Number(button.dataset.fingerprintPageStep) < 0 ? currentPage === 0 : (currentPage + 1) * size >= rows.length;
    });
    const exporter = $('#export-directory');
    if (exporter) exporter.disabled = !rows.length;
  }
  function ready() {
    const selected = $('#record-peek')?.dataset.selectedFingerprint;
    $$('#fingerprint-rows [data-record]').forEach(row => {
      const active = row.dataset.fingerprint === selected;
      row.dataset.selected = String(active);
      if (active) row.querySelector('[data-open]').setAttribute('aria-current', 'true');
      else row.querySelector('[data-open]').removeAttribute('aria-current');
    });
    const link = $('[data-record-link]');
    if (link) link.textContent = new URL(link.dataset.recordLink, location.origin).href;
    paginate();
  }
  document.addEventListener('htmx:beforeRequest', event => {
    const source = event.detail.elt;
    if (!source?.matches('[data-open]') || !source.closest('[data-fingerprint-page]')) return;
    const preview = $('#record-peek');
    if (!preview || !preview.getBoundingClientRect().width) {
      event.preventDefault(); location.assign(source.href);
    }
  });
  document.addEventListener('htmx:afterSwap', event => {
    if (event.detail.target.id === 'found') currentPage = 0;
    ready();
  });
  document.addEventListener('change', event => {
    if (event.target.matches('[data-fingerprint-page] #page-size')) paginate(true);
  });
  document.addEventListener('input', event => {
    if (!event.target.matches('[data-fingerprint-page] #setting-search')) return;
    const query = event.target.value.trim().toLowerCase();
    const rows = $$('#setting-rows tr');
    rows.forEach(row => { row.hidden = !row.textContent.toLowerCase().includes(query); });
    $('#no-settings').hidden = rows.some(row => !row.hidden);
  });
  document.addEventListener('click', event => {
    const row = event.target.closest('[data-fingerprint-page] #fingerprint-rows [data-record]');
    if (row && event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey &&
        !event.target.closest('a, button, input, select, textarea, label, summary, [role="button"]') &&
        !window.getSelection()?.toString()) {
      row.querySelector('[data-open]')?.click();
    }
    const step = event.target.closest('[data-fingerprint-page-step]');
    if (step) { currentPage += Number(step.dataset.fingerprintPageStep); paginate(); }
    if (event.target.closest('#fingerprints-table th[data-sort]')) {
      // sort.js has sorted the entire archive, including rows on other pages.
      queueMicrotask(() => paginate(true));
    }
    const exporter = event.target.closest('[data-fingerprint-export]');
    if (!exporter) return;
    const data = document.getElementById(exporter.dataset.fingerprintExport);
    if (!data) return;
    const value = JSON.parse(data.textContent);
    const filename = Array.isArray(value) ? 'fingerprints.json' : `fingerprint-${value.fingerprint.slice(0, 12)}.json`;
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type:'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = filename; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    $('#fingerprint-feedback').textContent = `Downloaded ${filename}.`;
  });
  // Section links keep their native anchors and also move keyboard focus to the section.
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-fingerprint-page] .fp-outline a');
    if (!link) return;
    const section = $(link.hash);
    if (section) { section.tabIndex = -1; section.focus({preventScroll:true}); }
  });
  ready();
})();
