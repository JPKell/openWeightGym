/* SoundRoom personal audio preferences. */
(() => {
  'use strict';
  const $ = id => document.getElementById('ap-' + id);
  if (!$('form')) return;
  const say = (text, bad) => { $('message').hidden = !text; $('message').className = 'alert ' + (bad ? 'alert-danger' : 'alert-success'); $('message').textContent = text; };
  async function api(path, body) {
    const response = await fetch('/soundroom-api/' + path, {method: body ? 'POST' : 'GET', cache: 'no-store',
      headers: body ? {'content-type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || 'Could not save.');
    return value;
  }
  const fill = (select, rows, chosen) => {
    if (!select) return;
    select.replaceChildren(new Option('Page default', ''), ...rows.map(([value, text]) => new Option(text, value)));
    if (rows.some(([value]) => value === chosen)) select.value = chosen;
    else if (chosen) { select.add(new Option(chosen + ' · currently unavailable', chosen)); select.value = chosen; }
  };
  (async () => {
    try {
      const state = await api('state?kinds=speech');
      const kinds = kinds => state.models.filter(row => kinds.includes(row.capabilities?.audio)).map(row => [row.id, row.id]);
      const preferences = state.preferences || {};
      $('format').value = preferences.format || 'wav';
      $('autoplay').checked = preferences.autoplay === true;
      fill($('speech'), kinds(['speech', 'clone', 'design']), preferences.speech_model);
      fill($('transcription'), kinds(['transcription']), preferences.transcription_model);
      fill($('voice'), state.voices.map(voice => [voice.id, voice.name]), preferences.voice);
      $('fields').disabled = false;
      $('form').dispatchEvent(new CustomEvent('account:loaded', {bubbles: true}));
    } catch (error) { say(error.message, true); }
  })();
  $('form').addEventListener('submit', async event => {
    event.preventDefault();
    try {
      await api('preferences', {format: $('format').value, autoplay: $('autoplay').checked, ...($('speech') ? {speech_model: $('speech').value || null,
        transcription_model: $('transcription').value || null, voice: $('voice').value || null} : {})});
      say('Saved.');
      $('form').dispatchEvent(new CustomEvent('account:saved', {bubbles: true}));
    } catch (error) { say(error.message, true); }
  });
})();
