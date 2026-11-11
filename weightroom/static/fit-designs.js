// Illustrative reservations only. No fetch, inference, model changes or live API calls.
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const clone = id => $(`#${id}`).content.firstElementChild.cloneNode(true);
  const copy = value => JSON.parse(JSON.stringify(value));
  const giB = mib => `${(mib / 1024).toFixed(1)} GiB`;
  const integer = value => value.toLocaleString('en-US');
  const palette = ['#3674be', '#9664bf', '#128681', '#c27a24', '#c25468', '#6175a0', '#788c32'];
  const placement = (label, allocations, extra = {}) => ({label, allocations, ...extra});
  const llm = (id, name, detail, min, max, context, placements, selected = false) =>
    ({id, name, detail, kind: 'llm', min, max, context, placements, selected, locked: false, mode: Object.keys(placements)[0]});
  const media = (id, name, kind, detail, profile, placements, selected = false) =>
    ({id, name, kind, detail, profile, placements, selected, mode: Object.keys(placements)[0]});
  const gpu = (id, label, total = 24576) => ({id, label, total, other: 1536, reserve: 2048});
  const ram = (total, other, reserve, limit) => ({id: 'ram', label: 'System RAM', total, other, reserve, limit});
  const nodes = {
    atlas: {
      summary: '1 × RTX 4090 · 24 GiB VRAM · 64 GiB RAM',
      resources: [gpu('gpu0', 'RTX 4090 · VRAM'), ram(65536, 10240, 8192, 40960)],
      models: [
        llm('qwen8', 'Qwen3 · 8B', 'Q4_K_M · llama.cpp', 2048, 65536, 32768, {
          gpu0: placement('RTX 4090', {gpu0: [5632, .12], ram: [768, 0]}),
          cpu: placement('System RAM', {ram: [6656, .14]}),
        }, true),
        llm('gemma4', 'Gemma 3 · 4B', 'Q4_K_M · llama.cpp', 2048, 32768, 16384, {
          gpu0: placement('RTX 4090', {gpu0: [4096, .1], ram: [512, 0]}),
          cpu: placement('System RAM', {ram: [5120, .12]}),
        }, true),
        media('whisper', 'Whisper · large v3', 'audio', 'Speech recognition · audiocpp', 'Measured · 30 s input envelope', {
          gpu0: placement('RTX 4090', {gpu0: [1536, 0], ram: [512, 0]}),
          cpu: placement('System RAM', {ram: [3584, 0]}),
        }, true),
        media('sdxl', 'SDXL · Base 1.0', 'image', 'Diffusion · sdcpp', 'Measured · 1024 × 1024 · batch 1', {
          gpu0: placement('RTX 4090', {gpu0: [7168, 0], ram: [3072, 0]}),
        }),
        llm('oss', 'GPT-OSS · 20B', 'Hybrid experts · Strata', 256, 8192, 8192, {
          hybrid: placement('GPU + system RAM', {gpu0: [4096, .05], ram: [16384, 0]}, {hybrid: true}),
        }),
        media('kokoro', 'Kokoro · 82M', 'audio', 'Speech synthesis · audiocpp', 'Measured · synthesis envelope', {
          cpu: placement('System RAM', {ram: [768, 0]}),
        }),
        media('flux', 'FLUX.1 · Dev', 'image', 'Diffusion · sdcpp', 'Unqualified · no memory profile', {
          gpu0: placement('RTX 4090', {}, {unknown: true, exclusive: true}),
        }),
      ],
    },
    orion: {
      summary: '2 × RTX 3090 · 48 GiB VRAM total · 128 GiB RAM',
      resources: [gpu('gpu0', 'RTX 3090 #1 · VRAM'), gpu('gpu1', 'RTX 3090 #2 · VRAM'), ram(131072, 16384, 12288, 98304)],
      models: [
        llm('qwen32', 'Qwen3 · 32B', 'Q4_K_M · llama.cpp', 2048, 65536, 32768, {
          split: placement('Both RTX 3090s', {gpu0: [10240, .07], gpu1: [10240, .07], ram: [1024, 0]}),
          cpu: placement('System RAM', {ram: [22528, .16]}),
        }, true),
        llm('qwen8', 'Qwen3 · 8B', 'Q4_K_M · llama.cpp', 2048, 65536, 16384, {
          gpu1: placement('RTX 3090 #2', {gpu1: [5632, .12], ram: [768, 0]}),
          gpu0: placement('RTX 3090 #1', {gpu0: [5632, .12], ram: [768, 0]}),
          cpu: placement('System RAM', {ram: [6656, .14]}),
        }),
        media('sdxl', 'SDXL · Base 1.0', 'image', 'Diffusion · sdcpp', 'Measured · 1024 × 1024 · batch 1', {
          gpu0: placement('RTX 3090 #1', {gpu0: [7168, 0], ram: [3072, 0]}),
          gpu1: placement('RTX 3090 #2', {gpu1: [7168, 0], ram: [3072, 0]}),
        }, true),
        media('whisper', 'Whisper · large v3', 'audio', 'Speech recognition · audiocpp', 'Measured · 30 s input envelope', {
          gpu1: placement('RTX 3090 #2', {gpu1: [1536, 0], ram: [512, 0]}),
          cpu: placement('System RAM', {ram: [3584, 0]}),
        }),
      ],
    },
    cedar: {
      summary: 'CPU inference · 32 GiB system RAM',
      // An inventory-only integrated device must never become another memory pool.
      inventory: [{name: 'AMD Radeon integrated graphics', independentMemory: false}],
      resources: [ram(32768, 6144, 4096, 20480)],
      models: [
        llm('qwen4', 'Qwen3 · 4B', 'Q4_K_M · llama.cpp · CPU', 2048, 32768, 16384, {
          cpu: placement('System RAM', {ram: [3584, .14]}),
        }, true),
        llm('gemma1', 'Gemma 3 · 1B', 'Q8_0 · llama.cpp · CPU', 2048, 32768, 8192, {
          cpu: placement('System RAM', {ram: [1536, .06]}),
        }),
        media('whisper-small', 'Whisper · small', 'audio', 'Speech recognition · CPU', 'Measured · 30 s input envelope', {
          cpu: placement('System RAM', {ram: [1536, 0]}),
        }, true),
        media('kokoro', 'Kokoro · 82M', 'audio', 'Speech synthesis · CPU', 'Measured · synthesis envelope', {
          cpu: placement('System RAM', {ram: [768, 0]}),
        }),
        media('sd15', 'Stable Diffusion · 1.5', 'image', 'Diffusion · sdcpp · CPU', 'Measured · 512 × 512 · batch 1', {
          cpu: placement('System RAM', {ram: [6144, 0]}),
        }),
      ],
    },
  };
  // Thirty distinct local choices on each node make catalogue scale reviewable.
  // These extra model/profile names and allocations are illustrative, not compatibility claims.
  const extraModels = [
    ['qwen-coder7', 'Qwen2.5 Coder · 7B', 'llm', 5120],
    ['qwen-coder14', 'Qwen2.5 Coder · 14B', 'llm', 9216],
    ['llama8', 'Llama 3.1 · 8B', 'llm', 5632],
    ['llama3', 'Llama 3.2 · 3B', 'llm', 2816],
    ['phi4', 'Phi 4 Mini · 3.8B', 'llm', 3072],
    ['mistral7', 'Mistral · 7B', 'llm', 5120],
    ['ministral8', 'Ministral · 8B', 'llm', 5632],
    ['gemma12', 'Gemma 3 · 12B', 'llm', 8192],
    ['deepseek8', 'DeepSeek R1 Distill · 8B', 'llm', 5632],
    ['deepseek14', 'DeepSeek R1 Distill · 14B', 'llm', 9216],
    ['smollm', 'SmolLM2 · 1.7B', 'llm', 1792],
    ['tinyllama', 'TinyLlama · 1.1B', 'llm', 1280],
    ['olmo7', 'OLMo 2 · 7B', 'llm', 5120],
    ['yi6', 'Yi · 6B', 'llm', 4352],
    ['qwen-embed', 'Qwen3 Embedding · 0.6B', 'llm', 1024],
    ['bge', 'BGE M3 · embeddings', 'llm', 1024],
    ['whisper-base', 'Whisper · base', 'audio', 512],
    ['whisper-medium', 'Whisper · medium', 'audio', 2048],
    ['whisper-turbo', 'Whisper · large v3 turbo', 'audio', 1536],
    ['piper', 'Piper · English voice', 'audio', 256],
    ['bark', 'Bark · small', 'audio', 3072],
    ['sd-turbo', 'SD Turbo', 'image', 4096],
    ['sdxl-turbo', 'SDXL Turbo', 'image', 6144],
    ['sd35', 'Stable Diffusion · 3.5', 'image', 12288],
    ['flux-schnell', 'FLUX.1 · Schnell', 'image', 14336],
    ['hunyuan', 'Hunyuan DiT', 'image', 12288],
    ['pixart', 'PixArt Sigma', 'image', 8192],
    ['janus', 'Janus Pro · 7B', 'image', 8192],
  ];
  for (const [nodeName, node] of Object.entries(nodes)) {
    for (const [id, name, kind, base] of extraModels) {
      if (node.models.length === 30) break;
      const cpuOnly = nodeName === 'cedar';
      const allocations = cpuOnly ? {ram: [base + 512, kind === 'llm' ? .12 : 0]} :
        {gpu0: [base, kind === 'llm' ? .1 : 0], ram: [512, 0]};
      const places = {[cpuOnly ? 'cpu' : 'gpu0']: placement(cpuOnly ? 'System RAM' : node.resources[0].label.split(' · ')[0], allocations)};
      if (!cpuOnly && kind !== 'image') places.cpu = placement('System RAM', {ram: [base + 512, kind === 'llm' ? .12 : 0]});
      node.models.push(kind === 'llm' ? llm(id, name, 'Q4 sample · llama.cpp', 2048, 32768, 8192, places) :
        media(id, name, kind, `${kind === 'audio' ? 'Audio' : 'Diffusion'} · sample profile`,
          kind === 'audio' ? 'Fixed sample audio envelope' : 'Fixed sample · 512 × 512 · batch 1', places));
    }
  }
  const descriptions = {
    workbench: '01 / Selected rows — recommended. Pick from 30 searchable models, then tune only your selection in compact rows.',
    matrix: '02 / Split browser — keep the searchable catalogue beside your selected models. Each list scrolls independently.',
    studio: '03 / All-model sheet — scan all 30 choices in one bounded table. Selected rows expose context and placement controls.',
  };
  const storageKey = 'owg-fit-design-sets-v1';
  let machine = 'atlas', design = 'workbench', filter = 'all', scenario = 'ready';
  let drafts = Object.fromEntries(Object.entries(nodes).map(([key, node]) => [key, copy(node.models)]));
  let saved = [];
  try {
    const stored = JSON.parse(localStorage.getItem(storageKey) || '[]');
    if (Array.isArray(stored)) saved = stored.filter(set => set && nodes[set.node] && typeof set.name === 'string' && Number.isInteger(set.version) && Array.isArray(set.models));
  } catch { /* The preview also works where local storage is disabled. */ }
  const allModels = () => scenario === 'empty' ? [] : drafts[machine];
  const selected = () => allModels().filter(model => model.selected);
  const currentPlacement = model => model.placements[model.mode];
  const capacity = resource => Math.min(resource.total - resource.other - resource.reserve, resource.limit ?? Infinity);
  const memory = model => Object.fromEntries(Object.entries(currentPlacement(model).allocations)
    .map(([id, [base, slope]]) => [id, base + slope * (model.kind === 'llm' ? model.context : 0)]));
  const memoryLabel = model => currentPlacement(model).unknown ? 'Unknown memory' : Object.entries(memory(model))
    .map(([id, amount]) => `${giB(amount)} ${id === 'ram' ? 'RAM' : nodes[machine].resources.find(r => r.id === id).label.split(' · ')[0]}`).join(' + ');
  const announce = message => { $('#fit-message').textContent = message; };
  const setState = () => selected().map(model => ({id: model.id, mode: model.mode, context: model.kind === 'llm' ? model.context : undefined, locked: Boolean(model.locked)}));
  const sameDefinition = set => JSON.stringify(set.models) === JSON.stringify(setState());

  function judgment() {
    const models = selected();
    const used = Object.fromEntries(nodes[machine].resources.map(resource => [resource.id, 0]));
    for (const model of models) for (const [id, amount] of Object.entries(memory(model))) used[id] += amount;
    const over = nodes[machine].resources.filter(resource => used[resource.id] > capacity(resource) + .001);
    const conflicts = models.filter(model => currentPlacement(model).exclusive && models.some(other => other !== model &&
      Object.keys(currentPlacement(model).allocations).some(id => id !== 'ram' && memory(other)[id] > 0)));
    const unknown = models.some(model => currentPlacement(model).unknown);
    if (scenario === 'offline') return {used, state: 'unknown', title: 'Node is offline', detail: 'Last-known models remain visible. Reconnect before confirming capacity or loading.'};
    if (scenario === 'unknown') return {used, state: 'unknown', title: 'Capacity is unknown', detail: 'Memory telemetry is unavailable. Model reservations alone cannot prove this set fits.'};
    if (!models.length) return {used, state: 'empty', title: 'Room for a new combination', detail: 'Choose local models to see their combined memory.'};
    if (unknown) return {used, state: 'unknown', title: 'Memory profile needed', detail: 'An unqualified model is selected. Known reservations are shown; the total fit remains unknown.'};
    if (conflicts.length) return {used, state: 'blocked', title: 'Exclusive placement conflict', detail: `${conflicts.map(m => m.name).join(', ')} needs a GPU to itself.`};
    if (over.length) return {used, state: 'over', title: 'A little too much', detail: over.map(resource => `${giB(used[resource.id] - capacity(resource))} over on ${resource.label}`).join(' · ')};
    return {used, state: 'fits', title: 'This combination fits the estimate', detail: `${models.length} models, with host headroom and other use already allowed for. Server review still required.`};
  }

  function renderModels() {
    const models = allModels();
    const visible = models.filter(model => (design === 'studio' || model.selected) &&
      (filter === 'all' || model.kind === filter) && `${model.name} ${model.detail}`.toLowerCase().includes($('#search').value.toLowerCase()));
    const fragments = visible.map(model => {
      const row = clone('fit-model-template');
      const prefix = `${machine}-${model.id}`;
      row.dataset.model = model.id;
      row.style.setProperty('--fit-model-color', palette[models.indexOf(model) % palette.length]);
      row.classList.toggle('is-selected', model.selected);
      const tick = row.querySelector('[data-select]');
      tick.id = `${prefix}-selected`; tick.checked = model.selected;
      const name = row.querySelector('[data-name]'); name.htmlFor = tick.id; name.textContent = model.name;
      row.querySelector('[data-detail]').textContent = model.detail;
      const kind = row.querySelector('[data-kind]'); kind.textContent = {llm: 'LLM', audio: 'Audio', image: 'Image'}[model.kind];
      kind.classList.add(`bg-${{llm: 'blue', audio: 'teal', image: 'purple'}[model.kind]}-lt`);
      const place = row.querySelector('[data-placement]'); place.id = `${prefix}-placement`;
      row.querySelector('[data-placement-label]').htmlFor = place.id;
      row.querySelector('[data-placement-label]').textContent = `Placement for ${model.name}`;
      for (const [id, value] of Object.entries(model.placements)) place.add(new Option(value.label, id));
      place.value = model.mode;
      const contextControls = row.querySelector('[data-context-controls]');
      contextControls.hidden = model.kind !== 'llm';
      const fixed = row.querySelector('[data-fixed]'); fixed.hidden = model.kind === 'llm';
      row.querySelector('[data-profile]').textContent = model.profile || '';
      if (model.kind === 'llm') {
        const number = row.querySelector('[data-context]'), slider = row.querySelector('[data-slider]');
        number.id = `${prefix}-context`; row.querySelector('[data-context-label]').htmlFor = number.id;
        row.querySelector('[data-context-label]').textContent = `Context tokens for ${model.name}`;
        for (const input of [number, slider]) {input.min = model.min; input.max = model.max; input.value = model.context;}
        slider.setAttribute('aria-label', `Context slider for ${model.name}`);
        row.querySelector('[data-lock]').checked = model.locked;
        row.querySelector('[data-lock]').setAttribute('aria-label', `Lock context for ${model.name}`);
        row.querySelector('[data-bounds]').textContent = `${integer(model.min)}–${integer(model.max)} tokens · sample measured range`;
        number.title = slider.title = `${integer(model.min)}–${integer(model.max)} tokens`;
      } else contextControls.remove();
      row.querySelector('[data-memory]').textContent = memoryLabel(model);
      row.querySelector('[data-evidence]').textContent = currentPlacement(model).unknown ? 'Needs qualification' :
        currentPlacement(model).hybrid ? 'Qualified hybrid sample · full host reservation' : model.kind === 'llm' ? 'Interpolated sample curve' : 'Fixed sample envelope';
      row.querySelector('[data-memory]').title = row.querySelector('[data-evidence]').textContent;
      return row;
    });
    $('#models').replaceChildren(...fragments);
    $('#models-empty').hidden = Boolean(visible.length);
    $('#models-empty').textContent = scenario === 'empty' ? 'No models are available on this node. Choose another node.' :
      !selected().length && design !== 'studio' ? 'Choose models from the catalogue to start tuning.' : 'No matching models. Clear your search or choose another type.';
    $('#visible-count').textContent = `${visible.length} rows shown · ${selected().length} selected across all types`;
    renderCatalogue();
  }

  function renderCatalogue() {
    const models = allModels(), query = $('#catalogue-search').value.toLowerCase();
    const kind = $('#catalogue-kind').value, order = $('#catalogue-sort').value;
    const visible = models.filter(model => (kind === 'all' || model.kind === kind) &&
      `${model.name} ${model.detail}`.toLowerCase().includes(query));
    const total = model => currentPlacement(model).unknown ? Infinity : Object.values(memory(model)).reduce((a, b) => a + b, 0);
    visible.sort((a, b) => (order === 'memory' ? total(a) - total(b) : order === 'selected' ? Number(b.selected) - Number(a.selected) : 0) || a.name.localeCompare(b.name));
    const scroll = $('#catalogue-list').scrollTop;
    $('#catalogue-list').replaceChildren(...visible.map(model => {
      const item = clone('fit-catalogue-template');
      const checkbox = item.querySelector('[data-catalogue-select]');
      checkbox.dataset.catalogueSelect = model.id;
      checkbox.id = `catalogue-${machine}-${model.id}`;
      checkbox.checked = model.selected;
      checkbox.setAttribute('aria-label', `Select ${model.name}`);
      item.querySelector('[data-name]').textContent = model.name;
      item.querySelector('[data-detail]').textContent = model.kind === 'llm' ? `${model.detail} · up to ${integer(model.max)} tokens` : model.detail;
      item.querySelector('[data-catalogue-kind]').textContent = {llm: 'LLM', audio: 'Audio', image: 'Image'}[model.kind];
      item.querySelector('[data-catalogue-memory]').textContent = currentPlacement(model).unknown ? 'Unknown' : giB(total(model));
      return item;
    }));
    if (!visible.length) $('#catalogue-list').textContent = models.length ? 'No matching models. Clear the search or change type.' : 'No local models available.';
    $('#catalogue-list').scrollTop = scroll;
    $('#catalogue-count').textContent = `${visible.length} of ${models.length} models · ${selected().length} selected`;
    $('#catalogue-open').textContent = `Choose models · ${models.length}`;
  }

  function renderBudget() {
    const verdict = judgment(), models = selected();
    $('#verdict').dataset.state = verdict.state;
    $('#verdict-title').textContent = verdict.title;
    $('#verdict-detail').textContent = verdict.detail;
    $('#live-budget-state').dataset.state = verdict.state;
    $('#live-budget-state').textContent = {fits: 'Fits estimate', over: 'Over budget', blocked: 'Placement conflict', unknown: 'Fit unknown', empty: 'No models selected'}[verdict.state];
    $('#live-budget-values').textContent = nodes[machine].resources.map(resource => `${resource.label.replace(' · VRAM', '')}: ${giB(verdict.used[resource.id])} / ${scenario === 'unknown' || scenario === 'offline' ? 'unknown' : giB(capacity(resource))} budget`).join(' · ');
    $('#selection-count').textContent = `${models.length} selected · ${models.filter(m => m.kind === 'llm').length} LLM · ${models.filter(m => m.kind === 'audio').length} audio · ${models.filter(m => m.kind === 'image').length} image`;
    const unavailable = scenario === 'unknown' || scenario === 'offline';
    $('#resources').replaceChildren(...nodes[machine].resources.map(resource => {
      const row = clone('fit-resource-template');
      row.dataset.resource = resource.id;
      row.setAttribute('aria-label', resource.label);
      row.querySelector('[data-resource-name]').textContent = resource.label;
      row.querySelector('[data-resource-total]').textContent = unavailable ? 'Capacity unknown' : `${giB(resource.total)} physical`;
      const used = verdict.used[resource.id], free = capacity(resource) - used;
      row.querySelector('[data-resource-used]').textContent = giB(used);
      const incomplete = models.some(model => currentPlacement(model).unknown);
      row.querySelector('[data-resource-free]').textContent = incomplete ? 'known reservations · allocation incomplete' : unavailable ? 'known selected reservations' :
        `${giB(Math.abs(free))} ${free < 0 ? 'over budget' : 'left for models'}`;
      const bar = row.querySelector('[data-resource-bar]');
      bar.hidden = unavailable;
      const scale = Math.max(resource.total, used + resource.other + resource.reserve);
      const segments = [];
      const segment = (amount, label, color, className = '') => {
        const element = clone('fit-segment-template');
        element.style.width = `${amount / scale * 100}%`;
        if (color) element.style.backgroundColor = color;
        if (className) element.classList.add(className);
        element.title = `${label}: ${giB(amount)}`;
        segments.push(element);
      };
      if (!unavailable) segment(resource.other, 'Other use', '', 'bg-secondary');
      for (const model of models) if (memory(model)[resource.id]) segment(memory(model)[resource.id], model.name, palette[allModels().indexOf(model) % palette.length]);
      if (!unavailable) {
        segment(Math.max(0, resource.total - resource.other - resource.reserve - used), 'Unallocated', 'transparent');
        segment(resource.reserve, 'Kept free for host', '', 'fit-headroom');
      }
      bar.replaceChildren(...segments);
      bar.setAttribute('aria-label', unavailable ? `${resource.label}: capacity unknown; ${giB(used)} known reservations` :
        `${resource.label}: ${giB(used)} selected, ${giB(Math.abs(free))} ${free < 0 ? 'over budget' : 'left for models'}`);
      row.querySelector('[data-resource-note]').textContent = unavailable ? 'No reliable capacity snapshot. The bar is not a fit verdict.' :
        `${giB(resource.other)} other use · ${giB(resource.reserve)} kept free${resource.limit ? ` · ${giB(resource.limit)} model RAM limit` : ''}`;
      return row;
    }));
    const hybrid = models.some(model => currentPlacement(model).hybrid);
    $('#memory-notes').textContent = machine === 'cedar' ? 'One shared system RAM budget. This node runs models on the CPU.' :
      hybrid ? 'Hybrid profile selected: the engine reserves system RAM as well as VRAM. Its full RAM budget counts here; loading can be slower.' :
        'System RAM includes CPU models and host allocations. Extra RAM does not automatically absorb a full GPU; spillover needs a supported engine profile.';
    const invalid = $$('input[data-context]').some(input => !input.checkValidity());
    $('#save').disabled = !models.length || scenario === 'offline' || invalid;
    $('#review-open').disabled = !models.length || invalid;
    $('#auto-fit').disabled = !models.some(m => m.kind === 'llm' && !m.locked) || unavailable || invalid;
  }

  function renderSaved() {
    const prior = $('#saved-sets').value;
    const sets = saved.filter(set => set.node === machine);
    $('#saved-sets').replaceChildren();
    if (!sets.length) $('#saved-sets').add(new Option('No saved sets yet', ''));
    sets.forEach((set, index) => $('#saved-sets').add(new Option(`${set.name} · v${set.version}`, String(index))));
    if ([...$('#saved-sets').options].some(option => option.value === prior)) $('#saved-sets').value = prior;
    $('#restore').disabled = !sets.length || scenario === 'empty' || scenario === 'offline';
    $('#api-open').disabled = !sets.length;
    $('#saved-detail').textContent = sets.length ? `${sets.length} saved set${sets.length === 1 ? '' : 's'} · ${machine} only · persists in this browser` : 'Save a combination to see its API loading example.';
  }

  function render() {
    const focused = document.activeElement?.id;
    const catalogueParent = design === 'matrix' ? $('.fit-workspace') : $('#catalogue-dialog');
    if ($('#catalogue').parentElement !== catalogueParent) catalogueParent.append($('#catalogue'));
    if (design === 'matrix' && $('#catalogue-dialog').open) $('#catalogue-dialog').close();
    $('#workspace').dataset.design = design;
    $('#design-description').textContent = descriptions[design];
    $('#node-summary').textContent = nodes[machine].summary;
    for (const button of $$('button[data-design]')) {
      button.classList.toggle('active', button.dataset.design === design);
      button.setAttribute('aria-pressed', String(button.dataset.design === design));
    }
    renderModels(); renderBudget(); renderSaved();
    if (focused) document.getElementById(focused)?.focus({preventScroll: true});
  }

  function fitContexts() {
    const movable = selected().filter(model => model.kind === 'llm' && !model.locked);
    const prior = new Map(movable.map(model => [model, model.context]));
    const fits = () => judgment().state === 'fits';
    for (const model of movable) model.context = model.min;
    if (!fits()) {
      for (const [model, context] of prior) model.context = context;
      announce('Cannot auto fit: minimum contexts, locked models, fixed reservations or unknown memory prevent a complete fit. Change the selection or placement.');
      return;
    }
    // Maximize a common fraction of each measured range; locks and fixed profiles stay put.
    let low = 0, high = 1;
    for (let i = 0; i < 22; i++) {
      const share = (low + high) / 2;
      for (const model of movable) model.context = Math.floor(model.min + share * (model.max - model.min));
      if (fits()) low = share; else high = share;
    }
    for (const model of movable) model.context = Math.floor(model.min + low * (model.max - model.min));
    // Let models on less busy resources use their remaining capacity.
    for (const model of movable) {
      let from = model.context, to = model.max;
      while (from < to) {
        model.context = Math.ceil((from + to) / 2);
        if (fits()) from = model.context; else to = model.context - 1;
      }
      model.context = from;
    }
    announce(`Adjusted ${movable.length} unlocked LLM context${movable.length === 1 ? '' : 's'} within the measured ranges. Locked contexts and media profiles stayed fixed.`);
    render();
  }

  function saveSet(event) {
    event.preventDefault();
    const name = $('#set-name').value.trim();
    if (!name || !selected().length || scenario === 'offline' || $$('input[data-context]').some(input => !input.checkValidity())) {announce('Choose models with valid contexts and enter a set name on an available node.'); return;}
    const previous = saved.find(set => set.node === machine && set.name === name);
    if (previous && !$('#replace').checked) {announce('This name already exists on this node. Tick Replace to save a new version.'); return;}
    const set = {node: machine, name, version: previous ? previous.version + 1 : 1, models: setState()};
    saved = [...saved.filter(one => one !== previous), set];
    let persistence = 'Saved in this browser';
    try {localStorage.setItem(storageKey, JSON.stringify(saved));} catch {persistence = 'Saved for this tab only; browser storage is unavailable';}
    announce(`${persistence}: ${name} · ${machine} · version ${set.version}. No models were loaded.`);
    renderSaved();
    $('#saved-sets').value = String(saved.filter(one => one.node === machine).indexOf(set));
  }

  const chosenSet = () => saved.filter(set => set.node === machine)[Number($('#saved-sets').value)];
  function restoreSet() {
    const set = chosenSet();
    if (!set) return;
    const warnings = [];
    for (const model of allModels()) {
      const entry = set.models.find(one => one.id === model.id);
      model.selected = Boolean(entry);
      if (!entry) continue;
      if (!model.placements[entry.mode]) {model.selected = false; warnings.push(`${model.name}: placement unavailable`); continue;}
      model.mode = entry.mode;
      if (model.kind === 'llm') {
        if (!Number.isInteger(entry.context) || entry.context < model.min || entry.context > model.max) {
          model.selected = false; warnings.push(`${model.name}: context outside current bounds`); continue;
        }
        model.context = entry.context; model.locked = Boolean(entry.locked);
      }
    }
    for (const entry of set.models) if (!allModels().some(model => model.id === entry.id)) warnings.push('A saved model is no longer available.');
    $('#set-name').value = set.name; $('#replace').checked = true;
    announce(warnings.length ? warnings.join(' ') : `Selected ${set.name} · v${set.version} on ${machine}. Exact saved settings restored; nothing loaded.`);
    render();
  }

  function openReview() {
    const verdict = judgment();
    $('#review-summary').textContent = `${$('#set-name').value.trim() || 'Untitled set'} · ${machine} · ${selected().length} models`;
    $('#review-models').replaceChildren(...selected().map(model => {
      const row = clone('fit-breakdown-template');
      row.querySelector('[data-name]').textContent = model.name;
      row.querySelector('[data-context]').textContent = model.kind === 'llm' ? `${integer(model.context)} tokens` : model.profile;
      row.querySelector('[data-placement]').textContent = currentPlacement(model).label;
      row.querySelector('[data-memory]').textContent = memoryLabel(model);
      return row;
    }));
    if (!selected().length) $('#review-models').append(clone('fit-empty-template'));
    const set = saved.find(one => one.node === machine && one.name === $('#set-name').value.trim());
    const savedMatches = set && sameDefinition(set);
    $('#review-status').textContent = `${verdict.title}. ${verdict.detail}${!savedMatches ? ' Save this exact selection before simulating its named load.' : ` Saved version ${set.version} matches this selection.`}`;
    $('#simulate-load').disabled = verdict.state !== 'fits' || !savedMatches;
    $('#review').showModal();
  }

  function apiExample() {
    const set = chosenSet(); if (!set) return;
    $('#api-summary').textContent = `${set.name} · version ${set.version} · ${machine}`;
    $('#api-note').textContent = 'Sample set only. This preview does not create real load-outs. The node-scoped API below is implemented; save a set on the live Dry fit page before running these calls. The primary worker also supports POST /loadcoach/loadouts.';
    // JSON text goes into a code node, never into HTML or an executed command.
    const reviewBody = JSON.stringify({action: 'preview', name: set.name});
    const requestBody = JSON.stringify({action: 'request', name: set.name, token: 'TOKEN_FROM_REVIEW'});
    // Pipe JSON through a quoted heredoc, so names cannot turn into shell commands.
    const heredoc = body => {let delimiter = 'OWG_JSON'; while (body.split('\n').includes(delimiter)) delimiter += '_'; return delimiter;};
    const curl = (body, suffix = '') => {
      const delimiter = heredoc(body);
      return `curl --fail-with-body "$OWG_URL/turnstile/machines/${encodeURIComponent(machine)}/control/loadouts" \\\n  -H "Authorization: Bearer $OWG_KEY" \\\n  -H 'Content-Type: application/json' --data-binary @- ${suffix}<<'${delimiter}'\n${body}\n${delimiter}`;
    };
    $('#api-code').textContent = `# First save this set through the live Dry fit page on the selected node.\n# 1. Review the saved definition, changes, warnings and joint admission.\n${curl(reviewBody)}\n\n# 2. After reviewing, substitute the returned token.\n# Add "acknowledge":true only after accepting any warnings.\n${curl(requestBody)}\n\n# Success returns a ready lease, version and seconds: 600.\n# Renew/release through the same endpoint using the lease ID.`;
    $('#copy-status').textContent = '';
    $('#api').showModal();
  }

  $('#models').addEventListener('change', event => {
    const row = event.target.closest('[data-model]'); if (!row) return;
    const model = allModels().find(one => one.id === row.dataset.model);
    if (event.target.matches('[data-select]')) model.selected = event.target.checked;
    else if (event.target.matches('[data-placement]')) model.mode = event.target.value;
    else if (event.target.matches('[data-lock]')) model.locked = event.target.checked;
    else return;
    render();
  });
  $('#models').addEventListener('input', event => {
    const input = event.target;
    if (!input.matches('[data-context], [data-slider]')) return;
    const row = input.closest('[data-model]'), model = allModels().find(one => one.id === row.dataset.model);
    const value = Number(input.value);
    if (!input.value || !Number.isInteger(value) || value < model.min || value > model.max) {
      input.setCustomValidity(`Enter a whole number from ${model.min} to ${model.max}.`);
      announce(`Context for ${model.name} must be a whole number from ${integer(model.min)} to ${integer(model.max)}. The last valid value remains in the estimate.`);
      $('#save').disabled = true; $('#review-open').disabled = true; $('#auto-fit').disabled = true;
      return;
    }
    input.setCustomValidity(''); row.querySelector('[data-context]').setCustomValidity(''); model.context = value;
    row.querySelector('[data-context]').value = value; row.querySelector('[data-slider]').value = value;
    row.querySelector('[data-memory]').textContent = memoryLabel(model);
    announce(''); renderBudget();
  });
  $('#fit-editor').addEventListener('submit', event => event.preventDefault());
  $('#catalogue-list').addEventListener('change', event => {
    const checkbox = event.target.closest('[data-catalogue-select]'); if (!checkbox) return;
    allModels().find(model => model.id === checkbox.dataset.catalogueSelect).selected = checkbox.checked;
    render();
  });
  $('#catalogue-open').addEventListener('click', () => {
    $('#catalogue-dialog').showModal(); $('#catalogue-search').focus();
  });
  $('#catalogue-done').addEventListener('click', () => $('#catalogue-dialog').close());
  $('#catalogue-search').addEventListener('input', () => {$('#catalogue-list').scrollTop = 0; renderCatalogue();});
  for (const id of ['catalogue-kind', 'catalogue-sort']) $(`#${id}`).addEventListener('change', () => {$('#catalogue-list').scrollTop = 0; renderCatalogue();});
  const updateFilterButtons = () => $$('[data-kind-filter]').forEach(button => {
    button.classList.toggle('active', button.dataset.kindFilter === filter);
    button.setAttribute('aria-pressed', String(button.dataset.kindFilter === filter));
  });
  $$('[data-kind-filter]').forEach(button => button.addEventListener('click', () => {filter = button.dataset.kindFilter; updateFilterButtons(); renderModels();}));
  $$('button[data-design]').forEach(button => button.addEventListener('click', () => {
    design = button.dataset.design; location.hash = design; render();
  }));
  $('#node').addEventListener('change', () => {
    machine = $('#node').value; $('#search').value = ''; $('#set-name').value = 'Daily assistant'; $('#replace').checked = false;
    $('#catalogue-search').value = ''; $('#catalogue-kind').value = 'all'; $('#catalogue-list').scrollTop = 0;
    announce(`Working on ${machine}. Its own models, draft and saved sets are shown.`); render();
  });
  $('#scenario').addEventListener('change', () => {scenario = $('#scenario').value; announce(''); render();});
  $('#search').addEventListener('input', renderModels);
  $('#auto-fit').addEventListener('click', fitContexts);
  $('#clear').addEventListener('click', () => {allModels().forEach(model => {model.selected = false;}); announce('Selection cleared for this node. Saved sets are kept.'); render();});
  $('#save-set').addEventListener('submit', saveSet);
  $('#restore').addEventListener('click', restoreSet);
  $('#review-open').addEventListener('click', openReview);
  $('#simulate-load').addEventListener('click', () => {
    $('#review').close(); announce(`Simulation complete: ${$('#set-name').value.trim()} would load on ${machine} and receive a 600-second set hold. No real API was called.`);
  });
  $('#api-open').addEventListener('click', apiExample);
  $('#copy-api').addEventListener('click', async () => {
    try {await navigator.clipboard.writeText($('#api-code').textContent); $('#copy-status').textContent = 'Copied API example.';}
    catch {$('#copy-status').textContent = 'Clipboard is unavailable here. Select and copy the example above.';}
  });
  $$('[data-close]').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark';
    const control = $('[data-theme-form] [name="mode"]');
    control.value = dark ? 'dark' : 'light';
    control.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = dark ? 'Light mode' : 'Dark mode';
  });
  $('#reset').addEventListener('click', () => {
    drafts = Object.fromEntries(Object.entries(nodes).map(([key, node]) => [key, copy(node.models)]));
    scenario = 'ready'; $('#scenario').value = scenario; filter = 'all'; $('#search').value = '';
    $('#catalogue-search').value = ''; $('#catalogue-kind').value = 'all'; $('#catalogue-sort').value = 'name';
    updateFilterButtons(); announce('Demo selections reset. Your browser-saved sets are kept.'); render();
  });
  window.addEventListener('hashchange', () => {
    if (descriptions[location.hash.slice(1)]) {design = location.hash.slice(1); render();}
  });
  if (descriptions[location.hash.slice(1)]) design = location.hash.slice(1);
  render();
})();
