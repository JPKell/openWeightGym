/* Shared authenticated audio inputs for Voice generation and Transcribe. */
window.soundroomAudio = (() => {
  'use strict';
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
  function fill(select, models, none, preferred) {
    const old = select.value;
    select.replaceChildren(...(none || !models.length ? [new Option(none || 'No compatible models available', '')] : []), ...models.map(row => new Option(row.id, row.id)));
    select.disabled = !none && !models.length;
    const chosen = [old, preferred].find(name => name && [...select.options].some(row => row.value === name));
    if (chosen) select.value = chosen;
  }
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
  return {element, message, api, safe, audioURL, fill, upload, recorder};
})();
