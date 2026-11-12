/* Standalone image library proposals. No network calls or live configuration changes. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const clone = value => structuredClone(value);
  const storageKey = 'owg-image-designs-v1';
  const sampleModel = (id, name, family, node = 'atlas') => ({id, name, family, node,
    diffusion: family === 'sdxl' ? 'sdxl-base.safetensors' : family === 'flux2-dev' ? 'flux2-dev-Q8.gguf' : 'flux2-klein-Q4.gguf',
    text_encoder: family === 'sdxl' ? '' : 'qwen-encoder.gguf', vae: 'ae.safetensors',
    clip_l: family === 'sdxl' ? 'clip-l.safetensors' : '', clip_g: family === 'sdxl' ? 'clip-g.safetensors' : '',
    upscaler: '4x-ultrasharp.pth', gpu: 'GPU 0', split: 'single', ram: 8192, threads: 4,
    staged: true, alone: true, budgets: '', weights: '', max_width: 2048, max_height: 2048,
    max_steps: 60, max_images: 4, max_references: 2, max_pixels: 4194304, max_upscale: 2,
    reference_megapixels: 1, 'lora-name': 'Soft light', 'lora-file': 'soft-light.safetensors'});
  const defaults = model => {
    const size = Math.max(256, Math.floor(Math.min(1024, Math.sqrt(model.max_pixels)) / 16) * 16);
    return {width: Math.min(size, model.max_width), height: Math.min(size, model.max_height),
    n: 1, steps: Math.min(6, model.max_steps), seed: -1, upscale: 1, guidance: 3.5, cfg_scale: 7,
    strength: .75, reference_megapixels: model.reference_megapixels,
    sampler: 'euler', scheduler: model.family === 'sdxl' ? 'karras' : 'flux2', negative_prompt: '', lora: .6};
  };
  const sample = () => {
    const models = [sampleModel('flux', 'FLUX.2 Klein', 'flux2-klein'), sampleModel('sdxl', 'SDXL Studio', 'sdxl', 'nova'),
      sampleModel('dev', 'FLUX.2 Dev', 'flux2-dev', 'cedar'), sampleModel('empty', 'SDXL Sketch', 'sdxl')];
    const definitions = [
      ['flux','Quick exploration',false,{steps:4,width:768,height:768}],
      ['flux','Product stills',true,{steps:12}], ['flux','Portrait studies',false,{width:768,height:1024,steps:8,lora:.75}],
      ['flux','Wide editorial',true,{width:1536,height:864,steps:12}],
      ['sdxl','Soft portraits',true,{steps:28,sampler:'dpmpp2m',negative_prompt:'blurry, low quality, artifacts'}],
      ['sdxl','Concept sketches',false,{steps:16,width:768,height:768}],
      ['dev','Campaign artwork',true,{steps:30,guidance:3.5}], ['dev','Material studies',false,{steps:20,guidance:2.5}],
    ];
    const workflows = definitions.map(([model,name,shared,settings], i) => {
      const values = {...defaults(models.find(m => m.id === model)), ...settings};
      const history = [{version:1,name,shared,settings:{...values,steps:Math.max(1,values.steps-2)}}];
      history.push({version:2,name,shared,settings:values});
      return {id:'workflow-'+i,model,name,shared,settings:values,history};
    });
    return {schema:1,models,workflows};
  };
  let data;
  try { data = JSON.parse(localStorage.getItem(storageKey)); } catch { /* Storage can be disabled. */ }
  if (!data || data.schema !== 1 || !Array.isArray(data.models) || !data.models.length || !Array.isArray(data.workflows)) data = sample();
  let modelID = data.models[0].id, workflowID = data.workflows.find(w => w.model === modelID)?.id || null;
  let layout = 'studio', editingModel = null;
  const drafts = new Map();
  const model = () => data.models.find(m => m.id === modelID);
  const workflow = () => data.workflows.find(w => w.id === workflowID);
  const draftKey = () => workflowID || 'new:'+modelID;
  const fields = ['width','height','n','upscale','steps','seed','sampler','scheduler','guidance','cfg_scale','reference_megapixels','strength','negative_prompt'];
  const modelFields = ['name','family','diffusion','text_encoder','vae','clip_l','clip_g','upscaler','node','gpu','split',
    'ram','threads','budgets','weights','max_width','max_height','max_steps','max_images','max_references','max_pixels',
    'max_upscale','reference_megapixels','lora-name','lora-file'];
  function persist() {
    try { localStorage.setItem(storageKey, JSON.stringify(data)); return true; }
    catch { $('status').textContent += ' Browser storage is unavailable; changes last for this visit.'; return false; }
  }
  function announce(text) { $('status').textContent = text; }
  const node = (template) => $(template).content.firstElementChild.cloneNode(true);
  function set(node, selector, text) { node.querySelector(selector).textContent = text; }
  function readDraft() {
    const settings = Object.fromEntries(fields.map(key => [key, ['sampler','scheduler','negative_prompt'].includes(key) ? $('wf-'+key).value : Number($('wf-'+key).value)]));
    settings.lora = Number($('workflow-lora')?.value ?? 0);
    return {name:$('workflow-name').value,shared:$('wf-shared').checked,settings};
  }
  function remember() { drafts.set(draftKey(), readDraft()); }
  function blankDraft() { return {name:'',shared:false,settings:defaults(model())}; }
  function currentDraft() { const w=workflow(); return drafts.get(draftKey()) || (w ? {name:w.name,shared:w.shared,settings:clone(w.settings)} : blankDraft()); }
  function changed() {
    const draft=readDraft(), w=workflow();
    const baseline=w || blankDraft();
    return draft.name!==baseline.name || draft.shared!==baseline.shared || Object.entries(draft.settings).some(([key,value])=>value!==baseline.settings[key]);
  }
  function markDraft() {
    remember(); $('draft-state').textContent = changed() ? 'Unsaved changes' : workflow() ? 'Saved' : 'New workflow';
    $('discard').disabled = !changed();
  }
  function showModels() {
    const search = $('model-search').value.toLowerCase();
    $('model-list').replaceChildren();
    data.models.filter(m => (m.name+' '+m.family+' '+m.node).toLowerCase().includes(search)).forEach(m => {
      const button = node('model-option'); button.dataset.model=m.id;
      button.setAttribute('aria-pressed',String(m.id === modelID));
      set(button,'[data-name]',m.name); set(button,'[data-detail]',m.family+' · '+m.node);
      set(button,'.model-monogram',m.family === 'sdxl' ? 'SD' : 'F');
      const count=data.workflows.filter(w => w.model === m.id).length;
      set(button,'[data-count]',String(count)); button.setAttribute('aria-label',m.name+' · '+count+' workflows');
      button.onclick=()=>{ remember(); modelID=m.id; workflowID=data.workflows.find(w=>w.model===m.id)?.id || null; render(); };
      $('model-list').append(button);
    });
    $('models-empty').hidden=Boolean($('model-list').children.length);
  }
  function showWorkflows() {
    const all=data.workflows.filter(w=>w.model===modelID), search=$('workflow-search').value.toLowerCase(), visibility=$('sharing-filter').value;
    const rows=all.filter(w=>w.name.toLowerCase().includes(search) && (visibility==='all' || w.shared===(visibility==='shared')));
    $('workflow-count').textContent=all.length+' workflow'+(all.length===1?'':'s')+' · 1 model setup';
    $('workflow-list').replaceChildren(); $('matrix-rows').replaceChildren();
    rows.forEach(w=>{
      const button=node('workflow-option'); button.dataset.workflow=w.id; button.setAttribute('aria-pressed',String(w.id===workflowID));
      set(button,'[data-name]',w.name); set(button,'[data-version]','v'+w.history.length);
      set(button,'[data-detail]',`${w.settings.width} × ${w.settings.height} · ${w.settings.steps} steps · ${w.settings.n} image${w.settings.n===1?'':'s'}`);
      set(button,'[data-sharing]',w.shared?'↗ Shared workflow':'○ Personal workflow');
      button.onclick=()=>selectWorkflow(w.id); $('workflow-list').append(button);
      const row=node('matrix-row'); row.classList.toggle('is-selected',w.id===workflowID); row.dataset.workflow=w.id;
      set(row,'[data-open]',w.name); row.querySelector('[data-open]').onclick=()=>selectWorkflow(w.id);
      set(row,'[data-sharing]',w.shared?'Shared':'Personal'); set(row,'[data-size]',w.settings.width+' × '+w.settings.height);
      set(row,'[data-steps]',w.settings.steps); set(row,'[data-sampling]',w.settings.sampler+' / '+w.settings.scheduler);
      set(row,'[data-upscale]',w.settings.upscale+'×'); set(row,'[data-version]','v'+w.history.length); $('matrix-rows').append(row);
    });
    $('workflows-empty').hidden=all.length>0;
    $('search-empty').hidden=!all.length || rows.length>0;
    $('workflow-list').hidden=layout==='matrix'; $('matrix-wrap').hidden=layout!=='matrix' || !all.length;
    if(!rows.length) $('matrix-rows').append(node('matrix-empty'));
  }
  function selectWorkflow(id) { remember(); workflowID=id; render(); if(innerWidth<760) $('editor-panel').scrollIntoView({block:'start'}); }
  function toggleField(key, visible) {
    const input=$('wf-'+key); input.disabled=!visible; input.closest('.col-md-6, .col-12').hidden=!visible;
  }
  function showEditor() {
    const m=model(), w=workflow(), draft=currentDraft();
    $('workflow-name').value=draft.name;
    fields.forEach(key=>{ $('wf-'+key).value=draft.settings[key]; });
    $('wf-shared').checked=draft.shared;
    $('workflow-owner').textContent='Uses '+m.name+' · '+m.node+' · '+m.gpu;
    $('editor-heading').textContent=w ? 'Edit workflow' : 'Create workflow';
    $('version-badge').textContent=w?'Version '+w.history.length:'New workflow';
    $('save-workflow').textContent=w?'Save new version':'Create workflow'; $('save-top').textContent=w?'Save version':'Create workflow'; $('duplicate').disabled=!w;
    for(const [key,limit] of [['width','max_width'],['height','max_height'],['steps','max_steps'],['n','max_images'],['upscale','max_upscale'],['reference_megapixels','reference_megapixels']]) $('wf-'+key).max=m[limit];
    toggleField('guidance',m.family==='flux2-dev'); toggleField('cfg_scale',m.family==='sdxl');
    toggleField('strength',m.family==='sdxl' && m.max_references>0);
    toggleField('reference_megapixels',m.max_references>0); toggleField('upscale',Boolean(m.upscaler));
    $('wf-negative_prompt').disabled=m.family!=='sdxl'; $('negative-field').hidden=m.family!=='sdxl';
    $('reference-note').textContent=m.max_references===0 ? 'This model does not accept reference images.' :
      `Up to ${m.max_references} references · ${m.reference_megapixels} MP maximum each. `+(m.family==='sdxl'?'Strength controls how much the start image changes.':'Use references to guide the composition.');
    $('lora-fields').replaceChildren(); $('no-loras').hidden=Boolean(m['lora-name']);
    if(m['lora-name']) {
      const control=node('lora-control'), input=control.querySelector('input');
      set(control,'[data-label]',m['lora-name']+' · strength'); input.id='workflow-lora'; input.value=draft.settings.lora;
      control.htmlFor=input.id; $('lora-fields').append(control);
    }
    $('history').replaceChildren();
    for(const entry of [...(w?.history || [])].reverse()) $('history').add(new Option('Version '+entry.version+' · '+entry.name,entry.version));
    $('history').disabled=!w; $('restore').disabled=!w; showHistory(); markDraft();
  }
  function showHistory() {
    const entry=workflow()?.history.find(h=>h.version===Number($('history').value));
    $('history-detail').textContent=entry?`${entry.settings.width} × ${entry.settings.height} · ${entry.settings.steps} steps · ${entry.shared?'Shared':'Personal'}`:'Save a workflow to start its history.';
  }
  function render() {
    showModels(); showWorkflows();
    const m=model(); $('model-name').textContent=m.name; $('model-family').textContent=m.family;
    $('model-detail').textContent=`${m.diffusion} · ${m.node} / ${m.gpu} · ${m.max_width} × ${m.max_height} max · ${m.max_steps} steps max`;
    showEditor();
  }
  function createWorkflow() {
    remember(); workflowID=null; drafts.set(draftKey(),blankDraft()); render();
    $('workflow-name').focus();
  }
  function problem(settings, m) {
    if(settings.width>m.max_width || settings.height>m.max_height || settings.width*settings.height>m.max_pixels) return 'Output size exceeds the model limits or pixel budget.';
    if(settings.steps>m.max_steps || settings.n>m.max_images) return 'Steps or image count exceed the model limits.';
    if(m.upscaler && settings.upscale>m.max_upscale) return 'Output enlargement exceeds the model limit.';
    if(m.max_references>0 && settings.reference_megapixels>m.reference_megapixels) return 'Reference size exceeds the model limit.';
    return '';
  }
  $('workflow-form').addEventListener('input',markDraft);
  $('workflow-form').addEventListener('change',markDraft);
  $('workflow-form').onsubmit=event=>{
    event.preventDefault(); const draft=readDraft();
    if(!draft.name.trim()) { $('workflow-name').setCustomValidity('Enter a workflow name.'); $('workflow-name').reportValidity(); return; }
    const error=problem(draft.settings,model()); if(error) { announce(error); return; }
    if(data.workflows.some(w=>w.model===modelID && w.id!==workflowID && w.name.toLowerCase()===draft.name.trim().toLowerCase())) { announce('A workflow with this name already exists for this model. Choose another name.'); return; }
    draft.name=draft.name.trim(); let w=workflow(); const previousKey=draftKey();
    if(!w) { w={id:crypto.randomUUID(),model:modelID,history:[]}; data.workflows.push(w); }
    Object.assign(w,clone(draft)); w.history.push({version:w.history.length+1,...clone(draft)});
    drafts.delete(previousKey); workflowID=w.id;
    announce(w.name+' saved · version '+w.history.length+'. Model setup is unchanged.'); persist(); render();
  };
  $('workflow-name').addEventListener('input',()=> $('workflow-name').setCustomValidity(''));
  $('new-workflow').onclick=createWorkflow;
  document.querySelector('[data-create]').onclick=createWorkflow;
  $('duplicate').onclick=()=>{
    const draft=readDraft(); remember(); workflowID=null; draft.name+=' copy'; draft.shared=false;
    drafts.set(draftKey(),draft); render(); $('workflow-name').focus();
  };
  $('discard').onclick=()=>{ drafts.delete(draftKey()); showEditor(); announce('Draft reset to '+(workflow()?'the saved version.':'model defaults.')); };
  $('history').onchange=showHistory;
  $('restore').onclick=()=>{
    const entry=workflow().history.find(h=>h.version===Number($('history').value));
    drafts.set(draftKey(),{name:entry.name,shared:entry.shared,settings:clone(entry.settings)});
    showEditor(); announce('Version '+entry.version+' loaded as a draft. Save to create a new version.');
  };
  $('model-search').oninput=showModels; $('workflow-search').oninput=showWorkflows; $('sharing-filter').onchange=showWorkflows;
  $('clear-search').onclick=()=>{ $('workflow-search').value=''; $('sharing-filter').value='all'; showWorkflows(); };
  const descriptions={
    studio:['FOCUSED / MODEL FIRST','A model navigator, a workflow list and a dedicated editor. Recommended for everyday setup and tuning.'],
    board:['EXPLORATORY / WORKFLOW FIRST','A visual collection of workflows with the selected recipe alongside. Useful when browsing by creative intent.'],
    matrix:['PRECISE / COMPARE & TUNE','Compare settings across workflows, then edit the selected recipe below. Useful for checking consistency and differences.'],
  };
  function setLayout(value) {
    layout=value; $('workspace').dataset.layout=value;
    document.querySelectorAll('[data-design]').forEach(button=>{ const active=button.dataset.design===value; button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active)); });
    $('design-intent').textContent=descriptions[value][0]; $('design-description').textContent=descriptions[value][1];
    showWorkflows(); history.replaceState(null,'','#'+value);
  }
  document.querySelectorAll('[data-design]').forEach(button=>button.onclick=()=>setLayout(button.dataset.design));
  const files={diffusion:['flux2-klein-Q4.gguf','flux2-dev-Q8.gguf','sdxl-base.safetensors'],text_encoder:['qwen-encoder.gguf'],
    vae:['ae.safetensors','sdxl-vae.safetensors'],clip_l:['clip-l.safetensors'],clip_g:['clip-g.safetensors'],upscaler:['4x-ultrasharp.pth']};
  for(const [key,values] of Object.entries(files)) values.forEach(value=>$('setup-'+key).add(new Option(value,value)));
  // Empty values reflect optional model components in the existing image model form.
  for(const key of [...Object.keys(files),'lora-file']) $('setup-'+key).options[0].value='';
  function modelFamily() {
    const family=$('setup-family').value;
    for(const key of ['text_encoder','clip_l','clip_g']) {
      const active=key==='text_encoder'?family!=='sdxl':family==='sdxl';
      $('setup-'+key).closest('.col-12').hidden=!active; $('setup-'+key).disabled=!active;
    }
  }
  function openModel(m) {
    remember(); editingModel=m?.id || null;
    const source=m || sampleModel('', '', 'flux2-klein');
    modelFields.forEach(key=>{ $('setup-'+key).value=source[key]; });
    for(const key of ['staged','alone']) $('setup-'+key).checked=source[key];
    $('model-dialog-title').textContent=m?'Edit model setup':'Add image model'; $('model-error').textContent='';
    modelFamily(); $('model-dialog').showModal(); $('setup-name').focus();
  }
  $('setup-family').onchange=modelFamily;
  $('add-model').onclick=()=>openModel(null); $('edit-model').onclick=()=>openModel(model());
  $('cancel-model').onclick=()=> $('model-dialog').close();
  $('model-form').onsubmit=event=>{
    event.preventDefault(); const values=Object.fromEntries(modelFields.map(key=>[key,$('setup-'+key).value]));
    for(const key of ['ram','threads','max_width','max_height','max_steps','max_images','max_references','max_pixels','max_upscale','reference_megapixels']) values[key]=Number(values[key]);
    for(const key of ['staged','alone']) values[key]=$('setup-'+key).checked;
    const error=text=>{ $('model-error').textContent=text; };
    values.name=values.name.trim();
    if(!values.name) return error('Enter a model name.');
    if(data.models.some(m=>m.id!==editingModel && m.name.toLowerCase()===values.name.toLowerCase())) return error('That model name already exists.');
    const required=values.family==='sdxl'?['diffusion','vae','clip_l','clip_g']:['diffusion','vae','text_encoder'];
    if(required.some(key=>!values[key])) return error('Select the diffusion weights, VAE and encoders required by this family.');
    const gpuCount=values.gpu==='GPU 0 + GPU 1'?2:1;
    if(gpuCount===2 && values.split==='single') return error('Choose a multi-GPU split strategy for two GPUs.');
    if(Boolean(values['lora-name'].trim())!==Boolean(values['lora-file'])) return error('Select both an adapter name and file, or clear both.');
    const affected=data.workflows.filter(w=>w.model===editingModel && problem(w.settings,values));
    if(affected.length) return error('These limits conflict with '+affected.map(w=>w.name).join(', ')+'. Adjust those workflows first.');
    for(const key of ['text_encoder','clip_l','clip_g']) if($('setup-'+key).disabled) values[key]='';
    let m=data.models.find(m=>m.id===editingModel);
    if(m && m.family!==values.family && data.workflows.some(w=>w.model===m.id)) return error('Create another model to change family while preserving existing workflows.');
    if(!m) { m={id:crypto.randomUUID()}; data.models.push(m); }
    Object.assign(m,values); modelID=m.id; workflowID=data.workflows.find(w=>w.model===m.id)?.id || null;
    $('model-dialog').close(); announce(m.name+' setup saved. '+data.workflows.filter(w=>w.model===m.id).length+' workflows retained.'); persist(); render();
  };
  $('theme').onclick=()=>{
    const input=document.querySelector('[data-theme-form] [name=mode]');
    input.value=document.documentElement.dataset.bsTheme==='dark'?'light':'dark'; input.dispatchEvent(new Event('change',{bubbles:true}));
  };
  document.addEventListener('weightroom:theme',()=>{ $('theme').textContent=document.documentElement.dataset.bsTheme==='dark'?'Light mode':'Dark mode'; });
  $('reset').onclick=()=>{ data=sample(); drafts.clear(); modelID=data.models[0].id; workflowID=data.workflows[0].id;
    $('model-search').value=''; $('workflow-search').value=''; $('sharing-filter').value='all'; announce('Sample library reset.'); persist(); render(); };
  render(); setLayout(descriptions[location.hash.slice(1)]?location.hash.slice(1):'studio');
})();
