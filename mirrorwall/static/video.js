/* Private Canvas video studio: the server owns jobs; tab memory owns unsent drafts. */
(() => {
  'use strict';
  const $=id=>document.getElementById('video-'+id), form=$('form'), drafts=new Map(), inputs=new Map();
  const player=$('player'), cards=new Map(),history=new Map();
  let oldest=null;
  let models=[], jobs=[], current=null, selection=null, running=null, polling=false, submitting=false, stopped=false;
  const message=value=>{ $('message').textContent=value || ''; $('message').hidden=!value; };
  const features=()=>{const caps=current?.video_workflow?.runtime_capabilities || {};return {...caps.generation_features,...caps.features_by_mode?.vid_gen,hires:caps.upscalers?.some(one=>one.model && !one.image_upscale)};};
  const profile=()=>current?.video_workflow || {};
  const options=[['text','Text'],['start','Start image'],['ends','Start and end images'],['references','Reference images'],['control','Control video']];
  const controls=()=>[...form.elements].filter(e=>e.name);
  const read=()=>({...Object.fromEntries(controls().filter(e=>!e.disabled).map(e=>[e.name,e.type==='checkbox'?e.checked:e.type==='number'?Number(e.value):e.value])),loras:Object.fromEntries([...$('loras').querySelectorAll('[data-lora]')].filter(e=>!e.disabled).map(e=>[e.dataset.lora,Number(e.value)]))});
  function remember() { if(current) drafts.set(current.id,{settings:read(),mode:$('mode').value}); }
  async function api(action,body,method=body?'POST':'GET') {
    const response=await fetch('/video-api'+(action?'/'+action:''),{method,cache:'no-store',
      headers:body?{'content-type':'application/json'}:{},body:body?JSON.stringify(body):undefined});
    if(response.redirected) {location.href='/sign-in';throw new Error('Please sign in again.');}
    const value=await response.json();if(!response.ok)throw new Error(value.error?.message || 'Video operation failed.');return value;
  }
  function length() {
    const fps=Number($('fps').value), seconds=Number($('seconds').value), target=Math.floor(seconds*fps);
    const frames=profile().family==='minimax-h3'?5+17*Math.max(0,Math.ceil((target-5)/17)):1+4*Math.max(1,Math.floor((target-1)/4));
    $('length').textContent=`${frames} frames · ${fps} fps · ${(frames/fps).toFixed(3)} s effective`;
    return frames;
  }
  function mode() {
    const value=$('mode').value;
    $('inputs').hidden=value==='text';
    for(const key of ['start','end','references','control']) $(key+'-row').hidden=!(value===key || value==='ends' && ['start','end'].includes(key));
    $('vace-row').hidden=value!=='control';$('vace').disabled=value!=='control';
  }
  function choose(id) {
    remember();current=models.find(m=>m.id===id);if(!current)return;
    $('model').value=id;
    const p=profile(), f=features(), saved=drafts.get(id), settings={...p.defaults,...saved?.settings};
    for(const e of controls()) {
      if(e.name in settings) {if(e.type==='checkbox')e.checked=Boolean(settings[e.name]);else e.value=settings[e.name];}
      else if(e.type==='checkbox')e.checked=false;
    }
    if(settings.width && settings.height) {
      const size=`${settings.width}x${settings.height}`;
      if(![...$('size').options].some(e=>e.value===size))$('size').add(new Option(size,size));$('size').value=size;
    }
    const modes=options.filter(([key])=>key==='text' || key==='start' && f.init_image || key==='ends' && f.init_image && f.end_image || key==='references' && f.ref_images || key==='control' && f.control_frames);
    $('mode').replaceChildren(...modes.map(([key,label])=>new Option(label,key)));
    if(saved?.mode && modes.some(([key])=>key===saved.mode))$('mode').value=saved.mode;
    const high=Boolean(f.high_noise_sample_params);
    $('high-noise').hidden=!high;
    for(const key of ['high-steps','high-guidance','moe'])$(key).disabled=!high;
    for(const [key,feature] of [['vae-tiling','vae_tiling'],['temporal-tiling','vae_tiling'],['flow-shift','flow_shift'],['hires','hires'],['live-previews','preview']])$(key).disabled=!f[feature];
    for(const key of ['vae_tiling','temporal_tiling'])if(p[key]) {$(key.replaceAll('_','-')).checked=true;$(key.replaceAll('_','-')).disabled=true;}
    $('hires-row').hidden=!f.hires || !p.components?.latent_upscaler;
    $('soundtrack-row').hidden=!p.components?.audio_vae;$('soundtrack').disabled=!p.components?.audio_vae;
    $('fps').disabled=p.family==='minimax-h3';if(p.family==='minimax-h3')$('fps').value=24;
    const caps=p.runtime_capabilities, ready=caps?.supported_modes?.includes('vid_gen') && caps?.output_formats_by_mode?.vid_gen?.includes('webm');
    $('model-note').textContent=ready?'Exclusive GPU · batch priority':'Runtime capabilities are unqualified or WebM is unavailable. Open Video models to qualify this profile.';
    $('generate').disabled=!ready || submitting;
    $('loras').replaceChildren();
    for(const [name,entry] of Object.entries(p.loras || {})) {
      const row=$('lora-template').content.firstElementChild.cloneNode(true),input=row.querySelector('input');
      row.querySelector('[data-label]').textContent=name;input.dataset.lora=name;input.value=saved?.settings.loras?.[name] ?? entry.scale ?? 1;
      input.disabled=!f.lora;$('loras').append(row);
    }
    for(const key of ['seconds','steps'])$(key).max=p.limits?.['max_'+key] || (key==='seconds'?5:100);
    for(const key of ['start','end','references','control']) {$(key).value='';const count=(inputs.get(id+'/'+key) || []).length;$(key+'-note').textContent=count?count+' selected in this tab':'';}
    mode();length();
  }
  function select(id) {
    const job=jobs.find(j=>j.id===id);if(!job?.asset_id)return;
    const changed=selection!==id;selection=id;
    if(changed)document.dispatchEvent(new CustomEvent('suite:actionsChanged',{detail:{form:$('delete-form')}}));
    if(changed) {
      player.src='/video-api/jobs/'+encodeURIComponent(id)+'/content';
      player.poster='/video-api/jobs/'+encodeURIComponent(id)+'/content?variant=thumbnail';
      $('download').href=player.src;$('download').hidden=false;
    }
    $('result-title').textContent=job.model+' · '+job.seconds+' s';
    $('result-settings').textContent=JSON.stringify(job.assets?.[0]?.metadata?.settings || job.settings,null,2);
    $('viewer-note').textContent=job.assets?.[0]?.metadata?.playback_note || 'Private clip · '+job.frames+' frames at '+job.fps+' fps';
    for(const [key,button] of cards)button.setAttribute('aria-selected',String(key===id));
  }
  function gallery() {
    const visible=jobs.filter(j=>j.asset_id && (!$('filter-model').value || j.model===$('filter-model').value) &&
      (!$('filter-mode').value || j.assets?.[0]?.metadata?.mode===$('filter-mode').value) &&
      (!$('filter-date').value || j.created_at*1000>=Date.parse($('filter-date').value)));
    visible.sort((a,b)=>($('sort').value==='newest'?-1:1)*(a.created_at-b.created_at));
    const present=new Set();
    for(const job of visible) {
      present.add(job.id);let button=cards.get(job.id);
      if(!button) {
        button=$('clip-template').content.firstElementChild.cloneNode(true);cards.set(job.id,button);
        button.addEventListener('click',()=>select(job.id));
        const img=button.querySelector('img');img.src='/video-api/jobs/'+encodeURIComponent(job.id)+'/content?variant=thumbnail';
        img.addEventListener('error',()=>{img.hidden=true;});
      }
      button.querySelector('[data-title]').textContent=job.model;
      button.querySelector('[data-detail]').textContent=job.seconds+' s · '+job.size;
      button.setAttribute('aria-selected',String(selection===job.id));$('gallery').append(button);
    }
    for(const [id,button] of cards)if(!present.has(id)){button.remove();cards.delete(id);}
    $('library-empty').hidden=!!visible.length;
  }
  async function refresh() {
    if(polling || stopped)return;polling=true;
    try {
      const state=await api('state');
      remember();models=state.models;
      const ids=new Set(state.jobs.map(job=>job.id)),floor=state.jobs.at(-1)?.created_at ?? 0;
      for(const [id,job] of history)if(!ids.has(id) && (job.temporary || job.created_at>=floor))history.delete(id);
      for(const job of state.jobs)history.set(job.id,job);
      jobs=[...history.values()].sort((a,b)=>b.created_at-a.created_at);
      if(!oldest && state.jobs.length)oldest=state.jobs.at(-1).id;
      if(history.size<=100)$('load-more').hidden=!state.history_has_more;
      const previous=current?.id, before=JSON.stringify(current?.video_workflow), requested=new URLSearchParams(location.search).get('model');
      const filterModel=$('filter-model').value;
      $('model').replaceChildren(...models.map(m=>new Option(m.id,m.id)));
      $('filter-model').replaceChildren(new Option('All models',''),...models.map(m=>new Option(m.id,m.id)));
      if(models.some(m=>m.id===filterModel))$('filter-model').value=filterModel;
      if(!models.some(m=>m.id===previous)) {current=null;if(models.length)choose(models.some(m=>m.id===requested)?requested:models[0].id);}
      else {
        const next=models.find(m=>m.id===previous);
        if(JSON.stringify(next.video_workflow)!==before)choose(previous);
        else {current=next;$('model').value=previous;}
      }
      $('temporary').closest('label').hidden=!state.can_unlogged;
      if(!models.length){$('model-note').textContent='No permitted video models. Ask an administrator to configure or grant access to a video model.';$('generate').disabled=true;}
      const active=jobs.find(j=>['queued','in_progress'].includes(j.status));running=active?.id || null;
      const latest=active || jobs[0];$('stop').disabled=!active;
      const rate=profile().envelope?.seconds_per_output_second;
      $('jobs').textContent=latest?[latest.model,latest.stage,latest.total_steps?`Step ${latest.step} / ${latest.total_steps}`:'',latest.error?.message,latest.queue_position!=null?'Queue position '+latest.queue_position:'',rate?'Estimated '+Math.ceil(rate*Number(latest.seconds))+' s':''].filter(Boolean).join(' · '):'Your next run will appear here.';
      $('preview').hidden=!latest?.preview_b64;if(latest?.preview_b64)$('preview').src='data:image/png;base64,'+latest.preview_b64;
      $('progress').value=latest?.progress || 0;
      gallery();if(!selection && jobs.some(j=>j.asset_id))select(jobs.find(j=>j.asset_id).id);
    }catch(error){message(error.message);}finally{polling=false;}
  }
  function encode(blob) {return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=reject;reader.readAsDataURL(blob);});}
  async function upload(file) {
    if(file.size>20*2**20)throw new Error('Each image must be at most 20 MiB.');
    const result=await api('assets',{b64_json:(await encode(file)).split(',')[1],filename:file.name || 'frame.png',temporary:$('temporary').checked});
    return result.id;
  }
  async function captureFrame(video,time) {
    if(video.readyState<2 || Math.abs(video.currentTime-time)>.0001)await new Promise((resolve,reject)=>{video.addEventListener('seeked',resolve,{once:true});video.addEventListener('error',reject,{once:true});video.currentTime=time;});
    const canvas=document.createElement('canvas'),scale=Math.min(1,1280/video.videoWidth,1280/video.videoHeight);canvas.width=Math.round(video.videoWidth*scale);canvas.height=Math.round(video.videoHeight*scale);
    canvas.getContext('2d').drawImage(video,0,0,canvas.width,canvas.height);return new Promise(resolve=>canvas.toBlob(resolve,'image/png'));
  }
  async function controlFrames(file) {
    const video=document.createElement('video'),url=URL.createObjectURL(file);video.muted=true;video.preload='auto';
    try {
      await new Promise((resolve,reject)=>{video.onloadedmetadata=resolve;video.onerror=reject;video.src=url;});
      const frames=length(),fps=Number($('fps').value);
      if(video.videoWidth*video.videoHeight>32*2**20 || !Number.isFinite(video.duration) || video.duration<frames/fps)throw new Error('Choose a bounded control clip at least as long as the effective output.');
      const result=[];for(let index=0;index<frames;index++)result.push(await upload(await captureFrame(video,(index+.01)/fps)));
      return result;
    }finally{URL.revokeObjectURL(url);video.removeAttribute('src');video.load();}
  }
  for(const key of ['start','end','references','control'])$(key).addEventListener('change',()=>{inputs.set((current?.id || '')+'/'+key,[...$(key).files]);$(key+'-note').textContent=$(key).files.length+' selected in this tab';});
  form.addEventListener('submit',async e=>{
    e.preventDefault();if(!current || submitting)return;submitting=true;$('generate').disabled=true;message('');
    try {
      const body=read();body.model=current.id;body.fps=Number($('fps').value);body.temporary=$('temporary').checked;
      body.loras=Object.fromEntries([...$('loras').querySelectorAll('[data-lora]')].filter(e=>!e.disabled).map(e=>[e.dataset.lora,Number(e.value)]));
      const mode_=$('mode').value;
      for(const [key,target] of [['start','init_image'],['end','end_image'],['references','reference_images'],['control','control_video']]) {
        if($(key+'-row').hidden)continue;
        const files=inputs.get(current.id+'/'+key) || [];if(!files.length)throw new Error('Choose the '+key+' input.');
        const ids=key==='control'?await controlFrames(files[0]):await Promise.all(files.map(upload));
        body[target]=['references','control'].includes(key)?ids:ids[0];
      }
      body.metadata={mode:mode_};
      const job=await api('jobs',body);running=job.id;await refresh();
    }catch(error){message(error.message);}finally{
      submitting=false;
      $('generate').disabled=!profile().runtime_capabilities?.output_formats_by_mode?.vid_gen?.includes('webm');
    }
  });
  $('stop').addEventListener('click',async()=>{if(!running)return;try{await api('jobs/'+encodeURIComponent(running)+'/cancel',{action:'cancel'});await refresh();}catch(error){message(error.message);}});
  $('model').addEventListener('change',()=>choose($('model').value));$('mode').addEventListener('change',mode);form.addEventListener('input',()=>{length();remember();});
  for(const key of ['filter-model','filter-mode','filter-date','sort'])$(key).addEventListener('change',gallery);
  $('reuse').addEventListener('click',()=>{
    const job=jobs.find(j=>j.id===selection);if(!job)return;
    if(!models.some(m=>m.id===job.model)){message('The model for this clip is unavailable.');return;}
    remember();drafts.set(job.model,{settings:job.assets?.[0]?.metadata?.settings || job.settings,mode:'text'});current=null;choose(job.model);
  });
  $('delete-form').addEventListener('submit',async event=>{
    event.preventDefault();
    if(!selection)return;
    try {await api('jobs/'+encodeURIComponent(selection),undefined,'DELETE');history.delete(selection);oldest=[...history.values()].sort((a,b)=>b.created_at-a.created_at).at(-1)?.id || null;selection=null;player.removeAttribute('src');player.load();$('delete').textContent='Delete clip';await refresh();}catch(error){message(error.message);}
  });
  $('load-more').addEventListener('click',async()=>{
    $('load-more').disabled=true;
    try {
      const page=await api('jobs?limit=100&after='+encodeURIComponent(oldest));
      for(const job of page.data)history.set(job.id,job);
      if(page.data.length)oldest=page.data.at(-1).id;
      jobs=[...history.values()];$('load-more').hidden=!page.has_more;gallery();
    }catch(error){message(error.message);}finally{$('load-more').disabled=false;}
  });
  for(const [key,direction] of [['previous-frame',-1],['next-frame',1]])$(key).addEventListener('click',()=>{
    const job=jobs.find(j=>j.id===selection);player.pause();if(Number.isFinite(player.duration))player.currentTime=Math.max(0,Math.min(player.duration,player.currentTime+direction/(job?.fps || 16)));
  });
  player.addEventListener('keydown',event=>{if(['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();$(event.key==='ArrowLeft'?'previous-frame':'next-frame').click();}});
  $('last-frame').addEventListener('click',async()=>{
    const job=jobs.find(j=>j.id===selection);if(!job || !features().init_image)return;
    try {const blob=await captureFrame(player,Math.max(0,player.duration-1/(job.fps || 16)));const file=new File([blob],'last-frame.png',{type:'image/png'});inputs.set(current.id+'/start',[file]);$('mode').value='start';mode();$('start-note').textContent='Last frame captured from your clip.';}catch(error){message('The browser could not capture the last frame: '+error.message);}
  });
  player.addEventListener('loadeddata',async()=>{
    const id=selection,job=jobs.find(j=>j.id===id);if(!job || job.assets?.[0]?.metadata?.poster_available)return;
    try {
      const blob=await captureFrame(player,player.currentTime);
      await api('jobs/'+encodeURIComponent(id)+'/poster',{b64_json:(await encode(blob)).split(',')[1]});
      const thumbnail='/video-api/jobs/'+encodeURIComponent(id)+'/content?variant=thumbnail';
      if(selection===id)player.poster=thumbnail;
      const img=cards.get(id)?.querySelector('img');if(img){img.src=thumbnail;img.hidden=false;}
    }catch(error){$('viewer-note').textContent='The clip is available; its browser poster could not be saved.';}
  });
  $('loop').addEventListener('change',()=>{player.loop=$('loop').checked;});$('muted').addEventListener('change',()=>{player.muted=$('muted').checked;});
  $('zoom').addEventListener('click',()=>{const actual=player.dataset.actual!=='true';player.dataset.actual=String(actual);$('zoom').textContent=actual?'Fit':'Actual size';$('zoom').setAttribute('aria-pressed',String(actual));});
  window.addEventListener('pagehide',()=>{stopped=true;clearInterval(interval);});
  const interval=setInterval(()=>{if(!document.hidden)refresh();},3000);refresh();
})();
