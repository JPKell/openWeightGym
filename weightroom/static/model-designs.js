/* Sample-only model settings design review. No network calls; saving, copying and moving are simulated. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const copy = value => structuredClone(value);
  const blank = value => value === '' || value == null;
  const same = (a, b) => JSON.stringify(blank(a) ? '' : a) === JSON.stringify(blank(b) ? '' : b);
  const num = value => blank(value) ? '' : Number(value).toLocaleString('en-US');
  const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;
  const words = items => items.length < 3 ? items.join(' and ') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`;
  const resolve = value => typeof value === 'function' ? value() : value;
  const basename = path => path ? path.split('/').pop() : 'none';

  const descriptions = {
    sheet: 'Model studio · A focused editor with grouped settings and a setup summary alongside. Recommended for everyday tuning.',
    summary: 'Configuration overview · See the whole setup in six compact cards. Open a group to edit without losing your draft.',
    matrix: 'Node workbench · Compare copies side by side, filter by settings group, and edit selected nodes together.'
  };

  // ---------- Sample fleet and models ----------
  const gpu = (id, model, gb) => ({ id, label: `${id} · ${model} ${gb} GB`, gb });
  const FLEET = [
    { name: 'atlas', state: 'ready', cards: [gpu('CUDA0', 'RTX 4090', 24), gpu('CUDA1', 'RTX 4090', 24)], free: 412, hasFile: true },
    { name: 'orion', state: 'ready', cards: [gpu('CUDA0', 'RTX 3090', 24)], free: 180, hasFile: true },
    { name: 'nova', state: 'offline', cards: [gpu('CUDA0', 'RTX A6000', 48)], free: null, hasFile: true },
    { name: 'cedar', state: 'ready', cards: [gpu('CUDA0', 'RTX 4080', 16), gpu('CUDA1', 'RTX 4080', 16)], free: 96, hasFile: false },
    { name: 'lyra', state: 'ready', cards: [], free: 640, hasFile: true },
    { name: 'vega', state: 'disabled', cards: [gpu('CUDA0', 'RTX 4090', 24)], free: 300, hasFile: false },
  ];
  const STATE_WORDS = { ready: 'available', offline: 'not reachable', disabled: 'disabled' };
  const STATE_COLORS = { ready: 'bg-green-lt', offline: 'bg-red-lt', disabled: 'bg-secondary-lt' };
  const TRAINED = 40960;
  const PROJECTORS = [['', 'none'], ['/models/qwen/mmproj-Qwen3-32B-f16.gguf', 'mmproj-Qwen3-32B-f16.gguf · metadata names agree'],
    ['/models/gemma/mmproj-gemma-3-27b-f16.gguf', 'mmproj-gemma-3-27b-f16.gguf · says it is for gemma-3-27b']];
  const ADAPTERS = [['', 'Choose a file'], ['/models/adapters/qwen3-32b-house-style.gguf', 'qwen3-32b-house-style.gguf · metadata names agree'],
    ['/models/adapters/qwen3-32b-sql.gguf', 'qwen3-32b-sql.gguf · metadata names agree'],
    ['/models/adapters/llama-3-tone.gguf', 'llama-3-tone.gguf · says it is for llama-3-8b']];
  const EFFORT = [['', 'Runtime default'], ['none', 'No reasoning'], ['minimal', 'Minimal'], ['low', 'Low'], ['medium', 'Medium'], ['high', 'High'], ['xhigh', 'Xhigh']];
  const SEPARATE_FLAGS = new Set(['model', 'ctx-size', 'mmproj', 'parallel', 'kv-unified', 'kv-unified-per-slot', 'no-kv-unified']);
  const GENERATION_FLAGS = new Set(['temp', 'top-p', 'top-k', 'min-p', 'reasoning', 'reasoning-effort', 'repeat-penalty', 'presence-penalty', 'seed', 'chat-template-kwargs']);
  const SUGGESTED = [
    { flag: 'mlock', value: 'on', why: 'Keeps the weights in RAM, so the system never swaps them out.' },
    { flag: 'cache-reuse', value: '256', why: 'Reuses matching prompt prefixes between requests.' },
  ];
  const REFERENCE = [
    ['ctx-size', 'N', 'Size of the prompt context.'], ['parallel', 'N', 'Number of parallel sequences to decode.'],
    ['flash-attn', 'on|off|auto', 'Use Flash Attention.'], ['cache-type-k', 'TYPE', 'KV cache data type for K.'],
    ['cache-type-v', 'TYPE', 'KV cache data type for V.'], ['threads', 'N', 'Number of CPU threads to use during generation.'],
    ['batch-size', 'N', 'Logical maximum batch size.'], ['ubatch-size', 'N', 'Physical maximum batch size.'],
    ['mlock', '', 'Force the system to keep the model in RAM rather than swapping.'], ['cache-reuse', 'N', 'Minimum chunk size to reuse from the cache via KV shifting.'],
    ['gpu-layers', 'N', 'Number of layers to store in VRAM.'], ['split-mode', 'none|layer|row', 'How to split the model across multiple GPUs.'],
    ['tensor-split', 'N0,N1,…', 'Fraction of the model to offload to each GPU.'], ['rope-scaling', 'none|linear|yarn', 'RoPE frequency scaling method.'],
    ['no-mmap', '', 'Do not memory-map the model.'], ['defrag-thold', 'N', 'KV cache defragmentation threshold.'],
  ];

  function sample(engine) {
    const llama = engine === 'llama_cpp';
    const node = values => ({ installed: true, enabled: true, stale: false, values });
    const nodes = Object.fromEntries(FLEET.map(item => [item.name, { installed: false, enabled: true, stale: false, values: null }]));
    if (llama) {
      nodes.atlas = node({ ctx: 32768, min_context: 8192, parallel_enabled: true, parallel_count: 4, parallel_context: 8192, parallel_shared: true,
        placement: 'auto', pins: [], allow_split: true, alone: false, flags: { 'flash-attn': 'on', 'cache-type-k': 'q8_0', 'cache-type-v': 'q8_0', threads: '16' } });
      nodes.orion = node({ ctx: 12288, min_context: '', parallel_enabled: true, parallel_count: 2, parallel_context: 6144, parallel_shared: false,
        placement: 'pin', pins: ['CUDA0'], allow_split: false, alone: true, flags: { 'flash-attn': 'on', threads: '12' } });
      nodes.nova = node({ ctx: 32768, min_context: 8192, parallel_enabled: false, parallel_count: 1, parallel_context: 32768, parallel_shared: true,
        placement: 'auto', pins: [], allow_split: true, alone: false, flags: { 'flash-attn': 'on', 'cache-type-k': 'q8_0', 'cache-type-v': 'q8_0', threads: '16' } });
    } else {
      nodes.atlas = node({ ctx: 512, min_context: '', gpu_card: 'CUDA1', alone: false });
      nodes.orion = node({ ctx: 512, min_context: '', gpu_card: '', alone: false });
    }
    const shared = llama ? { name: 'qwen3-32b', file: '/models/qwen/Qwen3-32B-Q4_K_M.gguf', projector: '', order: 10, temp: '0.6', 'top-p': '0.95',
      'top-k': '20', 'min-p': '0', reasoning: 'auto', 'reasoning-effort': '', 'repeat-penalty': '1.05',
      adapters: [{ name: 'house-style', file: '/models/adapters/qwen3-32b-house-style.gguf', scale: '0.8' }] }
      : { name: 'llama-3.1-8b-strata', file: '/prepared/llama-3.1-8b/weights.gguf', tokenizer: '/prepared/llama-3.1-8b/tokenizer', order: 20 };
    return {
      engine, design: S?.design || 'sheet', fleet: copy(FLEET), nodes, shared,
      weights: llama ? 19.8 : 4.9, perToken: llama ? 0.000256 : 0.000128,
      loaded: { atlas: { cards: llama ? ['CUDA0', 'CUDA1'] : ['CUDA1'], running: 2, waiting: llama ? 1 : 0 } },
      presets: llama ? [
        { name: 'Daily 16K', scope: '', node: { ctx: 16384, min_context: 8192, parallel_enabled: true, parallel_count: 2, parallel_context: 8192, parallel_shared: true }, shared: { temp: '0.7', 'top-p': '0.8', reasoning: 'off' } },
        { name: 'Deep reasoning 40K', scope: 'Same file only', node: { ctx: 40960, min_context: 16384, parallel_enabled: false, parallel_count: 1, parallel_context: 40960 }, shared: { temp: '0.6', reasoning: 'on', 'reasoning-effort': 'high' } },
      ] : [],
      history: [], scope: new Set(['atlas']), pick: new Set(), open: null, group: 'context', view: 'configuration', matrixGroup: 'all', collapsed: new Set(), measured: true,
    };
  }

  // ---------- Settings schema: groups ordered by how often they change ----------
  const F = (key, type, label, more = {}) => ({ key, type, label, ...more });
  const LLAMA = ['llama_cpp'], STRATA = ['strata'];
  const parallelOn = values => values.parallel_enabled;
  const GROUPS = [
    { id: 'context', title: 'Context and requests', scope: 'node', fields: [
      F('context', 'range', 'Context', { keys: ['min_context', 'ctx'],
        code: () => S.engine === 'strata' ? '512 qualified · 256–8,192' : `trained for ${num(TRAINED)}`,
        hint: 'Placement may shrink context down to the minimum to fit. A blank minimum keeps the maximum fixed.' }),
      F('parallel_enabled', 'switch', 'Enable parallel requests', { engines: LLAMA, wide: true, rerender: true, hint: 'Answer more than one request at a time.' }),
      F('parallel_count', 'number', 'Requests at once', { engines: LLAMA, min: 1, step: 1, needs: parallelOn }),
      F('parallel_context', 'number', 'Limit per request', { engines: LLAMA, min: 1, step: 1, unit: 'tokens', needs: parallelOn, hint: 'Prompt, history, tools, reasoning and output share it.' }),
      F('parallel_shared', 'switch', 'Share the context pool', { engines: LLAMA, needs: parallelOn, wide: true, hint: 'Requests draw from one pool as they need it. Off gives each request a fixed slot, even while idle.' }),
      F('fit', 'meter', 'What this gives you', { hint: 'Sample estimate from weights and context. Preview checks placement against live telemetry.' }),
    ] },
    { id: 'generation', title: 'Generation defaults', scope: 'all', engines: LLAMA, hint: 'Sampling and reasoning, wherever this model runs. Blank uses the runtime default.', fields: [
      F('temp', 'number', 'Temperature', { code: 'temp', step: 'any', min: 0, placeholder: 'Runtime default' }),
      F('top-p', 'number', 'Top-p', { code: 'top-p', step: 'any', min: 0, placeholder: 'Runtime default' }),
      F('top-k', 'number', 'Top-k', { code: 'top-k', step: 1, min: -1, placeholder: 'Runtime default' }),
      F('min-p', 'number', 'Min-p', { code: 'min-p', step: 'any', min: 0, placeholder: 'Runtime default' }),
      F('reasoning', 'select', 'Reasoning', { code: 'reasoning', options: [['on', 'On'], ['off', 'Off'], ['auto', 'Auto']] }),
      F('reasoning-effort', 'select', 'Reasoning effort', { code: 'reasoning-effort', options: EFFORT, hint: 'The template running on atlas offers low, medium and high.' }),
      F('repeat-penalty', 'text', 'Repeat penalty', { code: 'repeat-penalty' }),
    ] },
    { id: 'hardware', title: 'Hardware', scope: 'node', fields: [
      F('placement', 'choice', 'Placement', { engines: LLAMA, rerender: true, options: [['auto', 'Automatic GPU'], ['pin', 'Pin to GPUs'], ['cpu', 'CPU only']] }),
      F('pins', 'pins', 'GPUs', { engines: LLAMA, needs: values => values.placement === 'pin', hide: true }),
      F('gpu_card', 'pins', 'GPU', { engines: STRATA, single: true }),
      F('allow_split', 'switch', 'Allow split across GPUs', { engines: LLAMA, wide: true, needs: values => values.placement === 'auto', hide: true, hint: 'Lets it use several cards when one is not enough.' }),
      F('alone', 'switch', 'Keep GPUs to itself', { wide: true, hint: 'No other model is placed on its GPUs while it is loaded.' }),
    ] },
    { id: 'model', title: 'Model', scope: 'all', fields: [
      F('name', 'rename', 'Name', { hint: 'Renaming applies at once. Clients must use the new name.' }),
      F('file', 'text', () => S.engine === 'strata' ? 'Prepared GGUF weights' : 'File', { mono: true }),
      F('tokenizer', 'text', 'Prepared tokenizer directory', { engines: STRATA, mono: true }),
      F('projector', 'select', 'Projector', { engines: LLAMA, options: PROJECTORS }),
      F('order', 'number', 'List position', { step: 1, hint: 'Lower numbers appear first; matching positions sort by name.' }),
      F('engine', 'readonly', 'Engine', { value: () => S.engine === 'strata' ? 'Strata' : 'llama.cpp' }),
      F('strata-note', 'note', 'Strata uses one GPU and its configured hybrid host RAM budget. Text chat and streaming only; tools, images, adapters, GPU splitting, reasoning and native benchmarks are unsupported. No llama.cpp flags are inherited.', { engines: STRATA }),
    ] },
    { id: 'adapters', title: 'Adapters', scope: 'all', engines: LLAMA, fields: [F('adapters', 'adapters', 'Adapters')] },
    { id: 'advanced', title: 'Advanced loading flags', scope: 'node', engines: LLAMA, collapsible: true,
      hint: 'Other llama-server flags. Context, parallel requests and files have their own boxes above.', fields: [F('flags', 'flags', 'Flags')] },
  ];

  let S = null, uid = 0, live = [];
  const engineOk = item => !item.engines || item.engines.includes(S.engine);
  const groups = () => GROUPS.filter(engineOk).map(group => ({ ...group, fields: group.fields.filter(engineOk) }));
  const info = name => S.fleet.find(item => item.name === name);
  const installed = () => S.fleet.map(item => item.name).filter(name => S.nodes[name].installed);
  const scopeNames = () => installed().filter(name => S.scope.has(name));
  const targetNames = () => S.design === 'matrix' ? (S.pick.size ? installed().filter(name => S.pick.has(name)) : installed()) : scopeNames();
  const vals = name => S.nodes[name].values;
  const keysOf = field => field.keys || (['meter', 'note', 'readonly', 'rename', 'flags'].includes(field.type) ? [] : [field.key]);
  const valueOf = (group, key, name) => group.scope === 'all' ? S.shared[key] : vals(name)[key];
  const originalOf = (group, key, name) => group.scope === 'all' ? S.original.shared[key] : S.original.nodes[name]?.[key];
  const snapshot = () => ({ shared: copy(S.shared), nodes: Object.fromEntries(installed().map(name => [name, copy(vals(name))])) });

  function read(group, key, names) {
    if (group.scope === 'all') return { value: S.shared[key] };
    const values = names.map(name => vals(name)[key]);
    return values.every(value => same(value, values[0])) ? { value: values[0] } : { mixed: names.map((name, i) => [name, values[i]]) };
  }
  function write(group, key, value, names) {
    if (group.scope === 'all') S.shared[key] = copy(value);
    else names.forEach(name => { vals(name)[key] = copy(value); });
  }

  // ---------- Derived facts ----------
  function perRequest(values) {
    const total = Math.max(1, Number(values.ctx) || 1);
    if (!values.parallel_enabled) return total;
    const count = Math.max(1, Number(values.parallel_count) || 1);
    return Math.min(total, Math.max(1, Number(values.parallel_context) || 1), values.parallel_shared ? total : Math.floor(total / count));
  }
  function estimate(name) {
    const values = vals(name), node = info(name);
    const cache = values.flags?.['cache-type-k'] === 'q8_0' ? 0.5 : 1;
    const extra = (S.shared.projector ? 1.3 : 0) + (S.shared.adapters || []).filter(a => a.file).length * 0.3;
    const need = S.weights + extra + (Number(values.ctx) || 0) * S.perToken * cache;
    if (S.engine === 'llama_cpp' && values.placement === 'cpu') return { need, capacity: null, where: 'CPU' };
    let cards = node.cards;
    if (S.engine === 'strata') cards = values.gpu_card ? cards.filter(card => card.id === values.gpu_card) : cards.slice(0, 1);
    else if (values.placement === 'pin') cards = cards.filter(card => values.pins.includes(card.id));
    else if (!values.allow_split) cards = cards.slice(0, 1);
    return { need, capacity: cards.reduce((sum, card) => sum + card.gb, 0), where: cards.map(card => card.id).join(' + ') || 'no GPU' };
  }
  function requestsText(values) {
    if (S.engine === 'strata') return `One request at a time, up to ${num(values.ctx)} tokens`;
    const count = values.parallel_enabled ? Math.max(1, Number(values.parallel_count) || 1) : 1;
    return `${plural(count, 'request')} at once, up to ${num(perRequest(values))} tokens each`;
  }
  function display(field, key, value) {
    if (key === 'adapters') return (value || []).map(item => `${item.name || 'unnamed'} (${item.scale || 1})`).join(', ') || 'none';
    if (blank(value) || (Array.isArray(value) && !value.length)) return key === 'gpu_card' ? 'Any single enabled GPU' : 'not set · defaults apply';
    if (typeof value === 'boolean') return value ? 'On' : 'Off';
    if (key === 'placement') return { auto: 'Automatic GPU', pin: 'Pin to GPUs', cpu: 'CPU only' }[value];
    if (Array.isArray(value)) return value.join(', ');
    if (['ctx', 'min_context', 'parallel_context', 'parallel_count'].includes(key)) return num(value);
    if (key === 'projector' || key === 'file') return basename(value);
    return String(value);
  }
  function settingLabel(field, key) {
    if (key === 'ctx') return 'Maximum context';
    if (key === 'min_context') return 'Minimum context';
    return resolve(field.label);
  }
  function changes() {
    const out = [];
    for (const group of groups()) for (const field of group.fields) {
      for (const name of group.scope === 'all' ? [null] : installed()) {
        const where = name || 'All nodes';
        if (field.type === 'flags') {
          const before = S.original.nodes[name]?.flags || {}, after = vals(name).flags;
          for (const flag of new Set([...Object.keys(before), ...Object.keys(after)])) {
            if (!same(before[flag], after[flag]) || (flag in before) !== (flag in after)) out.push({ group, name, where, setting: `--${flag}`,
              before: flag in before ? (before[flag] || 'on') : 'not set', after: flag in after ? (after[flag] || 'on') : 'removed' });
          }
          continue;
        }
        for (const key of keysOf(field)) {
          const before = originalOf(group, key, name), after = valueOf(group, key, name);
          if (!same(before, after)) out.push({ group, name, where, setting: settingLabel(field, key), before: display(field, key, before), after: display(field, key, after) });
        }
      }
    }
    return out;
  }
  function fieldChanged(group, field, names) {
    if (field.type === 'flags') return names.some(name => !same(S.original.nodes[name]?.flags, vals(name).flags));
    return keysOf(field).some(key => group.scope === 'all' ? !same(S.original.shared[key], S.shared[key])
      : names.some(name => !same(S.original.nodes[name]?.[key], vals(name)[key])));
  }
  function problems() {
    const errors = [], warnings = [];
    for (const name of installed()) {
      const values = vals(name), max = Number(values.ctx), min = Number(values.min_context);
      if (!max || max < 1) errors.push(`${name}: set a maximum context.`);
      if (!blank(values.min_context) && min > max) errors.push(`${name}: the minimum context is larger than the maximum.`);
      if (S.engine === 'strata' && (max < 256 || max > 8192)) errors.push(`${name}: Strata context must be between 256 and 8,192 tokens.`);
      if (S.engine === 'llama_cpp') {
        if (max > TRAINED) warnings.push(`${name}: ${num(max)} tokens is more than the ${num(TRAINED)} this model was trained for.`);
        if (values.parallel_enabled && Number(values.parallel_context) > max) warnings.push(`${name}: the per-request limit exceeds the total context; the total pool remains the upper limit.`);
        if (values.placement === 'pin' && !values.pins.length) errors.push(`${name}: choose at least one GPU.`);
      }
      const fit = estimate(name);
      if (fit.capacity != null && fit.need > fit.capacity) warnings.push(`${name}: needs about ${fit.need.toFixed(1)} GB, but ${fit.where} has ${fit.capacity} GB. Lower the context or let it use more GPUs.`);
    }
    if (!S.shared.file) errors.push('Choose a model file.');
    (S.shared.adapters || []).forEach(item => { if (item.name && !item.file) errors.push(`Choose a file for adapter ${item.name}.`); });
    return { errors, warnings };
  }

  // ---------- Field rendering (shared by all three layouts) ----------
  function bindText(input, group, key, names, targets, numeric, placeholder = '') {
    const show = () => {
      const result = read(group, key, names);
      input.value = result.mixed ? '' : (result.value ?? '');
      input.placeholder = result.mixed ? 'Mixed' : placeholder;
    };
    show();
    input.addEventListener('input', () => {
      write(group, key, numeric && input.value !== '' ? Number(input.value) : input.value, targets());
      touch();
    });
    return () => { if (document.activeElement !== input) show(); };
  }

  function renderField(group, field, names, options = {}) {
    const compact = !!options.compact, targets = options.targets || (() => names);
    const el = clone(`#tpl-field-${field.type}`), id = `field-${++uid}`, text = resolve(field.label);
    const focusKey = `${field.key}|${names.join(',')}`;
    const label = $('[data-label]', el), code = $('[data-code]', el), hint = $('[data-hint]', el);
    const input = $('[data-input]', el), labelTag = $('label.form-label, label.form-check-label', el), syncs = [];
    el.dataset.key = field.key;
    if (label) label.textContent = compact && group.scope === 'node' ? `${text} on ${names[0]}` : text;
    if (code) code.textContent = resolve(field.code) || '';
    if (hint) hint.textContent = compact || options.hideHints ? '' : resolve(field.hint) || '';
    if (field.wide && !compact) el.classList.replace('col-md-6', 'col-12');
    if (compact) {
      el.classList.remove('col-md-6', 'col-12');
      el.classList.add('field-compact');
      if (label && field.type !== 'note') label.classList.add('visually-hidden');
      code?.remove();
    }
    if (input && labelTag) { input.id = id; labelTag.htmlFor = id; }
    if (input) input.dataset.focus = focusKey;

    switch (field.type) {
      case 'number': case 'text': {
        if (field.min != null) input.min = field.min;
        if (field.step) input.step = field.step;
        const unit = $('[data-unit]', el);
        if (unit) { if (field.unit && !compact) unit.textContent = field.unit; else unit.remove(); }
        if (field.mono) input.classList.add('font-monospace');
        syncs.push(bindText(input, group, field.key, names, targets, field.type === 'number' && group.scope === 'node', field.placeholder));
        break;
      }
      case 'range':
        $$('[data-input]', el).forEach(box => {
          const key = box.dataset.input;
          box.dataset.focus = `${key}|${names.join(',')}`;
          if (key !== 'min_context') box.removeAttribute('id');
          if (S.engine === 'strata') { box.min = 256; box.max = 8192; }
          box.setAttribute('aria-label', `${key === 'ctx' ? 'maximum' : 'minimum'} context${compact ? ` on ${names[0]}` : ''}`);
          syncs.push(bindText(box, group, key, names, targets, true));
        });
        break;
      case 'switch': {
        const result = read(group, field.key, names);
        input.checked = !!result.value;
        input.indeterminate = !!result.mixed;
        input.addEventListener('change', () => { write(group, field.key, input.checked, targets()); field.rerender ? rerender(input) : touch(); });
        break;
      }
      case 'select': {
        const result = read(group, field.key, names), current = result.value ?? '';
        const choices = resolve(field.options);
        if (!choices.some(([value]) => value === current)) input.add(new Option(`${basename(current)} · in none of the places`, current, true, true));
        choices.forEach(([value, label]) => input.add(new Option(label, value, value === current, value === current)));
        input.addEventListener('change', () => { write(group, field.key, input.value, targets()); touch(); });
        break;
      }
      case 'choice': {
        const result = read(group, field.key, names), box = $('[data-choices]', el);
        box.setAttribute('aria-label', compact ? `${text} on ${names[0]}` : text);
        field.options.forEach(([value, label]) => {
          const item = clone('#tpl-choice'), radio = $('input', item);
          Object.assign(radio, { name: id, value, checked: !result.mixed && result.value === value });
          radio.dataset.focus = `${focusKey}|${value}`;
          $('.form-selectgroup-label', item).textContent = compact ? ({ auto: 'Auto', pin: 'Pin', cpu: 'CPU' }[value] || label) : label;
          radio.addEventListener('change', () => { write(group, field.key, value, targets()); rerender(radio); });
          box.append(item);
        });
        break;
      }
      case 'pins': {
        const box = $('[data-pins]', el);
        const shown = field.needs ? names.filter(name => field.needs(vals(name))) : names;
        shown.forEach(name => {
          const part = clone('#tpl-pin-group'), cards = $('[data-cards]', part), node = info(name), values = vals(name);
          if (shown.length > 1) $('[data-node]', part).textContent = name; else $('[data-node]', part).remove();
          const choices = field.single ? [{ id: '', label: 'Any single enabled GPU' }, ...node.cards] : node.cards;
          if (!node.cards.length) { cards.textContent = 'No enabled GPUs reported for this node.'; cards.classList.add('form-hint'); }
          else choices.forEach(card => {
            const pin = clone('#tpl-pin'), box = $('input', pin);
            Object.assign(box, { type: field.single ? 'radio' : 'checkbox', name: `${id}-${name}`, value: card.id,
              checked: field.single ? (values.gpu_card || '') === card.id : values.pins.includes(card.id) });
            box.dataset.focus = `${field.key}|${name}|${card.id}`;
            const caption = $('.form-check-label', pin);
            caption.textContent = compact && card.id ? card.id : card.label;
            if (compact) box.setAttribute('aria-label', `${card.label} on ${name}`);
            box.addEventListener('change', () => {
              if (field.single) values.gpu_card = card.id;
              else values.pins = $$(`input[name="${id}-${name}"]:checked`, el).map(item => item.value);
              touch();
            });
            cards.append(pin);
          });
          box.append(part);
        });
        break;
      }
      case 'rename': {
        input.value = S.shared.name;
        $('[data-rename]', el).addEventListener('click', () => rename(input.value.trim()));
        break;
      }
      case 'readonly': $('[data-value]', el).textContent = resolve(field.value); break;
      case 'adapters': renderAdapters(el, group); break;
      case 'flags': renderFlags(el, group, names, targets); break;
      default: break;
    }

    const revert = !compact && !['readonly', 'meter', 'note', 'rename'].includes(field.type) ? clone('#tpl-revert') : null;
    if (revert) {
      el.append(revert);
      revert.setAttribute('aria-label', `Revert ${text}`);
      revert.addEventListener('click', () => {
        if (field.type === 'flags') names.forEach(name => { vals(name).flags = copy(S.original.nodes[name].flags); });
        else keysOf(field).forEach(key => {
          if (group.scope === 'all') S.shared[key] = copy(S.original.shared[key]);
          else names.forEach(name => { vals(name)[key] = copy(S.original.nodes[name][key]); });
        });
        rerender();
      });
    }
    const mixed = $('[data-mixed]', el);
    const refresh = () => {
      syncs.forEach(sync => sync());
      const changed = fieldChanged(group, field, names);
      el.classList.toggle('is-changed', changed);
      if (revert) revert.hidden = !changed;
      if (mixed) {
        const varied = group.scope === 'node' && names.length > 1 && field.type !== 'pins' ? keysOf(field).map(key => [key, read(group, key, names)]).filter(([, result]) => result.mixed) : [];
        mixed.hidden = !varied.length;
        mixed.textContent = varied.length ? `Varies · ${varied.map(([key, result]) => result.mixed.map(([name, value]) => `${name} ${display(field, key, value)}`).join(' · ')).join(' | ')}. A new value applies to all ${names.length}.` : '';
      }
      if (field.needs && group.scope === 'node') {
        const ok = names.some(name => field.needs(vals(name)));
        if (field.hide) el.hidden = !ok;
        else $$('input, select', el).forEach(item => { item.disabled = !ok; });
        el.classList.toggle('is-disabled', !ok);
      }
      if (field.type === 'meter') drawMeters(el, names, compact);
    };
    live.push({ refresh });
    refresh();
    return el;
  }

  function drawMeters(el, names, compact) {
    const box = $('[data-meters]', el);
    box.replaceChildren(...names.map(name => {
      const meter = clone('#tpl-meter'), fit = estimate(name), bar = $('.progress-bar', meter), progress = $('.progress', meter);
      $('[data-node]', meter).textContent = compact ? requestsText(vals(name)) : `${names.length > 1 ? `${name} · ` : ''}${requestsText(vals(name))}`;
      if (fit.capacity == null) { $('[data-amount]', meter).textContent = `CPU · about ${fit.need.toFixed(1)} GB of RAM`; progress.remove(); return meter; }
      const share = fit.capacity ? Math.min(100, fit.need / fit.capacity * 100) : 100;
      $('[data-amount]', meter).textContent = fit.capacity ? `${fit.need.toFixed(1)} of ${fit.capacity} GB on ${fit.where}` : 'No GPU reported';
      bar.style.width = `${share}%`;
      bar.classList.add(fit.need > fit.capacity ? 'bg-danger' : share > 90 ? 'bg-warning' : 'bg-primary');
      progress.setAttribute('aria-valuenow', String(Math.round(share)));
      progress.setAttribute('aria-label', `Estimated GPU memory on ${name}`);
      return meter;
    }));
  }

  function renderAdapters(el, group) {
    const rows = $('[data-adapter-rows]', el);
    S.shared.adapters.forEach((adapter, index) => {
      const row = clone('#tpl-adapter-row');
      const name = $('[data-adapter="name"]', row), file = $('[data-adapter="file"]', row), scale = $('[data-adapter="scale"]', row);
      name.value = adapter.name; scale.value = adapter.scale;
      name.dataset.focus = `adapter|${index}`;
      if (adapter.file && !ADAPTERS.some(([value]) => value === adapter.file)) file.add(new Option(`${basename(adapter.file)} · in none of the places`, adapter.file, true, true));
      ADAPTERS.forEach(([value, label]) => file.add(new Option(label, value, value === adapter.file, value === adapter.file)));
      name.setAttribute('aria-label', `adapter name ${index + 1}`);
      file.setAttribute('aria-label', `file of ${adapter.name || `adapter ${index + 1}`}`);
      scale.setAttribute('aria-label', `strength of ${adapter.name || `adapter ${index + 1}`}`);
      name.addEventListener('input', () => { adapter.name = name.value; touch(); });
      scale.addEventListener('input', () => { adapter.scale = scale.value; touch(); });
      file.addEventListener('change', () => { adapter.file = file.value; touch(); });
      $('[data-remove-adapter]', row).addEventListener('click', () => { S.shared.adapters.splice(index, 1); rerender(); });
      rows.append(row);
    });
    if (!S.shared.adapters.length) rows.append(clone('#tpl-empty-adapters'));
    $('[data-add-adapter]', el).addEventListener('click', () => {
      S.shared.adapters.push({ name: '', file: '', scale: '' });
      rerender(null, `adapter|${S.shared.adapters.length - 1}`);
    });
  }

  function renderFlags(el, group, names, targets) {
    const rows = $('[data-flag-rows]', el), message = $('[data-flag-message]', el);
    const keys = [...new Set(names.flatMap(name => Object.keys(vals(name).flags)))].sort();
    keys.forEach(flag => {
      const row = clone('#tpl-flag-row'), name = $('[data-flag-name]', row), value = $('[data-flag-value]', row);
      const values = names.map(item => vals(item).flags[flag]), missing = names.filter(item => !(flag in vals(item).flags));
      name.value = flag;
      value.value = values.every(item => item === values[0]) && !missing.length ? (values[0] ?? '') : '';
      value.placeholder = missing.length ? `Not set on ${words(missing)}` : values.every(item => item === values[0]) ? 'on' : 'Mixed';
      if (value.placeholder === 'Mixed') value.title = names.map((item, i) => `${item}: ${values[i]}`).join(', ');
      value.setAttribute('aria-label', `value of ${flag}`);
      value.dataset.focus = `flag|${flag}`;
      row.classList.toggle('is-changed', names.some(item => !same(S.original.nodes[item]?.flags[flag], vals(item).flags[flag])));
      value.addEventListener('input', () => { targets().forEach(item => { vals(item).flags[flag] = value.value; }); touch(); });
      $('[data-remove-flag]', row).setAttribute('aria-label', `Remove ${flag}`);
      $('[data-remove-flag]', row).addEventListener('click', () => { targets().forEach(item => { delete vals(item).flags[flag]; }); rerender(); });
      rows.append(row);
    });
    const add = () => {
      const flag = $('[data-new-flag]', el).value.trim().replace(/^-+/, ''), value = $('[data-new-value]', el).value.trim();
      if (!flag) return;
      if (SEPARATE_FLAGS.has(flag)) { message.textContent = `--${flag} has its own box above.`; return; }
      if (GENERATION_FLAGS.has(flag)) { message.textContent = `--${flag} is a generation default. Set it under Generation defaults, where it is shared by every node.`; return; }
      targets().forEach(item => { vals(item).flags[flag] = value; });
      rerender(null, `flag|${flag}`);
    };
    $('[data-add-flag]', el).addEventListener('click', add);
    $('[data-new-value]', el).addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); add(); } });
    const suggestions = $('[data-suggestions]', el);
    SUGGESTED.forEach(one => {
      const item = clone('#tpl-suggestion'), box = $('input', item), label = $('label', item), boxId = `suggest-${++uid}`;
      box.id = boxId; label.htmlFor = boxId;
      box.checked = names.every(name => vals(name).flags[one.flag] === one.value);
      $('[data-flag]', item).textContent = `${one.flag} = ${one.value}`;
      $('[data-why]', item).textContent = one.why;
      box.addEventListener('change', () => {
        targets().forEach(name => { if (box.checked) vals(name).flags[one.flag] = one.value; else delete vals(name).flags[one.flag]; });
        rerender(box);
      });
      box.dataset.focus = `suggest|${one.flag}`;
      suggestions.append(item);
    });
    const reference = $('[data-flag-reference]', el), search = $('[data-flag-search]', el), count = $('[data-flag-count]', el);
    REFERENCE.forEach(([flag, takes, says]) => {
      const row = clone('#tpl-reference-row');
      $('[data-flag]', row).textContent = flag;
      $('[data-takes]', row).textContent = SEPARATE_FLAGS.has(flag) ? 'has its own box above' : takes || 'nothing: a switch';
      $('[data-says]', row).textContent = says;
      if (SEPARATE_FLAGS.has(flag)) row.classList.add('text-secondary');
      reference.append(row);
    });
    const filter = () => {
      const term = search.value.trim().toLowerCase();
      const shown = $$('tr', reference).filter(row => { row.hidden = term && !row.textContent.toLowerCase().includes(term); return !row.hidden; });
      count.textContent = `${shown.length} of ${REFERENCE.length}`;
    };
    search.addEventListener('input', filter); filter();
    $('[data-flag-defaults]', el).textContent = 'Every model also gets these unless it sets its own: jinja = on, cont-batching = on, flash-attn = auto.';
  }

  // ---------- Group summaries (cards and section index) ----------
  function lines(group, name) {
    const shared = S.shared;
    if (group.id === 'context') {
      const values = vals(name);
      const context = blank(values.min_context) ? `${num(values.ctx)} tokens, fixed` : `${num(values.min_context)} – ${num(values.ctx)} tokens`;
      const fit = estimate(name);
      return [['Context', context], ['Requests', requestsText(values).replace(/^./, c => c.toUpperCase()) + (values.parallel_enabled && values.parallel_shared ? ' · shared pool' : '')],
        ['Memory', fit.capacity == null ? `CPU · about ${fit.need.toFixed(1)} GB RAM` : `${fit.need.toFixed(1)} of ${fit.capacity} GB`]];
    }
    if (group.id === 'hardware') {
      const values = vals(name);
      const where = S.engine === 'strata' ? `One GPU · ${values.gpu_card || 'any enabled'}` : values.placement === 'cpu' ? 'CPU only'
        : values.placement === 'pin' ? `Pinned to ${values.pins.join(' + ') || 'no GPU yet'}` : `Automatic GPU${values.allow_split ? ' · may split' : ''}`;
      return [['Placement', where], ['Sharing', values.alone ? 'Keeps its GPUs to itself' : 'Shares GPUs with other models']];
    }
    if (group.id === 'advanced') {
      const flags = Object.entries(vals(name).flags);
      return [['Flags', flags.length ? flags.map(([flag, value]) => `${flag} ${value || 'on'}`).join(' · ') : 'None set']];
    }
    if (group.id === 'generation') {
      const sampling = ['temp', 'top-p', 'top-k', 'min-p'].map(key => `${key} ${blank(shared[key]) ? 'default' : shared[key]}`).join(' · ');
      return [['Sampling', sampling], ['Reasoning', `${(shared.reasoning || 'auto').replace(/^./, c => c.toUpperCase())} · effort ${shared['reasoning-effort'] || 'runtime default'}`],
        ['Other', blank(shared['repeat-penalty']) ? 'None' : `repeat-penalty ${shared['repeat-penalty']}`]];
    }
    if (group.id === 'model') {
      return S.engine === 'strata' ? [['Weights', basename(shared.file)], ['Tokenizer', basename(shared.tokenizer)], ['Engine', 'Strata · list position ' + (shared.order ?? '—')]]
        : [['File', basename(shared.file)], ['Projector', basename(shared.projector)], ['Engine', `llama.cpp · list position ${shared.order ?? '—'}`]];
    }
    if (group.id === 'adapters') return [['Adapters', shared.adapters.length ? shared.adapters.map(item => `${item.name || 'unnamed'} · strength ${item.scale || 1}`).join(', ') : 'None']];
    return [];
  }
  function fillSummary(dl, group, names) {
    dl.replaceChildren();
    const all = group.scope === 'node' ? names.map(name => lines(group, name)) : [lines(group)];
    (all[0] || []).forEach(([term], index) => {
      const dt = document.createElement('dt'), dd = document.createElement('dd');
      dt.textContent = term;
      const texts = all.map(set => set[index][1]);
      if (texts.every(item => item === texts[0])) dd.textContent = texts[0];
      else { dd.classList.add('varies'); texts.forEach((item, i) => { const line = document.createElement('span'); line.textContent = `${names[i]}: ${item}`; dd.append(line); }); }
      dl.append(dt, dd);
    });
  }
  function scopeBadge(badge, group, names) {
    badge.classList.remove('bg-azure-lt', 'bg-orange-lt');
    badge.classList.add(group.scope === 'all' ? 'bg-azure-lt' : 'bg-purple-lt');
    badge.textContent = group.scope === 'all' ? `Shared · all ${plural(installed().length, 'node')}` : names.length === 1 ? `This node · ${names[0]}` : `${names.length} nodes · ${words(names)}`;
  }

  // ---------- Layouts ----------
  function showView(view) {
    S.view = view;
    $$('[data-view-panel]').forEach(panel => { panel.hidden = panel.dataset.viewPanel !== view; });
    $$('[data-view]').forEach(button => {
      const active = button.dataset.view === view;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
  }

  function renderOverview() {
    const names = S.design === 'matrix' ? installed() : scopeNames();
    const each = names.map(name => {
      const values = vals(name), fit = estimate(name);
      return {
        context: num(values.ctx),
        requests: String(S.engine === 'strata' || !values.parallel_enabled ? 1 : values.parallel_count),
        placement: S.engine === 'strata' ? 'Single GPU' : { auto: 'Automatic GPU', pin: 'Pinned GPUs', cpu: 'CPU only' }[values.placement],
        memory: `${fit.need.toFixed(1)} GB`,
        notes: {
          context: `${blank(values.min_context) ? 'Fixed' : `Minimum ${num(values.min_context)}`} · ${name}`,
          requests: `${num(perRequest(values))} tokens per request`,
          placement: lines(groups().find(group => group.id === 'hardware'), name)[0][1],
          memory: `${fit.capacity == null ? 'Host RAM' : `of ${fit.capacity} GB on ${name}`} · sample estimate`,
        },
      };
    });
    for (const key of ['context', 'requests', 'placement', 'memory']) {
      const identical = each.every(item => item[key] === each[0][key]);
      $(`[data-overview="${key}"]`).textContent = identical ? each[0][key] : 'Varies by node';
      const notes = each.map((item, i) => identical ? item.notes[key] : `${names[i]}: ${item[key]}`);
      $(`[data-overview-note="${key}"]`).textContent = [...new Set(notes)].join(' · ');
    }
    const box = $('#setup-summary-groups');
    box.replaceChildren(...groups().filter(group => ['model', 'hardware', 'generation', 'adapters'].includes(group.id)).map(group => {
      const el = clone('#tpl-glance-group');
      $('[data-title]', el).textContent = group.title;
      const summary = group.scope === 'node'
        ? names.map(name => `${names.length > 1 ? `${name}: ` : ''}${lines(group, name).map(([, value]) => value).join(' · ')}`).join('; ')
        : lines(group).map(([, value]) => value).join(' · ');
      $('[data-value]', el).textContent = group.id === 'generation'
        ? `temp ${S.shared.temp || 'default'} · top-p ${S.shared['top-p'] || 'default'} · reasoning ${S.shared.reasoning || 'auto'}`
        : group.id === 'model' ? `${S.engine === 'strata' ? 'Strata' : 'llama.cpp'} · ${basename(S.shared.file)}` : summary;
      return el;
    }));
  }

  function renderEditor() {
    const form = $('#editor'), index = $('#section-index'), names = scopeNames();
    form.replaceChildren(); index.replaceChildren();
    for (const group of groups()) {
      const isSummary = S.design === 'summary';
      const box = clone(isSummary ? '#tpl-summary' : '#tpl-section');
      box.id = `section-${group.id}`;
      box.dataset.group = group.id;
      $('.section-title', box).textContent = group.title;
      scopeBadge($('.scope-badge', box), group, names);
      const fields = $('[data-fields]', box);
      const fill = () => group.fields.filter(field => isSummary || field.type !== 'meter').forEach(field => fields.append(renderField(group, field, names, { hideHints: !isSummary })));
      if (isSummary) {
        const open = S.open === group.id, button = $('[data-edit]', box);
        fillSummary($('[data-summary]', box), group, names);
        button.textContent = open ? 'Done' : 'Edit';
        button.setAttribute('aria-expanded', String(open));
        button.setAttribute('aria-label', `${open ? 'Done editing' : 'Edit'} ${group.title}`);
        button.addEventListener('click', () => { S.open = open ? null : group.id; render(); $(`#section-${group.id} [data-edit]`)?.focus(); });
        box.classList.toggle('is-open', open);
        if (open) { fields.hidden = false; if (group.hint) { const hint = document.createElement('p'); hint.className = 'form-hint col-12 mb-0'; hint.textContent = group.hint; fields.append(hint); } fill(); }
        live.push({ refresh: () => {
          fillSummary($('[data-summary]', box), group, names);
          box.classList.toggle('is-changed', group.fields.some(field => fieldChanged(group, field, group.scope === 'all' ? names : installed())));
        } });
      } else {
        const hint = $('[data-group-hint]', box);
        if (group.hint) { hint.hidden = false; hint.textContent = group.hint; }
        fill();
        const help = $('[data-setting-help]', box), items = $('[data-help-items]', help);
        group.fields.filter(field => field.hint).forEach(field => {
          const li = document.createElement('li');
          li.textContent = `${resolve(field.label)}: ${resolve(field.hint)}`;
          items.append(li);
        });
        help.hidden = !items.children.length;
        box.hidden = S.group !== group.id;
        const link = clone('#tpl-index-link');
        link.dataset.section = group.id;
        link.setAttribute('aria-controls', box.id);
        link.setAttribute('aria-pressed', String(S.group === group.id));
        $('[data-label]', link).textContent = group.title;
        $('[data-detail]', link).textContent = {
          context: 'Window & concurrency', generation: 'Sampling & reasoning', hardware: 'Placement & sharing',
          model: 'Identity & files', adapters: 'LoRA & strengths', advanced: 'Cache & runtime flags',
        }[group.id];
        link.addEventListener('click', () => {
          S.group = group.id;
          $$('#editor > [data-group]').forEach(panel => { panel.hidden = panel.dataset.group !== S.group; });
          $$('#section-index [data-section]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.section === S.group)));
        });
        index.append(link);
        live.push({ refresh: () => { $('[data-dot]', link).hidden = !group.fields.some(field => fieldChanged(group, field, group.scope === 'all' ? names : installed())); } });
      }
      form.append(box);
    }
  }

  function renderMatrix() {
    const names = installed(), head = clone('#tpl-matrix-head'), body = $('#matrix-body');
    $('#matrix-group').replaceChildren(new Option('All settings', 'all'), ...groups().map(group => new Option(group.title, group.id)));
    $('#matrix-group').value = S.matrixGroup;
    names.forEach(name => {
      const th = clone('#tpl-matrix-node'), node = info(name), pick = $('[data-matrix-pick]', th);
      $('[data-node]', th).textContent = name;
      const badge = $('[data-state]', th);
      badge.textContent = S.nodes[name].enabled ? STATE_WORDS[node.state] : 'not offered';
      badge.classList.add(S.nodes[name].enabled ? STATE_COLORS[node.state] : 'bg-secondary-lt');
      pick.checked = S.pick.has(name);
      pick.setAttribute('aria-label', `Edit ${name} together with other ticked nodes`);
      pick.addEventListener('change', () => { pick.checked ? S.pick.add(name) : S.pick.delete(name); markPicks(); });
      th.dataset.node = name;
      locationButtons(th, name);
      head.append(th);
    });
    $('#matrix-head').replaceChildren(head);
    body.replaceChildren();
    const columns = names.length + 1;
    for (const group of groups().filter(group => S.matrixGroup === 'all' || S.matrixGroup === group.id)) {
      const row = clone('#tpl-matrix-group'), th = $('[data-colspan]', row), toggle = $('[data-toggle-group]', row), collapsed = S.collapsed.has(group.id);
      th.colSpan = columns;
      toggle.textContent = `${collapsed ? '▸' : '▾'} ${group.title}`;
      toggle.setAttribute('aria-expanded', String(!collapsed));
      toggle.dataset.focus = `group|${group.id}`;
      toggle.addEventListener('click', () => { collapsed ? S.collapsed.delete(group.id) : S.collapsed.add(group.id); rerender(toggle); });
      const badge = $('[data-scope]', row);
      badge.textContent = group.scope === 'all' ? 'Shared · one value' : 'Each node';
      badge.classList.add(group.scope === 'all' ? 'bg-azure-lt' : 'bg-purple-lt');
      body.append(row);
      if (collapsed) continue;
      // Shared groups have one value for every node: two settings to a row across the whole table.
      if (group.scope === 'all') { body.append(matrixShared(group, names, columns)); continue; }
      for (const field of group.fields) {
        if (field.type === 'flags') { matrixFlags(group, names, body); continue; }
        const line = clone('#tpl-matrix-row');
        $('[data-label]', line).textContent = resolve(field.label);
        $('[data-code]', line).remove();
        names.forEach(name => {
          const cell = clone('#tpl-matrix-cell');
          cell.dataset.node = name;
          const ok = !field.needs || field.needs(vals(name));
          if (ok || !field.hide) cell.append(renderField(group, field, [name], { compact: true, targets: () => S.pick.has(name) && S.pick.size > 1 ? [...S.pick] : [name] }));
          else cell.textContent = '—';
          line.append(cell);
        });
        const keys = keysOf(field);
        live.push({ refresh: () => {
          const values = names.map(name => JSON.stringify(keys.map(key => vals(name)[key] ?? '')));
          const counts = values.reduce((map, value) => map.set(value, (map.get(value) || 0) + 1), new Map());
          const common = [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0];
          const varied = counts.size > 1;
          $$('td', line).forEach((cell, i) => cell.classList.toggle('differs', varied && values[i] !== common));
          line.dataset.differs = String(varied);
        } });
        body.append(line);
      }
    }
    markPicks();
  }

  function matrixShared(group, names, columns) {
    const line = clone('#tpl-matrix-shared'), cell = $('[data-colspan]', line), items = $('[data-items]', line);
    cell.colSpan = columns;
    group.fields.forEach(field => {
      const item = clone('#tpl-matrix-shared-item'), code = $('[data-code]', item);
      $('[data-label]', item).textContent = field.type === 'note' ? 'Note' : resolve(field.label);
      // Generation defaults are llama.cpp flags: name the flag under its label. Other settings have no flag line.
      if (group.id === 'generation' && field.code) code.textContent = `--${field.code}`; else code.remove();
      if (['adapters', 'note'].includes(field.type)) item.classList.add('is-wide');
      $('[data-control]', item).append(renderField(group, field, names, { compact: true }));
      items.append(item);
    });
    return line;
  }

  function matrixFlags(group, names, body) {
    const flags = [...new Set(names.flatMap(name => Object.keys(vals(name).flags)))].sort();
    flags.forEach(flag => {
      const line = clone('#tpl-matrix-row');
      $('[data-label]', line).textContent = `--${flag}`;
      $('[data-code]', line).remove();
      names.forEach(name => {
        const cell = clone('#tpl-matrix-cell'), input = document.createElement('input');
        cell.dataset.node = name;
        input.className = 'form-control form-control-sm';
        input.value = vals(name).flags[flag] ?? '';
        input.placeholder = flag in vals(name).flags ? 'on' : 'not set';
        input.setAttribute('aria-label', `${flag} on ${name}`);
        input.dataset.focus = `mflag|${flag}|${name}`;
        input.addEventListener('input', () => {
          (S.pick.has(name) && S.pick.size > 1 ? [...S.pick] : [name]).forEach(item => {
            if (input.value === '') delete vals(item).flags[flag]; else vals(item).flags[flag] = input.value;
          });
          touch();
        });
        cell.append(input);
        line.append(cell);
        live.push({ refresh: () => {
          if (document.activeElement !== input) { input.value = vals(name).flags[flag] ?? ''; input.placeholder = flag in vals(name).flags ? 'on' : 'not set'; }
          cell.classList.toggle('is-changed', !same(S.original.nodes[name]?.flags[flag], vals(name).flags[flag]));
        } });
      });
      live.push({ refresh: () => {
        const values = names.map(name => vals(name).flags[flag] ?? null);
        const varied = values.some(value => value !== values[0]);
        $$('td', line).forEach((cell, i) => cell.classList.toggle('differs', varied && values[i] !== values[0] && values.filter(v => v === values[i]).length <= values.length / 2));
        line.dataset.differs = String(varied);
      } });
      body.append(line);
    });
    const line = clone('#tpl-matrix-row'), cell = clone('#tpl-matrix-cell');
    $('[data-label]', line).textContent = 'Add a flag';
    $('[data-code]', line).remove();
    cell.colSpan = names.length;
    const add = clone('#tpl-field-flags');
    const controls = $('.add-flag', add);
    controls.classList.add('matrix-add-flag');
    cell.append(controls);
    const button = $('[data-add-flag]', controls);
    button.textContent = 'Add to ticked nodes';
    button.addEventListener('click', () => {
      const flag = $('[data-new-flag]', controls).value.trim().replace(/^-+/, ''), value = $('[data-new-value]', controls).value.trim();
      if (!flag) return;
      if (SEPARATE_FLAGS.has(flag) || GENERATION_FLAGS.has(flag)) { say(`--${flag} has its own row above.`, 'warning'); return; }
      const targets = S.pick.size ? [...S.pick] : names;
      targets.forEach(name => { vals(name).flags[flag] = value; });
      rerender(null, `mflag|${flag}|${targets[0]}`);
    });
    line.dataset.shared = '';
    line.append(cell);
    body.append(line);
  }

  function markPicks() {
    const names = installed();
    $$('#matrix-panel [data-node]').forEach(cell => { if (cell.matches('th, td')) cell.classList.toggle('is-picked', S.pick.has(cell.dataset.node)); });
    $('#matrix-note').textContent = S.pick.size > 1 ? `Editing ${words(names.filter(name => S.pick.has(name)))} together: a change in one ticked column applies to all of them.`
      : 'Tick node columns to edit them together. Highlighted cells differ from the other nodes.';
  }

  // ---------- Page chrome: header, scope, locations, presets, facts ----------
  function renderHeader() {
    $('#model-header .page-title').textContent = S.shared.name;
    const box = $('#model-state');
    box.replaceChildren(...installed().map(name => {
      const chip = clone('#tpl-state-chip'), node = info(name), loaded = S.loaded[name], badge = $('[data-badge]', chip);
      $('[data-node]', chip).textContent = name;
      if (node.state !== 'ready') { badge.textContent = 'node unavailable'; badge.classList.add('bg-secondary-lt'); }
      else if (loaded) {
        badge.textContent = 'loaded'; badge.classList.add('bg-green-lt');
        $('[data-detail]', chip).textContent = ` on ${loaded.cards.join(' + ') || 'the CPU'} · ${loaded.running} answering${loaded.waiting ? ` · ${loaded.waiting} waiting` : ''}${S.nodes[name].stale ? ' · old settings until it reloads' : ''}`;
        if (S.nodes[name].stale) chip.classList.add('is-stale');
      } else { badge.textContent = 'not loaded'; badge.classList.add('bg-secondary-lt'); }
      if (!S.nodes[name].enabled) $('[data-detail]', chip).textContent += ' · not offered';
      return chip;
    }));
    const used = document.createElement('span'), file = document.createElement('span'), link = document.createElement('a'), size = document.createElement('span');
    used.innerHTML = '<span class="text-secondary">Last used</span> 3 minutes ago';
    link.href = '#facts'; link.textContent = basename(S.shared.file);
    link.addEventListener('click', event => { event.preventDefault(); showView('facts'); });
    size.className = 'text-secondary'; size.textContent = ` ${S.weights} GB`;
    file.className = 'wrap'; file.append(link, size);
    box.append(used, file);
  }

  function renderScope() {
    const box = $('#scope-nodes');
    box.replaceChildren(...installed().map(name => {
      const item = clone('#tpl-scope-node'), input = $('input', item), node = info(name);
      input.value = name; input.checked = S.scope.has(name);
      $('[data-name]', item).textContent = name;
      $('[data-state]', item).textContent = node.state === 'ready' ? (S.nodes[name].enabled ? '' : 'not offered') : STATE_WORDS[node.state];
      input.addEventListener('change', () => {
        if (input.checked) S.scope.add(name);
        else if (S.scope.size > 1) S.scope.delete(name);
        else { input.checked = true; say('Keep at least one node selected to edit its settings.', 'warning'); return; }
        render();
      });
      return item;
    }));
    const names = scopeNames(), offline = names.filter(name => info(name).state !== 'ready');
    $('#scope-note').textContent = `Node settings: ${words(names)} · shared settings: all ${plural(installed().length, 'node')} · applies on next load.${offline.length ? ` ${words(offline)} ${offline.length > 1 ? 'are' : 'is'} offline and gets them on reconnect.` : ''}`;
    $('#scope-all').disabled = names.length === installed().length;
  }

  function locationButtons(root, name) {
    const toggle = $('[data-toggle-location]', root), enabled = S.nodes[name].enabled;
    $('[data-transfer-from]', root).dataset.transferFrom = name;
    $('[data-transfer-from]', root).setAttribute('aria-label', `Copy or move from ${name}`);
    toggle.textContent = enabled ? 'Disable' : 'Enable';
    toggle.classList.toggle('btn-outline-danger', enabled);
    toggle.classList.toggle('btn-outline-success', !enabled);
    toggle.setAttribute('aria-label', `${enabled ? 'Disable' : 'Enable'} on ${name}`);
    toggle.addEventListener('click', () => {
      // Reversible actions ask twice, as data-confirm-two does on the live page.
      if (!toggle.dataset.armed) { toggle.dataset.armed = '1'; toggle.textContent = enabled ? 'Confirm disable' : 'Confirm enable'; return; }
      S.nodes[name].enabled = !enabled;
      say(`${enabled ? 'Disabled' : 'Enabled'} ${S.shared.name} on ${name}.${enabled ? ' Its settings and file stay in place.' : ''}`);
      renderAll();
    });
  }

  function renderLocations() {
    const body = $('#locations');
    body.replaceChildren(...installed().map(name => {
      const row = clone('#tpl-location-row'), node = info(name), values = vals(name), enabled = S.nodes[name].enabled;
      $('[data-node]', row).textContent = name;
      const state = $('[data-state]', row);
      state.textContent = STATE_WORDS[node.state]; state.classList.add(STATE_COLORS[node.state]);
      $('[data-local]', row).textContent = `${S.shared.name} · ${basename(S.shared.file)}`;
      const device = S.engine === 'strata' ? (values.gpu_card || 'any GPU') : values.placement === 'cpu' ? 'CPU' : values.placement === 'pin' ? values.pins.join(' + ') : 'GPU auto';
      $('[data-summary]', row).textContent = `${device} · ${num(values.ctx)} · ${S.engine === 'strata' || !values.parallel_enabled ? 1 : values.parallel_count}`;
      const badge = $('[data-enabled]', row);
      badge.textContent = enabled ? 'Enabled' : 'Disabled'; badge.classList.add(enabled ? 'text-bg-success' : 'text-bg-secondary');
      const edit = $('[data-edit-node]', row);
      edit.setAttribute('aria-label', `Edit settings on ${name}`);
      edit.disabled = S.scope.size === 1 && S.scope.has(name);
      edit.addEventListener('click', () => { S.scope = new Set([name]); render(); showView('configuration'); say(`Editing ${name} only.`); });
      locationButtons(row, name);
      return row;
    }));
  }

  function renderPresets() {
    const body = $('#presets');
    body.replaceChildren(...S.presets.map((preset, index) => {
      const row = clone('#tpl-preset-row'), remove = $('[data-delete-preset]', row);
      $('[data-name]', row).textContent = preset.name;
      $('[data-scope]', row).textContent = preset.scope;
      $('[data-context]', row).textContent = num(preset.node.ctx);
      $('[data-load-preset]', row).setAttribute('aria-label', `Load preset ${preset.name}`);
      $('[data-load-preset]', row).addEventListener('click', () => {
        const names = targetNames();
        names.forEach(name => Object.assign(vals(name), copy(preset.node)));
        Object.assign(S.shared, copy(preset.shared));
        render();
        showView('configuration');
        say(`Loaded preset “${preset.name}” for ${words(names)} as unsaved changes. Preview and save to apply it.`);
      });
      remove.setAttribute('aria-label', `Delete preset ${preset.name}`);
      remove.addEventListener('click', () => {
        if (!remove.dataset.armed) { remove.dataset.armed = '1'; remove.textContent = 'Confirm delete'; return; }
        S.presets.splice(index, 1); renderPresets(); say(`Deleted preset “${preset.name}”.`);
      });
      return row;
    }));
    if (!S.presets.length) body.append(clone('#tpl-empty-presets'));
    $('#undo').disabled = !S.history.length;
    $('#undo-note').textContent = S.history.length ? 'You can bring back the configuration from before your last saved change.' : 'No reviewed change to undo yet.';
  }

  function renderFacts() {
    const rows = installed().map(name => {
      const row = clone('#tpl-benchmark-row'), measured = S.measured && name !== 'nova';
      $('[data-node]', row).textContent = name;
      $('[data-prompt]', row).textContent = measured ? `${name === 'atlas' ? '1,850' : '1,120'} t/s` : '—';
      $('[data-generation]', row).textContent = measured ? `${name === 'atlas' ? '38.2' : '24.6'} t/s` : '—';
      $('[data-when]', row).textContent = measured ? (name === 'atlas' ? '2 days ago' : '5 days ago') : 'not measured';
      return row;
    });
    $('#benchmarks').replaceChildren(...rows);
    $('[data-fact="fingerprint"]').textContent = S.engine === 'strata' ? 'c41e09ab77d2' : '9f3a5c1e2b7d';
    $('[data-fact="sha"]').textContent = S.engine === 'strata' ? '1b7e…44d0 (sample)' : 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';
    $('[data-fact="runtime-label"]').textContent = S.engine === 'strata' ? 'Strata' : 'llama-server';
    $('[data-fact="runtime"]').textContent = S.engine === 'strata' ? '0.4.2 · qualified for 512 tokens' : 'b6123 · CUDA 12.8';
    $('#forget').hidden = !S.measured;
    $$('#remove-sure + label').forEach(label => { label.textContent = `Yes, remove ${S.shared.name} from every node`; });
  }

  // ---------- Changes, review and saving ----------
  function touch() {
    live.forEach(item => item.refresh());
    renderOverview();
    const list = changes(), nodes = [...new Set(list.map(item => item.where))];
    $('#change-count').textContent = list.length ? `${plural(list.length, 'unsaved change')} · ${words(nodes)}` : 'No unsaved changes.';
    $('#discard').disabled = !list.length;
    $('#preview').disabled = false;
    $('#save-bar').classList.toggle('has-changes', !!list.length);
    $('#matrix-panel').classList.toggle('only-differences', $('#matrix-differences').checked);
    const visibleRows = $$('#matrix-body .matrix-row').filter(row => !$('#matrix-differences').checked || (!row.hasAttribute('data-shared') && row.dataset.differs !== 'false'));
    $('#matrix-empty').hidden = visibleRows.length > 0;
  }
  function render() {
    live = [];
    const matrix = S.design === 'matrix';
    $('#matrix-panel').hidden = !matrix;
    $('.settings-layout').hidden = matrix;
    if (matrix) { $('#editor').replaceChildren(); renderMatrix(); }
    else { $('#matrix-body').replaceChildren(); renderEditor(); renderScope(); }
    renderLocations();
    showView(S.view);
    touch();
  }
  function rerender(from = null, focus = null) {
    const key = focus || from?.dataset.focus;
    render();
    if (key) $$('[data-focus]').find(item => item.dataset.focus === key)?.focus();
  }
  function renderAll() { renderHeader(); render(); renderPresets(); renderFacts(); }

  function say(message, kind = 'success') {
    const note = clone(kind === 'warning' ? '#tpl-alert-warning' : kind === 'danger' ? '#tpl-alert-danger' : '#tpl-said');
    note.textContent = message;
    note.classList.add('said', 'mb-3');
    $('#said').replaceChildren(note);
    clearTimeout(say.timer);
    say.timer = setTimeout(() => note.remove(), 7000);
  }

  function rename(name) {
    if (!name || name === S.shared.name) return;
    if (!/^[\w.:-]+$/.test(name)) { say('Names use letters, digits, dots, dashes, underscores and colons.', 'danger'); return; }
    if (name === 'gemma-3-27b') { say('Another model already has that name.', 'danger'); return; }
    S.shared.name = name; S.original.shared.name = name;
    say(`Renamed to ${name}. Clients must use the new name.`);
    renderAll();
  }

  function commandFor(name) {
    const values = vals(name);
    if (S.engine === 'strata') return `strata-server --weights ${S.shared.file} --tokenizer ${S.shared.tokenizer} --ctx-size ${values.ctx}${values.gpu_card ? ` --device ${values.gpu_card}` : ''} --port 0`;
    const parts = ['llama-server', `--model ${S.shared.file}`, `--ctx-size ${values.ctx}`];
    if (S.shared.projector) parts.push(`--mmproj ${S.shared.projector}`);
    if (values.parallel_enabled) parts.push(`--parallel ${values.parallel_count}`, values.parallel_shared ? '--kv-unified' : '--no-kv-unified');
    if (values.placement === 'cpu') parts.push('--gpu-layers 0');
    else if (values.placement === 'pin') parts.push(`--device ${values.pins.join(',')}`);
    S.shared.adapters.filter(item => item.file).forEach(item => parts.push(`--lora-scaled ${item.file} ${item.scale || 1}`));
    ['temp', 'top-p', 'top-k', 'min-p', 'reasoning', 'reasoning-effort', 'repeat-penalty'].forEach(key => { if (!blank(S.shared[key])) parts.push(`--${key} ${S.shared[key]}`); });
    Object.entries(values.flags).forEach(([flag, value]) => parts.push(`--${flag}${value ? ` ${value}` : ''}`));
    parts.push('--port 0');
    return `# ${name}\n${parts.join(' \\\n  ')}`;
  }

  function openReview() {
    const list = changes(), { errors, warnings } = problems();
    const nodes = [...new Set(list.filter(item => item.name).map(item => item.name))];
    const shown = nodes.length ? nodes : targetNames();
    $('#review-summary').textContent = list.length
      ? `${plural(list.length, 'change')}${nodes.length ? ` on ${words(nodes)}` : ''}${list.some(item => !item.name) ? `${nodes.length ? ', plus' : ' to'} shared settings for all ${plural(installed().length, 'node')}` : ''}. Node settings apply on each node's next load.`
      : 'No configuration changes. You can still save these settings as a preset.';
    const alertList = (template, items) => {
      if (!items.length) return [];
      const box = clone(template), ul = document.createElement('ul');
      ul.className = 'mb-0';
      items.forEach(text => { const li = document.createElement('li'); li.textContent = text; ul.append(li); });
      box.append(ul);
      return [box];
    };
    $('#review-errors').replaceChildren(...alertList('#tpl-alert-danger', errors));
    $('#review-warnings').replaceChildren(...alertList('#tpl-alert-warning', warnings));
    const body = $('#review-changes');
    body.replaceChildren(...list.map(item => {
      const row = clone('#tpl-change-row');
      $('[data-setting]', row).textContent = item.setting;
      $('[data-node]', row).textContent = item.where;
      $('[data-before]', row).textContent = item.before;
      $('[data-after]', row).textContent = item.after;
      return row;
    }));
    if (!list.length) body.append(clone('#tpl-empty-changes'));
    const placement = $('#review-placement');
    placement.replaceChildren();
    shown.forEach(name => {
      const dt = document.createElement('dt'), dd = document.createElement('dd'), fit = estimate(name), node = info(name);
      dt.className = 'col-md-3'; dd.className = 'col-md-9';
      dt.textContent = name;
      dd.textContent = `${fit.capacity == null ? 'CPU' : `${fit.need.toFixed(1)} of ${fit.capacity} GB on ${fit.where}`} · ${requestsText(vals(name))}`
        + (node.state !== 'ready' ? ' · not reachable now; applies when it reconnects' : S.loaded[name] ? ' · loaded now; keeps answering on old settings until it reloads' : ' · applies on next load');
      placement.append(dt, dd);
    });
    $('#review-command').textContent = shown.map(commandFor).join('\n\n');
    $('#review-acknowledge-row').hidden = !warnings.length;
    $('#review-acknowledge').checked = false;
    const blocked = () => !!errors.length || !list.length || (warnings.length && !$('#review-acknowledge').checked);
    $('#apply').disabled = blocked();
    $('#review-acknowledge').onchange = () => { $('#apply').disabled = blocked(); };
    $('#save-preset').disabled = !!errors.length;
    $('#review').showModal();
  }

  function apply() {
    const list = changes(), nodes = [...new Set(list.filter(item => item.name).map(item => item.name))];
    S.history.push(copy(S.original));
    nodes.forEach(name => { if (S.loaded[name]) S.nodes[name].stale = true; });
    S.original = snapshot();
    $('#review').close();
    say(`Saved ${plural(list.length, 'change')}${nodes.length ? ` for ${words(nodes)}` : ''}${list.some(item => !item.name) ? `${nodes.length ? ' and' : ' to'} shared settings` : ''}.${nodes.some(name => S.loaded[name]) ? ' Loaded copies use them after their next reload.' : ''}`);
    renderAll();
  }

  function savePreset() {
    const name = $('#preset-name').value.trim(), replace = $('#replace-preset').checked;
    if (!name) { $('#preset-name').focus(); return; }
    const existing = S.presets.findIndex(preset => preset.name === name);
    if (existing >= 0 && !replace) { say(`A preset named “${name}” exists. Tick Replace existing preset to overwrite it.`, 'warning'); $('#review').close(); return; }
    const source = vals(targetNames()[0] || installed()[0]);
    const node = Object.fromEntries(['ctx', 'min_context', 'parallel_enabled', 'parallel_count', 'parallel_context', 'parallel_shared'].filter(key => key in source).map(key => [key, source[key]]));
    const shared = Object.fromEntries(['temp', 'top-p', 'top-k', 'min-p', 'reasoning', 'reasoning-effort'].filter(key => key in S.shared).map(key => [key, S.shared[key]]));
    const preset = { name, scope: '', node, shared };
    if (existing >= 0) S.presets[existing] = preset; else S.presets.push(preset);
    $('#preset-name').value = '';
    $('#review').close();
    renderPresets();
    say(`Saved preset “${name}”. Every quant of this model can load it.`);
  }

  // ---------- Copy or move ----------
  const BUNDLE = () => S.weights + (S.shared.projector ? 1.3 : 0) + (S.shared.adapters || []).filter(item => item.file).length * 0.3;
  const transferState = () => ({ action: $('[name="transfer-action"]:checked').value, source: $('#transfer-source').value,
    targets: $$('[data-target-node]:checked').map(box => box.value), device: $('#transfer-device').value });

  function openTransfer(from) {
    const select = $('#transfer-source'), names = installed();
    select.replaceChildren(...names.map(name => new Option(info(name).state === 'ready' ? name : `${name} · ${STATE_WORDS[info(name).state]}`, name)));
    names.forEach((name, i) => { select.options[i].disabled = info(name).state !== 'ready'; });
    const usable = names.filter(name => info(name).state === 'ready');
    select.value = usable.includes(from) ? from : usable.includes(scopeNames()[0]) ? scopeNames()[0] : usable[0];
    $('[name="transfer-action"][value="copy"]').checked = true;
    $('#transfer-device').value = 'gpu';
    $('#transfer-device option[value="cpu"]').hidden = S.engine === 'strata';
    const box = $('#transfer-targets');
    box.replaceChildren(...S.fleet.filter(node => !S.nodes[node.name].installed).map(node => {
      const item = clone('#tpl-target-node'), input = $('input', item);
      input.value = node.name;
      input.disabled = node.state !== 'ready';
      $('[data-name]', item).textContent = node.name;
      $('[data-state]', item).textContent = node.state !== 'ready' ? STATE_WORDS[node.state] : `${node.cards.length ? '' : 'CPU only · '}${node.hasFile ? 'file present' : `${node.free} GB free`}`;
      input.addEventListener('change', () => {
        if (input.checked && transferState().action === 'move') $$('[data-target-node]:checked').forEach(other => { if (other !== input) other.checked = false; });
        updateTransfer();
      });
      return item;
    }));
    if (!box.children.length) box.textContent = 'Every node already has this model.';
    $('#transfer-progress').textContent = '';
    updateTransfer();
    $('#transfer').showModal();
  }

  function updateTransfer() {
    const { action, source, targets, device } = transferState(), plan = $('#transfer-plan'), bundle = BUNDLE();
    $$('.transfer-action label').forEach(label => label.classList.toggle('active', $('input', label).checked));
    if (action === 'move' && targets.length > 1) $$('[data-target-node]:checked').slice(1).forEach(box => { box.checked = false; });
    const chosen = transferState().targets;
    let blocked = !chosen.length || !source;
    const items = chosen.map(name => {
      const node = info(name);
      if (device === 'gpu' && !node.cards.length) { blocked = true; return [`${name} reports no GPU. Choose CPU to run it there.`, 'text-danger']; }
      if (node.hasFile) return [`${name}: the file is already in its cache, so only settings are added.`, ''];
      if (node.free < bundle) { blocked = true; return [`${name}: needs ${bundle.toFixed(1)} GB but has ${node.free} GB free.`, 'text-danger']; }
      return [`${name}: copies the ${bundle.toFixed(1)} GB bundle after hashes are checked; ${node.free} GB free, ${Math.round(node.free - bundle)} GB left.`, ''];
    });
    if (action === 'move' && chosen.length) items.push([`Then ${source} stops offering it. Requests answering there finish first; its file stays in ${source}'s cache until you clean it in Files.`, 'text-secondary']);
    plan.replaceChildren(...items.map(([text, tone]) => { const li = document.createElement('li'); li.textContent = text; if (tone) li.className = tone; return li; }));
    $('#transfer-carries').textContent = S.engine === 'strata'
      ? 'Carries context and the tokenizer. The GPU choice resets, because the cards differ.'
      : 'Carries context, parallel requests, loading flags, adapters and generation defaults. GPU pins and split settings reset, because the cards differ.';
    $('#transfer-target-hint').textContent = action === 'move' ? 'A move goes to one node.' : 'Copy to one node or several.';
    const button = $('#transfer-submit');
    button.textContent = action === 'move' ? (chosen.length ? `Move to ${chosen[0]}` : 'Move') : (chosen.length ? `Copy to ${plural(chosen.length, 'node')}` : 'Copy');
    button.disabled = blocked || !!$('#transfer').dataset.busy;
  }

  function runTransfer(event) {
    event.preventDefault();
    const { action, source, targets, device } = transferState(), progress = $('#transfer-progress');
    const needsCopy = targets.some(name => !info(name).hasFile);
    const steps = ['Checking space and hashes…', needsCopy ? `Copying ${BUNDLE().toFixed(1)} GB…` : 'Adding settings…', 'Verifying on destination…'];
    $('#transfer').dataset.busy = '1';
    $('#transfer-submit').disabled = true;
    steps.forEach((step, i) => setTimeout(() => { progress.textContent = step; }, i * 250));
    setTimeout(() => {
      targets.forEach(name => {
        const values = copy(vals(source));
        if (S.engine === 'strata') values.gpu_card = '';
        else {
          Object.assign(values, { placement: device === 'cpu' ? 'cpu' : 'auto', pins: [], allow_split: false });
          ['tensor-split', 'split-mode', 'main-gpu'].forEach(flag => delete values.flags[flag]);
        }
        Object.assign(S.nodes[name], { installed: true, enabled: true, stale: false, values });
        S.original.nodes[name] = copy(values);
        info(name).hasFile = true;
      });
      if (action === 'move') {
        S.nodes[source].installed = false;
        delete S.loaded[source];
        if (S.scope.delete(source)) targets.forEach(name => S.scope.add(name));
        S.pick.delete(source);
      }
      delete $('#transfer').dataset.busy;
      $('#transfer').close();
      say(action === 'move' ? `Moved ${S.shared.name} from ${source} to ${targets[0]}.` : `Copied ${S.shared.name} to ${words(targets)}. It is offered there now.`);
      renderAll();
    }, steps.length * 250);
  }

  // ---------- Wiring ----------
  function choose(design) {
    S.design = design;
    $('#workspace').dataset.design = design;
    $$('[data-design]').forEach(button => { if (button.matches('button')) { button.classList.toggle('active', button.dataset.design === design); button.setAttribute('aria-pressed', String(button.dataset.design === design)); } });
    $('#design-description').textContent = descriptions[design];
    history.replaceState(null, '', `#${design}`);
    S.view = 'configuration';
    render();
  }
  function reset() {
    S = sample($('#sample').value);
    S.original = snapshot();
    $('#workspace').dataset.design = S.design;
    $('#said').replaceChildren();
    renderAll();
    choose(S.design);
  }
  $$('button[data-design]').forEach(button => button.addEventListener('click', () => choose(button.dataset.design)));
  $('#scope-all').addEventListener('click', () => { S.scope = new Set(installed()); render(); });
  $('#discard').addEventListener('click', () => {
    Object.assign(S.shared, copy(S.original.shared));
    installed().forEach(name => { S.nodes[name].values = copy(S.original.nodes[name]); });
    render(); say('Discarded unsaved changes.');
  });
  $('#preview').addEventListener('click', openReview);
  $('#editor').addEventListener('submit', event => { event.preventDefault(); openReview(); });
  $('#apply').addEventListener('click', apply);
  $('#save-preset').addEventListener('click', savePreset);
  $('#undo').addEventListener('click', () => {
    const before = S.history.pop();
    Object.assign(S.shared, copy(before.shared));
    installed().forEach(name => { if (before.nodes[name]) S.nodes[name].values = copy(before.nodes[name]); });
    renderAll();
    say('Loaded the configuration from before your last change as unsaved changes. Preview and save to restore it.');
  });
  $('#open-presets').addEventListener('click', () => { showView('presets'); $('[data-view="presets"]').focus(); });
  $$('[data-view]').forEach(button => button.addEventListener('click', () => showView(button.dataset.view)));
  $('#open-transfer').addEventListener('click', () => openTransfer(scopeNames()[0]));
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-transfer-from]');
    if (button && !button.closest('dialog')) openTransfer(button.dataset.transferFrom);
    if (event.target.closest('[data-close]')) event.target.closest('dialog').close();
  });
  document.addEventListener('focusout', event => {
    const armed = event.target.closest?.('[data-armed]');
    if (armed) setTimeout(() => { if (document.activeElement !== armed && armed.isConnected) { delete armed.dataset.armed; renderLocations(); renderPresets(); } }, 150);
  });
  $('#transfer-form').addEventListener('change', updateTransfer);
  $('#transfer-form').addEventListener('submit', runTransfer);
  $('#matrix-differences').addEventListener('change', touch);
  $('#matrix-group').addEventListener('change', () => { S.matrixGroup = $('#matrix-group').value; render(); });
  $('#forget').addEventListener('click', () => { S.measured = false; renderFacts(); say('Forgot the measurements. The next load measures again.'); });
  $('#remove').addEventListener('click', () => {
    const sure = $('#remove-sure');
    if (!sure.checked) { sure.reportValidity(); return; }
    sure.checked = false;
    say(`Removed ${S.shared.name} (simulated). It stopped being offered; requests in flight finish first.`);
  });
  $('#sample').addEventListener('change', reset);
  $('#reset').addEventListener('click', reset);
  function appearance(mode) {
    const control = $('[data-theme-form] [name="mode"]');
    control.value = mode;
    control.dispatchEvent(new Event('change', { bubbles: true }));
    $('#theme').textContent = mode === 'dark' ? 'Light mode' : 'Dark mode';
  }
  $('#theme').addEventListener('click', () => appearance(document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark'));
  appearance('light');
  $('#flag-names').replaceChildren(...REFERENCE.filter(([flag]) => !SEPARATE_FLAGS.has(flag)).map(([flag]) => new Option('', flag)));
  S = { design: Object.hasOwn(descriptions, location.hash.slice(1)) ? location.hash.slice(1) : 'sheet' };
  reset();
})();
