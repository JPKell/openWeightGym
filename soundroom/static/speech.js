/* SoundRoom speech: script-room tools, modes, transcription and the voice library. */
(() => {
  'use strict';
  const root = document.querySelector('#soundroom[data-mode="speech"]');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const message = error => { $('sr-message').hidden = !error; $('sr-message').textContent = error || ''; };
  async function api(path, body, method) {
    const response = await fetch('/soundroom-api/' + path, {method: method || (body === undefined ? 'GET' : 'POST'),
      headers: body === undefined ? {} : {'content-type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store'});
    if (response.redirected) { location.href = '/sign-in'; throw new Error('Please sign in again.'); }
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || 'The audio operation failed.');
    return value;
  }
  const safe = action => async event => { try { await action(event); message(''); } catch (error) { message(error.message); } };
  const audioURL = id => '/soundroom-api/assets/' + encodeURIComponent(id) + '/content';
  let state = {models: [], voices: [], assets: [], groups: []}, current = null, transcript = null, voiceClip = null;
  let olderRecordings = [], nextRecordingPage = null;
  const defaults = JSON.parse(root.dataset.defaults || '{}');

  root.querySelectorAll('[data-tab]').forEach(tab => tab.addEventListener('click', () => {
    root.querySelectorAll('[data-tab]').forEach(other => { other.classList.toggle('active', other === tab); other.setAttribute('aria-pressed', String(other === tab)); });
    root.querySelectorAll('[data-panel]').forEach(panel => { panel.hidden = panel.dataset.panel !== tab.dataset.tab; });
    if (tab.dataset.tab !== 'studio') safe(refresh)();
  }));

  const studio = $('sr-form');
  function updateScript() {
    if (!$('script-count')) return;
    const text = $('sr-prompt').value, words = (text.trim().match(/\S+/g) || []).length;
    $('script-count').textContent = `${text.length.toLocaleString()} / 20,000 characters · ${words} words`;
    $('script-estimate').textContent = words ? `~${Math.max(1, Math.round(words / 2.5))}s at 150 words/min` : 'Add some words to begin';
  }
  studio.addEventListener('input', updateScript);
  root.addEventListener('soundroom:studio', updateScript);
  updateScript();
  function replaceScript(text) {
    $('sr-prompt').value = text;
    $('sr-prompt').dispatchEvent(new Event('input', {bubbles:true}));
    $('sr-prompt').focus();
  }
  $('script-example')?.addEventListener('change', () => {
    const examples = {
      narration: 'Some ideas start with a little space. A quiet room. An open window. A moment to hear yourself think.\n\nMake room for what comes next.',
      dialogue: 'Maya: What if we started with something simple?\nAlex: A little space. A clear idea.\nMaya: And a voice that makes it feel human.\nAlex: That sounds like a good place to begin.',
      announcement: 'Welcome to the next chapter. Your workspace is ready. Take a breath, settle in, and make something that matters.',
    };
    const text = examples[$('script-example').value];
    if (text) replaceScript(text);
    $('script-example').value = '';
  });
  $('script-import')?.addEventListener('change', async () => {
    const file = $('script-import').files[0];
    if (!file) return;
    try {
      if (!/\.txt$/i.test(file.name) || file.size > 80000) throw new Error('Choose a UTF-8 .txt file with at most 20,000 characters.');
      const text = await file.text();
      if (text.length > 20000) throw new Error('The script exceeds 20,000 characters. Shorten it before importing.');
      replaceScript(text); message('');
    } catch (error) { message(error.message); }
    finally { $('script-import').value = ''; }
  });
  $('add-speaker')?.addEventListener('click', () => {
    if ($('sr-prompt').value.length > 19940) { message('Shorten the script before adding a speaker.'); return; }
    let number = 1;
    const names = new Set([...$('sr-prompt').value.matchAll(/^\s*([^:\n]{1,40}):/gm)].map(match => match[1].trim()));
    while (names.has(`Speaker ${number}`)) number++;
    replaceScript($('sr-prompt').value + ($('sr-prompt').value ? '\n' : '') + `Speaker ${number}: Your next line.`);
    message('');
  });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && root.isConnected && !root.querySelector('[data-panel="studio"]').hidden && !$('sr-generate').disabled) {
      event.preventDefault(); studio.requestSubmit();
    }
  });

  async function refresh() {
    const latest = await api('state?kinds=recording,reference');
    if (!olderRecordings.length) nextRecordingPage = latest.next;
    const newest = new Set(latest.assets.map(row => row.id));
    state = {...latest, assets: [...latest.assets, ...olderRecordings.filter(row => !newest.has(row.id))]};
    $('tr-older').hidden = !nextRecordingPage;
    const of = kind => state.models.filter(row => row.capabilities?.audio === kind);
    fill($('tr-model'), of('transcription'), undefined, state.preferences?.transcription_model || defaults.transcription);
    fill($('tr-diarize'), of('diarization'), 'Not needed');
    fill($('vo-asr'), of('transcription'));
    fill($('vo-design-model'), of('design'));
    $('vo-design').disabled = !of('design').length;
    $('vo-design-unavailable').hidden = Boolean(of('design').length);
    $('tr-go').disabled = !current || !of('transcription').length;
    renderRecordings();
    renderVoices();
    if ($('sc-voice')) {
      const old = $('sc-voice').value || state.preferences?.voice;
      $('sc-voice').replaceChildren(...state.voices.map(voice => new Option(voice.name, voice.id)));
      if ([...$('sc-voice').options].some(row => row.value === old)) $('sc-voice').value = old;
      chatLink();
    }
  }
  function chatLink() {
    const link = $('sc-open'), url = new URL(link.dataset.base, location.href);
    if ($('sc-voice').value) url.searchParams.set('voice', $('sc-voice').value);
    if ($('sc-record').checked) url.searchParams.set('record', '1');
    link.href = url.toString();
    link.classList.toggle('disabled', !$('sc-voice').value);
    link.setAttribute('aria-disabled', String(!$('sc-voice').value));
    link.tabIndex = $('sc-voice').value ? 0 : -1;
    $('sc-unavailable').hidden = Boolean($('sc-voice').value);
  }
  $('sc-voice')?.addEventListener('change', chatLink);
  $('sc-record')?.addEventListener('change', chatLink);
  function fill(select, models, none, preferred) {
    const old = select.value;
    select.replaceChildren(...(none || !models.length ? [new Option(none || 'No compatible models available', '')] : []), ...models.map(row => new Option(row.id, row.id)));
    select.disabled = !none && !models.length;
    const chosen = [old, preferred].find(name => name && [...select.options].some(row => row.value === name));
    if (chosen) select.value = chosen;
  }
  function renderRecordings() {
    $('tr-list').replaceChildren(...state.assets.filter(row => ['recording', 'reference'].includes(row.kind)).map(row => {
      const item = element('button', undefined, 'list-group-item list-group-item-action sr-item');
      item.type = 'button';
      const said = row.metadata?.transcripts?.[0]?.text;
      item.append(element('span', row.filename, 'sr-item-title'), element('span', [Math.round(row.seconds) + ' s', said ? '“' + said.slice(0, 80) + '”' : 'not transcribed'].join(' · '), 'text-secondary small'));
      item.addEventListener('click', () => choose(row));
      return item;
    }));
  }
  $('tr-older').addEventListener('click', safe(async () => {
    if (!nextRecordingPage) return;
    $('tr-older').disabled = true;
    try {
      const page = await api('state?kinds=recording,reference&' + new URLSearchParams(nextRecordingPage));
      const known = new Set(state.assets.map(row => row.id));
      olderRecordings.push(...state.assets.filter(row => !olderRecordings.some(old => old.id === row.id)), ...page.assets.filter(row => !known.has(row.id)));
      nextRecordingPage = page.next;
      await refresh();
    } finally { $('tr-older').disabled = false; }
  }));
  function choose(row) {
    current = row;
    $('tr-audio').hidden = false;
    $('tr-audio').src = audioURL(row.id);
    transcript = row.metadata?.transcripts?.[0] || null;
    show();
    $('tr-go').disabled = !$('tr-model').value;
  }
  function show() {
    const box = $('tr-result');
    box.classList.toggle('text-secondary', !transcript);
    if (!transcript) { box.textContent = current ? 'Not transcribed yet.' : 'Choose a recording and a model.'; return; }
    box.replaceChildren(...(transcript.segments?.length ? transcript.segments : [{text: transcript.text, start: 0}]).map(part => {
      const line = element('p', undefined, 'mb-1');
      line.append(element('span', stamp(part.start || 0) + (part.speaker != null ? ' · ' + part.speaker : '') + '  ', 'text-secondary small font-monospace'), document.createTextNode(part.text));
      return line;
    }));
  }
  const stamp = seconds => new Date(seconds * 1000).toISOString().slice(11, 19);
  const cue = (seconds, comma) => new Date(seconds * 1000).toISOString().slice(11, 23).replace('.', comma ? ',' : '.');
  function formatted(kind) {
    const parts = transcript.segments?.length ? transcript.segments : [{text: transcript.text, start: 0, end: 0}];
    const who = part => part.speaker != null ? part.speaker + ': ' : '';
    if (kind === 'json') return JSON.stringify(transcript, null, 2);
    if (kind === 'txt') return parts.map(part => who(part) + part.text).join('\n') + '\n';
    const blocks = parts.map((part, index) => (kind === 'srt' ? (index + 1) + '\n' : '') + cue(part.start, kind === 'srt') + ' --> ' + cue(part.end, kind === 'srt') + '\n' + who(part) + part.text);
    return (kind === 'vtt' ? 'WEBVTT\n\n' : '') + blocks.join('\n\n') + '\n';
  }
  root.querySelectorAll('[data-save]').forEach(button => button.addEventListener('click', () => {
    if (!transcript) return;
    const link = element('a');
    link.href = URL.createObjectURL(new Blob([formatted(button.dataset.save)], {type: 'text/plain'}));
    link.download = (current?.filename || 'transcript').replace(/\.[^.]+$/, '') + '.' + button.dataset.save;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }));
  root.querySelector('[data-copy]').addEventListener('click', safe(async () => { if (transcript) await navigator.clipboard.writeText(formatted('txt')); }));

  async function upload(file, kind) {
    if (file.size > 32 * 1024 * 1024) throw new Error('Recordings are limited to 32 MiB.');
    const data = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',', 2)[1]);
      reader.onerror = () => reject(new Error('The file could not be read.'));
      reader.readAsDataURL(file);
    });
    return api('assets', {b64_audio: data, filename: file.name || 'recording.webm', kind});
  }
  function recorder(button, status, done) {
    let media = null;
    button.addEventListener('click', safe(async () => {
      if (media) { media.stop(); return; }
      if (!navigator.mediaDevices?.getUserMedia) throw new Error('Recording needs HTTPS or localhost.');
      const stream = await navigator.mediaDevices.getUserMedia({audio: true});
      const pieces = [];
      media = new MediaRecorder(stream);
      media.ondataavailable = event => pieces.push(event.data);
      media.onstop = safe(async () => {
        stream.getTracks().forEach(track => track.stop());
        media = null;
        button.textContent = '● Record';
        status.textContent = 'Saving…';
        const file = new File(pieces, 'recording-' + new Date().toISOString().slice(0, 19).replace(/:/g, '') + '.webm', {type: pieces[0]?.type || 'audio/webm'});
        await done(await upload(file, 'recording'));
        status.textContent = 'Saved.';
      });
      media.start();
      button.textContent = '■ Stop';
      status.textContent = 'Recording…';
    }));
  }
  recorder($('tr-record'), $('tr-recording'), async asset => { await refresh(); choose(state.assets.find(row => row.id === asset.id) || asset); });
  recorder($('vo-record'), $('vo-recording'), async asset => { voiceClip = asset; $('vo-recording').textContent = 'Recorded ' + Math.round(asset.seconds) + ' s'; });
  $('tr-file').addEventListener('change', safe(async () => {
    const file = $('tr-file').files[0];
    if (!file) return;
    const asset = await upload(file, 'recording');
    await refresh();
    choose(state.assets.find(row => row.id === asset.id) || asset);
  }));
  $('vo-file').addEventListener('change', safe(async () => {
    const file = $('vo-file').files[0];
    if (file) { voiceClip = await upload(file, 'reference'); $('vo-recording').textContent = file.name + ' · ' + Math.round(voiceClip.seconds) + ' s'; }
  }));
  $('tr-go').addEventListener('click', safe(async () => {
    $('tr-go').disabled = true;
    $('tr-result').textContent = 'Transcribing…';
    try {
      transcript = await api('transcribe', {asset: current.id, model: $('tr-model').value, diarization_model: $('tr-diarize').value || undefined,
        language: $('tr-language').value.trim() || undefined});
      current.metadata.transcripts = [transcript, ...(current.metadata.transcripts || [])].slice(0, 5);
      show();
      await refresh();
    } finally { $('tr-go').disabled = false; }
  }));
  $('vo-fill').addEventListener('click', safe(async () => {
    if (!voiceClip) throw new Error('Record or choose the voice recording first.');
    if (!$('vo-asr').value) throw new Error('No transcription model is available to you.');
    $('vo-transcript').value = (await api('transcribe', {asset: voiceClip.id, model: $('vo-asr').value})).text || '';
  }));
  $('vo-save').addEventListener('click', safe(async () => {
    if (!voiceClip) throw new Error('Record or choose the voice recording first.');
    await api('voices', {name: $('vo-name').value.trim(), asset: voiceClip.id, transcript: $('vo-transcript').value.trim(), consent: $('vo-consent').checked});
    voiceClip = null;
    for (const id of ['vo-name', 'vo-transcript']) $(id).value = '';
    $('vo-consent').checked = false;
    $('vo-recording').textContent = '';
    await refresh();
    window.soundroomRefresh?.();
  }));
  $('vo-design').addEventListener('click', safe(async () => {
    const model = $('vo-design-model').value, description = $('vo-description').value.trim(), sample = $('vo-sample').value.trim();
    if (!model) throw new Error('No voice-design model is available to you.');
    if (!description || !sample) throw new Error('Describe the voice and give a sample sentence.');
    $('vo-design').disabled = true;
    try {
      const job = await api('jobs', {request: {model, script: [{text: sample, instructions: description}]}});
      let take = null;
      for (let tries = 0; tries < 300 && !take; tries++) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        const listed = await api('state?kinds=speech');
        const row = listed.jobs.find(one => one.id === job.id);
        if (row && ['failed', 'cancelled', 'expired'].includes(row.status)) throw new Error('Designing the voice did not finish: ' + (row.error || row.status));
        take = listed.assets.find(one => one.metadata?.job === job.id);
      }
      if (!take) throw new Error('Designing the voice is taking too long; it will appear in Studio when done.');
      await api('voices', {name: $('vo-design-name').value.trim() || description.slice(0, 40), asset: take.id, transcript: sample, description, designed: true});
      await refresh();
      window.soundroomRefresh?.();
    } finally { $('vo-design').disabled = !state.models.some(row => row.capabilities?.audio === 'design'); }
  }));
  function renderVoices() {
    $('vo-list').replaceChildren(...state.voices.map(voice => {
      const item = element('div', undefined, 'list-group-item');
      const top = element('div', undefined, 'd-flex flex-wrap gap-2 align-items-center');
      top.append(element('strong', voice.name, 'me-auto'), element('span', voice.mine ? (voice.consent ? 'cloned with permission' : 'designed') : 'shared with you', 'small text-secondary'));
      const play = element('audio');
      play.controls = true; play.preload = 'none'; play.src = audioURL(voice.asset); play.className = 'w-100 mt-2';
      item.append(top);
      if (voice.mine) {
        const share = element('button', 'Share', 'btn btn-outline-secondary btn-sm');
        share.addEventListener('click', () => {
          const boxes = state.groups.map(group => {
            const label = element('label', ' ' + group, 'form-check d-block');
            const box = element('input', undefined, 'form-check-input');
            box.type = 'checkbox'; box.value = group; box.checked = (voice.shared_with || []).includes(group);
            label.prepend(box);
            return label;
          });
          const save = element('button', 'Save', 'btn btn-primary mt-2');
          save.type = 'button';
          save.addEventListener('click', safe(async () => {
            await api('voices/' + encodeURIComponent(voice.id) + '/share', {groups: boxes.map(label => label.querySelector('input')).filter(box => box.checked).map(box => box.value)});
            $('sr-dialog').close();
            await refresh();
          }));
          $('sr-dialog-title').textContent = 'Share ' + voice.name + ' with groups';
          $('sr-dialog-content').replaceChildren(...boxes, save);
          $('sr-dialog').showModal();
        });
        const remove = element('button', 'Delete', 'btn btn-outline-danger btn-sm');
        remove.addEventListener('click', safe(async () => { if (!await window.suiteConfirmLoss('Delete the saved voice ' + voice.name + '?')) return; await api('voices/' + encodeURIComponent(voice.id), undefined, 'DELETE'); await refresh(); window.soundroomRefresh?.(); }));
        top.append(share, remove);
      }
      if (voice.transcript) item.append(element('p', '“' + voice.transcript + '”', 'small mb-0 mt-1'));
      if (voice.mine && voice.shared_with?.length) item.append(element('p', 'Shared with ' + voice.shared_with.join(', '), 'small text-secondary mb-0'));
      if (voice.mine) item.append(play);
      return item;
    }));
    if (!state.voices.length) $('vo-list').append(element('p', 'No voices yet.', 'text-secondary'));
  }
})();
