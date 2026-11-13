/* Independent transcription workspace; reuses the authenticated audio APIs. */
(() => {
  'use strict';
  const root = document.getElementById('soundroom-transcribe');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const {element, api, safe, audioURL, fill, upload, recorder} = window.soundroomAudio;
  const defaults = JSON.parse(root.dataset.defaults || '{}');
  let state = {models: [], assets: []}, current = null, transcript = null;
  let olderRecordings = [], nextRecordingPage = null;
  async function refresh() {
    const latest = await api('state?kinds=recording,reference');
    if (!olderRecordings.length) nextRecordingPage = latest.next;
    const newest = new Set(latest.assets.map(row => row.id));
    state = {...latest, assets: [...latest.assets, ...olderRecordings.filter(row => !newest.has(row.id))]};
    $('tr-older').hidden = !nextRecordingPage;
    const of = kind => state.models.filter(row => row.capabilities?.audio === kind);
    fill($('tr-model'), of('transcription'), undefined, state.preferences?.transcription_model || defaults.transcription);
    fill($('tr-diarize'), of('diarization'), 'Not needed');
    $('tr-go').disabled = !current || !of('transcription').length;
    renderRecordings();
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

  recorder($('tr-record'), $('tr-recording'), async asset => { await refresh(); choose(state.assets.find(row => row.id === asset.id) || asset); });
  $('tr-file').addEventListener('change', safe(async () => {
    const file = $('tr-file').files[0];
    if (!file) return;
    const asset = await upload(file, 'recording');
    await refresh();
    choose(state.assets.find(row => row.id === asset.id) || asset);
  }));
  $('tr-go').addEventListener('click', safe(async () => {
    if (!current || !$('tr-model').value) return;
    $('tr-go').disabled = true;
    $('tr-result').textContent = 'Transcribing…';
    try {
      transcript = await api('transcribe', {asset: current.id, model: $('tr-model').value, diarization_model: $('tr-diarize').value || undefined,
        language: $('tr-language').value.trim() || undefined});
      current.metadata ||= {};
      current.metadata.transcripts = [transcript, ...(current.metadata.transcripts || [])].slice(0, 5);
      show();
      await refresh();
    } catch (error) {
      show();
      throw error;
    } finally { $('tr-go').disabled = !current || !$('tr-model').value; }
  }));
  $('tr-model').addEventListener('change', () => { $('tr-go').disabled = !current || !$('tr-model').value; });
  safe(refresh)();
})();
