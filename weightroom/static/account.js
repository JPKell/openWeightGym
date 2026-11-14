/* Settings forms keep drafts in page memory and use the existing native save routes. */
(() => {
  'use strict';
  const forms = new WeakMap();
  const identity = document.documentElement.dataset.themeUser;
  const prefix = 'weightroom-account-draft-v1:' + encodeURIComponent(identity) + ':';
  const section = form => form.id === 'ap-form' ? '/account/audio' : new URL(form.action).pathname.replace('/account/preferences/', '/account/');
  const storageKey = form => prefix + section(form);
  const fields = form => [...form.elements].filter(field => field.name && !['submit', 'button'].includes(field.type));
  const values = form => fields(form).map(field => ({value: field.value, checked: field.checked}));
  function marks() {
    const template = document.getElementById('account-draft-template');
    if (!template) return;
    document.querySelectorAll('a.section-link[href^="/account/"]').forEach(link => {
      let dirty = false;
      try { dirty = Boolean(sessionStorage.getItem(prefix + new URL(link.href).pathname)); } catch { /* In-memory edits still work. */ }
      if (dirty && !link.querySelector('[data-account-draft]')) link.append(template.content.cloneNode(true));
      if (!dirty) link.querySelector('[data-account-draft]')?.remove();
    });
  }
  function restore(form) {
    try {
      const raw = sessionStorage.getItem(storageKey(form));
      if (!raw || raw.length > 20000) return;
      const draft = JSON.parse(raw);
      if (!Array.isArray(draft) || draft.length !== fields(form).length) return;
      fields(form).forEach((field, index) => {
        if (field.tagName !== 'SELECT' || [...field.options].some(option => option.value === draft[index].value)) field.value = draft[index].value;
        if (['checkbox','radio'].includes(field.type)) field.checked = draft[index].checked === true;
      });
    } catch { /* Browser storage may be unavailable. */ }
    update(form); preview();
  }
  function preview() {
    const root = document.documentElement;
    if (document.getElementById('text_size')) root.dataset.textSize = document.getElementById('text_size').value;
    if (document.getElementById('motion')) root.dataset.motion = document.getElementById('motion').value;
    const example = document.getElementById('account-time-preview');
    if (example && window.SuiteTime) {
      const overrides = Object.fromEntries(['timezone', 'date_format', 'clock'].map(name => [name, document.getElementById(name).value]));
      example.textContent = SuiteTime.format(new Date().toISOString(), true, overrides);
    }
  }
  function remember(form) { forms.set(form, values(form)); update(form); }
  function update(form) {
    const dirty = JSON.stringify(values(form)) !== JSON.stringify(forms.get(form));
    const status = form.querySelector('[data-form-state]');
    if (status) status.textContent = dirty ? 'Unsaved changes' : 'No unsaved changes';
    try {
      if (dirty) sessionStorage.setItem(storageKey(form), JSON.stringify(values(form)));
      else sessionStorage.removeItem(storageKey(form));
    } catch { /* Never store credentials, and never block a native save. */ }
    marks();
  }
  function initialize(root = document) {
    root.querySelectorAll('[data-account-form]').forEach(form => {
      if (forms.has(form)) return;
      if (new URLSearchParams(location.search).get('said') === 'Preferences saved.') {
        try { sessionStorage.removeItem(storageKey(form)); } catch { /* No saved browser draft. */ }
      }
      // Preserve the tab's draft before recording the current server values.
      let draft = null;
      try { draft = sessionStorage.getItem(storageKey(form)); } catch { /* In-memory only. */ }
      remember(form);
      if (draft) { try { sessionStorage.setItem(storageKey(form), draft); } catch { /* In-memory only. */ } }
      if (form.id !== 'ap-form') restore(form);
      form.addEventListener('input', () => { update(form); preview(); });
      form.addEventListener('change', () => { update(form); preview(); });
      form.addEventListener('account:saved', () => remember(form));
      form.addEventListener('account:loaded', () => {
        let draft = null;
        try { draft = sessionStorage.getItem(storageKey(form)); } catch { /* In-memory only. */ }
        remember(form);
        if (draft) { try { sessionStorage.setItem(storageKey(form), draft); } catch { /* In-memory only. */ } }
        restore(form);
      });
      form.querySelector('[data-discard]')?.addEventListener('click', () => {
        fields(form).forEach((field, index) => {
          field.value = forms.get(form)[index].value;
          if (['checkbox', 'radio'].includes(field.type)) field.checked = forms.get(form)[index].checked;
        });
        update(form); preview();
      });
    });
    preview();
  }
  document.addEventListener('submit', event => {
    if (event.target.matches('[data-account-form]') && event.target.id !== 'ap-form') {
      const current = event.target.querySelector('[data-form-state]');
      if (current) current.textContent = 'Saving…';
    }
    if (event.target.action?.endsWith('/account/password')) {
      const again = event.target.elements.new_again, password = event.target.elements.new_password;
      if (again.value !== password.value) { event.preventDefault(); again.setCustomValidity('The new passwords do not match.'); again.reportValidity(); }
    }
  });
  document.getElementById('new_again')?.addEventListener('input', event => event.target.setCustomValidity(''));
  document.getElementById('new_password')?.addEventListener('input', () => document.getElementById('new_again').setCustomValidity(''));
  document.querySelector('[data-export-all-preferences]')?.addEventListener('click', async event => {
    const button = event.currentTarget; button.disabled = true;
    try {
      const response = await fetch('/account/preferences.json', {cache: 'no-store'});
      if (!response.ok || response.redirected) throw new Error('Could not export preferences. Try again.');
      const data = await response.json(); data.browser_theme = window.SuiteTheme?.preference();
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type:'application/json'}));
      const link = document.createElement('a'); link.href = url; link.download = 'my-preferences.json'; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { button.textContent = error.message; }
    finally { button.disabled = false; }
  });
  initialize();
  document.addEventListener('htmx:load', event => initialize(event.detail?.elt || document));
})();
