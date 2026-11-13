/* Prepared sample calls only. No audio, sockets, model APIs or persistence. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id), data = JSON.parse($('voice-samples').textContent);
  const designs = {
    desk: ['A natural rhythm. A clearer connection.', '01 · Conversation desk — a calm speaking space, with your models close and your transcript alongside.'],
    transcript: ['Every word, with room to think.', '02 · Transcript studio — a generous conversation record, a compact call dock and a dedicated model rail.'],
    interpreter: ['Your language in. A new language out.', '03 · Interpreter — a clear language bridge with paired speaker lanes and translations beneath the original words.']
  };
  let active = false, muted = false, running = false, phase = 'ready', seconds = 0, next = 1;
  let generation = 0, clock, pending, following = true, responseDraft = 'es';
  const timers = new Set(), messages = [], voiceDrafts = new Map();
  const inputLanguage = () => $('listen-language').value;
  const outputLanguage = () => $('translate').checked ? $('response-language').value : inputLanguage();
  const stt = () => data.recognition.find(m => m.id === $('call-recognition').value);
  const tts = () => data.synthesis.find(m => m.id === $('call-synthesis').value);
  const stamp = n => `${String(Math.floor(n / 60)).padStart(2, '0')}:${String(n % 60).padStart(2, '0')}`;
  const say = text => { $('action-status').textContent = text; };
  function after(fn, delay) {
    const token = generation, timer = setTimeout(() => { timers.delete(timer); if (token === generation) fn(); }, delay);
    timers.add(timer);
  }
  function options(id, values, wanted) {
    const old = $(id).value;
    $(id).replaceChildren(...values.map(v => new Option(v.name, v.id)));
    $(id).value = values.some(v => v.id === wanted) ? wanted : values.some(v => v.id === old) ? old : values[0]?.id || '';
  }
  options('call-recognition', data.recognition); options('call-model', data.conversation); options('call-synthesis', data.synthesis);
  function voiceDescription() {
    const voice = tts().voices.find(v => v.id === $('call-speaker').value);
    voiceDrafts.set(tts().id, voice.id);
    $('voice-description').textContent = voice.description;
    $('voice-summary').textContent = `${voice.name} · ${tts().name}`;
  }
  function voices() { options('call-speaker', tts().voices, voiceDrafts.get(tts().id)); voiceDescription(); }
  function validate() {
    let issue = '';
    if (!stt().languages.includes(inputLanguage())) issue = `${stt().name} does not support ${data.languages[inputLanguage()]} in this demo configuration. Choose Qwen3 ASR or change the listening language.`;
    else if (!tts().languages.includes(outputLanguage())) issue = `${tts().name} does not support ${data.languages[outputLanguage()]} in this demo configuration. Choose Qwen3 TTS or change the response language.`;
    $('compatibility-notice').hidden = !issue; $('compatibility-text').textContent = issue;
    $('connect').disabled = active || !!issue || ['offline','no-models','denied'].includes($('scenario').value);
    $('response-language').disabled = $('swap-languages').disabled = active || !$('translate').checked;
    $('language-summary').textContent = `${data.languages[inputLanguage()]} → ${data.languages[outputLanguage()]} · ${$('translate').checked ? 'Translation on' : 'Same language'}`;
  }
  function lock(on) {
    $('call-settings').querySelectorAll('input,select,textarea').forEach(el => { el.disabled = on; });
    ['listen-language','response-language','translate','swap-languages'].forEach(id => { $(id).disabled = on; });
    $('setup-note').textContent = on ? 'End the call to change models or languages.' : 'Models can be changed before a call.';
    validate();
  }
  const states = {
    ready: ['Ready when you are','Good conversations start with a voice.','Speak naturally. Listen closely. Pick up where you left off.'],
    listening: ['Listening','You have the floor.','Your words appear as they are recognised.'],
    thinking: ['Thinking','A moment to think.','The model is preparing a reply.'],
    speaking: ['Speaking','A voice on the other side.','Follow the transcript. You can interrupt at any time.'],
    muted: ['Microphone muted','Take your time.','Unmute when you are ready.'],
    ended: ['Call ended','A good place to pause.','Your transcript stays here. Start another call when you are ready.'],
    error: ['Unavailable','Let’s reconnect.','Your transcript and model choices are kept.']
  };
  function state(value, note) {
    phase = value;
    $('call-panel').dataset.state = value;
    ['connection-label','call-headline','call-hint'].forEach((id, i) => { $(id).textContent = states[value][i]; });
    $('call-status').textContent = note || (active ? 'Simulated call · Prepared text, no audio capture or playback.' : 'Sample call · No microphone or models are used.');
    $('mute').disabled = $('hangup').disabled = !active;
    $('interrupt').disabled = !active || !['thinking','speaking'].includes(value);
    $('next-turn').disabled = !active || running || muted;
    $('mute').textContent = muted ? 'Unmute mic' : 'Mute mic';
    $('mute').setAttribute('aria-pressed', String(muted));
    validate();
  }
  function scroll(force = false) {
    if (following || force) { $('transcript-scroll').scrollTop = $('transcript-scroll').scrollHeight; $('jump-latest').hidden = true; }
    else $('jump-latest').hidden = false;
  }
  function counts() {
    $('turn-count').textContent = `${messages.length} message${messages.length === 1 ? '' : 's'}`;
    $('transcript-empty').hidden = messages.length > 0;
    $('copy-transcript').disabled = $('export-transcript').disabled = !messages.length || running;
    $('transcript-count').textContent = active ? 'DEMO CALL / LIVE TRANSCRIPT' : 'SAMPLE TRANSCRIPT';
  }
  function translations() {
    messages.forEach(m => { m.node.querySelector('.vc-translation').hidden = !$('show-translations').checked || !m.translation || m.pending; });
  }
  function append(role, text, live = false) {
    const language = role === 'user' ? inputLanguage() : outputLanguage(), translatedLanguage = role === 'user' ? outputLanguage() : inputLanguage();
    const m = {role, language, translatedLanguage, text:live ? '' : text[language], translation:language === translatedLanguage ? '' : text[translatedLanguage], pending:live, status:live ? 'In progress' : 'Complete', timestamp:stamp(seconds), models:{stt:$('call-recognition').value, llm:$('call-model').value, tts:$('call-synthesis').value, voice:$('call-speaker').value}};
    const node = $('voice-message-template').content.firstElementChild.cloneNode(true); m.node = node;
    node.dataset.role = role;
    node.querySelector('[data-speaker]').textContent = role === 'user' ? 'You' : 'Assistant';
    node.querySelector('.vc-avatar').textContent = role === 'user' ? 'YOU' : 'AI';
    node.querySelector('[data-language]').textContent = data.languages[language];
    node.querySelector('time').textContent = m.timestamp;
    node.querySelector('[data-text]').textContent = m.text; node.querySelector('[data-text]').lang = language;
    node.querySelector('[data-translation]').textContent = m.translation; node.querySelector('[data-translation]').lang = translatedLanguage;
    node.querySelector('[data-translation-label]').textContent = `${data.languages[translatedLanguage]} translation · Sample`;
    if (live) { node.dataset.pending = ''; node.querySelector('[data-message-state]').textContent = role === 'user' ? 'Recognising…' : 'Responding…'; }
    messages.push(m); $('transcript').append(node); translations(); counts(); scroll();
    return m;
  }
  function finish(m, reason = 'Complete') {
    if (!m) return;
    m.pending = false; m.status = reason; delete m.node.dataset.pending;
    m.node.querySelector('[data-message-state]').textContent = reason === 'Complete' ? '' : reason;
    if (reason !== 'Complete') m.translation = '';
    m.node.querySelector('[data-translation]').textContent = m.translation;
    translations();
  }
  function cancel(reason) {
    generation++; timers.forEach(clearTimeout); timers.clear();
    finish(pending, reason); pending = null; running = false;
    $('transcript').setAttribute('aria-busy','false'); counts();
  }
  function stream(role, text, done) {
    const m = append(role, text, true), chars = Array.from(text[m.language]); pending = m;
    let position = 0;
    function tick() {
      position = Math.min(chars.length, position + 3); m.text = chars.slice(0, position).join('');
      m.node.querySelector('[data-text]').textContent = m.text; scroll();
      if (position < chars.length) after(tick, 36);
      else { finish(m); pending = null; done(); }
    }
    after(tick, 100);
  }
  function turn() {
    if (!active || running || muted) return;
    const fixture = data.turns[next++ % data.turns.length]; running = true; counts();
    $('transcript').setAttribute('aria-busy','true'); state('listening');
    stream('user', fixture.user, () => { state('thinking'); after(() => {
      state('speaking'); stream('assistant', fixture.assistant, () => {
        running = false; $('transcript').setAttribute('aria-busy','false'); counts();
        state(muted ? 'muted' : 'listening','Reply complete. Choose Next sample turn to continue.'); scroll();
      });
    }, 650); });
  }
  function end(reason = 'Call ended') {
    cancel(reason); active = muted = false; clearInterval(clock); counts(); lock(false); state('ended');
    delete $('voice-workspace').dataset.callActive;
    $('connect').querySelector('span').textContent = 'Start another demo';
  }
  function clear(seed = false) {
    if (active) end();
    messages.length = 0; $('transcript').replaceChildren(); seconds = 0; next = seed ? 1 : 0;
    $('call-timer').textContent = '00:00'; following = true; $('jump-latest').hidden = true;
    if (seed) { append('user',data.turns[0].user); append('assistant',data.turns[0].assistant); }
    counts(); state('ready'); $('connect').querySelector('span').textContent = 'Start demo call';
    $('transcript-note').textContent = seed ? 'Prepared sample · Original + translation' : 'A fresh conversation · Sample demo';
  }
  const failures = {
    offline:'The sample voice service is unavailable. Your transcript and setup are kept. In a live call, check System → Voice or reconnect.',
    'no-models':'No permitted voice models are available in this preview state. A live page would offer setup through Models and Cloud APIs.',
    denied:'Microphone access denied (sample state). In a live call, allow microphone access in browser site settings, then retry.'
  };
  function scenario() {
    if (active) end('Connection ended');
    const value = $('scenario').value;
    $('call-notice').hidden = !failures[value]; $('notice-text').textContent = failures[value] || '';
    if (value === 'empty') clear();
    else if (value === 'ready') { if (!messages.length) clear(true); state('ready'); }
    else state('error');
    validate();
  }
  function design(key) {
    if (!designs[key]) key = 'desk';
    $('voice-workspace').dataset.layout = key; $('design-tagline').textContent = designs[key][0]; $('design-description').textContent = designs[key][1];
    document.querySelectorAll('[data-design]').forEach(b => { b.classList.toggle('active',b.dataset.design === key); b.setAttribute('aria-pressed',String(b.dataset.design === key)); });
    try { history.replaceState(null,'','#' + key); } catch { /* restricted file preview */ }
  }
  const exported = () => messages.map(({node,pending,...m}) => m);
  const plain = () => 'OWG Voice · Sample transcript\n\n' + exported().map(m => `[${m.timestamp}] ${m.role === 'user' ? 'You' : 'Assistant'} · ${data.languages[m.language]}\n${m.text}` + (m.translation ? `\n${data.languages[m.translatedLanguage]} translation: ${m.translation}` : '') + (m.status === 'Complete' ? '' : `\n[${m.status}]`)).join('\n\n');
  $('connect').onclick = () => {
    if ($('connect').disabled || active) return;
    active = true; muted = false; $('voice-workspace').dataset.callActive = ''; lock(true); $('transcript-note').textContent = 'Simulated live transcript · No audio';
    clock = setInterval(() => { $('call-timer').textContent = stamp(++seconds); },1000); turn();
  };
  $('hangup').onclick = () => end(); $('next-turn').onclick = turn;
  $('interrupt').onclick = () => { if (!active || !['thinking','speaking'].includes(phase)) return; cancel('Interrupted'); state(muted ? 'muted' : 'listening','Reply interrupted. Choose Next sample turn when ready.'); };
  $('mute').onclick = () => {
    if (!active) return;
    muted = !muted;
    if (muted && phase === 'listening') { cancel('Not sent · Microphone muted'); state('muted'); }
    else if (!muted && phase === 'muted') state('listening','Microphone unmuted. Choose Next sample turn to continue.');
    else state(phase);
  };
  $('call-synthesis').onchange = () => { voices(); validate(); }; $('call-speaker').onchange = voiceDescription; $('call-recognition').onchange = validate;
  $('call-settings').onsubmit = e => e.preventDefault();
  $('listen-language').onchange = () => { if (!$('translate').checked) $('response-language').value = inputLanguage(); validate(); say('Listening language updated. Earlier messages retain their original languages.'); };
  $('response-language').onchange = () => { responseDraft = $('response-language').value; validate(); };
  $('translate').onchange = () => { if ($('translate').checked) $('response-language').value = responseDraft; else { responseDraft = $('response-language').value; $('response-language').value = inputLanguage(); } validate(); };
  $('swap-languages').onclick = () => { const old = inputLanguage(); $('listen-language').value = $('response-language').value; $('response-language').value = responseDraft = old; validate(); };
  $('show-translations').onchange = translations;
  $('new-conversation').onclick = () => { clear(); say('New conversation ready. Your models, voice and language choices are kept.'); };
  $('scenario').onchange = scenario;
  $('recover').onclick = () => { $('scenario').value = 'ready'; scenario(); say('Sample service restored. No live service was contacted.'); };
  document.querySelectorAll('[data-design]').forEach(b => { b.onclick = () => design(b.dataset.design); });
  window.addEventListener('hashchange',() => design(location.hash.slice(1)));
  $('transcript-scroll').addEventListener('scroll',() => { const el = $('transcript-scroll'); following = el.scrollHeight - el.clientHeight - el.scrollTop < 50; $('jump-latest').hidden = following; });
  $('jump-latest').onclick = () => { following = true; scroll(true); };
  $('export-transcript').onclick = () => {
    const format = $('export-format').value, content = format === 'json' ? JSON.stringify({sample:true,messages:exported()},null,2) : plain();
    const url = URL.createObjectURL(new Blob([content],{type:format === 'json' ? 'application/json' : 'text/plain;charset=utf-8'})), link = document.createElement('a');
    link.href = url; link.download = `owg-voice-transcript.${format}`; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
    say('Transcript exported with original languages and available translations.');
  };
  $('copy-transcript').onclick = async () => { try { await navigator.clipboard.writeText(plain()); say('Transcript copied.'); } catch { say('Clipboard unavailable. Use Export to save the transcript.'); } };
  function appearance(mode) { const input = document.querySelector('[data-theme-form] [name=mode]'); input.value = mode; input.dispatchEvent(new Event('change',{bubbles:true})); $('theme').textContent = mode === 'dark' ? 'Light mode' : 'Dark mode'; }
  $('theme').onclick = () => appearance(document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark');
  window.addEventListener('pagehide',() => { cancel('Call ended'); clearInterval(clock); });
  voices(); design(location.hash.slice(1)); appearance('light');
  const palette = document.querySelector('[data-theme-form] input[name=palette][value=lagoon]');
  palette.checked = true; palette.dispatchEvent(new Event('change',{bubbles:true}));
  clear(true);
})();
