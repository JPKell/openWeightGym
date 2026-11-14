/* Sample-only proposals: no fetch, account storage, or live mutations. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const root = $('#workspace');
  const descriptions = {
    desk: 'A stable settings rail, a focused editor and useful context. Best fit for the existing console.',
    overview: 'An approachable personal home. Discover each category, then open its focused editor.',
    notebook: 'Every category in one document. Expand sections to review several preferences together.',
  };
  const guidance = {
    account: 'Your password and keys belong here. Group membership and permissions stay with your administrator.',
    keys: 'Use a descriptive label for each application. Replace a key before removing it from a running client.',
    appearance: 'Themes remain specific to your account in each browser. Machine meters are an existing account preference.',
    audio: 'These choices initialize media pages. Only models and voices available to your account should appear.',
    workspace: 'Personal defaults should shorten repeated setup. They cannot expand access to routes, models or roles.',
    privacy: 'Preference controls cannot override deployment retention or logging. Alert delivery is a proposed addition.',
    logs: 'Keep activity scoped to the signed-in account. Date and time controls should apply consistently to the live ledger.',
  };
  const say = (text, bad = false) => {
    const feedback = $('#feedback');
    feedback.textContent = text;
    feedback.classList.toggle('alert-danger', bad);
    feedback.classList.toggle('alert-success', !bad);
    feedback.hidden = !text;
  };
  const snapshot = form => [...form.elements].filter(el => el.name).map(el => ({name: el.name, value: el.value, checked: el.checked}));
  const saved = new Map();
  let initialized = false;
  function updateOverview() {
    $('[data-overview=account]').textContent = `${$('[data-session-count]').textContent} active sign-ins`;
    $('[data-overview=keys]').textContent = `${$$('[data-key-row]').length} application keys`;
    $('[data-overview=appearance]').textContent = `${document.documentElement.dataset.siteTheme} · ${document.documentElement.dataset.bsTheme}`;
    const audio = saved.get($('#audio-form'));
    if (audio) $('[data-overview=audio]').textContent = `${audio.find(item => item.name === 'ap-format').value.toUpperCase()} · personal defaults`;
    const workspace = saved.get($('#workspace-form'));
    if (workspace) {
      const key = workspace.find(item => item.name === 'start-page').value;
      $('[data-overview=workspace]').textContent = `Start on ${{status:'Status',chat:'Text chat',requests:'My model requests'}[key]}`;
    }
  }
  function updateDraft(form) {
    if (!initialized) return;
    const dirty = JSON.stringify(snapshot(form)) !== JSON.stringify(saved.get(form));
    form.querySelector('[data-form-state]').textContent = dirty ? 'Unsaved changes' : 'No unsaved changes';
    const key = form.closest('[data-panel]').dataset.panel;
    $(`[data-draft-for="${key}"]`).hidden = !dirty;
  }
  function syncAppearance() {
    root.dataset.textSize = $('#text-size').value;
    root.dataset.motion = $('#motion').value;
    $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  }
  function dismissSecret() {
    $('#sample-secret').textContent = '';
    $('#secret-panel').hidden = true;
  }
  function selectSection(section, focus = false) {
    if (section !== root.dataset.section) {
      dismissSecret();
      $('#password-form').reset();
      $('#password-error').textContent = '';
    }
    root.dataset.section = section;
    updateOverview();
    root.dataset.view = section === 'home' ? 'home' : 'editor';
    $('#overview-home').hidden = section !== 'home';
    $$('button[data-section]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.section === section)));
    $$('[data-panel]').forEach(panel => {
      panel.hidden = root.dataset.layout !== 'notebook' && panel.dataset.panel !== section;
      panel.querySelector('summary').tabIndex = root.dataset.layout === 'notebook' ? 0 : -1;
      if (root.dataset.layout !== 'notebook') panel.open = panel.dataset.panel === section;
      else if (panel.dataset.panel === section) panel.open = true;
    });
    const selected = $(`[data-panel="${section}"]`);
    if (selected) $('#breadcrumb').textContent = selected.querySelector('.card-heading').textContent;
    $('#context-copy').textContent = guidance[section] || guidance.account;
    if (focus) {
      const target = root.dataset.layout === 'notebook' ? selected?.querySelector('summary') : $('#account-main');
      target?.focus({preventScroll: true});
      if (root.dataset.layout === 'notebook') selected?.scrollIntoView({block: 'start'});
    }
  }
  $$('[data-design]').forEach(button => button.addEventListener('click', () => {
    root.dataset.layout = button.dataset.design;
    $$('[data-design]').forEach(choice => { const active = choice === button; choice.classList.toggle('active', active); choice.setAttribute('aria-pressed', String(active)); });
    $('#design-description').textContent = descriptions[root.dataset.layout];
    $('#recommended').hidden = root.dataset.layout !== 'desk';
    selectSection(root.dataset.layout === 'overview' ? 'home' : (root.dataset.section === 'home' ? 'account' : root.dataset.section));
  }));
  $$('button[data-section]').forEach(button => button.addEventListener('click', () => selectSection(button.dataset.section, true)));
  // In the focused proposals, a panel's summary is a static header; notebook uses native disclosures.
  $$('[data-panel] > summary').forEach(summary => summary.addEventListener('click', event => {
    if (root.dataset.layout !== 'notebook') event.preventDefault();
    else {
      const key = summary.parentElement.dataset.panel;
      if (root.dataset.section !== key) { dismissSecret(); $('#password-form').reset(); $('#password-error').textContent = ''; }
      root.dataset.section = key;
      $$('.account-nav').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.section === key)));
    }
  }));
  $$('[data-preferences]').forEach(form => {
    form.addEventListener('input', () => updateDraft(form));
    form.addEventListener('change', () => { updateDraft(form); syncAppearance(); });
    form.addEventListener('submit', event => {
      event.preventDefault();
      saved.set(form, snapshot(form));
      updateDraft(form);
      updateOverview();
      say('Sample preferences saved for this preview tab.');
    });
    form.querySelector('[data-discard]').addEventListener('click', () => {
      const values = saved.get(form);
      [...form.elements].filter(el => el.name).forEach((el, index) => {
        el.value = values[index].value;
        if (['checkbox', 'radio'].includes(el.type)) el.checked = values[index].checked;
      });
      if (form.id === 'appearance-form') {
        const mode = values.find(item => item.name === 'mode').value;
        form.querySelector('[name=palette]:checked').dispatchEvent(new Event('change', {bubbles: true}));
        $('#appearance-mode').value = mode;
        $('#appearance-mode').dispatchEvent(new Event('change', {bubbles: true}));
      }
      syncAppearance(); updateDraft(form); timestamp(); say('Changes discarded in this preview.');
    });
  });
  $('#theme').addEventListener('click', () => {
    $('#appearance-mode').value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    $('#appearance-mode').dispatchEvent(new Event('change', {bubbles: true}));
    syncAppearance();
  });
  $('#reset').addEventListener('click', () => location.reload());
  document.addEventListener('weightroom:theme', syncAppearance);
  $('#password-form').addEventListener('submit', event => {
    event.preventDefault();
    if ($('#new_password').value !== $('#new_again').value) {
      $('#password-error').textContent = 'The new passwords do not match.'; $('#new_again').focus(); return;
    }
    $('#password-error').textContent = ''; event.target.reset();
    $('#other-session').hidden = true; $('[data-session-count]').textContent = '1'; $('#session-empty').hidden = false;
    updateOverview();
    say('Sample password change complete; the other sample sign-in has ended.');
  });
  $('#new_again').addEventListener('input', () => { $('#password-error').textContent = ''; });
  function confirmAction(title, action) {
    const dialog = $('#danger-confirmation');
    $('#danger-confirmation-title').textContent = title;
    const check = $('#danger-confirmation-check');
    check.checked = false;
    const confirm = dialog.querySelector('[value=confirm]');
    confirm.disabled = true;
    check.onchange = () => { confirm.disabled = !check.checked; };
    dialog.returnValue = '';
    dialog.addEventListener('close', () => { if (dialog.returnValue === 'confirm' && check.checked) action(); }, {once: true});
    dialog.showModal();
  }
  $('[data-revoke-session]').addEventListener('click', () => confirmAction('Sign out the other sample device?', () => {
    $('#other-session').hidden = true; $('[data-session-count]').textContent = '1'; $('#session-empty').hidden = false; updateOverview(); say('Other sample sign-in ended.');
  }));
  function keyCount() {
    const count = $$('[data-key-row]').length;
    $('[data-key-count]').textContent = String(count);
    $('#keys-empty').hidden = count > 0;
    updateOverview();
  }
  $('#key-rows').addEventListener('click', event => {
    const button = event.target.closest('[data-remove-key]'); if (!button) return;
    const row = button.closest('[data-key-row]');
    confirmAction('Remove this sample API key?', () => { row.remove(); keyCount(); dismissSecret(); say('Sample key removed.'); });
  });
  $('#key-form').addEventListener('submit', event => {
    event.preventDefault();
    const label = $('#label').value.trim();
    if (!label) { $('#label').setCustomValidity('Enter a label.'); $('#label').reportValidity(); return; }
    const id = `demo-${crypto.randomUUID().slice(0, 8)}`;
    const row = $('#key-template').content.firstElementChild.cloneNode(true);
    row.querySelector('[data-key-label]').textContent = label;
    row.querySelector('[data-key-made]').textContent = '14 Nov 2026';
    row.querySelector('[data-key-id]').textContent = id;
    row.querySelector('[data-remove-key]').setAttribute('aria-label', `Remove ${label} key`);
    $('#key-rows').insertBefore(row, $('#keys-empty'));
    $('#sample-secret').textContent = `DEMO-ONLY-NOT-A-CREDENTIAL-${id}`;
    $('#secret-panel').hidden = false;
    event.target.reset(); keyCount(); say('Sample key created. Copy the illustrative value before leaving.');
  });
  $('#label').addEventListener('input', () => $('#label').setCustomValidity(''));
  $('#dismiss-secret').addEventListener('click', dismissSecret);
  function filterActivity() {
    let count = 0;
    const search = $('#activity-search').value.toLowerCase().trim(), kind = $('#activity-kind').value;
    $$('[data-event-kind]').forEach(row => {
      row.hidden = !(row.textContent.toLowerCase().includes(search) && (kind === 'all' || row.dataset.eventKind === kind));
      if (!row.hidden) count++;
    });
    $('#activity-empty').hidden = count > 0; $('#activity-count').textContent = `${count} sample records`;
  }
  $('#activity-search').addEventListener('input', filterActivity);
  $('#activity-kind').addEventListener('change', filterActivity);
  function timestamp() {
    const zone = $('#timezone').value;
    const date = new Date('2026-11-15T00:30:00Z');
    const timeZone = zone === 'device' ? Intl.DateTimeFormat().resolvedOptions().timeZone : zone;
    const dateOptions = $('#date-format').value === 'local' ? {dateStyle: 'short'} : {year: 'numeric', month: 'short', day: '2-digit'};
    const locale = $('#date-format').value === 'local' ? undefined : 'en-GB';
    let formatted = new Intl.DateTimeFormat(locale, {...dateOptions, timeZone}).format(date);
    if ($('#date-format').value === 'iso') {
      const parts = new Intl.DateTimeFormat('en-CA', {timeZone, year:'numeric', month:'2-digit', day:'2-digit'}).formatToParts(date);
      const fields = Object.fromEntries(parts.map(part => [part.type, part.value])); formatted = `${fields.year}-${fields.month}-${fields.day}`;
    }
    const time = new Intl.DateTimeFormat(undefined, {timeZone, hour: '2-digit', minute: '2-digit', hour12: $('#clock').value === '12'}).format(date);
    $('#timestamp-preview').textContent = `${formatted}, ${time} · ${timeZone}`;
  }
  $('#workspace-form').addEventListener('change', timestamp);
  $('#export-preferences').addEventListener('click', () => {
    const preferences = Object.fromEntries([...saved].map(([form, values]) => [form.id, values]));
    const url = URL.createObjectURL(new Blob([JSON.stringify({sample: true, preferences}, null, 2)], {type:'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'sample-account-preferences.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); say('Exported sample preferences without credentials or activity.');
  });
  document.addEventListener('DOMContentLoaded', () => {
    $('#appearance-mode').value = 'light';
    $('#appearance-mode').dispatchEvent(new Event('change', {bubbles: true}));
    $$('[data-preferences]').forEach(form => saved.set(form, snapshot(form)));
    initialized = true; syncAppearance(); selectSection('account'); timestamp();
  });
})();
