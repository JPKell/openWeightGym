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
  };
  document.addEventListener('change', event => {
    const form = event.target.closest('form[data-file-boxes]');
    if (!form) return;
    const column = event.target.dataset.checkColumn;
    if (column !== undefined) {
      form.querySelectorAll(`input[name="want"][data-column="${CSS.escape(column)}"]:not(:disabled)`)
        .forEach(box => { box.checked = event.target.checked; });
    }
    summarize(form);
  });
})();
