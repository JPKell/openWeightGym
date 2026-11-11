// Three sample-only Users proposals. Everything stays in this browser; nothing is sent anywhere.
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const data = JSON.parse($('#sample-data').textContent);
  const STORE = 'owg-user-designs-v1';
  const groupOf = name => data.groups.find(group => group.name === name) || {name, can: [], priority: 'normal', max_requests: 1, max_sensitivity: data.levels[0], models: [], tools: []};
  const compact = new Intl.NumberFormat('en', {notation: 'compact', maximumFractionDigits: 1});
  const whole = new Intl.NumberFormat('en');
  const NAME = /^[a-z0-9][a-z0-9._-]{0,63}$/;

  const valid = list => Array.isArray(list) && list.length && list.every(user => user && typeof user.name === 'string' &&
    typeof user.group === 'string' && Array.isArray(user.keys) && user.own && Array.isArray(user.own.add) && Array.isArray(user.daily));
  let accounts = structuredClone(data.users);
  try {
    const stored = JSON.parse(localStorage.getItem(STORE) || 'null');
    if (valid(stored)) accounts = stored;
  } catch { /* A blocked or corrupt store starts a fresh sample session. */ }

  let drafts = {}, selected = 'maya.r', layout = 'directory', view = 'profile', section = 'activity', secrets = null, shownFor = '';
  const descriptions = {
    directory: 'The Compute nodes pattern: a persistent account list beside every setting for the chosen account. Fastest for day-to-day changes.',
    ledger: 'An audit view: compare every account’s sign-in security, keys and recent requests in one sortable ledger, with the chosen account in a side panel.',
    lanes: 'Accounts arranged by group, as Status arranges its lanes. Choosing one opens a full account page that compares it with its group.'
  };
  const notes = {
    directory: '01 / Recommended · matches the live Compute nodes navigator; the overview and the account page become one workspace.',
    ledger: '02 / Access ledger · best for many accounts and periodic reviews; the side panel keeps one section open at a time.',
    lanes: '03 / Group lanes · keeps a separate overview and account page, with permissions shown side by side with the group’s.'
  };

  const account = name => accounts.find(user => user.name === name);
  const current = () => account(selected);
  const isSelf = user => user.name === data.viewer;
  const kind = user => user.password ? 'person' : 'program';
  const groupCan = name => new Set(groupOf(name).can);
  const covers = (can, ability) => can.has(ability) || (can.has('superuser') && ability !== 'superuser' && !data.own_key.includes(ability));
  const lines = text => [...new Set(text.split('\n').map(line => line.trim()).filter(Boolean))];

  /** What the account's page edits: its group and what it has of its own, as form values. */
  function policy(user) {
    const own = user.own, can = groupCan(user.group);
    own.add.forEach(name => can.add(name)); own.remove.forEach(name => can.delete(name));
    return {group: user.group, can: [...can].sort(), max_requests: own.max_requests ? String(own.max_requests) : '', max_sensitivity: own.max_sensitivity || '',
      priority: own.priority || '', fewer: Array.isArray(own.models), models: [...(own.models || [])].sort(),
      add_tools: (own.add_tools || []).join('\n'), remove_tools: (own.remove_tools || []).join('\n')};
  }
  /** The draft turned back into the account's own settings, as LoadCoach keeps them. */
  function ownOf(draft) {
    const group = groupCan(draft.group), can = new Set(draft.can);
    const own = {add: [...can].filter(name => !group.has(name)).sort(), remove: [...group].filter(name => !can.has(name)).sort(),
      add_tools: lines(draft.add_tools), remove_tools: lines(draft.remove_tools)};
    if (draft.max_requests) own.max_requests = Number(draft.max_requests);
    if (draft.max_sensitivity) own.max_sensitivity = draft.max_sensitivity;
    if (draft.priority) own.priority = draft.priority;
    if (draft.fewer) own.models = [...draft.models].sort();
    return own;
  }
  const draftOf = name => (drafts[name] ??= policy(account(name)));
  const dirty = name => Boolean(drafts[name]) && JSON.stringify(drafts[name]) !== JSON.stringify(policy(account(name)));
  function changes(name) {
    if (!dirty(name)) return [];
    const now = drafts[name], was = policy(account(name)), differs = keys => keys.some(key => JSON.stringify(now[key]) !== JSON.stringify(was[key]));
    return [differs(['group']) && 'group', differs(['can']) && 'permissions', differs(['max_requests', 'max_sensitivity', 'priority', 'fewer', 'models']) && 'limits',
      differs(['add_tools', 'remove_tools']) && 'tools'].filter(Boolean);
  }
  const custom = user => Object.entries(user.own).some(([key, value]) => Array.isArray(value) ? value.length || key === 'models' : value !== undefined && value !== '');
  const adminGroup = user => [...groupCan(user.group)].some(name => name === 'superuser' || name.startsWith('manage-'));
  function attention(user) {
    if (!user.enabled) return [];
    return [user.password === 'temporary' && 'Waiting for a first sign-in with the temporary password.',
      kind(user) === 'person' && !user.second_step && adminGroup(user) && 'An administrator without an authenticator.',
      kind(user) === 'program' && !user.keys.length && 'A program without an API key: it cannot call anything.',
      user.allowance && user.spend / user.allowance >= .9 && `At ${Math.round(100 * user.spend / user.allowance)}% of its 24-hour spending allowance.`].filter(Boolean);
  }
  const state = user => !user.enabled ? 'disabled' : user.password === 'temporary' ? 'invited' : 'active';
  const stateWords = {active: 'Active', invited: 'Invited', disabled: 'Disabled'};
  const sum = values => values.reduce((total, value) => total + value, 0);
  const week = user => sum(user.daily.slice(-7));
  const feedback = message => { $('#feedback').textContent = message; };
  const plural = (count, word, many = `${word}s`) => `${count} ${count === 1 ? word : many}`;
  function persist() {
    try { localStorage.setItem(STORE, JSON.stringify(accounts)); return true; } catch { return false; }
  }
  function demoSecret(prefix) {
    const bytes = crypto.getRandomValues(new Uint8Array(12));
    return `DEMO-ONLY-${prefix}-${[...bytes].map(byte => byte.toString(16).padStart(2, '0')).join('')}`;
  }

  // ---- shared pieces ----------------------------------------------------------------------
  function spark(svg, values) {
    const most = Math.max(...values, 1), step = 120 / Math.max(values.length - 1, 1);
    const points = values.map((value, index) => `${(index * step).toFixed(1)},${(30 - 28 * value / most).toFixed(1)}`);
    const ns = 'http://www.w3.org/2000/svg', area = document.createElementNS(ns, 'path'), line = document.createElementNS(ns, 'path');
    area.setAttribute('class', 'usage-spark-area'); area.setAttribute('d', `M0,32 L${points.join(' L')} L120,32 Z`);
    line.setAttribute('class', 'usage-spark-line'); line.setAttribute('d', `M${points.join(' L')}`);
    svg.replaceChildren(area, line);
  }
  function chips(holder, user) {
    const items = kind(user) === 'person'
      ? [[user.password === 'temporary' ? 'Temporary password' : 'Password', user.password === 'temporary' ? 'warn' : 'muted'], [user.second_step ? 'Authenticator' : 'No authenticator', user.second_step ? 'good' : 'muted']]
      : [[user.keys.length ? plural(user.keys.length, 'key') : 'No key', user.keys.length ? 'muted' : 'warn']];
    holder.replaceChildren(...items.map(([words, tone]) => { const chip = Object.assign(document.createElement('span'), {className: 'chip', textContent: words}); chip.dataset.tone = tone; return chip; }));
  }
  function fill(element, user) {
    const set = (selector, apply) => element.querySelectorAll(selector).forEach(apply);
    const kindWords = kind(user) === 'person' ? 'Person' : `Program · ${user.keys.length ? plural(user.keys.length, 'key') : 'no key'}`;
    set('[data-user-name]', item => { item.textContent = user.name; });
    set('[data-avatar]', item => { item.textContent = user.name.replace(/[^a-z0-9]/g, '').slice(0, 2).toUpperCase(); item.dataset.kind = kind(user); });
    set('[data-user-sub]', item => { item.textContent = [user.group, kind(user), state(user) !== 'active' && state(user)].filter(Boolean).join(' · '); });
    set('[data-user-kind]', item => { item.textContent = kindWords; });
    set('[data-user-group]', item => { item.textContent = user.group; });
    set('[data-user-keys]', item => { item.textContent = user.keys.length || '—'; });
    set('[data-user-sessions]', item => { item.textContent = user.sessions || '—'; });
    set('[data-user-requests]', item => { item.textContent = `${compact.format(week(user))} this week`; });
    set('[data-user-spark]', item => spark(item, user.daily.slice(-14)));
    set('[data-sign-in]', item => chips(item, user));
    set('[data-account-status]', item => { item.dataset.state = state(user); item.textContent = stateWords[state(user)]; });
    set('[data-draft-mark]', item => { item.hidden = !dirty(user.name); });
    set('[data-attention-mark]', item => { item.hidden = !attention(user).length; });
    set('[data-attention-text]', item => { item.textContent = attention(user)[0] || ''; item.hidden = !attention(user).length; });
    set('[data-user-choice]', item => {
      item.dataset.userChoice = user.name;
      item.setAttribute('aria-pressed', String(user.name === selected));
      item.setAttribute('aria-label', `${user.name} · ${user.group} · ${stateWords[state(user)]}${attention(user).length ? ' · needs attention' : ''}`);
    });
    if (element.matches('[data-user-choice]')) {
      element.dataset.userChoice = user.name;
      element.setAttribute('aria-pressed', String(user.name === selected));
    }
  }
  const groupLine = group => `${group.can.includes('superuser') ? 'superuser' : plural(group.can.length, 'ability', 'abilities')} · ${group.priority} priority · ${group.max_requests} at once · up to ${group.max_sensitivity}`;

  // ---- overview -----------------------------------------------------------------------------
  function matching() {
    const query = $('#user-search').value.trim().toLowerCase(), filter = $('#user-filter').value;
    return accounts.filter(user => `${user.name} ${user.group} ${kind(user)} ${user.keys.map(key => key.label).join(' ')}`.toLowerCase().includes(query) &&
      (filter === 'all' || filter === kind(user) || (filter === 'attention' && attention(user).length) ||
       (filter === 'custom' && custom(user)) || (filter === 'disabled' && !user.enabled)));
  }
  function renderDirectory() {
    const matches = matching(), list = document.createDocumentFragment(), rows = document.createDocumentFragment(), lanes = document.createDocumentFragment();
    for (const user of matches) {
      const choice = clone('#user-choice-template'), row = clone('#user-row-template');
      fill(choice, user); fill(row, user);
      row.classList.toggle('is-selected', user.name === selected);
      list.append(choice); rows.append(row);
    }
    for (const group of data.groups) {
      const members = matches.filter(user => user.group === group.name);
      if (!members.length) continue;
      const lane = clone('#group-lane-template');
      lane.querySelector('[data-lane-name]').textContent = group.name;
      lane.querySelector('[data-lane-summary]').textContent = groupLine(group);
      lane.querySelector('[data-lane-count]').textContent = plural(members.length, 'account');
      lane.querySelector('[data-lane-cards]').append(...members.map(user => { const card = clone('#user-card-template'); fill(card, user); return card; }));
      lanes.append(lane);
    }
    $('#user-list').replaceChildren(list); $('#user-rows').replaceChildren(rows); $('#user-lanes').replaceChildren(lanes);
    $('[data-match-count]').textContent = matches.length;
    $('#no-users').hidden = matches.length > 0;
    const people = accounts.filter(user => kind(user) === 'person').length, keys = sum(accounts.map(user => user.keys.length));
    $('#kpi-accounts').textContent = accounts.length;
    $('#kpi-accounts-sub').textContent = `${people} people · ${accounts.length - people} programs`;
    $('#kpi-sessions').textContent = sum(accounts.map(user => user.sessions));
    $('#kpi-keys').textContent = keys;
    $('#kpi-keys-sub').textContent = `on ${accounts.filter(user => user.keys.length).length} accounts`;
    $('#kpi-requests').textContent = compact.format(sum(accounts.map(week)));
    const needing = accounts.filter(user => attention(user).length).length;
    $('#kpi-attention').textContent = needing;
    $('.kpi-attention').dataset.state = needing ? 'warn' : 'ok';
    $('#show-attention').hidden = !needing;
  }

  // ---- the chosen account -------------------------------------------------------------------
  const abilityRows = {};
  function buildAbilities() {
    const holder = document.createDocumentFragment();
    for (const [title, names] of data.categories) {
      const block = clone('#ability-category-template');
      block.dataset.category = title;
      block.querySelector('[data-category-title]').textContent = title;
      for (const name of names) {
        const row = clone('#ability-row-template'), box = row.querySelector('[data-ability]');
        box.value = name; box.id = `can-${name}`;
        row.dataset.abilityRow = name;
        row.querySelector('[data-ability-name]').textContent = name;
        row.querySelector('[data-ability-words]').textContent = data.abilities[name];
        row.title = data.abilities[name];
        abilityRows[name] = row; block.append(row);
      }
      holder.append(block);
    }
    $('#ability-groups').replaceChildren(holder);
  }
  function badge(words, tone) { return Object.assign(document.createElement('span'), {className: `badge ${tone}`, textContent: words}); }
  function markPermissions() {
    const user = current(), draft = draftOf(user.name), can = new Set(draft.can), group = groupCan(draft.group), locked = isSelf(user);
    let inherited = 0, given = 0, taken = 0;
    for (const [name, row] of Object.entries(abilityRows)) {
      const box = row.querySelector('[data-ability]'), viaSuper = !can.has(name) && covers(can, name);
      const has = covers(can, name), fromGroup = covers(group, name);
      box.checked = has; box.disabled = locked || viaSuper;
      const diff = viaSuper ? '' : has && !fromGroup ? 'given' : !has && fromGroup ? 'taken' : '';
      row.dataset.diff = diff;
      const marks = [];
      if (viaSuper) marks.push(badge('in superuser', 'bg-azure-lt'));
      else if (has && fromGroup) { marks.push(badge('group', 'bg-secondary-lt')); inherited++; }
      if (diff === 'given') { marks.push(badge('given', 'bg-green-lt')); given++; }
      if (diff === 'taken') { marks.push(badge('taken away', 'bg-red-lt')); taken++; }
      if (data.own_key.includes(name)) marks.push(badge('program key', 'bg-yellow-lt'));
      row.querySelector('[data-ability-marks]').replaceChildren(...marks);
      const groupState = row.querySelector('[data-ability-group]');
      groupState.textContent = fromGroup ? 'Allowed' : '—';
      groupState.dataset.allowed = String(fromGroup);
    }
    const only = $('#changed-only').checked;
    for (const block of $$('.ability-category')) {
      let shown = 0;
      for (const row of block.querySelectorAll('[data-ability-row]')) { row.hidden = only && !row.dataset.diff; shown += !row.hidden; }
      block.hidden = !shown;
    }
    $('#no-changes').hidden = !(only && !given && !taken);
    $('#permission-counts').innerHTML = '';
    for (const [count, words, tone] of [[inherited, 'from its group', 'secondary'], [given, 'given', 'green'], [taken, 'taken away', 'red']]) {
      const item = document.createElement('span'); item.dataset.tone = tone;
      item.append(Object.assign(document.createElement('strong'), {textContent: count}), ` ${words}`);
      $('#permission-counts').append(item);
    }
  }
  function options(select, choices, value) {
    select.replaceChildren(...choices.map(([optionValue, words]) => new Option(words, optionValue, false, optionValue === value)));
    select.value = value;
  }
  function renderLimits() {
    const user = current(), draft = draftOf(user.name), group = groupOf(draft.group), locked = isSelf(user);
    $('#max-requests').value = draft.max_requests; $('#max-requests').placeholder = `as group · ${group.max_requests}`; $('#max-requests').max = group.max_requests;
    const allowed = data.levels.slice(0, data.levels.indexOf(group.max_sensitivity) + 1);
    options($('#max-sensitivity'), [['', `As group · ${group.max_sensitivity}`], ...allowed.map(level => [level, level])], allowed.includes(draft.max_sensitivity) ? draft.max_sensitivity : '');
    options($('#priority'), [['', `As group · ${group.priority}`], ...['high', 'normal', 'low'].map(level => [level, level])], draft.priority);
    $('#fewer-models').checked = draft.fewer;
    const models = [...new Set([...(group.models === '*' ? data.models : group.models), ...draft.models])];
    $('#model-choices').replaceChildren(...models.map((model, index) => {
      const choice = clone('#model-choice-template'), box = choice.querySelector('[data-model]');
      box.value = model; box.id = `model-${index}`; box.checked = draft.fewer && draft.models.includes(model); box.disabled = locked || !draft.fewer;
      choice.querySelector('[data-model-name]').textContent = model;
      return choice;
    }));
    $('#add-tools').value = draft.add_tools; $('#remove-tools').value = draft.remove_tools;
    $('#tools-hint').textContent = group.tools === '*' ? `Its group is shown every tool, so there is none to add. A * hides everything.` :
      group.tools.length ? `Its group is shown ${group.tools.join(', ')}. One tool per line; * stands for anything.` : 'Its group is shown no tools. One tool per line; * stands for anything.';
    for (const field of $$('#limits-panel [data-field]')) field.disabled = locked;
  }
  function renderKeys() {
    const user = current();
    const rows = user.keys.map(key => {
      const row = clone('#key-row-template');
      row.querySelector('[data-key-label]').textContent = key.label;
      row.querySelector('[data-key-made]').textContent = key.created;
      row.querySelector('[data-key-id]').textContent = key.id;
      const remove = row.querySelector('[data-key-remove]');
      remove.dataset.keyRemove = key.id; remove.disabled = isSelf(user);
      remove.setAttribute('aria-label', `Remove key ${key.label}`);
      return row;
    });
    $('#key-rows').replaceChildren(...(rows.length ? rows : [clone('#key-empty-template')]));
    $('#new-key-label').disabled = $('#make-key').disabled = isSelf(user);
  }
  function renderActivity() {
    const user = current(), previous = sum(user.daily.slice(-14, -7)), now = week(user);
    $('#usage-day').textContent = whole.format(user.daily.at(-1));
    $('#usage-week').textContent = whole.format(now);
    $('#usage-week-trend').textContent = previous ? `${now >= previous ? '↑' : '↓'} ${Math.abs(Math.round(100 * (now - previous) / previous))}% on the week before` : now ? 'New this week' : 'No requests';
    $('#usage-tokens').textContent = compact.format(sum(user.tokens));
    $('#usage-tokens-split').textContent = `${compact.format(user.tokens[0])} in · ${compact.format(user.tokens[1])} out`;
    $('#usage-spend').textContent = `$${user.spend.toFixed(2)}`;
    $('#usage-allowance').textContent = user.allowance ? `of a $${user.allowance.toFixed(2)} allowance` : 'No spending allowance';
    $('#allowance-bar').hidden = !user.allowance;
    $('#allowance-bar .usage-segment').style.inlineSize = `${Math.min(100, 100 * user.spend / (user.allowance || 1))}%`;
    $('#allowance-bar').dataset.state = user.allowance && user.spend / user.allowance >= .9 ? 'warn' : 'ok';
    spark($('#usage-spark'), user.daily);
  }
  function renderEditor() {
    const user = current(), draft = draftOf(user.name), self = isSelf(user), reasons = attention(user);
    fill($('.account-heading'), user);
    $('#selected-name').textContent = user.name;
    $('#selected-sub').textContent = `${user.group} · ${kind(user) === 'person' ? 'person' : 'program'}`;
    $('#selected-facts').textContent = [plural(user.keys.length, 'key'), user.sessions ? `signed in at ${plural(user.sessions, 'place')}` : 'not signed in'].join(' · ');
    $('#toggle-enabled').hidden = self;
    $('#toggle-enabled').textContent = user.enabled ? 'Disable…' : 'Enable';
    $('#toggle-enabled').className = `btn btn-sm ${user.enabled ? 'btn-outline-warning' : 'btn-primary'}`;
    $('#attention-notice').hidden = !reasons.length;
    $('#attention-text').textContent = reasons.join(' ');
    $('#self-notice').hidden = !self;
    $('#secret-panel').hidden = !(secrets && secrets.user === user.name);
    if (secrets && secrets.user === user.name) {
      $('#secret-rows').replaceChildren(...secrets.rows.map(([what, value]) => {
        const row = clone('#secret-row-template');
        row.querySelector('[data-secret-what]').textContent = what;
        row.querySelector('[data-secret-value]').textContent = value;
        row.querySelector('[data-secret-copy]').setAttribute('aria-label', `Copy ${what}`);
        return row;
      }));
    }
    $('#fact-password').textContent = {set: 'Set', temporary: 'Temporary · waiting for them to choose one'}[user.password] || 'None · uses API keys';
    $('#fact-code').textContent = user.second_step ? 'Set up' : 'Not used';
    $('#fact-sessions').textContent = user.sessions ? plural(user.sessions, 'place') : 'Not signed in';
    $('#make-password').disabled = self;
    $('#forget-code').hidden = !user.second_step; $('#forget-code').disabled = self;
    options($('#user-group'), data.groups.map(group => [group.name, group.name]), draft.group);
    $('#user-group').disabled = self;
    $('#group-summary').textContent = `${draft.group}: ${groupLine(groupOf(draft.group))}.`;
    if (shownFor !== user.name) { $('#rename').value = user.name; $('#rename').removeAttribute('aria-invalid'); }
    $('#lifecycle').hidden = self;
    shownFor = user.name;
    renderActivity(); renderKeys(); markPermissions(); renderLimits(); renderStatus();
  }
  function problem() {
    const draft = draftOf(selected), group = groupOf(draft.group), value = draft.max_requests;
    if (value && !(/^\d+$/.test(value) && Number(value) >= 1)) return 'Requests at once is a whole number from 1.';
    if (value && Number(value) > group.max_requests) return `Requests at once cannot be above its group’s ${group.max_requests}.`;
    if (draft.fewer && !draft.models.length) return 'Choose at least one model, or allow all of its group’s.';
    return '';
  }
  function renderStatus() {
    const what = changes(selected), others = accounts.filter(user => user.name !== selected && dirty(user.name)).length, issue = problem();
    $('#max-requests').setAttribute('aria-invalid', String(Boolean(issue) && issue.startsWith('Requests')));
    $('#save-state').textContent = issue ? `Check: ${issue}` : what.length ? `Unsaved: ${what.join(', ')}` : 'No unsaved changes';
    if (others) $('#save-state').textContent += ` · ${plural(others, 'other account')} with unsaved changes`;
    $('#save').disabled = Boolean(issue) || !what.length;
    $('#discard').disabled = !what.length;
    $('#save').textContent = `Save ${selected}`;
    for (const mark of $$('[data-user-choice]')) {
      const holder = mark.closest('[data-user-row]') || mark;
      holder.querySelector('[data-draft-mark]').hidden = !dirty(mark.dataset.userChoice);
    }
  }

  // ---- layout -------------------------------------------------------------------------------
  function setLayout(next) {
    layout = next;
    $('#workspace').dataset.layout = layout;
    for (const button of $$('[data-design]')) { const on = button.dataset.design === layout; button.classList.toggle('active', on); button.setAttribute('aria-pressed', String(on)); }
    $('#design-description').textContent = descriptions[layout];
    $('#design-note').textContent = notes[layout];
    setView(layout === 'lanes' ? 'overview' : 'profile');
    try { history.replaceState(null, '', `?design=${layout}`); } catch { /* A file address may refuse; the choice still applies. */ }
  }
  function setView(next) {
    view = next;
    $('#workspace').dataset.view = view;
  }
  function setSection(next) {
    section = next;
    $('#workspace').dataset.section = section;
    for (const button of $$('[data-section]')) { const on = button.dataset.section === section; button.classList.toggle('active', on); button.setAttribute('aria-pressed', String(on)); }
    for (const panel of $$('[data-panel]')) panel.classList.toggle('is-active', panel.dataset.panel === section);
  }
  function choose(name, focus = false) {
    if (!account(name)) return;
    selected = name;
    if (layout === 'lanes') setView('profile');
    renderDirectory(); renderEditor();
    if (focus) $('#selected-name').focus({preventScroll: false});
  }

  // ---- confirmation -------------------------------------------------------------------------
  let answer = null;
  function confirmAction(title, impact, button, check = '') {
    $('#action-title').textContent = title;
    $('#action-impact').textContent = impact;
    $('#confirm-action').textContent = button;
    $('#action-check-row').hidden = !check;
    $('#action-check').checked = false;
    $('#action-check-label').textContent = check;
    $('#confirm-action').disabled = Boolean(check);
    $('#action-dialog').returnValue = '';
    $('#action-dialog').showModal();
    return new Promise(resolve => { answer = resolve; });
  }
  $('#action-check').addEventListener('change', () => { $('#confirm-action').disabled = !$('#action-check').checked; });
  $('#action-dialog').addEventListener('close', () => { answer?.($('#action-dialog').returnValue === 'confirm'); answer = null; });

  function showSecrets(user, rows) {
    secrets = {user, rows};
    renderEditor();
    $('#secret-panel').scrollIntoView({block: 'nearest'});
  }
  function commit(message) {
    renderDirectory(); renderEditor();
    feedback(persist() ? message : `${message} The browser could not keep it, so it lasts until you reload.`);
  }

  // ---- events -------------------------------------------------------------------------------
  $('#workspace').addEventListener('click', event => {
    const choice = event.target.closest('[data-user-choice]');
    if (choice) return choose(choice.dataset.userChoice, layout === 'lanes');
    const design = event.target.closest('[data-design]');
    if (design) { setLayout(design.dataset.design); return renderDirectory(); }
    const tab = event.target.closest('button[data-section]');
    if (tab) return setSection(tab.dataset.section);
    const inert = event.target.closest('[data-inert]');
    if (inert) { event.preventDefault(); feedback(`${inert.textContent.trim()} is its own page; this preview stays on Users.`); }
  });
  $('#workspace').addEventListener('keydown', event => {
    const tabs = event.target.closest('.section-tabs');
    if (!tabs || !['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    const buttons = [...tabs.querySelectorAll('button')], index = buttons.indexOf(event.target);
    const next = buttons[(index + (event.key === 'ArrowRight' ? 1 : buttons.length - 1)) % buttons.length];
    next.focus(); setSection(next.dataset.section);
  });
  $('#user-search').addEventListener('input', renderDirectory);
  $('#user-filter').addEventListener('change', renderDirectory);
  $('#show-attention').addEventListener('click', () => {
    $('#user-filter').value = 'attention';
    if (layout === 'lanes') setView('overview');
    renderDirectory(); $('#user-filter').focus();
  });
  $('#back-to-overview').addEventListener('click', () => {
    setView('overview'); renderDirectory();
    $(`#user-lanes [data-user-choice="${CSS.escape(selected)}"]`)?.focus();
  });

  // Drafts: the group, permissions, limits and tools are saved together, as one change of the account.
  $('#user-editor').addEventListener('input', event => {
    const draft = draftOf(selected), field = event.target.dataset.field;
    if (event.target.matches('[data-ability]')) {
      const can = new Set(draft.can);
      event.target.checked ? can.add(event.target.value) : can.delete(event.target.value);
      if (event.target.value === 'superuser' && event.target.checked) for (const name of [...can]) if (covers(new Set(['superuser']), name)) can.delete(name);
      draft.can = [...can].sort();
      markPermissions();
    } else if (event.target.matches('[data-model]')) {
      draft.models = $$('#model-choices [data-model]:checked').map(box => box.value).sort();
    } else if (field === 'group') {
      // What it has of its own goes with it to the new group, as LoadCoach keeps it.
      const own = ownOf(draft);
      draft.group = event.target.value;
      const can = groupCan(draft.group);
      own.add.forEach(name => can.add(name)); own.remove.forEach(name => can.delete(name));
      draft.can = [...can].sort();
      $('#group-summary').textContent = `${draft.group}: ${groupLine(groupOf(draft.group))}.`;
      markPermissions(); renderLimits();
    } else if (field === 'fewer') {
      draft.fewer = event.target.checked;
      if (draft.fewer && !draft.models.length) draft.models = [...(groupOf(draft.group).models === '*' ? data.models : groupOf(draft.group).models)].sort();
      if (!draft.fewer) draft.models = [];
      renderLimits();
    } else if (field) {
      draft[field] = event.target.value;
    } else return;
    renderStatus();
  });
  $('#changed-only').addEventListener('change', markPermissions);
  // Enter in a field with its own button presses that button, not Save.
  for (const [field, button] of [['#rename', '#rename-button'], ['#new-key-label', '#make-key']]) {
    $(field).addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); $(button).click(); } });
  }
  $('#user-editor').addEventListener('submit', event => {
    event.preventDefault();
    if (problem() || !dirty(selected)) return;
    const user = current(), before = structuredClone(user), draft = drafts[selected];
    user.group = draft.group; user.own = ownOf(draft);
    if (!persist()) { Object.assign(user, before); renderStatus(); return feedback(`The browser could not save ${selected}. Your changes are still here.`); }
    delete drafts[selected];
    renderDirectory(); renderEditor();
    feedback(`Saved ${selected} in this browser.`);
  });
  $('#discard').addEventListener('click', () => { delete drafts[selected]; renderDirectory(); renderEditor(); feedback(`Discarded the changes to ${selected}.`); });

  // Immediate actions: each is its own change, as on the live page.
  $('#make-password').addEventListener('click', () => {
    const user = current();
    user.password = 'temporary';
    showSecrets(user.name, [['Temporary password', demoSecret('pass')]]);
    commit(`Made a temporary password for ${user.name}. It only lets them choose their own.`);
  });
  $('#forget-code').addEventListener('click', async () => {
    const user = current();
    if (!await confirmAction('Forget the authenticator?', `${user.name} will sign in with a password alone until they set up a new authenticator.`, 'Forget authenticator')) return;
    user.second_step = false; commit(`Forgot ${user.name}'s authenticator.`);
  });
  $('#make-key').addEventListener('click', () => {
    const user = current(), label = $('#new-key-label').value.trim();
    if (!label) { $('#new-key-label').setAttribute('aria-invalid', 'true'); $('#new-key-label').focus(); return feedback('Give the new key a label: what it is for.'); }
    $('#new-key-label').removeAttribute('aria-invalid');
    user.keys.push({label, created: data.today, id: `k-${demoSecret('x').slice(-6)}`});
    $('#new-key-label').value = '';
    showSecrets(user.name, [[`API key “${label}”`, demoSecret('key')]]);
    commit(`Made the key ${label} for ${user.name}.`);
  });
  $('#key-rows').addEventListener('click', async event => {
    const button = event.target.closest('[data-key-remove]');
    if (!button) return;
    const user = current(), key = user.keys.find(item => item.id === button.dataset.keyRemove);
    if (!await confirmAction('Remove this key?', `Programs using “${key.label}” stop working at once. This cannot be undone.`, 'Remove key', `Yes, remove ${key.label}`)) return;
    user.keys = user.keys.filter(item => item !== key); commit(`Removed the key ${key.label}.`);
  });
  $('#rename-button').addEventListener('click', () => {
    const user = current(), name = $('#rename').value.trim();
    const issue = !NAME.test(name) ? 'A name is lowercase letters, digits, dot, dash or underscore, at most 64.' : name !== user.name && account(name) ? `There is already an account named ${name}.` : '';
    $('#rename').setAttribute('aria-invalid', String(Boolean(issue)));
    if (issue) return feedback(issue);
    if (name === user.name) return feedback('That is its name already.');
    const old = user.name;
    if (drafts[old]) { drafts[name] = drafts[old]; delete drafts[old]; }
    if (secrets?.user === old) secrets.user = name;
    user.name = name; selected = name; shownFor = '';
    commit(`Renamed ${old} to ${name}. Its keys and history go with it.`);
  });
  $('#toggle-enabled').addEventListener('click', async () => {
    const user = current();
    if (user.enabled && !await confirmAction(`Disable ${user.name}?`, 'Its sessions end and its keys stop working until it is enabled again. Nothing is deleted.', 'Disable')) return;
    user.enabled = !user.enabled;
    if (!user.enabled) user.sessions = 0;
    commit(`${user.enabled ? 'Enabled' : 'Disabled'} ${user.name}.`);
  });
  $('#remove-account').addEventListener('click', async () => {
    const user = current();
    if (!await confirmAction(`Remove ${user.name}?`, 'The account and its keys are deleted. Its history stays in the log. This cannot be undone.', 'Remove account', `Yes, remove ${user.name}`)) return;
    accounts = accounts.filter(item => item !== user); delete drafts[user.name];
    selected = accounts.find(item => !isSelf(item))?.name || data.viewer;
    if (layout === 'lanes') setView('overview');
    commit(`Removed ${user.name}.`);
  });
  $('#dismiss-secrets').addEventListener('click', () => { secrets = null; renderEditor(); feedback('The secrets are no longer shown. Make new ones if they were lost.'); });
  $('#secret-rows').addEventListener('click', async event => {
    const button = event.target.closest('[data-secret-copy]');
    if (!button) return;
    try { await navigator.clipboard.writeText(button.closest('[data-secret-row]').querySelector('[data-secret-value]').textContent); feedback('Copied. Sample secrets cannot sign in.'); }
    catch { feedback('Clipboard unavailable. Select the text and copy it.'); }
  });

  // Adding an account.
  options($('#add-group'), [['', 'Choose a group'], ...data.groups.map(group => [group.name, group.name])], '');
  $('#add-group').options[0].disabled = true;
  function toggleAdd(open) {
    $('#add-panel').hidden = !open;
    $('#add-account').setAttribute('aria-expanded', String(open));
    (open ? $('#add-name') : $('#add-account')).focus();
  }
  $('#add-account').addEventListener('click', () => toggleAdd($('#add-panel').hidden));
  $('#close-add').addEventListener('click', () => toggleAdd(false));
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('#add-panel').hidden && !$('#action-dialog').open) toggleAdd(false); });
  $('#add-form').addEventListener('change', event => {
    if (event.target.name === 'kind') {
      const person = event.target.value === 'person';
      $('#add-password').checked = person; $('#add-key').checked = !person;
    }
    if (event.target.id === 'add-group') $('#add-group-hint').textContent = groupLine(groupOf(event.target.value));
  });
  $('#add-form').addEventListener('submit', event => {
    event.preventDefault();
    const name = $('#add-name').value.trim(), group = $('#add-group').value, password = $('#add-password').checked, key = $('#add-key').checked;
    const label = $('#add-key-label').value.trim() || 'first';
    const issue = !NAME.test(name) ? 'A name is lowercase letters, digits, dot, dash or underscore, at most 64.' : account(name) ? `There is already an account named ${name}.` :
      !group ? 'Choose a group.' : !password && !key ? 'Give it a temporary password, an API key, or both: otherwise it cannot be used.' : '';
    $('#add-error').textContent = issue;
    $('#add-name').setAttribute('aria-invalid', String(issue.startsWith('A name') || issue.startsWith('There is')));
    if (issue) return;
    accounts.push({name, group, enabled: true, password: password ? 'temporary' : '', second_step: false, sessions: 0,
      keys: key ? [{label, created: data.today, id: `k-${demoSecret('x').slice(-6)}`}] : [], own: {add: [], remove: [], add_tools: [], remove_tools: []},
      daily: Array(28).fill(0), tokens: [0, 0], spend: 0, allowance: 0});
    $('#add-form').reset(); $('#add-group-hint').textContent = 'Choose deliberately: none is given by default.';
    $('#add-panel').hidden = true; $('#add-account').setAttribute('aria-expanded', 'false');
    $('#user-search').value = ''; $('#user-filter').value = 'all';
    selected = name; setSection('activity');
    if (layout === 'lanes') setView('profile');
    showSecrets(name, [password && ['Temporary password', demoSecret('pass')], key && [`API key “${label}”`, demoSecret('key')]].filter(Boolean));
    commit(`Added ${name} to ${group}.`);
    $('#selected-name').focus();
  });

  // The palette follows the theme form, as on the live console, so every token changes together.
  $('#theme').addEventListener('click', () => {
    const select = $('[data-theme-form] [name=mode]');
    select.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    select.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = select.value === 'dark' ? 'Light mode' : 'Dark mode';
  });
  $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  $('#reset').addEventListener('click', () => {
    try { localStorage.removeItem(STORE); } catch { /* Session reset still works. */ }
    accounts = structuredClone(data.users); drafts = {}; secrets = null; selected = 'maya.r'; shownFor = '';
    $('#user-search').value = ''; $('#user-filter').value = 'all';
    if (layout === 'lanes') setView('overview');
    renderDirectory(); renderEditor(); feedback('Sample accounts reset.');
  });

  $('#selected-name').tabIndex = -1;
  buildAbilities();
  const requested = new URL(location.href).searchParams.get('design');
  setLayout(Object.hasOwn(descriptions, requested) ? requested : 'directory');
  setSection('activity'); renderDirectory(); renderEditor();
})();
