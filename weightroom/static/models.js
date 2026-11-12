/* Filters stay outside the polling fragment; replacements retain sorting, scroll and open details. */
(() => {
  'use strict';
  const field = name => document.getElementById(`catalogue-${name}`);
  const expanded = new Set();
  let scroll = {top:0, left:0};
  function apply(restore = false) {
    const table = document.getElementById('models-table');
    if (!table || !field('search')) return;
    const query = field('search').value.trim().toLocaleLowerCase();
    const status = field('status').value, sort = field('sort').value;
    const rows = [...table.querySelectorAll('[data-catalogue-row]')];
    const number = (row, name, fallback = 0) => row.dataset[name] === '' ? fallback : Number(row.dataset[name]);
    const ordered = [...rows].sort((a,b) => {
      const difference = sort === 'coverage' ? number(b,'coverage') - number(a,'coverage') :
        sort === 'size' ? number(a,'size',Infinity) - number(b,'size',Infinity) :
        sort === 'order' ? number(a,'order',10) - number(b,'order',10) : 0;
      return difference || a.dataset.name.localeCompare(b.dataset.name, undefined, {numeric:true});
    });
    if (ordered.some((row,index) => row !== rows[index])) {
      const body = table.tBodies[0];
      for (const row of ordered) body.insertBefore(row, body.querySelector('[data-empty-row]'));
    }
    let visible = 0;
    for (const row of ordered) {
      const states = row.dataset.states.split(' ');
      const matches = status === 'all' || (status === 'active' ? states.some(s => ['loaded','in-use'].includes(s)) : states.includes(status));
      row.hidden = !matches || !row.dataset.search.toLocaleLowerCase().includes(query);
      if (!row.hidden) visible++;
      const detail = row.querySelector('details');
      if (restore && detail && expanded.has(row.dataset.name)) detail.open = true;
      if (detail) row.setAttribute('aria-expanded',String(detail.open));
    }
    const empty = table.querySelector('[data-catalogue-empty]');
    if (empty) empty.hidden = visible > 0 || !rows.length;
    const count = document.querySelector('[data-catalogue-count]');
    if (count) count.textContent = `${visible} of ${rows.length} models`;
    if (restore) {
      const region = table.closest('.inventory-table-scroll');
      region.scrollTop = scroll.top; region.scrollLeft = scroll.left;
    }
  }
  for (const name of ['search','status','sort']) field(name)?.addEventListener(name === 'search' ? 'input' : 'change',() => apply());
  function toggle(row) {
    const detail = row.querySelector('[data-catalogue-details]');
    if (!detail) return;
    detail.open = !detail.open;
    row.setAttribute('aria-expanded',String(detail.open));
  }
  document.addEventListener('click',event => {
    if (event.target.closest('[data-catalogue-clear]')) {
      field('search').value = ''; field('status').value = 'all'; apply(); field('search').focus();
      return;
    }
    const row = event.target.closest('#models-table [data-catalogue-row]');
    if (!row || event.defaultPrevented || event.target.closest('a, button, input, select, textarea, label, summary, [contenteditable]')) return;
    if (window.getSelection()?.toString()) return;
    toggle(row);
  });
  document.addEventListener('keydown',event => {
    const row = event.target.closest('#models-table [data-catalogue-row]');
    if (!row || event.target !== row || !['Enter',' '].includes(event.key)) return;
    event.preventDefault(); toggle(row);
  });
  document.addEventListener('toggle',event => {
    if (!event.target.matches('[data-catalogue-details]')) return;
    event.target.closest('[data-catalogue-row]')?.setAttribute('aria-expanded',String(event.target.open));
  },true);
  document.addEventListener('htmx:beforeSwap',event => {
    if (event.detail.target?.id !== 'models') return;
    const region = document.querySelector('#models .inventory-table-scroll');
    if (!region) return;
    scroll = {top:region.scrollTop, left:region.scrollLeft};
    expanded.clear();
    document.querySelectorAll('#models [data-catalogue-row]').forEach(row => {
      if (row.querySelector('details')?.open) expanded.add(row.dataset.name);
    });
  });
  document.addEventListener('htmx:afterSwap',event => { if (event.detail.target?.id === 'models') apply(true); });
  apply();
})();
