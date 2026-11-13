/* Static download-page review: presentation controls only. */
(() => {
  'use strict';
  const descriptions = {
    hub: 'A clear download, three platform cards and a concise connection guide.',
    guide: 'A focused setup sequence, with instructions for your chosen operating system.',
    reference: 'A compact installation reference beside everything you need to connect.'
  };
  function layout(value, updateHash = true) {
    value = Object.hasOwn(descriptions, value) ? value : 'hub';
    document.querySelectorAll('[data-download-layout]').forEach(panel => { panel.hidden = panel.dataset.downloadLayout !== value; });
    document.querySelectorAll('[data-download-design]').forEach(button => {
      const selected = button.dataset.downloadDesign === value;
      button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected));
    });
    document.getElementById('vdl-design-description').textContent = descriptions[value];
    if (updateHash) history.replaceState(null, '', `#${value}`);
  }
  function initialize() {
    document.querySelectorAll('[data-download-design]').forEach(button => button.addEventListener('click', () => layout(button.dataset.downloadDesign)));
    const form = document.querySelector('[data-theme-form]');
    const palette = form.querySelector('[name=palette][value=carbon-cyan]');
    palette.checked = true; palette.dispatchEvent(new Event('change', {bubbles:true}));
    const appearance = form.querySelector('[name=mode]');
    function theme(mode) {
      appearance.value = mode; appearance.dispatchEvent(new Event('change', {bubbles:true}));
      document.getElementById('vdl-theme').textContent = mode === 'dark' ? 'Light mode' : 'Dark mode';
    }
    theme('dark');
    document.getElementById('vdl-theme').addEventListener('click', () => theme(document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark'));
    document.addEventListener('click', async event => {
      const button = event.target.closest('[data-copy-target]'); if (!button) return;
      const text = document.getElementById(button.dataset.copyTarget).textContent;
      try { await navigator.clipboard.writeText(text); button.textContent = 'Copied'; }
      catch { document.getElementById('vdl-preview-status').textContent = 'Clipboard unavailable. Select and copy the command directly.'; }
    });
    window.addEventListener('hashchange', () => { if (Object.hasOwn(descriptions, location.hash.slice(1))) layout(location.hash.slice(1), false); });
    layout(location.hash.slice(1), false);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {once:true}); else initialize();
})();
