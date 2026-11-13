/* Private image studio. API keys stay in the authenticated server session. */
(() => {
  'use strict';
  const root = document.querySelector('#mirrorwall');
  if (!root) return;
  const $ = id => root.querySelector('#mw-' + id);
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const message = error => { if(!root.isConnected)return; $('message').hidden = !error; $('message').textContent = error || ''; };
  async function api(path, body, method) {
    const response = await fetch('/mirrorwall-api/' + path, {
      method: method || (body === undefined ? 'GET' : 'POST'),
      headers: body === undefined ? {} : {'content-type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store'
    });
    if (response.redirected) { location.href = '/sign-in'; throw new Error('Please sign in again.'); }
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || 'The image operation failed.');
    return value;
  }
  const safe = action => async event => {
    try { await action(event); message(''); } catch (error) { message(error.message); }
  };
  const imageURL = (id, kind = 'thumbnail') => '/mirrorwall-api/assets/' + encodeURIComponent(id) + '/' + kind;
  const fields = ['width', 'height', 'steps', 'n', 'seed', 'guidance', 'cfg_scale', 'sampler', 'scheduler', 'upscale', 'reference_megapixels', 'strength'];
  let state = {models: [], assets: [], references: [], presets: [], jobs: []};
  let selected = null, references = [], mask = null, modelName = '', gallerySignature = '', jobsSignature = '';
  let runningPreview = '', polling = false, refreshProblem = false, referenceSignature = '', detailsSignature = '';
  let older = [], moreImages = false;
  const drafts = new Map(), favorites = new Set();
  let activeTool = 'generate', resizeSource = null, submitting = false, uploading = 0;
  const all = selector => [...root.querySelectorAll(selector)];
  const profile = () => state.models.find(row => row.id === $('model').value)?.image_workflow || {};
  const clone = name => $(name + '-template').content.cloneNode(true);
  const sizes = {};  // reference asset id -> {width, height}, from uploads and the saved-reference list
  const settings = () => {
    const values = Object.fromEntries(new FormData($('form')));
    fields.forEach(name => { if (name in values && !['sampler', 'scheduler'].includes(name)) values[name] = Number(values[name]); });
    if ($('negative-wrap').hidden || !values.negative_prompt) delete values.negative_prompt;
    if ($('strength-wrap').hidden) delete values.strength;
    if (!SuiteImageProfiles.spec(profile().family).guidance) delete values.guidance;
    if (!SuiteImageProfiles.spec(profile().family).negative) delete values.cfg_scale;
    values.live_previews = $('preview').checked;
    const loras = {};
    $('loras').querySelectorAll('[data-lora]').forEach(node => { if (node.value !== '') loras[node.dataset.lora] = Number(node.value); });
    if (Object.keys(loras).length) values.loras = loras;
    if (activeTool === 'resize') {
      const plan = resizePlan();
      Object.assign(values, {width:plan.width,height:plan.height,n:1,upscale:1,
        strength:Number($('resize-strength').value),detail_resize:{width:plan.targetWidth,height:plan.targetHeight},
        prompt:$('resize-prompt').value.trim() || 'Preserve the original composition, colors and identity. Refine natural textures and fine edges with faithful high-resolution detail.'});
      delete values.negative_prompt;
    }
    return values;
  };
  function rememberDraft() {
    if (!modelName) return;
    const values = Object.fromEntries(fields.concat(['prompt','negative_prompt']).map(name => [name,$('form').elements.namedItem(name)?.value]));
    values.loras = Object.fromEntries(all('[data-lora]').filter(node=>node.value!=='').map(node=>[node.dataset.lora,Number(node.value)]));
    drafts.set(modelName + ':' + activeTool, {values,references:[...references],mask,preset:$('preset').value,
      sweep:$('sweep').value,enabled:$('sweep-enabled').checked,axis:$('sweep-axis').value,valuesList:$('sweep-values').value});
  }
  function apply(values) {
    fields.concat(['prompt', 'negative_prompt']).forEach(name => {
      const node = $('form').elements.namedItem(name);
      if (node && values[name] !== undefined) node.value = values[name];
    });
    $('loras').querySelectorAll('[data-lora]').forEach(node => { node.value = values.loras?.[node.dataset.lora] ?? node.dataset.default; });
  }
  function chooseModel(reset = true) {
    const one = state.models.find(row => row.id === $('model').value);
    if (!one) { $('family-caption').textContent='';$('model-description').textContent='No model available';return; }
    const unboundPrompt=!modelName?$('prompt').value:'';
    if (one.id !== modelName) rememberDraft();
    const changed = one.id !== modelName;
    modelName = one.id;
    const profile = one.image_workflow || {};
    $('negative-wrap').hidden = !SuiteImageProfiles.spec(profile.family).negative;
    const spec = SuiteImageProfiles.spec(profile.family);
    $('guidance-wrap').hidden = !spec.guidance;
    $('cfg-wrap').hidden = !spec.negative;
    $('text-wrap').hidden = !spec.qwen;
    $('family-caption').textContent = spec.label;
    $('model-description').textContent = spec.label + (profile.exclusive ? ' · exclusive workflow' : ' · shared residency');
    $('steps-help').textContent = profile.family === 'flux2-klein' ? 'Distilled guidance is fixed. Use the selected workflow’s step count.' : 'Start with the workflow default; more steps increase render time.';
    $('prompt-help').textContent = spec.qwen ? 'Put exact text in quotes and describe its placement. This profile generates from text only.' : spec.negative ? 'Describe the subject and style. Use the negative prompt for exclusions.' : 'Describe the subject, action, style and setting. Flux does not use negative prompts.';
    $('negative').disabled = !spec.negative;
    for (const id of ['cfg','sampler','scheduler']) $(id).disabled = !profile.exclusive;
    $('guidance').disabled = !spec.guidance;
    $('width').step = $('height').step = SuiteImageProfiles.spec(profile.family).alignment;
    $('upscale').querySelectorAll('option').forEach(node => { node.disabled = Number(node.value) > (profile.limits?.max_upscale || (profile.upscaling ? 4 : 1)); });
    $('reference-mp').max = profile.reference_megapixels || 1;
    if (reset) $('reference-mp').value = profile.reference_megapixels || 1;
    $('loras').replaceChildren();
    for (const [name, definition] of Object.entries(profile.loras || {})) {
      const fragment=clone('lora'),label=fragment.querySelector('label'),input=fragment.querySelector('input');
      label.classList.remove('col-md-6');label.classList.add('d-block');
      fragment.querySelector('[data-label]').textContent=name+' · LoRA scale (0 = off)';
      input.dataset.default = String(definition.scale ?? 0); input.placeholder = 'Profile default: ' + input.dataset.default; input.dataset.lora = name;
      $('loras').append(fragment);
    }
    const limits = profile.limits || {};
    for (const [name, limit] of [['width','max_width'], ['height','max_height'], ['steps','max_steps'], ['n','max_images']]) {
      $('form').elements.namedItem(name).max = limits[limit] || {width:2048,height:2048,steps:100,n:8}[name];
    }
    if (reset) {
      apply({...spec.defaults,...profile.defaults,prompt:'',negative_prompt:''});
      if(unboundPrompt)$('prompt').value=unboundPrompt;
      if (changed) { references=[];mask=null; }
      const draft = drafts.get(modelName + ':' + activeTool);
      if (draft) {
        apply(draft.values);references=[...draft.references];mask=draft.mask;
        $('sweep').value=draft.sweep;$('sweep-enabled').checked=draft.enabled;
        $('sweep-axis').value=draft.axis;$('sweep-values').value=draft.valuesList;
      } else { $('sweep').value='';$('sweep-enabled').checked=false; }
    }
    if(!profile.exclusive)apply(Object.fromEntries(['cfg_scale','sampler','scheduler'].map(key=>[key,profile.defaults?.[key]??spec.defaults[key]])));
    const guidanceOption=$('sweep-axis').querySelector('[data-sweep-guidance]'); guidanceOption.disabled=!spec.guidance;
    $('sweep-axis').querySelector('[data-sweep-cfg]').disabled=!spec.negative || !profile.exclusive;
    if ($('sweep-axis').selectedOptions[0]?.disabled) $('sweep-axis').value='seed';
    renderPresets();
    const saved=drafts.get(modelName + ':' + activeTool);
    if (reset && saved && [...$('preset').options].some(option=>option.value===saved.preset)) $('preset').value=saved.preset;
    renderReferences(); update();
  }
  function renderPresets() {
    if (!$('preset')) return;
    const old = $('preset').value;
    const model=state.models.find(row=>row.id===modelName);
    const choices=[['','Model defaults'],...state.presets.filter(row=>row.model_id&&model?.model_id?row.model_id===model.model_id:row.model===modelName).map(row=>[row.id,row.name+' · v'+row.version+(row.published?' · shared':'')])];
    if(JSON.stringify(choices)===JSON.stringify([...$('preset').options].map(option=>[option.value,option.text])))return;
    $('preset').replaceChildren(...choices.map(([value,label])=>new Option(label,value)));
    if ([...$('preset').options].some(row => row.value === old)) $('preset').value = old;
  }
  /** What the chosen model does with reference images: how many, how, and whether small ones can be enlarged. */
  function referenceRules() {
    const profile = state.models.find(row => row.id === $('model').value)?.image_workflow || {};
    const most = profile.limits?.max_references ?? 2;
    return {most, editing: profile.family === 'sdxl', upscaler: Boolean(profile.upscaling),
            pixels: Number($('reference-mp').value || profile.reference_megapixels || 1) * 1e6, known: Boolean(profile.family)};
  }
  /** True when a reference is below the size it is prepared at, as the server computes it (aspect kept, multiples of 16). */
  function tooSmall(size, pixels) {
    if (!size) return false;
    const factor = Math.sqrt(pixels / (size.width * size.height));
    return size.width * size.height < Math.max(16, Math.round(size.width * factor / 16) * 16) * Math.max(16, Math.round(size.height * factor / 16) * 16);
  }
  /** A reason generation cannot start with these references, or ''. */
  function referenceProblem() {
    const rules = referenceRules();
    if (!rules.known) return '';
    if (references.length > rules.most) return rules.most ? 'This model uses at most ' + rules.most + ' reference image' + (rules.most === 1 ? '' : 's') + '; remove ' + (references.length - rules.most) + '.' : 'This model does not use reference images; remove them to generate.';
    const small = references.findIndex(id => tooSmall(sizes[id], rules.pixels));
    if (small >= 0 && !rules.upscaler) return 'Reference ' + (small + 1) + ' is smaller than this model’s reference size and the model has no upscaler: use a larger image or a lower reference size.';
    if (mask && !references.length) return 'An edit mask needs a reference image.';
    return '';
  }
  function renderReferences() {
    const rules = referenceRules();
    const signature=JSON.stringify([rules,references.map(id=>[id,sizes[id],state.references.find(row=>row.id===id)?.filename])]);
    if(signature!==referenceSignature){
    referenceSignature=signature;
    $('references').replaceChildren();
    references.forEach((id, index) => {
      const used = index < rules.most, size = sizes[id];
      const small = tooSmall(size, rules.pixels);
      const fragment = clone('reference'), card=fragment.querySelector('.ig-reference');
      card.classList.toggle('mw-reference-unused',!used); card.classList.toggle('mw-reference-bad',small && !rules.upscaler);
      const image=fragment.querySelector('img'); image.src=imageURL(id); image.alt='Reference '+(index+1);
      fragment.querySelector('[data-name]').textContent=(index+1)+'. '+(state.references.find(row=>row.id===id)?.filename || 'Reference image');
      fragment.querySelector('[data-detail]').textContent=size ? size.width+' × '+size.height : 'Saved reference';
      const badge=fragment.querySelector('[data-reference-state]');
      badge.textContent=!used?'Not used':small&&!rules.upscaler?'Too small':small?'AI enlargement':rules.editing?'Start image':'Conditioning';
      badge.classList.add(!used||small&&!rules.upscaler?'text-bg-danger':'text-bg-secondary');
      const up=fragment.querySelector('[data-up]'); up.disabled=!index;
      up.onclick=()=>{[references[index-1],references[index]]=[id,references[index-1]];mask=null;renderReferences();};
      fragment.querySelector('[data-remove]').onclick=()=>{references=references.filter(one=>one!==id);mask=null;renderReferences();};
      $('references').append(fragment);
    });
    }
    const off = rules.known && rules.most === 0, full = references.length >= rules.most;
    $('reference-box').classList.toggle('mw-reference-off', off);
    $('upload').disabled = off || (rules.known && full) || uploading>0; $('library').disabled = off || (rules.known && full) || uploading>0;
    $('resize-upload').disabled=$('resize-library').disabled=uploading>0;
    $('reference-count').textContent = rules.known ? references.length + ' / ' + rules.most : String(references.length);
    $('reference-count').className = 'badge rounded-pill ' + (references.length > rules.most ? 'text-bg-danger' : references.length ? 'text-bg-success' : 'text-bg-light border');
    const help = !rules.known ? 'Choose a model to see whether it uses reference images.'
      : off ? '✗ This model does not use reference images.'
      : (rules.editing ? 'Optional: add one starting image to redraw it. Strength controls how much changes; without a reference, generation uses the prompt.'
         : '✓ Up to ' + rules.most + ' image' + (rules.most === 1 ? '' : 's') + (rules.most > 1 ? '. Image 1 is the main one; say what to take from each, as in “the person from image 1 in the jacket from image 2”.' : '.'))
        + (rules.upscaler ? ' Small reference images are enlarged first.' : ' No upscaler: each reference image needs at least ' + (rules.pixels / 1e6).toFixed(1) + ' MP.');
    $('reference-help').textContent = help;
    $('reference-help').className = 'mw-reference-help small mb-2 ' + (off ? 'text-danger-emphasis' : rules.known ? 'text-success-emphasis' : 'text-secondary');
    $('strength-wrap').hidden = !(rules.editing && references.length);
    $('mask').disabled = !references.length || uploading>0;

    $('reference-box').hidden = activeTool === 'resize';
    $('reference-options').hidden = !references.length;
    $('mask-wrap').hidden = !references.length;
    $('clear-mask').hidden = !mask;
    const problem = activeTool === 'resize' ? resizeProblem() : referenceProblem();
    $('generate').disabled = !state.models.length || Boolean(problem) || submitting || uploading > 0;
    $('availability').textContent = problem || (state.models.length ? '' : $('availability').textContent);
    $('availability').classList.toggle('text-danger', Boolean(problem));
  }
  async function upload(file, kind = 'reference') {
    if (file.size > 20 * 1024 * 1024) throw new Error('Reference files must be 20 MiB or smaller.');
    const encoded = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file); });
    const row = await api('assets', {filename: file.name, kind, b64_json: encoded, temporary: $('temporary').checked});
    if (row.width) sizes[row.id] = {width: row.width, height: row.height};
    return row;
  }
  function showSelected() {
    const one=state.assets.find(row=>row.id===selected);
    $('empty').hidden=Boolean(one);$('canvas').hidden=!one;$('large').hidden=!one;
    $('preview-label').hidden=true;
    ['delete','reuse','prev','next','zoom','favorite','export-settings','fullscreen','resize-selected'].forEach(name=>$(name).disabled=!one);
    $('download').hidden=!one;
    $('result-title').textContent=one ? one.metadata?.settings?.detail_resize?'Resized with AI detail':'Generated image' : 'No image selected';
    $('result-counter').textContent=one ? (state.assets.indexOf(one)+1)+' / '+state.assets.length : '0 / '+state.assets.length;
    const signature=JSON.stringify([selected,one?.metadata,one?.width,one?.height]);
    const changed=signature!==detailsSignature;detailsSignature=signature;
    if(changed)$('result-settings').replaceChildren();
    if(one) {
      const address=imageURL(one.id,'content');
      if($('large').getAttribute('src')!==address)$('large').src=address;
      $('download').href=address+'?download=1';
      $('image-info').textContent=one.width+' × '+one.height+' · seed '+(one.metadata?.seed??'unknown')+(one.temporary?' · temporary':'');
      $('favorite').textContent=favorites.has(one.id)?'★':'☆';$('favorite').setAttribute('aria-pressed',String(favorites.has(one.id)));
      const values=one.metadata?.settings || {};
      const facts={Model:values.model || 'Unavailable',Seed:one.metadata?.seed??'Unknown',Size:one.width+' × '+one.height,
        Steps:values.steps??'Unknown',Sampler:values.sampler||'Unknown',Prompt:values.prompt||'Not retained'};
      if(values.detail_resize)facts['Detail strength']=values.strength;
      if(changed)for(const [key,value] of Object.entries(facts)){const dt=element('dt',key),dd=element('dd',String(value));$('result-settings').append(dt,dd);}
    } else $('image-info').textContent='';
    $('thumbnails').querySelectorAll('button').forEach(node=>node.setAttribute('aria-selected',String(node.dataset.id===selected)));
    const before=$('compare-with').value;
    const choices=state.assets.filter(row=>row.id!==selected);
    if(resizeSource && !choices.some(row=>row.id===resizeSource.id) && resizeSource.id!==selected)choices.unshift(resizeSource);
    const options=choices.map(row=>[row.id,row.filename||'Image · seed '+(row.metadata?.seed??'unknown')]);
    if(JSON.stringify(options)!==JSON.stringify([...$('compare-with').options].map(option=>[option.value,option.text])))$('compare-with').replaceChildren(...options.map(([value,label])=>new Option(label,value)));
    if(choices.some(row=>row.id===before))$('compare-with').value=before;
    $('compare').disabled=!one||!choices.length;$('compare-with').disabled=!choices.length;
    if(!$('compare-pane').hidden && choices.length){$('compare-pane').querySelector('img').src=imageURL($('compare-with').value,'content');}
    else if(!choices.length){$('compare-pane').hidden=true;$('canvas').classList.remove('is-comparing');$('compare').setAttribute('aria-pressed','false');}
  }
  function step(direction) {
    const index = state.assets.findIndex(row => row.id === selected);
    selected = state.assets[(index + direction + state.assets.length) % state.assets.length]?.id || null; showSelected();
  }
  function renderGallery() {
    const signature=JSON.stringify([state.assets.map(row=>[row.id,row.width,row.height]),[...favorites],$('library-filter').value]);
    if(signature!==gallerySignature){
      gallerySignature=signature;
      if(!state.assets.some(row=>row.id===selected))selected=state.assets[0]?.id||null;
      $('thumbnails').replaceChildren();
      state.assets.filter(row=>$('library-filter').value==='all'||favorites.has(row.id)).forEach(row=>{
        const fragment=clone('result'),button=fragment.querySelector('button');button.dataset.id=row.id;button.setAttribute('role','option');
        const image=fragment.querySelector('img');image.src=imageURL(row.id);image.alt='Image, seed '+(row.metadata?.seed??'unknown');image.loading='lazy';
        fragment.querySelector('[data-title]').textContent=(favorites.has(row.id)?'★ ':'')+(row.metadata?.settings?.detail_resize?'AI detail resize':'Generated image');
        fragment.querySelector('[data-detail]').textContent=row.width+' × '+row.height+' · seed '+(row.metadata?.seed??'unknown');
        button.onclick=()=>{selected=row.id;runningPreview='';showSelected();};$('thumbnails').append(fragment);
      });
      $('library-empty').hidden=Boolean($('thumbnails').children.length);
    }
    showSelected();
  }
  function renderJobs() {
    const signature = JSON.stringify(state.jobs.map(row => [row.id, row.status, row.request_counts, row.progress?.stage, row.progress?.step, row.error]));
    if (signature !== jobsSignature) {
      jobsSignature = signature; $('jobs').replaceChildren();
      state.jobs.forEach(row => {
        const fragment=clone('job');
        const progress=row.progress||{},failed=progress.stage==='failed'||row.error||progress.error||row.request_counts?.failed>0;
        fragment.querySelector('[data-title]').textContent=row.model||row.settings?.model||row.settings?.request?.model||'Image job';
        fragment.querySelector('[data-state]').textContent=failed?'Failed':row.status.replaceAll('_',' ');
        const counts=row.request_counts||{};
        fragment.querySelector('[data-detail]').textContent=row.error||progress.error?.message||[progress.stage,progress.step?progress.step+'/'+progress.total_steps:'',counts.total?((counts.completed||0)+' / '+counts.total+' images'):''].filter(Boolean).join(' · ');
        const meter=fragment.querySelector('progress');
        if(counts.total)meter.value=100*(counts.completed||0)/counts.total;else meter.removeAttribute('value');
        const pause=fragment.querySelector('[data-pause]'),stop=fragment.querySelector('[data-stop]');
        pause.hidden=!['in_progress','paused'].includes(row.status)||row.id.startsWith('quiet_');
        stop.hidden=!['in_progress','paused'].includes(row.status);
        pause.textContent=row.status==='paused'?'Resume':'Pause after image';
        pause.onclick=safe(async()=>{await api('jobs/'+encodeURIComponent(row.id),{action:row.status==='paused'?'resume':'pause'});await refresh();});
        stop.onclick=safe(async()=>{await api('jobs/'+encodeURIComponent(row.id),{action:'cancel'});await refresh();});
        $('jobs').append(fragment);
      });
    }
    $('queue-empty').hidden=Boolean(state.jobs.length);
    const preview = state.jobs.find(row => row.status === 'in_progress' && row.progress?.b64_json)?.progress;
    $('live').hidden = !preview || !$('preview').checked;
    if (preview && $('preview').checked && preview.b64_json !== runningPreview) {
      runningPreview = preview.b64_json; $('live-image').src = 'data:image/png;base64,' + preview.b64_json;
      $('live-info').textContent = 'Step ' + preview.step + ' of ' + preview.total_steps;
    }
    if (preview && $('preview').checked && !selected) {
      $('canvas').hidden=false; $('large').src = $('live-image').src; $('large').hidden = false; $('empty').hidden = true; $('preview-label').hidden = false;
    }
  }
  async function refresh() {
    if (polling || document.hidden || !root.isConnected) return;
    polling = true;
    try {
      state = await api('state');
      if (!root.isConnected) return;
      if (refreshProblem) { message(''); refreshProblem = false; }
      if (!older.length) moreImages = state.assets.length === 100;
      const latest = new Set(state.assets.map(row => row.id));
      state.assets.push(...older.filter(row => !latest.has(row.id)));
      $('older').hidden = !moreImages;
      const before = $('model').value;
      if (JSON.stringify(state.models.map(row => row.id)) !== JSON.stringify([...$('model').options].map(row => row.value))) {
        $('model').replaceChildren(...state.models.map(row => new Option(row.id, row.id)));
        const wanted=before||modelName;
        if (state.models.some(row => row.id === wanted)) $('model').value = wanted;
        chooseModel(Boolean(before)||!modelName||$('model').value!==modelName);
      }
      $('generate').disabled = !state.models.length || submitting || uploading > 0;
      $('availability').textContent = state.models.length ? '' : 'No available image model. An administrator can install the optional runtime and add a model under Models → Images.';
      $('temporary-wrap').hidden = !state.can_unlogged;
      state.references.forEach(row => { if (row.width) sizes[row.id] = {width: row.width, height: row.height}; });
      renderPresets(); renderGallery(); renderJobs(); renderReferences(); update();
    } catch (error) { refreshProblem = true; message(error.message); } finally { polling = false; }
  }
  function dialog(title, content) { $('dialog-title').textContent = title; $('dialog-content').replaceChildren(content); $('dialog').showModal(); }
  function resizePlan() {
    const p=profile(),limits=p.limits||{},targetWidth=Number($('resize-width').value),targetHeight=Number($('resize-height').value);
    const failed=error=>({error,targetWidth,targetHeight});
    if(p.family!=='sdxl'||!p.exclusive||(limits.max_references??1)<1)return failed('Choose an exclusive SDXL workflow that accepts a source image.');
    if(!resizeSource)return failed('Upload an image or choose a saved source first.');
    if(![targetWidth,targetHeight].every(value=>Number.isInteger(value)&&value>=16&&value<=8192))return failed('Choose whole-number output dimensions from 16 to 8192 pixels.');
    if(targetWidth*targetHeight>33554432)return failed('The final image must be at most 32 megapixels.');
    const maxWidth=limits.max_width||2048,maxHeight=limits.max_height||2048,maxPixels=limits.max_pixels||2097152;
    const factor=Math.min(1,maxWidth/targetWidth,maxHeight/targetHeight,Math.sqrt(maxPixels/(targetWidth*targetHeight)));
    let width=Math.max(256,Math.floor(targetWidth*factor/16)*16),height=Math.max(256,Math.floor(targetHeight*factor/16)*16);
    const ceilWidth=Math.max(256,Math.ceil(targetWidth/16)*16),ceilHeight=Math.max(256,Math.ceil(targetHeight/16)*16);
    if(ceilWidth<=maxWidth&&ceilHeight<=maxHeight&&ceilWidth*ceilHeight<=maxPixels){width=ceilWidth;height=ceilHeight;}
    if(width>maxWidth||height>maxHeight||width*height>maxPixels)return failed('This aspect ratio exceeds the workflow’s working-size limits. Choose a less extreme shape.');
    const largest=Math.max(targetWidth/width,targetHeight/height),maxScale=limits.max_upscale??(p.upscaling?4:1);
    if(largest>Math.min(4,maxScale))return failed('The requested size exceeds this workflow’s AI upscale limit. Choose a smaller final size.');
    if(!p.upscaling&&(largest>1||resizeSource.width*resizeSource.height<width*height))return failed('This size needs a configured AI upscaler. Add one under Models → Images, or choose a compatible workflow.');
    if(resizeSource.temporary&&!$('temporary').checked)return failed('This source is temporary; enable Temporary to keep the run unlogged.');
    return {width,height,targetWidth,targetHeight,error:''};
  }
  function resizeProblem() { return resizePlan().error; }
  function renderResizeSource() {
    $('resize-source').hidden=!resizeSource;
    if(resizeSource){$('resize-image').src=imageURL(resizeSource.id);$('resize-name').textContent=resizeSource.filename||'Selected image';$('resize-source-size').textContent=resizeSource.width+' × '+resizeSource.height;}
  }
  function chooseResizeSource(row) {
    resizeSource=row;
    if(row.temporary&&state.can_unlogged)$('temporary').checked=true;
    const scale=Number($('resize-scale').value)||2;
    $('resize-width').value=row.width*scale;$('resize-height').value=row.height*scale;
    renderResizeSource();update();showSelected();
  }
  function update() {
    const resizing=activeTool==='resize',spec=SuiteImageProfiles.spec(profile().family);
    $('prompt-editor').hidden=resizing;$('resize-panel').hidden=!resizing;$('prompt').required=!resizing;
    $('prompt').disabled=resizing;$('negative').disabled=resizing||!spec.negative;
    for(const id of ['resize-width','resize-height','resize-strength','resize-prompt'])$(id).disabled=!resizing;
    for(const id of ['width','height','n'])$(id).readOnly=resizing;
    $('upscale').disabled=resizing;
    $('generate').textContent=resizing?'Resize & add detail':'Generate';
    if(resizing){
      const plan=resizePlan();
      if(!plan.error){$('width').value=plan.width;$('height').value=plan.height;}
      $('n').value=1;
      $('resize-help').textContent=plan.error||'Detail pass: '+plan.width+' × '+plan.height+' → final '+plan.targetWidth+' × '+plan.targetHeight+' px. AI enlargement is used wherever the source or result needs enlarging.';
      $('resize-strength-value').value=$('resize-strength').value;
    }
    const width=Number($('width').value),height=Number($('height').value),n=Number($('n').value),scale=resizing?1:Number($('upscale').value);
    $('pixel-count').textContent=(width*height/1e6).toFixed(2)+' MP';
    $('output-size').textContent=resizing?'Working size for the detail pass; final dimensions are set beside the source.':'Final output: '+Math.round(width*scale)+' × '+Math.round(height*scale)+' px';
    $('steps-value').value=$('steps').value;$('strength-value').value=$('strength').value;
    $('prompt-count').textContent=$('prompt').value.length.toLocaleString()+' characters';
    $('run-summary').textContent=resizing?'1 image · AI detail resize':n+' image'+(n===1?'':'s')+' · '+width+' × '+height;
    const sweepVisible=!resizing&&($('sweep-enabled').checked||Boolean($('sweep').value.trim()));
    $('sweep-panel').hidden=!sweepVisible;root.dataset.sweep=String(sweepVisible);
    const count=$('sweep-values').value.split(',').filter(value=>value.trim()).length;
    $('sweep-summary').textContent=count+' values × '+n+' image'+(n===1?'':'s')+' = '+count*n+' images';
    for(const button of all('[data-mw-ratio]')){const [w,h]=button.dataset.mwRatio.split(':').map(Number),active=Math.abs(width/height-w/h)<.015;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));button.disabled=resizing;}
    for(const button of all('[data-mw-tool]')){const tool=button.dataset.mwTool;button.classList.toggle('active',tool===activeTool);button.setAttribute('aria-pressed',String(tool===activeTool));}
    renderReferences();
  }
  function setView(view) {
    root.dataset.view=view;
    for(const button of all('[data-mw-view]')){const active=button.dataset.mwView===view;button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));}
  }
  function setTool(tool) {
    const mode=tool==='resize'?'resize':'generate';
    if(mode!==activeTool){rememberDraft();activeTool=mode;references=[];mask=null;chooseModel();}
    setView('generate');
    if(tool==='resize'&&profile().family!=='sdxl'){
      const compatible=state.models.find(row=>row.image_workflow?.family==='sdxl'&&row.image_workflow.exclusive&&(row.image_workflow.limits?.max_references??1)>0);
      if(compatible){$('model').value=compatible.id;chooseModel();}
    }
    if(tool==='reference'||tool==='mask'){
      $('reference-box').open=true;
      if(!referenceRules().most)message('This model supports text-to-image only. Select a model with reference support.');
      else if(tool==='mask'&&!references.length)message('Add a reference first, then upload its composite mask.');
      $('reference-box').scrollIntoView({block:'nearest'});
    }
    if(tool==='sweep'){$('sweep-enabled').checked=true;update();$('sweep-panel').scrollIntoView({block:'nearest'});}
    update();
  }
  function sweepSettings() {
    if($('sweep').value.trim())return JSON.parse($('sweep').value);
    if(!$('sweep-enabled').checked)return {};
    const axis=$('sweep-axis').value,parts=$('sweep-values').value.split(',').map(value=>value.trim());
    if(!parts.length||parts.length>32||parts.some(value=>!value))throw new Error('Enter 1–32 comma-separated sweep values.');
    const values=parts.map(Number);
    if(values.some(value=>!Number.isFinite(value)))throw new Error('Sweep values must be finite numbers.');
    if(values.length*Number($('n').value)>128)throw new Error('A sweep can produce at most 128 images.');
    return {[axis]:values};
  }
  function downloadJSON(value) {
    const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
    const link=element('a');link.href=url;link.download='image-settings.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  function bindDropzone(id,receive) {
    const box=$(id);
    for(const name of ['dragenter','dragover'])box.addEventListener(name,event=>{event.preventDefault();box.classList.add('drag-over');});
    box.addEventListener('dragleave',()=>box.classList.remove('drag-over'));
    box.addEventListener('drop',event=>{event.preventDefault();box.classList.remove('drag-over');receive([...event.dataTransfer.files]);});
  }
  async function addReferences(files) {
    if(uploading)throw new Error('Wait for the current image upload to finish.');
    if(files.length+references.length>referenceRules().most)throw new Error('This workflow accepts at most '+referenceRules().most+' reference images.');
    const owner=modelName+':'+activeTool,uploaded=[];uploading++;renderReferences();
    try {
      for(const file of files){const row=await upload(file);uploaded.push(row.id);}
      if(owner===modelName+':'+activeTool)references.push(...uploaded.filter(id=>!references.includes(id)));
      else {const draft=drafts.get(owner);if(draft)draft.references.push(...uploaded.filter(id=>!draft.references.includes(id)));}
      await refresh();
    } finally {uploading--;renderReferences();}
  }
  async function resizeUpload(files) {
    if(uploading)throw new Error('Wait for the current image upload to finish.');
    if(files.length!==1)throw new Error('Choose one source image for resizing.');
    uploading++;renderReferences();
    try{chooseResizeSource(await upload(files[0]));await refresh();}finally{uploading--;renderReferences();}
  }
  root.addEventListener('input',event=>{if(event.target.matches('input,textarea,select'))update();});
  root.addEventListener('change',event=>{if(event.target.matches('input,textarea,select'))update();});
  all('[data-mw-tool]').forEach(button=>button.onclick=()=>setTool(button.dataset.mwTool));
  all('[data-mw-view]').forEach(button=>button.onclick=()=>setView(button.dataset.mwView));
  all('[data-mw-ratio]').forEach(button=>button.onclick=()=>{const [w,h]=button.dataset.mwRatio.split(':').map(Number),alignment=SuiteImageProfiles.spec(profile().family).alignment;$('height').value=Math.round(Number($('width').value)*h/w/alignment)*alignment;update();});
  all('[data-mw-style]').forEach(button=>button.onclick=()=>{const styles={cinematic:'Cinematic lighting, deliberate composition, subtle film grain.',editorial:'Editorial photography, natural texture, considered color palette.',illustration:'Handcrafted illustration, expressive shapes, subtle paper texture.'};$('prompt').value=($('prompt').value.trim()+' '+styles[button.dataset.mwStyle]).trim();update();});
  $('random-seed').onclick=()=>{$('seed').value=-1;};
  $('prompt-tips').onclick=()=>{const content=element('p',$('prompt-help').textContent);dialog('Prompt tips',content);};
  $('insert-text').onclick=()=>{const text=$('image-text').value.trim();if(text){$('prompt').value+=' Include the exact text '+JSON.stringify(text)+' with clear, readable lettering.';update();}};
  $('open-sweep').onclick=()=>setTool('sweep');
  $('toolkit').onclick=()=>dialog('Creative toolkit',$('toolkit-template').content.cloneNode(true));
  $('model-guide').onclick=()=>{
    const content=element('div'),p=profile(),spec=SuiteImageProfiles.spec(p.family);
    content.append(element('p',spec.label+' · '+(spec.guidance?'Prompt guidance':spec.negative?'CFG and negative prompts':'Distilled guidance')+' · '+(p.limits?.max_references??0)+' reference images.'));
    const help=element('div');SuiteImageProfiles.help(help,p.family);content.append(help);
    const link=element('a','Open image generation guide','btn btn-outline-secondary btn-sm');link.href='/docs/modules/mirrorwall';content.append(link);dialog('Model guide',content);
  };
  $('library-filter').onchange=renderGallery;
  $('favorite').onclick=()=>{if(!selected)return;if(favorites.has(selected))favorites.delete(selected);else favorites.add(selected);renderGallery();};
  $('export-settings').onclick=()=>{const one=state.assets.find(row=>row.id===selected);if(one)downloadJSON({seed:one.metadata?.seed,settings:one.metadata?.settings||{},width:one.width,height:one.height});};
  $('compare').onclick=()=>{const enabled=$('compare-pane').hidden;$('compare-pane').hidden=!enabled;$('canvas').classList.toggle('is-comparing',enabled);$('compare').setAttribute('aria-pressed',String(enabled));if(enabled)$('compare-pane').querySelector('img').src=imageURL($('compare-with').value,'content');};
  $('compare-with').onchange=()=>{if(!$('compare-pane').hidden)$('compare-pane').querySelector('img').src=imageURL($('compare-with').value,'content');};
  $('fullscreen').onclick=()=>{if(document.fullscreenElement)document.exitFullscreen?.();else $('canvas').requestFullscreen?.().catch(()=>message('Expanded viewing is unavailable in this browser.'));};
  $('resize-upload').onchange=safe(async event=>{if(event.target.files.length)await resizeUpload([...event.target.files]);event.target.value='';});
  $('resize-selected').onclick=()=>{const row=state.assets.find(one=>one.id===selected);if(row)chooseResizeSource(row);};
  $('resize-scale').onchange=()=>{if(resizeSource&&$('resize-scale').value!=='custom'){const scale=Number($('resize-scale').value);$('resize-width').value=resizeSource.width*scale;$('resize-height').value=resizeSource.height*scale;}update();};
  for(const id of ['width','height'])$('resize-'+id).addEventListener('input',()=>{if(resizeSource&&$('resize-lock').checked){const other=id==='width'?'height':'width';$('resize-'+other).value=Math.round(Number($('resize-'+id).value)*resizeSource[other]/resizeSource[id]);}$('resize-scale').value='custom';update();});
  bindDropzone('dropzone',files=>safe(()=>addReferences(files))());
  bindDropzone('resize-dropzone',files=>safe(()=>resizeUpload(files))());
  $('model').onchange = () => chooseModel();
  $('older').onclick = safe(async () => {
    const last = state.assets[state.assets.length - 1]; if (!last) return;
    const page = await api('assets?kind=output&before=' + encodeURIComponent(last.created));
    older.push(...page.assets); moreImages = page.assets.length === 100;
    await refresh();
  });
  $('preset').onchange = () => { const preset = state.presets.find(row => row.id === $('preset').value); if (preset) apply(preset.settings); else chooseModel(); };
  $('form').onsubmit = safe(async event => {
    event.preventDefault(); if(submitting||uploading)return; $('generate').disabled = true;
    try {
      const problem=activeTool==='resize'?resizeProblem():referenceProblem();
      if(problem)throw new Error(problem);
      submitting=true;
      const sweep=activeTool==='resize'?{}:sweepSettings();
      await api('jobs', {request: settings(), references:activeTool==='resize'?[resizeSource.id]:references, mask:activeTool==='resize'?null:mask, sweep, temporary: $('temporary').checked});
      await refresh();
    } finally { submitting=false;renderReferences(); }
  });
  $('upload').onchange = safe(async event => {
    await addReferences([...event.target.files]);event.target.value='';
  });
  $('mask').onchange = safe(async event => {
    if(!event.target.files[0])return;
    const owner=modelName+':'+activeTool;uploading++;renderReferences();
    try{const row=await upload(event.target.files[0],'mask');if(owner===modelName+':'+activeTool)mask=row.id;else if(drafts.has(owner))drafts.get(owner).mask=row.id;}
    finally{uploading--;renderReferences();}
  });
  $('clear-mask').onclick = () => { mask = null; $('mask').value = ''; $('clear-mask').hidden = true; renderReferences(); };
  $('reference-mp').addEventListener('input', renderReferences);
  $('strength').addEventListener('input', () => { $('strength-value').textContent = Number($('strength').value).toFixed(2); });
  $('library').onclick = safe(async () => {
    await refresh(); const list = element('div', undefined, 'mw-library');
    state.references.forEach(row => {
      const entry = element('div');
      const button = element('button', undefined, 'btn btn-outline-secondary'); const image = element('img'); image.src = imageURL(row.id); image.alt = row.filename;
      button.append(image, element('span', row.filename, 'small d-block text-truncate'));
      button.onclick = () => {
        if(activeTool==='resize')chooseResizeSource(row);
        else {if(!references.includes(row.id)&&references.length>=referenceRules().most){message('This workflow’s reference slots are full.');return;}if(!references.includes(row.id))references.push(row.id);renderReferences();}
        $('dialog').close();
      };
      const remove = element('button', 'Delete', 'btn btn-link text-danger btn-sm');
      remove.onclick = safe(async () => { if (!await window.suiteConfirmLoss('Delete this saved reference permanently?')) return; await api('assets/' + encodeURIComponent(row.id), undefined, 'DELETE'); references = references.filter(id => id !== row.id); if(resizeSource?.id===row.id){resizeSource=null;renderResizeSource();}renderReferences(); entry.remove(); await refresh(); });
      const download = element('a', 'Download', 'btn btn-link btn-sm'); download.href = imageURL(row.id, 'content') + '?download=1';
      entry.append(button, download, remove); list.append(entry);
    });
    if (!state.references.length) list.append(element('p', 'No saved references yet.'));
    dialog('Saved references', list);
  });
  $('resize-library').onclick=$('library').onclick;
  $('save-preset').onclick = () => {
    const form = element('form'); const label = element('label', 'Workflow name', 'form-label'); const input = element('input', undefined, 'form-control mb-3'); input.required = true; input.maxLength = 120; label.append(input); form.append(label);
    const publish = element('input', undefined, 'form-check-input me-2'); publish.type = 'checkbox';
    if (state.can_manage) { const choice = element('label', undefined, 'form-check-label d-block mb-3'); choice.append(publish, document.createTextNode('Share with other users')); form.append(choice); }
    form.append(element('button', 'Save workflow', 'btn btn-primary'));
    form.onsubmit = safe(async event => { event.preventDefault(); const values = settings(); delete values.model; delete values.prompt; delete values.partial_images; delete values.detail_resize; await api('presets', {name: input.value, model: modelName, settings: values, published: publish.checked}); $('dialog').close(); await refresh(); });
    dialog('Save workflow', form); input.focus();
  };
  $('prev').onclick = () => step(-1); $('next').onclick = () => step(1);
  $('zoom').onclick = () => { const original = $('large').parentElement.classList.toggle('mw-original'); $('zoom').textContent = original ? 'Fit to view' : 'Original size'; };
  $('reuse').onclick = () => {
    const one = state.assets.find(row => row.id === selected); if (!one) return;
    const values = one.metadata.settings || {};
    const model = state.models.find(row => one.metadata.model_id ? row.model_id === one.metadata.model_id : row.id === values.model);
    if (!model) { message('The original image model is unavailable. Choose a model and workflow manually.'); return; }
    setTool(values.detail_resize?'resize':'generate');
    $('model').value = model.id; chooseModel(); apply({...values, seed: one.metadata.seed});
    const originalJob = state.jobs.find(row => row.id === one.metadata.job);
    const original = originalJob?.settings;
    references = original?.references || []; mask = original?.mask || null;
    if(values.detail_resize){const id=references[0]||originalJob?.references?.[0];resizeSource=state.references.find(row=>row.id===id)||state.assets.find(row=>row.id===id)||null;$('resize-scale').value='custom';$('resize-width').value=values.detail_resize.width;$('resize-height').value=values.detail_resize.height;$('resize-strength').value=values.strength;$('resize-prompt').value=values.prompt||'';renderResizeSource();}
    update(); $('clear-mask').hidden = !mask; $('clear-mask').textContent = 'Remove saved mask'; $('mask').value = ''; renderReferences();
  };
  $('delete').onclick = safe(async () => { if (!selected || !await window.suiteConfirmLoss('Delete this generated image permanently?')) return; await api('assets/' + encodeURIComponent(selected), undefined, 'DELETE'); older = older.filter(row => row.id !== selected); await refresh(); });
  const handleKey = event => {
    if(!root.isConnected||$('dialog').open)return;
    if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();setView('generate');$('form').requestSubmit();return;}
    if(event.target.closest('input,textarea,select,button,a,summary'))return;
    if(event.key==='ArrowLeft'){event.preventDefault();step(-1);}if(event.key==='ArrowRight'){event.preventDefault();step(1);}
  };
  document.addEventListener('keydown',handleKey);
  refresh();
  const poller=setInterval(()=>{
    if(!root.isConnected){clearInterval(poller);document.removeEventListener('visibilitychange',refresh);document.removeEventListener('keydown',handleKey);return;}
    refresh();
  },1500);
  document.addEventListener('visibilitychange', refresh);
})();
