// Telephone editors stay in the DOM so unsent drafts survive number switches.
(() => {
  'use strict';
  function select(root, key, focus = false) {
    root.querySelectorAll('[data-voice-panel]').forEach(panel => { panel.hidden = panel.dataset.voicePanel !== key; });
    root.querySelectorAll('[data-voice-choice]').forEach(link => {
      const active = link.dataset.voiceChoice === key;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    if (focus) root.querySelector('[data-voice-panel]:not([hidden]) input:not([type=hidden])')?.focus();
  }
  function setup() {
    const root = document.querySelector('[data-voice-admin]');
    if (!root) return;
    const key = new URL(location.href).searchParams.get('number');
    const panels = [...root.querySelectorAll('[data-voice-panel]')];
    if (panels.length) select(root, panels.find(panel => panel.dataset.voicePanel === key)?.dataset.voicePanel || panels[0].dataset.voicePanel);
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-voice-choice]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); event.stopPropagation();
    const root = link.closest('[data-voice-admin]');
    select(root, link.dataset.voiceChoice);
    history.replaceState(history.state, '', link.href);
  });
  document.addEventListener('htmx:afterSwap', setup);
  document.addEventListener('htmx:historyRestore', setup);
  setup();
})();
