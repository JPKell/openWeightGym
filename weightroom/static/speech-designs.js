/* Standalone design studies. No model API calls or retained browser storage. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const data = JSON.parse($('speech-data').textContent);
  const root = $('speech-workspace');
  const designs = {
    desk: ['A focused everyday studio. Write on the left, shape the voice on the right, then review each take below.', 'Give your words a voice.'],
    script: ['A workspace for dialogue. Keep the cast beside your script, with per-speaker voices and uninterrupted writing space.', 'Every character has a voice.'],
    listen: ['A voice-first workspace. Browse the cast, then write and listen side by side to find the right delivery.', 'Find the voice. Feel the difference.'],
  };
  let layout = 'desk', scenario = 'normal', selected = data.takes[0], sequence = 0;
  let favouriteOnly = false, auditioning = false;
  const assignments = new Map(), jobs = [];
  const modelDrafts = new Map();
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const model = () => data.models.find(row => row.id === $('sr-model').value);
  const notice = text => { $('action-status').textContent = text; };
  const voice = () => data.voices.find(row => row.id === $('sr-voice').value);
  const names = () => [...new Set([...$('sr-prompt').value.matchAll(/^\s*([^:\n]{1,40}):\s*\S/gm)].map(match => match[1].trim()))];
  const words = text => (text.trim().match(/\S+/g) || []).length;
  const estimate = text => Math.max(1, Math.round(words(text) / 2.5));

  function download(value, name, type) {
    const url = URL.createObjectURL(new Blob([value], {type}));
    const link = document.createElement('a');
    link.href = url; link.download = name; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function rememberModel() {
    const id = $('sr-model').dataset.previous;
    if (id) modelDrafts.set(id, {voice: $('sr-voice').value, preset: $('sr-preset').value,
      design: $('sr-design').value, assignments: new Map(assignments)});
  }

  function changeModel() {
    stopAudition();
    rememberModel();
    const current = model(), saved = modelDrafts.get(current.id);
    $('sr-model').dataset.previous = current.id;
    $('sr-voice').value = saved?.voice || 'juniper';
    $('sr-preset').value = saved?.preset || '';
    $('sr-design').value = saved?.design || '';
    assignments.clear();
    saved?.assignments.forEach((value, key) => assignments.set(key, value));
    $('sr-voice-wrap').hidden = current.kind !== 'clone';
    $('sr-preset-wrap').hidden = current.kind !== 'speech';
    $('sr-design-wrap').hidden = current.kind !== 'design';
    $('sr-licence').textContent = current.licence;
    $('model-node').textContent = `● ${current.node} · sample node`;
    renderVoices(); renderSpeakers(); updateSummary(); updateAvailability();
  }

  function renderVoices() {
    const query = $('voice-search').value.trim().toLowerCase();
    const voices = scenario === 'no-voices' ? [] : data.voices.filter(row =>
      `${row.name} ${row.description} ${row.source}`.toLowerCase().includes(query));
    $('voice-choices').replaceChildren(...voices.map(row => {
      const button = clone('speech-voice-template');
      button.dataset.voice = row.id;
      button.querySelector('[data-initials]').textContent = row.initials;
      button.querySelector('[data-name]').textContent = row.name;
      button.querySelector('[data-description]').textContent = row.description;
      button.querySelector('[data-note]').textContent = row.note;
      button.querySelector('[data-source]').textContent = row.source;
      button.setAttribute('aria-pressed', String($('sr-voice').value === row.id));
      button.addEventListener('click', () => {
        $('sr-voice').value = row.id;
        renderVoices(); renderSpeakers(); updateSummary();
        notice(`${row.name} selected. Saved voice selection is independent of browser audition.`);
      });
      return button;
    }));
    $('voice-empty').hidden = voices.length > 0;
    $('voice-empty').textContent = scenario === 'no-voices' ? 'No saved voices. Choose a built-in or voice-design model to continue.' : 'No voices match your search.';
    $('voice-count').textContent = `${scenario === 'no-voices' ? 0 : data.voices.length} voices`;
  }

  function renderSpeakers() {
    const current = model(), detected = names();
    // Keep inputs intact while typing dialogue or changing the layout.
    const signature = JSON.stringify([detected, current.id, $('sr-voice').value, scenario]);
    if ($('sr-speakers').dataset.signature === signature) return;
    $('sr-speakers').dataset.signature = signature;
    const labels = detected.length ? detected : ['Narrator'];
    $('sr-speakers').replaceChildren(...labels.map((name, index) => {
      const label = clone('speech-speaker-template');
      label.querySelector('.sp-speaker-initial').textContent = name.slice(0, 2).toUpperCase();
      label.querySelector('[data-speaker-name]').textContent = name;
      const select = label.querySelector('select');
      const input = current.kind === 'clone' ? select : document.createElement(current.kind === 'design' ? 'textarea' : 'input');
      input.dataset.speaker = name;
      input.setAttribute('aria-label', `${name} ${current.kind === 'design' ? 'voice description' : 'voice'}`);
      if (current.kind === 'clone') {
        const available = scenario === 'no-voices' ? [] : data.voices;
        available.forEach(row => input.add(new Option(row.name, row.id)));
        input.disabled = !available.length;
        input.value = (detected.length ? assignments.get(name) ?? available[index % Math.max(1, available.length)]?.id : $('sr-voice').value) ?? '';
      } else {
        input.className = 'form-control form-control-sm';
        input.maxLength = current.kind === 'design' ? 1000 : 80;
        input.value = (detected.length ? assignments.get(name) : null) ?? (current.kind === 'design' ? $('sr-design').value : $('sr-preset').value);
        input.placeholder = current.kind === 'design' ? 'Describe this voice' : 'Model default';
        select.replaceWith(input);
      }
      assignments.set(name, input.value);
      input.addEventListener('input', () => {
        assignments.set(name, input.value);
        if (!detected.length) {
          $(current.kind === 'clone' ? 'sr-voice' : current.kind === 'design' ? 'sr-design' : 'sr-preset').value = input.value;
          if (current.kind === 'clone') renderVoices();
        }
        updateSummary();
      });
      return label;
    }));
  }

  function payload() {
    const current = model(), detected = names(), lines = [];
    let speaker = detected[0] || 'Narrator';
    for (const raw of $('sr-prompt').value.split('\n')) {
      const match = raw.match(/^\s*([^:\n]{1,40}):\s*(.*)$/);
      if (match && detected.includes(match[1].trim())) speaker = match[1].trim();
      const text = (match ? match[2] : raw).trim();
      if (!text) continue;
      const line = {text};
      const selectedVoice = detected.length ? assignments.get(speaker) : null;
      if (current.kind === 'clone') line.voice_id = selectedVoice ?? $('sr-voice').value;
      if (current.kind === 'speech' && (selectedVoice ?? $('sr-preset').value).trim()) line.voice = (selectedVoice ?? $('sr-preset').value).trim();
      if (current.kind === 'design') line.instructions = (selectedVoice ?? $('sr-design').value).trim();
      lines.push(line);
    }
    return {model: current.id, script: lines, seed: Number($('sr-seed').value), gap: Number($('sr-gap').value)};
  }

  function updateSummary() {
    const text = $('sr-prompt').value, count = words(text), current = model();
    $('script-count').textContent = `${text.length.toLocaleString()} / 20,000 characters · ${count} words`;
    $('script-estimate').textContent = count ? `~${estimate(text)}s at 150 words/min` : 'Add some words to begin';
    const cast = names();
    root.dataset.dialogue = String(cast.length > 0);
    $('generation-summary').textContent = `${cast.length ? `${cast.length} speakers` : current.kind === 'clone' ? voice().name : current.kind === 'design' ? 'Designed voice' : $('sr-preset').value || 'Model default'} on ${current.name.split(' · ')[0]}`;
    $('generation-detail').textContent = `${$('sr-gap').value}s between lines · ${current.node} · ${$('sr-temporary').checked ? 'temporary request' : 'private history'}`;
    $('sr-generate').disabled = !count || scenario === 'offline' || scenario === 'no-models' || (scenario === 'no-voices' && current.kind === 'clone');
  }

  function switchDesign(value, updateURL = true) {
    layout = value in designs ? value : 'desk';
    root.dataset.layout = layout;
    document.querySelectorAll('[data-design]').forEach(button => {
      const active = button.dataset.design === layout;
      button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    });
    $('design-note').textContent = designs[layout][0];
    $('design-headline').textContent = designs[layout][1];
    if (updateURL) history.replaceState(null, '', `#${layout}`);
    renderSpeakers();
  }

  function updateAvailability() {
    const unavailable = scenario === 'offline' || scenario === 'no-models';
    $('availability-note').hidden = !unavailable;
    $('availability-text').textContent = scenario === 'offline'
      ? 'Sample compute is unavailable. Your script and settings are kept; restore the Ready to create state to preview a request.'
      : 'No speech models installed in this sample state. Your script is kept. Add a model from Models → Audio in the live application.';
    $('compute-state').textContent = unavailable ? '○ Compute unavailable' : '● Sample compute ready';
    $('sr-model').disabled = scenario === 'no-models';
    renderVoices(); renderSpeakers(); updateSummary();
  }

  function renderLibrary() {
    const search = $('sr-filter').value.trim().toLowerCase();
    const source = scenario === 'empty' ? [] : data.takes;
    const rows = source.filter(take => (!favouriteOnly || take.favourite) && `${take.title} ${take.voice} ${take.model}`.toLowerCase().includes(search));
    $('sr-list').replaceChildren(...rows.map(take => {
      const row = clone('speech-take-template');
      row.querySelector('[data-title]').textContent = take.title;
      row.querySelector('[data-meta]').textContent = `${take.voice} · ${take.model} · ${take.time}`;
      row.querySelector('[data-duration]').textContent = take.duration;
      const open = row.querySelector('[data-open-take]');
      open.setAttribute('aria-pressed', String(selected?.id === take.id));
      open.addEventListener('click', () => { stopAudition(); selected = take; renderSelected(); renderLibrary(); });
      const favourite = row.querySelector('[data-favourite]');
      favourite.textContent = take.favourite ? '★' : '☆';
      favourite.setAttribute('aria-label', `${take.favourite ? 'Unfavourite' : 'Favourite'} ${take.title}`);
      favourite.setAttribute('aria-pressed', String(take.favourite));
      favourite.addEventListener('click', () => { take.favourite = !take.favourite; renderLibrary(); renderSelected(); });
      return row;
    }));
    $('library-empty').hidden = rows.length > 0;
    $('library-empty').textContent = source.length ? 'No matching recordings. Try another search or turn off Favourites.' : 'No recordings yet. Your first take will find a home here.';
    $('recording-count').textContent = `${rows.length} of ${source.length} sample recordings`;
  }

  function renderSelected() {
    const hasSelected = selected && scenario !== 'empty';
    $('selected-recording').hidden = !hasSelected;
    $('output-empty').hidden = hasSelected;
    if (!hasSelected) return;
    $('sr-title').textContent = selected.title;
    $('sr-info').textContent = `${selected.voice} · ${selected.model} · ${selected.duration}`;
    $('wave-end').textContent = selected.duration;
    $('sr-favourite').setAttribute('aria-pressed', String(selected.favourite));
    $('sr-favourite').textContent = selected.favourite ? '★ Favourite' : '☆ Favourite';
    $('sr-details').textContent = `Illustrative sample; no model audio file.\n\n${data.scripts[selected.script]}\n\n${selected.voice}\n${selected.model}`;
  }

  function stopAudition() {
    window.speechSynthesis?.cancel();
    auditioning = false;
    $('audition-script').textContent = 'Browser audition';
    $('play-take').textContent = 'Audition sample text';
  }

  function audition(text, button) {
    if (auditioning) { stopAudition(); notice('Browser audition stopped.'); return; }
    if (!window.speechSynthesis || !window.SpeechSynthesisUtterance || !speechSynthesis.getVoices().length) {
      notice('No browser speech voice is available on this device. You can still edit scripts and preview generation requests.'); return;
    }
    if (!text.trim()) { notice('Add some text to audition.'); return; }
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = 'en';
    auditioning = true; button.textContent = 'Stop browser audition';
    utterance.onend = () => { stopAudition(); notice('Browser audition finished. This was your device’s voice, not model output.'); };
    utterance.onerror = () => { stopAudition(); notice('Browser audition was interrupted or unavailable. Your script is kept.'); };
    speechSynthesis.speak(utterance);
    notice('Reading with your device’s browser voice. The selected model and saved voice are not used.');
  }

  function renderJobs() {
    $('jobs-section').hidden = !jobs.length;
    $('sr-jobs').replaceChildren(...jobs.map(job => {
      const row = clone('speech-job-template');
      row.querySelector('[data-job-state]').textContent = job.state;
      row.querySelector('[data-job-text]').textContent = `${job.title} · ${job.request.model}${job.temporary ? ' · temporary' : ''}`;
      const button = row.querySelector('[data-job-action]');
      button.textContent = job.state === 'Queued' ? 'Cancel' : 'Export request';
      button.addEventListener('click', () => {
        if (job.state === 'Queued') {
          clearTimeout(job.timer); job.state = 'Cancelled'; notice('Request preview cancelled.'); renderJobs();
        } else download(JSON.stringify(job.request, null, 2), 'speech-request.json', 'application/json');
      });
      return row;
    }));
  }

  $('sr-form').addEventListener('submit', event => {
    event.preventDefault();
    if (!$('sr-form').reportValidity() || $('sr-generate').disabled) return;
    if (model().kind === 'design' && payload().script.some(line => !line.instructions)) {
      notice('Describe the voice before generating, including each named speaker in Script room.');
      if (names().length) switchDesign('script'); else $('sr-design').focus();
      return;
    }
    const job = {id: ++sequence, title: $('sr-prompt').value.trim().slice(0, 48), request: payload(), temporary: $('sr-temporary').checked, state: 'Queued'};
    jobs.unshift(job); renderJobs();
    notice('Request preview queued. No model is being called.');
    job.timer = setTimeout(() => {
      job.state = 'Preview ready'; renderJobs();
      notice('Request preview ready. No model was called and no audio was generated. Export the request to inspect its settings.');
      if (job.temporary) { jobs.splice(jobs.indexOf(job), 1); renderJobs(); notice('Temporary request preview finished and was removed from history. No model was called.'); }
    }, 1400);
  });
  $('sr-model').addEventListener('change', changeModel);
  $('voice-search').addEventListener('input', renderVoices);
  $('sr-prompt').addEventListener('input', () => { renderSpeakers(); updateSummary(); });
  ['sr-gap', 'sr-seed', 'sr-temporary'].forEach(id => $(id).addEventListener('input', updateSummary));
  ['sr-design', 'sr-preset'].forEach(id => $(id).addEventListener('input', () => {
    if (!names().length) { assignments.delete('Narrator'); $('sr-speakers').dataset.signature = ''; renderSpeakers(); }
    updateSummary();
  }));
  $('scenario').addEventListener('change', () => { stopAudition(); scenario = $('scenario').value; updateAvailability(); renderLibrary(); renderSelected(); });
  $('sr-filter').addEventListener('input', renderLibrary);
  $('sr-favourites').addEventListener('click', () => {
    favouriteOnly = !favouriteOnly; $('sr-favourites').setAttribute('aria-pressed', String(favouriteOnly)); renderLibrary();
  });
  $('sr-favourite').addEventListener('click', () => { selected.favourite = !selected.favourite; renderLibrary(); renderSelected(); });
  $('sr-reuse').addEventListener('click', () => {
    stopAudition(); $('sr-prompt').value = data.scripts[selected.script]; renderSpeakers(); updateSummary();
    notice(`Script from “${selected.title}” copied into the editor. Current model settings are kept.`);
  });
  $('export-take').addEventListener('click', () => {
    download(JSON.stringify({sample: true, audio_generated: false, ...selected, text: data.scripts[selected.script]}, null, 2), 'speech-sample-settings.json', 'application/json');
    notice('Sample settings exported. Illustrative recordings have no audio file.');
  });
  $('script-example').addEventListener('change', () => {
    const text = data.scripts[$('script-example').value];
    if (!text) return;
    stopAudition(); $('sr-prompt').value = text; renderSpeakers(); updateSummary();
    if (names().length) switchDesign('script');
    notice('Example script loaded. Edits stay in this tab.');
    $('script-example').value = '';
  });
  $('script-import').addEventListener('change', async () => {
    const file = $('script-import').files[0];
    if (!file) return;
    if (file.size > 80000 || !/\.txt$/i.test(file.name)) { notice('Choose a UTF-8 .txt file with at most 20,000 characters.'); $('script-import').value = ''; return; }
    try {
      const text = await file.text();
      if (text.length > 20000) { notice('The script exceeds 20,000 characters. Shorten it before importing.'); return; }
      stopAudition(); $('sr-prompt').value = text; renderSpeakers(); updateSummary();
      notice(`Imported ${file.name} locally. No file was uploaded.`);
    } catch { notice('The file could not be read. Your previous script is kept.'); }
    finally { $('script-import').value = ''; }
  });
  $('add-speaker').addEventListener('click', () => {
    if ($('sr-prompt').value.length > 19940) { notice('Shorten the script before adding another speaker.'); return; }
    const detected = names();
    let number = detected.length + 1;
    while (detected.includes(`Speaker ${number}`)) number++;
    $('sr-prompt').value += `${$('sr-prompt').value.trim() ? '\n' : ''}Speaker ${number}: Your next line.`;
    renderSpeakers(); updateSummary(); $('sr-prompt').focus();
  });
  $('audition-script').addEventListener('click', () => audition($('sr-prompt').value, $('audition-script')));
  $('play-take').addEventListener('click', () => audition(data.scripts[selected.script], $('play-take')));
  window.addEventListener('pagehide', stopAudition);
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !$('sr-form').hidden) { event.preventDefault(); $('sr-form').requestSubmit(); }
  });
  document.querySelectorAll('[data-design]').forEach(button => button.addEventListener('click', () => switchDesign(button.dataset.design)));
  function switchView(view) {
    stopAudition();
    if (view === 'voices' && model().kind !== 'clone') {
      $('sr-model').value = 'qwen-clone'; changeModel();
    }
    document.querySelectorAll('[data-view]').forEach(button => {
      const active = button.dataset.view === view;
      button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
    });
    const auxiliary = view === 'transcribe' || view === 'chat';
    $('sr-form').hidden = auxiliary; $('auxiliary').hidden = !auxiliary;
    if (view === 'voices') { $('voice-search').focus(); notice('Saved voices are in the Voice & model panel. Voice management remains on the live Speech page.'); }
    if (auxiliary) {
      $('aux-title').textContent = view === 'transcribe' ? 'Transcribe audio' : 'Spoken chat';
      $('aux-copy').textContent = view === 'transcribe' ? 'Transcription stays available as a neighbouring workflow. This study focuses on voice generation; recording, file uploads, diarization and transcript exports remain on the live Speech page.' : 'Spoken chat stays available as a neighbouring workflow. The live page opens a hands-free conversation using a saved voice. This preview does not start a microphone or a call.';
      $('auxiliary').focus();
    }
  }
  document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => switchView(button.dataset.view)));
  $('back-to-generate').addEventListener('click', () => switchView('generate'));
  $('theme').addEventListener('click', () => {
    const select = document.querySelector('[data-theme-form] [name=mode]');
    select.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    select.dispatchEvent(new Event('change', {bubbles: true}));
  });
  const themeLabel = () => { $('theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode'; };
  document.addEventListener('weightroom:theme', themeLabel);
  const browserStatus = () => { $('browser-status').textContent = window.speechSynthesis?.getVoices().length ? 'Browser audition available' : 'Browser audition depends on device voices'; };
  if (window.speechSynthesis) speechSynthesis.addEventListener('voiceschanged', browserStatus);
  document.addEventListener('DOMContentLoaded', () => {
    const palette = document.querySelector('[data-theme-form] [name=palette][value=sage]');
    palette.checked = true; palette.dispatchEvent(new Event('change', {bubbles:true}));
  });
  changeModel(); switchDesign(location.hash.slice(1), false); renderLibrary(); renderSelected(); themeLabel(); browserStatus();
})();
