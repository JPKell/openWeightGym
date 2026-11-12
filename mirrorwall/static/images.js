/* Private image studio. API keys stay in the authenticated server session. */
(() => {
  'use strict';
  const root = document.querySelector('#mirrorwall');
  if (!root) return;
  const $ = id => document.getElementById('mw-' + id);
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const message = error => { $('message').hidden = !error; $('message').textContent = error || ''; };
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
  let runningPreview = '', polling = false, refreshProblem = false;
  let older = [], moreImages = false;
  const sizes = {};  // reference asset id -> {width, height}, from uploads and the saved-reference list
  const settings = () => {
    const values = Object.fromEntries(new FormData($('form')));
    fields.forEach(name => { if (!['sampler', 'scheduler'].includes(name)) values[name] = Number(values[name]); });
    if ($('negative-wrap').hidden || !values.negative_prompt) delete values.negative_prompt;
    if ($('strength-wrap').hidden) delete values.strength;
    values.live_previews = $('preview').checked;
    const loras = {};
    $('loras').querySelectorAll('[data-lora]').forEach(node => { if (node.value !== '') loras[node.dataset.lora] = Number(node.value); });
    if (Object.keys(loras).length) values.loras = loras;
    return values;
  };
  function apply(values) {
    fields.concat(['prompt', 'negative_prompt']).forEach(name => {
      const node = $('form').elements.namedItem(name);
      if (node && values[name] !== undefined) node.value = values[name];
    });
    $('loras').querySelectorAll('[data-lora]').forEach(node => { node.value = values.loras?.[node.dataset.lora] ?? node.dataset.default; });
  }
  function chooseModel(reset = true) {
    const one = state.models.find(row => row.id === $('model').value);
    if (!one) return;
    modelName = one.id;
    const profile = one.image_workflow || {};
    $('negative-wrap').hidden = !SuiteImageProfiles.spec(profile.family).negative;
    $('guidance').closest('.col-6').hidden = !SuiteImageProfiles.spec(profile.family).guidance;
    $('cfg').closest('.col-6').hidden = !SuiteImageProfiles.spec(profile.family).negative;
    $('width').step = $('height').step = SuiteImageProfiles.spec(profile.family).alignment;
    $('upscale').querySelectorAll('option').forEach(node => { node.disabled = Number(node.value) > (profile.limits?.max_upscale || (profile.upscaling ? 4 : 1)); });
    $('reference-mp').max = profile.reference_megapixels || 1;
    if (reset) $('reference-mp').value = profile.reference_megapixels || 1;
    $('loras').replaceChildren();
    for (const [name, definition] of Object.entries(profile.loras || {})) {
      const label = element('label', name + ' · LoRA scale (0 = off)', 'form-label');
      const input = element('input', undefined, 'form-control mb-2');
      input.type = 'number'; input.min = '-4'; input.max = '4'; input.step = '.05';
      input.dataset.default = String(definition.scale ?? 0); input.placeholder = 'Profile default: ' + input.dataset.default; input.dataset.lora = name;
      label.append(input); $('loras').append(label);
    }
    if (reset) apply(profile.defaults || {});
    renderReferences();
    const limits = profile.limits || {};
    for (const [name, limit] of [['width','max_width'], ['height','max_height'], ['steps','max_steps'], ['n','max_images']]) {
      if (limits[limit]) $('form').elements.namedItem(name).max = limits[limit];
    }
    renderPresets();
  }
  function renderPresets() {
    if (!$('preset')) return;
    const old = $('preset').value;
    $('preset').replaceChildren(new Option('Model defaults', ''));
    const model=state.models.find(row=>row.id===modelName);
    state.presets.filter(row => row.model_id && model?.model_id ? row.model_id===model.model_id : row.model === modelName).forEach(row => $('preset').add(new Option(row.name + ' · v' + row.version + (row.published ? ' · shared' : ''), row.id)));
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
    $('references').replaceChildren();
    references.forEach((id, index) => {
      const used = index < rules.most, size = sizes[id];
      const small = tooSmall(size, rules.pixels);
      const card = element('div', undefined, 'mw-reference' + (used ? '' : ' mw-reference-unused') + (small && !rules.upscaler ? ' mw-reference-bad' : ''));
      const image = element('img'); image.src = imageURL(id); image.alt = 'Reference ' + (index + 1); card.append(image);
      const tag = !used ? ['Not used', 'text-bg-danger', 'This model uses only ' + rules.most + ' reference image' + (rules.most === 1 ? '' : 's') + '.']
        : small && !rules.upscaler ? ['Too small', 'text-bg-danger', size.width + '×' + size.height + ' is below this model’s reference size and it has no upscaler.']
        : small ? ['Enlarged', 'text-bg-info', size.width + '×' + size.height + ' will be enlarged with the model’s upscaler first.']
        : [rules.editing ? 'Start image' : index === 0 ? '1 · main' : String(index + 1), 'text-bg-success', size ? size.width + '×' + size.height : ''];
      const badge = element('span', tag[0], 'badge mw-reference-tag ' + tag[1]); badge.title = tag[2]; card.append(badge);
      const buttons = element('div', undefined, 'd-flex justify-content-between mt-1');
      if (index) {
        const left = element('button', '←', 'btn btn-outline-secondary'); left.type = 'button'; left.ariaLabel = 'Move reference earlier';
        left.onclick = () => { [references[index-1], references[index]] = [id, references[index-1]]; renderReferences(); }; buttons.append(left);
      }
      const remove = element('button', 'Remove', 'btn btn-outline-secondary'); remove.type = 'button';
      remove.onclick = () => { references = references.filter(one => one !== id); renderReferences(); }; buttons.append(remove); card.append(buttons); $('references').append(card);
    });
    const off = rules.known && rules.most === 0, full = references.length >= rules.most;
    $('reference-box').classList.toggle('mw-reference-off', off);
    $('upload').disabled = off || (rules.known && full); $('library').disabled = off || (rules.known && full);
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
    $('mask').disabled = !references.length;
    $('mask-state').textContent = references.length ? (mask ? 'Mask set' : 'Optional') : 'Needs a reference image';
    const problem = referenceProblem();
    $('generate').disabled = !state.models.length || Boolean(problem);
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
    const one = state.assets.find(row => row.id === selected);
    $('empty').hidden = Boolean(one); $('large').hidden = !one;
    $('preview-label').hidden = true;
    ['delete', 'reuse', 'prev', 'next', 'zoom'].forEach(name => { $(name).disabled = !one; });
    $('download').hidden = !one;
    if (one) {
      const address = imageURL(one.id, 'content');
      if ($('large').getAttribute('src') !== address) $('large').src = address;
      $('download').href = address + '?download=1';
      $('image-info').textContent = one.width + ' × ' + one.height + ' · seed ' + (one.metadata.seed ?? 'unknown') + (one.temporary ? ' · temporary' : '');
    } else $('image-info').textContent = '';
    $('thumbnails').querySelectorAll('button').forEach(node => node.setAttribute('aria-selected', node.dataset.id === selected ? 'true' : 'false'));
  }
  function step(direction) {
    const index = state.assets.findIndex(row => row.id === selected);
    selected = state.assets[(index + direction + state.assets.length) % state.assets.length]?.id || null; showSelected();
  }
  function renderGallery() {
    const signature = state.assets.map(row => row.id).join(',');
    if (signature !== gallerySignature) {
      gallerySignature = signature;
      if (!state.assets.some(row => row.id === selected)) selected = state.assets[0]?.id || null;
      $('thumbnails').replaceChildren();
      state.assets.forEach(row => {
        const button = element('button', undefined, 'mw-thumb'); button.dataset.id = row.id; button.setAttribute('role', 'option');
        const image = element('img'); image.src = imageURL(row.id); image.alt = 'Image, seed ' + (row.metadata.seed ?? 'unknown'); image.loading = 'lazy';
        button.append(image); button.onclick = () => { selected = row.id; runningPreview = ''; showSelected(); }; $('thumbnails').append(button);
      });
    }
    showSelected();
  }
  function renderJobs() {
    const signature = JSON.stringify(state.jobs.map(row => [row.id, row.status, row.request_counts, row.progress?.stage, row.progress?.step, row.error]));
    if (signature !== jobsSignature) {
      jobsSignature = signature; $('jobs').replaceChildren();
      state.jobs.forEach(row => {
        const card = element('div', undefined, 'mw-job studio-job frame');
        const failed = row.progress?.stage === 'failed' || row.error || row.progress?.error || row.request_counts?.failed > 0;
        const status = failed ? 'failed' : row.status;
        card.append(element('span', row.model || row.settings?.model || row.settings?.request?.model || 'Image job', 'fw-medium me-auto'));
        const progress = row.progress || {};
        card.append(element('span', status + (progress.stage && progress.stage !== status ? ' · ' + progress.stage : '') + (progress.step ? ' ' + progress.step + '/' + progress.total_steps : ''), 'text-secondary small'));
        if (row.error || progress.error) card.append(element('span', row.error || progress.error.message, 'text-danger small'));
        const action = (label, value) => {
          const button = element('button', label, 'btn btn-outline-secondary btn-sm');
          button.onclick = safe(async () => { await api('jobs/' + encodeURIComponent(row.id), {action: value}); await refresh(); }); card.append(button);
        };
        if (row.status === 'in_progress') { if (!row.id.startsWith('quiet_')) action('Pause after image', 'pause'); action('Stop', 'cancel'); }
        if (row.status === 'paused') { action('Resume', 'resume'); action('Stop', 'cancel'); }
        $('jobs').append(card);
      });
    }
    const preview = state.jobs.find(row => row.status === 'in_progress' && row.progress?.b64_json)?.progress;
    $('live').hidden = !preview || !$('preview').checked;
    if (preview && $('preview').checked && preview.b64_json !== runningPreview) {
      runningPreview = preview.b64_json; $('live-image').src = 'data:image/png;base64,' + preview.b64_json;
      $('live-info').textContent = 'Step ' + preview.step + ' of ' + preview.total_steps;
    }
    if (preview && $('preview').checked && !selected) {
      $('large').src = $('live-image').src; $('large').hidden = false; $('empty').hidden = true; $('preview-label').hidden = false;
    }
  }
  async function refresh() {
    if (polling || document.hidden) return;
    polling = true;
    try {
      state = await api('state');
      if (refreshProblem) { message(''); refreshProblem = false; }
      if (!older.length) moreImages = state.assets.length === 100;
      const latest = new Set(state.assets.map(row => row.id));
      state.assets.push(...older.filter(row => !latest.has(row.id)));
      $('older').hidden = !moreImages;
      const before = $('model').value;
      if (JSON.stringify(state.models.map(row => row.id)) !== JSON.stringify([...$('model').options].map(row => row.value))) {
        $('model').replaceChildren(...state.models.map(row => new Option(row.id, row.id)));
        if (state.models.some(row => row.id === before)) $('model').value = before;
        chooseModel();
      }
      $('generate').disabled = !state.models.length;
      $('availability').textContent = state.models.length ? '' : 'No available image model. An administrator can install the optional runtime and add a model under Models → Images.';
      $('temporary-wrap').hidden = !state.can_unlogged;
      state.references.forEach(row => { if (row.width) sizes[row.id] = {width: row.width, height: row.height}; });
      renderPresets(); renderGallery(); renderJobs(); renderReferences();
    } catch (error) { refreshProblem = true; message(error.message); } finally { polling = false; }
  }
  function dialog(title, content) { $('dialog-title').textContent = title; $('dialog-content').replaceChildren(content); $('dialog').showModal(); }
  $('model').onchange = () => chooseModel();
  $('older').onclick = safe(async () => {
    const last = state.assets[state.assets.length - 1]; if (!last) return;
    const page = await api('assets?kind=output&before=' + encodeURIComponent(last.created));
    older.push(...page.assets); moreImages = page.assets.length === 100;
    await refresh();
  });
  $('preset').onchange = () => { const preset = state.presets.find(row => row.id === $('preset').value); if (preset) apply(preset.settings); else chooseModel(); };
  $('form').onsubmit = safe(async event => {
    event.preventDefault(); $('generate').disabled = true;
    try {
      if (referenceProblem()) throw new Error(referenceProblem());
      const sweep = $('sweep').value.trim() ? JSON.parse($('sweep').value) : {};
      await api('jobs', {request: settings(), references, mask, sweep, temporary: $('temporary').checked});
      await refresh();
    } finally { $('generate').disabled = !state.models.length || Boolean(referenceProblem()); }
  });
  $('upload').onchange = safe(async event => {
    for (const file of event.target.files) { const row = await upload(file); if (!references.includes(row.id)) references.push(row.id); }
    renderReferences(); event.target.value = ''; await refresh();
  });
  $('mask').onchange = safe(async event => { if (event.target.files[0]) { mask = (await upload(event.target.files[0], 'mask')).id; $('clear-mask').hidden = false; renderReferences(); } });
  $('clear-mask').onclick = () => { mask = null; $('mask').value = ''; $('clear-mask').hidden = true; renderReferences(); };
  $('reference-mp').addEventListener('input', renderReferences);
  $('strength').addEventListener('input', () => { $('strength-value').textContent = Number($('strength').value).toFixed(2); });
  $('library').onclick = safe(async () => {
    await refresh(); const list = element('div', undefined, 'mw-library');
    state.references.forEach(row => {
      const entry = element('div');
      const button = element('button', undefined, 'btn btn-outline-secondary'); const image = element('img'); image.src = imageURL(row.id); image.alt = row.filename;
      button.append(image, element('span', row.filename, 'small d-block text-truncate'));
      button.onclick = () => { if (!references.includes(row.id)) references.push(row.id); renderReferences(); $('dialog').close(); };
      const remove = element('button', 'Delete', 'btn btn-link text-danger btn-sm');
      remove.onclick = safe(async () => { if (!await window.suiteConfirmLoss('Delete this saved reference permanently?')) return; await api('assets/' + encodeURIComponent(row.id), undefined, 'DELETE'); references = references.filter(id => id !== row.id); renderReferences(); entry.remove(); await refresh(); });
      const download = element('a', 'Download', 'btn btn-link btn-sm'); download.href = imageURL(row.id, 'content') + '?download=1';
      entry.append(button, download, remove); list.append(entry);
    });
    if (!state.references.length) list.append(element('p', 'No saved references yet.'));
    dialog('Saved references', list);
  });
  $('save-preset').onclick = () => {
    const form = element('form'); const label = element('label', 'Workflow name', 'form-label'); const input = element('input', undefined, 'form-control mb-3'); input.required = true; input.maxLength = 120; label.append(input); form.append(label);
    const publish = element('input', undefined, 'form-check-input me-2'); publish.type = 'checkbox';
    if (state.can_manage) { const choice = element('label', undefined, 'form-check-label d-block mb-3'); choice.append(publish, document.createTextNode('Share with other users')); form.append(choice); }
    form.append(element('button', 'Save workflow', 'btn btn-primary'));
    form.onsubmit = safe(async event => { event.preventDefault(); const values = settings(); delete values.model; delete values.prompt; delete values.partial_images; await api('presets', {name: input.value, model: modelName, settings: values, published: publish.checked}); $('dialog').close(); await refresh(); });
    dialog('Save workflow', form); input.focus();
  };
  $('prev').onclick = () => step(-1); $('next').onclick = () => step(1);
  $('zoom').onclick = () => { const original = $('large').parentElement.classList.toggle('mw-original'); $('zoom').textContent = original ? 'Fit to view' : 'Original size'; };
  $('reuse').onclick = () => {
    const one = state.assets.find(row => row.id === selected); if (!one) return;
    const values = one.metadata.settings || {};
    const model = state.models.find(row => row.model_id === one.metadata.model_id || row.id === values.model);
    if (!model) { message('The original image model is unavailable. Choose a model and workflow manually.'); return; }
    $('model').value = model.id; chooseModel(); apply({...values, seed: one.metadata.seed});
    const original = state.jobs.find(row => row.id === one.metadata.job)?.settings;
    references = original?.references || []; mask = original?.mask || null; $('clear-mask').hidden = !mask; $('clear-mask').textContent = 'Remove saved mask'; $('mask').value = ''; renderReferences();
  };
  $('delete').onclick = safe(async () => { if (!selected || !await window.suiteConfirmLoss('Delete this generated image permanently?')) return; await api('assets/' + encodeURIComponent(selected), undefined, 'DELETE'); older = older.filter(row => row.id !== selected); await refresh(); });
  document.addEventListener('keydown', event => { if (['INPUT','TEXTAREA','SELECT'].includes(event.target.tagName) || $('dialog').open) return; if (event.key === 'ArrowLeft') step(-1); if (event.key === 'ArrowRight') step(1); });
  refresh(); setInterval(refresh, 1500); document.addEventListener('visibilitychange', refresh);
})();
