/* Add a model (pages/models_editor.html). The nodes, file, projector and engine are checked here as they
   change, so that Add says why it cannot be used yet; turnstile checks the whole bundle again. */
/** Initialize controls after the initial document or an independently loaded section arrives. */
function initializeModelFiles() {
  'use strict';
  const picker = document.querySelector('[data-model-file]');
  if (!picker || picker.dataset.initialized) return;
  picker.dataset.initialized = "true";
  const form = picker.form;
  const nodes = JSON.parse(document.getElementById('model-file-inventory')?.textContent || '[]');
  const warning = document.querySelector('[data-file-warning]');
  const submit = document.querySelector('[data-file-submit]');
  const engine = form.querySelector('[data-engine-picker]');
  const projector = form.querySelector('[data-model-projector]');
  const name = form.querySelector('[name="name"]');
  const spare = 2 ** 30;
  /** The ticked compute nodes. */
  const targets = () => [...form.querySelectorAll('[data-model-node]:checked')].map(box => box.value);
  /** The engine's nodes, from the option the server drew. */
  const enabledOn = option => (option?.dataset.nodes || '').split(' ').filter(Boolean);
  /** The name a file gives a model: its own, without folders or extension, in lower case, as Files' Add gives it. */
  const named = path => path.split('/').pop().replace(/\.[^.]+$/, '').toLowerCase();
  /** The chosen model file and projector, each as {path, machine}. */
  function chosenFiles() {
    const files = picker.value ? [JSON.parse(picker.value)] : [];
    if (projector?.value) files.push({path: projector.value, machine: projector.selectedOptions[0]?.dataset.machine});
    return files;
  }
  /** Why the chosen files do not fit the caches they go to, or ''. A copy that is there already needs no room. */
  function room(chosen) {
    const needed = new Map();
    for (const pick of chosenFiles()) {
      const source = nodes.find(node => node.name === pick.machine);
      const file = source?.files.find(one => one.path === pick.path);
      if (!file) continue;
      const cacheName = file.place.replace(/^storage_/, '');
      const root = source.places.find(one => one.name === file.place)?.folder || '';
      const relative = file.path.slice(root.length + 1);
      // A stored file is promoted into its own machine's cache first, then copied from there.
      const machines = new Set([...(file.tier === 'storage' ? [source] : []), ...chosen.map(each => nodes.find(node => node.name === each))]);
      for (const node of machines) {
        const place = node.places.find(one => one.name === cacheName && one.tier !== 'storage');
        if (!place) return node.name + ' has no matching active cache.';
        if (node.files.some(one => one.path === place.folder + '/' + relative)) continue;
        const key = node.name + '\n' + (place.filesystem || place.folder);
        const entry = needed.get(key) || {node: node.name, free: place.free, bytes: 0};
        entry.bytes += file.size;
        needed.set(key, entry);
      }
    }
    for (const entry of needed.values()) {
      if (entry.free < entry.bytes + spare) {
        return 'Not enough cache space on ' + entry.node + '. The files need ' + (entry.bytes / 2 ** 30).toFixed(2) + ' GiB plus 1 GiB spare. No action can start.';
      }
    }
    return '';
  }
  function check() {
    const chosen = targets();
    // An engine can be chosen when it is enabled on every ticked node.
    for (const option of engine?.options || []) {
      option.disabled = option !== engine.selectedOptions[0] && !chosen.every(each => enabledOn(option).includes(each));
    }
    const lacking = engine?.value ? chosen.find(each => !enabledOn(engine.selectedOptions[0]).includes(each)) : null;
    const away = chosen.find(each => !nodes.some(node => node.name === each && node.answering));
    let why = '';
    if (!chosen.length) why = 'Choose at least one compute node.';
    else if (!picker.value) why = 'Choose a model file from cache/storage.';
    else if (away) why = away + ' is not answering.';
    else if (lacking) why = engine.selectedOptions[0].textContent + ' is not enabled on ' + lacking + '.';
    else why = room(chosen);
    if (warning) { warning.hidden = !why; warning.textContent = why; }
    if (submit) { submit.disabled = Boolean(why); submit.title = why || 'Add this model to the chosen nodes'; }
  }
  function contextDefaults() {
    const file = JSON.parse(picker.value || '{}');
    const facts = nodes.find(node => node.name === file.machine)?.files.find(row => row.path === file.path)?.facts || {};
    const trained = facts.trained_context ?? facts.context;
    const valid = Number.isSafeInteger(trained) && trained > 0;
    const classifier = Boolean(facts.decision_type);
    const maximum = document.querySelector('#ctx[data-context-default]');
    const minimum = document.querySelector('#min-context[data-context-default]');
    const note = document.querySelector('[data-trained-note]');
    if (note) note.textContent = valid ? 'Trained context: ' + trained.toLocaleString() + ' tokens.' : '';
    const context = valid ? (classifier ? Math.min(trained, 8192) : trained) : '';
    if (maximum) maximum.value = context;
    if (minimum) minimum.value = valid ? (classifier ? context : Math.max(1, Math.floor(trained / 2))) : '';
    const embedding = document.querySelector('#model-embedding');
    if (embedding) { embedding.disabled=classifier; if(classifier) embedding.checked=false; }
    const choice = document.querySelector('[data-embedding-choice]');
    if (choice) choice.hidden=classifier;
    if (classifier && note) note.textContent += ` Classifier: ${facts.decision_type}; up to ${facts.decision_max_choices || (facts.decision_type === 'openjev' ? 52 : 255)} choices. Context defaults to a bounded prompt budget.`;
  }
  /** Name the model after its file, unless a name of the person's own has been typed. */
  function nameFromFile() {
    if (!name || !picker.value) return;
    const wanted = named(JSON.parse(picker.value).path);
    if (!name.value || name.value === name.dataset.fromFile) {
      name.value = wanted;
      name.dataset.fromFile = wanted;
    }
  }
  if (name && picker.value && name.value === named(JSON.parse(picker.value).path)) name.dataset.fromFile = name.value;
  picker.addEventListener('change', () => { nameFromFile(); contextDefaults(); check(); });
  projector?.addEventListener('change', check);
  form.addEventListener('change', event => { if (event.target.matches('[data-model-node]')) check(); });
  if (!document.querySelector('#ctx')?.value) contextDefaults();
  check();
}
initializeModelFiles();
document.addEventListener('htmx:afterSwap', initializeModelFiles);
