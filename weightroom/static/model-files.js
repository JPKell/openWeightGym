/* Cache/storage choices are advisory here; the server rechecks the entire bundle. */
/** Initialize controls after the initial document or an independently loaded section arrives. */
function initializeModelFiles() {
  'use strict';
  const picker = document.querySelector('[data-model-file]');
  if (!picker || picker.dataset.initialized) return;
  picker.dataset.initialized = "true";
  const nodes = JSON.parse(document.getElementById('model-file-inventory')?.textContent || '[]');
  const warning = document.querySelector('[data-file-warning]');
  const submit = document.querySelector('[data-file-submit]');
  function check() {
    let why = '';
    if (!picker.value) why = 'Choose a model file from cache/storage.';
    else {
      const chosen = JSON.parse(picker.value);
      const source = nodes.find(node => node.name === chosen.machine);
      const file = source?.files.find(one => one.path === chosen.path);
      const target = nodes.find(node => node.name === picker.dataset.node && node.answering);
      if (!target) why = 'The selected compute node is unavailable.';
      else if (file) {
        const cacheName = file.place.replace(/^storage_/, '');
        for (const node of file.tier === 'storage' && source !== target ? [source, target] : [target]) {
          const place = node.places.find(one => one.name === cacheName && one.tier !== 'storage');
          const root = source.places.find(one => one.name === file.place)?.folder || '';
          const relative = file.path.slice(root.length + 1);
          const exists = place && node.files.some(one => one.path === place.folder + '/' + relative);
          if (!place) why = node.name + ' has no matching active cache.';
          else if (!exists && place.free < file.size + 2 ** 30) why = 'Not enough cache space on ' + node.name + '. The model needs ' + (file.size / 2 ** 30).toFixed(2) + ' GiB plus 1 GiB spare. No action can start.';
        }
      }
    }
    if (warning) { warning.hidden = !why; warning.textContent = why; }
    if (submit) { submit.disabled = Boolean(why); submit.title = why || 'Add this model to the selected node'; }
  }
  function contextDefaults() {
    const file = JSON.parse(picker.value || '{}');
    const facts = nodes.find(node => node.name === file.machine)?.files.find(row => row.path === file.path)?.facts || {};
    const trained = facts.trained_context ?? facts.context;
    const valid = Number.isSafeInteger(trained) && trained > 0;
    const maximum = document.querySelector('#ctx[data-context-default]');
    const minimum = document.querySelector('#min-context[data-context-default]');
    const note=document.querySelector('[data-trained-note]');if(note)note.textContent=valid?'Trained context: '+trained.toLocaleString()+' tokens.':'';
    if (maximum) maximum.value = valid ? trained : '';
    if (minimum) minimum.value = valid ? Math.max(1, Math.floor(trained / 2)) : '';
  }
  picker.addEventListener('change', () => { contextDefaults(); check(); });
  if (!document.querySelector('#ctx')?.value) contextDefaults();
  check();
}
initializeModelFiles();
document.addEventListener('htmx:afterSwap', initializeModelFiles);
