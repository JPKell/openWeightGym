/* Offline interaction model. No requests, native inference or persistent user assets. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), all = selector => [...document.querySelectorAll(selector)];
  const samples = JSON.parse($('sample-data').textContent), profiles = window.SuiteImageProfiles;
  const fields = ['prompt','negative_prompt','width','height','steps','n','seed','guidance','cfg_scale','sampler','scheduler','upscale','strength','reference_megapixels','lora','lora-strength','image-text'];
  const numeric = new Set(['width','height','steps','n','seed','guidance','cfg_scale','upscale','strength','reference_megapixels','lora-strength']);
  const descriptions = {
    'flux2-dev':'Precise composition · ordered references', 'flux2-klein':'Fast exploration · distilled model',
    sdxl:'Flexible styles · image-to-image', 'qwen-image':'Typography & multilingual text', 'qwen-image-2.1':'Detailed typography · 32px alignment',
  };
  const notes = {
    canvas:'Canvas studio · an image-first workspace with tools on the left and a focused inspector on the right.',
    lab:'Batch lab · compare variations, sweep parameters and monitor a queue. Built for repeatable experimentation.',
    desk:'Creative desk · start with the brief, choose a direction, then refine. A calmer space for art direction.',
  };
  const drafts = new Map(), presets = new Map(), jobs = [];
  let family = 'flux2-dev', layout = 'canvas', references = [], mask = null, sequence = 0, selected = 'sample-0';
  const samplePrompts = [
    'A tiger cub resting beneath a broad banana leaf in a tropical rainforest. Rain beads on the leaf, soft overcast light, intimate wildlife portrait, rich green tones.',
    'A vivid blue apple, blue color #0047AB, realistic still-life photograph, studio lighting.',
    'A bright neon sign reading "Open", atmospheric night photography, rich colors.',
  ];
  let results = ['tiger','apple','neon'].map((asset,i) => ({id:`sample-${i}`,src:samples[asset],title:['Rainforest study','Color study','After hours'][i],prompt:samplePrompts[i],sample:true,favorite:false,settings:null}));
  function say(message, error = false) {
    $('feedback').hidden = false; $('feedback-text').textContent = message;
    $('feedback-text').classList.toggle('alert-danger', error);
    $('feedback-text').classList.toggle('alert-info', !error);
    $('feedback-text').setAttribute('role',error ? 'alert' : 'status');
  }
  function valueSnapshot() { return Object.fromEntries(fields.map(id => [id, numeric.has(id) ? Number($(id).value) : $(id).value])); }
  function remember() {
    drafts.set(family,{values:valueSnapshot(),references:[...references],mask,preset:$('preset').value,sweep:$('sweep-enabled').checked,axis:$('sweep-axis').value,valuesList:$('sweep-values').value});
  }
  function fill(values) { Object.entries(values).forEach(([id,value]) => { if ($(id) && value !== undefined) $(id).value = value; }); }
  function defaultValues() { return {...profiles.spec(family).defaults,prompt:samplePrompts[0],negative_prompt:'',strength:.75,reference_megapixels:.5,lora:'','lora-strength':.8,'image-text':''}; }
  function selectModel(next) {
    remember(); family = next; $('model').value = next;
    const draft = drafts.get(family);
    references = draft?.references || []; mask = draft?.mask || null;
    fill(draft?.values || defaultValues());
    renderPresets(); $('preset').value = draft?.preset || 'default';
    $('sweep-enabled').checked = draft?.sweep || false;
    $('sweep-axis').value = draft?.axis || 'seed'; $('sweep-values').value = draft?.valuesList || '42, 43, 44';
    const spec = profiles.spec(family);
    $('model-description').textContent = descriptions[family]; $('family-caption').textContent = spec.label.toUpperCase();
    $('negative-wrap').hidden = !spec.negative; $('cfg-wrap').hidden = !spec.negative;
    $('guidance-wrap').hidden = !spec.guidance; $('text-wrap').hidden = !spec.qwen;
    $('guidance').disabled = !spec.guidance; $('cfg_scale').disabled = !spec.negative;
    $('negative_prompt').disabled = !spec.negative;
    $('reference-section').hidden = spec.qwen;
    $('reference-upload').disabled = spec.qwen;
    all('[data-tool=reference], [data-tool=mask]').forEach(button => { button.disabled = spec.qwen; button.title = spec.qwen ? 'This Qwen profile supports text-to-image only.' : ''; });
    $('reference-tool-note').textContent = spec.strength ? 'One source image' : spec.qwen ? 'Needs an edit profile' : 'Ordered visual references';
    $('steps-help').textContent = family === 'flux2-klein' ? '6-step app default. Distilled guidance is fixed; extra steps may not improve quality.' : spec.qwen ? '50-step app default. CFG and a negative prompt control adherence.' : family === 'sdxl' ? '28-step app default. Balance detail with render time.' : '28 steps for exploration; the upstream example also uses 50 for detail.';
    $('prompt-help').textContent = spec.qwen ? 'Put exact lettering in quotes. Describe its placement and typography. This profile generates from text only.' : family === 'sdxl' ? 'Describe the subject and style; use the negative prompt for exclusions.' : 'Describe the subject, action, style and setting. Flux uses positive descriptions; negative prompts are unavailable.';
    $('width').step = $('height').step = spec.alignment;
    $('reference-help').textContent = spec.strength ? 'One start image. Image strength controls how much it changes.' : 'Up to two ordered references in this sample workflow. Mention image 1 or image 2 in the prompt. Inputs are prepared automatically.';
    document.querySelector('[data-sweep-guidance]').disabled = !spec.guidance;
    document.querySelector('[data-sweep-cfg]').disabled = !spec.negative;
    if ($('sweep-axis').selectedOptions[0]?.disabled) $('sweep-axis').value = 'seed';
    renderReferences(); update();
  }
  function renderPresets() {
    all('#preset option[data-custom]').forEach(option => option.remove());
    for (const [id,preset] of presets) {
      if (preset.family !== family) continue;
      const option = new Option(preset.name,id); option.dataset.custom = ''; $('preset').add(option);
    }
  }
  function update() {
    const width=Number($('width').value),height=Number($('height').value),count=Number($('n').value),scale=Number($('upscale').value);
    $('pixel-count').textContent = `${(width*height/1e6).toFixed(2)} MP`;
    $('output-size').textContent = `Final output: ${width*scale} × ${height*scale} px`;
    $('steps-value').value = $('steps').value;
    $('strength-value').value = $('strength').value;
    $('prompt-count').textContent = `${$('prompt').value.length.toLocaleString()} characters`;
    $('run-summary').textContent = `${count} image${count===1?'':'s'} · ${width} × ${height}`;
    $('lora-strength-wrap').hidden = !$('lora').value;
    const sweepValues = $('sweep-values').value.split(',').filter(v=>v.trim());
    $('sweep-summary').textContent = `${sweepValues.length} values × ${count} image${count===1?'':'s'} = ${sweepValues.length*count} images`;
    $('generate').firstChild.textContent = `Generate ${$('sweep-enabled').checked ? sweepValues.length*count : count} image${(!$('sweep-enabled').checked && count===1)?'':'s'} `;
    document.querySelector('.ig-sweep-panel').hidden = layout !== 'lab' && !$('sweep-enabled').checked;
    $('workspace').dataset.sweep = String(layout === 'lab' || $('sweep-enabled').checked);
    all('[data-ratio]').forEach(button => {
      const [w,h] = button.dataset.ratio.split(':').map(Number), active = Math.abs(width/height-w/h)<.015;
      button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active));
    });
  }
  function setLayout(next) {
    layout = next; $('workspace').dataset.layout = next; $('design-note').textContent = notes[next];
    all('[data-design]').forEach(button => { const active=button.dataset.design===next; button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active)); });
    update();
    history.replaceState(null,'',`#${next}`);
  }
  function setView(next) {
    $('workspace').dataset.view = next;
    all('button[data-view]').forEach(button=>{const active=button.dataset.view===next;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));});
  }
  function renderReferences() {
    $('reference-list').replaceChildren(); const spec=profiles.spec(family),max=spec.strength?1:2;
    $('reference-count').textContent = `${references.length} / ${max}`;
    $('sample-reference').disabled = references.length >= max;
    $('reference-options').hidden = !references.length;
    $('strength-wrap').hidden = !(spec.strength && references.length);
    $('mask-wrap').hidden = !references.length;
    $('clear-mask').hidden = !mask;
    references.forEach((ref,i)=>{
      const fragment=$('reference-template').content.cloneNode(true);
      fragment.querySelector('img').src=ref.src; fragment.querySelector('img').alt=ref.name;
      fragment.querySelector('[data-name]').textContent=`${i+1}. ${ref.name}`;
      fragment.querySelector('[data-detail]').textContent=spec.strength?'Start image':`Reference ${i+1} · auto preparation`;
      const up=fragment.querySelector('[data-up]'); up.disabled=i===0; up.onclick=()=>{[references[i-1],references[i]]=[references[i],references[i-1]];mask=null;renderReferences();};
      fragment.querySelector('[data-remove]').onclick=()=>{references.splice(i,1);mask=null;$('mask-upload').value='';renderReferences();};
      $('reference-list').append(fragment);
    });
  }
  function readImage(file) {
    return new Promise((resolve,reject)=>{
      if (!['image/png','image/jpeg','image/webp'].includes(file.type) || file.size>32*1024*1024) { reject(new Error('Choose PNG, JPEG or WebP files under 32 MB.')); return; }
      const reader=new FileReader(); reader.onerror=()=>reject(new Error('Could not read this image.'));
      reader.onload=()=>{const img=new Image();img.onerror=()=>reject(new Error('This image could not be decoded.'));img.onload=()=>resolve({name:file.name,src:reader.result,width:img.naturalWidth,height:img.naturalHeight});img.src=reader.result;};
      reader.readAsDataURL(file);
    });
  }
  async function addFiles(files) {
    const max=profiles.spec(family).strength?1:2;
    if(profiles.spec(family).qwen) {say('This Qwen profile supports text-to-image only.',true);return;}
    if(references.length+files.length>max) {say(`This workflow accepts at most ${max} reference image${max===1?'':'s'}.`,true);return;}
    const uploadingFamily=family;
    try {
      const images=await Promise.all([...files].map(readImage));
      if(family!==uploadingFamily) {say('Model changed during upload. Select the images again for the current model.',true);return;}
      references.push(...images);renderReferences();
    } catch(error) {say(error.message,true);}
    $('reference-upload').value='';
  }
  function current() { return results.find(row=>row.id===selected); }
  function renderResults() {
    const filter=$('library-filter').value;
    const visible=results.filter(row=>filter==='all'||(filter==='favorites'?row.favorite:!row.sample));
    $('results').replaceChildren(); $('library-empty').hidden=!!visible.length;
    visible.forEach(row=>{
      const fragment=$('result-template').content.cloneNode(true),button=fragment.querySelector('button');
      button.dataset.result=row.id; button.setAttribute('aria-pressed',String(row.id===selected));
      fragment.querySelector('img').src=row.src;fragment.querySelector('img').alt=`${row.sample?'Illustrative sample':'Simulated result'}: ${row.title}`;
      fragment.querySelector('[data-title]').textContent=`${row.favorite?'★ ':''}${row.title}`;
      fragment.querySelector('[data-detail]').textContent=row.sample?'BFL · sample artwork':`Demo · seed ${row.settings.seed}`;
      button.onclick=()=>{selected=row.id;renderResults();}; $('results').append(fragment);
    });
    const row=current();
    $('artboard').hidden=!row;$('canvas-empty').hidden=!!row;
    all('#reuse,#favorite,#download,#export-settings,#delete-result,#compare,#zoom,#fullscreen').forEach(button=>button.disabled=!row);
    $('result-counter').textContent=row?`${String(results.indexOf(row)+1).padStart(2,'0')} / ${String(results.length).padStart(2,'0')}`:'00 / 00';
    $('result-settings').replaceChildren();
    $('result-title').textContent=row?.title||'No image selected';
    $('result-detail').textContent=row?(row.sample?'Illustrative artwork · not a run from this workspace':`${profiles.spec(row.settings.family).label} · ${row.settings.width} × ${row.settings.height} · seed ${row.settings.seed} · simulated`):'Choose a result from your library.';
    if(!row) return;
    $('large-image').src=row.src;$('large-image').alt=`${row.sample?'Illustrative sample':'Simulated result'}: ${row.title}`;
    $('favorite').textContent=row.favorite?'★':'☆';$('favorite').setAttribute('aria-pressed',String(row.favorite));
    document.querySelector('.ig-art-label').textContent=row.sample?'SAMPLE ARTWORK · BLACK FOREST LABS':'SIMULATED RESULT · SAMPLE ARTWORK';
    const facts=row.settings?{Model:profiles.spec(row.settings.family).label,Seed:row.settings.seed,Steps:row.settings.steps,Size:`${row.settings.width} × ${row.settings.height}`,Upscale:`${row.settings.upscale}×`,Sampler:row.settings.sampler,Scheduler:row.settings.scheduler,Prompt:row.prompt}:{Source:'Black Forest Labs · illustrative sample',Prompt:row.prompt,Settings:'Original generation settings are unavailable.'};
    for(const [key,value] of Object.entries(facts)) {const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;$('result-settings').append(dt,dd);}
    const other=results.find(one=>one.id!==row.id);$('compare').disabled=!other;
    if(other) $('compare-pane').querySelector('img').src=other.src;
  }
  function requestSettings() {
    const raw=valueSnapshot(),spec=profiles.spec(family);
    if(!spec.negative) {delete raw.cfg_scale;delete raw.negative_prompt;}
    if(!spec.guidance) delete raw.guidance;
    if(!spec.strength||!references.length) delete raw.strength;
    if(spec.qwen) delete raw.reference_megapixels;
    delete raw['image-text'];
    if(!raw.lora) {delete raw.lora;delete raw['lora-strength'];}
    return {...raw,family,node:$('node').value,reference_count:references.length,mask_mode:mask?'composite':null,temporary:$('temporary').checked,live_preview:$('live-preview').checked};
  }
  function validate(settings) {
    const alignment=profiles.spec(family).alignment;
    if(settings.width%alignment || settings.height%alignment) throw new Error(`This model needs dimensions in multiples of ${alignment}.`);
    if(settings.width*settings.height>2097152) throw new Error('The sample workflow allows at most 2.10 megapixels before upscaling. Reduce width or height.');
    if(mask && references.length && (references[0].width!==mask.width || references[0].height!==mask.height)) throw new Error('The mask must match the first reference’s dimensions.');
    if(mask && references[0] && Math.abs(settings.width/settings.height-references[0].width/references[0].height)>.001) throw new Error('A composite mask requires the same output aspect ratio as the first reference.');
  }
  function run(event) {
    event.preventDefault();
    if(!$('generation-form').reportValidity()) return;
    try {
      const settings=requestSettings();validate(settings);
      let variants=[settings];
      if($('sweep-enabled').checked) {
        const axis=$('sweep-axis').value,parts=$('sweep-values').value.split(',').map(part=>part.trim());
        if(parts.some(part=>!part)||parts.length>32) throw new Error('Enter 1–32 comma-separated sweep values.');
        const values=parts.map(Number),limits={seed:[0,2147483647],steps:[1,100],guidance:[0,20],cfg_scale:[0,30]}[axis];
        if(values.some(value=>!Number.isFinite(value)||value<limits[0]||value>limits[1]||(['seed','steps'].includes(axis)&&!Number.isInteger(value)))) throw new Error(`Sweep values for ${axis} must be ${limits[0]}–${limits[1]}${['seed','steps'].includes(axis)?' whole numbers':''}.`);
        variants=values.map(value=>({...settings,[axis]:value}));
      }
      if(variants.length*settings.n>128) throw new Error('A sweep can produce at most 128 images.');
      const expanded=variants.flatMap(value=>Array.from({length:settings.n},()=>({...value,seed:value.seed<0?Math.floor(Math.random()*2147483647):value.seed})));
      const job={id:++sequence,settings,images:expanded,done:0,tick:0,state:'Queued',paused:false,stopped:false,refs:[...references],mask};
      jobs.push(job);renderJobs();say(`Demo job ${job.id} queued: ${expanded.length} image${expanded.length===1?'':'s'}. Sample artwork will stand in for generated images.`);
      job.timer=setInterval(()=>advance(job),220);
    } catch(error) {say(error.message,true);}
  }
  function advance(job) {
    if(job.stopped||job.paused) return;
    // One simulated worker, with later jobs waiting in their existing queue position.
    if(jobs.some(other=>other.id<job.id&&!['Complete','Stopped'].includes(other.state))) return;
    job.tick++;
    job.state=job.tick<2?'Preparing':job.tick<3?'Loading':job.tick<7?'Generating':Number(job.settings.upscale)>1?'Upscaling':'Finishing';
    if(job.tick>=8) {
      const settings=job.images[job.done],asset=['tiger','apple','neon'][job.done%3];
      const row={id:`run-${job.id}-${job.done}`,src:samples[asset],title:`Study ${job.id} / ${job.done+1}`,prompt:job.settings.prompt,sample:false,favorite:false,settings,refs:job.refs,mask:job.mask};
      results.unshift(row); if(!selected||!results.some(one=>one.id===selected)) selected=row.id;
      job.done++;job.tick=0;renderResults();
      if(job.done===job.images.length) {job.state='Complete';clearInterval(job.timer);}
    }
    renderJobs();
  }
  function renderJobs() {
    $('jobs').replaceChildren();$('queue-empty').hidden=!!jobs.length;
    jobs.slice().reverse().forEach(job=>{
      const fragment=$('job-template').content.cloneNode(true);
      fragment.querySelector('[data-title]').textContent=`Job ${job.id} · ${profiles.spec(job.settings.family).label}`;
      fragment.querySelector('[data-state]').textContent=job.state;
      fragment.querySelector('[data-detail]').textContent=`${job.done} / ${job.images.length} images · ${job.settings.steps} steps · ${job.settings.temporary?'temporary':'session library'}`;
      fragment.querySelector('progress').value=100*(job.done+job.tick/8)/job.images.length;
      const pause=fragment.querySelector('[data-pause]'),stop=fragment.querySelector('[data-stop]'),finished=['Complete','Stopped'].includes(job.state);
      pause.disabled=stop.disabled=finished;pause.textContent=job.paused?'Resume':'Pause';
      pause.onclick=()=>{job.paused=!job.paused;job.state=job.paused?'Paused':'Queued';renderJobs();};
      stop.onclick=()=>{job.stopped=true;job.state='Stopped';clearInterval(job.timer);renderJobs();say(`Job ${job.id} stopped. ${job.done} completed images remain in the session library.`);};
      $('jobs').append(fragment);
    });
  }
  function download(name,content,type) {
    const blob=new Blob([content],{type}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  all('[data-design]').forEach(button=>button.onclick=()=>setLayout(button.dataset.design));
  all('button[data-view]').forEach(button=>button.onclick=()=>setView(button.dataset.view));
  $('model').onchange=()=>selectModel($('model').value);
  $('generation-form').onsubmit=run;
  document.addEventListener('input',event=>{if(event.target.matches('input,select,textarea'))update();});
  $('preset').onchange=()=>{
    const id=$('preset').value,preset=presets.get(id);
    if(preset) fill(preset.values);
    else {const defaults=profiles.spec(family).defaults;fill({...defaults,steps:id==='draft'?(family==='flux2-klein'?4:12):id==='detail'?(family==='flux2-klein'?6:50):defaults.steps});}
    update();
  };
  all('[data-ratio]').forEach(button=>button.onclick=()=>{
    const alignment=profiles.spec(family).alignment,[w,h]=button.dataset.ratio.split(':').map(Number);
    $('width').value=Math.round(1024/alignment)*alignment;
    $('height').value=Math.round(1024*h/w/alignment)*alignment;update();
  });
  $('random-seed').onclick=()=>{$('seed').value=-1;say('Seed set to random. Each new image gets a resolved seed you can reuse.');};
  all('[data-style]').forEach(button=>button.onclick=()=>{const styles={cinematic:'Cinematic lighting, deliberate composition, subtle film grain.',editorial:'Editorial photography, natural texture, considered color palette.',illustration:'Handcrafted illustration, expressive shapes, subtle paper texture.'};$('prompt').value=`${$('prompt').value.trim()} ${styles[button.dataset.style]}`;update();});
  $('insert-text').onclick=()=>{const text=$('image-text').value.trim();if(text){$('prompt').value+=` Include the exact text ${JSON.stringify(text)} with clear, readable lettering.`;update();}};
  $('reference-upload').onchange=event=>addFiles(event.target.files);
  for(const event of ['dragenter','dragover']) $('dropzone').addEventListener(event,e=>{e.preventDefault();$('dropzone').classList.add('drag-over');});
  $('dropzone').addEventListener('dragleave',()=>$('dropzone').classList.remove('drag-over'));
  $('dropzone').addEventListener('drop',event=>{event.preventDefault();$('dropzone').classList.remove('drag-over');addFiles(event.dataTransfer.files);});
  $('sample-reference').onclick=()=>{const max=profiles.spec(family).strength?1:2;if(references.length<max)references.push({name:`Sample ${references.length+1}`,src:samples[['tiger','apple'][references.length]],width:references.length?1000:1000,height:references.length?641:566});renderReferences();};
  $('mask-upload').onchange=async event=>{const file=event.target.files[0];if(!file)return;if(file.type!=='image/png'){say('A composite mask must be a PNG image.',true);return;}try{mask=await readImage(file);say(`Mask ready: ${mask.width} × ${mask.height}. White and transparent regions will be replaced.`);renderReferences();}catch(error){say(error.message,true);}};
  $('clear-mask').onclick=()=>{mask=null;$('mask-upload').value='';renderReferences();};
  all('[data-tool]').forEach(button=>button.onclick=()=>{
    all('[data-tool]').forEach(other=>{const active=other===button;other.classList.toggle('active',active);other.setAttribute('aria-pressed',String(active));});
    const tool=button.dataset.tool;
    if(tool==='reference'||tool==='mask'){$('reference-section').open=true;$('reference-section').scrollIntoView({block:'nearest'});if(tool==='mask'&&!references.length)say('Add a reference image first, then choose a composite mask.');}
    if(tool==='sweep'){$('sweep-enabled').checked=true;update();document.querySelector('.ig-sweep-panel').scrollIntoView({block:'nearest'});}
    if(tool==='upscale'){$('upscale').focus();say('Output upscaling enlarges the next generation. Standalone upscaling of existing images is a proposed addition.');}
    if(tool==='generate')$('prompt').focus();
  });
  $('research').onclick=()=>$('guide-dialog').showModal();
  $('more-tools').onclick=()=>$('tools-dialog').showModal();
  $('toolkit').onclick=()=>$('tools-dialog').showModal();
  $('open-sweep').onclick=()=>{$('sweep-enabled').checked=true;update();document.querySelector('.ig-sweep-panel').scrollIntoView({block:'nearest'});};
  all('[data-close]').forEach(button=>button.onclick=()=>button.closest('dialog').close());
  $('save-preset').onclick=()=>{$('preset-name').value='';$('preset-dialog').showModal();};
  $('preset-form').onsubmit=event=>{
    event.preventDefault();if(!$('generation-form').reportValidity())return;
    const values=valueSnapshot();for(const key of ['prompt','negative_prompt','image-text'])delete values[key];
    const id=`preset-${Date.now()}`;presets.set(id,{name:$('preset-name').value.trim(),family,values});renderPresets();$('preset').value=id;$('preset-dialog').close();say('Workflow settings saved in this tab. Prompt and input images were excluded.');
  };
  $('prompt-tips').onclick=()=>say($('prompt-help').textContent);
  $('library-filter').onchange=renderResults;
  $('favorite').onclick=()=>{const row=current();if(row){row.favorite=!row.favorite;renderResults();}};
  $('delete-result').onclick=()=>{const index=results.findIndex(row=>row.id===selected);if(index<0)return;results.splice(index,1);selected=results[Math.min(index,results.length-1)]?.id||null;renderResults();};
  $('clear-library').onclick=()=>{results=[];selected=null;renderResults();say('Session library cleared. Your prompt and generation settings are preserved.');};
  $('reuse').onclick=()=>{
    const row=current();if(!row)return;
    if(row.settings){selectModel(row.settings.family);fill(row.settings);references=[...(row.refs||[])];mask=row.mask||null;renderReferences();}else{$('prompt').value=row.prompt;say('Sample prompt copied. Original generation settings are unavailable.');}
    update();setView('generate');
  };
  $('export-settings').onclick=()=>{const row=current();if(row)download('image-settings.json',JSON.stringify({simulation:!row.sample,sample_artwork:true,prompt:row.prompt,settings:row.settings},null,2),'application/json');};
  $('download').onclick=()=>{const row=current();if(!row)return;const a=document.createElement('a');a.href=row.src;a.download=`${row.id}-sample-artwork.webp`;a.click();};
  $('zoom').onclick=()=>{const zoomed=$('artboard').classList.toggle('is-zoomed');$('zoom').textContent=zoomed?'Fit':'100%';};
  $('compare').onclick=()=>{const active=$('artboard').classList.toggle('is-comparing');$('compare-pane').hidden=!active;$('compare').setAttribute('aria-pressed',String(active));};
  $('fullscreen').onclick=()=>{if(document.fullscreenElement)document.exitFullscreen?.();else $('artboard').requestFullscreen?.().catch(()=>say('Expanded viewing is unavailable in this browser.'));};
  $('theme').onclick=()=>{const input=document.querySelector('[data-theme-form] [name=mode]');input.value=document.documentElement.dataset.bsTheme==='dark'?'light':'dark';input.dispatchEvent(new Event('change',{bubbles:true}));$('theme').textContent=input.value==='dark'?'Light mode':'Dark mode';};
  $('reset').onclick=()=>location.reload();
  document.addEventListener('keydown',event=>{
    if(document.querySelector('dialog[open]'))return;
    if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();$('generation-form').requestSubmit();return;}
    if(event.target.closest('input,textarea,select,button,a,summary'))return;
    if(['ArrowLeft','ArrowRight'].includes(event.key)&&results.length){event.preventDefault();const index=results.findIndex(row=>row.id===selected);selected=results[(index+(event.key==='ArrowRight'?1:results.length-1))%results.length].id;renderResults();}
  });
  fill(defaultValues());selectModel(family);setLayout(['canvas','lab','desk'].includes(location.hash.slice(1))?location.hash.slice(1):'canvas');renderResults();renderJobs();
})();
