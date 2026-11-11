// The node matrix on a model's page: ticked node columns are edited together, cells that differ
// between nodes are marked, groups collapse, and Move takes one destination.
(() => {
  'use strict';
  const controls = cell => [...cell.querySelectorAll('input, select, textarea')].filter(input => input.type !== 'hidden');
  // A cell's value as the form would send it, for comparing one node with another.
  const valueOf = cell => controls(cell).map(input =>
    input.type === 'checkbox' || input.type === 'radio' ? (input.checked ? input.value : null) : input.value.trim()).filter(value => value !== null).join('\u0000');
  const picked = table => new Set([...table.querySelectorAll('[data-matrix-pick]:checked')].map(box => box.value));

  function mark(table) {
    const nodes = picked(table);
    table.querySelectorAll('.matrix-cell[data-node], .matrix-node[data-node]').forEach(cell => cell.classList.toggle('is-picked', nodes.size > 1 && nodes.has(cell.dataset.node)));
    table.querySelectorAll('[data-matrix-row]').forEach(row => {
      const cells = [...row.querySelectorAll('.matrix-cell[data-node]')];
      const values = cells.map(valueOf), common = values[cells.findIndex(cell => cell.classList.contains('is-selected'))] ?? values[0];
      cells.forEach((cell, index) => cell.classList.toggle('differs', cells.length > 1 && values[index] !== common));
      row.dataset.differs = String(new Set(values).size > 1);
    });
  }

  // A change in a ticked column is made in the other ticked columns too, control by control.
  function spread(event) {
    const cell = event.target.closest('.matrix-cell[data-node]'), table = cell?.closest('[data-model-matrix]');
    if (!table) return;
    const nodes = picked(table);
    if (nodes.size > 1 && nodes.has(cell.dataset.node)) {
      const position = controls(cell).indexOf(event.target);
      cell.closest('tr').querySelectorAll('.matrix-cell[data-node]').forEach(other => {
        if (other === cell || !nodes.has(other.dataset.node)) return;
        const twin = controls(other)[position];
        if (!twin || twin.type !== event.target.type) return;
        if (twin.type === 'checkbox' || twin.type === 'radio') {
          if (twin.checked === event.target.checked) return;
          twin.checked = event.target.checked;
        } else {
          if (twin.value === event.target.value) return;
          twin.value = event.target.value;
        }
        twin.dispatchEvent(new Event(event.type === 'input' && twin.type !== 'checkbox' && twin.type !== 'radio' ? 'input' : 'change', { bubbles: true }));
      });
    }
    mark(table);
  }

  function transfer(form) {
    const move = form.querySelector('[data-transfer]:checked')?.value === 'move';
    const targets = [...form.querySelectorAll('[data-transfer-target]')];
    const chosen = targets.filter(box => box.checked);
    const button = form.querySelector('[data-transfer-submit]');
    if (button) button.textContent = `${move ? 'Move' : 'Copy'} to ${chosen.length > 1 ? `${chosen.length} nodes` : 'node'}`;
    if (targets[0]) targets[0].setCustomValidity(!chosen.length ? 'Choose a node.' : move && chosen.length > 1 ? 'A move takes the model to one node.' : '');
  }

  document.addEventListener('input', spread);
  document.addEventListener('change', event => {
    if (event.target.matches('[data-matrix-pick]')) mark(event.target.closest('[data-model-matrix]'));
    else if (event.target.matches('[data-matrix-differences]')) {
      document.querySelectorAll('[data-model-matrix]').forEach(table => table.classList.toggle('only-differences', event.target.checked));
    } else if (event.target.closest('[data-model-transfer]')) {
      const form = event.target.closest('[data-model-transfer]');
      // A move has one destination: choosing one replaces the last.
      if (event.target.matches('[data-transfer-target]') && event.target.checked && form.querySelector('[data-transfer]:checked')?.value === 'move') {
        form.querySelectorAll('[data-transfer-target]:checked').forEach(box => { if (box !== event.target) box.checked = false; });
      }
      if (event.target.matches('[data-transfer]') && event.target.value === 'move') {
        form.querySelectorAll('[data-transfer-target]:checked').forEach((box, index) => { if (index) box.checked = false; });
      }
      transfer(form);
    } else spread(event);
  });
  document.addEventListener('click', event => {
    const toggle = event.target.closest('[data-matrix-toggle]');
    if (!toggle) return;
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(open));
    toggle.closest('tbody').classList.toggle('is-collapsed', !open);
  });
  const update = () => {
    document.querySelectorAll('[data-model-matrix]').forEach(mark);
    document.querySelectorAll('[data-model-transfer]').forEach(transfer);
  };
  document.addEventListener('htmx:afterSwap', update);
  update();
})();
