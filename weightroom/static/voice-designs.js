/* Offline UI study: no network, microphone capture, storage or speech synthesis. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const app = $('voice-app');
  const directions = {
    desk: {title: 'A little room to think.', note: 'A calm, transcript-first workspace. Everything you need, within reach.',
      caption: 'A generous reading space, a quiet listening indicator and a persistent setup panel.', name: 'Conversation desk', palette: 'coffee', mode: 'light', number: '01'},
    focus: {title: 'Less on screen. More in the conversation.', note: 'An immersive voice experience. A single point of focus, with details on demand.',
      caption: 'A luminous voice indicator, centered call controls and a transcript that steps aside when you need space.', name: 'Focus', palette: 'coffee', mode: 'dark', number: '02'},
    control: {title: 'Your conversation. Your control.', note: 'An organized three-column workspace for people who like the whole picture.',
      caption: 'Audio setup on the left, conversation in the center and session details on the right.', name: 'Control room', palette: 'lagoon', mode: 'dark', number: '03'}
  };
  let state = 'listening', active = true, muted = false, seconds = 142, credential = true;
  let pending = null;
  const draft = {server: $('vd-server').value, mode: 'direct', input: $('vd-input').value, output: $('vd-output').value, echo: true};
  const seed = [
    ['user', 'Help me make some space for focused work this morning.', '00:08'],
    ['assistant', 'Of course. Let’s keep it simple. What’s the one thing you’d feel good about finishing today?', '00:14'],
    ['user', 'I want to finish the first draft of my proposal. I keep getting pulled into smaller tasks.', '00:32'],
    ['assistant', 'Give the proposal your first uninterrupted hour. Close the inbox, put the smaller tasks on a list, and aim for a complete draft rather than a perfect one.\n\nWhat time would you like to start?', '00:41']
  ];
  function feedback(text) { $('vd-feedback').textContent = text; }
  function clock(value) { return `${String(Math.floor(value / 60)).padStart(2, '0')}:${String(value % 60).padStart(2, '0')}`; }
  function message(role, text, time = clock(seconds)) {
    const row = $('vd-message').content.firstElementChild.cloneNode(true);
    row.dataset.role = role;
    row.querySelector('.vd-avatar').textContent = role === 'user' ? 'Y' : 'V';
    row.querySelector('[data-speaker]').textContent = role === 'user' ? 'You' : 'Voice';
    row.querySelector('[data-time]').textContent = time;
    row.querySelector('[data-message-text]').textContent = text;
    $('vd-transcript').append(row);
    $('vd-message-count').textContent = $('vd-transcript').childElementCount;
    $('vd-empty').hidden = true;
  }
  function showSeed(empty = false) {
    $('vd-transcript').replaceChildren();
    if (!empty) seed.forEach(row => message(...row));
    $('vd-empty').hidden = !empty;
    $('vd-message-count').textContent = empty ? '0' : '4';
    requestAnimationFrame(() => { $('vd-transcript').scrollTop = $('vd-transcript').scrollHeight; });
  }
  function showSettings(show) {
    $('vd-settings').hidden = !show;
    app.dataset.settingsOpen = String(show);
    $('vd-settings-toggle').setAttribute('aria-expanded', String(show));
  }
  function appearance(palette, mode) {
    const form = document.querySelector('[data-theme-form]');
    const choice = form.querySelector(`[name=palette][value="${palette}"]`);
    if (choice) { choice.checked = true; choice.dispatchEvent(new Event('change', {bubbles: true})); }
    const input = form.querySelector('[name=mode]');
    input.value = mode;
    input.dispatchEvent(new Event('change', {bubbles: true}));
    $('vd-theme').textContent = mode === 'dark' ? 'Light mode' : 'Dark mode';
  }
  function design(name, updateHash = true) {
    const direction = directions[name] || directions.desk;
    name = Object.hasOwn(directions, name) ? name : 'desk';
    app.dataset.layout = name;
    document.querySelectorAll('[data-design]').forEach(button => {
      const selected = button.dataset.design === name;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    $('vd-page-heading').querySelector('h1').textContent = direction.title;
    $('vd-design-note').textContent = direction.note;
    $('vd-caption-index').textContent = `${direction.number} / 03`;
    $('vd-caption').replaceChildren();
    const strong = document.createElement('strong');
    strong.textContent = `${direction.name}. `;
    $('vd-caption').append(strong, direction.caption);
    const logo = document.querySelector('.vd-wordmark');
    logo.href = `#${name}`;
    logo.setAttribute('aria-label', `Voice, ${direction.name} design`);
    showSettings(name !== 'focus');
    appearance(direction.palette, direction.mode);
    if (updateHash) history.replaceState(null, '', `#${name}`);
  }
  function cancelPending() { clearTimeout(pending); pending = null; }
  function render() {
    app.dataset.state = muted && active ? 'muted' : state;
    const status = state === 'replying' ? 'Speaking' : state === 'connecting' ? 'Connecting…' :
      state === 'error' ? 'Connection paused' : active ? (muted ? 'Microphone muted' : 'Listening') : 'Ready when you are';
    $('vd-call-status').textContent = status;
    $('vd-call-hint').textContent = state === 'replying' ? 'You can speak to interrupt at any time.' :
      state === 'connecting' ? 'Preparing your conversation.' : state === 'error' ? 'Check the message below to continue.' :
      active ? (muted ? 'Unmute when you’re ready to speak.' : 'Take your time. I’m listening.') : 'Start a conversation with your local model.';
    $('vd-connection-label').textContent = state === 'connecting' ? 'Connecting' : active ? 'Connected' : 'Disconnected';
    $('vd-input-status').textContent = active ? (muted ? 'Microphone muted' : 'Microphone active') : 'Microphone inactive';
    $('vd-callbar-title').textContent = active ? (muted ? 'Your mic is muted' : 'Your mic is open') : 'Make yourself heard';
    $('vd-callbar-note').textContent = active ? 'Speak naturally to interrupt a reply.' : 'Your audio devices are ready when you are.';
    $('vd-start').hidden = active;
    $('vd-start').disabled = state === 'connecting' || $('vd-scenario').value === 'no-mic';
    $('vd-hangup').hidden = !active;
    $('vd-hangup').querySelector('span').textContent = state === 'connecting' ? 'Cancel' : 'Hang up';
    $('vd-mute').disabled = !active || state === 'connecting';
    $('vd-mute').setAttribute('aria-pressed', String(muted));
    $('vd-mute').querySelector('span').textContent = muted ? 'Unmute' : 'Mute';
    $('vd-finish').disabled = !active || muted || state !== 'listening';
    $('vd-interrupt').disabled = !active || state !== 'replying';
    document.querySelectorAll('[data-connection-field]').forEach(field => { field.disabled = active; });
    $('vd-key-state').textContent = credential ? 'Configured' : 'Key needed';
    $('vd-mode-summary').textContent = $('vd-route').textContent = $('vd-mode').selectedOptions[0].textContent;
    $('vd-mode-help').textContent = $('vd-mode').value === 'hermes' ? 'Talk to an agent with tools. Requires Hermes access.' : 'Talk directly to your local model.';
    $('vd-echo-status').textContent = `Echo cancellation ${$('vd-echo').checked ? 'on' : 'off'}`;
    $('vd-transcript-note').textContent = active ? state === 'replying' ? 'Voice is replying · interrupt any time' : muted ? 'Microphone muted · conversation paused' : 'Listening for your next thought' : 'Start a conversation to continue';
    $('vd-settings-notice').textContent = active ? 'Connection and devices lock during a call.' : 'Edits stay in this tab across design switches.';
    $('vd-event').textContent = active ? (muted ? 'Microphone muted' : 'Audio devices connected') : 'Waiting for a conversation';
    $('vd-duration').textContent = $('vd-detail-duration').textContent = clock(seconds);
  }
  function error(text) {
    cancelPending(); active = false; muted = false; state = 'error';
    $('vd-error').hidden = false; $('vd-error-text').textContent = text;
    render(); feedback(text);
  }
  function scenario(value) {
    cancelPending(); muted = false; active = ['listening', 'replying'].includes(value);
    state = active ? value : 'ready'; seconds = active ? 142 : 0;
    credential = value !== 'no-key';
    $('vd-key').value = ''; $('vd-key').placeholder = credential ? 'Sample credential configured' : 'Enter a sample API key';
    $('vd-error').hidden = true;
    showSeed(['empty', 'no-key'].includes(value));
    $('vd-connection-settings').open = value === 'no-key';
    if (value === 'no-key') { showSettings(true); feedback('First-time setup · enter a sample key, save settings, then start.'); }
    else if (value === 'offline') error('The server is unavailable. Check the server address and try Start conversation again.');
    else if (value === 'no-mic') error('No microphone found. Connect a microphone, then choose Refresh devices in settings.');
    else feedback('Preview updated. All calls, devices and messages are simulated.');
    render();
  }
  function start() {
    if (!credential) { showSettings(true); $('vd-connection-settings').open = true; error('Add a sample API key in Your setup before starting.'); $('vd-key').focus(); return; }
    if (!$('vd-server').checkValidity()) { showSettings(true); $('vd-connection-settings').open = true; error('Enter a valid HTTP or HTTPS server address.'); return; }
    let protocol;
    try { protocol = new URL($('vd-server').value).protocol; } catch { protocol = ''; }
    if (!['http:', 'https:'].includes(protocol)) { error('Use an HTTP or HTTPS server address.'); return; }
    $('vd-error').hidden = true; active = true; state = 'connecting'; seconds = 0; muted = false; render();
    feedback('Simulating connection. Use Cancel to stop startup.');
    pending = setTimeout(() => {
      pending = null;
      if ($('vd-scenario').value === 'offline') { error('The server is still unavailable. Your settings and transcript are preserved.'); return; }
      state = 'listening'; render(); feedback('Demo connected. Finish speaking runs a sample exchange.');
    }, 650);
  }
  function interrupt() {
    if (state !== 'replying') return;
    cancelPending(); state = 'listening'; render(); feedback('Reply interrupted. Ready for your next thought.');
  }
  function finish() {
    if (!active || muted || state !== 'listening') return;
    message('user', 'Let’s start at nine. Help me protect that first hour.');
    state = 'replying'; render(); feedback('Playing a simulated reply. Try Interrupt to stop it.');
    message('assistant', 'Nine it is. Set aside one hour for the proposal, silence notifications, and keep a small list beside you for anything that can wait. Your only goal is a complete first draft.');
    $('vd-transcript').scrollTop = $('vd-transcript').scrollHeight;
    pending = setTimeout(() => { pending = null; state = 'listening'; render(); feedback('Demo reply finished. Listening again.'); }, 5000);
  }
  document.querySelectorAll('[data-design]').forEach(button => button.addEventListener('click', () => design(button.dataset.design)));
  $('vd-scenario').addEventListener('change', event => scenario(event.target.value));
  $('vd-settings-toggle').addEventListener('click', () => showSettings($('vd-settings').hidden));
  $('vd-theme').addEventListener('click', () => appearance(document.documentElement.dataset.siteTheme, document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark'));
  $('vd-start').addEventListener('click', start);
  $('vd-hangup').addEventListener('click', () => {
    const connecting = state === 'connecting'; cancelPending(); active = false; muted = false; state = 'ready'; render();
    feedback(connecting ? 'Startup canceled. No connection was made.' : 'Conversation ended. Transcript stays in this window until reload.');
  });
  $('vd-mute').addEventListener('click', () => { muted = !muted; render(); feedback(muted ? 'Microphone muted.' : 'Microphone unmuted.'); });
  $('vd-interrupt').addEventListener('click', interrupt);
  $('vd-finish').addEventListener('click', finish);
  $('vd-echo').addEventListener('change', () => { draft.echo = $('vd-echo').checked; render(); feedback(`Echo cancellation ${draft.echo ? 'enabled' : 'disabled'} for the demo.`); });
  $('vd-mode').addEventListener('change', render);
  $('vd-transcript-toggle').addEventListener('click', () => {
    const show = $('vd-transcript-body').hidden;
    $('vd-transcript-body').hidden = !show;
    $('vd-transcript-toggle').setAttribute('aria-expanded', String(show));
    $('vd-transcript-toggle').textContent = show ? 'Hide transcript' : 'Show transcript';
  });
  $('vd-settings-form').addEventListener('submit', event => {
    event.preventDefault();
    Object.assign(draft, {server: $('vd-server').value, mode: $('vd-mode').value, input: $('vd-input').value, output: $('vd-output').value, echo: $('vd-echo').checked});
    if (!active && $('vd-key').value.trim()) credential = true;
    render();
    $('vd-settings-notice').textContent = 'Settings saved for this preview tab.';
    feedback('Preview settings saved in memory. Nothing was written to your desktop configuration.');
  });
  $('vd-key').addEventListener('input', () => { credential = Boolean($('vd-key').value.trim()); $('vd-key-state').textContent = credential ? 'Configured' : 'Key needed'; });
  $('vd-import').addEventListener('click', () => $('vd-key-file').click());
  $('vd-key-file').addEventListener('change', async () => {
    const file = $('vd-key-file').files[0]; if (!file) return;
    try {
      if (file.size > 4096) throw new Error('Choose a sample key file smaller than 4 KB.');
      const key = (await file.text()).trim();
      if (!/^[\x21-\x7e]{1,2048}$/.test(key)) throw new Error('Use one sample key without spaces or line breaks.');
      $('vd-key').value = key; credential = true; render(); feedback('Sample key imported into this tab. It will be cleared on reload.');
    } catch (reason) { feedback(reason.message); }
    $('vd-key-file').value = '';
  });
  $('vd-refresh').addEventListener('click', () => {
    if ($('vd-scenario').value === 'no-mic') { $('vd-scenario').value = 'ready'; scenario('ready'); }
    feedback('Sample device list refreshed. Your device selections are preserved.');
  });
  document.addEventListener('keydown', event => {
    if (!event.altKey || event.ctrlKey || event.metaKey || event.target.closest('input, textarea, select, [contenteditable]')) return;
    const controls = {m: $('vd-mute'), i: $('vd-interrupt'), Enter: $('vd-finish')};
    const button = controls[event.key] || controls[event.key.toLowerCase()];
    if (button && !button.disabled) { event.preventDefault(); button.click(); }
  });
  window.addEventListener('hashchange', () => design(location.hash.slice(1), false));
  setInterval(() => { if (active && state !== 'connecting') { seconds++; $('vd-duration').textContent = $('vd-detail-duration').textContent = clock(seconds); } }, 1000);
  function initialize() { showSeed(); render(); design(location.hash.slice(1) || 'desk', false); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {once: true});
  else initialize();
})();
