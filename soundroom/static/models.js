/* Live Audio model studio; managed files and runtime contracts retain the existing API envelope. */
(() => {
  'use strict';
  const root = document.getElementById('soundroom-models');
  if (!root) return;
  const $ = name => document.getElementById('am-' + name);
  const params = new URLSearchParams(location.search);
  const computeNodes = JSON.parse($('nodes').textContent);
  const inventories = new Map(), failures = new Set(), drafts = new Map(), installations = new Map();
  const kinds = {music:'Music',sound:'Sound effects',speech:'Speech',clone:'Voice cloning',design:'Voice design',transcription:'Transcription',alignment:'Alignment',diarization:'Speaker detection'};
  let activeNode = params.get('node') || computeNodes[0]?.name || '';
  let inventory = {}, nodes = [], choices = [], selected = null, task = 'all', section = 'files';
  let editing = false, dirty = false, busy = false, loading = true, initialized = false;
  let installTimer, checkingInstallations=false, installRefreshNeeded=false;
  const terminal = job => ['complete','failed'].includes(job.state);
  const element = (tag, text, className) => { const one = document.createElement(tag); if (text !== undefined) one.textContent = text; if (className) one.className = className; return one; };
  const message = error => { $('message').hidden = !error; $('message').textContent = error || ''; };
  const key = (name=selected?.name || '', node=activeNode) => JSON.stringify([name ? node : '',name]);
  const available = name => inventories.has(name) && !failures.has(name);
  const accepting = name => available(name) && computeNodes.find(one => one.name === name)?.accepting_work !== false;
  const targets = () => [...$('install-targets').querySelectorAll('input:checked')].map(input=>input.value);
  const installing = () => !selected && [...installations.values()].some(job=>!terminal(job) && job.name === $('name').value.trim() && targets().includes(job.destination));
  const targetCards = () => selected ? inventory.cards || [] : (inventories.get(targets()[0])?.cards || []).filter(card=>targets().every(node=>(inventories.get(node)?.cards || []).includes(card)));
  function renderTargets(names=[activeNode]) {
    const area=$('install-targets').querySelector('[data-target-choices]');
    area.classList.remove('justify-content-end');
    $('install-targets').setAttribute('aria-labelledby','am-target-label');
    area.replaceChildren(...computeNodes.map(node=>{
      const label=$('node-target').content.firstElementChild.cloneNode(true), input=label.querySelector('input');
      input.value=node.name; input.checked=names.includes(node.name);
      input.disabled=Boolean(selected) || busy || (!input.checked && !accepting(node.name));
      label.querySelector('[data-node-name]').textContent=node.name;
      label.querySelector('[data-node-state]').textContent=available(node.name) ? node.accepting_work===false ? ' disabled' : '' : ' unavailable';
      return label;
    }));
  }
  function hardwareError() {
    if (selected || !targets().length) return '';
    const mode=root.querySelector('[data-hardware-mode]:checked')?.value;
    const pins=[...root.querySelectorAll('[data-gpu-card]:checked')].map(input=>input.value);
    const incompatible=targets().filter(node=> mode==='auto' ? !(inventories.get(node)?.cards || []).length : mode==='pin' && pins.some(card=>!(inventories.get(node)?.cards || []).includes(card)));
    return incompatible.length ? 'Choose CPU only or GPUs available on every selected node. Incompatible nodes: ' + incompatible.join(', ') + '.' : '';
  }
  const safe = action => async event => { try { await action(event); } catch (error) { message(error.message); } };
  async function call(path, body, node=activeNode) {
    if (path === '/soundroom-models') {
      if (!node) throw new Error('Choose a compute node.');
      path += '?node=' + encodeURIComponent(node);
    }
    const response = await fetch(path,{method:body === undefined ? 'GET' : 'POST',cache:'no-store',
      headers:body === undefined ? {} : {'content-type':'application/json'},body:body === undefined ? undefined : JSON.stringify(body)});
    if (response.redirected || response.status === 401) { location.href='/sign-in'; throw new Error('Please sign in again.'); }
    let value;
    try { value = await response.json(); } catch { throw new Error('The server did not return model data. Try again.'); }
    if (!response.ok) throw new Error(value.error?.message || 'The operation failed.');
    return value;
  }
  const chosen = () => $('file').value === '' ? null : choices[Number($('file').value)];
  const audioFamily = file => file?.facts?.what === 'audio' ? file.facts.family : '';
  const audioFile = (file,source) => file.facts?.what === 'audio' || file.facts?.architecture === 'audiocpp' ||
    file.place?.replace(/^storage_/, '') === 'audio' || file.path.split('/').includes('audio') ||
    (inventories.get(source.name)?.models || []).some(model=>model.flags.model === file.path);
  function families() {
    const list = [...(inventory.catalogue?.families || [])];
    for (const file of choices) if (audioFamily(file) && !list.some(one => one.family === file.facts.family))
      list.push({family:file.facts.family, name:file.facts.family, kinds:file.facts.kinds || [], description:'Contract embedded in this model file; the runtime must implement its architecture.'});
    if (selected && !list.some(one => one.family === selected.audiocpp.family)) list.push({family:selected.audiocpp.family, name:selected.audiocpp.family, kinds:[selected.audiocpp.kind], description:'Current model family; runtime catalogue unavailable.'});
    return list;
  }
  function fileKinds(file) {
    if (file.facts?.kinds?.length) return file.facts.kinds;
    const profiles=(inventories.get(file.machine)?.models || []).filter(row=>row.flags.model === file.path);
    const family=audioFamily(file) || profiles[0]?.audiocpp.family;
    const declared=[...inventories.values()].flatMap(known=>known.catalogue?.families || []).find(one=>one.family === family)?.kinds || [];
    return declared.length ? declared : profiles.map(row=>row.audiocpp.kind);
  }
  function modelKinds() {
    const declared=chosen() ? fileKinds(chosen()) : [];
    return (families().find(one=>one.family === $('family').value)?.kinds || []).filter(kind=>!declared.length || declared.includes(kind));
  }
  function modelKind() {
    const supported=modelKinds();
    return $('kind').value || (supported.includes(selected?.audiocpp.kind) ? selected.audiocpp.kind : supported[0]) || '';
  }
  function filterFiles(preferred=chosen()) {
    const kind=$('kind').value;
    const options=choices.flatMap((file,index)=>{
      if (kind && !fileKinds(file).includes(kind)) return [];
      const folder=file.path.split('/').filter(Boolean).at(-2) || '/';
      const option=new Option(file.machine + ' · ' + folder,String(index));
      option.dataset.path=file.path; option.dataset.machine=file.machine; option.dataset.tier=file.tier;
      option.disabled=Boolean(selected && (file.machine !== activeNode || file.tier === 'storage') && file.path !== selected.flags.model);
      return [option];
    });
    $('file').replaceChildren(new Option(options.length ? 'Choose an audio model file' : kind ? 'No model files for this task' : 'No audio model files available',''),...options);
    const index=choices.findIndex(file=>file.path === preferred?.path && file.machine === preferred?.machine);
    $('file').value=options.some(option=>option.value === String(index)) ? String(index) : '';
  }
  function taskChanged() {
    const previous=chosen();
    filterFiles();
    if (previous && !chosen()) { $('root').value=''; $('companions').replaceChildren(); }
    filterFamilies(); familyChanged();
  }
  function filterFamilies() {
    const file = chosen(), previous = $('family').value;
    const known = audioFamily(file);
    const declared=file ? fileKinds(file) : [];
    const choices = families().filter(one => one.kinds.length && (!known || one.family === known) && (!$('kind').value || one.kinds.includes($('kind').value)) && (!declared.length || one.kinds.some(kind=>declared.includes(kind))));
    $('family').replaceChildren(new Option('Choose a compatible model family',''), ...choices.map(one => new Option(one.name || one.family, one.family)));
    $('family').value = known || (choices.some(one => one.family === previous) ? previous : '');
  }
  function defaultFields(family) {
    let values = {};
    try { values = SuiteModelFields.read('am-defaults'); } catch { return; }
    $('default-fields').replaceChildren();
    const spec = family?.options?.request || [];
    for (const option of spec) {
      const column = element('div', undefined, 'col-md-6'), label = element('label', option.name.replace(/_/g, ' '), 'form-label');
      const input = element(option.values?.length || ['bool','boolean'].includes(option.type) ? 'select' : 'input');
      input.className = input.tagName === 'SELECT' ? 'form-select' : 'form-control';
      input.id = 'am-option-' + option.name; label.htmlFor = input.id;
      input.dataset.jsonSource = 'am-defaults'; input.dataset.jsonField = Object.hasOwn(values, family.family + '.' + option.name) ? family.family + '.' + option.name : option.name;
      if (input.tagName === 'SELECT') {
        input.append(new Option('Runtime default', ''));
        for (const value of option.values || [true, false]) input.add(new Option(String(value), String(value)));
        if (['bool','boolean'].includes(option.type)) input.dataset.scalar = 'boolean';
      } else if (['int','integer','float','number'].includes(option.type)) {
        input.type = 'number'; input.step = ['int','integer'].includes(option.type) ? '1' : 'any';
        if (option.min !== undefined) input.min = option.min;
        if (option.max !== undefined) input.max = option.max;
      }
      column.append(label, input);
      if (option.description) { const more = document.getElementById('am-default-help').content.firstElementChild.cloneNode(true); more.querySelector('[data-help-text]').textContent = option.description; column.append(more); }
      $('default-fields').append(column);
    }
    $('defaults-empty').hidden = spec.length > 0;
    $('defaults-empty').textContent = spec.length ? '' : 'No request options are declared; additional defaults remain in Advanced.';
    SuiteModelFields.load('am-defaults', values);
  }
  function exclusive() {
    const required = ['music','sound'].includes(modelKind());
    $('alone').disabled = required; if (required) $('alone').checked = true;
    $('exclusive-hint').hidden = !required;
  }
  function familyChanged() {
    const family = families().find(one => one.family === $('family').value);
    $('family-note').textContent = [family?.description, family?.note].filter(Boolean).join(' ');
    $('session').title = family?.note || ''; defaultFields(family); exclusive();
  }
  function companions(extra=[]) {
    const file = chosen(), root = $('root').value.replace(/\/$/, '');
    const previous = new Set(Array.from($('companions').selectedOptions).map(option => option.value));
    const source = nodes.find(one => one.name === file?.machine);
    const available = (source?.files || []).filter(one => !one.moving && one.path !== file?.path && one.path.startsWith(root + '/'));
    const profile=selected?.audiocpp;
    const savedRoot=profile?.path?.endsWith('.gguf') ? profile.path.slice(0,profile.path.lastIndexOf('/')) : profile?.path;
    const paths=new Set([...available.map(one=>one.path),...(profile?.files || []).map(path=>savedRoot + '/' + path),...(Array.isArray(extra) ? extra : []),...previous]);
    $('companions').replaceChildren(...[...paths].filter(path=>path.startsWith(root + '/') && path !== file?.path).map(path => { const option = new Option(path.slice(root.length + 1),path); option.selected = previous.has(path); return option; }));
  }
  function fileChanged() {
    const file = chosen();
    if (!file) { filterFamilies(); familyChanged(); return; }
    $('root').value = file.path.slice(0,file.path.lastIndexOf('/'));
    filterFamilies();
    familyChanged(); companions();
    if (!selected && !$('name').value) $('name').value = file.path.split('/').pop().replace(/\.[^.]+$/, '').toLowerCase().replace(/[^a-z0-9._-]/g,'-');
  }

  function showSection(value) {
    section = value;
    root.querySelectorAll('[data-panel]').forEach(panel => { panel.hidden = panel.dataset.panel !== section; });
    root.querySelectorAll('[data-section]').forEach(button => {
      const active = button.dataset.section === section;
      button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active));
    });
  }
  function updateSummary() {
    const destinations=selected ? [activeNode] : targets();
    $('save-summary').textContent = ($('name').value || 'New audio model') + ' → ' + (destinations.join(', ') || 'Choose nodes');
    const mode = root.querySelector('[data-hardware-mode]:checked')?.value;
    const device = mode === 'cpu' ? 'CPU' : mode === 'pin' ? [...root.querySelectorAll('[data-gpu-card]:checked')].map(one=>one.value).join(', ') || 'Choose GPUs' : 'Automatic GPU';
    $('save-detail').textContent = [device,Number($('ram').value || 0).toLocaleString() + ' MiB host RAM',kinds[modelKind()] || 'Choose a task'].join(' · ');
    $('draft-status').textContent = dirty ? 'Unsaved draft' : selected ? 'Saved configuration' : 'Review setup, then add model';
    const reachable = available(activeNode);
    $('editor-state').textContent = !reachable ? 'Node unavailable' : selected ? 'Installed' : 'New model';
    $('editor-context').textContent = selected ? 'Editing ' + selected.name + ' on ' + activeNode + '. Changes apply to this installation.' : 'Choose a managed file, assign hardware and review the output defaults.';
    $('submit').textContent = busy ? 'Saving…' : installing() ? 'Installing…' : selected ? 'Save model' : destinations.length > 1 ? 'Install on ' + destinations.length + ' nodes' : 'Add model';
    $('submit').disabled = busy || installing() || !reachable || (!selected && (!destinations.length || destinations.some(node=>!accepting(node))));
    $('install-targets').querySelectorAll('input').forEach(input=>{ input.disabled=Boolean(selected) || busy || (!input.checked && !accepting(input.value)); });
    $('target-note').textContent = selected ? 'Changes apply to this installation on ' + activeNode + '.' : hardwareError() || (!destinations.length ? 'Select at least one destination node.' : destinations.some(node=>!accepting(node)) ? 'A selected node is unavailable. Deselect it or retry to reconnect.' : destinations.some(node=>!(inventories.get(node)?.cards || []).length) ? 'CPU only is the default when a selected node has no audio GPUs. The same configuration applies to every selected node.' : 'Select one or more nodes. The same configuration applies to every selected node.');
    const runtime = (inventory.engines?.engines || []).find(one => one.id === 'audiocpp');
    $('runtime').textContent = !reachable ? 'This node is unavailable. Saved settings and drafts remain visible; retry to reconnect.' : runtime?.available ? 'audio.cpp is installed on ' + activeNode + '.' : runtime?.why || inventory.catalogue?.error || '';
    $('runtime').hidden = !$('runtime').textContent;
    const file = chosen();
    $('file-note').textContent = file ? file.machine + ' · ' + file.tier + '. ' + (file.tier === 'storage' ? 'Stored files are promoted and copied after space checks when adding a model.' : 'Managed model file.') : $('kind').value ? 'Showing files for ' + kinds[$('kind').value] + '. Choose All tasks to include files with unknown task metadata.' : 'Choose an audio task to filter model files in cache or storage.';
    $('session-note').textContent = families().find(one=>one.family === $('family').value)?.note || 'Optional runtime-specific session settings. Saved keys are preserved.';
    $('model-actions').hidden = !selected;
    $('test-button').hidden = !selected || !['music','sound'].includes(selected.audiocpp.kind);
    $('test-button').disabled = busy || !reachable || dirty;
    $('test-button').title = dirty ? 'Save or discard changes before testing.' : '';
    if (selected) {
      $('details').href = '/models/' + encodeURIComponent(selected.name) + '?node=' + encodeURIComponent(activeNode);
      $('measured').textContent = selected.measured?.mib?.length ? 'Peak seen: ' + selected.measured.mib.map(value=>value + ' MiB').join(' + ') : 'Memory not measured';
    }
  }
  function captureDraft() {
    if (!editing || loading || !dirty) return;
    const file = chosen();
    drafts.set(key(),{
      fields:[...$('form').querySelectorAll('input[id],select[id],textarea[id]')].map(input=>({id:input.id,value:input.value,checked:input.checked,jsonField:input.dataset.jsonField})),
      file:file ? {path:file.path,machine:file.machine} : null,
      companions:[...$('companions').selectedOptions].map(option=>option.value),
      targets:targets(),
      mode:root.querySelector('[data-hardware-mode]:checked')?.value || 'auto',
      pins:[...root.querySelectorAll('[data-gpu-card]:checked')].map(input=>input.value),
      automatic:root.querySelector('[data-hardware]').dataset.automaticGpus
    });
  }
  function restoreDraft(draft) {
    if (!draft) return;
    renderTargets(selected ? [activeNode] : draft.targets);
    const fieldValue = id => draft.fields.find(one=>one.id === 'am-' + id)?.value;
    $('kind').value=fieldValue('kind') || '';
    filterFiles(draft.file);
    $('defaults').value = '{}';
    filterFamilies(); $('family').value = fieldValue('family'); familyChanged();
    for (const saved of draft.fields) {
      if (saved.id === 'am-file') continue;
      const input = document.getElementById(saved.id);
      if (!input) continue;
      input.value = saved.value;
      if (saved.jsonField) input.dataset.jsonField = saved.jsonField;
      if (['checkbox','radio'].includes(input.type)) input.checked = saved.checked;
    }
    companions(draft.companions);
    for (const option of $('companions').options) option.selected = draft.companions.includes(option.value);
    SuiteHardware.set('am',draft.mode === 'cpu' ? 0 : draft.mode === 'pin' ? draft.pins : Number(draft.automatic) || 1,targetCards());
    if (draft.mode === 'pin' && !draft.pins.length) {
      root.querySelector('[data-hardware-mode][value="pin"]').checked = true;
      root.querySelector('[data-hardware-mode][value="pin"]').dispatchEvent(new Event('change',{bubbles:true}));
    }
    exclusive(); dirty = true;
  }
  function populate(name='') {
    loading = true; message(''); $('saved').textContent = '';
    inventory = inventories.get(activeNode) || {};
    selected = (inventory.models || []).find(row=>row.name === name) || null;
    $('form').reset(); $('defaults').value = '{}'; $('session').value = '{}';
    $('default-fields').replaceChildren();
    // Always retain installed paths, even if a cache scan cannot currently see the file.
    // Audio locations and configured profiles also identify weights when an older index lacks audio metadata.
    choices = nodes.flatMap(source=>(source.files || []).filter(file=>audioFile(file,source) && !file.moving && /\.(gguf|safetensors|pth|pt|bin|onnx)$/i.test(file.path)).map(file=>({...file,machine:source.name})));
    if (selected && !choices.some(file=>file.machine === activeNode && file.path === selected.flags.model)) {
      choices.push({path:selected.flags.model,machine:activeNode,tier:'current',facts:{what:'audio',family:selected.audiocpp.family,kinds:[selected.audiocpp.kind]}});
    }
    $('kind').replaceChildren(new Option('All tasks',''),...Object.entries(kinds).map(([kind,label])=>new Option(label,kind)));
    $('kind').value=selected?.audiocpp.kind || '';
    filterFiles(selected ? {path:selected.flags.model,machine:activeNode} : null);
    renderTargets();
    SuiteHardware.set('am',selected?.gpus ?? (inventory.cards?.length ? 1 : 0),inventory.cards || []);
    filterFamilies(); familyChanged();
    $('name').readOnly=Boolean(selected);
    $('heading').textContent=selected?.name || 'Add an audio model';
    if (selected) {
      const profile=selected.audiocpp;
      $('file').value=String(choices.findIndex(file=>file.path === selected.flags.model && file.machine === activeNode));
      fileChanged(); $('family').value=profile.family; familyChanged(); $('kind').value=profile.kind;
      $('name').value=selected.name; $('alone').checked=selected.alone ?? true; exclusive();
      $('seconds').value=profile.limits?.max_seconds ?? 600; $('takes').value=profile.limits?.max_takes ?? 8;
      $('text').value=profile.limits?.max_text_chars ?? 20000; $('audio').value=profile.limits?.max_audio_mib ?? 32;
      $('ram').value=profile.host_ram_mib ?? 8192; $('session').value=JSON.stringify(profile.session_options || {},null,2);
      $('defaults').value=JSON.stringify(profile.defaults || {}); defaultFields(families().find(one=>one.family === profile.family));
      SuiteModelFields.load('am-defaults',profile.defaults || {});
      $('root').value=profile.path.endsWith('.gguf') ? profile.path.slice(0,profile.path.lastIndexOf('/')) : profile.path;
      companions();
      for (const option of $('companions').options) option.selected=(profile.files || []).includes(option.value.slice($('root').value.length+1));
    }
    dirty=false; restoreDraft(drafts.get(key())); editing=true;
    $('controls').disabled=!available(activeNode);
    loading=false; showSection(section); updateSummary();
  }
  const kindGroup = kind => ['speech','clone','design'].includes(kind) ? 'voice' : ['transcription','alignment','diarization'].includes(kind) ? 'understand' : kind;
  const state = (row,node) => !available(node) ? 'offline' : row.running ? 'in-use' : row.loaded === 'ready' ? 'loaded' : row.loaded ? 'loading' : row.enabled === false ? 'disabled' : 'installed';
  const stateLabel = value => ({offline:'Node unavailable',loaded:'Loaded','in-use':'In use',loading:'Loading',disabled:'Disabled',installed:'Installed'})[value];
  function renderProfiles() {
    const grouped=new Map();
    for (const [node,known] of inventories) for (const row of known.models || []) {
      if (!grouped.has(row.name)) grouped.set(row.name,[]);
      grouped.get(row.name).push({node,row});
    }
    const all=[...grouped];
    const shown=all.map(([name,places])=>[name,places.filter(place=>task === 'all' || kindGroup(place.row.audiocpp.kind) === task)])
      .filter(([,places])=>places.length)
      .sort(([a],[b])=>a.localeCompare(b));
    const scroll=$('profiles').scrollTop;
    $('profiles').replaceChildren(...shown.map(([name,places])=>{
      const choice=$('profile-template').content.firstElementChild.cloneNode(true);
      const place=places.find(one=>one.node === activeNode) || places[0], row=place.row;
      choice.dataset.model=name;
      choice.querySelector('[data-name]').textContent=name;
      choice.querySelector('[data-meta]').textContent=row.audiocpp.family + ' · audio.cpp';
      choice.querySelector('[data-initial]').textContent=name[0].toUpperCase();
      choice.querySelector('[data-kind]').textContent=kinds[row.audiocpp.kind] || row.audiocpp.kind;
      const active=selected?.name === name && places.some(one=>one.node === activeNode);
      choice.classList.toggle('is-selected',active);
      const button=choice.querySelector('[data-identity]'); button.setAttribute('aria-pressed',String(active));
      button.addEventListener('click',()=>selectModel(name,place.node));
      choice.querySelector('[data-draft]').textContent=places.some(one=>drafts.has(key(name,one.node))) ? 'Unsaved draft' : '';
      for (const installation of places) {
        const chip=$('placement-template').content.firstElementChild.cloneNode(true), status=state(installation.row,installation.node);
        chip.querySelector('[data-node-name]').textContent=installation.node;
        chip.querySelector('[data-status]').textContent=' · ' + stateLabel(status);
        chip.dataset.state=status;
        chip.title=installation.node + ' · ' + stateLabel(status);
        chip.setAttribute('aria-label','Configure ' + name + ' on ' + installation.node + ': ' + stateLabel(status));
        chip.addEventListener('click',()=>selectModel(name,installation.node));
        choice.querySelector('[data-placements]').append(chip);
      }
      return choice;
    }));
    if (!shown.length) $('profiles').append(element('p',all.length ? 'No models for this task. Choose All tasks or add a model.' : failures.size ? 'Inventory is unavailable. Retry to reconnect to your compute nodes.' : 'No audio models yet. Choose a file to add your first model.','small text-secondary p-3 mb-0'));
    $('profiles').scrollTop=scroll;
    $('total-models').textContent=all.length || (failures.size ? '—' : '0');
    $('total-installations').textContent=all.reduce((count,[,places])=>count+places.length,0) || (failures.size ? '—' : '0');
    const reachable=[...inventories].filter(([node])=>available(node));
    $('total-loaded').textContent=reachable.length ? reachable.reduce((count,[node,known])=>count+(known.models || []).filter(row=>['loaded','in-use'].includes(state(row,node))).length,0) : '—';
    $('total-kinds').textContent=new Set(all.flatMap(([,places])=>places.map(place=>place.row.audiocpp.kind))).size || (failures.size ? '—' : '0');
    $('result-count').textContent=shown.length + ' of ' + all.length + ' models' + (failures.size ? ' · inventory incomplete' : '');
    root.querySelectorAll('[data-audio-task]').forEach(button=>{
      const active=button.dataset.audioTask === task;
      button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active));
    });
  }
  function updateLocation() {
    const url=new URL(location.href);
    if (activeNode) url.searchParams.set('node',activeNode);
    if (selected) { url.searchParams.set('model',selected.name); url.searchParams.delete('new'); }
    else { url.searchParams.delete('model'); url.searchParams.set('new','1'); }
    history.replaceState(null,'',url);
  }
  function selectModel(name,node) {
    if (busy) return;
    captureDraft(); activeNode=node; populate(name); updateLocation(); renderProfiles();
  }
  function startNew() {
    if (busy) return;
    captureDraft();
    activeNode=(accepting(activeNode) ? activeNode : computeNodes.find(one=>accepting(one.name))?.name) || activeNode;
    section='files'; populate(); updateLocation(); renderProfiles(); $('install-targets').querySelector('input:not(:disabled)')?.focus();
  }
  async function refresh(background=false) {
    if (busy) return false;
    captureDraft(); $('retry').disabled=true;
    try {
      const outcomes=await Promise.allSettled([call('/model-files'),...computeNodes.map(one=>call('/soundroom-models',undefined,one.name))]);
      if (!root.isConnected) return false;
      const files=outcomes[0];
      let fileError='';
      if (files.status === 'fulfilled') nodes=files.value.nodes || [];
      else fileError='Managed files could not be refreshed. ' + files.reason.message;
      for (const [index,one] of computeNodes.entries()) {
        const result=outcomes[index+1];
        if (result.status === 'fulfilled') { inventories.set(one.name,result.value); failures.delete(one.name); }
        else failures.add(one.name);
      }
      const warning=[fileError,failures.size ? 'Unavailable nodes: ' + [...failures].join(', ') + '. Last known configurations remain visible.' : '',!computeNodes.length ? 'No compute nodes are configured. Add one under Compute nodes first.' : ''].filter(Boolean).join(' ');
      $('load-error').hidden=!warning; $('load-error-text').textContent=warning;
      if (background === true) {
        // Completion updates the library without replacing a form the user is filling in.
        $('controls').disabled=busy || !available(activeNode);
        updateSummary();
      } else if (!initialized) {
        const requested=params.get('model');
        if (!params.get('node') && requested) activeNode=computeNodes.find(one=>(inventories.get(one.name)?.models || []).some(row=>row.name === requested))?.name || activeNode;
        if (!params.get('node') && !available(activeNode)) activeNode=computeNodes.find(one=>available(one.name))?.name || activeNode;
        const first=(inventories.get(activeNode)?.models || [])[0]?.name;
        populate(params.has('new') ? '' : requested || first || '');
        initialized=true;
      } else populate(selected?.name || '');
      renderProfiles();
      $('add').disabled=!computeNodes.some(one=>accepting(one.name));
      return true;
    } finally { if (root.isConnected) $('retry').disabled=false; }
  }
  function showInstallations(error='') {
    const jobs=[...installations.values()];
    $('install-status').hidden=!jobs.length;
    const kind=error ? 'warning' : jobs.some(job=>job.state === 'failed') ? 'danger' : jobs.every(terminal) ? 'success' : 'info';
    for (const value of ['info','warning','danger','success']) $('install-status').classList.toggle('alert-' + value,value === kind);
    const states={queued:'Queued',promoting:'Preparing files',copying:'Copying files',complete:'Installed',failed:'Failed'};
    $('install-text').textContent=jobs.map(job=>job.name + ' on ' + job.destination + ': ' + (states[job.state] || job.state) +
      (job.state === 'copying' && job.of ? ' (' + Math.round(100 * (job.done || 0) / job.of) + '%)' : '') + (job.why ? ' · ' + job.why : '')).join('; ') + (error ? '. Progress unavailable: ' + error : '');
    $('install-retry').hidden=!error;
  }
  async function checkInstallations() {
    if (!root.isConnected || checkingInstallations) return;
    clearTimeout(installTimer);
    const pending=[...installations.values()].filter(job=>!terminal(job));
    if (!pending.length && !installRefreshNeeded) return;
    checkingInstallations=true;
    try {
      if (pending.length) {
        const query=new URLSearchParams(); pending.forEach(job=>query.append('install',job.id));
        const answer=await call('/soundroom-installations?' + query);
        if (!root.isConnected) return;
        const found=new Map((answer.installations || []).map(job=>[job.id,job]));
        if (pending.some(job=>!found.has(job.id))) throw new Error('Some jobs could not be found. Retry to check their status.');
        for (const prior of pending) {
          const job=found.get(prior.id);
          Object.assign(prior,job);
          if (job.state === 'complete') installRefreshNeeded=true;
        }
      }
      showInstallations(); updateSummary();
      if (installRefreshNeeded && !busy) installRefreshNeeded=!(await refresh(true));
      if (!root.isConnected) return;
      if (installRefreshNeeded || [...installations.values()].some(job=>!terminal(job))) installTimer=setTimeout(checkInstallations,1000);
    } catch (error) { if (root.isConnected) showInstallations(error.message); }
    finally { checkingInstallations=false; }
  }
  async function runTest() {
    if (!selected || dirty) return;
    message('');
    const row=selected, node=activeNode;
    $('test-button').disabled=true; $('test').hidden=false; $('test-title').textContent='Testing ' + row.name + '…';
    $('test-result').textContent=''; $('test-audio').removeAttribute('src');
    try {
      const answer=await call('/soundroom-test',{model:row.name,prompt:row.audiocpp.kind === 'music' ? 'short bright piano melody, gentle drums' : 'a single door knock',duration:10},node);
      $('test-title').textContent=row.name + ' · ' + answer.seconds + ' s';
      $('test-result').textContent=JSON.stringify(answer.take?.metrics || answer,null,2);
      if (answer.take?.asset_id) $('test-audio').src='/soundroom-api/assets/' + encodeURIComponent(answer.take.asset_id) + '/content';
    } finally { updateSummary(); }
  }
  $('file').addEventListener('change',fileChanged); $('family').addEventListener('change',familyChanged); $('root').addEventListener('change',companions); $('kind').addEventListener('change',taskChanged);
  root.querySelectorAll('[data-section]').forEach(button=>button.addEventListener('click',()=>showSection(button.dataset.section)));
  root.querySelectorAll('[data-audio-task]').forEach(button=>button.addEventListener('click',()=>{ task=button.dataset.audioTask; renderProfiles(); }));
  $('add').addEventListener('click',startNew);
  $('install-targets').addEventListener('change',()=>{
    if (selected || busy) return;
    // Retain the form while updating the GPUs shared by the selected destinations.
    const mode=root.querySelector('[data-hardware-mode]:checked')?.value;
    let gpus;
    try { gpus=SuiteHardware.read('am'); } catch { gpus=[]; }
    if (mode==='auto' && targets().some(node=>!(inventories.get(node)?.cards || []).length)) gpus=0;
    SuiteHardware.set('am',gpus,targetCards());
    if (mode==='pin' && !gpus.length) {
      root.querySelector('[data-hardware-mode][value="pin"]').checked=true;
      root.querySelector('[data-hardware-mode][value="pin"]').dispatchEvent(new Event('change',{bubbles:true}));
    }
  });
  $('reset').addEventListener('click',()=>{ drafts.delete(key()); populate(selected?.name || ''); renderProfiles(); });
  const changed=()=>{
    if (loading) return; dirty=true; $('saved').textContent='';
    queueMicrotask(()=>{ updateSummary(); captureDraft(); });
  };
  $('form').addEventListener('input',changed); $('form').addEventListener('change',changed);
  $('form').addEventListener('submit',safe(async event=>{
    event.preventDefault(); message('');
    const machines=targets();
    if (installing()) throw new Error('This model is already being installed on a selected node.');
    if (!selected && (!machines.length || machines.some(node=>!accepting(node)))) throw new Error('Choose at least one available destination node.');
    if (hardwareError()) { showSection('hardware'); throw new Error(hardwareError()); }
    for (const input of $('form').querySelectorAll('input,select,textarea')) if (!input.checkValidity()) {
      const panel=input.closest('[data-panel]'); if (panel) showSection(panel.dataset.panel);
      input.reportValidity(); return;
    }
    const file=chosen(),family=$('family').value,kind=modelKind(),root=$('root').value.trim().replace(/\/$/,'');
    if (!file || !family) throw new Error('Choose an audio model file and its compatible runtime family.');
    if (!modelKinds().includes(kind)) throw new Error('Choose an audio task supported by this model file and family.');
    if (!file.path.startsWith(root + '/')) throw new Error('The model file must be inside its package folder.');
    const parse=name=>{
      const value=JSON.parse($(name).value || '{}');
      if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error(name + ' must be a JSON object.');
      return value;
    };
    let session,defaults;
    try { session=parse('session'); defaults=SuiteModelFields.read('am-defaults'); }
    catch (error) { showSection('advanced'); throw new Error('Advanced JSON: ' + error.message); }
    const device=SuiteHardware.read('am');
    const preserved=Object.fromEntries(['allow_split','adapters','min_context','order'].filter(name=>Object.hasOwn(selected || {},name)).map(name=>[name,selected[name]]));
    const settings={...preserved,engine:'audiocpp',flags:{...selected?.flags,model:file.path,'ctx-size':0},gpus:device,
      alone:['music','sound'].includes(kind) || $('alone').checked,
      audiocpp:{...selected?.audiocpp,family,kind,package:selected?.audiocpp.package || '',
        path:file.path.endsWith('.gguf') ? file.path : root,files:[...$('companions').selectedOptions].map(option=>option.value.slice(root.length+1)),
        session_options:session,defaults,
        limits:{...selected?.audiocpp.limits,max_seconds:Number($('seconds').value),max_takes:Number($('takes').value),max_text_chars:Number($('text').value),max_audio_mib:Number($('audio').value)},host_ram_mib:Number($('ram').value)}};
    const name=$('name').value.trim(), draftKey=key();
    busy=true; $('controls').disabled=true; updateSummary();
    try {
      const answer=await call('/soundroom-models',{name,settings,new:!selected,source_machine:file.machine,...(!selected ? {machines} : {})});
      if (Array.isArray(answer.jobs) || answer.kind === 'model-install') {
        const jobs=answer.jobs || [answer];
        if (!jobs.length) throw new Error('The server did not return an installation job.');
        jobs.forEach((job,index)=>installations.set(job.id,{...job,name:job.name || name,destination:machines[index] || job.machine || activeNode,state:job.state || 'queued'}));
        installRefreshNeeded ||= jobs.some(job=>job.state === 'complete');
        drafts.delete(draftKey); dirty=false;
        const kindFilter=$('kind').value;
        populate(); renderTargets(machines); $('kind').value=kindFilter; taskChanged(); SuiteHardware.set('am',device,targetCards());
        updateLocation(); renderProfiles(); showInstallations();
        clearTimeout(installTimer); installTimer=setTimeout(checkInstallations,0);
        return;
      }
      drafts.delete(draftKey); dirty=false;
      const savedName=answer.name || name;
      const known=inventories.get(activeNode);
      const row=(known.models || []).find(one=>one.name === savedName);
      if (row) Object.assign(row,settings);
      else (known.models ||= []).push({...settings,name:savedName});
      populate(savedName); updateLocation(); renderProfiles(); $('saved').textContent='Saved.';
    } finally { busy=false; $('controls').disabled=!available(activeNode); updateSummary(); }
  }));
  $('retry').addEventListener('click',safe(refresh));
  $('install-retry').addEventListener('click',checkInstallations);
  $('test-button').addEventListener('click',safe(runTest));
  window.addEventListener('beforeunload',event=>{
    if (!root.isConnected || !dirty && !drafts.size) return;
    event.preventDefault(); event.returnValue='';
  });
  refresh().catch(error=>{ $('load-error').hidden=false; $('load-error-text').textContent=error.message; });
})();
