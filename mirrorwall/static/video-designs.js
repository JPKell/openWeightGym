/* Offline video proposals. Sample state never submits work or contacts a service. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const sample = JSON.parse($('video-samples').textContent);
  const form = $('video-form'), drafts = new Map(), clips = [], selected = {id: null};
  let model = sample.models[0], active = false, timer, counter = 0, surface = 'generation';
  const modes = [['text','Text'],['start','Start image'],['ends','Start and end images'],['references','Reference images'],['control','Control video']];
  const message = text => { $('video-message').textContent=text; $('video-message').hidden=!text; };
  const values = () => Object.fromEntries([...form.elements].filter(e => e.name).map(e => [e.name,e.type==='checkbox'?e.checked:e.value]).concat([['prompt',$('video-prompt').value],['negative_prompt',$('video-negative').value]]));
  function restore(value) {
    for (const [key, value_] of Object.entries(value)) {
      const e = form.elements.namedItem(key);
      if (e) { if (e.type==='checkbox') e.checked=value_; else e.value=value_; }
    }
    $('video-prompt').value=value.prompt || '';
    $('video-negative').value=value.negative_prompt || '';
  }
  function length() {
    const fps=model.family==='minimax-h3'?24:Number($('video-fps').value), seconds=Number($('video-seconds').value);
    $('video-fps').value=fps;
    const target=Math.floor(seconds*fps), frames=model.family==='minimax-h3'?Math.max(5,Math.ceil((target-5)/17)*17+5):Math.max(5,Math.floor((target-1)/4)*4+1);
    $('video-length').textContent=`${frames} frames · ${fps} fps · ${(frames/fps).toFixed(3)} s effective`;
  }
  function inputs() {
    const mode=$('video-mode').value;
    $('video-inputs').hidden=mode==='text';
    for (const id of ['start','end','references','control']) $('video-'+id+'-row').hidden=!(mode===id || (mode==='ends' && ['start','end'].includes(id)));
    $('video-vace-row').hidden=mode!=='control';
  }
  function choose(id) {
    if (model) drafts.set(model.id,values());
    model=sample.models.find(m=>m.id===id);
    $('video-model').value=id;
    $('video-model-note').textContent=model.ready?'Sample qualified profile · exclusive GPU · batch priority':'Sample unqualified profile · generation unavailable';
    const available=modes.filter(([id_])=>id_==='text' || id_==='start' && model.features.includes('init_image') || id_==='ends' && model.features.includes('end_image') || id_==='references' && model.features.includes('ref_images') || id_==='control' && model.features.includes('vace'));
    $('video-mode').replaceChildren(...available.map(([id_,label])=>new Option(label,id_)));
    for (const [id_,feature] of [['soundtrack','soundtrack'],['hires','hires'],['high-noise','high_noise']]) $(id_==='high-noise'?'video-high-noise':`video-${id_}-row`).hidden=!model.features.includes(feature);
    restore(drafts.get(id) || {model:id,seconds:5,fps:model.family==='minimax-h3'?24:16,steps:20,guidance:6,seed:-1,size:'832x480',sampler:'euler',scheduler:'discrete',prompt:'A tiger cub shelters beneath a banana leaf. Rain falls softly; the camera moves slowly closer.'});
    $('video-generate').disabled=!model.ready || active;
    $('video-profile-name').textContent=model.label;
    $('video-profile-status').textContent=model.ready?'Sample ready: complete components, WebM enabled, measured envelope':'Not runnable in this sample: component conversion or host RAM qualification required';
    $('video-profile-evidence').replaceChildren();
    for (const [key,value] of [['Node','compute'],['Placement','Exclusive · one GPU'],['Load peak','Illustrative: 9.2 GiB'],['Sampling peak','Illustrative: 12.8 GiB'],['VAE decode peak','Illustrative: 11.6 GiB'],['Host RAM','Illustrative: 18.4 GiB'],['Time per output second','Illustrative: 28 seconds']]) {
      const dt=document.createElement('dt'),dd=document.createElement('dd'); dt.textContent=key;dd.textContent=value;$('video-profile-evidence').append(dt,dd);
    }
    $('mw-name').value=model.id; $('mw-family').value=model.family;
    inputs(); length();
    for (const e of $('video-model-list').children) e.setAttribute('aria-pressed',String(e.dataset.model===id));
  }
  function gallery() {
    const visible=clips.filter(c=>(!$('video-filter-model').value || c.model===$('video-filter-model').value) && (!$('video-filter-mode').value || c.mode===$('video-filter-mode').value) && (!$('video-filter-date').value || c.created>=Date.parse($('video-filter-date').value)));
    visible.sort((a,b)=>($('video-sort').value==='newest'?-1:1)*(a.created-b.created));
    const old = new Map([...$('video-gallery').children].map(e=>[e.dataset.id,e]));
    for (const clip of visible) {
      const button=old.get(clip.id) || $('video-clip-template').content.firstElementChild.cloneNode(true);
      button.dataset.id=clip.id;button.querySelector('img').src=sample.poster;
      button.querySelector('[data-title]').textContent=clip.title;
      button.querySelector('[data-detail]').textContent=`${clip.model} · sample ${clip.mode}`;
      button.setAttribute('aria-selected',String(selected.id===clip.id));
      if (!old.has(clip.id)) button.addEventListener('click',()=>select(clip.id));
      $('video-gallery').append(button);old.delete(clip.id);
    }
    for (const e of old.values()) e.remove();
    $('video-library-empty').hidden=!!visible.length;
  }
  function select(id) {
    selected.id=id;const clip=clips.find(c=>c.id===id);
    $('video-result-title').textContent=clip.title;
    $('video-viewer-note').textContent='Illustrative poster · this sample has no generated video or soundtrack.';
    $('video-result-settings').textContent=JSON.stringify(clip.settings,null,2);gallery();
  }
  for (const m of sample.models) {
    $('video-model').add(new Option(m.label,m.id));$('video-filter-model').add(new Option(m.label,m.id));
    const button=$('model-option').content.firstElementChild.cloneNode(true);
    button.dataset.model=m.id;button.querySelector('[data-name]').textContent=m.label;
    button.querySelector('[data-detail]').textContent=m.ready?'compute · sample ready':'studio · not runnable';
    button.querySelector('[data-count]').textContent=m.ready?'Ready':'Needs review';
    button.addEventListener('click',()=>choose(m.id));$('video-model-list').append(button);
  }
  for (const select_ of $('mw-model-form').querySelectorAll('select[id^="mw-"]')) {
    if (['mw-family','mw-split'].includes(select_.id) || select_.id.includes('default-')) continue;
    select_.add(new Option('Sample component · header identified','sample.gguf'));
  }
  $('mw-model-form').addEventListener('submit',e=>{e.preventDefault();$('mw-saved').textContent='Sample setup saved in this tab.';});
  $('video-model').addEventListener('change',()=>choose($('video-model').value));
  $('video-mode').addEventListener('change',inputs);
  form.addEventListener('input',length);
  form.addEventListener('submit',e=>{
    e.preventDefault();if (active) return;
    if ($('video-demo-state').value!=='ready') { message('Choose Sample library to simulate a run.');return; }
    active=true;$('video-generate').disabled=true;$('video-stop').disabled=false;
    const settings=values(), current=model;let stage=0;
    const stages=['Queued · position 1','Loading','Encoding prompt','Sampling · step 8 / 20','Decoding','Encoding container','Saving'];
    timer=setInterval(()=>{
      $('video-jobs').textContent=stages[stage];$('video-progress').value=stage*100/stages.length;
      if (++stage===stages.length) {
        clearInterval(timer);active=false;$('video-stop').disabled=true;$('video-generate').disabled=!model.ready;
        const clip={id:`demo-${++counter}`,title:`Sample run ${counter}`,model:current.id,mode:$('video-mode').value,settings,created:Date.now()};clips.unshift(clip);select(clip.id);
        $('video-jobs').textContent='Completed simulation · no inference ran';$('video-progress').value=100;
      }
    },180);
  });
  $('video-stop').addEventListener('click',()=>{clearInterval(timer);active=false;$('video-stop').disabled=true;$('video-generate').disabled=!model.ready;$('video-jobs').textContent='Cancelled simulation';});
  $('video-reuse').addEventListener('click',()=>{const clip=clips.find(c=>c.id===selected.id);if(clip){choose(clip.model);restore(clip.settings);length();}});
  $('video-last-frame').addEventListener('click',()=>message('Sample only: the live player captures the last frame from a completed clip.'));
  $('video-delete-form').addEventListener('submit',event=>{
    event.preventDefault();
    if (!selected.id) return;
    if ($('video-delete').dataset.pending!==selected.id) { $('video-delete').dataset.pending=selected.id;$('video-delete').textContent='Confirm delete';return; }
    clips.splice(clips.findIndex(c=>c.id===selected.id),1);selected.id=null;$('video-delete').textContent='Delete clip';gallery();
    if(clips.length)select(clips[0].id);else{$('video-result-title').textContent='Choose a clip';$('video-result-settings').textContent='';}
  });
  for(const id of ['video-filter-model','video-filter-mode','video-filter-date','video-sort'])$(id).addEventListener('change',gallery);
  $('video-loop').addEventListener('change',()=>{$('video-player').loop=$('video-loop').checked;});
  $('video-muted').addEventListener('change',()=>{$('video-player').muted=$('video-muted').checked;});
  $('video-zoom').addEventListener('click',()=>{const actual=$('video-player').dataset.actual!=='true';$('video-player').dataset.actual=String(actual);$('video-zoom').setAttribute('aria-pressed',String(actual));$('video-zoom').textContent=actual?'Fit':'Actual size';});
  $('video-theme').addEventListener('click',()=>{const dark=document.documentElement.dataset.bsTheme!=='dark';document.documentElement.dataset.bsTheme=dark?'dark':'light';$('video-theme').textContent=dark?'Light mode':'Dark mode';});
  function layout() {
    const id=$('video-workspace').dataset.layout;
    const descriptions={canvas:'Model and controls beside the clip. Recommended: the same arrangement as Images.',desk:'Clip and prompt on the left; settings on the right.',review:'Clip first, prompt below. Models use a full-width readiness and setup sequence.'};
    $('video-design-note').textContent=descriptions[id];
    $('video-generation-proposal').hidden=surface!=='generation';$('video-model-proposal').hidden=surface!=='models';
  }
  document.querySelectorAll('[data-video-design]').forEach(button=>button.addEventListener('click',()=>{
    $('video-workspace').dataset.layout=button.dataset.videoDesign;$('video-model-proposal').dataset.layout=button.dataset.videoDesign;
    document.querySelectorAll('[data-video-design]').forEach(e=>{e.classList.toggle('active',e===button);e.setAttribute('aria-pressed',String(e===button));});layout();
  }));
  document.querySelectorAll('[data-surface]').forEach(button=>button.addEventListener('click',()=>{surface=button.dataset.surface;document.querySelectorAll('[data-surface]').forEach(e=>{e.classList.toggle('active',e===button);e.setAttribute('aria-pressed',String(e===button));});layout();}));
  $('video-demo-state').addEventListener('change',()=>{
    const state=$('video-demo-state').value;
    const messages={ready:'',empty:'No video models. Add a model under Models → Video.',forbidden:'No permitted video models. Ask an administrator for model access.',runtime:'Video runtime is missing WebM support. Rebuild through scripts/install-sdcpp.sh.',quota:'Your video quota is full. Delete clips or ask an administrator to raise your quota.',error:'The sample model failed. Your prompt and earlier clips are retained.',timeout:'The sample run timed out. Reduce its length or resolution.',cancelled:'The sample run was cancelled. Your prompt and earlier clips are retained.'};
    message(messages[state]);$('video-generate').disabled=state!=='ready'||!model.ready;
    $('video-model').disabled=['empty','forbidden'].includes(state);
  });
  clips.push({id:'sample',title:'Rainforest study',model:'wan-small',mode:'text',settings:{seconds:2.0625,fps:16,frames:33,seed:42,size:'832x480',model:'wan-small',prompt:'Rain beneath a banana leaf'},created:Date.now()});
  model=null;choose('wan-small');select('sample');layout();
})();
