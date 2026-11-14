// Each purpose section saves through the existing per-service handlers, in order.
(() => {
  'use strict';
  if (window.owgSettingsReady) return;
  window.owgSettingsReady = true;
  let workspace = null, observer = null, pendingRefresh = false, filters = {query: '', changed: false};
  const filterKey = () => 'owg-settings-filters-v1:' + encodeURIComponent(document.documentElement.dataset.themeUser || '') + ':' + location.pathname;
  const privateField = field => field.type === 'password' || field.autocomplete === 'new-password';
  const editable = field => field.name && !['hidden', 'submit', 'button', 'reset'].includes(field.type) && !privateField(field);
  function rememberFilters() {
    try { sessionStorage.setItem(filterKey(), JSON.stringify(filters)); } catch {}
  }
  function dirty(row) {
    return [...row.querySelectorAll('input, select, textarea')].filter(editable).some(field =>
      ['checkbox', 'radio'].includes(field.type) ? field.checked !== field.defaultChecked :
      field.tagName === 'SELECT' ? [...field.options].some(option => option.selected !== option.defaultSelected) : field.value !== field.defaultValue);
  }
  function rowsIn(content) {
    for (const field of content.querySelectorAll('form input, form select, form textarea')) {
      if (!editable(field) || field.closest('[data-voice-preview], [data-settings-ignore]') || field.closest('.settings-row, [data-setting-row], [data-setting-search]')) continue;
      const row = field.closest('tr, .col-md-6, .col-md-4, .col-md-8, .col-12, .form-check') || field.parentElement;
      if (row && !row.matches('.sticky-actions')) row.dataset.settingRow = '';
    }
    return [...content.querySelectorAll('.settings-row, [data-setting-row], [data-setting-search]')];
  }
  const externalHidden = element => !!element.closest('[hidden]');
  function refreshFilters() {
    pendingRefresh = false;
    if (!workspace?.isConnected) return;
    const content = workspace.querySelector('.settings-content'), rows = rowsIn(content);
    const words = filters.query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);
    const active = !!words.length || filters.changed;
    let shown = 0;
    for (const row of rows) {
      const fields = [...row.querySelectorAll('input, select, textarea')].filter(editable);
      const labels = fields.flatMap(field => [...(field.labels || [])].map(label => label.textContent));
      const text = [row.dataset.settingSearch || labels.join(' ') || row.textContent,
        ...fields.map(field => field.name + ' ' + field.value)].join(' ').toLocaleLowerCase();
      const saved = row.closest('[data-setting-changed="true"], [data-setting-refused="true"], [data-setting-pending="true"]');
      const matches = words.every(word => text.includes(word)) && (!filters.changed || saved || dirty(row));
      row.classList.toggle('settings-filtered-out', !matches);
      if (matches && !externalHidden(row)) shown++;
    }
    const groups = [...content.querySelectorAll('.card')].filter(card => !card.parentElement.closest('.card'));
    const links = workspace.querySelector('[data-settings-section-links]'), template = workspace.querySelector('[data-settings-section-link]');
    links.replaceChildren();
    let visibleGroups = 0;
    const used = new Set();
    for (const group of groups) {
      const members = rows.filter(row => group.contains(row));
      const hasMatch = group.querySelector('[role=alert]') || (members.length ? members.some(row => !row.classList.contains('settings-filtered-out')) :
        !filters.changed && words.every(word => group.textContent.toLocaleLowerCase().includes(word)));
      group.classList.toggle('settings-filtered-out', active && !hasMatch);
      if (externalHidden(group) || group.classList.contains('settings-filtered-out')) continue;
      visibleGroups++;
      if (active && members.length && group.tagName === 'DETAILS') group.open = true;
      const title = group.querySelector('.card-heading, .section-title')?.textContent.trim();
      if (!title) continue;
      let id = group.id || 'settings-section-' + (group.closest('[data-voice-panel]')?.dataset.voicePanel || '') + '-' + title.toLocaleLowerCase().replace(/[^a-z0-9]+/g, '-');
      while (used.has(id)) id += '-section';
      used.add(id); group.id = id;
      const link = template.content.firstElementChild.cloneNode(true);
      link.textContent = title; link.setAttribute('href', '#' + id);
      if (location.hash === '#' + id || !location.hash && !links.childElementCount) { link.classList.add('active'); link.setAttribute('aria-current', 'location'); }
      links.append(link);
    }
    workspace.querySelector('[data-settings-count]').textContent = rows.length ? shown + ' setting' + (shown === 1 ? '' : 's') + ' shown' : '';
    workspace.querySelector('[data-settings-no-match]').hidden = !active || (shown > 0 || visibleGroups > 0);
    workspace.querySelector('[data-settings-clear]').hidden = !active;
  }
  function queueRefresh() {
    if (!pendingRefresh) { pendingRefresh = true; requestAnimationFrame(refreshFilters); }
  }
  function setupFilters() {
    const current = document.querySelector('[data-settings-workspace]');
    if (!current) { observer?.disconnect(); workspace = null; return; }
    if (current !== workspace) {
      observer?.disconnect(); workspace = current;
      try {
        const saved = JSON.parse(sessionStorage.getItem(filterKey()) || '{}');
        filters = {query: typeof saved.query === 'string' ? saved.query.slice(0, 256) : '', changed: saved.changed === true};
      } catch { filters = {query: '', changed: false}; }
      const search = workspace.querySelector('[data-settings-search]'), changed = workspace.querySelector('[data-settings-changed]');
      search.disabled = false; changed.disabled = false;
      search.value = filters.query; changed.checked = filters.changed;
      observer = new MutationObserver(queueRefresh);
      observer.observe(workspace.querySelector('.settings-content'), {childList: true, subtree: true, attributes: true, attributeFilter: ['hidden']});
    }
    queueRefresh();
  }
  document.addEventListener('input', event => {
    if (!workspace?.contains(event.target)) return;
    if (event.target.matches('[data-settings-search]')) { filters.query = event.target.value.slice(0, 256); rememberFilters(); }
    queueRefresh();
  });
  document.addEventListener('change', event => {
    if (!workspace?.contains(event.target)) return;
    if (event.target.matches('[data-settings-changed]')) { filters.changed = event.target.checked; rememberFilters(); }
    queueRefresh();
  });
  document.addEventListener('click', event => {
    const clear = event.target.closest('[data-settings-clear]');
    if (clear && workspace?.contains(clear)) {
      filters = {query:'', changed:false};
      workspace.querySelector('[data-settings-search]').value = '';
      workspace.querySelector('[data-settings-changed]').checked = false;
      rememberFilters(); queueRefresh();
    }
    const sectionLink = event.target.closest('.settings-section-link');
    if (sectionLink) {
      for (const link of workspace?.querySelectorAll('.settings-section-link') || []) { link.classList.remove('active'); link.removeAttribute('aria-current'); }
      sectionLink.classList.add('active'); sectionLink.setAttribute('aria-current', 'location');
      const section = document.getElementById(sectionLink.hash.slice(1));
      if (section?.tagName === 'DETAILS') section.open = true;
    }
  });

  function setup() {
    setupFilters();
    document.querySelectorAll('[data-settings-section]').forEach(section => {
      section.querySelector('[data-save-settings]').hidden = false;
      section.querySelectorAll('[data-settings-fallback]').forEach(button => { button.hidden = true; });
    });
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-save-settings]');
    if (!button || button.disabled) return;
    const section = button.closest('[data-settings-section]'), forms = [...section.querySelectorAll('[data-settings-fragment]')];
    const invalid = forms.flatMap(form => [...form.elements]).find(field => field.willValidate && !field.checkValidity());
    if (invalid) {
      filters = {query:'', changed:false};
      workspace.querySelector('[data-settings-search]').value = '';
      workspace.querySelector('[data-settings-changed]').checked = false;
      rememberFilters(); refreshFilters();
      for (let parent = invalid.closest('details'); parent; parent = parent.parentElement.closest('details')) parent.open = true;
      invalid.reportValidity(); invalid.focus(); return;
    }
    const status = section.querySelector('[data-settings-status]');
    button.disabled = true; status.className = 'small text-secondary'; status.textContent = 'Saving…';
    let saved = 0;
    const responses = [];
    document.dispatchEvent(new CustomEvent('weightroom:settings-saving', {detail: {forms}}));
    try {
      for (const form of forms) {
        const response = await fetch(form.action, {method: 'POST', body: new URLSearchParams(new FormData(form)), credentials: 'same-origin'});
        if (new URL(response.url).pathname === '/sign-in') { location.href = '/sign-in'; return; }
        if (!response.ok) {
          const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
          throw new Error(doc.querySelector('.said-box')?.textContent.trim() || 'A service refused its settings.');
        }
        responses.push(await response.text());
        saved++;
      }
      const savedEvent = new CustomEvent('weightroom:settings-saved', {cancelable: true, detail: {forms, responses}});
      if (!document.dispatchEvent(savedEvent)) return;
      const destination = new URL(location.href);
      destination.searchParams.set('said', 'Saved.');
      location.assign(destination);
    } catch (error) {
      status.className = 'small text-danger';
      status.textContent = (saved ? `${saved} service${saved === 1 ? '' : 's'} saved. ` : '') + error.message;
    } finally { button.disabled = false; }
  });
  document.addEventListener('htmx:afterSwap', setup);
  setup();
})();
