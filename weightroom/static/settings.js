// Each purpose section saves through the existing per-service handlers, in order.
(() => {
  'use strict';
  function setup() {
    document.querySelectorAll('[data-settings-section]').forEach(section => {
      section.querySelector('[data-save-settings]').hidden = false;
      section.querySelectorAll('[data-settings-fallback]').forEach(button => { button.hidden = true; });
    });
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-save-settings]');
    if (!button || button.disabled) return;
    const section = button.closest('[data-settings-section]'), forms = [...section.querySelectorAll('[data-settings-fragment]')];
    if (forms.some(form => !form.reportValidity())) return;
    const status = section.querySelector('[data-settings-status]');
    button.disabled = true; status.className = 'small text-secondary'; status.textContent = 'Saving…';
    let saved = 0;
    try {
      for (const form of forms) {
        const response = await fetch(form.action, {method: 'POST', body: new URLSearchParams(new FormData(form)), credentials: 'same-origin'});
        if (new URL(response.url).pathname === '/sign-in') { location.href = '/sign-in'; return; }
        if (!response.ok) {
          const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
          throw new Error(doc.querySelector('.said-box')?.textContent.trim() || 'A service refused its settings.');
        }
        saved++;
      }
      location.assign(location.pathname + '?said=Saved.' + (location.search.includes('open=') ? '&open=' + encodeURIComponent(new URLSearchParams(location.search).get('open')) : ''));
    } catch (error) {
      status.className = 'small text-danger';
      status.textContent = (saved ? `${saved} service${saved === 1 ? '' : 's'} saved. ` : '') + error.message;
    } finally { button.disabled = false; }
  });
  document.addEventListener('htmx:afterSwap', setup);
  setup();
})();
