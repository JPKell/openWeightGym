// The heatmap retains the full catalogue; GET filters replace only the measurement workspace.
(() => {
  if (window.weightroomResultsReady) return;
  window.weightroomResultsReady = true;
  let searchTimer;
  const positions = new Map();
  const table = () => document.getElementById('benchmark-results');
  function restore() {
    const grid = table();
    if (!grid) return;
    const baseline = document.querySelector('#results-filters [name="baseline"]')?.value;
    grid.querySelectorAll('tbody [data-model]').forEach(row => {
      row.classList.toggle('baseline-row', row.dataset.model === baseline);
    });
    const saved = positions.get(grid.dataset.benchmark);
    if (saved) grid.parentElement.scrollTo(saved.left, saved.top);
  }
  document.addEventListener('input', event => {
    if (!event.target.matches('#results-filters #search-models')) return;
    clearTimeout(searchTimer);
    const form = event.target.form;
    searchTimer = setTimeout(() => { if (form.isConnected) form.requestSubmit(); }, 300);
  });
  document.addEventListener('change', event => {
    if (event.target.closest('#results-filters')) clearTimeout(searchTimer);
  });
  document.addEventListener('submit', event => {
    if (event.target.id === 'results-filters') clearTimeout(searchTimer);
  });
  document.addEventListener('click', event => {
    const toggle = event.target.closest('[data-score-table-toggle]');
    if (toggle) {
      const expanded = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', String(expanded));
      document.getElementById('benchmark-score-table')?.classList.toggle('show-score-table', expanded);
    }
    const jump = event.target.closest('#benchmark-measurements [data-scroll-metric]');
    if (jump) {
      const grid = table(), heading = grid?.querySelector(`[data-metric="${jump.dataset.scrollMetric}"]`);
      if (heading) grid.parentElement.scrollTo({left: Math.max(0, heading.offsetLeft - grid.rows[1].cells[0].offsetWidth), behavior: 'smooth'});
    }
    const column = event.target.closest('#benchmark-score-table thead th[data-bench]');
    const form = document.getElementById('results-filters');
    if (column && form) {
      event.preventDefault();
      form.elements.benchmark.value = column.dataset.bench;
      form.requestSubmit();
    }
  });
  document.addEventListener('htmx:beforeSwap', event => {
    if (event.detail.target?.id !== 'benchmark-measurements') return;
    const grid = table();
    if (grid) positions.set(grid.dataset.benchmark, {left: grid.parentElement.scrollLeft, top: grid.parentElement.scrollTop});
  });
  document.addEventListener('htmx:afterSwap', restore);
  restore();
})();
