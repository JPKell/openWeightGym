// Account desk: page-memory drafts survive account changes, discovery and successful section saves.
(() => {
  'use strict';
  const drafts = new Map(), baselines = new WeakMap();
  let initialized, pending = null;
  const view = {search:'', filter:'all', addOpen:null};
  const root = () => document.querySelector('[data-cloud-apis]');
  const fields = form => [...form.elements].filter(field => field.name && !['hidden','submit','button','reset'].includes(field.type));
  const values = form => fields(form).map(field => ({name:field.name,type:field.type,value:field.value,checked:field.checked}));
  const key = form => `${form.dataset.cloudName}:${form.dataset.cloudForm}`;
  const modelRows = form => [...form.querySelectorAll('tbody tr')].map(row =>
    Object.fromEntries([...row.querySelectorAll('input, select')].map(input => [input.name,input.value])));
  function remember(page) {
    page.querySelectorAll('[data-cloud-form]').forEach(form => {
      const now = values(form), base = baselines.get(form);
      if (!base) return;
      const edits = now.map((value,index) => ({...value,index})).filter((value,index) => JSON.stringify(now[index]) !== JSON.stringify(base[index]));
      if (edits.length) {
        const entry = {edits};
        if (form.dataset.cloudForm === 'models') {
          entry.rows = modelRows(form);
          entry.known = base.filter(field => field.name === 'theirs').map(field => field.value).filter(Boolean);
        }
        drafts.set(key(form),entry);
      } else drafts.delete(key(form));
    });
  }
  function marks(page) {
    page.querySelectorAll('[data-cloud-choice]').forEach(choice => {
      choice.querySelector('[data-draft-mark]').hidden = ![...drafts.keys()].some(name => name.startsWith(`${choice.dataset.cloudChoice}:`));
    });
    page.querySelectorAll('[data-draft]').forEach(label => {
      label.textContent = drafts.has(`${page.dataset.cloudAccount}:${label.dataset.draft}`) ? 'Unsaved changes' : '';
    });
  }
  function filter(page) {
    const choices = [...page.querySelectorAll('[data-cloud-choice]')];
    choices.forEach(choice => {
      choice.hidden = !choice.dataset.cloudSearch.toLowerCase().includes(view.search.toLowerCase().trim()) ||
        (view.filter !== 'all' && choice.dataset.cloudState !== view.filter);
    });
    page.querySelectorAll('.ca-provider-group').forEach(group => { group.hidden = ![...group.querySelectorAll('[data-cloud-choice]')].some(choice => !choice.hidden); });
    const count = choices.filter(choice => !choice.hidden).length;
    const label = page.querySelector('#result-count');
    if (label) label.textContent = `${count} of ${choices.length} accounts`;
    const empty = page.querySelector('[data-no-accounts]');
    if (empty) empty.hidden = Boolean(count);
  }
  function addPanel(page, open, focus = false) {
    view.addOpen = open;
    const panel = page.querySelector('[data-add-panel]');
    panel.hidden = !open;
    page.querySelectorAll('[data-add]').forEach(button => button.setAttribute('aria-expanded',String(open)));
    if (focus) (open ? panel.querySelector('[name=name]') : page.querySelector('[data-add]')).focus();
  }
  function showSection(page, section) {
    page.dataset.cloudSection = section;
    document.dispatchEvent(new CustomEvent('suite:actionsChanged',{detail:{form:page.querySelector('#remove-form')}}));
    page.querySelectorAll('[data-panel]').forEach(panel => { panel.hidden = panel.dataset.panel !== section; });
    page.querySelectorAll('.ca-settings-nav [data-editor-section]').forEach(link => {
      const active = link.dataset.editorSection === section;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current','page'); else link.removeAttribute('aria-current');
    });
    page.querySelectorAll('[data-cloud-choice]').forEach(link => {
      const url = new URL(link.href); url.searchParams.set('section',section); link.href = url.href;
    });
  }
  function restoreModels(form, entry, page) {
    const serverRows = modelRows(form);
    const rows = [...entry.rows, ...serverRows.filter(row => row.theirs && !entry.known.includes(row.theirs) && !entry.rows.some(one => one.theirs === row.theirs))];
    const body = form.querySelector('tbody'); body.replaceChildren();
    rows.forEach(row => {
      const element = page.querySelector('#model-row').content.firstElementChild.cloneNode(true);
      element.querySelectorAll('input, select').forEach(input => { input.value = row[input.name] ?? (input.tagName === 'SELECT' ? 'text' : ''); });
      body.append(element);
    });
  }
  function setup() {
    const page = root();
    if (!page || page === initialized) return;
    initialized = page;
    const failed = Boolean(page.querySelector('.said-box.alert-danger'));
    if (pending && !failed) {
      if (pending.kind === 'remove') for (const name of drafts.keys()) { if (name.startsWith(pending.name + ':')) drafts.delete(name); }
      else drafts.delete(pending.key);
      if (pending.kind === 'add') view.addOpen = false;
    }
    pending = null;
    // Dropped accounts no longer retain private drafts.
    const known = new Set(['@add', ...[...page.querySelectorAll('[data-cloud-choice]')].map(choice => choice.dataset.cloudChoice)]);
    for (const name of drafts.keys()) if (!known.has(name.split(':')[0])) drafts.delete(name);
    page.querySelectorAll('[data-cloud-form]').forEach(form => {
      baselines.set(form,values(form));
      const entry = drafts.get(key(form));
      if (!entry) return;
      if (entry.rows) restoreModels(form, entry, page);
      else for (const edit of entry.edits) {
        const field = fields(form)[edit.index];
        if (!field || field.name !== edit.name || field.type !== edit.type) continue;
        if (['checkbox','radio'].includes(field.type)) field.checked = edit.checked; else field.value = edit.value;
      }
    });
    const search = page.querySelector('#search'), select = page.querySelector('#status-filter');
    if (search) search.value = view.search;
    if (select) select.value = view.filter;
    addPanel(page, failed && !page.querySelector('[data-add-panel]').hidden ? true : view.addOpen ?? !page.querySelector('[data-add-panel]').hidden);
    showSection(page,page.dataset.cloudSection); filter(page); marks(page);
  }
  document.addEventListener('input', event => {
    const page = root(); if (!page?.contains(event.target)) return;
    if (event.target.id === 'search') { view.search = event.target.value; filter(page); }
    else { remember(page); marks(page); }
  });
  document.addEventListener('change', event => {
    const page = root(); if (!page?.contains(event.target)) return;
    if (event.target.id === 'status-filter') { view.filter = event.target.value; filter(page); }
    if (event.target.id === 'new-provider' && event.target.value) {
      page.querySelector('#new-url').value = event.target.value;
    }
    remember(page); marks(page);
  });
  document.addEventListener('click', event => {
    const page = root(); if (!page?.contains(event.target)) return;
    const section = event.target.closest('[data-editor-section]');
    if (section && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {
      event.preventDefault(); showSection(page,section.dataset.editorSection);
      const url = new URL(location.href); url.searchParams.set('section',section.dataset.editorSection); url.searchParams.delete('offers'); url.hash = '';
      history.replaceState(history.state,'',url); return;
    }
    if (event.target.closest('[data-add]')) { event.preventDefault(); addPanel(page,true,true); }
    if (event.target.closest('#cancel-add')) addPanel(page,false,true);
    if (event.target.closest('#add-model-row')) {
      const row = page.querySelector('#model-row').content.firstElementChild.cloneNode(true);
      page.querySelector('#model-rows').append(row); row.querySelector('input').focus();
    }
  });
  document.addEventListener('keydown', event => {
    const page = root(); if (event.key === 'Escape' && page && view.addOpen) addPanel(page,false,true);
  });
  document.addEventListener('htmx:beforeRequest', event => {
    const page = root(), form = event.detail.elt?.closest?.('[data-cloud-form]');
    if (page && form && event.detail.requestConfig?.verb !== 'get') {
      remember(page); pending = {key:key(form),kind:form.dataset.cloudForm,name:form.dataset.cloudName};
    }
  });
  // Boosted links cache their initial address; carry the currently selected settings group.
  document.addEventListener('htmx:configRequest', event => {
    const page = root(), choice = event.detail.elt?.closest?.('[data-cloud-choice]');
    if (!page || !choice) return;
    const url = new URL(event.detail.path,location.href);
    url.searchParams.set('section',page.dataset.cloudSection);
    event.detail.path = url.pathname + url.search;
  });
  document.addEventListener('htmx:beforeSwap', event => {
    const page = root(); if (page && event.detail.target?.id === 'page-content') remember(page);
  });
  document.addEventListener('htmx:afterSwap',setup);
  document.addEventListener('htmx:historyRestore',setup);
  setup();
})();
