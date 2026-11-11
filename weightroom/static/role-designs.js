// Sample-only Roles prototypes. Never call inference, console APIs, or external services.
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const all = selector => [...document.querySelectorAll(selector)];
  const clone = value => JSON.parse(JSON.stringify(value));
  const storageKey = 'owg-role-designs-v1';
  const catalogue = [
    {id: 'deepseek-coder-v2', name: 'DeepSeek Coder V2', detail: 'Local · 128K context', context: 131072, tools: true, pictures: false, local: true, speed: 38, load: 24, reliability: .99, code_retrieval: 92, ifeval: 82},
    {id: 'qwen3-32b', name: 'Qwen3 32B', detail: 'Local · 32K context', context: 32768, tools: true, pictures: false, local: true, speed: 55, load: 18, reliability: .99, code_retrieval: 84, ifeval: 94},
    {id: 'qwen3-8b', name: 'Qwen3 8B', detail: 'Local · 32K context', context: 32768, tools: true, pictures: false, local: true, speed: 90, load: 8, reliability: .98, code_retrieval: 70, ifeval: 88},
    {id: 'qwen2.5-vl-7b', name: 'Qwen2.5 VL 7B', detail: 'Local · 32K context · Images', context: 32768, tools: false, pictures: true, local: true, speed: 65, load: 9, reliability: .97, code_retrieval: 60, ifeval: 80},
  ];
  const byId = id => catalogue.find(model => model.id === id);
  const makeRole = (id, name, models, requires = {}) => ({id, name, models, requires: {tokens: 0, pictures: false, tools: false, stays: false, ...requires}, benchmarks: {code_retrieval: 0, ifeval: 0}, loaded_bonus: 25, fast_loaded_bonus: 100});
  const initial = [
    makeRole('sample-code', 'code', [{id: 'deepseek-coder-v2', weight: 3}, {id: 'qwen3-32b', weight: 2.8}, {id: 'qwen3-8b', weight: 1}], {tools: true}),
    makeRole('sample-chat', 'chat', [{id: 'qwen3-32b', weight: 3}, {id: 'qwen3-8b', weight: 2}]),
    makeRole('sample-vision', 'vision', [{id: 'qwen2.5-vl-7b', weight: 3}], {pictures: true}),
  ];
  let saved = clone(initial);
  try {
    const stored = JSON.parse(localStorage.getItem(storageKey));
    if (Array.isArray(stored) && stored.length && stored.every(role =>
      typeof role.id === 'string' && typeof role.name === 'string' && Array.isArray(role.models) &&
      role.models.every(model => byId(model.id) && Number.isFinite(model.weight) && model.weight >= 0) &&
      role.requires && role.benchmarks && Number.isFinite(role.loaded_bonus) && Number.isFinite(role.fast_loaded_bonus))) saved = stored;
  } catch { /* A file preview can run with storage unavailable. Saving reports that limit. */ }
  const requestedLayout = location.hash.slice(1);
  let drafts = clone(saved), selected = drafts[0].id,
    layout = ['studio', 'guided', 'canvas'].includes(requestedLayout) ? requestedLayout : 'studio', step = 1;
  const current = () => drafts.find(role => role.id === selected);
  const dirty = role => JSON.stringify(role) !== JSON.stringify(saved.find(one => one.id === role.id));
  const displayNumber = value => Number.isFinite(value) ? Number(value.toPrecision(4)).toLocaleString('en', {maximumFractionDigits: 3}) : '—';
  const validWeight = value => Number.isFinite(value) && value >= 0 && value <= 1e100;

  function announce(message) {
    $('#said').textContent = message;
    $('#said').className = 'small text-primary mb-3';
  }

  function requestName() {
    const mode = $('#request-mode').value;
    return `${current().name || 'your-role'}${mode === 'default' ? '' : `:${mode}`}`;
  }

  function nameError(name, id) {
    if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(name)) return 'Use 1–64 letters, digits, dots, underscores, or dashes. Start with a letter or digit.';
    if (drafts.some(role => role.id !== id && role.name === name)) return 'That role name is already in use. Choose another name.';
    if (catalogue.some(model => model.id === name)) return 'That name belongs to a model. Choose a different role name.';
    return '';
  }

  function renderRoleList() {
    const list = $('#role-list');
    list.replaceChildren();
    drafts.forEach(role => {
      const item = $('#role-choice-template').content.firstElementChild.cloneNode(true);
      item.dataset.roleChoice = role.id;
      item.setAttribute('aria-pressed', String(role.id === selected));
      item.querySelector('[data-initial]').textContent = (role.name || '?')[0].toUpperCase();
      item.querySelector('[data-name]').textContent = role.name || 'Unnamed role';
      item.querySelector('[data-count]').textContent = `${role.models.filter(model => model.weight > 0).length} active models`;
      item.querySelector('[data-dirty]').hidden = !dirty(role);
      list.append(item);
    });
    $('#role-total').textContent = drafts.length;
  }

  function renderCandidates() {
    const candidates = $('#candidates');
    candidates.replaceChildren();
    current().models.forEach(model => {
      const item = $('#candidate-template').content.firstElementChild.cloneNode(true);
      const meta = byId(model.id), prefix = `candidate-${meta.id}`;
      item.dataset.candidate = model.id;
      item.querySelector('[data-initial]').textContent = meta.id === 'deepseek-coder-v2' ? 'DS' : 'Q';
      item.querySelector('[data-model-name]').textContent = meta.name;
      item.querySelector('[data-model-detail]').textContent = meta.detail;
      const input = item.querySelector('[data-weight]'), slider = item.querySelector('[data-weight-slider]');
      input.id = `${prefix}-weight`;
      input.value = model.weight;
      item.querySelector('[data-weight-label]').htmlFor = input.id;
      input.setAttribute('aria-label', `Preference weight for ${meta.name}`);
      slider.setAttribute('aria-label', `Adjust preference for ${meta.name}`);
      slider.value = Math.min(10, model.weight);
      item.querySelector('[data-remove]').setAttribute('aria-label', `Remove ${meta.name} from role`);
      candidates.append(item);
    });
    $('#models-empty').hidden = current().models.length > 0;
    const select = $('#add-model');
    select.replaceChildren();
    catalogue.filter(model => !current().models.some(one => one.id === model.id)).forEach(model => {
      select.add(new Option(`${model.name}${model.pictures ? ' · Images' : ''}`, model.id));
    });
    if (!select.options.length) select.add(new Option('All sample models added', ''));
    select.disabled = !select.value;
    $('#add-model-button').disabled = !select.value;
  }

  function renderEditor() {
    const role = current();
    $('#role-name').value = role.name;
    $('#minimum-context').value = role.requires.tokens;
    all('[data-requirement]').forEach(field => { field.checked = role.requires[field.dataset.requirement]; });
    all('[data-benchmark]').forEach(field => { field.value = role.benchmarks[field.dataset.benchmark]; });
    $('#loaded-bonus').value = role.loaded_bonus;
    $('#fast-bonus').value = role.fast_loaded_bonus;
    renderCandidates();
    renderRoleList();
    update();
    updateLayout();
  }

  // Mirror loadcoach.roles: base ordering, ordinary-role resident bonus, and per-request strengths.
  // Scores use compatible sample evidence only; this is not a production routing endpoint.
  function calculate() {
    const role = current(), sampleState = $('#sample-state').value;
    const selectedBenchmarks = Object.entries(role.benchmarks).filter(([, weight]) => weight > 0);
    const scale = Math.max(0, ...selectedBenchmarks.map(([, weight]) => weight));
    const total = scale ? selectedBenchmarks.reduce((sum, [, weight]) => sum + weight / scale, 0) : 0;
    const pool = role.models.map((model, index) => {
      const meta = byId(model.id), excluded = [];
      if (model.weight === 0) excluded.push('Paused · weight is 0');
      if (role.requires.tokens > meta.context) excluded.push(`Needs ${role.requires.tokens.toLocaleString()} context tokens; model supports ${meta.context.toLocaleString()}`);
      if (role.requires.pictures && !meta.pictures) excluded.push('Does not accept images');
      if (role.requires.tools && !meta.tools) excluded.push('Does not support tool calls');
      if (role.requires.stays && !meta.local) excluded.push('Requests must stay local');
      return {...model, index, meta, excluded};
    });
    const active = pool.filter(model => !model.excluded.length).map(model => model.meta);
    const scores = pool.map(model => {
      const {meta, excluded} = model;
      let quality = 0;
      if (!excluded.length) selectedBenchmarks.forEach(([key, weight]) => {
        const measurements = active.map(one => one[key]);
        const low = Math.min(...measurements), high = Math.max(...measurements);
        const normalized = high === low ? .5 : (meta[key] - low) / (high - low);
        quality += normalized * (weight / scale) / total;
      });
      const warm = model.id === 'qwen3-32b' && sampleState === 'normal';
      const resident = model.id === 'qwen3-32b' && sampleState !== 'cold';
      const loadedLocal = resident && meta.local && !excluded.length;
      const base = model.weight * (1 + quality) * meta.reliability;
      const loadedFactor = loadedLocal ? 1 + role.loaded_bonus / 100 : 1;
      const fastFactor = loadedLocal ? 1 + role.fast_loaded_bonus / 100 : 1;
      const seconds = (resident ? 0 : meta.load) + 400 / meta.speed;
      return {...model, quality, warm, resident, base, loadedFactor, fastFactor, seconds,
        score: base * loadedFactor, fastSeconds: seconds / fastFactor};
    });
    const eligible = scores.filter(model => !model.excluded.length).sort((a, b) => b.base - a.base || a.index - b.index);
    const mode = $('#request-mode').value;
    let picked = eligible[0];
    if (mode === 'default') picked = [...eligible].sort((a, b) => b.score - a.score || Number(b.loadedFactor > 1) - Number(a.loadedFactor > 1) || a.index - b.index)[0];
    if (mode === 'available') picked = eligible.find(model => model.warm) || eligible[0];
    if (mode === 'fast') picked = [...eligible].sort((a, b) => a.fastSeconds - b.fastSeconds || b.fastFactor - a.fastFactor || a.index - b.index)[0];
    return {scores, eligible, picked, mode, scale, total};
  }

  function update() {
    const role = current();
    const error = nameError(role.name, role.id);
    $('#role-name').setCustomValidity(error);
    const invalid = !$('#role-editor').checkValidity() || role.models.some(model => !validWeight(model.weight));
    all('#role-editor input:not([type=checkbox]):not([type=range])').forEach(field => {
      field.setAttribute('aria-invalid', String(!field.validity.valid));
    });
    const {scores, eligible, picked, mode, scale, total} = calculate();
    const changed = dirty(role);
    $('#save').disabled = invalid || !eligible.length || !changed;
    $('#discard').disabled = !changed;
    $('#save-state').textContent = invalid ? 'Check the highlighted fields before saving.' :
      !eligible.length ? 'Add an eligible model with a weight above 0 to save.' :
      changed ? 'Unsaved changes' : 'Saved sample · no unsaved changes';
    all('[data-request-name]').forEach(node => { node.textContent = role.name || 'your-role'; });
    $('#request-code').textContent = requestName();
    const explanations = {
      default: 'Your weights and reliability decide preference. Loaded local models get the bonus you set.',
      best: 'Choose the highest base score, even if it needs loading. May wait and unload idle models to make room.',
      available: 'Choose the highest base score among ready models. If none are ready, load the highest-ranked candidate.',
      fast: 'Prefer the lowest time estimate after the loaded-model reuse preference. This is not a promise of response time.',
    };
    $('#mode-explanation').textContent = explanations[mode];
    $('#pick-label').textContent = invalid ? 'FIX THE DRAFT TO PREVIEW' : 'WOULD PICK NOW';
    $('#picked-model').textContent = invalid ? 'Check your settings' : picked ? picked.meta.name : 'No eligible model';
    let reason = 'Add a model, increase a paused weight, or relax a requirement to let this role answer.';
    if (picked) {
      if (mode === 'default') reason = picked.loadedFactor > 1 ?
        `Score ${displayNumber(picked.score)} after a ${displayNumber(role.loaded_bonus)}% loaded local model bonus. ${picked.warm ? 'Ready to answer.' : 'Already loaded, but busy; may wait for capacity.'}` :
        `Highest score: ${displayNumber(picked.score)} after loaded-model bonuses.${picked.warm ? ' Ready to answer.' : picked.resident ? ' Already loaded; may wait for capacity.' : ' Will need to load.'}`;
      if (mode === 'best') reason = `Highest base score: ${displayNumber(picked.base)}.${picked.warm ? ' Already ready.' : picked.resident ? ' Loaded, but may wait for capacity.' : ' Will load, even if another model is ready.'}`;
      if (mode === 'available') reason = picked.warm ? `Highest base score among ready models: ${displayNumber(picked.base)}. No loading needed.` : 'No eligible model is ready. Falls back to the highest base score and may load or wait.';
      if (mode === 'fast') reason = `About ${displayNumber(picked.seconds)} s for loading + 400 output tokens, before queue and prompt time. Reuse preference ×${displayNumber(picked.fastFactor)}; comparison value ${displayNumber(picked.fastSeconds)}.`;
    }
    $('#pick-reason').textContent = invalid ? 'Use a unique valid name and finite values within each field’s limits.' : reason;
    const maximum = Math.max(0, ...eligible.map(model => model.score));
    scores.forEach(model => {
      const item = all('[data-candidate]').find(node => node.dataset.candidate === model.id);
      item.classList.toggle('is-picked', !invalid && model.id === picked?.id);
      item.classList.toggle('is-excluded', model.excluded.length > 0);
      item.querySelector('[data-state]').textContent = model.warm ? 'Ready now' : model.resident ? 'Loaded · busy' : 'Needs loading';
      item.querySelector('[data-state-dot]').classList.toggle('is-ready', model.warm);
      item.querySelector('[data-score]').textContent = invalid ? '—' : model.excluded.length ? 'Excluded' : `Score ${displayNumber(model.score)}`;
      item.querySelector('[data-score-bar]').style.width = !invalid && !model.excluded.length && maximum ? `${model.score / maximum * 100}%` : '0%';
      const exclusion = item.querySelector('[data-exclusion]');
      exclusion.hidden = !model.excluded.length;
      exclusion.textContent = model.excluded.join(' · ');
      const input = item.querySelector('[data-weight]');
      input.setAttribute('aria-invalid', String(!input.validity.valid));
    });
    all('[data-share]').forEach(output => {
      const weight = role.benchmarks[output.dataset.share];
      output.textContent = !validWeight(weight) ? 'Enter a valid weight' : weight > 0 && total ? `${displayNumber((weight / scale) / total * 100)}% of benchmark quality` : 'Off · no influence';
    });
    const rows = $('#evidence-rows');
    rows.replaceChildren();
    scores.forEach(model => {
      const row = $('#evidence-template').content.firstElementChild.cloneNode(true);
      const values = {model: model.meta.name, weight: displayNumber(model.weight), quality: total ? `${displayNumber(model.quality * 100)}%` : 'Off', reliability: `×${displayNumber(model.meta.reliability)}`, bonus: `×${displayNumber(model.loadedFactor)}`, score: invalid ? '—' : model.excluded.length ? model.excluded.join(' · ') : displayNumber(model.score)};
      Object.entries(values).forEach(([key, value]) => { row.querySelector(`[data-evidence-${key}]`).textContent = value; });
      rows.append(row);
    });
    $('#review-text').textContent = `${role.name || 'Your role'} has ${eligible.length} eligible model${eligible.length === 1 ? '' : 's'}. Your app sends this name in its model field; the system picks again for each request.`;
    renderRoleList();
  }

  const descriptions = {
    studio: 'Recommended · a focused workspace for everyday use, with the current pick beside your model weights.',
    guided: 'A guided flow · name the role, choose its models, then review what your app will use.',
    canvas: 'A visual decision canvas · compare candidates side by side and follow the path from app to model.',
  };

  function updateLayout() {
    $('#workspace').dataset.layout = layout;
    $('#workspace').dataset.step = step;
    $('#design-description').textContent = descriptions[layout];
    $('#design-note').textContent = {studio: '01 / Role Studio', guided: '02 / Guided Setup', canvas: '03 / Decision Canvas'}[layout];
    all('[data-design]').forEach(button => {
      button.classList.toggle('active', button.dataset.design === layout);
      button.setAttribute('aria-pressed', String(button.dataset.design === layout));
    });
    $('.wizard-steps').hidden = layout !== 'guided';
    all('[data-step-to]').forEach(button => {
      if (Number(button.dataset.stepTo) === step) button.setAttribute('aria-current', 'step');
      else button.removeAttribute('aria-current');
    });
    all('.setup-panel').forEach(panel => {
      panel.hidden = layout === 'guided' ? Number(panel.dataset.setupStep) !== step : panel.id === 'review-summary';
    });
    $('#previous-step').hidden = layout !== 'guided' || step === 1;
    $('#next-step').hidden = layout !== 'guided' || step === 3;
    $('#next-step').textContent = step === 1 ? 'Choose models →' : 'Review role →';
    $('#save').hidden = layout === 'guided' && step !== 3;
  }

  function goToStep(target) {
    if (target > step) {
      if (nameError(current().name, current().id)) { step = 1; updateLayout(); $('#role-name').reportValidity(); return; }
      if (target === 3 && (!calculate().eligible.length || all('#candidates input').some(input => !input.validity.valid))) {
        announce('Choose at least one eligible model with a valid weight above 0 before reviewing.');
        step = 2; updateLayout(); return;
      }
    }
    step = target;
    updateLayout();
  }

  $('#role-editor').addEventListener('input', event => {
    const field = event.target, role = current();
    if (field.id === 'role-name') role.name = field.value;
    else if (field.matches('[data-weight], [data-weight-slider]')) {
      const candidate = field.closest('[data-candidate]');
      const model = role.models.find(one => one.id === candidate.dataset.candidate);
      model.weight = field.value === '' ? NaN : Number(field.value);
      if (field.matches('[data-weight-slider]')) candidate.querySelector('[data-weight]').value = model.weight;
      else candidate.querySelector('[data-weight-slider]').value = Math.min(10, model.weight || 0);
    } else if (field.id === 'minimum-context') role.requires.tokens = field.value === '' ? NaN : Number(field.value);
    else if (field.matches('[data-requirement]')) role.requires[field.dataset.requirement] = field.checked;
    else if (field.matches('[data-benchmark]')) role.benchmarks[field.dataset.benchmark] = field.value === '' ? NaN : Number(field.value);
    else if (field.id === 'loaded-bonus') role.loaded_bonus = field.value === '' ? NaN : Number(field.value);
    else if (field.id === 'fast-bonus') role.fast_loaded_bonus = field.value === '' ? NaN : Number(field.value);
    update();
  });

  document.addEventListener('click', async event => {
    const design = event.target.closest('[data-design]');
    if (design) { layout = design.dataset.design; history.replaceState(null, '', `#${layout}`); updateLayout(); }
    const choice = event.target.closest('[data-role-choice]');
    if (choice) { selected = choice.dataset.roleChoice; step = 1; renderEditor(); $('#said').textContent = ''; }
    const remove = event.target.closest('[data-remove]');
    if (remove) {
      const id = remove.closest('[data-candidate]').dataset.candidate;
      current().models = current().models.filter(model => model.id !== id);
      renderCandidates(); update(); announce(`Removed ${byId(id).name} from this draft. Discard changes to undo.`);
      $('#add-model').focus();
    }
    const stepButton = event.target.closest('[data-step-to]');
    if (stepButton) goToStep(Number(stepButton.dataset.stepTo));
    const copy = event.target.closest('[data-copy]');
    if (copy) {
      const value = copy.closest('#identity') ? current().name : requestName();
      try {
        if (!navigator.clipboard) throw new Error('Clipboard unavailable');
        await navigator.clipboard.writeText(value);
        announce(`Copied “${value}”. Paste it into your app’s model field.`);
      } catch { announce(`Model name: ${value}. Select the name and copy it to your app’s model field.`); }
    }
  });
  $('#add-model-button').addEventListener('click', () => {
    const id = $('#add-model').value;
    if (!id || current().models.some(model => model.id === id)) return;
    current().models.push({id, weight: 1});
    renderCandidates(); update();
    all('[data-candidate]').at(-1).querySelector('[data-weight]').focus();
  });
  $('#sample-state').addEventListener('change', update);
  $('#request-mode').addEventListener('change', update);
  $('#previous-step').addEventListener('click', () => goToStep(step - 1));
  $('#next-step').addEventListener('click', () => goToStep(step + 1));
  $('#role-editor').addEventListener('submit', event => {
    event.preventDefault();
    if (layout === 'guided' && step < 3) { goToStep(step + 1); return; }
    if (!$('#role-editor').reportValidity() || !calculate().eligible.length) { announce('Check the role name, weights, and requirements before saving.'); return; }
    const next = clone(saved), index = next.findIndex(role => role.id === selected);
    if (index === -1) next.push(clone(current()));
    else next[index] = clone(current());
    try {
      localStorage.setItem(storageKey, JSON.stringify(next));
      saved = next;
      update();
      announce(`Saved ${current().name} in this browser. The live Roles configuration is unchanged.`);
    } catch { announce('This browser could not save the preview. Your draft is still here; keep this tab open.'); }
  });
  $('#discard').addEventListener('click', () => {
    const original = saved.find(role => role.id === selected);
    if (original) drafts[drafts.findIndex(role => role.id === selected)] = clone(original);
    else { drafts = drafts.filter(role => role.id !== selected); selected = drafts[0].id; }
    renderEditor(); announce('Discarded changes for this role.');
  });
  $('#new-role').addEventListener('click', () => {
    $('#new-name').value = '';
    $('#new-name').setCustomValidity('');
    $('#create-error').textContent = '';
    $('#create-dialog').showModal();
    $('#new-name').focus();
  });
  $('#cancel-create').addEventListener('click', () => $('#create-dialog').close());
  $('#new-name').addEventListener('input', () => {
    $('#new-name').setCustomValidity('');
    $('#create-error').textContent = '';
  });
  $('#create-form').addEventListener('submit', event => {
    event.preventDefault();
    const name = $('#new-name').value, error = nameError(name, '');
    $('#new-name').setCustomValidity(error);
    if (error) { $('#create-error').textContent = error; $('#new-name').reportValidity(); return; }
    selected = `role-${crypto.randomUUID()}`;
    drafts.push(makeRole(selected, name, []));
    step = layout === 'guided' ? 2 : 1;
    $('#create-dialog').close();
    renderEditor();
    announce(`Created a draft for ${name}. Add a model to make it usable.`);
    $('#add-model').focus();
  });
  $('#reset').addEventListener('click', () => {
    try { localStorage.removeItem(storageKey); } catch { /* Reset in memory even without storage. */ }
    saved = clone(initial); drafts = clone(initial); selected = drafts[0].id; step = 1;
    $('#sample-state').value = 'normal'; $('#request-mode').value = 'default';
    renderEditor(); announce('Demo reset to the original sample roles.');
  });
  $('#theme').addEventListener('click', () => {
    const select = $('[data-theme-form] [name=mode]');
    select.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    select.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = select.value === 'dark' ? 'Light mode' : 'Dark mode';
  });
  window.addEventListener('beforeunload', event => {
    if (drafts.some(dirty)) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('hashchange', () => {
    if (['studio', 'guided', 'canvas'].includes(location.hash.slice(1))) {
      layout = location.hash.slice(1); updateLayout();
    }
  });
  renderEditor();
  $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
})();
