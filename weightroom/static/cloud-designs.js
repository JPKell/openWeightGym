// Sample-only Cloud APIs. No network requests, credential storage or live mutations.
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const sample = JSON.parse($('#cloud-data').textContent).accounts;
  const copy = value => structuredClone(value);
  const money = value => '$' + Number(value || 0).toFixed(2);
  const editor = $('#account-editor');
  const settings = $('#settings-dialog');
  const add = $('#add-dialog');
  const drafts = new Map();
  let accounts = copy(sample.slice(0, 6)), selected = accounts[0].id;
  let layout = 'desk', section = 'connection', matrixOpen = false, discoveryError = false;
  let returnFocus = null;
  const notes = {
    desk: '01 / Account desk — a quiet, focused workspace. Keep your accounts beside the settings. Recommended for everyday use.',
    gallery: '02 / Provider gallery — a visual overview. Compare account health and spending, then open the exact setting you need.',
    matrix: '03 / Settings matrix — a compact control room. Compare all accounts by setting and edit directly below the matrix.',
  };
  const current = () => accounts.find(account => account.id === selected);
  const kind = account => !account.key ? 'key' : account.ceiling && account.spent >= account.ceiling ? 'limit' : 'ready';
  const status = account => ({key:'Needs API key',limit:'Limit reached',ready:'Key installed'})[kind(account)];
  const rows = account => Object.entries(account.models).map(([ours, model]) => ({ours, ...model}));
  function draft(account = current()) {
    if (!drafts.has(account.id)) drafts.set(account.id, {url:account.url, ceiling:account.ceiling ?? '', key:'', rows:rows(account), dirty:new Set()});
    return drafts.get(account.id);
  }
  function clone(id) { return $(id).content.firstElementChild.cloneNode(true); }
  function text(root, selector, value) { root.querySelectorAll(selector).forEach(el => { el.textContent = value; }); }
  function identity(root, account) {
    text(root, '[data-mark]', account.mark);
    text(root, '[data-label]', account.label);
    text(root, '[data-provider]', account.provider);
    text(root, '[data-name]', account.name);
    root.querySelectorAll('[data-open]').forEach(button => { button.dataset.account = account.id; });
    if (root.matches('[data-open]')) root.dataset.account = account.id;
  }
  function notice(message, error = false) {
    $('#feedback').hidden = false;
    $('#feedback-text').textContent = message;
    $('#editor-message').hidden = false;
    $('#editor-message-text').textContent = message;
    $('#editor-message-text').classList.toggle('alert-danger', error);
    $('#editor-message-text').classList.toggle('alert-info', !error);
  }
  function progress(el, account) {
    el.value = account.ceiling ? Math.min(100, account.spent / account.ceiling * 100) : 0;
    el.dataset.kind = kind(account);
    el.setAttribute('aria-valuetext', account.ceiling ? `${money(account.spent)} of ${money(account.ceiling)}` : `${money(account.spent)} spent, no daily limit`);
  }
  function filtered() {
    const query = $('#search').value.trim().toLowerCase(), filter = $('#status-filter').value;
    return accounts.filter(account => `${account.name} ${account.label} ${account.provider} ${account.url}`.toLowerCase().includes(query) && (filter === 'all' || kind(account) === filter));
  }
  function renderInventory() {
    const found = filtered();
    const providers = [...new Set(accounts.map(account => account.provider))];
    found.sort((a,b) => providers.indexOf(a.provider) - providers.indexOf(b.provider));
    $('#total-accounts').textContent = accounts.length;
    $('#total-endpoints').textContent = new Set(accounts.map(a => a.url)).size;
    $('#total-models').textContent = accounts.reduce((sum, a) => sum + Object.keys(a.models).length, 0);
    $('#total-spend').textContent = money(accounts.reduce((sum, a) => sum + a.spent, 0));
    $('#rail-count').textContent = accounts.length;
    $('#result-count').textContent = `${found.length} of ${accounts.length} accounts`;
    $('#account-choices').replaceChildren();
    $('#account-cards').replaceChildren();
    $('#matrix-rows').replaceChildren();
    const groups = new Map();
    found.forEach(account => {
      if (!groups.has(account.provider)) {
        const group = clone('#provider-group');
        text(group, '[data-group-name]', account.provider);
        groups.set(account.provider, group);
        $('#account-choices').append(group);
      }
      const choice = clone('#account-choice');
      identity(choice, account);
      choice.setAttribute('aria-pressed', String(account.id === selected));
      text(choice, '[data-state]', kind(account) === 'ready' ? '●' : '!');
      choice.querySelector('[data-state]').dataset.attention = String(kind(account) !== 'ready');
      choice.setAttribute('aria-label', `${account.label}, ${account.provider}, ${status(account)}`);
      groups.get(account.provider).querySelector('[data-group-accounts]').append(choice);
      const card = clone('#account-card');
      identity(card, account);
      text(card, '[data-url]', account.url);
      text(card, '[data-status]', status(account));
      card.querySelector('[data-status]').dataset.kind = kind(account);
      text(card, '[data-spend]', money(account.spent));
      text(card, '[data-limit]', account.ceiling ? `${money(account.ceiling)} daily limit` : 'No daily limit');
      text(card, '[data-models]', `${Object.keys(account.models).length} models`);
      progress(card.querySelector('[data-progress]'), account);
      $('#account-cards').append(card);
      const row = clone('#matrix-row');
      identity(row, account);
      row.dataset.selected = String(matrixOpen && account.id === selected);
      text(row, '[data-host]', new URL(account.url).host);
      text(row, '[data-status]', status(account));
      text(row, '[data-models]', `${Object.keys(account.models).length} models`);
      text(row, '[data-spend]', money(account.spent));
      text(row, '[data-limit]', account.ceiling ? `of ${money(account.ceiling)} / 24h ↗` : 'No daily limit ↗');
      $('#matrix-rows').append(row);
    });
    const empty = !found.length;
    $('#empty-state').hidden = !empty;
    $('#empty-title').textContent = accounts.length ? 'No accounts match' : 'Your next connection starts here';
    $('#empty-description').textContent = accounts.length ? 'Try a different account name, endpoint or state filter.' : 'Add a cloud account, install its key, then choose the models to offer.';
    $('#empty-state [data-add]').textContent = accounts.length ? '+ Add account' : '+ Add your first account';
    $('#desk-view').hidden = layout !== 'desk' || empty;
    $('#gallery-view').hidden = layout !== 'gallery' || empty;
    $('#matrix-view').hidden = layout !== 'matrix' || empty;
    if (empty) $('#matrix-rows').append(clone('#empty-matrix'));
  }
  function fillRows() {
    $('#model-rows').replaceChildren();
    const entries = [...draft().rows];
    if (!entries.some(row => !row.ours && !row.theirs)) entries.push({ours:'',theirs:'',in:'',out:''});
    entries.forEach(row => {
      const element = clone('#model-row');
      ['ours','theirs','in','out'].forEach(name => { element.querySelector(`[name="${name}"]`).value = row[name] ?? ''; });
      $('#model-rows').append(element);
    });
  }
  function dirtyLabels() {
    if (!current()) return;
    $$('[data-draft]').forEach(el => { el.textContent = draft().dirty.has(el.dataset.draft) ? 'Unsaved changes' : ''; });
  }
  function showSection(value, focus = false) {
    section = value;
    $$('[data-panel]').forEach(panel => { panel.hidden = panel.dataset.panel !== section; });
    $$('[data-editor-section]').forEach(button => {
      const active = button.dataset.editorSection === section;
      if (button.classList.contains('nav-link')) {
        button.classList.toggle('active', active);
        button.setAttribute('aria-pressed', String(active));
      }
    });
    if (focus) {
      const title = $(`#panel-${section} .card-heading`);
      title.setAttribute('tabindex','-1'); title.focus({preventScroll:true});
    }
  }
  function renderEditor() {
    const account = current();
    if (!account) return;
    const d = draft(account);
    $('#editor-title').textContent = account.label;
    $('#editor-provider').textContent = account.provider;
    $('#editor-mark').textContent = account.mark;
    $('#editor-name').textContent = account.name;
    $('#editor-status').textContent = status(account);
    $('#editor-status').dataset.kind = kind(account);
    $$('[data-account-name]').forEach(input => { input.value = account.name; });
    $('#account-url').value = d.url;
    $('#account-id').value = account.name;
    $('#account-limit').value = d.ceiling;
    $('#account-key').value = d.key;
    $('#key-state').textContent = account.key ? 'API key installed. The saved credential stays hidden.' : 'No API key installed. Install a key to use this account’s models.';
    $('#key-state').dataset.kind = account.key ? 'ready' : 'key';
    $('#key-label').textContent = account.key ? 'Replace API key' : 'API key';
    $('#save-key').textContent = account.key ? 'Update API key' : 'Install API key';
    const siblings = accounts.filter(a => a.url === account.url && a.id !== account.id);
    $('#endpoint-context').textContent = siblings.length ? `Also used by ${siblings.map(a => a.label).join(', ')}. Changes here apply only to ${account.label}.` : 'Add another account at this endpoint whenever you need a different key or spending limit.';
    $('#spend-value').textContent = money(account.spent);
    $('#spend-note').textContent = account.ceiling ? `of ${money(account.ceiling)} in the last 24 hours` : 'spent in the last 24 hours · no limit';
    progress($('#editor-progress'), account);
    $('#limit-warning').hidden = kind(account) !== 'limit';
    document.dispatchEvent(new CustomEvent('suite:actionsChanged', {detail:{form:$('#remove-form')}}));
    $('#offers-panel').hidden = true;
    $('#editor-message').hidden = true;
    fillRows(); dirtyLabels(); showSection(section);
  }
  function placeEditor() {
    if (layout === 'desk' && current()) $('#desk-editor').append(editor);
    else if (layout === 'matrix' && matrixOpen && current()) $('#matrix-editor').append(editor);
    else if (layout === 'gallery' && settings.open) $('#dialog-editor').append(editor);
    else $('#editor-parking').append(editor);
    $('#matrix-hint').hidden = matrixOpen;
  }
  function openAccount(id, group = section, trigger = null) {
    selected = id; section = group; matrixOpen = layout === 'matrix';
    if (layout === 'gallery') {
      returnFocus = trigger;
      $('#dialog-editor').append(editor);
      if (!settings.open) settings.showModal();
    }
    placeEditor(); renderInventory(); renderEditor();
    if (layout === 'matrix') $('#matrix-editor').scrollIntoView({behavior:'auto',block:'start'});
  }
  function closeSettings() { settings.close(); }
  settings.addEventListener('close', () => {
    placeEditor();
    const target = returnFocus && $$('#account-cards [data-open]').find(button =>
      button.dataset.account === returnFocus.dataset.account && button.dataset.section === returnFocus.dataset.section);
    (target || $('[data-add]')).focus();
  });
  $('#close-settings').addEventListener('click', closeSettings);
  document.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.hasAttribute('data-open')) openAccount(button.dataset.account, button.dataset.section || section, button);
    if (button.dataset.editorSection) showSection(button.dataset.editorSection);
    if (button.hasAttribute('data-add')) {
      $('#add-form').reset(); $('#new-provider').value = urls.OpenRouter; $('#add-error').hidden = true; setProvider(); add.showModal();
    }
    if (button.dataset.design) {
      if (settings.open) settings.close();
      layout = button.dataset.design;
      $('#workspace').dataset.layout = layout;
      $('#design-note').textContent = notes[layout];
      $$('[data-design]').forEach(el => { const active = el.dataset.design === layout; el.classList.toggle('active',active); el.setAttribute('aria-pressed',String(active)); });
      placeEditor(); renderInventory();
    }
  });
  $('#search').addEventListener('input', renderInventory);
  $('#status-filter').addEventListener('change', renderInventory);
  function collectModels() {
    return $$('#model-rows tr').map(row => Object.fromEntries(['ours','theirs','in','out'].map(name => [name,row.querySelector(`[name="${name}"]`).value])));
  }
  editor.addEventListener('input', event => {
    if (!current() || event.target.id === 'remove-confirm') return;
    const d = draft();
    if (event.target.closest('#connection-form')) d.url = $('#account-url').value;
    else if (event.target.closest('#key-form')) d.key = $('#account-key').value;
    else if (event.target.closest('#limits-form')) d.ceiling = $('#account-limit').value;
    else if (event.target.closest('#models-form')) d.rows = collectModels();
    else return;
    d.dirty.add(section); dirtyLabels();
  });
  function saved(group, message) {
    draft().dirty.delete(group); renderInventory(); renderEditor(); notice(message + ' Preview saved in this tab.');
  }
  $('#connection-form').addEventListener('submit', event => {
    event.preventDefault();
    current().url = $('#account-url').value.trim(); draft().url = current().url;
    saved('connection',`Connection saved for ${current().name}.`);
  });
  $('#key-form').addEventListener('submit', event => {
    event.preventDefault(); current().key = true; draft().key = '';
    saved('access',`API key installation simulated for ${current().name}; the entered value was discarded.`);
  });
  $('#limits-form').addEventListener('submit', event => {
    event.preventDefault();
    const value = $('#account-limit').value;
    current().ceiling = value === '' ? null : Number(value);
    draft().ceiling = value; saved('limits',`Spending limit saved for ${current().name}.`);
  });
  $('#add-model-row').addEventListener('click', () => {
    const row = clone('#model-row'); $('#model-rows').append(row); row.querySelector('input').focus();
  });
  $('#models-form').addEventListener('submit', event => {
    event.preventDefault();
    const entries = collectModels(), models = {};
    for (const row of entries) {
      const name = row.ours.trim();
      if (!name) continue;
      if (!row.theirs.trim()) return notice('Enter a service model ID for every named model.',true);
      if (models[name]) return notice('Each model needs a unique local name.',true);
      if (['in','out'].some(key => row[key] !== '' && (!Number.isFinite(Number(row[key])) || Number(row[key]) < 0))) return notice('Token prices must be zero or a positive number.',true);
      models[name] = {theirs:row.theirs.trim(),...Object.fromEntries(['in','out'].filter(key => row[key] !== '').map(key => [key,Number(row[key])]))};
    }
    current().models = models; draft().rows = rows(current()); saved('models',`Models saved for ${current().name}.`);
  });
  $('#discover').addEventListener('click', () => {
    if (!current().key) return notice('Install an API key before discovering models.',true);
    if (discoveryError) return notice('Sample discovery failed: the service could not be reached. Your model settings and drafts are kept. Try again or add a row manually.',true);
    $('#offers-list').replaceChildren();
    const have = Object.values(current().models).map(model => model.theirs);
    ['sample/fast','sample/reasoning','sample/vision','sample/coding'].forEach(name => {
      const row = clone('#offer-row'), input = row.querySelector('input');
      input.value = name; input.checked = input.disabled = have.includes(name);
      row.querySelector('span').textContent = name + (input.disabled ? ' · already offered' : '');
      $('#offers-list').append(row);
    });
    $('#offers-panel').hidden = false;
  });
  $('#offers-form').addEventListener('submit', event => {
    event.preventDefault(); const d = draft();
    d.rows = d.rows.filter(row => ['ours','theirs','in','out'].some(key => String(row[key] ?? '').trim()));
    const chosen = $$('#offers-list input:checked:not(:disabled)');
    chosen.forEach(input => {
      let name = `${current().name}-${input.value.split('/').pop()}`, suffix = 2;
      const base = name;
      while (d.rows.some(row => row.ours === name)) name = `${base}-${suffix++}`;
      if (!d.rows.some(row => row.theirs === input.value)) d.rows.push({ours:name,theirs:input.value,in:'',out:''});
    });
    fillRows(); d.dirty.add('models'); dirtyLabels(); $('#offers-panel').hidden = true;
    notice('Selected sample models added to your draft. Save models to keep them in this preview.');
  });
  $('#remove-form').addEventListener('submit', event => {
    event.preventDefault(); const name = current().name;
    accounts = accounts.filter(account => account.id !== selected); drafts.delete(selected);
    selected = accounts[0]?.id; matrixOpen = false;
    if (settings.open) settings.close();
    placeEditor(); renderInventory(); renderEditor(); notice(`${name} removed from the preview. Other accounts are kept.`);
  });
  const urls = Object.fromEntries(sample.map(a => [a.provider,a.url]));
  function setProvider() { $('#new-url').value = $('#new-provider').value; }
  $('#new-provider').addEventListener('change', setProvider);
  $('#cancel-add').addEventListener('click', () => add.close());
  $('#new-name').addEventListener('input', () => { $('#add-error').hidden = true; });
  $('#add-form').addEventListener('submit', event => {
    event.preventDefault(); const name = $('#new-name').value.trim();
    if (accounts.some(a => a.name === name)) {
      $('#add-error').textContent = 'That account name is already used. Choose a different name; the endpoint may stay the same.';
      $('#add-error').hidden = false; return;
    }
    const provider = $('#new-provider').value ? $('#new-provider').selectedOptions[0].textContent : 'Custom endpoint';
    const account = {id:name,name,label:name,provider,mark:provider === 'Custom endpoint' ? 'API' : sample.find(a => a.provider === provider).mark,
      url:$('#new-url').value.trim(),ceiling:$('#new-ceiling').value === '' ? null : Number($('#new-ceiling').value),spent:0,key:false,models:{}};
    accounts.push(account); add.close();
    $('#search').value = ''; $('#status-filter').value = 'all';
    openAccount(account.id,'access'); notice(`Added ${name} to the preview. Install a sample key next.`);
  });
  $('#scenario').addEventListener('change', () => {
    if (settings.open) settings.close();
    const value = $('#scenario').value;
    accounts = copy(sample.slice(0,value === 'ten' ? 10 : value === 'one' ? 1 : value === 'empty' ? 0 : 6));
    drafts.clear(); selected = accounts[0]?.id; matrixOpen = false; section = 'connection'; discoveryError = value === 'error';
    $('#search').value = ''; $('#status-filter').value = 'all'; $('#feedback').hidden = true;
    placeEditor(); renderInventory(); renderEditor();
    if (discoveryError) { openAccount(selected,'models'); $('#discover').click(); }
  });
  $('#theme').addEventListener('click', () => {
    const form = $('[data-theme-form]'), mode = form.querySelector('[name="mode"]');
    mode.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    mode.dispatchEvent(new Event('change',{bubbles:true}));
    $('#theme').textContent = mode.value === 'dark' ? 'Light mode' : 'Dark mode';
  });
  $('#design-note').textContent = notes[layout];
  $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  placeEditor(); renderInventory(); renderEditor();
})();
