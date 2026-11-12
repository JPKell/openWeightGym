// Enhance the server-rendered matrix. Evidence links and all nodes work without JavaScript.
(() => {
  function initialize() {
    const matrix = document.querySelector('[data-measurement-matrix]');
    if (!matrix || matrix.dataset.initialized) return;
    matrix.dataset.initialized = 'true';
    const rows = [...matrix.querySelectorAll('[data-measurement-row]')];
    const search = matrix.querySelector('[data-matrix-search]');
    const filter = matrix.querySelector('[data-matrix-filter]');
    const order = matrix.querySelector('[data-matrix-sort]');
    const display = matrix.querySelector('[data-matrix-display]');
    const table = matrix.querySelector('table');
    const columns = [...table.tHead.rows[0].cells].slice(1);
    const viewport = matrix.querySelector('.measurement-table-scroll');
    const controls = {q: search, evidence: filter, order, display};
    const defaults = {q: '', evidence: 'all', order: 'name', display: 'values'};
    const query = new URLSearchParams(location.search);
    for (const [name, control] of Object.entries(controls)) {
      const value = query.get(name);
      if (value !== null && (control === search || [...control.options].some(option => option.value === value))) control.value = value;
    }
    const mode = () => display.value;
    const higher = () => matrix.dataset[mode() === 'scores' ? 'scoreHigher' : 'valueHigher'] === 'true';
    function numeric(cell) {
      if (!cell || cell.dataset.state !== 'current') return null;
      const raw = cell.dataset[mode() === 'scores' ? 'score' : 'value'];
      return raw !== '' && Number.isFinite(Number(raw)) ? Number(raw) : null;
    }
    function rank(row) {
      const cells = [...row.cells].slice(1);
      const index = order.value.startsWith('node-') ? Number(order.value.slice(5)) : null;
      const values = (index === null ? cells : [cells[index]]).map(numeric).filter(value => value !== null);
      return values.length ? (higher() ? Math.max(...values) : -Math.min(...values)) : -Infinity;
    }
    function update() {
      const query = search.value.trim().toLocaleLowerCase();
      const sorted = [...rows].sort((a, b) => (order.value !== 'name' ? rank(b) - rank(a) : 0) || a.dataset.model.localeCompare(b.dataset.model));
      let visible = 0;
      for (const row of sorted) {
        const cells = [...row.cells].slice(1);
        const matches = filter.value === 'all' || cells.some(cell => filter.value === 'gaps'
          ? ['missing', 'failed', 'cancelled', 'unavailable'].includes(cell.dataset.state) : cell.dataset.state === filter.value);
        row.hidden = !row.dataset.search.includes(query) || !matches;
        visible += !row.hidden;
        table.tBodies[0].append(row);
        for (const cell of cells) {
          const best = cell.dataset[mode() === 'scores' ? 'bestScores' : 'bestValues'] === 'true';
          cell.toggleAttribute('data-best', best);
          const note = cell.querySelector('[data-best-note]');
          if (note) note.hidden = !best;
        }
      }
      matrix.querySelector('[data-matrix-empty]').hidden = visible > 0;
      matrix.querySelector('[data-matrix-count]').textContent = `${visible} of ${rows.length} models`;
      matrix.querySelectorAll('[data-matrix-values]').forEach(el => { el.hidden = mode() !== 'values'; });
      matrix.querySelectorAll('[data-matrix-scores]').forEach(el => { el.hidden = mode() !== 'scores'; });
      matrix.querySelector('[data-matrix-direction]').textContent = `${higher() ? 'Higher' : 'Lower'} is better.${mode() === 'scores' ? ` Score formula: ${matrix.dataset.formula}.` : ''}`;
      columns.forEach((column, index) => column.setAttribute('aria-sort', order.value === `node-${index}` ? (higher() ? 'descending' : 'ascending') : 'none'));
      const url = new URL(location.href);
      for (const [name, control] of Object.entries(controls)) {
        if (control.value !== defaults[name]) url.searchParams.set(name, control.value);
        else url.searchParams.delete(name);
      }
      history.replaceState(null, '', url);
      document.querySelectorAll('[data-matrix-navigation]').forEach(link => {
        const target = new URL(link.href);
        for (const [name, control] of Object.entries(controls)) {
          if (control.value !== defaults[name]) target.searchParams.set(name, control.value);
          else target.searchParams.delete(name);
        }
        link.href = target.href;
      });
    }
    search.addEventListener('input', update);
    for (const control of [filter, order, display]) control.addEventListener('change', update);
    matrix.querySelectorAll('[data-matrix-column]').forEach(button => button.addEventListener('click', () => {
      order.value = `node-${button.dataset.matrixColumn}`; update();
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
