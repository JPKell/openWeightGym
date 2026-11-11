// The Storage view on Files: a box for each place a file can be (see storage_table in
// pages/files_parts.html). A box in a column's heading ticks or clears every box in that column
// that can be changed, and the line by Apply changes counts what will be copied and removed.
// The page works without this: the form sends every box either way.
(() => {
  /** Count the boxes changed since the form was drawn and say what Apply changes will do. */
  const summarize = form => {
    const summary = form.querySelector('[data-file-boxes-summary]');
    if (!summary) return;
    summary.dataset.original ??= summary.textContent;
    let copies = 0, removals = 0;
    form.querySelectorAll('input[name="want"]:not(:disabled)').forEach(box => {
      if (box.checked && !box.defaultChecked) copies++;
      if (!box.checked && box.defaultChecked) removals++;
    });
    const parts = [];
    if (copies) parts.push(`${copies} ${copies === 1 ? 'copy' : 'copies'} to make`);
    if (removals) parts.push(`${removals} ${removals === 1 ? 'copy' : 'copies'} to remove`);
    summary.textContent = parts.length ? parts.join(', ') + '. The storage copy always stays.' : summary.dataset.original;
    if (form.matches('[data-file-matrix]')) updateMatrix(form, copies + removals);
  };
  const updateMatrix = (form, pending = 0) => {
    const rows = [...form.querySelectorAll('[data-file-row]')];
    const source = form.querySelector('[data-file-source]').value;
    const destination = form.querySelector('[data-file-destination]')?.value;
    const search = form.querySelector('[data-file-search]').value.trim().toLowerCase();
    rows.forEach(row => {
      row.hidden = !row.dataset.name.includes(search) || Boolean(source && !JSON.parse(row.dataset.copies).includes(source));
      if (row.hidden) row.querySelector('[name="selected"]').checked = false;
    });
    const visible = rows.filter(row => !row.hidden);
    const selected = visible.filter(row => row.querySelector('[name="selected"]').checked);
    const all = form.querySelector('[data-select-files]');
    all.checked = visible.length > 0 && selected.length === visible.length;
    all.indeterminate = selected.length > 0 && selected.length < visible.length;
    all.disabled = !visible.length;
    form.querySelector('[data-selected-count]').textContent = selected.length ? `${selected.length} ${selected.length === 1 ? 'file' : 'files'} selected` : `${visible.length} files · select to transfer`;
    let empty = form.querySelector('[data-file-empty]');
    if (!visible.length && rows.length && !empty) {
      empty = document.querySelector('#empty-row-template').content.firstElementChild.cloneNode(true);
      empty.dataset.fileEmpty = '';
      empty.firstElementChild.colSpan = form.querySelectorAll('thead th').length;
      empty.firstElementChild.textContent = 'No files match this search or location.';
      form.querySelector('tbody').append(empty);
    }
    if (empty) empty.hidden = visible.length > 0;
    const operation = form.querySelector('[data-file-operation]');
    if (!operation) return;
    const moving = source && source !== 'storage';
    operation.querySelector('[value="move"]').disabled = !moving;
    if (!moving) operation.value = 'copy';
    const columns = [...form.querySelectorAll('[name="column"]')].map(field => field.value);
    const locked = (row, column, removing) => {
      const box = row.querySelector(`[name="want"][data-column="${columns.indexOf(column)}"]`);
      return !box || box.disabled && (removing || !box.checked);
    };
    const protectedSelection = selected.some(row => locked(row, destination, false) || operation.value === 'move' && locked(row, source, true));
    const button = form.querySelector('[data-transfer-files]');
    form.querySelector('[data-matrix-changes]').hidden = !pending;
    form.querySelector('[data-matrix-changes]').classList.toggle('d-flex', pending > 0);
    button.disabled = form.dataset.arranging === 'true' || !selected.length || source === destination || protectedSelection || pending > 0;
    button.textContent = `${operation.value === 'move' ? 'Move' : 'Copy'} selected${selected.length ? ` (${selected.length})` : ''}`;
    form.querySelector('[data-transfer-note]').textContent = pending ? 'Apply or undo the matrix checkbox changes before transferring selected files.'
      : source === destination ? 'Choose a different destination.'
      : protectedSelection ? 'A selected copy is in use, protected, or unavailable at the destination. Choose another action or selection.'
      : operation.value === 'move' ? 'Source copies stay until destination copies succeed. Storage originals always stay.'
      : 'Storage originals always stay.';
  };
  document.addEventListener('change', event => {
    const form = event.target.closest('form[data-file-boxes]');
    if (!form) return;
    const column = event.target.dataset.checkColumn;
    if (column !== undefined) {
      form.querySelectorAll(`input[name="want"][data-column="${CSS.escape(column)}"]:not(:disabled)`)
        .forEach(box => { box.checked = event.target.checked; });
    }
    if (event.target.matches('[data-select-files]')) {
      form.querySelectorAll('[data-file-row]:not([hidden]) [name="selected"]').forEach(box => { box.checked = event.target.checked; });
    }
    if (event.target.matches('select')) form.querySelector('[data-file-view-state]').value = 'edited';
    summarize(form);
  });
  document.addEventListener('input', event => {
    const form = event.target.closest('[data-file-matrix]');
    if (form && event.target.matches('[data-file-search]')) summarize(form);
  });
  const initialize = () => document.querySelectorAll('[data-file-matrix]').forEach(summarize);
  initialize();
  document.addEventListener('htmx:afterSwap', initialize);
  document.addEventListener('htmx:oobAfterSwap', initialize);
})();
