// Compact overview of the real model form. Hidden editors retain all successful controls.
(() => {
  'use strict';
  if (window.suiteModelConfiguration) return;
  window.suiteModelConfiguration = true;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const number = value => Number.isFinite(Number(value)) && value !== '' ? Number(value).toLocaleString() : value || 'Not set';
  const basename = path => path.split('/').pop() || 'Not set';
  const states = new WeakMap();
  const page = () => $('[data-model-configuration]');
  const formOf = root => $('[data-model-settings]', root);
  const fields = (form, name) => [...form.elements].filter(field => field.name === name);
  const value = (form, name) => fields(form, name)[0]?.value || '';
  const checked = (form, name) => fields(form, name).some(field => field.checked);
  const selected = root => $$('[data-model-node]:checked', root);
  const digest = form => JSON.stringify([...new FormData(form)].filter(([key]) => !['preset', 'replace'].includes(key)));

  function flags(form, prefix) {
    const keys = fields(form, prefix + 'flag'), values = fields(form, prefix + 'value');
    const result = {};
    keys.forEach((key, index) => { if (key.value.trim() && values[index]?.value.trim()) result[key.value.trim().replace(/^-+/, '')] = values[index].value.trim(); });
    fields(form, prefix + 'suppress_flag').filter(field => field.checked).forEach(field => { result[field.value] = null; });
    return result;
  }
  function nodeSetup(root, node) {
    const form = formOf(root), prefix = node.dataset.prefix;
    const defaults = JSON.parse(node.dataset.defaults), own = flags(form, prefix);
    const parallel = checked(form, prefix + 'parallel_enabled'), total = Number(value(form, prefix + 'ctx'));
    const count = root.dataset.engine === 'strata' || !parallel ? 1 : Number(value(form, prefix + 'parallel_count'));
    const shared = checked(form, prefix + 'parallel_shared'), cap = Number(value(form, prefix + 'parallel_context'));
    const limit = Math.max(0, Math.min(total, cap > 0 && parallel ? cap : total, parallel && !shared && count > 0 ? Math.floor(total / count) : total));
    const mode = fields(form, prefix + 'cpu').some(field => field.checked) ? 'CPU'
      : fields(form, prefix + 'gpu_card').some(field => field.value && (field.type === 'select-one' || field.checked)) ? 'Pinned GPUs' : 'Automatic GPU';
    const pins = fields(form, prefix + 'gpu_card').filter(field => field.value && (field.type === 'select-one' || field.checked)).map(field => field.value);
    const fixed = fields(form, prefix + 'keep_gpu_count').find(field => field.checked);
    const inherited = checked(form, prefix + 'keep_default_gpus');
    const placement = root.dataset.engine === 'strata' ? (pins.length ? `GPU ${pins[0]}` : 'Any single GPU') : mode === 'Pinned GPUs' ? pins.join(' + ')
      : mode === 'CPU' ? 'CPU' : fixed ? `${fixed.value} GPU${fixed.value === '1' ? '' : 's'} · fixed count`
      : inherited ? `${JSON.parse(node.dataset.cards).length} GPUs · node default` : mode;
    const minimum = value(form, prefix + 'min_context');
    return {node: node.value, prefix, total, count, limit, defaults, own, placement,
      context: `${minimum ? `${number(minimum)}–` : ''}${number(String(total))} tokens${minimum ? '' : ' · fixed'}`,
      requests: count === -1 ? 'Server auto · concurrency determined at load' : `${number(String(count))} at once · up to ${number(String(limit))} tokens each`,
      sharing: checked(form, prefix + 'alone') ? 'Exclusive GPUs' : 'Shares GPUs',
      split: mode === 'CPU' ? 'CPU only' : pins.length ? 'Pinned placement' : fixed || inherited ? 'Fixed device count' : checked(form, prefix + 'allow_split') ? 'May split across GPUs' : 'Single GPU preferred',
    };
  }
  function sharedFlags(root) { return flags(formOf(root), ''); }
  function explainFlag(root, nodes, flag) {
    const own = sharedFlags(root);
    if (flag in own) return own[flag] == null ? 'Inherited default ignored' : own[flag];
    const inherited = nodes.map(node => node.defaults[flag]);
    if (inherited.every(item => item === undefined)) return 'Runtime default';
    if (inherited.every(item => JSON.stringify(item) === JSON.stringify(inherited[0]))) return `${inherited[0] == null ? 'Runtime default' : JSON.stringify(inherited[0])} · inherited`;
    return 'Varies by node · inherited';
  }
  function summarize(root) {
    const form = formOf(root), nodes = selected(root).map(node => nodeSetup(root, node));
    if (!nodes.length) return;
    const perNode = key => nodes.every(node => node[key] === nodes[0][key]) ? nodes[0][key] : nodes.map(node => `${node.node}: ${node[key]}`).join('; ');
    const ownShared = sharedFlags(root);
    const globalFlags = new Set(JSON.parse(root.dataset.globalFlags));
    const adapterNames = fields(form, 'adapter').map((field, index) => {
      const file = fields(form, 'adapter_file')[index]?.value;
      return field.value && file ? `${field.value} · strength ${fields(form, 'adapter_scale')[index]?.value || '1'}` : '';
    }).filter(Boolean);
    const summaries = {
      context: [['Context', perNode('context')], ['Requests', perNode('requests')], ['Pool', nodes.map(node => `${node.node}: ${checked(form, node.prefix + 'parallel_shared') && checked(form, node.prefix + 'parallel_enabled') ? 'shared' : 'fixed'}`).join(' · ')]],
      generation: [['Sampling', `Temperature ${explainFlag(root, nodes, 'temp')} · top-p ${explainFlag(root, nodes, 'top-p')}`], ['Reasoning', explainFlag(root, nodes, 'reasoning')], ['Defaults', `${Object.keys(ownShared).filter(key => globalFlags.has(key)).length} explicit flags · all copies`]],
      hardware: [['Placement', perNode('placement')], ['Sharing', perNode('sharing')], ['Splitting', root.dataset.engine === 'strata' ? 'Single GPU only' : perNode('split')]],
      model: [['File', selected(root).map(node => `${selected(root).length > 1 ? `${node.value}: ` : ''}${basename(value(form, node.dataset.prefix + 'file'))}`).join('; ')], ['Engine', root.dataset.engine === 'strata' ? 'Strata' : 'llama.cpp'], [root.dataset.engine === 'strata' ? 'Tokenizer' : 'Projector', root.dataset.engine === 'strata' ? selected(root).map(node => basename(value(form, node.dataset.prefix + 'tokenizer'))).join(' · ') : basename(value(form, 'projector'))]],
      adapters: [['Adapters', adapterNames.length ? adapterNames.join('; ') : 'No adapters'], ['Scope', 'Shared across every copy']],
      advanced: [['Flags', nodes.map(node => `${node.node}: ${Object.keys(node.own).filter(flag => !globalFlags.has(flag)).length} explicit flags`).join(' · ')], ['Defaults', 'Inherited values and the installed runtime’s complete flag reference are inside.']],
    };
    if (root.dataset.engine === 'strata') summaries.context.pop();
    $$('[data-model-group]', root).forEach(group => {
      const rows = summaries[group.dataset.modelGroup] || [];
      $('[data-model-summary]', group).replaceChildren(...rows.map(([term, text]) => {
        const line = $('#model-summary-line').content.firstElementChild.cloneNode(true);
        $('[data-term]', line).textContent = term;
        $('[data-value]', line).textContent = text;
        return line;
      }));
      const badge = $('[data-model-scope]', group);
      badge.textContent = group.dataset.modelGroup === 'model' ? 'Shared identity · node-local files' : group.dataset.shared === 'true' ? 'Shared · all nodes' : selected(root).map(node => node.value).join(' · ');
    });
    for (const [key, property] of [['context', 'total'], ['requests', 'count'], ['placement', 'placement']]) {
      const same = nodes.every(node => node[property] === nodes[0][property]);
      const label = same ? (key === 'placement' ? nodes[0][property] : nodes[0][property] === -1 ? 'Server auto' : number(String(nodes[0][property]))) : 'Varies by node';
      $(`[data-model-kpi="${key}"]`, root).textContent = label;
      $(`[data-model-kpi-note="${key}"]`, root).textContent = key === 'requests' ? perNode('requests') : key === 'context' ? perNode('context') : perNode('sharing');
    }
    const changed = root.dataset.modelDraft === 'true' || digest(form) !== states.get(root).baseline;
    $('[data-model-dirty]', root).textContent = root.dataset.failed === 'true' ? 'Save failed. Your draft is kept; review the error above.' : changed ? 'Unsaved changes · applies on next load' : 'No unsaved changes.';
    $('[data-model-discard]', root).disabled = !changed;
    $('.model-config-save', root).classList.toggle('has-changes', changed);
    for (const group of $$('[data-model-group]', root)) {
      const saved = states.get(root).groups.get(group.dataset.modelGroup);
      group.classList.toggle('is-changed', saved !== groupDigest(group));
      const table = $('[data-model-matrix]', group);
      const different = $$('[data-matrix-row]', table).some(row => row.dataset.differs === 'true');
      $('[data-model-no-differences]', group).hidden = !table.classList.contains('only-differences') || different;
    }
  }
  function groupDigest(group) { return JSON.stringify($$('input,select,textarea', group).filter(field => field.name && field.form === formOf(page())).map(field => [field.name, field.value, field.checked, field.disabled])); }
  function view(root, name) {
    $$('[data-model-panel]', root).forEach(panel => { panel.hidden = panel.dataset.modelPanel !== name; });
    $$('[data-model-view]', root).forEach(button => {
      const active = button.dataset.modelView === name;
      button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    });
    states.get(root).view = name;
  }
  function openGroup(root, key) {
    $$('[data-model-group]', root).forEach(group => {
      const open = group.dataset.modelGroup === key;
      group.classList.toggle('is-open', open);
      $('[data-model-group-editor]', group).hidden = !open;
      $('[data-model-summary]', group).hidden = open;
      const button = $('[data-model-edit]', group);
      button.textContent = open ? 'Done' : 'Edit';
      button.setAttribute('aria-expanded', String(open));
      button.setAttribute('aria-label', `${open ? 'Done editing' : 'Edit'} ${$('.card-heading', group).textContent}`);
    });
  }
  function scope(root) {
    const names = new Set(selected(root).map(node => node.value));
    $$('[data-model-matrix]', root).forEach(table => {
      table.classList.toggle('is-single', names.size === 1);
      $$('th[data-node],td[data-node]', table).forEach(cell => { cell.hidden = !names.has(cell.dataset.node); });
      $$('[data-matrix-pick]', table).forEach(box => { box.checked = names.has(box.value); });
    });
    $('[data-model-scope-note]', root).textContent = `Node settings: ${[...names].join(', ')} · shared settings: all copies · applies on next load.${names.size > 1 ? ' Edits apply to all selected nodes.' : ''}`;
    summarize(root);
  }
  function setup() {
    const root = page();
    if (!root || states.has(root)) return;
    const form = formOf(root);
    states.set(root, {baseline: digest(form), groups: new Map(), view: 'configuration', submitting: false});
    $$('[data-model-group]', root).forEach(group => states.get(root).groups.set(group.dataset.modelGroup, groupDigest(group)));
    root.classList.add('model-configuration-enhanced');
    view(root, 'configuration'); openGroup(root, null); scope(root);
    // A rejected save must keep its fields discoverable, even before a native validation event.
    if (root.dataset.modelDraft === 'true') openGroup(root, 'context');
    const notice = $('.said-box')?.textContent.trim() || '';
    if (/^(Saved the preset|Preset deleted)/.test(notice)) view(root, 'presets');
  }
  document.addEventListener('click', event => {
    const root = page();
    if (!root || (!root.contains(event.target) && !event.target.closest('[data-model-show]'))) return;
    const tab = event.target.closest('[data-model-view]'), action = event.target.closest('[data-model-show]');
    if (tab || action) { event.preventDefault(); view(root, (tab || action).dataset[tab ? 'modelView' : 'modelShow']); }
    if (event.target.closest('[data-add-generation]')) {
      const select = $('#add-generation-flag', root), flag = select.value;
      if (!flag) return;
      const form = formOf(root), group = $('#model-group-generation', root);
      const existing = fields(form, 'flag').find(input => input.closest('#model-group-generation') && input.value === flag);
      if (existing) { existing.parentElement.querySelector('[name="value"]')?.focus(); return; }
      const item = $('#model-generation-item', root).content.firstElementChild.cloneNode(true);
      item.dataset.modelAdded = '';
      $('.matrix-item-label', item).textContent = flag.replaceAll('-', ' ');
      $('[name="flag"]', item).value = flag;
      const input = $('[name="value"]', item); input.setAttribute('aria-label', `value of ${flag}`);
      $('[name="suppress_flag"]', item).value = flag;
      $('.form-check-label', item).textContent = `Ignore inherited default for ${flag}`;
      $('[data-generation-add]', group).before(item);
      select.value = ''; input.focus(); summarize(root);
    }
    const edit = event.target.closest('[data-model-edit]');
    if (edit) { const group = edit.closest('[data-model-group]'); openGroup(root, group.classList.contains('is-open') ? null : group.dataset.modelGroup); edit.focus(); }
    if (event.target.closest('[data-model-discard]')) {
      const form = formOf(root);
      if (root.dataset.modelDraft === 'true') { states.get(root).submitting = true; location.assign(form.action); return; }
      $$('[data-model-added]', form).forEach(row => row.remove()); form.reset();
      form.dispatchEvent(new Event('change', {bubbles: true}));
      root.dataset.failed = 'false'; summarize(root);
    }
    if (event.target.closest('[data-add-row]')) queueMicrotask(() => { scope(root); summarize(root); });
  });
  const update = event => {
    const root = page();
    if (!root || !states.has(root) || !root.contains(event.target)) return;
    $$('[data-suppress-follow]', root).forEach(box => {
      const row = box.closest('.matrix-shared-item, .matrix-cell');
      box.value = row?.querySelector('input[name$="flag"]:not([type="checkbox"])')?.value.trim().replace(/^-+/, '') || '';
    });
    if (event.target.matches('[data-model-node]')) {
      if (!selected(root).length) event.target.checked = true;
      scope(root);
    } else summarize(root);
  };
  document.addEventListener('input', update);
  document.addEventListener('change', update);
  document.addEventListener('reset', () => queueMicrotask(() => { const root = page(); if (root && states.has(root)) scope(root); }));
  document.addEventListener('invalid', event => {
    const root = page(), group = event.target.closest('[data-model-group]');
    if (!root || !group) return;
    const differences = $('[data-matrix-differences]', root);
    if (differences?.checked) { differences.checked = false; differences.dispatchEvent(new Event('change', {bubbles: true})); }
    view(root, 'configuration'); openGroup(root, group.dataset.modelGroup);
    const cell = event.target.closest('[data-node]');
    if (cell) { $$('[data-model-node]', root).forEach(node => { if (node.value === cell.dataset.node) node.checked = true; }); scope(root); }
  }, true);
  document.addEventListener('submit', event => { const root = page(); if (root && event.target === formOf(root)) states.get(root).submitting = true; });
  window.addEventListener('beforeunload', event => {
    const root = page();
    if (root && states.has(root) && !states.get(root).submitting && (root.dataset.modelDraft === 'true' || digest(formOf(root)) !== states.get(root).baseline)) { event.preventDefault(); event.returnValue = ''; }
  });
  document.addEventListener('htmx:afterSwap', setup);
  setup();
})();
