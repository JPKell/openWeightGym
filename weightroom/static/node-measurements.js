// Enhance the server-rendered matrix. Evidence links and all nodes work without JavaScript.
(() => {
  function initialize() {
    const matrix = document.querySelector('[data-measurement-matrix]');
    if (!matrix || matrix.dataset.initialized) return;
    matrix.dataset.initialized = 'true';
    const rows = [...matrix.querySelectorAll('[data-measurement-row]')];
    let sortColumn = null;
    const display = matrix.querySelector('[data-matrix-display]');
    const table = matrix.querySelector('table');
    const columns = [...table.tHead.rows[0].cells].slice(1);
    const viewport = matrix.querySelector('.measurement-table-scroll');
    const controls = {display};
    const defaults = {display: 'values'};
    const query = new URLSearchParams(location.search);
    for (const [name, control] of Object.entries(controls)) {
      const value = query.get(name);
      if (value !== null && [...control.options].some(option => option.value === value)) control.value = value;
    }
    const mode = () => display.value;
    const higher = () => matrix.dataset[mode() === 'scores' ? 'scoreHigher' : 'valueHigher'] === 'true';
    function numeric(cell) {
      if (!cell || cell.dataset.state !== 'current') return null;
      const raw = cell.dataset[mode() === 'scores' ? 'score' : 'value'];
      return raw !== '' && Number.isFinite(Number(raw)) ? Number(raw) : null;
    }
    function rank(row) {
      const value = numeric(row.cells[sortColumn + 1]);
      return value === null ? -Infinity : higher() ? value : -value;
    }
    function update() {
      const sorted = [...rows].sort((a, b) => (sortColumn !== null ? rank(b) - rank(a) : 0) || a.dataset.model.localeCompare(b.dataset.model));
      for (const row of sorted) {
        const cells = [...row.cells].slice(1);
        table.tBodies[0].append(row);
        for (const cell of cells) {
          const best = cell.dataset[mode() === 'scores' ? 'bestScores' : 'bestValues'] === 'true';
          cell.toggleAttribute('data-best', best);
          const note = cell.querySelector('[data-best-note]');
          if (note) note.hidden = !best;
        }
      }
      matrix.querySelector('[data-matrix-count]').textContent = `${rows.length} models`;
      matrix.querySelectorAll('[data-matrix-values]').forEach(el => { el.hidden = mode() !== 'values'; });
      matrix.querySelectorAll('[data-matrix-scores]').forEach(el => { el.hidden = mode() !== 'scores'; });
      columns.forEach((column, index) => column.setAttribute('aria-sort', sortColumn === index ? (higher() ? 'descending' : 'ascending') : 'none'));
      const setupDisplay = document.querySelector('#benchmark-choice [name="display"]');
      if (setupDisplay) setupDisplay.value = mode();
      const url = new URL(location.href);
      for (const name of ['q', 'evidence', 'order']) url.searchParams.delete(name);
      for (const [name, control] of Object.entries(controls)) {
        if (control.value !== defaults[name]) url.searchParams.set(name, control.value);
        else url.searchParams.delete(name);
      }
      history.replaceState(null, '', url);
      document.querySelectorAll('[data-matrix-navigation]').forEach(link => {
        const target = new URL(link.href);
        for (const name of ['q', 'evidence', 'order']) target.searchParams.delete(name);
        for (const [name, control] of Object.entries(controls)) {
          if (control.value !== defaults[name]) target.searchParams.set(name, control.value);
          else target.searchParams.delete(name);
        }
        link.href = target.href;
      });
    }
    display.addEventListener('change', update);
    matrix.querySelectorAll('[data-matrix-column]').forEach(button => button.addEventListener('click', () => {
      sortColumn = Number(button.dataset.matrixColumn); update();
    }));
    matrix.querySelectorAll('[data-matrix-node]').forEach(link => link.addEventListener('click', event => {
      event.preventDefault();
      const column = columns[Number(link.dataset.matrixNode)];
      const firstWidth = table.tHead.rows[0].cells[0].getBoundingClientRect().width;
      viewport.scrollLeft += column.getBoundingClientRect().left - viewport.getBoundingClientRect().left - firstWidth;
      column.querySelector('button').focus({preventScroll: true});
      viewport.scrollIntoView({block: 'nearest'});
    }));
    update();
  }
  initialize();
  document.addEventListener('htmx:load', initialize);
})();
