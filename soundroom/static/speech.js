/* Voice generation: script-room tools and saved voice management. */
(() => {
  'use strict';
  const root = document.querySelector('#soundroom[data-mode="speech"]');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const {element, message, api, safe, audioURL, fill, upload, recorder} = window.soundroomAudio;
  let state = {models: [], voices: [], groups: []}, voiceClip = null;

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
    state = await api('state?kinds=reference');
    const of = kind => state.models.filter(row => row.capabilities?.audio === kind);
    fill($('vo-asr'), of('transcription'));
    fill($('vo-design-model'), of('design'));
    $('vo-design').disabled = !of('design').length;
    $('vo-design-unavailable').hidden = Boolean(of('design').length);
    renderVoices();
  }
  recorder($('vo-record'), $('vo-recording'), async asset => { voiceClip = asset; $('vo-recording').textContent = 'Recorded ' + Math.round(asset.seconds) + ' s'; });
  $('vo-file').addEventListener('change', safe(async () => {
    const file = $('vo-file').files[0];
    if (file) { voiceClip = await upload(file, 'reference'); $('vo-recording').textContent = file.name + ' · ' + Math.round(voiceClip.seconds) + ' s'; }
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
      if (!take) throw new Error('Designing the voice is taking too long; it will appear under Generate when done.');
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
