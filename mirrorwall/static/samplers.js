/* Sampler comparison (Models → Sampler comparison). One prompt runs through every chosen scheduler × sampler as one
   ordinary image job (/mirrorwall-api/jobs with a sampler/scheduler sweep); results are read per line from
   /mirrorwall-api/jobs/<id>/lines into one continuous grid. Models that take reference images get them on every line.
   The results use the shared comparison grid (comparison-grid.js): one zoom for every image, size, selection and filter. */
(() => {
  'use strict';
  const root = document.getElementById('samplers');
  if (!root) return;
  const $ = id => document.getElementById('sc-' + id), P = window.SuiteImageProfiles;
  const limit = Number(root.dataset.limit) || 512;
  let runs = new Map();  // earlier comparison id -> status
  let models = [], saved = [], references = [], job = null, timer = null;
  const grid = window.SuiteComparisonGrid.create({prefix: 'sc', store: 'sampler-comparison'});
  const drafts = new Map();  // per model: the form and its reference images as you left them, in page memory
  const sizes = {};  // reference asset id -> {width, height}

  function say(text, problem = false) {
    $('error').hidden = !problem; if (problem) $('error').textContent = text;
    $('status').textContent = problem ? '' : text;
  }
  async function api(path, {method = 'GET', body} = {}) {
    const answer = await fetch('/mirrorwall-api/' + path, {method, cache: 'no-store', headers: body ? {'content-type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
    const found = await answer.json().catch(() => ({}));
    if (!answer.ok) throw new Error(found.error?.message || `The image service answered ${answer.status}.`);
    return found;
  }

  const model = () => models.find(one => one.id === $('model').value);
  const profile = one => one?.image_workflow || {};
  const randomSeed = () => Math.floor(Math.random() * 2147483647);
  const checked = kind => [...root.querySelectorAll(`input[name="${kind}"]:checked`)].map(input => input.value);

  /** The selected model's family decides which fields apply, its profile their defaults and limits. */
  function applyModel(reset = true) {
    const one = model(), workflow = profile(one), spec = P.spec(workflow.family), limits = workflow.limits || {};
    const defaults = {...spec.defaults, ...(workflow.defaults || {})};
    $('negative-wrap').hidden = !spec.negative;
    $('cfg-wrap').hidden = !spec.negative;
    $('guidance-wrap').hidden = !spec.guidance;
    // A shared model measured at CFG 1 has no room measured for the negative-prompt pass, so its CFG stays 1.
    const locked = !workflow.exclusive && Number(defaults.cfg_scale) === 1;
    $('cfg').disabled = locked;
    $('cfg-note').hidden = !(locked && spec.negative);
    $('cfg-note').textContent = 'This shared-residency model was measured at CFG 1, so its CFG stays 1. An exclusive model entry may change it.';
    $('model-note').textContent = spec.label + (workflow.exclusive ? ' · exclusive model entry' : ' · shared residency');
    for (const [name, key, fallback] of [['width', 'max_width', 2048], ['height', 'max_height', 2048], ['steps', 'max_steps', 100]]) $(name).max = limits[key] || fallback;
    $('width').step = $('height').step = spec.alignment;
    $('reference-mp').max = workflow.reference_megapixels || 1;
    const draft = drafts.get(one?.id);
    references = [...(draft?.references || [])];
    if (draft) fill(draft);
    else if (reset) fill({width: defaults.width, height: defaults.height, steps: defaults.steps, cfg_scale: locked ? 1 : defaults.cfg_scale, guidance: defaults.guidance,
                          reference_megapixels: workflow.reference_megapixels || 1});
    renderReferences();
  }
  function fill(values) {
    for (const [name, value] of Object.entries(values)) {
      const field = $('form').elements.namedItem(name);
      if (field && value !== undefined && value !== null && !(field instanceof RadioNodeList)) field.value = value;
    }
  }
  function values() {
    const form = $('form'), read = name => form.elements.namedItem(name).value;
    return {prompt: read('prompt'), negative_prompt: read('negative_prompt'), width: read('width'), height: read('height'), steps: read('steps'),
            cfg_scale: read('cfg_scale'), guidance: read('guidance'), seed: read('seed'), n: read('n'),
            reference_megapixels: read('reference_megapixels'), strength: read('strength'), references: [...references]};
  }
  const remember = () => { if (model()) drafts.set(model().id, values()); };

  /** What the chosen model does with reference images, as the Image generation page reads it. */
  function referenceRules() {
    const workflow = profile(model());
    return {most: workflow.limits?.max_references ?? 2, editing: workflow.family === 'sdxl', upscaler: Boolean(workflow.upscaling),
            pixels: Number($('reference-mp').value || workflow.reference_megapixels || 1) * 1e6};
  }
  /** True when a reference is below the size it is prepared at (aspect kept, multiples of 16), as the server computes it. */
  function tooSmall(size, pixels) {
    if (!size) return false;
    const factor = Math.sqrt(pixels / (size.width * size.height));
    return size.width * size.height < Math.max(16, Math.round(size.width * factor / 16) * 16) * Math.max(16, Math.round(size.height * factor / 16) * 16);
  }
  function referenceProblem() {
    const rules = referenceRules();
    if (references.length > rules.most) return rules.most ? `This model uses at most ${rules.most} reference image${rules.most === 1 ? '' : 's'}; remove ${references.length - rules.most}.` : '';
    const small = references.findIndex(id => tooSmall(sizes[id], rules.pixels));
    return small >= 0 && !rules.upscaler ? `Reference ${small + 1} is smaller than the reference size and this model has no upscaler: use a larger image or a lower reference size.` : '';
  }
  /** The reference section shows only for models that take references; the chosen images are sent with every combination. */
  function renderReferences() {
    const rules = referenceRules(), full = references.length >= rules.most;
    $('reference-box').hidden = !rules.most || !model();
    $('references').replaceChildren(...references.map((id, index) => {
      const row = document.getElementById('sc-reference-template').content.firstElementChild.cloneNode(true), size = sizes[id];
      const image = row.querySelector('img'); image.src = '/mirrorwall-api/assets/' + encodeURIComponent(id) + '/thumbnail'; image.alt = 'Reference ' + (index + 1);
      row.querySelector('[data-name]').textContent = (index + 1) + '. ' + (saved.find(one => one.id === id)?.filename || 'Reference image');
      row.querySelector('[data-detail]').textContent = size ? size.width + ' × ' + size.height : 'Saved reference';
      row.querySelector('[data-up]').disabled = !index;
      row.querySelector('[data-up]').addEventListener('click', () => { [references[index - 1], references[index]] = [id, references[index - 1]]; remember(); renderReferences(); });
      row.querySelector('[data-remove]').addEventListener('click', () => { references = references.filter(one => one !== id); remember(); renderReferences(); });
      return row;
    }));
    $('upload').disabled = $('saved').disabled = full;
    $('reference-mp-wrap').hidden = !references.length;
    $('strength-wrap').hidden = !(rules.editing && references.length);
    // A field shown is a field needed: left empty it would be sent as 0, below its minimum (QA-087).
    $('reference-mp').required = !$('reference-mp-wrap').hidden;
    $('strength').required = !$('strength-wrap').hidden;
    $('reference-help').textContent = (rules.editing ? 'Optional: one starting image that every combination redraws.'
      : `Optional: up to ${rules.most} image${rules.most === 1 ? '' : 's'}, used by every combination.`) + (rules.upscaler ? ' Small images are enlarged first.' : '');
    $('saved').replaceChildren(new Option('Add a saved reference…', ''), ...saved.filter(one => !references.includes(one.id)).map(one => new Option(one.filename || one.id, one.id)));
    count();
  }
  async function upload(files) {
    for (const file of files) {
      if (references.length >= referenceRules().most) break;
      if (file.size > 20 * 1024 * 1024) throw new Error('Reference files must be 20 MiB or smaller.');
      const encoded = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file); });
      const row = await api('assets', {method: 'POST', body: {filename: file.name, kind: 'reference', b64_json: encoded}});
      if (row.width) sizes[row.id] = {width: row.width, height: row.height};
      saved.unshift(row); references.push(row.id);
    }
  }

  function count() {
    const schedulers = checked('scheduler').length, samplers = checked('sampler').length, n = Math.max(1, Number($('n').value) || 1);
    const total = schedulers * samplers * n;
    $('count').textContent = `${schedulers} scheduler${schedulers === 1 ? '' : 's'} × ${samplers} sampler${samplers === 1 ? '' : 's'}${n > 1 ? ' × ' + n : ''} = ${total} image${total === 1 ? '' : 's'}` +
      (total > limit ? ` · at most ${limit}; untick some` : '');
    const problem = model() ? referenceProblem() : '';
    if (problem) $('count').textContent += ' · ' + problem;
    $('run').disabled = !total || total > limit || !model() || Boolean(problem) || Boolean(job && running(job.status));
    return total;
  }
  const running = status => ['validating', 'in_progress', 'finalizing', 'cancelling', 'paused'].includes(status);

  /** The request the job runs: the settings once, with the scheduler as the outer sweep so each scheduler tries every sampler. */
  function request() {
    const one = model(), spec = P.spec(profile(one).family), form = $('form');
    if (!form.reportValidity()) throw new Error('Check the highlighted settings.');
    const asked = {model: one.id, prompt: $('prompt').value, width: Number($('width').value), height: Number($('height').value),
      steps: Number($('steps').value), seed: Number($('seed').value), n: Number($('n').value)};
    if (spec.negative) { asked.cfg_scale = Number($('cfg').value); if ($('negative').value.trim()) asked.negative_prompt = $('negative').value; }
    if (spec.guidance) asked.guidance = Number($('guidance').value);
    const sent = {request: asked, sweep: {scheduler: checked('scheduler'), sampler: checked('sampler')}};
    if (references.length) {
      sent.references = [...references];
      asked.reference_megapixels = Number($('reference-mp').value);
      if (referenceRules().editing) asked.strength = Number($('strength').value);
    }
    return sent;
  }

  async function run(event) {
    event.preventDefault();
    if (!$('prompt').value.trim()) { say('Write a prompt first.', true); $('prompt').focus(); return; }
    let body;
    try { body = request(); } catch (error) { say(error.message, true); return; }
    $('run').disabled = true;
    try {
      const started = await api('jobs', {method: 'POST', body});
      say(`Started ${count()} images. They are saved to your image library as they finish; you can leave this page and open the comparison again later.`);
      await open(started.id, body);
      reloadRuns();
    } catch (error) { say(error.message, true); count(); }
  }

  /** Show a comparison job: its grid is built from the job's lines and filled in as images finish. */
  async function open(identifier, settings = null) {
    clearTimeout(timer);
    job = {id: identifier, status: 'in_progress', settings};
    grid.reset(['scheduler', 'sampler'], ['sampler', 'scheduler']);  // filter by scheduler then sampler; captions name the sampler first
    $('earlier').value = identifier;
    await refresh();
  }

  async function refresh() {
    if (!job) return;
    let found;
    try { found = await api('jobs/' + encodeURIComponent(job.id) + '/lines'); }
    catch (error) { say(error.message, true); timer = setTimeout(refresh, 4000); return; }
    job.status = found.status;
    if (runs.has(job.id)) runs.set(job.id, found.status); else if (!running(found.status)) reloadRuns();
    runButtons();
    let next = found.lines.find(line => line.state === 'waiting');
    for (const line of found.lines) grid.line(line, {active: running(found.status) && found.status !== 'paused' && line === next, progress: found.progress});
    const summary = SuiteComparisonGrid.summary(found.lines);
    $('pause').hidden = $('stop').hidden = !running(found.status);
    $('pause').textContent = found.status === 'paused' ? 'Resume' : 'Pause';
    if (running(found.status)) {
      say((found.status === 'paused' ? 'Paused · ' : found.status === 'cancelling' ? 'Stopping · ' : 'Running · ') + summary + (found.progress?.stage === 'queued' ? ' · waiting for the GPU' : ''));
      timer = setTimeout(refresh, found.status === 'paused' ? 4000 : 1500);
    } else {
      say(`Comparison ${found.status === 'completed' ? 'complete' : found.status}: ${summary}.` + (found.lines.some(line => line.state === 'failed') ? ' Failed combinations show the reason; a sampler or scheduler this model’s runtime does not offer is refused.' : ''));
    }
    count();
  }

  /** Hide settings gives the results the whole width; the settings and their drafts stay as they were. */
  function setWide(wide) {
    $('form').hidden = wide; $('layout').toggleAttribute('data-wide', wide);
    $('hide-settings').textContent = wide ? 'Show settings' : 'Hide settings';
    $('hide-settings').setAttribute('aria-pressed', String(wide));
    grid.keep('wide', wide);
  }

  async function control(action) {
    if (!job) return;
    try { await api('jobs/' + encodeURIComponent(job.id), {method: 'POST', body: {action}}); }
    catch (error) { say(error.message, true); }
    clearTimeout(timer); refresh();
  }

  function earlier(jobs) {
    const select = $('earlier'), keep = select.value;
    select.replaceChildren(new Option('Earlier comparisons…', ''));
    runs = new Map();
    for (const row of jobs.filter(one => one.settings?.sweep?.sampler || one.settings?.sweep?.scheduler)) {
      runs.set(row.id, row.status);
      const sweep = row.settings.sweep, when = window.suiteFormatTime?.(row.created * 1000, false) ?? new Date(row.created * 1000).toLocaleString();
      select.add(new Option(`${row.model} · ${(sweep.scheduler || ['—']).length}×${(sweep.sampler || ['—']).length} · ${when}`, row.id));
    }
    select.value = runs.has(keep) ? keep : '';
    runButtons();
  }
  /** Delete run acts on the open (or chosen) finished run; Clear all on every finished one. Running runs are kept. */
  function runButtons() {
    const chosen = $('earlier').value, status = chosen === job?.id ? job.status : runs.get(chosen);
    $('delete-run').disabled = !chosen || running(status);
    $('clear-runs').disabled = ![...runs.keys()].some(id => !running(id === job?.id ? job.status : runs.get(id)));
  }
  async function reloadRuns() {
    try { earlier((await api('state')).jobs || []); } catch (error) { say(error.message, true); }
  }
  function closeRun(identifiers) {
    if (!job || !identifiers.includes(job.id)) return;
    clearTimeout(timer); job = null; grid.reset(); count();
  }
  async function deleteRun() {
    const identifier = $('earlier').value; if (!identifier) return;
    try {
      await api('jobs/' + encodeURIComponent(identifier), {method: 'DELETE'});
      closeRun([identifier]); say('Deleted the comparison and its images.');
    } catch (error) { say(error.message, true); }
    reloadRuns();
  }
  async function clearRuns() {
    try {
      const found = await api('comparisons', {method: 'DELETE'});
      closeRun(found.deleted);
      say(`Deleted ${found.deleted.length} comparison${found.deleted.length === 1 ? '' : 's'} and their images.` + (found.running ? ` ${found.running} still running ${found.running === 1 ? 'was' : 'were'} kept.` : ''));
    } catch (error) { say(error.message, true); }
    reloadRuns();
  }

  function wire() {
    $('model').addEventListener('change', () => applyModel());
    $('form').addEventListener('input', event => {
      // Choosing a model already names the new one, so its draft is restored by applyModel, not written over with
      // the form of the model before it (QA-075). Every other field's change is kept at once.
      if (event.target.name && event.target !== $('model') && !['scheduler', 'sampler'].includes(event.target.name)) remember();
      if (event.target === $('reference-mp')) renderReferences(); else count();
    });
    $('form').addEventListener('submit', run);
    $('new-seed').addEventListener('click', () => { $('seed').value = randomSeed(); remember(); });
    $('upload').addEventListener('change', async () => {
      const files = [...$('upload').files]; $('upload').value = ''; $('upload').disabled = true;
      try { await upload(files); $('error').hidden = true; } catch (error) { say(error.message, true); }
      remember(); renderReferences();
    });
    $('saved').addEventListener('change', () => {
      if ($('saved').value) references.push($('saved').value);
      remember(); renderReferences();
    });
    root.querySelectorAll('[data-sc-all],[data-sc-none]').forEach(button => button.addEventListener('click', () => {
      const kind = button.dataset.scAll || button.dataset.scNone;
      root.querySelectorAll(`input[name="${kind}"]`).forEach(input => { input.checked = Boolean(button.dataset.scAll); });
      count();
    }));
    $('earlier').addEventListener('change', () => { runButtons(); if ($('earlier').value) open($('earlier').value); });
    $('delete-run').addEventListener('click', deleteRun);
    $('clear-runs').addEventListener('click', clearRuns);
    $('pause').addEventListener('click', () => control(job?.status === 'paused' ? 'resume' : 'pause'));
    $('stop').addEventListener('click', () => control('cancel'));
    $('hide-settings').addEventListener('click', () => setWide($('form').hidden === false));
  }

  async function start() {
    wire();
    setWide(grid.kept('wide') === true);
    $('seed').value = randomSeed();
    try {
      const state = await api('state');
      models = state.models || [];  // already only the image models you may use
      saved = state.references || [];
      saved.forEach(row => { if (row.width) sizes[row.id] = {width: row.width, height: row.height}; });
      $('model').replaceChildren(...models.map(one => new Option(one.id, one.id)));
      earlier(state.jobs || []);
      if (!models.length) { say('No image models are available to you. Add one under Models → Images.'); count(); return; }
      applyModel();
      say('Choose the prompt and settings, untick samplers or schedulers you do not want, then Run comparison.');
      const latest = (state.jobs || []).find(row => (row.settings?.sweep?.sampler || row.settings?.sweep?.scheduler) && running(row.status));
      if (latest) open(latest.id);
    } catch (error) { say(error.message, true); }
  }
  start();
})();
