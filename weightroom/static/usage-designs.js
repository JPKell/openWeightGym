// Switch between the three Usage proposals and sample scenarios, and draw what only the spend console adds.
// The chart, tables, figures, trend lines and cost bars are static/usage.js, as on the live page. It announces each
// redraw with `weightroom:usage`, carrying the report it drew, so the outcomes and leaders here follow it too.
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const descriptions = {
    briefing: 'Briefing — filters in one toolbar under the heading, a token band with 30-day trends, a cost band with its sources and pricing assumptions, then the chart and the exact tables.',
    analyst: 'Analyst — every filter and assumption in a sticky rail beside the figures, so the totals, chart and tables stay in view while you narrow them.',
    spend: 'Spend console — cost first, in a bento of tiles: where the spend comes from, the token split, request outcomes and the top models beside the chart. Filters fold into a drawer that lists the ones in use.',
  };
  const filterLabels = {user: 'User', group: 'Group', model: 'Model', source: 'Source', outcome: 'Outcome', level: 'Level'};
  let design = 'briefing', scenario = 'everyone';

  /** Shorten a count as the live KPIs do: 1.2K, 3.4M. */
  const compact = value => {
    let suffix = '';
    for (const next of ['K', 'M', 'B', 'T']) {
      if (Math.abs(value) < 999.95) break;
      value /= 1000; suffix = next;
    }
    return suffix ? `${value.toFixed(1)}${suffix}` : String(Math.round(value));
  };
  const percent = (part, whole) => whole ? `${(100 * part / whole).toFixed(part / whole < 0.1 ? 1 : 0)}%` : '—';
  const money = value => '$' + Number(value).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const stamp = id => $(`#${id}`).content.firstElementChild.cloneNode(true);
  const announce = text => { $('#preview-status').textContent = text; };

  function outcomes(report) {
    const list = $('[data-outcome-mix]');
    if (!list) return;
    const rows = report.breakdowns.outcome, all = rows.reduce((sum, row) => sum + row.requests, 0);
    list.replaceChildren(...rows.map(row => {
      const item = stamp('usage-outcome-template');
      item.dataset.outcome = row.name;
      $('.usage-outcome-name', item).textContent = row.name;
      $('.usage-outcome-bar span', item).style.inlineSize = all ? `${100 * row.requests / all}%` : '0';
      $('.usage-outcome-value', item).textContent = `${row.requests.toLocaleString()} · ${percent(row.requests, all)}`;
      return item;
    }));
    if (!rows.length) list.replaceChildren(Object.assign(document.createElement('li'), {className: 'text-secondary small', textContent: 'No requests in this range.'}));
  }

  /** The leading rows of the dimension the chart shows, or of models while it shows time. */
  function leaders(report, view, metric) {
    const list = $('[data-ranked]');
    if (!list) return;
    const dimension = view === 'trend' ? 'model' : view;
    const key = metric === 'cost' ? 'effective_cost' : metric === 'mock' ? 'total_cost' : 'total_tokens';
    const rows = [...report.breakdowns[dimension]].sort((a, b) => b[key] - a[key] || a.name.localeCompare(b.name));
    const top = rows[0]?.[key] || 0, whole = rows.reduce((sum, row) => sum + row[key], 0);
    $('#usage-leaders-heading').textContent = `Top ${dimension === 'group' ? 'groups' : dimension + 's'}`;
    list.replaceChildren(...rows.slice(0, 6).map(row => {
      const item = stamp('usage-ranked-template');
      $('.usage-ranked-name', item).textContent = row.name;
      $('.usage-ranked-value', item).textContent = `${key === 'total_tokens' ? compact(row[key]) : money(row[key])} · ${percent(row[key], whole)}`;
      $('.usage-ranked-bar span', item).style.inlineSize = top ? `${100 * row[key] / top}%` : '0';
      return item;
    }));
    if (!rows.length) list.replaceChildren(Object.assign(document.createElement('li'), {className: 'text-secondary small', textContent: 'Nothing to rank in this range.'}));
  }

  /** Which filters narrow the report, as chips in the drawer's header. usage.js counts them. */
  function filters(report) {
    const summary = $('[data-filter-summary]');
    if (!summary) return;
    const used = Object.entries(report.filters).filter(([key, value]) => value && !(key === 'user' && !report.everyone));
    summary.replaceChildren(...(used.length ? used : [['', 'All activity']]).map(([key, value]) => Object.assign(
      document.createElement('span'), {className: 'badge bg-secondary-lt', textContent: key ? `${filterLabels[key]}: ${value}` : value})));
  }

  document.addEventListener('weightroom:usage', event => {
    const {report, view, metric} = event.detail;
    outcomes(report); leaders(report, view, metric); filters(report);
  });

  // Nothing here reaches a server: say what the live page would do instead.
  document.addEventListener('submit', event => {
    if (event.target.id !== 'usage-filters') return;
    event.preventDefault();
    announce('Sample preview: on the live page, Apply reloads /usage with these filters in the address. No request was sent.');
  });
  document.addEventListener('click', event => {
    if (event.target.closest('a[href="/usage"]')) {
      event.preventDefault();
      announce('Sample preview: Reset clears every filter. The sample has been reloaded.');
      show();
      return;
    }
    const toggle = event.target.closest('[data-toggle-filters]');
    if (toggle) {
      const drawer = $('#usage-filter-drawer');
      drawer.open = !drawer.open;
      toggle.setAttribute('aria-expanded', String(drawer.open));
      if (drawer.open) $('#usage-start').focus();
    }
  });

  /** Put the chosen proposal and scenario in the workspace, and let the live scripts take it over. */
  function show() {
    const workspace = $('#workspace');
    workspace.dataset.design = design;
    workspace.replaceChildren($(`template[data-proposal="${design}"][data-scenario="${scenario}"]`).content.cloneNode(true));
    for (const button of $$('[data-design]', $('.usage-design-tabs'))) {
      const chosen = button.dataset.design === design;
      button.classList.toggle('active', chosen); button.setAttribute('aria-pressed', String(chosen));
    }
    $('#design-description').textContent = descriptions[design];
    const drawer = $('#usage-filter-drawer');
    if (drawer) drawer.addEventListener('toggle', () => $('[data-toggle-filters]')?.setAttribute('aria-expanded', String(drawer.open)));
    prepareSortableTables();
    initializeUsage();
  }

  for (const button of $$('[data-design]', $('.usage-design-tabs'))) {
    button.addEventListener('click', () => { design = button.dataset.design; history.replaceState(null, '', `#${design}`); announce(''); show(); });
  }
  $('#scenario').addEventListener('change', event => { scenario = event.target.value; announce(''); show(); });
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark';
    const control = $('[data-theme-form] [name="mode"]');
    control.value = dark ? 'dark' : 'light';
    control.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = dark ? 'Light mode' : 'Dark mode';
  });
  if (descriptions[location.hash.slice(1)]) design = location.hash.slice(1);
  show();
})();
