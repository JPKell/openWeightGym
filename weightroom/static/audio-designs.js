/* Standalone proposals. No requests, installation, loading or audio generation. */
(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('audio-data').textContent);
  const $ = id => document.getElementById(id);
  const field = id => $('am-' + id);
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const labels = {music:'Music', sound:'Sound effects', speech:'Speech', clone:'Voice cloning', design:'Voice design', transcription:'Transcription', alignment:'Alignment', diarization:'Speaker detection'};
  const groups = {all:'All tasks', music:'Music', voice:'Voices', sound:'Sound effects', understand:'Understanding'};
  const descriptions = {
    music:'Songs, instrumentals and musical ideas.', sound:'Atmospheres, textures and sound effects.',
    speech:'Natural speech from your text.', clone:'Speech with a reference voice.', design:'Create a voice from a description.',
    transcription:'Turn recordings into readable text.', alignment:'Match spoken words to audio timestamps.',
    diarization:'Find who spoke, and when.'
  };
  const notes = {
    catalogue:'01 / Familiar, refined, and fleet-first. The existing Models catalogue, with a clearer path from installed inventory to configuration.',
    studio:'02 / A focused configuration studio. Keep your models on the left and their settings on the right, with drafts preserved as you switch.',
    library:'03 / Start with the sound you want to make. A task-based library with spacious model cards and configuration one click away.'
  };
  let layout = new URLSearchParams(location.search).get('design') || location.hash.slice(1) || 'catalogue';
  if (!Object.hasOwn(notes, layout)) layout = 'catalogue';
  let task = 'all', node = 'all', selected = null, editing = null, section = 'files', sequence = 0;
  let editorVisible = false, updating = false;
  const emptyModels = [], drafts = new Map();
  const scenario = () => $('scenario').value;
  const models = () => scenario() === 'empty' ? emptyModels : data.models;
  const online = name => scenario() !== 'offline' && Boolean(data.nodes.find(one => one.id === name)?.online);
  const kindGroup = kind => ['speech','clone','design'].includes(kind) ? 'voice' : ['transcription','alignment','diarization'].includes(kind) ? 'understand' : kind;
  const relevantPlaces = model => model.placements.filter(place => node === 'all' || place.node === node);
  const state = place => online(place.node) ? place.state : 'offline';
  const stateLabel = value => ({loaded:'Loaded', installed:'Installed', offline:'Unverified'})[value];
  const draftKey = one => `${one.id || 'new'}/${one.node || ''}/${one.empty ? 'empty' : 'normal'}`;
  const read = () => Object.fromEntries(['name','file','family','kind','root','companions','node','gpu','ram','seconds','takes','text','audio','defaults','session'].map(key => [key,field(key).value]).concat([['alone',field('alone').checked]]));
  function stash() {
    if (editing && editorVisible) drafts.set(draftKey(editing), {values:read(), dirty:$('am-form').dataset.dirty === 'true'});
  }
  function setSection(value) {
    section = value;
    document.querySelectorAll('[data-panel]').forEach(el => { el.hidden = el.dataset.panel !== section; });
    document.querySelectorAll('[data-section]').forEach(el => {
      const active = el.dataset.section === section;
      el.classList.toggle('active',active); el.setAttribute('aria-pressed',String(active));
    });
  }
  function enforceExclusive() {
    const exclusive = ['music','sound'].includes(field('kind').value);
    if (exclusive) field('alone').checked = true;
    field('alone').disabled = exclusive;
    $('exclusive-note').textContent = exclusive ? 'Required for music and sound models.' : 'Enable to reserve these GPUs while this model is loaded.';
  }
  function updateSummary() {
    enforceExclusive();
    const info = data.nodes.find(one => one.id === field('node').value);
    const unavailable = !online(field('node').value);
    const savedFile = data.files.find(one => one.path === field('file').value);
    $('save-summary').textContent = `${field('name').value || 'New audio model'} → ${field('node').value || 'Choose a node'}`;
    $('save-detail').textContent = `${field('gpu').value} · ${Number(field('ram').value || 0).toLocaleString()} MiB host RAM · ${labels[field('kind').value] || 'Choose a task'}`;
    $('editor-context').textContent = editing?.id ? `${field('node').value} · ${info?.hardware || ''}. Files and output apply to this model; hardware applies to this node.` : 'Choose a model file, set its compute location, then save. Hardware and output defaults are ready to review.';
    $('editor-state').textContent = unavailable ? 'Node unavailable' : editing?.id ? 'Installed' : 'New model';
    $('save-model').disabled = unavailable;
    $('save-model').textContent = unavailable ? 'Node unavailable' : editing?.id ? 'Save configuration' : 'Add to sample library';
    $('file-note').textContent = savedFile ? `${savedFile.tier === 'storage' ? 'Storage file · copied to the selected compute node after space checks in the live app.' : 'Managed cache file · ' + savedFile.node + '.'} Sample setup only; no files are copied here.` : 'Choose a managed file. Its family determines the available audio tasks.';
    $('family-note').textContent = data.families[field('family').value]?.note || 'Optional runtime-specific session settings. Leave empty to use runtime defaults.';
    const dirty = $('am-form').dataset.dirty === 'true';
    $('draft-status').textContent = dirty ? 'Unsaved draft' : editing?.id ? 'All changes saved in preview' : 'Review setup, then add model';
  }
  function familyChanged(preferred) {
    const family = field('family').value;
    field('kind').replaceChildren(...(data.families[family]?.kinds || []).map(kind => new Option(labels[kind],kind)));
    if ([...field('kind').options].some(option => option.value === preferred)) field('kind').value = preferred;
    updateSummary();
  }
  function fileChanged() {
    const info = data.files.find(one => one.path === field('file').value);
    field('family').replaceChildren(new Option(info?.family || 'Choose a file first',info?.family || ''));
    if (info) {
      field('root').value = info.path.slice(0,info.path.lastIndexOf('/'));
      if (!field('name').value) field('name').value = info.path.split('/').pop().replace(/\.gguf$/,'');
    }
    familyChanged();
  }
  function fillEditor() {
    updating = true;
    const model = models().find(one => one.id === editing.id);
    const place = model?.placements.find(one => one.node === editing.node);
    const draft = drafts.get(draftKey(editing));
    const values = draft?.values || (model ? {...model,...place,companions:model.companions.join('\n')} : {
      name:'',file:'',family:'',kind:'',root:'',companions:'',node:node !== 'all' && online(node) ? node : 'atlas',gpu:'GPU 0',ram:8192,
      alone:false,seconds:600,takes:8,text:20000,audio:32,defaults:'{}',session:'{}'
    });
    field('file').replaceChildren(new Option('Choose an audio model file',''),...data.files.map(one => new Option(`${one.path} · ${one.tier}`,one.path)));
    field('node').replaceChildren(...data.nodes.map(one => {
      const option = new Option(`${one.id} · ${one.hardware}${online(one.id) ? '' : ' · unavailable'}`,one.id);
      option.disabled = !online(one.id); return option;
    }));
    field('file').value = values.file;
    field('family').replaceChildren(new Option(values.family || 'Choose a file first',values.family || ''));
    familyChanged(values.kind);
    for (const [key,value] of Object.entries(values)) if (field(key) && !['family','kind','alone'].includes(key)) field(key).value = value;
    field('alone').checked = values.alone;
    $('am-form').dataset.dirty = String(Boolean(draft?.dirty));
    $('editor-title').textContent = model ? model.name : 'Add an audio model';
    $('form-error').hidden = true;
    setSection(section); updateSummary(); updating = false;
  }
  function attachEditor(host) {
    $(host).append($('audio-editor')); editorVisible = true;
  }
  function openEditor(id, chosenNode, isNew=false) {
    stash();
    const model = models().find(one => one.id === id);
    editing = {id:isNew ? null : id,node:chosenNode || model?.placements[0].node || '',empty:scenario() === 'empty'};
    selected = isNew ? selected : id;
    section = 'files';
    fillEditor();
    if (layout === 'studio' && !isNew) { attachEditor('inline-editor'); renderChoices(filtered()); }
    else { attachEditor('dialog-editor'); $('model-dialog').showModal(); }
  }
  function filtered() {
    const search = $('model-search').value.trim().toLowerCase(), availability = $('availability').value;
    return models().filter(model => {
      const places = relevantPlaces(model);
      return places.length && (task === 'all' || kindGroup(model.kind) === task) &&
        (!search || [model.name,model.family,model.kind,...model.placements.map(one => one.node)].join(' ').toLowerCase().includes(search)) &&
        (availability === 'all' || places.some(place => state(place) === availability));
    }).sort((a,b) => $('sort').value === 'size' ? a.size-b.size : $('sort').value === 'task' ? labels[a.kind].localeCompare(labels[b.kind]) || a.name.localeCompare(b.name) : a.name.localeCompare(b.name));
  }
  function identity(el, model) {
    el.querySelector('[data-name]').textContent = model.name;
    el.querySelector('[data-meta]').textContent = model.family + ' · audio.cpp';
    el.querySelector('[data-initial]').textContent = model.name[0].toUpperCase();
  }
  function decorate(el, model) {
    identity(el,model);
    el.dataset.model = model.id;
    el.querySelector('[data-kind]').textContent = labels[model.kind];
    el.querySelectorAll('[data-open],[data-configure]').forEach(button => {
      button.setAttribute('aria-label',`Configure ${model.name}`);
      button.addEventListener('click',() => openEditor(model.id,relevantPlaces(model)[0].node));
    });
    const places = relevantPlaces(model), known = places.some(place => state(place) === 'loaded') ? 'loaded' : places.every(place => !online(place.node)) ? 'offline' : 'installed';
    el.querySelector('[data-availability]').dataset.state = known;
    el.querySelector('[data-state-label]').textContent = stateLabel(known);
    el.querySelector('[data-size]').textContent = `${model.size.toFixed(1)} GiB`;
    for (const place of places) {
      const chip = clone('audio-placement');
      chip.dataset.state = state(place); chip.querySelector('[data-node-name]').textContent = place.node;
      chip.querySelector('[data-status]').textContent = ' · ' + stateLabel(state(place));
      chip.title = `${place.node} · ${stateLabel(state(place))}`;
      chip.setAttribute('aria-label',`Configure ${model.name} on ${place.node}`);
      chip.addEventListener('click',() => openEditor(model.id,place.node));
      el.querySelector('[data-placements]').append(chip);
    }
    if (el.querySelector('[data-description]')) el.querySelector('[data-description]').textContent = descriptions[model.kind];
    return el;
  }
  function renderChoices(list) {
    $('model-choices').replaceChildren(...list.map(model => {
      const el = clone('audio-choice'); identity(el,model); el.dataset.model = model.id;
      el.classList.toggle('is-selected',selected === model.id);
      el.querySelector('[data-kind]').textContent = labels[model.kind];
      el.querySelector('[data-identity]').setAttribute('aria-pressed',String(selected === model.id));
      el.querySelector('[data-identity]').addEventListener('click',() => openEditor(model.id,relevantPlaces(model)[0].node));
      return el;
    }));
  }
  function render() {
    const list = filtered(), all = models(), places = all.flatMap(model => model.placements);
    $('workspace').dataset.layout = layout;
    $('design-note').textContent = notes[layout];
    document.querySelectorAll('[data-design]').forEach(button => {
      button.classList.toggle('active',button.dataset.design === layout);
      button.setAttribute('aria-pressed',String(button.dataset.design === layout));
    });
    $('catalogue-intro').hidden = layout !== 'catalogue';
    $('task-rail').hidden = layout !== 'library';
    $('task-pills').hidden = layout === 'library';
    $('catalogue-view').hidden = layout !== 'catalogue' || !list.length;
    $('studio-view').hidden = layout !== 'studio' || !list.length;
    $('library-view').hidden = layout !== 'library' || !list.length;
    $('empty-state').hidden = Boolean(list.length);
    $('empty-title').textContent = all.length ? 'No models match' : 'A little quiet in here';
    $('empty-description').textContent = all.length ? 'Try another task, choose all nodes, or clear your filters.' : 'Add your first audio model to start building your sound library.';
    $('empty-state').querySelector('[data-add]').textContent = all.length ? 'Add audio model' : 'Add your first model';
    $('connection-note').hidden = scenario() !== 'offline';
    $('total-models').textContent = all.length;
    $('total-installations').textContent = places.length;
    $('total-loaded').textContent = scenario() === 'offline' ? '—' : places.filter(place => online(place.node) && place.state === 'loaded').length;
    $('total-kinds').textContent = new Set(all.map(model => model.kind)).size;
    $('result-count').textContent = `${list.length} of ${all.length} models`;
    document.querySelectorAll('[data-node]').forEach(button => {
      button.classList.toggle('active',button.dataset.node === node);
      button.setAttribute('aria-pressed',String(button.dataset.node === node));
    });
    document.querySelectorAll('[data-task]').forEach(button => {
      button.classList.toggle('active',button.dataset.task === task);
      button.setAttribute('aria-pressed',String(button.dataset.task === task));
    });
    $('task-choices').replaceChildren(...Object.entries(groups).map(([key,label]) => {
      const button = document.createElement('button'); button.type = 'button';
      button.setAttribute('aria-pressed',String(task === key));
      const text = document.createElement('span'), count = document.createElement('span');
      text.textContent = label; count.textContent = all.filter(model => key === 'all' || kindGroup(model.kind) === key).length;
      count.className = 'text-secondary small'; button.append(text,count);
      button.addEventListener('click',() => { stash(); task = key; render(); });
      return button;
    }));
    $('audio-rows').replaceChildren(...(list.length ? list.map(model => decorate(clone('audio-row'),model)) : [clone('audio-empty-row')]));
    $('audio-cards').replaceChildren(...list.map(model => decorate(clone('audio-card'),model)));
    renderChoices(list);
    if (!$('model-dialog').open) {
      if (layout === 'studio' && list.length) {
        if (!list.some(model => model.id === selected)) selected = list[0].id;
        const model = list.find(one => one.id === selected);
        const targetNode = relevantPlaces(model).some(place => place.node === editing?.node) ? editing.node : relevantPlaces(model)[0].node;
        editing = {id:selected,node:targetNode,empty:scenario() === 'empty'};
        fillEditor(); attachEditor('inline-editor'); renderChoices(list);
      } else { $('editor-parking').append($('audio-editor')); editorVisible = false; }
    }
  }
  function fail(message, control) {
    $('form-error').hidden = false; $('form-error-text').textContent = message;
    if (control) { setSection(control.closest('[data-panel]').dataset.panel); control.focus(); }
  }
  function validate() {
    for (const control of $('am-form').querySelectorAll('input,select,textarea')) {
      if (!control.checkValidity()) { fail(control.validationMessage,control); control.reportValidity(); return false; }
    }
    const values = read(), info = data.files.find(one => one.path === values.file);
    if (!info) { fail('Choose a model file from cache or storage.',field('file')); return false; }
    if (!values.name.trim()) { fail('Enter a model name.',field('name')); return false; }
    if (models().some(model => model.id !== editing.id && model.name.toLowerCase() === values.name.trim().toLowerCase())) { fail('A model already uses this name.',field('name')); return false; }
    const root = values.root.trim().replace(/\/+$/,'');
    if (!root || !values.file.startsWith(root + '/')) { fail('The model file must be inside the package folder.',field('root')); return false; }
    const companions = values.companions.split('\n').map(one => one.trim()).filter(Boolean);
    if (companions.some(path => path.startsWith('/') || path.split('/').includes('..'))) { fail('Companion paths must stay inside the package folder.',field('companions')); return false; }
    for (const key of ['defaults','session']) {
      try { const parsed = JSON.parse(values[key]); if (!parsed || Array.isArray(parsed) || typeof parsed !== 'object') throw Error(); }
      catch { fail('Enter a valid JSON object.',field(key)); return false; }
    }
    if (!online(values.node)) { fail('Choose an available compute node.',field('node')); return false; }
    return {...values,root,companions};
  }
  $('am-form').addEventListener('submit',event => {
    event.preventDefault(); const values = validate(); if (!values) return;
    const isNew = !editing.id, list = models();
    const info = data.files.find(one => one.path === values.file);
    let model = list.find(one => one.id === editing.id);
    if (!model) { model = {id:'preview-' + ++sequence,placements:[],size:2.4}; list.push(model); }
    const oldFile = model.file;
    Object.assign(model,...['name','file','family','kind','root','companions','defaults','session'].map(key => ({[key]:values[key]})));
    model.name = values.name.trim();
    for (const key of ['seconds','takes','text','audio']) model[key] = Number(values[key]);
    const place = model.placements.find(one => one.node === values.node) || {state:'installed'};
    Object.assign(place,{node:values.node,gpu:values.gpu,ram:Number(values.ram),alone:values.alone});
    if (oldFile !== values.file) place.state = 'installed';
    if (!model.placements.includes(place)) model.placements.push(place);
    // A node's installation must remain unique if a placement is moved.
    model.placements = model.placements.filter((one,index,array) => array.findIndex(other => other.node === one.node) === index);
    const source = data.models.find(one => one.file === info.path);
    if (isNew && source && source !== model) model.size = source.size;
    drafts.delete(draftKey(editing)); selected = model.id;
    if (isNew) {
      task = 'all'; node = values.node;
      $('model-search').value = ''; $('availability').value = 'all';
    }
    $('save-notice').hidden = false;
    $('save-notice-text').textContent = `${model.name} ${isNew ? 'added' : 'saved'} on ${values.node}. Preview only.`;
    $('am-form').dataset.dirty = 'false';
    editing = {id:model.id,node:values.node,empty:scenario() === 'empty'};
    if ($('model-dialog').open) $('model-dialog').close();
    else { render(); }
  });
  $('am-form').addEventListener('input',() => {
    if (updating) return; $('am-form').dataset.dirty = 'true'; updateSummary();
  });
  $('am-form').addEventListener('change',() => {
    if (updating) return; $('am-form').dataset.dirty = 'true'; updateSummary();
  });
  field('file').addEventListener('change',fileChanged);
  field('kind').addEventListener('change',enforceExclusive);
  field('node').addEventListener('change',() => {
    const model = models().find(one => one.id === editing?.id);
    const place = model?.placements.find(one => one.node === field('node').value);
    if (place) { field('gpu').value = place.gpu; field('ram').value = place.ram; field('alone').checked = place.alone; updateSummary(); }
  });
  $('reset-model').addEventListener('click',() => { drafts.delete(draftKey(editing)); fillEditor(); });
  document.querySelectorAll('[data-section]').forEach(button => button.addEventListener('click',() => setSection(button.dataset.section)));
  document.querySelectorAll('[data-design]').forEach(button => button.addEventListener('click',() => {
    stash(); layout = button.dataset.design; history.replaceState(null,'','#' + layout); render();
  }));
  document.querySelectorAll('[data-task]').forEach(button => button.addEventListener('click',() => { stash(); task = button.dataset.task; render(); }));
  document.querySelectorAll('[data-node]').forEach(button => button.addEventListener('click',() => { stash(); node = button.dataset.node; render(); }));
  ['model-search','availability','sort'].forEach(id => $(id).addEventListener(id === 'model-search' ? 'input' : 'change',() => { stash(); render(); }));
  $('scenario').addEventListener('change',() => { stash(); selected = null; editing = null; render(); });
  $('clear-filters').addEventListener('click',() => { stash(); task = 'all'; node = 'all'; $('model-search').value = ''; $('availability').value = 'all'; render(); });
  [$('add-model'),...document.querySelectorAll('[data-add]')].forEach(button => button.addEventListener('click',() => openEditor(null,null,true)));
  $('close-dialog').addEventListener('click',() => $('model-dialog').close());
  $('model-dialog').addEventListener('close',() => { stash(); render(); });
  $('theme').addEventListener('click',() => {
    const select = document.querySelector('[data-theme-form] [name="mode"]');
    select.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    select.dispatchEvent(new Event('change',{bubbles:true}));
  });
  const themeLabel = () => { $('theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode'; };
  document.addEventListener('weightroom:theme',themeLabel);
  themeLabel();
  render();
})();
