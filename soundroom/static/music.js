/* SoundRoom music and sounds. API keys stay in the authenticated server session. */
(() => {
  'use strict';
  const root = document.querySelector('#soundroom');
  if (!root) return;
  const page = root.dataset.mode;  // music, sound or speech
  const SPOKEN = ['speech', 'clone', 'design'];
  const defaults = JSON.parse(root.dataset.defaults || '{}');  // the administrator's first choices
  const $ = id => document.getElementById('sr-' + id);
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const message = error => { $('message').hidden = !error; $('message').textContent = error || ''; };
  async function api(path, body, method) {
    const response = await fetch('/soundroom-api/' + path, {
      method: method || (body === undefined ? 'GET' : 'POST'),
      headers: body === undefined ? {} : {'content-type': 'application/json'},
      body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store'
    });
    if (response.redirected) { location.href = '/sign-in'; throw new Error('Please sign in again.'); }
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || 'The audio operation failed.');
    return value;
  }
  const safe = action => async event => {
    try { await action(event); message(''); } catch (error) { message(error.message); }
  };
  const audioURL = (id, format) => '/soundroom-api/assets/' + encodeURIComponent(id) + '/content' + (format ? '?format=' + format + '&download=1' : '');
  const seconds = value => { const s = Math.round(value || 0); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
  const kinds = {music: 'take', sound: 'sound', speech: 'speech'}[page];
  const ours = row => page === 'speech' ? SPOKEN.includes(row.capabilities?.audio) : row.capabilities?.audio === page;
  const MAPPED = new Set(['route', 'lyrics', 'duration_sec', 'seed', 'text', 'tags', 'style', 'prompt', 'repainting_start', 'repainting_end',
    'audio_input_kind', 'inpaint_mask_start_sec', 'inpaint_mask_end_sec', 'semantic_min_tokens', 'semantic_max_tokens', 'batch_size']);
  const MUSICAL = {bpm: 'Tempo (BPM)', keyscale: 'Key', timesignature: 'Time signature', language: 'Vocal language'};
  let state = {models: [], assets: [], voices: [], jobs: [], groups: []};
  let selected = null, source = null, listSignature = '', jobsSignature = '', polling = false;
  let region = null, peaks = null, licenceNoted = false;
  let older = [], nextPage = null, libraryQuery = '', loadingOlder = false;
  let speakerSignature = '';
  let speechModel = null, voiceSignature = '';
  const speechDrafts = new Map();

  function rememberSpeechModel() {
    if (!speechModel) return;
    speechDrafts.set(speechModel, {voice: $('voice').value, preset: $('preset').value,
      design: $('design').value, options: advanced(),
      speakers: Object.fromEntries([...$('speakers').querySelectorAll('[data-speaker]')].map(node => [node.dataset.speaker, node.value]))});
  }

  const profile = () => state.models.find(row => row.id === $('model').value)?.audio_profile || {};
  function updateAvailability() {
    const available = state.models.some(ours);
    const needsVoice = page === 'speech' && profile().kind === 'clone' && !state.voices.length;
    $('generate').disabled = !available || needsVoice;
    $('availability').textContent = !available ? 'No ' + page + ' model is available to you yet. An administrator adds one under Models → Audio models.' : needsVoice ? 'Save a voice on the Voices tab, or choose a built-in or voice-design model.' : '';
  }
  function chooseModel() {
    const one = profile();
    let draft;
    if (page === 'speech') {
      rememberSpeechModel();
      speechModel = $('model').value;
      draft = speechDrafts.get(speechModel);
      $('voice').value = draft?.voice || state.preferences?.voice || '';
      $('preset').value = draft?.preset || '';
      $('design').value = draft?.design || '';
      speakerSignature = '';
    }
    const commercial = {yes: 'commercial use allowed', no: 'non-commercial only', conditional: 'commercial use has conditions'}[one.commercial] || '';
    $('licence').textContent = one.licence ? one.licence + ' · ' + commercial : '';
    if ($('model-help')) $('model-help').textContent = (one.help || '') + (one.turbo ? ' Turbo uses at most 8 denoising steps; diffusion guidance has no effect.' : '');
    if ($('mode')) {
      const modes = page === 'music' ? (one.modes || ['song']) : ['song'];
      $('mode').replaceChildren(...modes.map(mode => new Option({song: page === 'music' ? 'Song' : 'Sound', continue: 'Continue a track', cover: 'Cover / restyle', repaint: 'Repaint a range'}[mode] || mode, mode)));
      $('mode-wrap').hidden = modes.length < 2;
      $('duration').max = one.limits?.max_seconds || 600;
      $('n').max = one.limits?.max_takes || 8;
    }
    if ($('lyrics-wrap')) $('lyrics-wrap').hidden = !one.lyrics;
    if ($('instrumental')) {
      $('instrumental').disabled = !!one.lyrics_required;
      if (one.lyrics_required) $('instrumental').checked = false;
      $('lyrics').required = !!one.lyrics_required;
      $('lyrics').disabled = $('instrumental').checked;
    }
    if (page === 'speech') chooseVoice(draft?.speakers || {});
    $('advanced').replaceChildren();
    $('musical')?.replaceChildren();
    for (const option of (one.options || []).filter(row => !MAPPED.has(row.name))) {
      const label = element('label', MUSICAL[option.name] || option.name.replaceAll('_', ' '), 'form-label small mb-0');
      const configured = one.defaults?.[option.name] ?? one.defaults?.[one.family + '.' + option.name] ?? option.default;
      let input;
      if (option.type === 'enum') {
        input = element('select', undefined, 'form-select form-select-sm mb-2');
        input.add(new Option('Default' + (configured !== undefined ? ' (' + configured + ')' : ''), ''));
        (option.values || []).forEach(value => input.add(new Option(value, value)));
      } else if (option.type === 'bool') {
        input = element('select', undefined, 'form-select form-select-sm mb-2');
        [['', 'Default' + (configured !== undefined ? ' (' + configured + ')' : '')], ['true', 'On'], ['false', 'Off']].forEach(([value, text]) => input.add(new Option(text, value)));
      } else {
        input = element('input', undefined, 'form-control form-control-sm mb-2');
        input.type = option.type === 'string' ? 'text' : 'number';
        if (option.type === 'float') input.step = 'any';
        input.placeholder = configured !== undefined ? String(configured) : 'Default';
        if (option.min !== undefined) input.min = option.min;
        if (option.max !== undefined) input.max = option.max;
      }
      input.id = 'sr-option-' + option.name;
      label.htmlFor = input.id;
      input.dataset.option = option.name;
      input.dataset.type = option.type;
      input.title = option.description || '';
      label.title = option.description || '';
      if (one.turbo && option.name === 'num_inference_steps') input.max = Math.min(Number(input.max) || 8, 8);
      if (one.turbo && option.name === 'guidance_scale') { input.disabled = true; input.title = 'Diffusion guidance has no effect on this Turbo profile.'; }
      const target = page === 'music' && MUSICAL[option.name] ? $('musical') : $('advanced');
      target.append(label, input);
    }
    if ($('musical-wrap')) $('musical-wrap').hidden = !$('musical').children.length;
    if (!$('advanced').children.length) $('advanced').append(element('p', 'This model has no further settings.', 'text-secondary small mb-0'));
    if (draft) root.querySelectorAll('[data-option]').forEach(node => { node.value = String(draft.options[node.dataset.option] ?? ''); });
    chooseMode();
    updateAvailability();
  }
  function chooseMode() {
    if (!$('mode')) return;
    const mode = $('mode').value || 'song';
    $('source-wrap').hidden = mode === 'song';
    $('window').hidden = mode !== 'repaint';
    const locked = profile().family === 'ace_step' && ['cover', 'repaint'].includes(mode);
    $('duration').disabled = locked;
    if (locked && source?.seconds) $('duration').value = Math.ceil(source.seconds);
    if ($('mode-help')) $('mode-help').textContent = {
      song: page === 'sound' ? 'Describe the sound, its setting and distance. Generate variations; use Seamless loop for repeating ambience.' : 'Describe genre, mood, instruments and vocals. Generate a few takes, then reuse your favourite seed to refine it.',
      continue: 'Use a recording as the starting point and describe how the music should develop.',
      cover: 'Choose source audio and describe the new style.' + (locked ? ' The source sets the track length.' : ''),
      repaint: 'Choose source audio and the time range to replace.' + (locked ? ' The rest of the track keeps its length.' : '')
    }[mode] || '';
    if ($('use-selected')) $('use-selected').disabled = !selected?.mine;
  }
  function speakers() {
    const names = [];
    for (const line of $('prompt').value.split('\n')) {
      const match = line.match(/^\s*([^:\n]{1,40}):\s*\S/);
      if (match && !names.includes(match[1].trim())) names.push(match[1].trim());
    }
    return names;
  }
  function voiceSelect(id, chosen) {
    const select = element('select', undefined, 'form-select form-select-sm');
    select.id = id;
    select.disabled = !state.voices.length;
    state.voices.forEach(voice => select.add(new Option(voice.name + (voice.mine ? '' : ' · shared'), voice.id)));
    if (chosen && [...select.options].some(row => row.value === chosen)) select.value = chosen;
    return select;
  }
  const speakerKey = (kind, names) => JSON.stringify([kind, names, state.voices.map(voice => [voice.id, voice.name]), !names.length && [$('voice').value, $('preset').value, $('design').value]]);
  function renderSpeechVoices() {
    const template = document.getElementById('speech-voice-template');
    if (!template) return;
    const query = document.getElementById('voice-search').value.trim().toLowerCase();
    const signature = JSON.stringify([state.voices, $('voice').value, query]);
    if (signature === voiceSignature) return;
    voiceSignature = signature;
    const voices = state.voices.filter(voice => (voice.name + ' ' + (voice.description || '')).toLowerCase().includes(query));
    document.getElementById('voice-count').textContent = state.voices.length + ' voices';
    document.getElementById('voice-empty').hidden = !!voices.length || !state.voices.length;
    document.getElementById('voice-choices').replaceChildren(...voices.map(voice => {
      const button = template.content.firstElementChild.cloneNode(true);
      button.dataset.voice = voice.id;
      button.querySelector('[data-name]').textContent = voice.name;
      button.querySelector('[data-initials]').textContent = voice.name.slice(0, 2).toUpperCase();
      button.querySelector('[data-description]').textContent = voice.description || '';
      button.querySelector('[data-source]').textContent = voice.mine ? 'Your voice' : 'Shared with you';
      button.querySelector('[data-note]').hidden = true;
      button.setAttribute('aria-pressed', String(voice.id === $('voice').value));
      button.addEventListener('click', () => { $('voice').value = voice.id; $('voice').dispatchEvent(new Event('change', {bubbles:true})); });
      return button;
    }));
  }
  function chooseVoice(saved) {
    const kind = profile().kind;
    $('voice-wrap').hidden = kind !== 'clone';
    $('preset-wrap').hidden = kind !== 'speech';
    $('design-wrap').hidden = kind !== 'design';
    const old = $('voice').value || state.preferences?.voice || state.voices[0]?.id;
    if (JSON.stringify([...$('voice').options].map(row => [row.value, row.text])) !== JSON.stringify(state.voices.map(voice => [voice.id, voice.name + (voice.mine ? '' : ' · shared')]))) {
      $('voice').replaceChildren(...voiceSelect('sr-voice-list').options);
    }
    if ([...$('voice').options].some(row => row.value === old)) $('voice').value = old;
    $('voice-help').textContent = state.voices.length ? '' : 'Save a voice on the Voices tab first.';
    renderSpeechVoices();
    const names = speakers();
    const signature = speakerKey(kind, names);
    if (signature === speakerSignature) return;
    speakerSignature = signature;
    const kept = saved || Object.fromEntries([...$('speakers').querySelectorAll('[data-speaker]')].map(node => [node.dataset.speaker, node.value]));
    const template = document.getElementById('speech-speaker-template');
    $('speakers').replaceChildren(...(names.length ? names : ['Narrator']).map(name => {
      const label = template.content.firstElementChild.cloneNode(true);
      const input = kind === 'clone' ? voiceSelect('', names.length ? kept[name] || $('voice').value : $('voice').value) : element(kind === 'design' ? 'textarea' : 'input', undefined, 'form-control form-control-sm');
      if (kind !== 'clone') {
        input.value = (names.length ? kept[name] : null) ?? (kind === 'design' ? $('design').value : $('preset').value);
        input.maxLength = kind === 'design' ? 1000 : 80;
        input.placeholder = kind === 'design' ? 'Describe this speaker’s voice' : 'Built-in voice name (optional)';
      }
      if (names.length) input.dataset.speaker = name;
      else input.dataset.narrator = '';
      input.setAttribute('aria-label', name + (kind === 'design' ? ' voice description' : ' voice'));
      label.querySelector('[data-speaker-name]').textContent = name;
      label.querySelector('.sp-speaker-initial').textContent = name.slice(0, 2).toUpperCase();
      label.querySelector('[data-speaker-choice]').replaceWith(input);
      if (!names.length) input.addEventListener('input', () => {
        const field = kind === 'clone' ? $('voice') : kind === 'design' ? $('design') : $('preset');
        field.value = input.value;
        speakerSignature = speakerKey(kind, names);
        renderSpeechVoices();
      });
      return label;
    }));
  }
  function script() {
    const kind = profile().kind, names = speakers(), lines = [];
    const choice = name => $('speakers').querySelector('[data-speaker="' + CSS.escape(name) + '"]')?.value;
    const add = (text, name) => {
      text = text.trim();
      if (!text) return;
      const line = {text};
      if (kind === 'clone') line.voice_id = choice(name) || $('voice').value;
      if (kind === 'design') line.instructions = (choice(name) ?? $('design').value).trim();
      if (kind === 'speech') {
        const voice = (choice(name) ?? $('preset').value).trim();
        if (voice) line.voice = voice;
      }
      lines.push(line);
    };
    if (!names.length) {
      for (const part of $('prompt').value.split(/\n\s*\n/)) add(part);
      return lines;
    }
    let current = names[0];
    for (const raw of $('prompt').value.split('\n')) {
      const match = raw.match(/^\s*([^:\n]{1,40}):\s*(.*)$/);
      if (match && names.includes(match[1].trim())) { current = match[1].trim(); add(match[2], current); }
      else add(raw, current);
    }
    return lines;
  }
  function settings() {
    if (page === 'speech') {
      const values = {model: $('model').value, script: script(), seed: Number($('seed').value), gap: Number($('gap').value)};
      const options = advanced();
      if (Object.keys(options).length) values.options = options;
      return values;
    }
    const values = {model: $('model').value, prompt: $('prompt').value.trim(), mode: $('mode').value || 'song',
      duration: Number($('duration').value), n: Number($('n').value), seed: Number($('seed').value)};
    if ($('lyrics-wrap') && !$('lyrics-wrap').hidden) {
      if ($('instrumental').checked) values.instrumental = true;
      else if ($('lyrics').value.trim()) values.lyrics = $('lyrics').value;
    }
    if (values.mode === 'repaint') { values.repaint_start = Number($('start').value); values.repaint_end = Number($('end').value); }
    if ($('loop')?.checked) values.loop = true;
    const options = advanced();
    if (Object.keys(options).length) values.options = options;
    if (values.mode === 'song') delete values.mode;
    return values;
  }
  function advanced() {
    const options = {};
    root.querySelectorAll('[data-option]').forEach(node => {
      if (node.disabled || node.value === '') return;
      options[node.dataset.option] = node.dataset.type === 'bool' ? node.value === 'true' : ['int', 'float'].includes(node.dataset.type) ? Number(node.value) : node.value;
    });
    return options;
  }
  function apply(values, sourceId = null) {
    if (values.model && [...$('model').options].some(row => row.value === values.model)) { $('model').value = values.model; chooseModel(); }
    else if (values.model) throw new Error('The original model is no longer available to you. Choose a model before reusing these settings.');
    const lines = values.script || values.lines;
    const voices = [...new Set((lines || []).map(line => line.voice_id || line.voice || line.instructions || ''))];
    $('prompt').value = lines ? lines.map(line => (voices.length > 1 ? 'Speaker ' + (voices.indexOf(line.voice_id || line.voice || line.instructions || '') + 1) + ': ' : '') + line.text).join('\n\n') : values.prompt || '';
    if ($('lyrics')) {
      $('lyrics').value = values.lyrics || '';
      $('instrumental').checked = !!values.instrumental && !profile().lyrics_required;
      $('lyrics').disabled = $('instrumental').checked;
    }
    for (const name of ['duration', 'n', 'seed', 'gap']) if (values[name] !== undefined && $(name)) $(name).value = values[name];
    if ($('mode')) {
      const mode = values.mode || 'song';
      if (![...$('mode').options].some(row => row.value === mode)) throw new Error('This model no longer supports the saved mode.');
      $('mode').value = mode;
      source = sourceId ? state.assets.find(row => row.id === sourceId) || {id: sourceId, filename: 'Saved source audio'} : null;
      $('source-name').textContent = source ? source.filename : '';
      $('source').value = '';
      $('start').value = values.repaint_start ?? '';
      $('end').value = values.repaint_end ?? '';
      if ($('loop')) $('loop').checked = !!values.loop;
      chooseMode();
    }
    root.querySelectorAll('[data-option]').forEach(node => { node.value = String(values.options?.[node.dataset.option] ?? values.options?.[profile().family + '.' + node.dataset.option] ?? ''); });
    if (page === 'speech' && lines?.length) {
      if (lines[0].voice_id) $('voice').value = lines[0].voice_id;
      $('preset').value = lines[0].voice || '';
      $('design').value = lines[0].instructions || '';
      chooseVoice();
      $('speakers').querySelectorAll('[data-speaker]').forEach(node => {
        const line = lines.find(line => 'Speaker ' + (voices.indexOf(line.voice_id || line.voice || line.instructions || '') + 1) === node.dataset.speaker);
        if (line) node.value = line.voice_id || line.voice || line.instructions || '';
      });
      if (profile().kind === 'clone' && lines.some(line => !state.voices.some(voice => voice.id === line.voice_id))) throw new Error('A saved voice is unavailable; choose a voice for each speaker before generating.');
    }
    if (page === 'speech') root.dispatchEvent(new Event('soundroom:studio'));
  }

  const query = () => 'state?kinds=' + kinds + ($('favourites').checked ? '&favourites=1' : '') + ($('filter').value.trim() ? '&tag=' + encodeURIComponent($('filter').value.trim()) : '');

  async function refresh() {
    const wantedQuery = query();
    if (wantedQuery !== libraryQuery) { older = []; nextPage = null; libraryQuery = wantedQuery; }
    const latest = await api(wantedQuery);
    if (wantedQuery !== query()) return;
    if (!older.length) nextPage = latest.next;
    const newest = new Set(latest.assets.map(row => row.id));
    state = {...latest, assets: [...latest.assets, ...older.filter(row => !newest.has(row.id))]};
    $('older').hidden = !nextPage;
    const wanted = state.models.filter(ours);
    const current = $('model').value;
    if ($('model').options.length !== wanted.length || [...$('model').options].some((row, index) => row.value !== wanted[index]?.id)) {
      $('model').replaceChildren(...wanted.map(row => new Option(row.id, row.id)));
      const first = [current, page === 'speech' ? state.preferences?.speech_model : null, defaults[page]].find(name => name && wanted.some(row => row.id === name));
      if (first) $('model').value = first;
      if (!current && state.preferences?.format) $('format').value = state.preferences.format;
      chooseModel();
    }
    updateAvailability();
    if (page === 'speech') chooseVoice();
    $('temporary-wrap').hidden = !state.can_unlogged;
    renderList();
    renderJobs();
    if (selected && !state.assets.some(row => row.id === selected.id)) select(null);
    if (!polling && state.jobs.some(row => ['in_progress', 'validating', 'cancelling'].includes(row.status))) poll();
    if (page === 'speech') root.dispatchEvent(new Event('soundroom:studio'));
  }
  function renderList() {
    const signature = JSON.stringify(state.assets.map(row => [row.id, row.favourite, row.tags, row.filename]));
    if (signature === listSignature) return;
    listSignature = signature;
    $('list').replaceChildren(...state.assets.map(row => {
      const item = element('button', undefined, 'list-group-item list-group-item-action sr-item');
      item.type = 'button';
      item.setAttribute('aria-selected', String(selected?.id === row.id));
      const words = row.metadata?.settings?.prompt || row.metadata?.settings?.lines?.map(line => line.text).join(' ') || row.filename;
      item.append(element('span', (row.favourite ? '★ ' : '') + words, 'sr-item-title'),
        element('span', [seconds(row.seconds), row.metadata?.model || '', row.metadata?.seed !== undefined ? 'seed ' + row.metadata.seed : '',
          row.metadata?.settings?.mode && row.metadata.settings.mode !== 'song' ? row.metadata.settings.mode : '',
          new Date(row.created * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'}),
          row.mine ? '' : 'shared', ...(row.tags || [])].filter(Boolean).join(' · '), 'text-secondary small'));
      item.addEventListener('click', () => select(row));
      return item;
    }));
    $('empty').hidden = !!selected;
    $('empty-title').textContent = state.assets.length ? 'Select audio below' : 'Your ' + ({music: 'takes', sound: 'sounds', speech: 'recordings'}[page]) + ' appear here';
    $('empty-help').textContent = state.assets.length ? 'Choose a recording to listen, reuse its settings or make a new variation.' : 'Choose a model and describe what you want to hear.';
  }
  function renderJobs() {
    const signature = JSON.stringify(state.jobs.map(row => [row.id, row.status, row.request_counts, row.progress, row.error]));
    if (signature === jobsSignature) return;
    jobsSignature = signature;
    const jobs = state.jobs.filter(row => {
      const model = state.models.find(model => model.id === row.model);
      return !model || ours(model);
    });
    $('jobs').replaceChildren(...jobs.slice(0, 8).map(row => {
      const box = element('div', undefined, 'sr-job studio-job frame');
      const counts = row.request_counts || {};
      const active = ['in_progress', 'validating', 'cancelling'].includes(row.status);
      const failed = row.status === 'failed' || row.status === 'completed' && counts.failed && !counts.completed;
      const stage = active && row.progress?.stage;
      const status = failed ? 'Failed' : row.status === 'completed' && counts.failed ? 'Completed with errors' : row.status.replaceAll('_', ' ');
      const progress = row.progress?.lines ? 'line ' + (row.progress.line + 1) + '/' + row.progress.lines : '';
      const reason = row.error || row.progress?.error?.message || (failed ? 'No detailed error was retained.' : '');
      box.append(element('span', row.model || '', 'fw-medium'),
        element('span', [status, stage && stage !== row.status ? stage : '', active ? progress : '', counts.total ? (counts.completed || 0) + '/' + counts.total + ' finished' : '', reason].filter(Boolean).join(' · '), (failed ? 'text-danger' : 'text-secondary') + ' small me-auto'));
      if (['in_progress', 'validating'].includes(row.status)) {
        const stop = element('button', 'Stop', 'btn btn-outline-danger btn-sm');
        stop.addEventListener('click', safe(async () => { await api('jobs/' + encodeURIComponent(row.id), {action: 'cancel'}); await refresh(); }));
        box.append(stop);
      }
      if (!active && row.settings) {
        const saved = row.settings.request ? row.settings : {request: row.settings};
        const reuse = element('button', 'Use settings', 'btn btn-outline-secondary btn-sm');
        reuse.addEventListener('click', safe(() => { apply(saved.request, saved.source); $('prompt').focus(); }));
        box.append(reuse);
        if (failed || row.status === 'cancelled' && !counts.completed) {
          const retry = element('button', 'Retry', 'btn btn-outline-primary btn-sm');
          retry.addEventListener('click', safe(async () => {
            retry.disabled = true;
            try { await api('jobs', saved); await refresh(); } finally { retry.disabled = false; }
          }));
          box.append(retry);
        }
      }
      return box;
    }));
  }
  async function poll() {
    polling = true;
    let failures = 0;
    try {
      while (state.jobs.some(row => ['in_progress', 'validating', 'cancelling'].includes(row.status))) {
        await new Promise(resolve => setTimeout(resolve, Math.min(30000, 2000 * 2 ** failures)));
        try {
          await refresh();
          if (failures) message('');
          failures = 0;
        } catch (error) {
          failures = Math.min(4, failures + 1);
          message('Audio status is unavailable; retrying automatically. ' + error.message);
        }
      }
    } finally { polling = false; }
  }

  async function select(row) {
    selected = row;
    region = null;
    $('audio').pause();
    $('audio').loop = $('loop-region').checked;
    $('selected').hidden = !row;
    $('empty').hidden = !!row;
    listSignature = '';
    renderList();
    if (!row) { $('audio').removeAttribute('src'); if ($('use-selected')) $('use-selected').disabled = true; return; }
    $('title').textContent = row.metadata?.settings?.prompt || row.metadata?.settings?.lines?.map(line => line.text).join(' ').slice(0, 120) || row.filename;
    const measured = row.metadata?.metrics || {};
    $('info').textContent = [seconds(row.seconds), (row.rate / 1000) + ' kHz', row.channels === 2 ? 'stereo' : row.channels + ' ch',
      measured.loudness_lufs != null ? measured.loudness_lufs + ' LUFS' : '', row.metadata?.seed !== undefined ? 'seed ' + row.metadata.seed : '', row.metadata?.model].filter(Boolean).join(' · ');
    $('nc').hidden = row.metadata?.commercial !== 'no';
    $('favourite').textContent = row.favourite ? '★ Favourite' : '☆ Favourite';
    $('favourite').setAttribute('aria-pressed', String(!!row.favourite));
    for (const id of ['favourite', 'save-tags', 'share', 'delete']) $(id).disabled = !row.mine || row.temporary;
    $('tags').value = (row.tags || []).join(', ');
    $('details').textContent = JSON.stringify(row.metadata?.settings || {}, null, 2);
    $('audio').src = audioURL(row.id);
    $('region').textContent = '';
    $('region-start').value = '0';
    $('region-end').value = String(row.seconds || 0);
    if ($('edit-range')) $('edit-range').disabled = true;
    if ($('use-selected')) $('use-selected').disabled = !row.mine;
    peaks = null;
    drawWave();
    try {
      const data = await (await fetch(audioURL(row.id), {cache: 'no-store'})).arrayBuffer();
      if (selected?.id !== row.id) return;
      const context = new (window.AudioContext || window.webkitAudioContext)();
      const decoded = await context.decodeAudioData(data);
      context.close();
      const channel = decoded.getChannelData(0), buckets = 1000, size = Math.max(1, Math.floor(channel.length / buckets));
      peaks = Array.from({length: buckets}, (_, index) => {
        let most = 0;
        for (let at = index * size, end = Math.min(channel.length, at + size); at < end; at += 16) most = Math.max(most, Math.abs(channel[at]));
        return most;
      });
      drawWave();
    } catch (error) { peaks = null; drawWave(); }
  }
  function drawWave() {
    const canvas = $('wave'), context = canvas.getContext('2d');
    canvas.width = canvas.clientWidth * devicePixelRatio;
    canvas.height = 120 * devicePixelRatio;
    const style = getComputedStyle(root);
    context.clearRect(0, 0, canvas.width, canvas.height);
    const duration = $('audio').duration || selected?.seconds || 1;
    if (region) {
      context.fillStyle = style.getPropertyValue('--sr-region') || 'rgba(13,110,253,.18)';
      context.fillRect(region[0] / duration * canvas.width, 0, (region[1] - region[0]) / duration * canvas.width, canvas.height);
    }
    if (peaks) {
      context.fillStyle = style.getPropertyValue('--sr-wave') || '#6c757d';
      const middle = canvas.height / 2, step = canvas.width / peaks.length;
      peaks.forEach((value, index) => { const height = Math.max(1, value * middle); context.fillRect(index * step, middle - height, Math.max(1, step - .5), height * 2); });
    }
    const at = $('audio').currentTime / duration * canvas.width;
    context.fillStyle = style.getPropertyValue('--sr-cursor') || '#dc3545';
    context.fillRect(at, 0, Math.max(1, devicePixelRatio), canvas.height);
  }
  function wavePosition(event) {
    const box = $('wave').getBoundingClientRect();
    return Math.min(1, Math.max(0, (event.clientX - box.left) / box.width)) * recordingDuration();
  }
  function recordingDuration() {
    const duration = $('audio').duration;
    return Number.isFinite(duration) && duration > 0 ? duration : selected?.seconds || 0;
  }
  function updateRegion() {
    const duration = recordingDuration();
    $('region-start').value = region ? region[0].toFixed(2) : '0';
    $('region-end').value = (region ? region[1] : duration).toFixed(2);
    $('region').textContent = region ? 'Region ' + region[0].toFixed(2) + '–' + region[1].toFixed(2) + ' s' : '';
    $('audio').loop = $('loop-region').checked && !region;
    if ($('edit-range')) $('edit-range').disabled = !region || !selected?.mine || !(profile().modes || []).includes('repaint');
    if (region && $('loop-region').checked && ($('audio').currentTime < region[0] || $('audio').currentTime >= region[1])) $('audio').currentTime = region[0];
    drawWave();
  }
  function keepLooping() {
    if (!$('audio').ended && region && $('loop-region').checked && ($('audio').currentTime >= region[1] || $('audio').currentTime < region[0] - .01)) $('audio').currentTime = region[0];
    drawWave();
  }
  let playFrame = null;
  function playbackFrame() {
    keepLooping();
    playFrame = !$('audio').paused ? requestAnimationFrame(playbackFrame) : null;
  }
  let dragFrom = null, dragX = 0;
  $('wave').addEventListener('pointerdown', event => {
    if (!selected || event.button !== 0 || !recordingDuration()) return;
    dragX = event.clientX; dragFrom = wavePosition(event); $('wave').setPointerCapture(event.pointerId);
  });
  $('wave').addEventListener('pointermove', event => {
    if (dragFrom === null) return;
    const to = wavePosition(event);
    region = [Math.min(dragFrom, to), Math.max(dragFrom, to)];
    drawWave();
  });
  $('wave').addEventListener('pointerup', event => {
    if (dragFrom === null) return;
    const to = wavePosition(event);
    if (Math.abs(event.clientX - dragX) < 3) { region = null; $('audio').currentTime = to; }
    else region = [Math.min(dragFrom, to), Math.max(dragFrom, to)];
    dragFrom = null;
    updateRegion();
  });
  $('wave').addEventListener('pointercancel', () => { dragFrom = null; updateRegion(); });
  for (const id of ['region-start', 'region-end']) $(id).addEventListener('change', () => {
    const start = Number($('region-start').value), end = Number($('region-end').value), duration = recordingDuration();
    if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start || end > duration + .01) {
      message('Choose a region whose end is after its start, within this recording.'); updateRegion(); return;
    }
    region = [start, Math.min(end, duration)]; updateRegion();
  });
  $('clear-region').addEventListener('click', () => { region = null; updateRegion(); });
  $('loop-region').addEventListener('change', updateRegion);
  $('audio').addEventListener('timeupdate', keepLooping);
  $('audio').addEventListener('play', () => { if (playFrame === null) playbackFrame(); });
  $('audio').addEventListener('pause', () => { cancelAnimationFrame(playFrame); playFrame = null; });
  $('audio').addEventListener('ended', () => {
    if (!region || !$('loop-region').checked) return;
    $('audio').currentTime = region[0];
    $('audio').play().catch(error => message(error.message));
  });
  $('audio').addEventListener('loadedmetadata', updateRegion);
  addEventListener('resize', drawWave);
  document.addEventListener('weightroom:theme', drawWave);

  async function uploadSource(file, kind = 'reference') {
    if (file.size > 32 * 1024 * 1024) throw new Error('Source audio is limited to 32 MiB.');
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',', 2)[1]);
      reader.onerror = () => reject(new Error('The file could not be read.'));
      reader.readAsDataURL(file);
    });
    return api('assets', {b64_audio: data, filename: file.name, kind, temporary: $('temporary').checked});
  }
  $('source')?.addEventListener('change', safe(async () => {
    const file = $('source').files[0];
    if (!file) return;
    $('source-name').textContent = 'Checking ' + file.name + '…';
    source = null;
    try {
      source = await uploadSource(file);
      $('source-name').textContent = file.name + ' · ' + seconds(source.seconds);
      chooseMode();
    } catch (error) { $('source-name').textContent = ''; throw error; }
  }));
  $('use-selected')?.addEventListener('click', safe(() => {
    if (!selected?.mine) throw new Error('Choose one of your own takes first.');
    source = selected;
    if (source.temporary) $('temporary').checked = true;
    $('source-name').textContent = source.filename + ' · ' + seconds(source.seconds);
    chooseMode();
  }));
  $('model').addEventListener('change', chooseModel);
  $('mode')?.addEventListener('change', chooseMode);
  if (page === 'speech') {
    $('prompt').addEventListener('input', () => chooseVoice());
    ['voice', 'preset', 'design'].forEach(id => $(id).addEventListener('change', () => chooseVoice()));
    document.getElementById('voice-search')?.addEventListener('input', renderSpeechVoices);
  }
  root.querySelectorAll('[data-tag]').forEach(button => button.addEventListener('click', () => {
    const area = $('lyrics'), at = area.selectionStart;
    const before = area.value.slice(0, at), text = (before && !before.endsWith('\n') ? '\n' : '') + button.dataset.tag + '\n';
    area.setRangeText(text, at, area.selectionEnd, 'end');
    area.focus();
  }));
  $('instrumental')?.addEventListener('change', () => { $('lyrics').disabled = $('instrumental').checked; });
  $('write')?.addEventListener('click', safe(async () => {
    $('write').disabled = true;
    try {
      const written = await api('lyrics', {idea: $('idea').value, model: $('helper-model').value.trim(),
        duration: Number($('duration').value), instrumental: $('instrumental').checked});
      $('prompt').value = written.caption;
      if ($('lyrics') && !$('lyrics-wrap').hidden) { $('lyrics').value = written.lyrics; $('instrumental').checked = !written.lyrics; $('lyrics').disabled = !written.lyrics; }
    } finally { $('write').disabled = false; }
  }));
  $('form').addEventListener('submit', safe(async event => {
    event.preventDefault();
    const values = settings();
    if (page === 'speech' && !values.script.length) throw new Error('Write something to say.');
    if (page === 'speech' && profile().kind === 'clone' && values.script.some(line => !line.voice_id)) throw new Error('Choose a saved voice for every speaker.');
    if (page === 'speech' && profile().kind === 'design' && values.script.some(line => !line.instructions)) throw new Error('Describe the voice for every speaker.');
    if (profile().lyrics_required && !values.lyrics?.trim()) throw new Error('This model needs lyrics. Write lyrics or use the song-writing helper first.');
    if (values.mode && !source) throw new Error('Choose source audio for this mode.');
    if (values.mode === 'repaint' && (!(values.repaint_end > values.repaint_start) || values.repaint_start < 0 || source.seconds && values.repaint_end > source.seconds)) throw new Error('Choose a repaint range within the source recording.');
    $('generate').disabled = true;
    try {
      await api('jobs', {request: values, source: values.mode ? source.id : undefined, temporary: $('temporary').checked});
      await refresh();
    } finally { updateAvailability(); }
  }));
  function reuseSelected(variation = false) {
    if (!selected) return;
    const saved = selected.reuse || {request: {...selected.metadata?.settings, model: selected.metadata?.model, seed: selected.metadata?.seed}};
    apply({...saved.request, ...(variation ? {seed: -1, n: 1} : {})}, saved.source);
    if (selected.temporary) $('temporary').checked = true;
    $('prompt').focus();
  }
  $('reuse').addEventListener('click', safe(() => reuseSelected()));
  $('variation').addEventListener('click', safe(() => reuseSelected(true)));
  $('edit-range')?.addEventListener('click', safe(() => {
    if (!selected || !region) return;
    const current = $('model').value;
    const model = (profile().modes || []).includes('repaint') ? current : selected.metadata?.model;
    apply({...selected.reuse?.request || selected.metadata?.settings, model, mode: 'repaint'}, selected.id);
    source = selected;
    if (source.temporary) $('temporary').checked = true;
    $('source-name').textContent = 'This take · ' + seconds(selected.seconds);
    $('start').value = region[0].toFixed(1);
    $('end').value = region[1].toFixed(1);
    $('prompt').focus();
  }));
  $('favourite').addEventListener('click', safe(async () => {
    const changed = await api('assets/' + encodeURIComponent(selected.id), {favourite: !selected.favourite});
    selected = changed;
    await refresh();
    select(changed);
  }));
  $('save-tags').addEventListener('click', safe(async () => {
    const tags = $('tags').value.split(',').map(tag => tag.trim()).filter(Boolean);
    selected = await api('assets/' + encodeURIComponent(selected.id), {tags});
    await refresh();
  }));
  $('download').addEventListener('click', () => {
    if (!selected) return;
    if (selected.metadata?.commercial === 'no' && !licenceNoted) {
      showDialog('Non-commercial licence', [element('p', 'This was made with ' + (selected.metadata?.model || 'a model') + ' under ' + (selected.metadata?.licence || 'a non-commercial licence') + '. Do not use it commercially.'),
        button('I understand, download', () => { licenceNoted = true; $('dialog').close(); location.href = audioURL(selected.id, $('format').value); })]);
      return;
    }
    location.href = audioURL(selected.id, $('format').value);
  });
  $('share').addEventListener('click', () => {
    if (!selected) return;
    let confirmed = false;
    const boxes = state.groups.map(group => {
      const label = element('label', ' ' + group, 'form-check d-block');
      const box = element('input', undefined, 'form-check-input');
      box.type = 'checkbox'; box.value = group; box.checked = (selected.shared_with || []).includes(group);
      label.prepend(box);
      return label;
    });
    showDialog('Share with groups', [...(boxes.length ? boxes : [element('p', 'There are no groups.', 'text-secondary')]), button('Save', safe(async () => {
      const groups = boxes.map(label => label.querySelector('input')).filter(box => box.checked).map(box => box.value);
      if (selected.metadata?.commercial === 'no' && groups.length && !confirmed) { confirmed = true; throw new Error('This audio is under a non-commercial licence. Press Save again to share it anyway.'); }
      await api('assets/' + encodeURIComponent(selected.id), {groups});
      $('dialog').close();
      await refresh();
    }))]);
  });
  $('delete').addEventListener('click', safe(async () => {
    if (!await window.suiteConfirmLoss('Delete this audio permanently?')) return;
    await api('assets/' + encodeURIComponent(selected.id), undefined, 'DELETE');
    older = older.filter(row => row.id !== selected.id);
    $('dialog').close();
    select(null);
    await refresh();
  }));
  $('filter').addEventListener('change', safe(refresh));
  $('favourites').addEventListener('change', safe(refresh));
  $('older').addEventListener('click', safe(async () => {
    if (!nextPage || loadingOlder) return;
    loadingOlder = true;
    $('older').disabled = true;
    try {
      const wantedQuery = query();
      const page = await api(wantedQuery + '&' + new URLSearchParams(nextPage));
      if (wantedQuery !== query()) return;
      const known = new Set(state.assets.map(row => row.id));
      older.push(...state.assets.filter(row => !older.some(old => old.id === row.id)), ...page.assets.filter(row => !known.has(row.id)));
      nextPage = page.next;
      await refresh();
    } finally { loadingOlder = false; $('older').disabled = false; }
  }));
  function button(text, action, className = 'btn btn-primary') {
    const node = element('button', text, className + ' mt-2');
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  }
  function showDialog(title, children) {
    $('dialog-title').textContent = title;
    $('dialog-content').replaceChildren(...children);
    $('dialog').showModal();
  }
  window.soundroomRefresh = safe(refresh);  // the speech page's other tabs refresh voices through it
  safe(refresh)();
})();
