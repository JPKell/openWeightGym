// Settings › Users and Settings › Groups. Each is a directory beside the one chosen (data-directory="user" or "group", with the
// chosen name in data-user or data-group). The directory is filtered in the page; what is typed into the chosen one's settings
// (form[data-account-draft]) is kept in this page's memory while another is looked at, and put back when it is chosen again.
(() => {
  'use strict';
  const drafts = new Map();          // "user:cleo" or "group:readers" → [{index, name, type, value, checked}] that differ from what was saved
  const baselines = new WeakMap();   // field → what the page came with
  const views = {};                  // kind → {search, filter, addOpen}: kept across swaps
  let initialized = null, saving = '';
  const root = () => document.querySelector('[data-directory]');
  const kindOf = page => page.dataset.directory;
  const chosen = page => page.dataset[kindOf(page)];
  const key = (page, name = chosen(page)) => `${kindOf(page)}:${name}`;
  const view = page => (views[kindOf(page)] ||= {search: '', filter: 'all', addOpen: null});
  const find = (page, part) => page.querySelector(`[data-${kindOf(page)}-${part}]`);
  const choices = page => [...page.querySelectorAll(`[data-${kindOf(page)}-choice]`)];
  const draftForm = page => page.querySelector('form[data-account-draft]');
  const controls = form => [...form.elements].filter(field => field.name && !['hidden', 'submit', 'button', 'reset'].includes(field.type));
  const now = field => JSON.stringify({value: field.value, checked: field.checked});
  // What each filter of a directory keeps. A group's row lists the words it matches; an account's says them one by one.
  const matches = {
    user: (facts, filter) => filter === facts.userKind || (filter === 'disabled' && facts.userState === 'disabled') ||
      (filter === 'attention' && facts.userAttention) || (filter === 'custom' && facts.userCustom),
    group: (facts, filter) => (facts.groupFilters || '').split(' ').includes(filter),
  };

  function remember(page) {
    const form = draftForm(page);
    if (!form) return;
    const edits = controls(form).map((field, index) => ({field, index})).filter(({field}) => now(field) !== baselines.get(field))
      .map(({field, index}) => ({index, name: field.name, type: field.type, value: field.value, checked: field.checked}));
    if (edits.length) drafts.set(key(page), edits); else drafts.delete(key(page));
  }
  function showDrafts(page) {
    const kind = kindOf(page), noun = kind === 'user' ? 'account' : kind;
    for (const choice of choices(page)) choice.querySelector('[data-draft-mark]').hidden = !drafts.has(key(page, choice.dataset[`${kind}Choice`]));
    const count = drafts.get(key(page))?.length || 0;
    const others = [...drafts.keys()].filter(name => name.startsWith(`${kind}:`) && name !== key(page)).length;
    const status = find(page, 'draft-status');
    if (!status) return;
    status.textContent = (count ? `Unsaved changes: ${count} field${count === 1 ? '' : 's'}` : 'No unsaved changes') +
      (others ? ` · ${others} other ${noun}${others === 1 ? '' : 's'} with unsaved changes` : '');
    status.closest('.account-save-bar').classList.toggle('is-dirty', Boolean(count));
    page.querySelector('[data-discard-draft]').hidden = !count;
  }
  function showOnlyChanges(page) {
    const only = page.querySelector('[data-changed-only]')?.checked;
    let shown = 0;
    for (const category of page.querySelectorAll('[data-ability-category]')) {
      let visible = 0;
      for (const row of category.querySelectorAll('.col-md-6')) { row.hidden = only && !row.dataset.abilityDiff; visible += !row.hidden; }
      category.hidden = !visible; shown += visible;
    }
    const none = page.querySelector('[data-no-changes]');
    if (none) none.hidden = !(only && !shown);
  }
  function filterChoices(page) {
    const {search, filter} = view(page), words = search.trim().toLowerCase(), kind = kindOf(page), listed = choices(page);
    for (const choice of listed) {
      const facts = choice.dataset;
      choice.hidden = !(filter === 'all' || matches[kind](facts, filter)) || !facts[`${kind}Search`].toLowerCase().includes(words);
    }
    const count = listed.filter(choice => !choice.hidden).length;
    find(page, 'count').textContent = count;
    find(page, 'empty').hidden = count > 0;
  }
  function showAdd(page, open, focus = false) {
    const panel = page.querySelector('[data-add-panel]'), toggle = page.querySelector('[data-toggle-add]');
    if (!panel || !toggle) return;
    view(page).addOpen = open; panel.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
    if (focus) (open ? panel.querySelector('[name=name]') : toggle).focus();
  }

  function setup() {
    const page = root();
    if (!page || page === initialized) return;
    initialized = page;
    const failed = page.querySelector('.said-box.alert-danger');
    // A save that went through leaves nothing unsaved; one that was refused keeps what was typed.
    if (saving === key(page) && !failed) drafts.delete(saving);
    saving = '';
    const form = draftForm(page);
    if (form) {
      const fields = controls(form);
      fields.forEach(field => baselines.set(field, now(field)));
      for (const edit of drafts.get(key(page)) || []) {
        const field = fields[edit.index];
        if (!field || field.name !== edit.name || field.type !== edit.type) continue;
        if (['checkbox', 'radio'].includes(field.type)) field.checked = edit.checked; else field.value = edit.value;
      }
    }
    const kind = kindOf(page), {search, filter, addOpen} = view(page);
    page.querySelector(`#${kind}-search`).value = search;
    page.querySelector(`#${kind}-filter`).value = filter;
    const panel = page.querySelector('[data-add-panel]');
    if (panel) showAdd(page, addOpen === null || !panel.hidden ? !panel.hidden : addOpen);
    filterChoices(page); showDrafts(page); showOnlyChanges(page);
  }

  document.addEventListener('input', event => {
    const page = root();
    if (!page || !page.contains(event.target)) return;
    if (event.target.id === `${kindOf(page)}-search`) { view(page).search = event.target.value; return filterChoices(page); }
    if (event.target.closest('form[data-account-draft]')) { remember(page); showDrafts(page); }
  });
  document.addEventListener('change', event => {
    const page = root();
    if (!page || !page.contains(event.target)) return;
    if (event.target.id === `${kindOf(page)}-filter`) { view(page).filter = event.target.value; filterChoices(page); }
    if (event.target.matches('[data-changed-only]')) showOnlyChanges(page);
    if (event.target.matches('#add-account [name=kind]')) {
      const person = event.target.value === 'person';
      page.querySelector('#add-password').checked = person;
      page.querySelector('#add-key').checked = !person;
    }
  });
  document.addEventListener('click', event => {
    const page = root();
    if (!page || !page.contains(event.target)) return;
    if (event.target.closest('[data-toggle-add]')) { event.preventDefault(); showAdd(page, page.querySelector('[data-add-panel]').hidden, true); }
    if (event.target.closest('[data-close-add]')) showAdd(page, false, true);
    if (event.target.closest('[data-show-attention]')) {
      const select = page.querySelector(`#${kindOf(page)}-filter`);
      view(page).filter = select.value = 'attention'; filterChoices(page);
      select.focus();
    }
    if (event.target.closest('[data-discard-draft]')) {
      const form = draftForm(page);
      for (const field of controls(form)) {
        const was = JSON.parse(baselines.get(field));
        if (['checkbox', 'radio'].includes(field.type)) field.checked = was.checked; else field.value = was.value;
      }
      drafts.delete(key(page)); showDrafts(page);
      form.querySelector('[name=do][value=save]').focus();
    }
  });
  document.addEventListener('keydown', event => {
    const page = root();
    if (event.key === 'Escape' && page && view(page).addOpen && !document.querySelector('dialog[open]')) showAdd(page, false, true);
  });
  document.addEventListener('htmx:beforeRequest', event => {
    const page = root();
    if (page && event.detail.elt?.closest?.('form[data-account-draft]')) saving = key(page);
  });
  // Before another account or group replaces this one, keep what was typed here.
  document.addEventListener('htmx:beforeSwap', event => {
    const page = root();
    if (page && event.detail.target?.id === 'page-content') remember(page);
  });
  document.addEventListener('htmx:afterSwap', setup);
  document.addEventListener('htmx:historyRestore', setup);
  setup();
})();
