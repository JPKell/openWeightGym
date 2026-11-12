(() => {
  'use strict';
  const $ = id => document.getElementById('mw-' + id);
  const node = new URLSearchParams(location.search).get('node') || 'compute';
  const query = node ? '?node=' + encodeURIComponent(node) : '';
  const SLOTS = ['diffusion', 'text_encoder', 'vae', 'clip_l', 'clip_g', 'upscaler'];
  const FAMILIES = SuiteImageProfiles.labels;
  const modelDrafts = new Map();
  const dialog = document.getElementById('model-dialog');
  let nodeCards = [], models = [], selected = null, checking = 0, timer = null, initialized = false;
  const message = error => { $('message').hidden = !error; $('message').textContent = error || ''; };
  const element = (tag, text, className) => { const one = document.createElement(tag); if (text !== undefined) one.textContent = text; if (className) one.className = className; return one; };
  async function api(body, path = '') {
    const response = await fetch('/mirrorwall-models' + path + query, {method: body ? 'POST' : 'GET', cache: 'no-store', headers: body ? {'content-type':'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
    if (response.redirected) { location.href = '/sign-in'; throw new Error('Please sign in again.'); }
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || 'Image model management failed.');
    return value;
  }
  /** The profile the form describes; `strict` refuses unreadable JSON, otherwise it is left out so the files can still be checked. */
  function draft(strict = true) {
    const parsed = (name, fallback) => { try { return ['defaults','limits'].includes(name) ? SuiteModelFields.read('mw-' + name) : JSON.parse($(name).value); } catch (error) { if (strict) throw new Error('The ' + name + ' field is not valid JSON.'); return fallback; } };
    const profile = {...selected?.sdcpp, family:$('family').value, split_mode:$('split').value, components:{},
      host_ram_mib:Number($('ram').value), reference_megapixels:Number($('ref-mp').value), threads:Number($('threads').value), staged:$('staged').checked,
      defaults:parsed('defaults', {}), limits:parsed('limits', {}), loras:parsed('loras', {})};
    ['text_encoder','vae','clip_l','clip_g'].forEach(name => { if ($(name).value.trim()) profile.components[name] = $(name).value.trim(); });
    if ($('upscaler').value.trim()) profile.upscaler = $('upscaler').value.trim(); else delete profile.upscaler;
    if ($('vram').value.trim()) profile.vram_limit_gib = $('vram').value.split(',').map(Number); else delete profile.vram_limit_gib;
    if ($('layer-weights').value.trim()) profile.layer_weights = $('layer-weights').value.split(',').map(Number); else delete profile.layer_weights;
    const gpus = SuiteHardware.read('mw', profile.split_mode === 'single' ? 1 : 2);
    if (gpus === 0) profile.split_mode = 'single';
    return {engine:'sdcpp', flags:{model:$('diffusion').value.trim(),'ctx-size':0}, sdcpp:profile, gpus, alone:$('alone').checked};
  }
  function familyDefaults() {
    return SuiteImageProfiles.spec($('family').value).defaults;
  }
  function setFamily(family) {
    $('family').value = family;
    if (!selected) SuiteModelFields.load('mw-defaults', familyDefaults());
    if (!selected && family === 'sdxl') SuiteModelFields.load('mw-limits', {...SuiteModelFields.read('mw-limits'), max_references: 1});
    if (!selected && family.startsWith('qwen-image')) SuiteModelFields.load('mw-limits', {...SuiteModelFields.read('mw-limits'), max_references:0});
    familyHelp();
    remember();
    recheck(0);
  }
  function familyHelp() {
    SuiteImageProfiles.help($('family-help'),$('family').value,values=>{
      SuiteModelFields.load('mw-defaults',values); remember(); recheck(0);
    });
  }
  /** Show, for each part, whether it is right for these diffusion weights, and what to use or get instead. */
  function render(found) {
    const box = $('check'); box.replaceChildren();
    const ready = found.ready;
    const head = element('div', undefined, 'mw-check-head ' + (ready ? 'is-ready' : 'is-missing'));
    const missing = found.slots.filter(one => ['missing', 'wrong'].includes(one.status) || (one.status === 'unused' && one.found)).length + found.loras.filter(one => one.status !== 'ok').length;
    head.append(element('strong', ready ? '✓ Everything this model needs is here' : !$('diffusion').value.trim() ? 'Choose the diffusion weights to see what this model needs' : '✗ ' + missing + ' part' + (missing === 1 ? '' : 's') + ' to fix before it can run'));
    if (found.variant_label) head.append(element('span', 'Detected: ' + found.variant_label, 'badge text-bg-secondary ms-2'));
    box.append(head);
    if (found.detected_family && !found.family_matches && !selected) { setFamily(found.detected_family); return; }  // a new model takes the family its weights say
    if (found.detected_family && !found.family_matches) {
      const wrong = $('family-warning').content.firstElementChild.cloneNode(true);
      wrong.querySelector('[data-message]').textContent = 'These weights are ' + FAMILIES[found.detected_family] + ', but the family above says ' + FAMILIES[$('family').value] + '.';
      const fix = wrong.querySelector('button'); fix.textContent = 'Use ' + FAMILIES[found.detected_family];
      fix.onclick = () => setFamily(found.detected_family); box.append(wrong);
    }
    for (const slot of found.slots) {
      const input = $(slot.slot), says = $('says-' + slot.slot), need = $('need-' + slot.slot);
      input.classList.toggle('is-valid', slot.status === 'ok');
      input.classList.toggle('is-invalid', slot.status === 'wrong' || slot.status === 'missing' || (slot.status === 'unused' && Boolean(slot.found)));
      $('row-' + slot.slot).hidden = slot.need === 'unused' && !input.value.trim();
      need.textContent = {required: 'Required', optional: 'Optional', unused: 'Not used'}[slot.need];
      need.className = 'badge rounded-pill fw-normal ' + (slot.need === 'required' ? 'text-bg-primary' : 'text-bg-light border');
      says.replaceChildren();
      const icon = {ok: '✓', missing: '✗', wrong: '✗', optional: '○', unused: '!'}[slot.status];
      const line = element('span', icon + ' ' + slot.message, 'mw-says-' + slot.status);
      says.append(line);
      if (slot.why && slot.status !== 'ok') says.append(element('span', ' ' + slot.why, 'text-secondary'));
      const actions = element('div', undefined, 'd-flex flex-wrap gap-1 mt-1');
      for (const path of slot.candidates || []) {
        const use = element('button', 'Use ' + path.split('/').pop(), 'btn btn-outline-success btn-sm'); use.type = 'button'; use.title = path;
        use.onclick = () => { input.value = path; recheck(0); }; actions.append(use);
      }
      if (slot.get) {
        const get = element('a', 'Get ' + slot.get.file.split('/').pop() + ' from ' + slot.get.repository, 'btn btn-outline-secondary btn-sm');
        get.href = '/hub?repository=' + encodeURIComponent(slot.get.repository); actions.append(get);
      }
      if (actions.children.length) says.append(actions);
    }
    if (!$('diffusion').value.trim() || found.slots[0].status === 'wrong') {
      const list = element('p', 'Supported diffusion weights: ', 'small mb-1');
      found.get_diffusion.forEach((one, index) => {
        const link = element('a', one.label); link.href = '/hub?repository=' + encodeURIComponent(one.repository); link.title = one.repository + ' · ' + one.file;
        list.append(index ? ' · ' : '', link);
      });
      box.append(list);
    }
    for (const lora of found.loras) box.append(element('div', (lora.status === 'ok' ? '✓ ' : '✗ ') + 'LoRA ' + lora.name + ': ' + lora.message, 'small mw-says-' + (lora.status === 'ok' ? 'ok' : 'wrong')));
    if (!ready && $('diffusion').value.trim()) box.append(element('p', 'Downloaded files arrive in Files on front; copy them to ' + node + ' there, then use them here.', 'mw-model-note mb-0 mt-2'));
  }
  async function check(mine) {
    try {
      const found = await api({settings: draft(false)}, '/check');
    if (mine === checking) render(found);
    } catch (error) { if (mine === checking) $('check').replaceChildren(element('p', 'The files could not be checked: ' + error.message, 'text-secondary small')); }
  }
  function recheck(wait = 450) { clearTimeout(timer); const mine=++checking; timer = setTimeout(() => check(mine), wait); }
  function remember() {
    if (!dialog?.open) return;
    const controls=[...$('model-form').querySelectorAll('input,select,textarea')].filter(input=>input.id || input.name).map(input=>({id:input.id,name:input.name,value:input.value,checked:input.checked}));
    const loras=[...$('lora-rows').querySelectorAll('tr:not([data-empty-row])')].map(row=>Object.fromEntries([...row.querySelectorAll('[data-lora-field]')].map(input=>[input.dataset.loraField,input.value])));
    modelDrafts.set(selected?.name || '',{controls,loras});
  }
  function fill(model) {
    selected = model; const profile = model?.sdcpp || {};
    $('install-targets').hidden=Boolean(model); $('target-note').hidden=Boolean(model);
    $('model-form').querySelector('section[aria-label="Hardware"] .scope-badge').textContent=model?'This node':'Selected nodes';
    $('heading').textContent = model ? model.name : 'Add image model';
    const choose = (name, value) => { if (value && !Array.from($(name).options).some(option => option.value === value)) $(name).add(new Option(value + ' · current selection', value)); $(name).value = value || ''; };
    $('name').value = model?.name || ''; $('name').readOnly = Boolean(model); choose('diffusion', model?.flags?.model);
    for (const name of ['text_encoder','vae','clip_l','clip_g']) choose(name, profile.components?.[name]);
    $('family').value = profile.family || 'flux2-klein'; $('split').value = profile.split_mode || 'single';
    SuiteHardware.set('mw', model?.gpus ?? 1, nodeCards);
    document.querySelector('[data-image-split]').hidden = model?.gpus === 0;
    $('ram').value = profile.host_ram_mib || 8192; $('vram').value = profile.vram_limit_gib?.join(',') || '';
    $('layer-weights').value = profile.layer_weights?.join(',') || ''; $('alone').checked = model?.alone || false;
    $('ref-mp').value = profile.reference_megapixels || 1; $('threads').value = profile.threads || 4;
    choose('upscaler', profile.upscaler); $('staged').checked = profile.staged !== false;
    SLOTS.forEach(name => Array.from($(name).options).forEach(option => {
      option.disabled = Boolean(model && option.value && option.value !== $(name).value &&
        (option.dataset.machine !== node || option.dataset.tier === 'storage'));
    }));
    SuiteModelFields.load('mw-defaults', profile.defaults || familyDefaults());
    SuiteModelFields.load('mw-limits', profile.limits || {max_width:1024,max_height:1024,max_pixels:1048576,max_steps:50,max_images:8,max_references:2});
    $('loras').value = JSON.stringify(profile.loras || {}, null, 2); renderLoras(); $('saved').textContent = '';
    $('measurement').textContent = model?.measured ? 'Reserved per GPU: ' + model.measured.mib.join(', ') + ' MiB · ' + model.measured.at : 'No qualified full-workflow memory envelope yet.';
    if (model?.image_profile) $('measurement').textContent += ' Observed GPU peaks: ' + model.image_profile.gpu_peak_mib.join(', ') + ' MiB; host RSS peak: ' + model.image_profile.rss_peak_mib + ' MiB.';
    $('list').querySelectorAll('button').forEach(button => button.classList.toggle('active', button.dataset.name === model?.name));
    const saved=modelDrafts.get(model?.name || '');
    if(saved) {
      saved.controls.forEach(row=>{const input=row.id?document.getElementById(row.id):[...$('model-form').querySelectorAll('input')].find(input=>input.name===row.name && input.value===row.value); if(input) {input.value=row.value; if(row.checked!==undefined) input.checked=row.checked;}});
      document.querySelector('[data-hardware-prefix="mw"] [data-hardware-mode]:checked')?.dispatchEvent(new Event('change',{bubbles:true}));
      $('lora-rows').replaceChildren(); saved.loras.forEach(one=>addLora(one.name,one));
    }
    familyHelp();
    recheck(0);
  }
  async function refresh() {
    const value = await api(); models = value.models; nodeCards = value.cards;
    $('list').replaceChildren(); models.forEach(model => {
      const button = document.createElement('button'); button.className = 'btn btn-outline-secondary'; button.textContent = model.name; button.dataset.name = model.name;
      button.onclick = () => { location.href = '/models/images?model=' + encodeURIComponent(model.name) + '&node=' + encodeURIComponent(node); }; $('list').append(button);
    });
    $('cards').textContent = 'Enabled: ' + (value.cards.join(', ') || 'none');
    $('node').replaceChildren(new Option('Default', ''));
    value.nodes.forEach(one => $('node').add(new Option(one.name + ' · ' + ({unknown:'status not reported',offline:'not reachable',unavailable:'not reachable',ready:'available'}[one.state] || one.state || 'status not reported'), one.name))); $('node').value = node;
    const choices=$('install-targets').querySelector('[data-target-choices]');
    const before=[...choices.querySelectorAll('input:checked')].map(input=>input.value), first=!choices.children.length;
    choices.replaceChildren();
    value.nodes.forEach(one=>{
      const label=$('node-target').content.firstElementChild.cloneNode(true), input=label.querySelector('input');
      input.value=one.name; input.disabled=one.accepting_work===false;
      input.checked=!input.disabled && (first?one.name===node:before.includes(one.name));
      label.querySelector('[data-node-name]').textContent=one.name;
      label.querySelector('[data-node-state]').textContent=input.disabled?' '+(one.lifecycle || 'disabled'):one.state==='ready'?'':' '+({unknown:'status not reported',offline:'not reachable',unavailable:'not reachable',sleeping:'asleep'}[one.state] || one.state || 'status not reported');
      choices.append(label);
    });
    const engines = value.engines.engines || [];
    const runtime = engines.find(one => one.id === 'sdcpp');
    $('runtime').hidden = Boolean(runtime?.available);
    $('runtime').textContent = runtime?.why || 'Optional runtime: install with scripts/install-sdcpp.sh on this compute node. Manual directions are in Docs → Deployment and operations → Installation.';
    if(initialized) document.dispatchEvent(new CustomEvent('weightroom:image-inventory',{detail:{models,node}}));
  }
  async function files() {
    const response = await fetch('/model-files', {cache:'no-store'});
    const found = await response.json();
    if (!response.ok) throw new Error(found.error?.message || 'Model files could not be listed.');
    const choices = found.nodes.flatMap(source => source.files.filter(file => (file.facts?.what === 'image' || file.facts?.image_component) && !file.moving).map(file => ({...file, machine:source.name})));
    SLOTS.forEach(name => {
      $(name).replaceChildren(new Option(name === 'diffusion' ? 'Choose diffusion weights' : 'No file', ''));
      choices.forEach(file => { const option = new Option(file.path + ' · ' + file.machine + ' · ' + file.tier, file.path); option.dataset.machine = file.machine; option.dataset.tier = file.tier; $(name).add(option); });
    });
  }
  function renderLoras() {
    const values = JSON.parse($('loras').value || '{}');
    if (!values || Array.isArray(values) || typeof values !== 'object') throw new Error('LoRAs must be a JSON object.');
    $('lora-rows').replaceChildren();
    for (const [name, entry] of Object.entries(values)) addLora(name, entry);
  }
  function addLora(name = '', entry = {}) {
    const row = $('lora-row').content.firstElementChild.cloneNode(true);
    for (const [key, value] of [['name', name], ['file', entry.file || ''], ['scale', entry.scale ?? 1]]) {
      row.querySelector('[data-lora-field="'+key+'"]').value=value;
    }
    row.querySelector('[data-remove-lora]').onclick = () => {
      row.remove();
      try { syncLoras(); recheck(0); message(''); } catch (error) { message(error.message); }
      remember();
    }; $('lora-rows').append(row);
  }
  function syncLoras() {
    const raw = JSON.parse($('loras').value || '{}');
    if (!raw || Array.isArray(raw) || typeof raw !== 'object') throw new Error('LoRAs must be a JSON object.');
    const values = {};
    for (const row of $('lora-rows').rows) {
      if (row.hasAttribute('data-empty-row')) continue;
      const field = key => row.querySelector('[data-lora-field="' + key + '"]').value;
      if (!field('name') && !field('file')) continue;
      if (!field('name') || !field('file')) throw new Error('Each LoRA needs a name and a file.');
      if (Object.hasOwn(values, field('name'))) throw new Error('LoRA names must be unique.');
      Object.defineProperty(values, field('name'), {value:{file:field('file'), scale:Number(field('scale'))}, enumerable:true});
    }
    $('loras').value = JSON.stringify(values, null, 2);
  }
  $('add-lora').onclick = () => addLora();
  $('loras').addEventListener('change', () => { try { renderLoras(); recheck(0); message(''); } catch (error) { message(error.message); } });
  $('lora-rows').addEventListener('change', () => { try { syncLoras(); recheck(0); message(''); } catch (error) { message(error.message); } });
  document.querySelector('[data-hardware-prefix="mw"]').addEventListener('change', () => {
    const gpus = (() => { try { return SuiteHardware.read('mw'); } catch { return []; } })();
    document.querySelector('[data-image-split]').hidden = gpus === 0;
    if (gpus === 0 || Array.isArray(gpus) && gpus.length === 1) $('split').value = 'single';
    if (Array.isArray(gpus) && gpus.length > 1 && $('split').value === 'single') $('split').value = 'components';
  });
  $('split').addEventListener('change', () => { delete document.querySelector('[data-hardware-prefix="mw"]').dataset.automaticGpus; });
  $('new').onclick = () => {remember(); fill(null); dialog.showModal(); $('name').focus();};
  document.addEventListener('weightroom:image-select',event=>{remember(); fill(models.find(model=>model.name===event.detail.name) || null);});
  document.addEventListener('weightroom:image-edit',event=>{fill(models.find(model=>model.name===event.detail.name) || null); dialog.showModal();});
  dialog.addEventListener('close',remember);
  dialog.addEventListener('cancel',remember);
  $('model-form').addEventListener('input',remember);
  $('model-form').addEventListener('change',remember);
  $('family').onchange = () => setFamily($('family').value);
  [...SLOTS, 'loras'].forEach(name => { $(name).addEventListener('input', () => recheck()); $(name).addEventListener('change', () => recheck(0)); });
  $('model-form').onsubmit = async event => {
    event.preventDefault();
    try {
      syncLoras();
      const machines=[...$('install-targets').querySelectorAll('input:checked:not(:disabled)')].map(input=>input.value);
      if(!selected && !machines.length) {message('Choose at least one destination node.'); $('install-targets').scrollIntoView({block:'center'}); return;}
      const answer = await api({name:$('name').value.trim(), settings:draft(), new:!selected,
        source_machine:$('diffusion').selectedOptions[0]?.dataset.machine || node,...(!selected?{machines}:{})});
      if(Array.isArray(answer.jobs)) {
        const query=new URLSearchParams({kind:'image'}); answer.jobs.forEach(job=>query.append('install',job.id));
        location.href='/models?'+query; return;
      }
      if (answer.kind === 'model-install') { location.href = '/models?kind=image&install=' + encodeURIComponent(answer.id); return; }
      modelDrafts.delete(selected?.name || '');
      await refresh(); fill(models.find(one => one.name === answer.name)); $('saved').textContent = 'Saved.'; message('');
      dialog.close(); document.dispatchEvent(new CustomEvent('weightroom:image-saved',{detail:{name:answer.name}}));
    } catch (error) { message(error.message); }
  };
  Promise.all([refresh(), files()]).then(() => {
    initialized=true;
    document.dispatchEvent(new CustomEvent('weightroom:image-inventory',{detail:{models,node}}));
    const params = new URLSearchParams(location.search);
    if (params.has('new')) {fill(null); dialog.showModal();}
    if (params.has('file')) { fill(null); if (!Array.from($('diffusion').options).some(one => one.value === params.get('file'))) $('diffusion').add(new Option(params.get('file'), params.get('file'))); $('diffusion').value = params.get('file'); dialog.showModal(); recheck(0); }
  }).catch(error => message(error.message));
})();
