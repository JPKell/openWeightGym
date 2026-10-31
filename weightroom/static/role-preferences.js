// Each role keeps its own benchmark weights. Edits remain local until its form is saved.
(() => {
  if (window.suiteRolePreferences) return;
  window.suiteRolePreferences = true;
  /** Recalculate the role form's visible preference values and availability. */
  const update = (form, edited = false) => {
    if (edited) form.dataset.weightsEdited = 'true';
    const fields = [...form.querySelectorAll('[data-benchmark-id]')];
    const values = fields.map(field => field.value.trim() === '' ? 0 : Number(field.value));
    const invalid = values.some(value => !Number.isFinite(value) || value < 0 || value > 1e100);
    const scale = invalid ? 0 : Math.max(0, ...values);
    const total = scale ? values.reduce((sum, value) => sum + value / scale, 0) : 0;
    fields.forEach((field, index) => {
      const output = [...form.querySelectorAll('[data-benchmark-share]')].find(one => one.dataset.benchmarkShare === field.dataset.benchmarkId);
      if (output) output.textContent = invalid ? 'Invalid weights' : values[index] > 0 ? `${((values[index] / scale) / total * 100).toFixed(1)}% share` : 'Ignored';
    });
    const status = form.querySelector('[data-benchmark-status]');
    if (status) status.textContent = invalid ? 'Use finite weights from 0 to 1e100.' :
      `${values.filter(value => value > 0).length} benchmarks selected.${total ? '' : ' Manual model weights decide quality order.'}${form.dataset.weightsEdited ? ' Unsaved changes: save this role to update its model preview.' : ''}`;
  };
  document.addEventListener('input', event => {
    if (event.target.matches('[data-benchmark-id]')) update(event.target.form, true);
  });
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-benchmark-preset]');
    if (!button) return;
    const weights = JSON.parse(button.dataset.benchmarkPreset);
    button.form.querySelectorAll('[data-benchmark-id]').forEach(field => { field.value = weights[field.dataset.benchmarkId] || 0; });
    update(button.form, true);
  });
  /** Attach role preference controls to newly rendered forms. */
  const initialize = () => document.querySelectorAll('[data-benchmark-weights]').forEach(section => update(section.closest('form')));
  document.addEventListener('htmx:afterSwap', initialize);
  initialize();
})();
