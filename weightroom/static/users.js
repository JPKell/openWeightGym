// Settings › Users. The directory is filtered in the page; what is typed into an account's permissions, limits and tools
// is kept in this page's memory while another account is looked at, and put back when it is chosen again.
(() => {
  'use strict';
  const drafts = new Map();          // account name → [{index, name, type, value, checked}] that differ from what was saved
  const baselines = new WeakMap();   // field → what the page came with
  let search = '', filter = 'all', addOpen = null, initialized = null, saving = '';
  const root = () => document.querySelector('[data-users]');
  const draftForm = page => page.querySelector('form[data-account-draft]');
  const controls = form => [...form.elements].filter(field => field.name && !['hidden', 'submit', 'button', 'reset'].includes(field.type));
  const now = field => JSON.stringify({value: field.value, checked: field.checked});

  function remember(page) {
    const form = draftForm(page);
    if (!form) return;
    const edits = controls(form).map((field, index) => ({field, index})).filter(({field}) => now(field) !== baselines.get(field))
      .map(({field, index}) => ({index, name: field.name, type: field.type, value: field.value, checked: field.checked}));
    if (edits.length) drafts.set(page.dataset.user, edits); else drafts.delete(page.dataset.user);
  }
  function showDrafts(page) {
    for (const choice of page.querySelectorAll('[data-user-choice]')) choice.querySelector('[data-draft-mark]').hidden = !drafts.has(choice.dataset.userChoice);
    const count = drafts.get(page.dataset.user)?.length || 0, others = [...drafts.keys()].filter(name => name !== page.dataset.user).length;
    const status = page.querySelector('[data-user-draft-status]');
    if (!status) return;
    status.textContent = (count ? `Unsaved changes: ${count} field${count === 1 ? '' : 's'}` : 'No unsaved changes') +
      (others ? ` · ${others} other account${others === 1 ? '' : 's'} with unsaved changes` : '');
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
  function filterAccounts(page) {
    const words = search.trim().toLowerCase(), choices = [...page.querySelectorAll('[data-user-choice]')];
    for (const choice of choices) {
      const facts = choice.dataset;
      const matches = filter === 'all' || filter === facts.userKind || (filter === 'disabled' && facts.userState === 'disabled') ||
        (filter === 'attention' && facts.userAttention) || (filter === 'custom' && facts.userCustom);
      choice.hidden = !matches || !facts.userSearch.toLowerCase().includes(words);
    }
    const count = choices.filter(choice => !choice.hidden).length;
    page.querySelector('[data-user-count]').textContent = count;
    page.querySelector('[data-user-empty]').hidden = count > 0;
  }
  function showAdd(page, open, focus = false) {
    const panel = page.querySelector('#add-account');
    addOpen = open; panel.hidden = !open;
    page.querySelector('[data-toggle-add]').setAttribute('aria-expanded', String(open));
    if (focus) (open ? panel.querySelector('[name=name]') : page.querySelector('[data-toggle-add]')).focus();
  }

  function setup() {
    const page = root();
    if (!page || page === initialized) return;
    initialized = page;
    const failed = page.querySelector('.said-box.alert-danger');
    // A save that went through leaves nothing unsaved; one that was refused keeps what was typed.
    if (saving === page.dataset.user && !failed) drafts.delete(saving);
    saving = '';
    const form = draftForm(page);
    if (form) {
      const fields = controls(form);
      fields.forEach(field => baselines.set(field, now(field)));
      for (const edit of drafts.get(page.dataset.user) || []) {
        const field = fields[edit.index];
        if (!field || field.name !== edit.name || field.type !== edit.type) continue;
        if (['checkbox', 'radio'].includes(field.type)) field.checked = edit.checked; else field.value = edit.value;
      }
    }
    page.querySelector('#user-search').value = search;
    page.querySelector('#user-filter').value = filter;
    const panel = page.querySelector('#add-account');
    showAdd(page, addOpen === null || !panel.hidden ? !panel.hidden : addOpen);
    filterAccounts(page); showDrafts(page); showOnlyChanges(page);
  }

  document.addEventListener('input', event => {
    const page = root();
    if (!page || !page.contains(event.target)) return;
    if (event.target.id === 'user-search') { search = event.target.value; return filterAccounts(page); }
    if (event.target.closest('form[data-account-draft]')) { remember(page); showDrafts(page); }
  });
  document.addEventListener('change', event => {
    const page = root();
    if (!page || !page.contains(event.target)) return;
    if (event.target.id === 'user-filter') { filter = event.target.value; filterAccounts(page); }
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
    if (event.target.closest('[data-toggle-add]')) { event.preventDefault(); showAdd(page, page.querySelector('#add-account').hidden, true); }
    if (event.target.closest('[data-close-add]')) showAdd(page, false, true);
    if (event.target.closest('[data-show-attention]')) {
      filter = 'attention'; page.querySelector('#user-filter').value = filter; filterAccounts(page);
      page.querySelector('#user-filter').focus();
    }
    if (event.target.closest('[data-discard-draft]')) {
      const form = draftForm(page);
      for (const field of controls(form)) {
        const was = JSON.parse(baselines.get(field));
        if (['checkbox', 'radio'].includes(field.type)) field.checked = was.checked; else field.value = was.value;
      }
      drafts.delete(page.dataset.user); showDrafts(page);
      form.querySelector('[name=do][value=save]').focus();
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && root() && addOpen && !document.querySelector('dialog[open]')) showAdd(root(), false, true);
  });
  document.addEventListener('htmx:beforeRequest', event => {
    const page = root();
    if (page && event.detail.elt?.closest?.('form[data-account-draft]')) saving = page.dataset.user;
  });
  // Before another account replaces this one, keep what was typed here.
  document.addEventListener('htmx:beforeSwap', event => {
    const page = root();
    if (page && event.detail.target?.id === 'page-content') remember(page);
  });
  document.addEventListener('htmx:afterSwap', setup);
  document.addEventListener('htmx:historyRestore', setup);
  setup();
})();
