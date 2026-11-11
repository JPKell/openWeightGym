// Node-local planning only. Saved-set forms are the sole mutation path; loading needs server review.
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const data = JSON.parse($('#fit-data').textContent), models = data.models, resources = data.resources;
  const clone = id => document.getElementById(id).content.firstElementChild.cloneNode(true);
  const size = value => `${(value / 1024).toFixed(1)} GiB`;
  const number = value => value.toLocaleString('en-US');
  const color = model => `hsl(${models.indexOf(model) * 137.5 % 360} 55% 43%)`;
  const invalid = new Map();
  let kind = 'all';
  const selected = () => models.filter(model => model.selected);
  const placement = model => model.placements[model.placement];
  const needed = resource => resource.id === '__cpu__' || selected().some(model => placement(model)?.cards.includes(resource.id));
  const account = document.documentElement.dataset.themeUser || 'anonymous';
  const storageKey = `weightroom-fit-v1:${account}:${data.machine}`;
  const message = value => {$('#fit-message').textContent = value;};

  function interpolate(points, at, index = null) {
    if (!points.length) return 0;
    const value = point => index === null ? point[1] : point[1][index];
    if (points.length === 1) return value(points[0]) || 0;
    let end = points.findIndex(point => point[0] >= at);
    end = end < 0 ? points.length - 1 : Math.max(1, end);
    const from = points[end - 1], to = points[end];
    return Math.max(0, value(from) + (value(to) - value(from)) * (at - from[0]) / (to[0] - from[0]));
  }

  function allocation(model) {
    const on = {};
    (placement(model)?.cards || []).forEach((card, index) => {on[card] = interpolate(model.points, model.context || 0, index);});
    if (model.ram_points.length) on.__cpu__ = interpolate(model.ram_points, model.context || 0);
    return on;
  }
  const memoryText = model => model.unknown ? 'Unknown / unqualified' : Object.entries(allocation(model))
    .map(([id, amount]) => `${size(amount)} ${id === '__cpu__' ? 'RAM' : id}`).join(' + ');
  const definition = () => selected().map(model => ({id: model.id, placement: placement(model)?.cards || [], context: model.context, locked: model.locked}));
  function keepDraft() {
    if (!data.available) return;
    try {sessionStorage.setItem(storageKey, JSON.stringify({models: definition(), invalid: [...invalid]}));} catch { /* Planning remains available without storage. */ }
  }
  function restoreDraft() {
    try {
      const kept = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      if (!kept || !Array.isArray(kept.models)) return;
      const warnings = [];
      for (const model of models) {
        const one = kept.models.find(entry => entry?.id === model.id);
        model.selected = Boolean(one && model.enabled);
        if (!one) continue;
        const index = model.placements.findIndex(place => JSON.stringify(place.cards) === JSON.stringify(one.placement));
        if (index < 0 || (!model.fixed && (!Number.isInteger(one.context) || one.context < model.min || one.context > model.max))) {
          model.selected = false; warnings.push(model.name); continue;
        }
        model.placement = index; model.locked = Boolean(one.locked);
        if (!model.fixed) model.context = one.context;
      }
      for (const entry of Array.isArray(kept.invalid) ? kept.invalid : []) {
        if (Array.isArray(entry) && models.some(model => model.id === entry[0] && model.selected) && typeof entry[1] === 'string') invalid.set(entry[0], entry[1]);
      }
      if (warnings.length) message(`Review changed placement or context limits for: ${warnings.join(', ')}.`);
    } catch { /* Ignore malformed browser-local drafts. */ }
  }

  function judge() {
    const used = Object.fromEntries(resources.map(resource => [resource.id, 0]));
    for (const model of selected()) for (const [id, amount] of Object.entries(allocation(model))) used[id] += amount;
    let state = 'fits', title = 'Known reservations fit the estimate';
    let detail = 'No model is loaded. A fresh server review checks actual resources, holds and protected work.';
    const over = resources.filter(resource => resource.budget !== null && used[resource.id] > resource.budget + .001);
    const exclusive = resources.filter(resource => resource.id !== '__cpu__' && selected().filter(model => placement(model)?.cards.includes(resource.id)).length > 1 &&
      selected().some(model => model.exclusive && placement(model)?.cards.includes(resource.id)));
    if (!data.available) {state = 'unknown'; title = 'Node unavailable'; detail = 'Capacity is unknown. Your browser draft is retained until this node reconnects.';}
    else if (!selected().length) {state = 'empty'; title = 'Choose a combination'; detail = 'Select models from the catalogue to plan them together.';}
    else if (invalid.size) {state = 'blocked'; title = 'Review context values'; detail = 'Use whole-number contexts within each model’s configured bounds.';}
    else if (exclusive.length) {state = 'blocked'; title = 'Exclusive placement conflict'; detail = `A selected model requires ${exclusive.map(resource => resource.name).join(', ')} to itself.`;}
    else if (selected().some(model => model.unknown) || resources.some(resource => needed(resource) && resource.budget === null)) {
      state = 'unknown'; title = 'Combined fit is unknown'; detail = 'A selected profile or memory pool is unmeasured. Known reservations are shown without assuming missing values are zero.';
    } else if (over.length) {state = 'over'; title = 'Over the memory budget'; detail = over.map(resource => `${resource.name}: ${size(used[resource.id] - resource.budget)} over`).join(' · ');}
    return {state, title, detail, used};
  }

  function renderRows() {
    const query = $('#search').value.toLowerCase();
    $('#fit-picks').replaceChildren(...selected().map(model => {
      const row = clone('fit-model-template'), prefix = `fit-${models.indexOf(model)}`;
      row.dataset.model = model.name; row.dataset.modelId = model.id;
      row.classList.add('is-selected'); row.style.setProperty('--fit-model-color', color(model));
      row.hidden = (kind !== 'all' && model.kind !== kind) || !model.name.toLowerCase().includes(query);
      const tick = row.querySelector('[data-select]');
      tick.id = prefix + '-on'; tick.name = 'on'; tick.value = model.name; tick.checked = true;
      row.querySelector('[data-name]').htmlFor = tick.id; row.querySelector('[data-name]').textContent = model.name;
      row.querySelector('[data-detail]').textContent = `${model.engine}${model.fixed ? '' : ` · ${model.shared_context ? 'shared' : 'fixed'} slots × ${Number.isInteger(model.parallel) && model.parallel > 0 ? model.parallel : 'auto'}`}`;
      const badge = row.querySelector('[data-kind]'); badge.textContent = {llm: 'LLM', audio: 'Audio', image: 'Image'}[model.kind]; badge.classList.add(`bg-${{llm: 'blue', audio: 'teal', image: 'purple'}[model.kind]}-lt`);
      const place = row.querySelector('[data-placement]'); place.id = prefix + '-placement';
      row.querySelector('[data-placement-label]').htmlFor = place.id;
      row.querySelector('[data-placement-label]').textContent = `Placement for ${model.name}`;
      model.placements.forEach((one, index) => place.add(new Option(one.label, String(index))));
      if (!model.placements.length) place.add(new Option('No eligible devices', '0'));
      place.value = String(model.placement);
      row.querySelector('[data-fixed]').hidden = !model.fixed;
      row.querySelector('[data-profile]').textContent = `${model.profile_label || model.kind} · fixed envelope`;
      if (model.fixed) row.querySelector('[data-context-controls]').remove();
      else {
        const context = row.querySelector('[data-context]'), slider = row.querySelector('[data-slider]');
        context.id = prefix + '-context'; context.classList.add(placement(model)?.cards.length ? 'fit-context-number' : 'fit-cpu-context');
        row.querySelector('[data-context-label]').htmlFor = context.id;
        row.querySelector('[data-context-label]').textContent = `Context tokens for ${model.name}`;
        for (const input of [context, slider]) {input.min = model.min; input.max = model.max; input.value = model.context || ''; input.title = `${number(model.min)}–${number(model.max)} total tokens`;}
        context.value = invalid.has(model.id) ? invalid.get(model.id) : model.context || '';
        if (invalid.has(model.id)) context.setCustomValidity('Enter a whole-number context within the model bounds.');
        slider.classList.add('fit-context'); slider.setAttribute('aria-label', `Context slider for ${model.name}`);
        const lock = row.querySelector('[data-lock]'); lock.checked = model.locked; lock.setAttribute('aria-label', `Lock context for ${model.name}`);
        row.querySelector('[data-bounds]').textContent = context.title;
      }
      row.querySelector('[data-memory]').textContent = memoryText(model);
      row.querySelector('[data-memory]').title = model.ram_note + (model.guessed ? '; interpolated/estimated VRAM curve' : '');
      row.querySelector('[data-evidence]').textContent = model.ram_note;
      return row;
    }));
    const shown = $$('#fit-picks tr').filter(row => !row.hidden).length;
    $('#fit-none').hidden = Boolean(shown);
    $('#fit-none').textContent = selected().length ? 'No selected rows match this filter. The combined budget still includes every selected model.' : 'Choose models from this node’s catalogue to start tuning.';
    $('#visible-count').textContent = `${shown} rows shown · ${selected().length} selected across all types`;
  }

  function renderCatalogue() {
    const query = $('#catalogue-search').value.toLowerCase(), filter = $('#catalogue-kind').value, order = $('#catalogue-sort').value;
    const visible = models.filter(model => (filter === 'all' || model.kind === filter) && `${model.name} ${model.engine}`.toLowerCase().includes(query));
    const total = model => model.unknown ? Infinity : Object.values(allocation(model)).reduce((a, b) => a + b, 0);
    visible.sort((a, b) => (order === 'memory' ? total(a) - total(b) : order === 'selected' ? Number(b.selected) - Number(a.selected) : 0) || a.name.localeCompare(b.name));
    const scroll = $('#catalogue-list').scrollTop;
    $('#catalogue-list').replaceChildren(...visible.map(model => {
      const item = clone('fit-catalogue-template'), tick = item.querySelector('[data-catalogue-select]');
      tick.id = `fit-choice-${models.indexOf(model)}`; tick.dataset.modelId = model.id; tick.checked = model.selected;
      tick.disabled = !model.enabled || !model.placements.length;
      tick.setAttribute('aria-label', `Select ${model.name}`);
      item.querySelector('[data-name]').textContent = model.name;
      item.querySelector('[data-detail]').textContent = model.engine;
      item.title = model.enabled ? `${model.engine} · ${memoryText(model)}` : 'This model location is disabled.';
      item.querySelector('[data-catalogue-kind]').textContent = {llm: 'LLM', audio: 'Audio', image: 'Image'}[model.kind];
      item.querySelector('[data-catalogue-memory]').textContent = model.unknown ? 'Unknown' : size(total(model));
      return item;
    }));
    if (!visible.length) $('#catalogue-list').textContent = models.length ? 'No matches. Clear the search or change type.' : 'No local models. Add a model on this node first.';
    $('#catalogue-list').scrollTop = scroll;
    $('#catalogue-count').textContent = `${visible.length} of ${models.length} models · ${selected().length} selected`;
  }

  function renderBudget() {
    const result = judge();
    $('#verdict').dataset.state = result.state; $('#verdict-title').textContent = result.title; $('#verdict-detail').textContent = result.detail;
    $('#selection-count').textContent = `${selected().length} selected · ${selected().filter(m => m.kind === 'llm').length} LLM · ${selected().filter(m => m.kind === 'audio').length} audio · ${selected().filter(m => m.kind === 'image').length} image`;
    $('#resources').replaceChildren(...resources.map(resource => {
      const row = clone('fit-resource-template'), used = result.used[resource.id];
      row.dataset.card = resource.id;
      row.querySelector('[data-resource-name]').textContent = resource.name;
      row.querySelector('[data-resource-total]').textContent = resource.total ? `${size(resource.total)} physical` : 'Capacity unknown';
      row.querySelector('[data-resource-used]').textContent = size(used);
      const free = resource.budget === null ? null : resource.budget - used;
      row.querySelector('[data-resource-free]').textContent = result.state === 'unknown' ? 'known allocations only' : free === null ? 'budget unknown' : `${size(Math.abs(free))} ${free < 0 ? 'over budget' : 'left for models'}`;
      row.querySelector('[data-resource-note]').textContent = resource.budget === null ? 'Memory telemetry is unavailable; capacity is not assumed.' :
        `${size(resource.other || 0)} other use · ${size(resource.reserve)} kept free${resource.configured_limit ? ` · ${size(resource.configured_limit)} model RAM limit` : ''}${resource.policy_reserved ? ` · ${size(resource.policy_reserved)} outside current model budget` : ''}`;
      const bar = row.querySelector('[data-resource-bar]');
      const scale = Math.max(resource.total || 0, used + (resource.other || 0) + resource.reserve + (resource.policy_reserved || 0), 1);
      const segments = [];
      const add = (amount, label, background, extra) => {
        const segment = clone('fit-segment-template'); segment.style.width = `${amount / scale * 100}%`;
        if (background) segment.style.backgroundColor = background;
        if (extra) segment.classList.add(extra);
        segment.title = `${label}: ${size(amount)}`; segments.push(segment);
      };
      if (resource.budget !== null) add(resource.other || 0, 'Other use', '', 'bg-secondary');
      selected().forEach(model => {const amount = allocation(model)[resource.id]; if (amount) add(amount, model.name, color(model));});
      if (resource.total) {add(Math.max(0, resource.total - (resource.other || 0) - resource.reserve - (resource.policy_reserved || 0) - used), 'Unallocated model budget', 'transparent'); add(resource.policy_reserved || 0, 'Outside current model budget', '', 'bg-secondary'); add(resource.reserve, 'Kept free', '', 'fit-headroom');}
      bar.replaceChildren(...segments); bar.hidden = resource.budget === null;
      bar.setAttribute('aria-label', `${resource.name}: ${size(used)} known selected allocations; ${free === null ? 'budget unknown' : `${size(Math.abs(free))} ${free < 0 ? 'over budget' : 'left'}`}`);
      return row;
    }));
    $('#memory-notes').textContent = resources.filter(r => r.service_limit).map(r => `Service memory ceiling: ${size(r.service_limit)}; current cgroup room: ${r.service_available === null ? 'unknown' : size(r.service_available)}. RSS planning does not replace cgroup admission.`).join(' ');
    $('#fit-auto').disabled = !data.available || invalid.size > 0 || !selected().some(model => !model.fixed && !model.locked) || selected().some(model => model.unknown) || resources.some(resource => needed(resource) && resource.budget === null);
    const submit = $('#fit-save button[value="save"]');
    if (submit) submit.disabled = !data.available || !selected().length || invalid.size > 0 || selected().length > data.max_members || selected().some(model => model.fixed && !model.profile);
    if ($('#fit-selection-message')) $('#fit-selection-message').textContent = selected().length > data.max_members ? `A named set supports up to ${data.max_members} models; reduce the selection before saving.` :
      invalid.size ? 'Correct context values before saving.' : 'Saving keeps exact contexts, placements and fixed profiles. It loads nothing.';
  }

  function render() {
    const focused = document.activeElement?.id;
    const scroll = $('.fit-row-scroll').scrollTop;
    renderRows(); renderCatalogue(); renderBudget();
    $('.fit-row-scroll').scrollTop = scroll;
    if (focused) document.getElementById(focused)?.focus({preventScroll: true});
    keepDraft();
  }
  const roles = () => {if ($('#fit-roles')) htmx.trigger('#fit-roles', 'ticked');};

  $('#catalogue-list').addEventListener('change', event => {
    if (!event.target.matches('[data-catalogue-select]')) return;
    const model = models.find(one => one.id === event.target.dataset.modelId);
    model.selected = event.target.checked;
    if (!model.selected) invalid.delete(model.id);
    render(); roles();
  });
  $('#fit-picks').addEventListener('change', event => {
    const row = event.target.closest('[data-model-id]'); if (!row) return;
    const model = models.find(one => one.id === row.dataset.modelId);
    if (event.target.matches('[data-select]')) {model.selected = false; invalid.delete(model.id);}
    else if (event.target.matches('[data-placement]')) model.placement = Number(event.target.value);
    else if (event.target.matches('[data-lock]')) model.locked = event.target.checked;
    else return;
    render(); roles();
  });
  $('#fit-picks').addEventListener('input', event => {
    if (!event.target.matches('[data-context], [data-slider]')) return;
    const row = event.target.closest('[data-model-id]'), model = models.find(one => one.id === row.dataset.modelId);
    const value = Number(event.target.value);
    if (!event.target.value || !Number.isInteger(value) || value < model.min || value > model.max) {
      invalid.set(model.id, event.target.value); event.target.setCustomValidity(`Enter ${model.min}–${model.max} whole tokens.`);
    } else {
      invalid.delete(model.id); model.context = value;
      for (const input of row.querySelectorAll('[data-context], [data-slider]')) {input.setCustomValidity(''); input.value = value;}
      row.querySelector('[data-memory]').textContent = memoryText(model);
    }
    renderBudget(); keepDraft();
  });
  $('#fit-editor').addEventListener('submit', event => event.preventDefault());
  for (const id of ['catalogue-search', 'catalogue-kind', 'catalogue-sort']) $(`#${id}`).addEventListener(id === 'catalogue-search' ? 'input' : 'change', () => {$('#catalogue-list').scrollTop = 0; renderCatalogue();});
  $('#search').addEventListener('input', renderRows);
  $$('[data-fit-kind]').forEach(button => button.addEventListener('click', () => {
    kind = button.dataset.fitKind;
    $$('[data-fit-kind]').forEach(other => {other.classList.toggle('active', other === button); other.setAttribute('aria-pressed', String(other === button));});
    renderRows();
  }));
  $('#clear').addEventListener('click', () => {models.forEach(model => {model.selected = false;}); invalid.clear(); render(); roles();});
  $('#fit-auto').addEventListener('click', () => {
    const moving = selected().filter(model => !model.fixed && !model.locked), before = moving.map(model => model.context);
    moving.forEach(model => {model.context = model.min;});
    if (judge().state !== 'fits') {
      moving.forEach((model, index) => {model.context = before[index];});
      message('Minimum contexts, locked models or fixed reservations cannot fit. Change placement or remove a model; contexts were not lowered below their minimum.'); render(); return;
    }
    let low = 0, high = 1;
    for (let i = 0; i < 24; i++) {
      const share = (low + high) / 2;
      moving.forEach(model => {model.context = Math.floor(model.min + share * (model.max - model.min));});
      if (judge().state === 'fits') low = share; else high = share;
    }
    moving.forEach(model => {model.context = Math.floor(model.min + low * (model.max - model.min));});
    for (const model of moving) {
      let from = model.context, to = model.max;
      while (from < to) {model.context = Math.ceil((from + to) / 2); if (judge().state === 'fits') from = model.context; else to = model.context - 1;}
      model.context = from;
    }
    message(`Adjusted ${moving.length} unlocked LLM contexts. Locked values and fixed audio/image profiles stayed unchanged.`); render();
  });
  $('#fit-save')?.addEventListener('submit', event => {
    if (invalid.size || !selected().length || selected().length > data.max_members) {event.preventDefault(); message('Choose 1–16 models and correct all context values.'); return;}
    $('#fit-selection').value = JSON.stringify(selected().map(model => ({model: model.local_name, cards: placement(model).cards,
      ...(model.fixed ? {profile: model.profile} : {context: model.context})})));
    const prior = $$('[data-loadout-name]').find(row => row.dataset.loadoutName === $('#fit-name').value.trim());
    $('#fit-version').value = $('#fit-replace').checked && prior ? prior.dataset.version : '';
    keepDraft();
  });
  $$('[data-loadout-edit]').forEach(button => button.addEventListener('click', () => {
    const saved = JSON.parse(button.dataset.loadoutEdit), warnings = [];
    invalid.clear();
    models.forEach(model => {
      const entry = saved.models.find(one => one.model_id === model.id); model.selected = Boolean(entry);
      if (!entry) return;
      const index = model.placements.findIndex(place => JSON.stringify(place.cards) === JSON.stringify(entry.cards));
      if (index < 0 || (model.fixed ? entry.profile !== model.profile : !Number.isInteger(entry.context) || entry.context < model.min || entry.context > model.max)) {
        model.selected = false; warnings.push(`${model.name}: placement, context bounds or fixed profile changed`); return;
      }
      model.placement = index; if (!model.fixed) model.context = entry.context;
    });
    saved.models.forEach(entry => {if (!models.some(model => model.id === entry.model_id)) warnings.push(`${entry.label}: unavailable on this node`);});
    $('#fit-name').value = saved.name; $('#fit-replace').checked = true;
    message(warnings.length ? warnings.join('; ') : `Selected ${saved.name} at its exact saved settings. Nothing loaded.`); render(); roles();
  }));
  $$('[data-loadout-api]').forEach(button => button.addEventListener('click', () => {
    const body = action => JSON.stringify({action, name: button.dataset.loadoutApi, ...(action === 'request' ? {token: 'TOKEN_FROM_PREVIEW'} : {})});
    const command = action => `curl --fail-with-body "$OWG_URL${data.api_path}" -H "Authorization: Bearer $OWG_KEY" -H 'Content-Type: application/json' --data-binary @- <<'OWG_JSON'\n${body(action)}\nOWG_JSON`;
    $('#fit-api-code').textContent = `# ${data.machine}: preview first and review all changes/warnings.\n${command('preview')}\n\n# Substitute the returned token after review.\n${command('request')}`;
    $('#fit-api').showModal();
  }));
  $('[data-fit-close]').addEventListener('click', () => $('#fit-api').close());
  window.addEventListener('pagehide', keepDraft);
  restoreDraft(); render(); roles();
})();
