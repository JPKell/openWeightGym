/* A self-contained Music prototype. Every edit and simulated job stays in this tab. */
(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('music-data').textContent);
  const $ = id => document.getElementById('sr-' + id);
  const byId = id => document.getElementById(id);
  const root = byId('soundroom');
  document.dispatchEvent(new Event('htmx:load'));
  const changePalette = () => {
    const choice = document.querySelector('[data-theme-form] input[value="' + byId('palette').value + '"]');
    choice.checked = true;
    choice.dispatchEvent(new Event('change', {bubbles:true}));
  };
  changePalette();
  byId('palette').addEventListener('change', changePalette);
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const clone = id => byId(id).content.cloneNode(true);
  const title = 'After the last light';
  const initialLyrics = '[verse]\nThe city folds its paper wings\nWe follow where the river bends\nA little light in everything\nA road that never has to end\n\n[chorus]\nAfter the last light\nWe are still here\nCarrying the night\nTill the sky turns clear';
  const presets = Object.fromEntries([...byId('music-presets').content.querySelectorAll('[data-preset]')].map(button => [button.dataset.preset, button.dataset.stylePrompt]));
  const notes = {
    studio: '01 / Session studio — a balanced workspace for shaping an idea, listening, and keeping the best takes within reach.',
    writer: '02 / Songwriter desk — a generous writing sheet, a compact direction rail, and a dedicated listening column.',
    lab: '03 / Take lab — a wide listening surface and a dense take ledger for seed experiments and fast comparisons.'
  };
  let takes = [], selected = null, source = null, scenario = 'normal', region = null;
  let currentModel = data.models[0].id, serial = 6, jobs = [], frame = null, drag = null;
  const drafts = new Map();
  const profile = () => data.models.find(model => model.id === $('model').value) || data.models[0];
  const seconds = value => Math.floor(value / 60) + ':' + String(Math.floor(value % 60)).padStart(2, '0');
  const status = (message, error = false) => {
    $('message').hidden = true;
    byId('music-status').hidden = true;
    const node = error ? $('message') : byId('music-status');
    node.textContent = message;
    node.hidden = !message;
  };
  const safe = action => async event => {
    try { await action(event); } catch (error) { status(error.message, true); }
  };

  // Reuse the live player, library toolbar and jobs heading; arrange them without copies.
  const results = document.querySelector('.sr-results');
  const player = results.querySelector('.sr-player');
  const queue = results.lastElementChild;
  queue.classList.add('music-queue');
  queue.classList.remove('mt-4');
  const library = make('section', undefined, 'music-library');
  library.setAttribute('aria-label', 'Take library');
  for (const node of [...results.children]) if (node !== player && node !== queue) library.append(node);
  library.append(clone('music-library-tools'));
  library.insertBefore(library.querySelector('.music-library-tools'), library.querySelector('#sr-list'));
  library.append(clone('music-empty-library'));
  results.insertBefore(library, queue);
  player.querySelector('.card-body').prepend(clone('music-player-intro'));
  $('prompt').after(clone('music-presets'));
  document.querySelector('.music-workbench').append(clone('music-no-lyrics-template'));
  byId('refine-style').addEventListener('click', () => $('prompt').focus());
  // Put the optional helper after the primary style controls.
  document.querySelector('[data-music-section="direction"]').append($('helper'));
  $('filter').placeholder = 'Search titles, models or tags…';
  $('filter').setAttribute('aria-label', 'Find a take');
  $('older').hidden = true;
  $('temporary-wrap').hidden = false;
  $('temporary').nextElementSibling.textContent = 'Temporary take · removed on reload';
  $('format').replaceChildren(new Option('WAV · demo audio', 'wav'));
  $('download').textContent = 'Download demo';
  $('share').textContent = 'Sharing preview';
  $('generate').textContent = 'Simulate generation';
  $('write').textContent = 'Use sample draft';
  $('write').parentElement.nextElementSibling.textContent = 'Loads prepared sample style and lyrics. No writing model is called in this preview.';
  document.querySelector('[data-music-section="lyrics"]').insertBefore(
    make('p', 'A place for your words. Use section tags to shape the song.', 'small text-secondary mb-3'), $('lyrics'));
  const loopHelp = $('clear-region').nextElementSibling;
  loopHelp.textContent = 'Drag the waveform or enter times to select a region.';

  function sampleTakes() {
    return ['Midnight transmission', 'After the last light', 'Quiet way home', 'Glasshouse', 'Blue hour', 'First train out'].map((name, index) => ({
      id: 'demo-' + index, name, model: index === 3 ? 'Stable Audio' : index === 4 ? 'HeartMuLa Songs' : 'ACE-Step Studio',
      seed: 4821 + index, favourite: [1, 4].includes(index), tags: index < 3 ? ['night-drive', 'synth-pop'] : ['sketch'],
      seconds: 16, audio: data.audio[index % 3], demo: index % 3, temporary: false,
      request: {prompt: Object.values(presets)[index % 3], lyrics: initialLyrics, duration: 16, n: 1, seed: 4821 + index, mode: 'song', instrumental: index === 3, bpm: 120, key: 'A minor', steps: 30, guidance: 7},
      created: 6 - index
    }));
  }
  function capture() {
    const request = {};
    for (const id of ['prompt', 'lyrics', 'mode', 'duration', 'n', 'seed', 'start', 'end']) request[id] = $(id).value;
    request.instrumental = $('instrumental').checked;
    request.temporary = $('temporary').checked;
    for (const node of root.querySelectorAll('[data-option]')) request[node.dataset.option] = node.value;
    request.source = source;
    return request;
  }
  function restore(request) {
    if (!request) return;
    for (const id of ['prompt', 'lyrics', 'mode', 'duration', 'n', 'seed', 'start', 'end']) {
      if (request[id] !== undefined && (id !== 'mode' || [...$('mode').options].some(option => option.value === request[id]))) $(id).value = request[id];
    }
    $('instrumental').checked = !profile().lyrics_required && !!request.instrumental;
    $('temporary').checked = !!request.temporary;
    for (const node of root.querySelectorAll('[data-option]')) if (request[node.dataset.option] !== undefined) node.value = request[node.dataset.option];
    source = request.source || null;
    chooseMode();
  }
  function field(id, text, value, options, target) {
    const label = make('label', text, 'form-label small');
    const input = make(options ? 'select' : 'input', undefined, options ? 'form-select form-select-sm' : 'form-control form-control-sm');
    input.id = 'sr-option-' + id;
    input.dataset.option = id;
    if (options) for (const option of options) input.add(new Option(option, option));
    else { input.type = 'number'; input.min = id === 'bpm' ? 40 : id === 'steps' ? 1 : 0; input.max = id === 'bpm' ? 240 : id === 'steps' ? 100 : 20; input.step = id === 'guidance' ? '.1' : '1'; }
    input.value = value;
    label.append(input);
    target.append(label);
  }
  function chooseModel(keep = true) {
    const model = profile();
    if (keep) drafts.set(currentModel, capture());
    currentModel = model.id;
    $('licence').textContent = model.licence + ' · ' + ({yes:'Commercial use allowed', no:'Non-commercial only', conditional:'Commercial use has conditions'}[model.commercial]);
    $('model-help').textContent = model.help;
    const labels = {song:'Create a song', continue:'Continue a track', cover:'Cover / restyle', repaint:'Repaint a range'};
    $('mode').replaceChildren(...(model.modes || ['song']).map(mode => new Option(labels[mode], mode)));
    $('mode-wrap').hidden = false;
    $('lyrics-wrap').hidden = !model.lyrics;
    byId('music-no-lyrics').hidden = !!model.lyrics;
    $('instrumental').disabled = !!model.lyrics_required;
    if (model.lyrics_required) $('instrumental').checked = false;
    $('lyrics').required = !!model.lyrics_required;
    $('duration').max = '600';
    $('n').max = '8';
    $('musical').replaceChildren();
    const musical = document.querySelector('[data-music-section="musical"]');
    musical.hidden = model.family !== 'ace_step';
    $('musical-wrap').hidden = musical.hidden;
    if (!musical.hidden) {
      field('bpm', 'Tempo · BPM', 120, null, $('musical'));
      field('key', 'Key', 'A minor', ['Auto', 'A minor', 'C major', 'D minor', 'E minor', 'G major'], $('musical'));
    }
    $('advanced').replaceChildren();
    $('advanced').append(make('p', 'Illustrative controls; production options come from the installed model profile.', 'form-text'));
    field('steps', 'Sampling steps', 30, null, $('advanced'));
    field('guidance', 'Guidance', 7, null, $('advanced'));
    restore(drafts.get(currentModel));
    chooseMode();
    if (!keep) return;
    status('Model changed. Each model keeps its own draft in this tab.');
  }
  function chooseMode() {
    const mode = $('mode').value;
    $('source-wrap').hidden = mode === 'song';
    $('window').hidden = mode !== 'repaint';
    $('lyrics').disabled = $('instrumental').checked && !$('instrumental').disabled;
    $('source-name').textContent = source ? source.name + ' · ' + seconds(source.seconds) : 'Upload audio or use the selected take.';
    $('use-selected').disabled = !selected;
    const locked = profile().family === 'ace_step' && ['cover', 'repaint'].includes(mode);
    $('duration').disabled = locked;
    if (locked && source) $('duration').value = Math.ceil(source.seconds);
    $('mode-help').textContent = {
      song:'Build a new song from a style description and optional lyrics.',
      continue:'Start with audio and describe where the track goes next.',
      cover:'Keep a source as the starting point and explore a different style.',
      repaint:'Replace a specific time range. The source sets the total length.'
    }[mode];
  }
  $('model').replaceChildren(...data.models.map(model => new Option(model.id, model.id)));
  $('prompt').value = presets['Night drive'];
  $('lyrics').value = initialLyrics;
  $('n').value = '2';
  chooseModel(false);

  function setDesign(design) {
    if (!notes[design]) design = 'studio';
    byId('music-workspace').dataset.layout = design;
    byId('design-note').textContent = notes[design];
    for (const button of document.querySelectorAll('[data-design]')) {
      const active = button.dataset.design === design;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    }
    history.replaceState(null, '', '#' + design);
    requestAnimationFrame(drawWave);
  }
  document.querySelectorAll('[data-design]').forEach(button => button.addEventListener('click', () => setDesign(button.dataset.design)));
  byId('theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark';
    const choice = document.querySelector('[data-theme-form] [name="mode"]');
    choice.value = dark ? 'dark' : 'light';
    choice.dispatchEvent(new Event('change', {bubbles:true}));
    byId('theme').textContent = dark ? 'Light mode' : 'Dark mode';
  });
  document.querySelectorAll('[data-preset]').forEach(button => button.addEventListener('click', () => {
    $('prompt').value = presets[button.dataset.preset];
    if (!$('instrumental').disabled) $('instrumental').checked = button.dataset.preset === 'Soft focus';
    chooseMode();
    status('Style starter applied. Refine it to make it yours.');
  }));
  $('model').addEventListener('change', () => chooseModel());
  $('mode').addEventListener('change', chooseMode);
  $('instrumental').addEventListener('change', chooseMode);
  document.querySelectorAll('[data-tag]').forEach(button => button.addEventListener('click', () => {
    const area = $('lyrics'), at = area.selectionStart;
    area.setRangeText((at && area.value[at - 1] !== '\n' ? '\n' : '') + button.dataset.tag + '\n', at, area.selectionEnd, 'end');
    area.focus();
  }));
  $('write').addEventListener('click', () => {
    $('prompt').value = presets['Night drive'];
    $('lyrics').value = initialLyrics;
    if (!$('instrumental').disabled) $('instrumental').checked = false;
    chooseMode();
    status('Prepared sample draft loaded. No writing model was called.');
  });

  const pcm = data.audio.map(url => {
    const bytes = Uint8Array.from(atob(url.split(',')[1]), value => value.charCodeAt(0));
    return new Int16Array(bytes.buffer, 44);
  });
  function paint(canvas, demo, mini = false) {
    const width = Math.round(canvas.clientWidth * devicePixelRatio), height = (mini ? 36 : 150) * devicePixelRatio;
    if (!width) return;
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext('2d');
    const css = getComputedStyle(root);
    const samples = pcm[demo || 0];
    if (!mini && region) {
      context.fillStyle = css.getPropertyValue('--theme-border');
      context.fillRect(region[0] / 16 * width, 0, (region[1] - region[0]) / 16 * width, height);
    }
    context.fillStyle = css.getPropertyValue('--theme-accent');
    const step = mini ? 4 * devicePixelRatio : 3 * devicePixelRatio;
    for (let x = 0; x < width; x += step) {
      const start = Math.floor(x / width * samples.length), end = Math.floor((x + step) / width * samples.length);
      let peak = 0;
      for (let at = start; at < end; at += 16) peak = Math.max(peak, Math.abs(samples[at] || 0) / 32768);
      const bar = Math.max(2, peak * height * 1.6);
      context.globalAlpha = mini ? .55 : .75;
      context.fillRect(x, (height - bar) / 2, Math.max(1, step - devicePixelRatio), bar);
    }
    context.globalAlpha = 1;
    if (!mini) {
      context.fillStyle = css.getPropertyValue('--theme-text');
      context.fillRect($('audio').currentTime / 16 * width, 0, devicePixelRatio * 2, height);
    }
  }
  function drawWave() {
    if (selected) paint($('wave'), selected.demo);
    for (const canvas of $('list').querySelectorAll('canvas')) paint(canvas, Number(canvas.dataset.demo), true);
  }
  function renderTakes() {
    const query = $('filter').value.trim().toLowerCase();
    const rows = takes.filter(take => (!$('favourites').checked || take.favourite) && [take.name, take.model, ...take.tags].join(' ').toLowerCase().includes(query));
    rows.sort({name:(a,b) => a.name.localeCompare(b.name), seed:(a,b) => a.seed-b.seed, newest:(a,b) => b.created-a.created}[byId('take-sort').value]);
    $('list').replaceChildren(...rows.map(take => {
      const fragment = clone('music-take-template');
      const button = fragment.querySelector('button');
      button.dataset.takeId = take.id;
      button.setAttribute('aria-pressed', String(selected?.id === take.id));
      button.querySelector('[data-take-title]').textContent = take.name;
      button.querySelector('[data-take-description]').textContent = take.model + ' · seed ' + take.seed;
      button.querySelector('[data-take-tags]').textContent = take.tags.join(' / ');
      button.querySelector('[data-take-length]').textContent = seconds(take.seconds);
      button.querySelector('[data-take-star]').textContent = take.favourite ? '★' : '';
      button.querySelector('canvas').dataset.demo = take.demo;
      button.addEventListener('click', () => select(take));
      return fragment;
    }));
    byId('take-count').textContent = rows.length + ' of ' + takes.length + ' takes';
    const empty = library.querySelector('.music-empty-library');
    empty.hidden = !!rows.length;
    empty.querySelector('.section-title').textContent = takes.length ? 'No matching takes' : 'Room for your next idea';
    empty.querySelector('p').textContent = takes.length ? 'Clear the search or favourite filter to see more takes.' : 'Describe a sound, write a few lines, and simulate your first takes.';
    requestAnimationFrame(drawWave);
  }
  function select(take) {
    $('audio').pause();
    selected = take;
    region = null;
    $('selected').hidden = !take;
    $('empty').hidden = !!take;
    $('use-selected').disabled = !take;
    if (!take) {
      $('audio').removeAttribute('src');
      $('audio').load();
      renderTakes();
      return;
    }
    $('title').textContent = take.name;
    $('info').textContent = '16-second synth demo · 16 kHz · mono · seed ' + take.seed + ' · ' + take.model;
    $('audio').src = take.audio;
    $('favourite').setAttribute('aria-pressed', String(take.favourite));
    $('favourite').textContent = (take.favourite ? '★' : '☆') + ' Favourite';
    $('tags').value = take.tags.join(', ');
    $('nc').hidden = take.model !== 'YuE 2';
    $('details').textContent = JSON.stringify({preview: true, requested: take.request, output: 'Original 16-second synth fixture; no model executed.'}, (key, value) => key === 'source' ? value?.name || null : value, 2);
    updateRegion();
    renderTakes();
  }
  function updateRegion() {
    $('region-start').value = region ? region[0].toFixed(2) : '0';
    $('region-end').value = region ? region[1].toFixed(2) : '16';
    $('region').textContent = region ? 'Selected ' + region.map(value => value.toFixed(2)).join('–') + ' s' : 'Click to seek · drag to select';
    $('audio').loop = $('loop-region').checked && !region;
    $('edit-range').disabled = !region || !selected || !(profile().modes || []).includes('repaint');
    drawWave();
  }
  function keepPlaying() {
    if (region && $('loop-region').checked && ($('audio').currentTime >= region[1] || $('audio').currentTime < region[0])) $('audio').currentTime = region[0];
    drawWave();
  }
  function animate() { keepPlaying(); frame = !$('audio').paused ? requestAnimationFrame(animate) : null; }
  $('audio').addEventListener('play', () => { if (frame === null) animate(); });
  $('audio').addEventListener('pause', () => { cancelAnimationFrame(frame); frame = null; });
  $('audio').addEventListener('timeupdate', keepPlaying);
  $('audio').addEventListener('ended', () => {
    if (region && $('loop-region').checked) { $('audio').currentTime = region[0]; $('audio').play().catch(error => status(error.message, true)); }
  });
  const waveAt = event => Math.max(0, Math.min(16, (event.clientX - $('wave').getBoundingClientRect().left) / $('wave').clientWidth * 16));
  $('wave').addEventListener('pointerdown', event => {
    if (!selected || event.button !== 0) return;
    drag = {x: event.clientX, start: waveAt(event)};
    $('wave').setPointerCapture(event.pointerId);
  });
  $('wave').addEventListener('pointermove', event => {
    if (!drag) return;
    region = [drag.start, waveAt(event)].sort((a,b) => a-b);
    drawWave();
  });
  $('wave').addEventListener('pointerup', event => {
    if (!drag) return;
    if (Math.abs(drag.x - event.clientX) < 3) { region = null; $('audio').currentTime = waveAt(event); }
    else region = [drag.start, waveAt(event)].sort((a,b) => a-b);
    drag = null;
    updateRegion();
  });
  $('wave').addEventListener('pointercancel', () => { drag = null; updateRegion(); });
  for (const id of ['region-start', 'region-end']) $(id).addEventListener('change', () => {
    const start = Number($('region-start').value), end = Number($('region-end').value);
    if (start < 0 || end > 16 || end <= start || !Number.isFinite(start + end)) { status('Choose a range within this 16-second demo, with the end after the start.', true); updateRegion(); return; }
    region = [start, end]; updateRegion();
  });
  $('loop-region').addEventListener('change', updateRegion);
  $('clear-region').addEventListener('click', () => { region = null; updateRegion(); });
  $('edit-range').addEventListener('click', () => {
    if (!region || !selected) return;
    source = {id:selected.id, name:selected.name, seconds:selected.seconds};
    $('mode').value = 'repaint';
    $('start').value = region[0].toFixed(2);
    $('end').value = region[1].toFixed(2);
    chooseMode();
    $('prompt').focus();
    status('Selected range is ready to repaint. Describe the change, then simulate generation.');
  });
  $('use-selected').addEventListener('click', () => {
    if (!selected) return;
    source = {id:selected.id, name:selected.name, seconds:selected.seconds};
    chooseMode();
  });
  $('source').addEventListener('change', safe(async () => {
    const file = $('source').files[0];
    if (!file) return;
    source = null;
    chooseMode();
    if (file.size > 32 * 1024 * 1024) throw new Error('Source audio is limited to 32 MiB.');
    const context = new AudioContext();
    try {
      const audio = await context.decodeAudioData(await file.arrayBuffer());
      source = {id:'upload-' + ++serial, name:file.name, seconds:audio.duration};
      chooseMode();
      status('Source checked locally. No file was uploaded to a server.');
    } finally { await context.close(); }
  }));
  const reuse = variation => {
    if (!selected) return;
    drafts.set(currentModel, capture());
    $('model').value = selected.model;
    chooseModel(false);
    restore(selected.request);
    if (variation) { $('seed').value = '-1'; $('n').value = '1'; }
    $('prompt').focus();
    status(variation ? 'Settings restored with a random seed for the next variation.' : 'Settings restored. Your take remains unchanged.');
  };
  $('reuse').addEventListener('click', () => reuse(false));
  $('variation').addEventListener('click', () => reuse(true));
  $('favourite').addEventListener('click', () => {
    if (!selected) return;
    selected.favourite = !selected.favourite;
    $('favourite').setAttribute('aria-pressed', String(selected.favourite));
    $('favourite').textContent = (selected.favourite ? '★' : '☆') + ' Favourite';
    renderTakes();
  });
  $('save-tags').addEventListener('click', () => {
    if (!selected) return;
    selected.tags = $('tags').value.split(',').map(value => value.trim()).filter(Boolean);
    renderTakes(); status('Tags saved for this tab.');
  });
  $('filter').addEventListener('input', renderTakes);
  $('favourites').addEventListener('change', renderTakes);
  byId('take-sort').addEventListener('change', renderTakes);
  byId('clear-take-filters').addEventListener('click', () => { $('filter').value = ''; $('favourites').checked = false; renderTakes(); });

  function dialog(title_, nodes) {
    $('dialog-title').textContent = title_;
    $('dialog-content').replaceChildren(...nodes);
    $('dialog').showModal();
  }
  $('download').addEventListener('click', () => {
    if (!selected) return;
    const link = make('a');
    link.href = selected.audio;
    link.download = selected.name.replace(/[^a-z0-9-]/gi, '-').toLowerCase() + '-synth-demo.wav';
    document.body.append(link); link.click(); link.remove();
    status('Downloaded original synth demo audio. No model-generated audio is included.');
  });
  $('share').addEventListener('click', () => {
    if (!selected) return;
    const label = make('label', ' Studio group · sample', 'form-check');
    const box = make('input', undefined, 'form-check-input'); box.type = 'checkbox'; box.checked = !!selected.shared;
    label.prepend(box);
    const save = make('button', 'Save preview choice', 'btn btn-primary mt-3'); save.type = 'button';
    save.addEventListener('click', () => { selected.shared = box.checked; $('dialog').close(); status('Sharing choice saved in this tab only. Nothing was shared.'); });
    dialog('Sharing preview', [make('p', 'Explore the sharing control. This prototype does not publish audio or contact anyone.', 'small text-secondary'), label, save]);
  });
  $('delete').addEventListener('click', () => {
    if (!selected) return;
    const id = selected.id, remove = make('button', 'Remove demo take', 'btn btn-danger'); remove.type = 'button';
    remove.addEventListener('click', () => { takes = takes.filter(take => take.id !== id); select(takes[0] || null); $('dialog').close(); status('Demo take removed from this tab.'); });
    dialog('Remove this demo take?', [make('p', 'This affects the browser preview only. Reload to restore the sample library.'), remove]);
  });

  function renderJobs() {
    $('jobs').replaceChildren();
    if (!jobs.length) $('jobs').append(make('p', 'Nothing in the queue. Your next idea starts on the left.', 'small text-secondary mb-0'));
    for (const job of jobs.slice(0, 6)) {
      const fragment = clone('music-job-template');
      fragment.querySelector('[data-job-name]').textContent = job.model + ' · ' + job.request.n + ' takes';
      const failed = job.state === 'failed', active = job.state === 'running';
      fragment.querySelector('[data-job-status]').textContent = {running:'Simulating generation · ' + job.progress + '%', failed:'Failed · sample out-of-memory error. Adjust settings or retry.', cancelled:'Cancelled · no demo outputs added.', completed:'Complete · demo takes added. No model was called.'}[job.state];
      fragment.querySelector('progress').value = job.progress;
      const action = fragment.querySelector('[data-job-action]');
      action.hidden = !active && !failed && job.state !== 'cancelled';
      action.textContent = active ? 'Cancel' : 'Retry';
      action.addEventListener('click', () => {
        if (active) { clearInterval(job.timer); job.state = 'cancelled'; status('Simulation cancelled. Your draft is intact.'); renderJobs(); }
        else startJob(job.request, job.model);
      });
      $('jobs').append(fragment);
    }
  }
  function startJob(request, model) {
    if (scenario === 'no-model') { status('Choose an available music model first.', true); return; }
    const job = {id:++serial, model, request:structuredClone(request), state:'running', progress:0};
    jobs.unshift(job); renderJobs();
    status('Generation simulation queued. It will add playable demo takes in a few seconds.');
    job.timer = setInterval(() => {
      job.progress += 25;
      if (job.progress >= 100) {
        clearInterval(job.timer); job.state = 'completed';
        const outputs = [];
        for (let n = 0; n < Number(request.n); n++) {
          const id = ++serial, demo = n % 3;
          const seed = Number(request.seed) < 0 ? crypto.getRandomValues(new Uint32Array(1))[0] : Number(request.seed) + n;
          outputs.push({id:'take-' + id, name:(byId('session-name').value.trim() || title) + ' · take ' + (n + 1), model, seed, favourite:false,
            tags:['demo', request.mode], seconds:16, audio:data.audio[demo], demo, request:{...job.request, seed}, temporary:request.temporary, created:id});
        }
        takes.unshift(...outputs); select(outputs[0]);
        status(outputs.length + ' demo takes ready. Each plays an original 16-second synth fixture; no model was called.');
      }
      renderJobs();
    }, 700);
  }
  $('form').addEventListener('submit', event => {
    event.preventDefault();
    if (scenario === 'no-model') return;
    const request = capture();
    if (profile().lyrics_required && !request.lyrics.trim()) { status('This model requires lyrics. Add tagged lyrics before generating.', true); return; }
    if (request.mode !== 'song' && !source) { status('Choose source audio or use the selected take for this mode.', true); return; }
    if (request.mode === 'repaint' && (!(Number(request.end) > Number(request.start)) || Number(request.start) < 0 || Number(request.end) > source.seconds)) {
      status('Choose a repaint range within the source audio.', true); return;
    }
    startJob(request, profile().id);
  });
  byId('scenario').addEventListener('change', () => {
    scenario = byId('scenario').value;
    for (const job of jobs) clearInterval(job.timer);
    jobs = [];
    takes = scenario === 'empty' ? [] : sampleTakes();
    $('model').disabled = scenario === 'no-model';
    $('generate').disabled = scenario === 'no-model';
    $('write').disabled = scenario === 'no-model';
    $('availability').textContent = scenario === 'no-model' ? 'No music models available. Add a model under Models → Audio models.' : '';
    $('filter').value = ''; $('favourites').checked = false;
    if (scenario === 'error') jobs.push({id:++serial, model:profile().id, request:capture(), state:'failed', progress:0});
    select(takes[1] || null); renderJobs(); status('');
  });
  document.addEventListener('weightroom:theme', drawWave);
  addEventListener('resize', drawWave);
  new ResizeObserver(drawWave).observe(player);
  takes = sampleTakes();
  setDesign(location.hash.slice(1));
  select(takes[1]);
  renderJobs();
})();
