/* Preserve the local filter and keep node controls current across status polling. */
(() => {
  const update = () => {
    const root = document.getElementById('status');
    if (!root) return;
    const search = document.getElementById('status-search');
    const query = search?.value.trim().toLowerCase() || '';
    root.querySelectorAll('[data-status-search]').forEach(row => row.hidden = !row.dataset.statusSearch.includes(query));
    root.querySelectorAll('#current-requests tbody').forEach(body => {
      const rows = [...body.querySelectorAll('[data-status-search]')];
      const empty = body.querySelector('.status-empty');
      if (empty) {
        empty.dataset.original ||= empty.textContent.trim();
        empty.textContent = query ? 'No matching requests.' : empty.dataset.original;
        empty.classList.toggle('d-none', rows.some(row => !row.hidden));
      }
    });
    const scope = document.getElementById('status-control-machine');
    if (scope) {
      const nodes = [...root.querySelectorAll('[data-status-node]')];
      const selected = scope.value;
      scope.replaceChildren(...nodes.map(node => {
        const option = new Option(node.dataset.statusNode + (node.dataset.statusLive === 'true' ? '' : ' (unavailable)'), node.dataset.statusNode);
        option.disabled = node.dataset.statusLive !== 'true';
        return option;
      }));
      if (nodes.some(node => node.dataset.statusNode === selected)) scope.value = selected;
      const available = nodes.some(node => node.dataset.statusNode === scope.value && node.dataset.statusLive === 'true');
      scope.form.querySelectorAll('button').forEach(button => button.disabled = !available);
    }
  };
  document.addEventListener('input', event => { if (event.target.id === 'status-search') update(); });
  document.addEventListener('change', event => { if (event.target.id === 'status-control-machine') update(); });
  document.addEventListener('htmx:afterSwap', update);
  update();
})();
