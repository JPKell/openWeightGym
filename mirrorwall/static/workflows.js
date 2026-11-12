/* Integrated Model studio, using authenticated workflow APIs and page-memory drafts. */
(() => {
  'use strict';
  const root=document.getElementById('image-studio'); if(!root) return;
  const $=id=>document.getElementById('studio-'+id), field=key=>document.getElementById('workflow-'+key);
  const manage=root.dataset.manage==='true', params=new URLSearchParams(location.search);
  const drafts=new Map(), histories=new Map(), selections=new Map();
  const keys=['width','height','n','upscale','steps','seed','sampler','scheduler','guidance','cfg_scale','reference_megapixels','strength'];
  let library={models:[],presets:[],can_manage:false}, models=[], selected=null, workflowID=null, saving=false, historyEpoch=0;
  let inventoryReady=!manage, workflowsReady=false, loading=false;
  const copy=value=>structuredClone(value);
  const template=id=>document.getElementById(id).content.firstElementChild.cloneNode(true);
  const text=(node,selector,value)=>{node.querySelector(selector).textContent=value;};
  const announce=value=>{ $('status').textContent=value; };
  const error=(value,save=false)=>{ const box=$(save?'save-error':'error'); box.hidden=!value; box.textContent=value || ''; };
  async function api(path,body) {
    const response=await fetch('/mirrorwall-api/'+path,{method:body?'POST':'GET',cache:'no-store',
      headers:body?{'content-type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
    if(response.redirected) { location.href='/sign-in'; throw new Error('Please sign in again.'); }
    const value=await response.json(); if(!response.ok) throw new Error(value.error?.message || 'Image workflows are unavailable.'); return value;
  }
  const current=()=>library.presets.find(row=>row.id===workflowID);
  const modelKey=m=>m?.key || m?.id;
  const catalogueModel=m=>library.models.find(row=>m.model_id && row.model_id?row.model_id===m.model_id:row.id===(m.workflow_id || m.id));
  const key=()=>selected?(selected.model_id || selected.id)+'/'+(workflowID || 'new'):null;
  function available() {
    if(!selected) return null;
    return catalogueModel(selected);
  }
  function profile() { return selected?.image_workflow || {}; }
  function belongs(row) { return selected && (row.model_id && selected.model_id?row.model_id===selected.model_id:row.model===selected.id); }
  function baseline() {
    const row=current(), p=profile();
    const result={name:row?.name || '',published:Boolean(row?.published),settings:{
      ...SuiteImageProfiles.spec(p.family).defaults,reference_megapixels:p.reference_megapixels || 1,strength:.75,negative_prompt:'',
      ...p.defaults,...row?.settings,loras:copy(row?.settings.loras || Object.fromEntries(Object.entries(p.loras || {}).map(([name,one])=>[name,one.scale ?? 0])))}};
    const rules=SuiteImageProfiles.spec(p.family), settings=result.settings;
    if(!rules.guidance) delete settings.guidance;
    if(!rules.negative) {delete settings.cfg_scale; delete settings.negative_prompt;}
    if(!rules.strength || p.limits?.max_references===0) delete settings.strength;
    if(p.limits?.max_references===0) delete settings.reference_megapixels;
    if(!p.upscaling) delete settings.upscale;
    if(!Object.keys(p.loras || {}).length) delete settings.loras;
    return result;
  }
  function read() {
    const old=drafts.get(key()) || baseline(), settings={...old.settings};
    keys.forEach(name=>{ const input=field(name); delete settings[name]; if(!input.disabled) settings[name]=input.tagName==='SELECT'?input.value:Number(input.value); });
    delete settings.negative_prompt;
    if(!field('negative_prompt').disabled) settings.negative_prompt=field('negative_prompt').value;
    const loras=Object.fromEntries([...$('loras').querySelectorAll('[data-workflow-lora]')].map(input=>[input.dataset.workflowLora,Number(input.value)]));
    if(Object.keys(loras).length) settings.loras=loras; else delete settings.loras;
    return {name:field('name').value,published:field('shared').checked,settings};
  }
  function remember() { if(selected) drafts.set(key(),read()); }
  const equal=(a,b)=>JSON.stringify(sort(a))===JSON.stringify(sort(b));
  function sort(value) { return value && typeof value==='object' && !Array.isArray(value)?Object.fromEntries(Object.keys(value).sort().map(key=>[key,sort(value[key])])):value; }
  function changed() {
    const row=current();
    const saved=drafts.get(key());
    if(!saved) return false;
    if(!row) return Boolean(saved.name) || !equal(saved.settings,baseline().settings);
    return saved.name!==row.name || saved.published!==row.published || !equal(saved.settings,baseline().settings);
  }
  function mark() {
    remember(); $('draft').textContent=changed()?'Unsaved changes':current()?'Saved':'New workflow';
    $('discard').disabled=!changed() || saving; error('',true);
  }
  function rows() { return library.presets.filter(belongs); }
  function showModels() {
    const search=$('model-search').value.toLowerCase(); $('model-list').replaceChildren();
    models.filter(m=>(m.id+' '+(m.node || '')+' '+m.image_workflow?.family).toLowerCase().includes(search)).forEach(m=>{
      const button=template('model-option'); button.dataset.model=m.id; button.setAttribute('aria-pressed',String(m===selected));
      if(m.node) button.dataset.node=m.node;
      text(button,'[data-name]',m.id); text(button,'[data-detail]',[SuiteImageProfiles.spec(m.image_workflow?.family).label,m.node].filter(Boolean).join(' · '));
      text(button,'.model-monogram',m.image_workflow?.family?.startsWith('qwen')?'Q':m.image_workflow?.family==='sdxl'?'SD':'F');
      const count=library.presets.filter(row=>row.model_id && m.model_id?row.model_id===m.model_id:row.model===m.id).length;
      text(button,'[data-count]',count); button.onclick=()=>select(m); button.disabled=saving || loading;
      $('model-list').append(button);
    });
    $('models-empty').hidden=Boolean($('model-list').children.length);
    $('models-empty').textContent=models.length?'No models match your search.':'No image models in this selection. Add an image model to begin.';
  }
  function showWorkflows() {
    const all=rows(), search=$('workflow-search').value.toLowerCase(), visibility=$('visibility').value;
    const matching=all.filter(w=>w.name.toLowerCase().includes(search) && (visibility==='all' || w.published===(visibility==='shared')));
    $('count').textContent=all.length+' workflow'+(all.length===1?'':'s')+' · 1 model setup'; $('workflow-list').replaceChildren();
    matching.forEach(w=>{
      const button=template('workflow-option'); button.dataset.workflow=w.id; button.setAttribute('aria-pressed',String(w.id===workflowID));
      text(button,'[data-name]',w.name); text(button,'[data-version]','v'+w.version);
      const settings={...SuiteImageProfiles.spec(profile().family).defaults,...profile().defaults,...w.settings};
      text(button,'[data-detail]',`${settings.width} × ${settings.height} · ${settings.steps} steps · ${settings.n} image${settings.n===1?'':'s'}`);
      text(button,'[data-sharing]',w.published?'↗ Shared workflow':'○ Personal workflow'); button.disabled=saving || loading;
      button.onclick=()=>{remember(); workflowID=w.id; showWorkflows(); showEditor();}; $('workflow-list').append(button);
    });
    $('workflow-empty').hidden=!selected || all.length>0; $('no-match').hidden=!all.length || matching.length>0;
  }
  function toggle(name,visible) {
    const input=field(name); input.disabled=!visible; input.closest('.col-md-6, .col-12').hidden=!visible;
  }
  function updateHistory() {
    const entry=(histories.get(workflowID) || []).find(row=>String(row.version)===$('history').value);
    $('history-detail').textContent=entry?`${entry.settings.width || 'Model default'} × ${entry.settings.height || 'Model default'} · ${entry.settings.steps || 'Default'} steps`:'Earlier revisions saved before version history was introduced are unavailable.';
    $('restore').disabled=!entry || saving;
  }
  async function showHistory() {
    const id=workflowID, epoch=++historyEpoch;
    $('history').replaceChildren(); $('restore').disabled=true; $('history-detail').textContent=id?'Loading versions…':'Save a workflow to start its history.';
    if(!id) return;
    try {
      if(!histories.has(id)) histories.set(id,(await api('presets/'+encodeURIComponent(id))).versions);
      if(epoch!==historyEpoch || id!==workflowID) return;
      histories.get(id).forEach(row=>$('history').add(new Option('Version '+row.version+' · '+row.name,row.version)));
      updateHistory();
    } catch(problem) {if(epoch===historyEpoch) $('history-detail').textContent=problem.message;}
  }
  function showEditor() {
    const p=profile(), rules=SuiteImageProfiles.spec(p.family), row=current();
    const draft=drafts.get(key()) || baseline();
    $('workflow-fields').disabled=!selected || saving || loading;
    field('name').value=draft.name; field('name').maxLength=100; field('shared').checked=draft.published;
    keys.forEach(name=>field(name).value=draft.settings[name] ?? '');
    field('negative_prompt').value=draft.settings.negative_prompt || '';
    toggle('guidance',rules.guidance); toggle('cfg_scale',rules.negative); toggle('strength',rules.strength && p.limits?.max_references!==0);
    toggle('reference_megapixels',p.limits?.max_references!==0); toggle('upscale',Boolean(p.upscaling));
    $('negative').hidden=!rules.negative; field('negative_prompt').disabled=!rules.negative;
    const limits=p.limits || {};
    for(const [name,limit] of [['width','max_width'],['height','max_height'],['steps','max_steps'],['n','max_images'],['upscale','max_upscale']]) field(name).max=limits[limit] || ({width:4096,height:4096,steps:200,n:64,upscale:1}[name]);
    field('width').step=field('height').step=rules.alignment;
    field('reference_megapixels').max=p.reference_megapixels || 1;
    $('reference-note').textContent=limits.max_references===0?'This profile generates from text only.':`Up to ${limits.max_references ?? (rules.strength?1:2)} references. `+(rules.strength?'Strength controls how much the starting image changes.':'References guide the composition.');
    $('loras').replaceChildren(); $('no-loras').hidden=Boolean(Object.keys(p.loras || {}).length);
    for(const [name,definition] of Object.entries(p.loras || {})) {
      const control=template('lora-control'), input=control.querySelector('input'); text(control,'[data-label]',name+' · LoRA scale');
      input.dataset.workflowLora=name; input.value=draft.settings.loras?.[name] ?? definition.scale ?? 0; $('loras').append(control);
    }
    $('owner').textContent=selected?'Uses '+selected.id:''; $('version').textContent=row?'Version '+row.version:'New workflow';
    $('editor-title').textContent=row?'Edit workflow':'Create workflow';
    field('shared').disabled=Boolean(row) || !library.can_manage;
    $('sharing-note').textContent=row?'Duplicate this workflow to change its visibility.':library.can_manage?'Shared workflows can be used by other authorized users.':'Personal workflows are visible only to you.';
    $('save').textContent=row?.published && !library.can_manage?'Save personal copy':row?'Save new version':'Save workflow';
    $('save').disabled=!selected || !available() || saving || loading; $('duplicate').disabled=!selected || !row || saving || loading;
    SuiteImageProfiles.help($('workflow-helper'),p.family,values=>{
      for(const [name,value] of Object.entries(values)) if(field(name) && !field(name).disabled && Number(value)<=Number(field(name).max || Infinity)) field(name).value=value;
      mark(); announce('Suggested settings applied to the draft. Review the model limits before saving.');
    });
    mark(); showHistory();
  }
  function select(m) {
    if(saving) return; remember(); if(selected) selections.set(selected.model_id || selected.id,workflowID); selected=m;
    const remembered=selections.get(m.model_id || m.id);
    workflowID=remembered===null?null:rows().find(w=>w.id===(remembered || params.get('workflow')))?.id || rows()[0]?.id || null;
    const next=new URL(location.href); next.searchParams.delete('new'); next.searchParams.set('model',m.id);
    if(m.node) next.searchParams.set('model_node',m.node); else next.searchParams.delete('model_node');
    history.replaceState(null,'',next);
    $('model-name').textContent=m.id; $('family').textContent=SuiteImageProfiles.spec(profile().family).label;
    $('model-detail').textContent=m.node?`${m.node} · ${m.flags?.model?.split('/').pop() || ''}`:'Image model';
    $('unavailable').hidden=Boolean(available()); $('unavailable').textContent='This model is unavailable in your current image catalogue. Saved drafts are kept; workflow saves need a reachable, permitted model.';
    $('new-workflow').disabled=!available();
    if(manage) { $('edit-model').disabled=false; document.dispatchEvent(new CustomEvent('weightroom:image-select',{detail:{name:m.id,node:m.node}})); }
    showModels(); showWorkflows(); showEditor();
  }
  function mapInventory(items,node) {
    return items.map(m=>({id:m.name,workflow_id:m.workflow_model || m.name,model_id:m.workflow_model_id || m.model_id || m.id,
      flags:m.flags,node:m.node || node,key:JSON.stringify([m.node || node,m.name]),
      image_workflow:{family:m.sdcpp.family,defaults:m.sdcpp.defaults,limits:m.sdcpp.limits,reference_megapixels:m.sdcpp.reference_megapixels,
        loras:m.sdcpp.loras,upscaling:Boolean(m.sdcpp.upscaler),exclusive:m.alone}}));
  }
  function populate() {
    if(!inventoryReady || !workflowsReady || saving) return;
    const previous=selected; if(previous) selections.set(previous.model_id || previous.id,workflowID); selected=null;
    const asked=models.find(m=>modelKey(m)===modelKey(previous)) || models.find(m=>m.id===(previous?.id || params.get('model')) &&
      (!params.get('model_node') || m.node===params.get('model_node'))) || models[0];
    if(asked) select(asked); else {
      $('model-name').textContent='Choose an image model'; $('family').textContent=''; $('model-detail').textContent='';
      $('new-workflow').disabled=true; $('unavailable').hidden=true;
      if(manage) { $('edit-model').disabled=true; document.dispatchEvent(new CustomEvent('weightroom:image-select',{detail:{name:null}})); }
      showModels(); showWorkflows(); showEditor();
    }
  }
  document.addEventListener('weightroom:image-inventory',event=>{
    remember(); loading=false; error(''); models=mapInventory(event.detail.models,event.detail.node); inventoryReady=true;
    if(manage) document.getElementById('mw-new').disabled=saving;
    for(const m of models) {const publicModel=catalogueModel(m); if(publicModel) m.image_workflow=publicModel.image_workflow;}
    populate();
  });
  document.addEventListener('weightroom:image-loading',()=>{
    remember(); loading=true; $('new-workflow').disabled=true;
    if(manage) { $('edit-model').disabled=true; document.getElementById('mw-new').disabled=true; }
    showModels(); showEditor();
  });
  document.addEventListener('weightroom:image-error',event=>{
    loading=false; if(manage) document.getElementById('mw-new').disabled=saving;
    populate(); error(event.detail.message);
  });
  document.addEventListener('weightroom:image-saved',async event=>{
    try { library=await api('workflows'); const m=models.find(row=>row.id===event.detail.name && row.node===event.detail.node);
      if(m) {const actual=catalogueModel(m); if(actual) m.image_workflow=actual.image_workflow; select(m);}
      announce('Model setup saved. Workflow settings are preserved.'); } catch(problem) {error(problem.message);}
  });
  $('new-workflow').onclick=()=>{remember(); workflowID=null; if(!drafts.has(key())) drafts.set(key(),baseline()); showWorkflows(); showEditor(); field('name').focus();};
  $('duplicate').onclick=()=>{const draft=read(); remember(); workflowID=null; draft.name=(draft.name+' copy').slice(0,100); draft.published=false; drafts.set(key(),draft); showWorkflows(); showEditor(); field('name').focus();};
  $('discard').onclick=()=>{drafts.delete(key()); showEditor();};
  $('workflow-form').addEventListener('input',mark); $('workflow-form').addEventListener('change',mark);
  $('history').onchange=updateHistory;
  $('restore').onclick=()=>{const entry=histories.get(workflowID)?.find(row=>String(row.version)===$('history').value); if(!entry) return;
    const draft=baseline(); draft.settings={...draft.settings,...copy(entry.settings)}; drafts.set(key(),draft); showEditor(); announce('Earlier settings loaded as a draft. Saving creates a new version.');};
  $('workflow-form').onsubmit=async event=>{
    event.preventDefault(); if(saving || loading || !selected || !available()) return;
    const draft=read(), row=current(), model=selected, originalKey=key();
    if(!draft.name.trim()) {error('Enter a workflow name.',true); return;}
    saving=true; $('save').disabled=true; $('workflow-fields').disabled=true; $('duplicate').disabled=true; $('new-workflow').disabled=true; showModels(); showWorkflows();
    if(manage) { $('edit-model').disabled=true; document.getElementById('mw-new').disabled=true; document.getElementById('mw-node').disabled=true; }
    try {
      const ownCopy=row?.published && !library.can_manage;
      const saved=await api('presets',{...draft,model:available().id,id:ownCopy?undefined:row?.id,version:ownCopy?undefined:row?.version,published:ownCopy?false:draft.published});
      const index=library.presets.findIndex(w=>w.id===saved.id); if(index<0) library.presets.push(saved); else library.presets[index]=saved;
      histories.delete(saved.id); drafts.delete(originalKey); workflowID=saved.id;
      announce('Workflow saved. '+saved.name+' · version '+saved.version+'.'); error('');
    } catch(problem) { error(problem.message,true); }
    finally {saving=false; selected=model; $('workflow-fields').disabled=false; $('save').disabled=!available(); $('new-workflow').disabled=!available(); showModels(); showWorkflows();
      if(manage) { $('edit-model').disabled=!selected; document.getElementById('mw-new').disabled=false; document.getElementById('mw-node').disabled=false; }
      if(!drafts.has(originalKey)) showEditor(); else {field('shared').disabled=Boolean(current()) || !library.can_manage; $('duplicate').disabled=!current();}}
  };
  $('model-search').oninput=showModels; $('workflow-search').oninput=showWorkflows; $('visibility').onchange=showWorkflows;
  if(manage) {
    $('edit-model').onclick=()=>document.dispatchEvent(new CustomEvent('weightroom:image-edit',{detail:{name:selected.id,node:selected.node}}));
    $('close-model').onclick=()=>document.getElementById('model-dialog').close();
  }
  api('workflows').then(value=>{
    remember(); library=value; workflowsReady=true;
    if(!manage) models=library.models;
    else for(const m of models) {const actual=catalogueModel(m); if(actual) m.image_workflow=actual.image_workflow;}
    populate();
  }).catch(problem=>{workflowsReady=true; populate(); error(problem.message);});
})();
