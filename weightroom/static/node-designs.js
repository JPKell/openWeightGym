(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const nodes = JSON.parse($('#sample-nodes').textContent);
  const defaults = Object.fromEntries(nodes.map(node => [node.name, {
    headroom: 4096, ramLimit: 0, disableOffload: false,
    llama_cppEnabled: true, llama_cppPath: '/opt/llama.cpp/bin/llama-server', llama_cppSlots: 4,
    strataEnabled: node.cards.some(card => card.vendor === 'nvidia'), strataPath: '/opt/strata/bin/strata-server', strataSlots: 2,
    cache: '/srv/models/cache', storage: '/srv/models/storage', sample: 30, retention: 30, sleep: node.name === 'orion' ? 30 : 0,
    rotation: 30, lifecycle: node.lifecycle,
    gpus: Object.fromEntries(node.cards.map(card => [card.id, {disabled: false, llama_cpp: true, strata: card.vendor === 'nvidia'}]))
  }]));
  let saved = structuredClone(defaults);
  try {
    const stored = JSON.parse(localStorage.getItem('owg-node-designs-v1') || 'null');
    if (stored) for (const node of nodes) {
      const item = stored[node.name];
      if (item && Object.keys(defaults[node.name]).every(key => key in item) &&
          Object.keys(defaults[node.name].gpus).every(key => item.gpus?.[key])) saved[node.name] = item;
    }
  } catch { /* A blocked or corrupt store starts a fresh sample session. */ }
  let drafts = structuredClone(saved), selected = 'atlas', layout = 'navigator', section = 'hardware', pendingAction = null;
  const descriptions = {
    navigator: 'Persistent node list. Related settings side by side. Best for moving quickly between machines.',
    focus: 'A fast node strip and one settings category at a time. The shortest, calmest editing workspace.',
    fleet: 'Compare the whole fleet, then configure a node directly below. Best for larger installations.'
  };
  const notes = {
    navigator: '01 / Recommended · fast navigation, visible context and compact settings without extra clicks.',
    focus: '02 / Focus workspace · use the category buttons to reveal hardware, runtime, file or security settings.',
    fleet: '03 / Fleet inspector · GPU memory is shown per device; capacities across nodes are not pooled.'
  };
  const currentNode = () => nodes.find(node => node.name === selected);
  const dirty = name => JSON.stringify(drafts[name]) !== JSON.stringify(saved[name]);
  const state = node => drafts[node.name].lifecycle === 'active' ? node.state : drafts[node.name].lifecycle;
  const labels = {ready: 'Available', sleeping: 'Asleep', disabled: 'Disabled', offline: 'Unreachable', retired: 'Retired'};
  const feedback = message => { $('#feedback').textContent = message; };

  function setStatus(element, node) {
    element.dataset.state = state(node);
    element.textContent = labels[state(node)] || 'Unknown';
  }
  function renderDirectory() {
    const query = $('#node-search').value.trim().toLowerCase(), filter = $('#node-filter').value;
    const matches = nodes.filter(node => `${node.name} ${node.role} ${node.cpu} ${node.cards.map(card => card.device).join(' ')}`.toLowerCase().includes(query) &&
      (filter === 'all' || (filter === 'available' && state(node) === 'ready') ||
       (filter === 'attention' && !['ready', 'retired'].includes(state(node))) ||
       (filter === 'retired' && state(node) === 'retired')));
    const list = document.createDocumentFragment(), rows = document.createDocumentFragment();
    for (const node of matches) {
      const choice = clone('#node-choice-template'), row = clone('#fleet-row-template');
      choice.dataset.nodeChoice = node.name;
      row.querySelector('[data-node-choice]').dataset.nodeChoice = node.name;
      for (const element of [choice, row]) {
        element.querySelector('[data-node-name]').textContent = node.name;
        element.querySelector('[data-draft-mark]').hidden = !dirty(node.name);
        const button = element.matches('button') ? element : element.querySelector('button');
        button.setAttribute('aria-pressed', String(node.name === selected));
        button.setAttribute('aria-label', `${node.name} · ${labels[state(node)]}`);
        setStatus(element.querySelector('[data-node-status]'), node);
      }
      choice.querySelector('[data-initial]').textContent = node.name[0].toUpperCase();
      choice.querySelector('[data-node-spec]').textContent = node.cards.length ? `${node.cards.length} GPU${node.cards.length > 1 ? 's' : ''} · ${node.ram} GiB RAM` : `CPU only · ${node.ram} GiB RAM`;
      row.querySelector('[data-node-role]').textContent = node.role;
      row.querySelector('[data-fleet-gpus]').textContent = node.cards.length ? `${node.cards.length} × ${node.cards[0].vram} GiB` : 'CPU only';
      row.querySelector('[data-fleet-ram]').textContent = `${node.ram} GiB`;
      row.querySelector('[data-fleet-engine]').textContent = [drafts[node.name].llama_cppEnabled && 'llama.cpp', drafts[node.name].strataEnabled && 'Strata'].filter(Boolean).join(' · ') || 'None enabled';
      row.classList.toggle('is-selected', node.name === selected);
      list.append(choice); rows.append(row);
    }
    $('#node-list').replaceChildren(list); $('#fleet-rows').replaceChildren(rows);
    $('[data-match-count]').textContent = matches.length;
    $('#no-nodes').hidden = matches.length > 0;
    $('#node-total').textContent = nodes.length;
    $('#available-total').textContent = nodes.filter(node => state(node) === 'ready').length;
    $('#attention-total').textContent = `${nodes.filter(node => !['ready', 'retired'].includes(state(node))).length} need attention`;
  }
  function renderEditor() {
    const node = currentNode(), draft = drafts[selected];
    $('#selected-name').textContent = selected;
    $('#selected-initial').textContent = selected[0].toUpperCase();
    $('#selected-role').textContent = node.role;
    $('#machine-name').value = selected;
    $('#node-spec').textContent = `${node.cpu} · ${node.ram} GiB RAM`;
    setStatus($('#selected-status'), node);
    const notice = draft.lifecycle === 'retired' ? 'Retired node. Historical configuration is visible; enable it before editing.' :
      draft.lifecycle === 'disabled' ? 'Maintenance mode. New work and wake requests are stopped; telemetry remains visible.' :
      node.state === 'sleeping' ? 'This node is asleep. Hardware is last reported; changes are saved for when it reconnects.' : '';
    $('#node-notice').hidden = !notice; $('#node-notice-text').textContent = notice;
    for (const field of $$('[data-field]')) {
      if (field.type === 'checkbox') field.checked = Boolean(draft[field.dataset.field]);
      else field.value = draft[field.dataset.field];
      field.disabled = draft.lifecycle === 'retired';
      field.removeAttribute('aria-invalid');
    }
    const rows = document.createDocumentFragment();
    for (const card of node.cards) {
      const row = clone('#gpu-row-template'), gpu = draft.gpus[card.id];
      row.dataset.gpuRow = card.id;
      row.querySelector('[data-gpu-id]').textContent = card.id;
      row.querySelector('[data-gpu-device]').textContent = card.device;
      row.querySelector('[data-gpu-models]').textContent = card.models;
      row.querySelector('[data-gpu-vram]').textContent = `${card.vram} GiB`;
      const disabled = row.querySelector('[data-gpu-disabled]');
      disabled.value = card.id; disabled.checked = gpu.disabled;
      disabled.disabled = draft.lifecycle === 'retired';
      disabled.setAttribute('aria-label', `Disable ${card.id}`);
      for (const field of row.querySelectorAll('[data-gpu-engine]')) {
        field.value = card.id; field.checked = gpu[field.dataset.gpuEngine];
        field.setAttribute('aria-label', `Allow ${card.id} for ${field.dataset.gpuEngine === 'llama_cpp' ? 'llama.cpp' : 'Strata'}`);
        field.hidden = field.dataset.gpuEngine === 'strata' && card.vendor !== 'nvidia';
        field.disabled = gpu.disabled || draft.lifecycle === 'retired' || field.hidden;
      }
      row.querySelector('[data-gpu-unsupported]').hidden = card.vendor === 'nvidia';
      rows.append(row);
    }
    if (!node.cards.length) rows.append(clone('#gpu-empty-template'));
    $('#gpu-rows').replaceChildren(rows);
    $('#lifecycle-state').textContent = draft.lifecycle;
    $('#disable-node').hidden = draft.lifecycle !== 'active';
    $('#enable-node').hidden = draft.lifecycle === 'active';
    $('#retire-node').hidden = draft.lifecycle === 'retired';
    $('[data-action=rotate]').disabled = draft.lifecycle === 'retired';
    updateSaveState();
  }
  function updateSaveState() {
    const valid = [...$$('#node-editor input')].every(field => field.disabled || field.checkValidity());
    $('#save-state').textContent = !valid ? 'Check the highlighted values' : dirty(selected) ? `Unsaved changes · ${selected}` : 'No unsaved changes';
    $('#save').textContent = `Save ${selected}`;
    $('#save').disabled = !valid || !dirty(selected) || drafts[selected].lifecycle === 'retired';
    $('#discard').disabled = !dirty(selected);
  }
  function setLayout(next) {
    layout = next;
    $('#workspace').dataset.layout = layout;
    $$('[data-design]').forEach(button => { const active = button.dataset.design === layout; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
    $('#design-description').textContent = descriptions[layout];
    $('#design-note').textContent = notes[layout];
    const url = new URL(location.href); url.searchParams.set('design', layout);
    history.replaceState(null, '', url);
  }
  function setSection(next) {
    section = next; $('#workspace').dataset.section = section;
    $$('button[data-section]').forEach(button => { const active = button.dataset.section === section; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
    $$('[data-panel]').forEach(panel => panel.classList.toggle('is-active', panel.dataset.panel === section));
  }
  function togglePairing(open) {
    $('#pairing-panel').hidden = !open;
    $('#add-node').setAttribute('aria-expanded', String(open));
    (open ? $('#copy-command') : $('#add-node')).focus();
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-node-choice], [data-design], button[data-section], [data-action]');
    if (!button) return;
    if (button.hasAttribute('data-node-choice')) {
      const name = button.dataset.nodeChoice;
      if (name === selected) return;
      selected = name; renderDirectory(); renderEditor(); feedback('');
      const replacement = $$(layout === 'fleet' ? '#fleet-rows [data-node-choice]' : '#node-list [data-node-choice]').find(element => element.dataset.nodeChoice === name);
      replacement?.focus({preventScroll: true});
    } else if (button.dataset.design) setLayout(button.dataset.design);
    else if (button.dataset.section) setSection(button.dataset.section);
    else if (button.dataset.action) {
      pendingAction = {name: selected, action: button.dataset.action};
      const messages = {disable: 'Disable this node? New work and wake requests stop. Telemetry remains visible and existing work may finish.',
        retire: 'Retire this node? New work, wake requests and monitoring stop. Historical logs are preserved.',
        enable: 'Enable this node? It becomes eligible for new work and monitoring resumes.',
        rotate: 'Rotate the machine key now? The replacement is sent to the node first. The old key stays valid for ten minutes.'};
      $('#action-title').textContent = `${button.dataset.action === 'rotate' ? 'Rotate key' : button.dataset.action[0].toUpperCase() + button.dataset.action.slice(1) + ' node'} · ${selected}`;
      $('#action-impact').textContent = messages[button.dataset.action];
      $('#action-dialog').returnValue = '';
      $('#action-dialog').showModal();
    }
  });
  $('#node-editor').addEventListener('input', event => {
    const field = event.target;
    if (field.dataset.field) drafts[selected][field.dataset.field] = field.type === 'checkbox' ? field.checked : field.value;
    else if (field.closest('[data-gpu-row]')) {
      const row = field.closest('[data-gpu-row]'), gpu = drafts[selected].gpus[row.dataset.gpuRow];
      gpu[field.hasAttribute('data-gpu-disabled') ? 'disabled' : field.dataset.gpuEngine] = field.checked;
      row.querySelectorAll('[data-gpu-engine]').forEach(control => { control.disabled = gpu.disabled || control.hidden; });
    }
    if (field.willValidate) field.setAttribute('aria-invalid', String(!field.checkValidity()));
    updateSaveState();
    // Keep keyboard focus stable while an editor field is being changed.
    renderDirectory();
  });
  $('#node-editor').addEventListener('submit', event => {
    event.preventDefault();
    if (!$('#node-editor').checkValidity()) { updateSaveState(); return; }
    const next = {...saved, [selected]: structuredClone(drafts[selected])};
    try { localStorage.setItem('owg-node-designs-v1', JSON.stringify(next)); }
    catch { feedback('This browser could not save. Your draft is still here.'); return; }
    saved = structuredClone(next);
    renderDirectory(); updateSaveState();
    feedback(`Saved ${selected} in this browser. Runtime paths and file locations would require a compute restart.`);
  });
  $('#discard').addEventListener('click', () => { drafts[selected] = structuredClone(saved[selected]); renderEditor(); renderDirectory(); feedback(`Discarded changes for ${selected}.`); });
  $('#action-dialog').addEventListener('close', () => {
    if ($('#action-dialog').returnValue !== 'confirm' || !pendingAction) { pendingAction = null; return; }
    const {name, action} = pendingAction;
    if (action === 'rotate') feedback(`Sample key rotation requested for ${name}. No real key was changed.`);
    else {
      const lifecycle = {disable: 'disabled', enable: 'active', retire: 'retired'}[action];
      const next = structuredClone(saved); next[name].lifecycle = lifecycle;
      try { localStorage.setItem('owg-node-designs-v1', JSON.stringify(next)); }
      catch { feedback('This browser could not save the lifecycle change.'); pendingAction = null; return; }
      saved[name].lifecycle = lifecycle; drafts[name].lifecycle = lifecycle;
      renderDirectory(); renderEditor(); feedback(`${name} is ${lifecycle} in this sample.`);
    }
    pendingAction = null;
  });
  $('#node-search').addEventListener('input', renderDirectory);
  $('#node-filter').addEventListener('change', renderDirectory);
  $('#add-node').addEventListener('click', () => togglePairing($('#pairing-panel').hidden));
  $('#close-pairing').addEventListener('click', () => togglePairing(false));
  document.addEventListener('keydown', event => { if (event.key === 'Escape' && !$('#pairing-panel').hidden && !$('#action-dialog').open) togglePairing(false); });
  $('#copy-command').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText($('#pairing-command').textContent); feedback('Demo command copied. It cannot pair a real machine.'); }
    catch { feedback('Clipboard unavailable. Select and copy the demo command above.'); }
  });
  $('#theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark';
    document.documentElement.dataset.bsTheme = dark ? 'dark' : 'light';
    $('#theme').textContent = dark ? 'Light mode' : 'Dark mode';
  });
  $('#reset').addEventListener('click', () => {
    try { localStorage.removeItem('owg-node-designs-v1'); } catch { /* Session reset still works. */ }
    saved = structuredClone(defaults); drafts = structuredClone(defaults); selected = 'atlas';
    $('#node-search').value = ''; $('#node-filter').value = 'all';
    renderDirectory(); renderEditor(); feedback('Sample nodes reset.');
  });
  const requested = new URL(location.href).searchParams.get('design');
  setLayout(Object.hasOwn(descriptions, requested) ? requested : 'navigator');
  setSection('hardware'); renderDirectory(); renderEditor();
})();
