// Filters and unsent forms stay local to this tab. Every operation uses the existing server forms.
(() => {
  'use strict';
  if (window.evaluationStudioReady) return;
  window.evaluationStudioReady = true;
  const drafts = new Map();
  let initialized = null;
  const root = () => document.querySelector('[data-evaluation-studio]');
  const fields = form => [...form.elements].filter(field => field.name && !['file', 'hidden', 'submit', 'button'].includes(field.type));
  function remember(form) {
    drafts.set(form.dataset.evaluationDraft, fields(form).map(field => ({name: field.name, value: field.value})));
  }
  function filter(page, updateUrl = false) {
    const form = page.querySelector('[data-evaluation-filter]');
    if (!form) return;
    const query = form.elements.q.value.trim().toLowerCase(), state = form.elements.state.value;
    const rows = [...page.querySelectorAll('[data-evaluation-row]')];
    for (const row of rows) row.hidden = !row.dataset.search.toLowerCase().includes(query) || !!state && row.dataset.state !== state;
    const count = rows.filter(row => !row.hidden).length;
    const counter = page.querySelector('[data-evaluation-count]');
    if (counter) counter.textContent = `${count} of ${rows.length} ${page.dataset.evaluationStudio === 'collections' ? 'collections' : 'runs'}`;
    const empty = page.querySelector('[data-evaluation-empty]');
    if (empty) empty.hidden = count > 0;
    const url = new URL(location.href);
    for (const key of ['q', 'state']) {
      const value = form.elements[key].value;
      if (value) url.searchParams.set(key, value); else url.searchParams.delete(key);
    }
    if (updateUrl) history.replaceState(history.state, '', url);
    for (const link of page.querySelectorAll('.workflow-choice')) {
      const destination = new URL(link.href);
      for (const key of ['q', 'state']) {
        if (url.searchParams.has(key)) destination.searchParams.set(key, url.searchParams.get(key));
        else destination.searchParams.delete(key);
      }
      link.href = destination;
    }
  }
  function setup() {
    const page = root();
    if (!page || initialized === page) return;
    initialized = page;
    for (const form of page.querySelectorAll('[data-evaluation-draft]')) {
      const saved = drafts.get(form.dataset.evaluationDraft);
      for (const value of saved || []) {
        const field = form.elements.namedItem(value.name);
        if (field && (field.tagName !== 'SELECT' || [...field.options].some(option => option.value === value.value))) field.value = value.value;
      }
      if (saved) { const section = form.closest('details'); if (section) section.open = true; }
    }
    if (new URL(location.href).searchParams.has('prompts')) {
      const library = page.querySelector('#prompt-library'); if (library) library.open = true;
    }
    filter(page);
  }
  document.addEventListener('input', event => {
    const page = event.target.closest('[data-evaluation-studio]');
    if (!page) return;
    if (event.target.closest('[data-evaluation-filter]')) filter(page, true);
    const form = event.target.closest('[data-evaluation-draft]'); if (form) remember(form);
  });
  document.addEventListener('change', event => {
    const page = event.target.closest('[data-evaluation-studio]');
    if (page && event.target.closest('[data-evaluation-filter]')) filter(page, true);
    const form = event.target.closest('[data-evaluation-draft]'); if (form) remember(form);
  });
  document.addEventListener('submit', event => {
    if (event.target.matches('[data-evaluation-filter]')) { event.preventDefault(); filter(event.target.closest('[data-evaluation-studio]'), true); }
    if (event.target.matches('[data-evaluation-draft]')) drafts.delete(event.target.dataset.evaluationDraft);
  });
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-evaluation-open]');
    if (!link) return;
    const section = root()?.querySelector(`#${link.dataset.evaluationOpen}`);
    if (!section) return;
    event.preventDefault(); section.open = true;
    section.scrollIntoView({block: 'nearest'}); section.querySelector('input, textarea, select')?.focus({preventScroll: true});
  });
  // Boosted links retain their initial path; use the current filtered destination at dispatch.
  document.addEventListener('htmx:configRequest', event => {
    const link = event.detail.elt?.closest('.workflow-choice');
    if (link) event.detail.path = link.href;
  });
  document.addEventListener('htmx:afterSwap', setup);
  setup();
})();
