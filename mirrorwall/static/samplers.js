/* Sampler comparison (Models → Sampler comparison). One prompt runs through every chosen scheduler × sampler as one
   ordinary image job (/mirrorwall-api/jobs with a sampler/scheduler sweep); results are read per line from
   /mirrorwall-api/jobs/<id>/lines. The viewer's zoom applies one focus point to every image, so each shows the same region. */
(() => {
  'use strict';
  const root = document.getElementById('samplers');
  if (!root) return;
  const $ = id => document.getElementById('sc-' + id), P = window.SuiteImageProfiles;
  const limit = Number(root.dataset.limit) || 512;
  let models = [], job = null, timer = null, tiles = new Map(), groups = new Map(), focus = {x: 50, y: 50}, zoom = 1;
  const drafts = new Map();  // per model: the form as you left it, in page memory

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
    const draft = drafts.get(one?.id);
    if (draft) fill(draft);
    else if (reset) fill({width: defaults.width, height: defaults.height, steps: defaults.steps, cfg_scale: locked ? 1 : defaults.cfg_scale, guidance: defaults.guidance});
    count();
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
            cfg_scale: read('cfg_scale'), guidance: read('guidance'), seed: read('seed'), n: read('n')};
  }

  function count() {
    const schedulers = checked('scheduler').length, samplers = checked('sampler').length, n = Math.max(1, Number($('n').value) || 1);
    const total = schedulers * samplers * n;
    $('count').textContent = `${schedulers} scheduler${schedulers === 1 ? '' : 's'} × ${samplers} sampler${samplers === 1 ? '' : 's'}${n > 1 ? ' × ' + n : ''} = ${total} image${total === 1 ? '' : 's'}` +
      (total > limit ? ` · at most ${limit}; untick some` : '');
    $('run').disabled = !total || total > limit || !model() || Boolean(job && running(job.status));
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
    return {request: asked, sweep: {scheduler: checked('scheduler'), sampler: checked('sampler')}};
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
    } catch (error) { say(error.message, true); count(); }
  }

  /** Show a comparison job: its grid is built from the job's lines and filled in as images finish. */
  async function open(identifier, settings = null) {
    clearTimeout(timer);
    job = {id: identifier, status: 'in_progress', settings};
    tiles = new Map(); groups = new Map(); $('grid').replaceChildren();
    $('earlier').value = identifier;
    await refresh();
  }

  async function refresh() {
    if (!job) return;
    let found;
    try { found = await api('jobs/' + encodeURIComponent(job.id) + '/lines'); }
    catch (error) { say(error.message, true); timer = setTimeout(refresh, 4000); return; }
    job.status = found.status;
    let next = found.lines.find(line => line.state === 'waiting');
    for (const line of found.lines) draw(line, running(found.status) && found.status !== 'paused' && line === next, found.progress);
    const done = found.lines.filter(line => line.state === 'done').length, failed = found.lines.filter(line => line.state === 'failed').length;
    const summary = `${done} of ${found.lines.length} done` + (failed ? ` · ${failed} failed` : '');
    $('pause').hidden = $('stop').hidden = !running(found.status);
    $('pause').textContent = found.status === 'paused' ? 'Resume' : 'Pause';
    if (running(found.status)) {
      say((found.status === 'paused' ? 'Paused · ' : found.status === 'cancelling' ? 'Stopping · ' : 'Running · ') + summary + (found.progress?.stage === 'queued' ? ' · waiting for the GPU' : ''));
      timer = setTimeout(refresh, found.status === 'paused' ? 4000 : 1500);
    } else {
      say(`Comparison ${found.status === 'completed' ? 'complete' : found.status}: ${summary}.` + (failed ? ' Failed combinations show the reason; a sampler or scheduler this model’s runtime does not offer is refused.' : ''));
    }
    count();
  }

  function draw(line, active, progress) {
    let tile = tiles.get(line.line);
    if (!tile) {
      let group = groups.get(line.scheduler);
      if (!group) {
        const fragment = document.getElementById('sc-group-template').content.cloneNode(true);
        group = fragment.querySelector('.sc-group'); group.querySelector('[data-title]').textContent = 'Scheduler · ' + line.scheduler;
        groups.set(line.scheduler, group); $('grid').append(fragment);
      }
      tile = document.getElementById('sc-tile-template').content.firstElementChild.cloneNode(true);
      tile.dataset.line = line.line;
      tile.querySelector('[data-sampler]').textContent = line.sampler;
      tile.querySelector('[data-scheduler]').textContent = line.scheduler;
      tile.querySelector('[data-pick]').setAttribute('aria-label', `Select ${line.sampler} with ${line.scheduler}`);
      group.querySelector('[data-tiles]').append(tile);
      tiles.set(line.line, tile);
    }
    const state = line.state === 'waiting' && active ? 'running' : line.state;
    tile.dataset.state = state;
    const frame = tile.querySelector('.sc-frame'), image = tile.querySelector('img'), placeholder = tile.querySelector('[data-placeholder]');
    frame.setAttribute('aria-label', `${line.sampler} with ${line.scheduler}` + (state === 'failed' ? ', failed' : state === 'done' ? '' : ', not finished'));
    tile.querySelector('[data-detail]').textContent = `seed ${line.seed ?? '—'} · ${line.steps} steps` + (line.cfg_scale !== undefined && line.cfg_scale !== null ? ` · CFG ${line.cfg_scale}` : '');
    if (line.state === 'done' && image.dataset.asset !== line.asset_id) {
      image.dataset.asset = line.asset_id;
      image.dataset.thumbnail = '/mirrorwall-api/assets/' + encodeURIComponent(line.asset_id) + '/thumbnail';
      image.dataset.full = '/mirrorwall-api/assets/' + encodeURIComponent(line.asset_id) + '/content';
      image.src = zoom > 1 ? image.dataset.full : image.dataset.thumbnail;
      image.alt = `${line.sampler} with ${line.scheduler}, seed ${line.seed}`;
      image.hidden = false; placeholder.hidden = true;
      if (line.width && line.height) $('grid').style.setProperty('--sc-aspect', line.width + ' / ' + line.height);
    } else if (line.state !== 'done') {
      placeholder.hidden = false;
      placeholder.textContent = state === 'failed' ? (line.error || 'Failed') : state === 'running' ? (progress?.stage === 'queued' ? 'Waiting for the GPU' : 'Generating' + (progress?.step ? ` · step ${progress.step}` : '')) : 'Waiting';
    }
    filter();
  }

  /** One focus point and zoom for every image: the same region of each, side by side. */
  function setZoom(value) {
    zoom = Math.min(8, Math.max(1, value));
    $('zoom').value = zoom; $('zoom-value').textContent = zoom + '×';
    $('grid').style.setProperty('--sc-zoom', zoom);
    for (const image of $('grid').querySelectorAll('img[data-full]')) {
      const wanted = zoom > 1 ? image.dataset.full : image.dataset.thumbnail;
      if (image.getAttribute('src') !== wanted) image.src = wanted;
    }
  }
  function setFocus(x, y) {
    focus = {x: Math.min(100, Math.max(0, x)), y: Math.min(100, Math.max(0, y))};
    $('grid').style.setProperty('--sc-x', focus.x + '%'); $('grid').style.setProperty('--sc-y', focus.y + '%');
  }
  function filter() {
    const only = $('selected-only').checked;
    for (const tile of tiles.values()) tile.hidden = only && !tile.hasAttribute('data-picked');
    for (const group of groups.values()) group.hidden = only && !group.querySelector('.sc-tile:not([hidden])');
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
    for (const row of jobs.filter(one => one.settings?.sweep?.sampler || one.settings?.sweep?.scheduler)) {
      const sweep = row.settings.sweep, when = window.suiteFormatTime?.(row.created * 1000, false) ?? new Date(row.created * 1000).toLocaleString();
      select.add(new Option(`${row.model} · ${(sweep.scheduler || ['—']).length}×${(sweep.sampler || ['—']).length} · ${when}`, row.id));
    }
    select.value = keep;
  }

  function wire() {
    $('model').addEventListener('change', () => applyModel());
    $('form').addEventListener('input', event => {
      if (model() && event.target.name && !['scheduler', 'sampler'].includes(event.target.name)) drafts.set(model().id, values());
      count();
    });
    $('form').addEventListener('submit', run);
    $('new-seed').addEventListener('click', () => { $('seed').value = randomSeed(); if (model()) drafts.set(model().id, values()); });
    root.querySelectorAll('[data-sc-all],[data-sc-none]').forEach(button => button.addEventListener('click', () => {
      const kind = button.dataset.scAll || button.dataset.scNone;
      root.querySelectorAll(`input[name="${kind}"]`).forEach(input => { input.checked = Boolean(button.dataset.scAll); });
      count();
    }));
    $('earlier').addEventListener('change', () => { if ($('earlier').value) open($('earlier').value); });
    $('pause').addEventListener('click', () => control(job?.status === 'paused' ? 'resume' : 'pause'));
    $('stop').addEventListener('click', () => control('cancel'));
    $('zoom').addEventListener('input', () => setZoom(Number($('zoom').value)));
    $('size').addEventListener('change', () => { $('grid').dataset.size = $('size').value; });
    $('selected-only').addEventListener('change', filter);
    const grid = $('grid');
    grid.addEventListener('pointermove', event => {
      const frame = event.target.closest('.sc-frame'); if (!frame || zoom <= 1) return;
      const box = frame.getBoundingClientRect();
      setFocus((event.clientX - box.left) / box.width * 100, (event.clientY - box.top) / box.height * 100);
    });
    grid.addEventListener('wheel', event => {
      if (!event.target.closest('.sc-frame') || !event.ctrlKey) return;  // Ctrl + wheel zooms; plain scrolling still scrolls
      event.preventDefault(); setZoom(zoom + (event.deltaY < 0 ? .5 : -.5));
    }, {passive: false});
    grid.addEventListener('keydown', event => {
      if (!event.target.classList.contains('sc-frame')) return;
      const moves = {ArrowLeft: [-5, 0], ArrowRight: [5, 0], ArrowUp: [0, -5], ArrowDown: [0, 5]};
      if (moves[event.key]) { event.preventDefault(); if (zoom === 1) setZoom(2); setFocus(focus.x + moves[event.key][0], focus.y + moves[event.key][1]); }
      else if (event.key === '+' || event.key === '=') { event.preventDefault(); setZoom(zoom + .5); }
      else if (event.key === '-') { event.preventDefault(); setZoom(zoom - .5); }
      else if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); const pick = event.target.closest('.sc-tile').querySelector('[data-pick]'); pick.click(); }
    });
    grid.addEventListener('change', event => {
      if (!event.target.matches('[data-pick]')) return;
      event.target.closest('.sc-tile').toggleAttribute('data-picked', event.target.checked);
      filter();
    });
  }

  async function start() {
    wire();
    $('seed').value = randomSeed();
    try {
      const state = await api('state');
      models = state.models || [];  // already only the image models you may use
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
