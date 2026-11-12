// Individual-file inspector. Forms continue to use existing server actions without JavaScript.
(() => {
  'use strict';
  if (window.weightroomFileInspector) return;
  window.weightroomFileInspector = true;

  function filterMetadata(root) {
    const query = root.querySelector('[data-file-metadata-search]').value.toLowerCase().split(/\s+/).filter(Boolean);
    const group = root.querySelector('[data-file-metadata-group]').value;
    const rows = [...root.querySelectorAll('[data-file-metadata-row]')];
    let count = 0;
    rows.forEach(row => {
      row.hidden = (group !== 'all' && row.dataset.group !== group) || !query.every(word => row.textContent.toLowerCase().includes(word));
      if (!row.hidden) count++;
    });
    root.querySelector('[data-file-metadata-count]').textContent = `${count} / ${rows.length} entries`;
    root.querySelector('[data-file-metadata-empty]').hidden = count !== 0;
  }
  function distribution(form) {
    const count = form.querySelectorAll('input[name="machine"]:checked:not(:disabled)').length;
    const status = form.querySelector('[data-file-selection]'), button = form.querySelector('button[value="distribute"]');
    if (status) status.textContent = `${count} node${count === 1 ? '' : 's'} selected`;
    if (button) button.disabled = !count;
  }
  function transfer(form) {
    const move = form.querySelector('[name="do"]').value === 'move';
    form.querySelector('[data-file-transfer-submit]').textContent = move ? 'Move file' : 'Copy file';
    form.querySelector('[data-file-transfer-note]').textContent = move
      ? 'Move verifies the copy, updates model paths, then removes the original. Loaded models keep answering.'
      : 'Copy keeps the original. Existing model paths stay the same.';
  }
  function initialize() {
    document.querySelectorAll('[data-file-distribute]').forEach(distribution);
    document.querySelectorAll('[data-file-transfer]').forEach(transfer);
  }
  document.addEventListener('input', event => {
    if (event.target.matches('[data-file-metadata-search]')) filterMetadata(event.target.closest('[data-file-metadata]'));
  });
  document.addEventListener('change', event => {
    if (event.target.matches('[data-file-metadata-group]')) filterMetadata(event.target.closest('[data-file-metadata]'));
    const distribute = event.target.closest('[data-file-distribute]');
    if (distribute) distribution(distribute);
    const form = event.target.closest('[data-file-transfer]');
    if (form) transfer(form);
  });
  // The backend treats an empty list as every node; never submit an empty UI selection.
  document.addEventListener('submit', event => {
    if (event.target.matches('[data-file-distribute]') && !event.target.querySelector('input[name="machine"]:checked:not(:disabled)')) event.preventDefault();
  });
  document.addEventListener('htmx:afterSwap', initialize);
  initialize();
})();
