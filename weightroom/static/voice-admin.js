/* Voice selections and non-secret drafts live only in this tab's page memory. */
(() => {
  'use strict';
  if (window.voiceAdmin) { window.voiceAdmin.setup(); return; }
  const drafts = new Map();
  let replacing = false;
  const scope = () => location.pathname;
  function identity(form) { return scope() + '|' + form.getAttribute('action') + '|' + [...form.elements].map(field => field.name).filter(name => name?.startsWith('value-')).sort().join(','); }
  function capture(except = []) {
    document.querySelectorAll('[data-voice-admin] form[data-hold-refresh], [data-voice-admin] form[data-settings-fragment]').forEach(form => {
      const key = identity(form);
      if (except.includes(form)) { drafts.delete(key); return; }
      const fields = [...form.elements].filter(input => input.name && !['hidden', 'password', 'submit'].includes(input.type) && input.autocomplete !== 'one-time-code');
      const values = fields.map(input => ({name: input.name, type: input.type, value: input.value, checked: input.checked}));
      const changed = fields.some(input => input.type === 'checkbox' ? input.checked !== input.defaultChecked : input.tagName === 'SELECT' ?
        [...input.options].some(option => option.selected !== option.defaultSelected) : input.value !== input.defaultValue);
      if (changed) drafts.set(key, values); else drafts.delete(key);
    });
  }
  function select(root, key) {
    root.querySelectorAll('[data-voice-panel]').forEach(panel => { panel.hidden = panel.dataset.voicePanel !== key; });
    root.querySelectorAll('[data-voice-choice]').forEach(link => {
      const active = link.dataset.voiceChoice === key;
      link.classList.toggle('active', active);
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
  }
  function setup() {
    const root = document.querySelector('[data-voice-admin]');
    if (!root) return;
    root.querySelectorAll('form').forEach(form => {
      for (const saved of drafts.get(identity(form)) || []) {
        const input = [...form.elements].find(field => field.name === saved.name && field.type === saved.type);
        if (!input) continue;
        if (input.type === 'checkbox') input.checked = saved.checked; else input.value = saved.value;
      }
    });
    const key = new URL(location.href).searchParams.get(root.dataset.voiceQuery || 'number');
    const panels = [...root.querySelectorAll('[data-voice-panel]')];
    if (panels.length) select(root, panels.find(panel => panel.dataset.voicePanel === key)?.dataset.voicePanel || panels[0].dataset.voicePanel);
  }
  async function replacePage(response, except = []) {
    if (new URL(response.url).pathname === '/sign-in') { location.assign('/sign-in'); return; }
    const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
    const next = doc.querySelector('#page-content');
    if (!next || !doc.querySelector('[data-voice-admin]')) { location.assign(response.url); return; }
    capture(response.ok ? except : []);
    const destination = new URL(response.url);
    if (destination.pathname.startsWith('/system/voice/telephone/')) {
      destination.pathname = '/system/voice/telephone';
      const selected = new URL(location.href).searchParams.get('number');
      if (selected && !destination.searchParams.has('number')) destination.searchParams.set('number', selected);
    }
    history.replaceState(history.state, '', destination);
    replacing = true;
    window.htmx.swap('#page-content', next.outerHTML, {swapStyle: 'outerHTML'});
    replacing = false;
    setup();
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-voice-choice]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); event.stopPropagation();
    select(link.closest('[data-voice-admin]'), link.dataset.voiceChoice);
    history.replaceState(history.state, '', link.href);
  });
  document.addEventListener('keydown', event => {
    const link = event.target.closest('[data-voice-choice]');
    if (!link || !['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
    const links = [...link.closest('nav').querySelectorAll('[data-voice-choice]')];
    let index = links.indexOf(link) + (event.key === 'ArrowUp' ? -1 : 1);
    if (event.key === 'Home') index = 0;
    if (event.key === 'End') index = links.length - 1;
    event.preventDefault(); links[(index + links.length) % links.length].focus();
  });
  document.addEventListener('submit', async event => {
    const form = event.target;
    if (!form.closest('[data-voice-admin]') || !form.action.includes('/system/voice/telephone/') || event.defaultPrevented) return;
    // The shared two-click handler runs before submit; browser validation still applies.
    event.preventDefault();
    const button = event.submitter; if (button) button.disabled = true;
    try {
      await replacePage(await fetch(button?.getAttribute('formaction') || form.action, {method: 'POST', body: new URLSearchParams(new FormData(form)), credentials: 'same-origin'}), [form]);
    } catch {
      let message = form.querySelector('[data-voice-save-error]');
      if (!message) { message = document.createElement('p'); message.dataset.voiceSaveError = ''; message.setAttribute('role','alert'); message.className = 'text-danger mt-2'; form.append(message); }
      message.textContent = 'The gateway could not be reached. Your unsent fields are still here; retry when it is available.';
    } finally { if (button) button.disabled = false; }
  });
  document.addEventListener('weightroom:settings-saved', event => {
    if (!event.detail.forms[0]?.closest('[data-voice-admin]')) return;
    event.preventDefault();
    const updated = new URL(location.href); updated.searchParams.set('said', 'Saved.');
    fetch(updated).then(response => replacePage(response, event.detail.forms)).catch(() => {
      const status = event.detail.forms[0].closest('[data-settings-section]').querySelector('[data-settings-status]');
      status.textContent = 'Saved. Refresh to read the current values.';
    });
  });
  document.addEventListener('htmx:beforeSwap', event => {
    if (replacing || event.detail.target?.id !== 'page-content') return;
    capture();
    const detail = event.detail, submitter = detail.requestConfig?.triggeringEvent?.submitter;
    const form = submitter?.form;
    if (form?.closest('[data-voice-admin]') && submitter.name === 'reset' && detail.xhr?.status < 400) {
      const key = identity(form);
      drafts.set(key, (drafts.get(key) || []).filter(field => field.name !== 'value-' + submitter.value));
    }
  });
  document.addEventListener('htmx:afterSwap', setup);
  document.addEventListener('htmx:historyRestore', setup);
  window.voiceAdmin = {setup};
  setup();
})();
