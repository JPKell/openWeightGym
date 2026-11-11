// Role Studio keeps drafts in page memory. The server remains the source of routing decisions.
(() => {
  'use strict';
  if (window.suiteRoleStudio) return;
  window.suiteRoleStudio = true;
  const drafts = new Map(), baselines = new WeakMap(), baselineRows = new WeakMap(), submitted = new WeakMap();
  let initialized = null, account = '', mode = '', serial = 0;
  const root = () => document.querySelector('[data-role-studio]');
  const editor = page => page?.querySelector('[data-role-editor]');
  const rows = form => [...form.querySelectorAll('[data-role-candidate]')];
  const fields = form => [...form.elements].filter(field => field.name &&
    !['model', 'weight', 'name'].includes(field.name) && !['submit', 'button'].includes(field.type));
  const capture = form => ({
    models: rows(form).map(row => ({name: row.dataset.modelName, weight: row.querySelector('[data-weight]').value, hidden: row.hidden})),
    fields: fields(form).map(field => ({name: field.name, value: field.value, checked: field.checked})),
    extra: {model: form.querySelector('#add-role-model').value, weight: form.querySelector('#add-role-weight').value},
  });
  const digest = state => JSON.stringify({...state, models: [...state.models].sort((a, b) => a.name.localeCompare(b.name))});
  function savedState(page, form) {
    // Failed-form middleware echoes submitted inputs. The server's saved settings are
    // separate, so Discard and dirty indicators still compare against accepted values.
    const settings = JSON.parse(page.dataset.roleSettings), state = capture(form), controls = fields(form);
    state.models.forEach(model => { model.weight = String(settings.models[model.name]); model.hidden = false; });
    state.fields.forEach((field, index) => {
      if (field.name === 'loaded_bonus') field.value = String(settings.loaded_bonus ?? 25);
      else if (field.name === 'fast_loaded_bonus') field.value = String(settings.fast_loaded_bonus ?? 100);
      else if (field.name === 'benchmark-weight') field.value = String(settings.benchmarks?.[controls[index].dataset.benchmarkId] ?? 0);
      else if (field.name === 'missing') field.value = settings.missing ?? 'manual';
      else if (field.name === 'stale') field.value = settings.stale ?? 'warn';
      else if (field.name === 'max_age_days') field.value = String(settings.max_age_days ?? 0);
      else if (field.name === 'requires-tokens') field.value = String(settings.requires?.tokens ?? 0);
      else if (field.name.startsWith('requires-')) field.checked = Boolean(settings.requires?.[field.name.slice(9)]);
    });
    state.extra = {model: '', weight: '1'};
    return state;
  }
  function remember(page) {
    const form = editor(page);
    if (!form || !baselines.has(form)) return;
    const state = capture(form);
    if (digest(state) === digest(baselines.get(form))) drafts.delete(page.dataset.role);
    else drafts.set(page.dataset.role, state);
  }
  function enhanceRow(row) {
    const weight = row.querySelector('[data-weight]'), slider = row.querySelector('[data-weight-slider]');
    slider.hidden = false;
    slider.value = Math.min(10, Number(weight.value) || 0);
    row.querySelector('[data-role-remove]').hidden = false;
    row.classList.toggle('is-paused', weight.value !== '' && Number(weight.value) === 0);
  }
  function newRow(page, name, weight) {
    const row = page.querySelector('#role-candidate-template').content.firstElementChild.cloneNode(true);
    row.dataset.modelName = name;
    row.querySelector('[name=model]').value = name;
    row.querySelector('[data-candidate-name]').textContent = name;
    row.querySelector('.model-mark').textContent = name[0]?.toUpperCase() || 'M';
    const input = row.querySelector('[data-weight]');
    input.id = `added-role-model-${++serial}`;
    input.value = weight;
    input.setAttribute('aria-label', `weight of ${name}`);
    row.querySelector('[data-weight-label]').htmlFor = input.id;
    row.querySelector('[data-weight-slider]').setAttribute('aria-label', `Adjust preference for ${name}`);
    row.querySelector('[data-role-remove]').setAttribute('aria-label', `Remove ${name} from role`);
    editor(page).querySelector('[data-role-candidates]').append(row);
    enhanceRow(row);
    return row;
  }
  function restore(page, state) {
    const form = editor(page), controls = fields(form);
    state.fields.forEach((value, index) => {
      const field = controls[index];
      if (!field || field.name !== value.name) return;
      if (['checkbox', 'radio'].includes(field.type)) field.checked = value.checked;
      else field.value = value.value;
    });
    state.models.forEach(model => {
      const row = rows(form).find(row => row.dataset.modelName === model.name) || newRow(page, model.name, model.weight);
      row.querySelector('[data-weight]').value = model.weight;
      row.hidden = model.hidden;
      enhanceRow(row);
    });
    form.querySelector('#add-role-model').value = state.extra.model;
    form.querySelector('#add-role-weight').value = state.extra.weight;
    // Recovered advanced edits should be discoverable after a role switch or rejected save.
    if (state.fields.some((value, index) => JSON.stringify(value) !== JSON.stringify(baselines.get(form).fields[index]))) {
      form.querySelector('#role-tuning').open = true;
    }
  }
  function show(page) {
    const form = editor(page), changed = drafts.has(page.dataset.role);
    for (const link of page.querySelectorAll('[data-role-choice]')) {
      link.querySelector('[data-role-draft-mark]').hidden = !drafts.has(link.dataset.roleChoice);
    }
    const count = page.querySelector('[data-role-draft-count]');
    if (count) { count.hidden = !drafts.size; count.textContent = `${drafts.size} role${drafts.size === 1 ? '' : 's'} with unsaved changes`; }
    if (!form) return;
    form.querySelector('[data-role-save-state]').textContent = changed ? 'Unsaved changes' : 'No unsaved changes';
    form.querySelector('[data-role-discard]').disabled = !changed;
    page.querySelector('[data-role-preview-stale]').hidden = !changed;
    const asked = page.dataset.role + mode;
    let picked = '';
    for (const panel of page.querySelectorAll('[data-role-pick]')) {
      panel.hidden = panel.dataset.rolePick !== asked;
      panel.querySelector('[data-role-pick-label]').textContent = changed ? 'LAST SAVED PICK' : 'WOULD PICK NOW';
      if (!panel.hidden) picked = panel.dataset.picked;
    }
    for (const row of rows(form)) row.classList.toggle('is-picked', !changed && !row.hidden && row.dataset.modelName === picked);
    const select = form.querySelector('#add-role-model');
    for (const option of select.options) {
      option.disabled = Boolean(option.value && rows(form).some(row => row.dataset.modelName === option.value && !row.hidden));
    }
    form.querySelector('[data-role-add]').disabled = !select.value;
  }
  function setup() {
    const page = root();
    if (!page || page === initialized) return;
    initialized = page;
    const identity = document.documentElement.dataset.themeUser || '';
    if (identity !== account) { drafts.clear(); account = identity; }
    const known = new Set([...page.querySelectorAll('[data-role-choice]')].map(link => link.dataset.roleChoice));
    for (const name of drafts.keys()) if (!known.has(name)) drafts.delete(name);
    const form = editor(page);
    if (!form) return;
    baselines.set(form, savedState(page, form));
    const savedRows = form.querySelector('[data-role-candidates]').cloneNode(true);
    for (const row of savedRows.querySelectorAll('[data-role-candidate]')) {
      row.querySelector('[data-weight]').value = baselines.get(form).models.find(model => model.name === row.dataset.modelName).weight;
      row.querySelector('[name=model]').value = row.dataset.modelName;
    }
    baselineRows.set(form, savedRows);
    const draft = drafts.get(page.dataset.role);
    if (draft) restore(page, draft);
    rows(form).forEach(enhanceRow);
    page.querySelectorAll('[data-role-copy], [data-role-mode-control], [data-role-add], [data-role-discard]').forEach(control => { control.hidden = false; });
    form.querySelector('[data-add-weight]').hidden = true;
    page.querySelector('#role-request-mode').value = mode;
    remember(page); show(page);
    document.dispatchEvent(new Event('weightroom:role-preferences'));
  }
  document.addEventListener('input', event => {
    const page = root(), form = editor(page);
    if (page?.contains(event.target) && event.target.matches('[data-name-validation]')) {
      event.target.setCustomValidity(event.target.validity.patternMismatch ? event.target.dataset.nameValidation : '');
    }
    if (!form?.contains(event.target)) return;
    if (event.target.matches('[data-weight-slider]')) {
      event.target.closest('[data-role-candidate]').querySelector('[data-weight]').value = event.target.value;
    }
    const row = event.target.closest('[data-role-candidate]');
    if (row) enhanceRow(row);
    remember(page); show(page);
  });
  document.addEventListener('invalid', event => {
    if (root()?.contains(event.target) && event.target.matches('[data-name-validation]')) {
      event.target.setCustomValidity(event.target.validity.patternMismatch ? event.target.dataset.nameValidation : '');
    }
  }, true);
  document.addEventListener('change', event => {
    const page = root();
    if (!page?.contains(event.target)) return;
    if (event.target.id === 'role-request-mode') mode = event.target.value;
    else if (editor(page)?.contains(event.target)) remember(page);
    show(page);
  });
  document.addEventListener('click', async event => {
    const page = root(), form = editor(page);
    if (!page?.contains(event.target)) return;
    if (event.target.closest('[data-create-role]')) {
      const panel = page.querySelector('#create-role');
      panel.open = true;
      page.querySelector('#new-role').focus();
    }
    if (!form) return;
    if (event.target.closest('[data-role-add]')) {
      const select = form.querySelector('#add-role-model'), name = select.value;
      if (!name) return;
      const existing = rows(form).find(row => row.dataset.modelName === name);
      const row = existing || newRow(page, name, form.querySelector('#add-role-weight').value);
      row.hidden = false;
      row.querySelector('[data-weight]').value = form.querySelector('#add-role-weight').value;
      enhanceRow(row);
      select.value = ''; remember(page); show(page);
      row.querySelector('[data-weight]').focus();
    }
    const remove = event.target.closest('[data-role-remove]');
    if (remove) {
      const row = remove.closest('[data-role-candidate]');
      row.querySelector('[data-weight]').value = ''; row.hidden = true;
      // Keep paired model/weight fields for the existing POST removal contract.
      const select = form.querySelector('#add-role-model');
      if (![...select.options].some(option => option.value === row.dataset.modelName)) select.add(new Option(row.dataset.modelName, row.dataset.modelName));
      remember(page); show(page); select.focus();
    }
    if (event.target.closest('[data-role-discard]')) {
      form.querySelector('[data-role-candidates]').replaceWith(baselineRows.get(form).cloneNode(true));
      restore(page, baselines.get(form));
      delete form.dataset.weightsEdited;
      rows(form).forEach(enhanceRow);
      remember(page); show(page);
      document.dispatchEvent(new Event('weightroom:role-preferences'));
    }
    const copy = event.target.closest('[data-role-copy]');
    if (copy) {
      const status = page.querySelector('[data-role-copy-status]');
      try { await navigator.clipboard.writeText(copy.dataset.roleCopy); status.textContent = `Copied ${copy.dataset.roleCopy}. Paste it into your app’s model field.`; }
      catch { status.textContent = `Model name: ${copy.dataset.roleCopy}. Select and copy it into your app’s model field.`; }
    }
  });
  document.addEventListener('htmx:beforeRequest', event => {
    const form = event.detail.elt?.closest('[data-role-editor]');
    if (form) submitted.set(form, digest(capture(form)));
  });
  document.addEventListener('htmx:beforeSwap', event => {
    const page = root(), detail = event.detail;
    if (!page || detail.target?.id !== 'page-content') return;
    remember(page);
    const form = detail.requestConfig?.elt?.closest('[data-role-editor]');
    if (detail.requestConfig?.verb !== 'post' || !form || detail.xhr.status >= 400) return;
    const response = new DOMParser().parseFromString(detail.xhr.responseText, 'text/html');
    if (response.querySelector('[data-role-studio]') && !response.querySelector('.said-box.alert-danger') &&
        submitted.get(form) === digest(capture(form))) drafts.delete(page.dataset.role);
  });
  document.addEventListener('htmx:afterSwap', setup);
  document.addEventListener('htmx:historyRestore', setup);
  window.addEventListener('beforeunload', event => {
    if (drafts.size) { event.preventDefault(); event.returnValue = ''; }
  });
  setup();
})();
