// Progressive platform choices: without JavaScript every setup remains readable.
(() => {
  'use strict';
  function platform(root, value) {
    root.querySelectorAll('[data-platform-panel]').forEach(panel => { panel.hidden = panel.dataset.platformPanel !== value; });
    root.querySelectorAll('[data-platform]').forEach(button => {
      const selected = button.dataset.platform === value;
      button.classList.toggle('active', selected);
      button.setAttribute('aria-pressed', String(selected));
    });
    root.dataset.voicePlatform = value;
  }
  function initialize(scope = document) {
    const roots = [...scope.querySelectorAll('[data-voice-download]')];
    if (scope.matches?.('[data-voice-download]')) roots.unshift(scope);
    for (const root of roots) {
      if (root.dataset.voicePlatform) continue;
      platform(root, 'windows');
      root.querySelector('[data-platform-navigation]').hidden = false;
    }
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-platform]');
    const root = button?.closest('[data-voice-download]');
    if (root) platform(root, button.dataset.platform);
  });
  document.addEventListener('htmx:afterSwap', event => initialize(event.detail?.target || document));
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initialize(), {once:true});
  else initialize();
})();
