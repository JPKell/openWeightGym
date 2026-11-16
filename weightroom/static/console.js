window.suiteNodeLabel = name => name === 'front' ? 'Control node' : name;
// Delegated handlers continue working when htmx replaces a table or form.
(() => {
  const armed = new Map();
  const submitters = new WeakMap();
  const working = new Map();
  const formRequests = new WeakMap();
  const announcement = document.getElementById('action-confirmation-status');
  /** Restore armed controls after cancellation, editing, navigation or their timeout. */
  function reset(form) {
    for (const [button, saved] of armed) {
      if (form && button.form !== form) continue;
      if (button instanceof HTMLInputElement) button.value = saved.value;
      else button.innerHTML = saved.html;
      if (saved.label === null) button.removeAttribute('aria-label');
      else button.setAttribute('aria-label', saved.label);
      button.removeAttribute('data-confirm-armed');
      clearTimeout(saved.timer);
      armed.delete(button);
    }
    if (!armed.size && announcement) announcement.textContent = '';
  }
  /** One shared two-click contract for forms, dialogs and JavaScript actions. */
  function confirmAction(button, description = '') {
    if (!button?.isConnected || button.disabled) return false;
    if (armed.has(button)) { reset(); return true; }
    reset();
    const saved = {html: button.innerHTML, value: button.value, label: button.getAttribute('aria-label')};
    const label = saved.label || (button instanceof HTMLInputElement ? button.value : button.textContent.trim());
    const text = description || button.dataset.confirmText || 'Are you sure?';
    saved.timer = setTimeout(() => reset(), 10000);
    armed.set(button, saved);
    button.dataset.confirmArmed = 'true';
    button.setAttribute('aria-label', text + ' ' + label);
    if (button instanceof HTMLInputElement) button.value = text;
    else button.textContent = text;
    if (announcement) announcement.textContent = text + ' Activate the same button again to confirm, or press Escape to cancel.';
    return false;
  }
  window.suiteConfirmAction = confirmAction;
  /** Add server acknowledgement fields only for the confirmed submission. */
  function acknowledge(form, button) {
    const fields = JSON.parse(button.dataset.confirmFields || form.dataset.confirmFields || '{}');
    const inputs = Object.entries(fields).map(([name, value]) => {
      const input = Object.assign(document.createElement('input'), {type: 'hidden', name, value});
      form.append(input); return input;
    });
    // Native and HTMX serialization both finish during the submit event.
    setTimeout(() => inputs.forEach(input => input.remove()), 0);
  }
  document.addEventListener('click', event => {
    const button = event.target.closest('button, input[type="submit"], input[type="button"]');
    if (![...armed.keys()].includes(button)) reset();
    if (!button?.matches('[data-confirm-two]') || (button.type === 'submit' && button.form)) return;
    if (!confirmAction(button)) { event.preventDefault(); event.stopImmediatePropagation(); }
  }, true);
  document.addEventListener('submit', event => {
    const form = event.target;
    if (working.has(form)) { event.preventDefault(); event.stopImmediatePropagation(); return; }
    if (!(form instanceof HTMLFormElement)) return;
    const button = event.submitter;
    if (!button) {
      // requestSubmit() without a submitter must not bypass a confirmed form.
      if (form.matches('[data-confirm-two]') || form.querySelector('[data-confirm-two]')) {
        event.preventDefault(); event.stopImmediatePropagation();
      }
      return;
    }
    submitters.set(form, button);
    if (!button.matches('[data-confirm-two]') && !form.matches('[data-confirm-two]')) return;
    if (confirmAction(button)) { acknowledge(form, button); return; }
    event.preventDefault(); event.stopImmediatePropagation();
  }, true);
  document.addEventListener('input', event => reset(event.target.closest('form')));
  document.addEventListener('change', event => reset(event.target.closest('form')));
  document.addEventListener('suite:actionsChanged', event => reset(event.detail?.form));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') reset();
    if (event.repeat && ['Enter', ' '].includes(event.key) && event.target.closest('[data-confirm-two], [data-confirm-armed]')) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener('close', () => reset(), true);
  document.addEventListener('htmx:beforeCleanupElement', event => {
    if ([...armed.keys()].some(button => event.detail.elt.contains(button))) reset();
  });
  // A fresh page may already accept clicks before its load event finishes.
  addEventListener('pageshow', event => { if (event.persisted) reset(); });
  /** Mark a submitted action only after its fields have been serialized. */
  function startWorking(form, button) {
    if (!form || !button || working.has(form) || !button.isConnected) return;
    working.set(form, {button, html: button.innerHTML, value: button.value, disabled: button.disabled,
      busy: form.getAttribute('aria-busy')});
    form.setAttribute('aria-busy', 'true');
    if (button instanceof HTMLInputElement) button.value = 'Working…';
    else button.textContent = 'Working…';
    button.disabled = true;
  }
  function finishWorking(form) {
    const saved = working.get(form);
    if (!saved) return;
    saved.button.innerHTML = saved.html;
    saved.button.value = saved.value;
    saved.button.disabled = saved.disabled;
    if (saved.busy === null) form.removeAttribute('aria-busy');
    else form.setAttribute('aria-busy', saved.busy);
    working.delete(form);
  }
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method === 'dialog') return;
    // Native submission serializes the successful submitter before this timer runs.
    // Custom handlers can cancel the event to own their progress feedback.
    setTimeout(() => { if (!event.defaultPrevented) startWorking(form, event.submitter); }, 0);
  });
  document.addEventListener('htmx:beforeRequest', event => {
    const source = event.detail.elt;
    const form = source?.closest('form');
    if (!form || event.detail.requestConfig?.verb === 'get') return;
    if (event.detail.xhr) formRequests.set(event.detail.xhr, form);
    startWorking(form, submitters.get(form) || event.detail.requestConfig?.triggeringEvent?.submitter);
  });
  document.addEventListener('htmx:afterRequest', event => {
    const form = formRequests.get(event.detail.xhr) || event.detail.elt?.closest('form');
    if (form) finishWorking(form);
  });
  addEventListener('pageshow', event => { if (event.persisted) for (const form of working.keys()) finishWorking(form); });
  document.addEventListener('change', event => {
    if (event.target.matches('[data-engine-picker]')) {
      // The page again with the engine's own fields, keeping what was chosen. `node` stays the page's pill;
      // the nodes ticked for the new model travel as `add_to`.
      const url = new URL(window.location.href);
      url.hash = '';
      url.searchParams.set('engine', event.target.value);
      url.searchParams.delete('add_to');
      for (const box of event.target.form.querySelectorAll('[data-model-node]:checked')) url.searchParams.append('add_to', box.value);
      const picker = event.target.form.querySelector('[data-model-file]');
      if (picker?.value) url.searchParams.set('file', JSON.parse(picker.value).path);
      const projector = event.target.form.querySelector('[data-model-projector]');
      if (projector?.value) url.searchParams.set('projector', projector.value); else url.searchParams.delete('projector');
      const name = event.target.form.querySelector('[name="name"]');
      if (name?.value) url.searchParams.set('name', name.value);
      window.location.assign(url.href);
      return;
    }
    const confirmed = event.target.closest('form[data-confirm-two]');
    if (confirmed) reset(confirmed);
    const form = event.target.closest('form[data-auto-submit]');
    if (form && (form.id !== 'request-filters' || event.target.matches('select'))) form.requestSubmit();
  });
  document.addEventListener('selectionchange', () => {
    const selection = window.getSelection(), printed = document.getElementById('printed');
    if (!selection?.rangeCount || selection.isCollapsed || !printed) return;
    const range = selection.getRangeAt(0);
    if (!printed.contains(range.commonAncestorContainer)) return;
    const field = document.querySelector('[data-log-selection]');
    if (!field) return;
    field.value = selection.toString();
    document.querySelector('[data-log-selection-note]').textContent = `${field.value.length.toLocaleString()} selected characters will be sent.`;
    document.querySelector('[data-whole-log]').hidden = false;
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('[data-whole-log]')) return;
    document.querySelector('[data-log-selection]').value = '';
    document.querySelector('[data-log-selection-note]').textContent = '';
    document.querySelector('[data-whole-log]').hidden = true;
    window.getSelection()?.removeAllRanges();
  });
})();


// Recorded instants stay UTC; presentation follows the account's regional preferences.
(() => {
  let preferences = {};
  try { preferences = JSON.parse(document.documentElement.dataset.personalPreferences || '{}'); } catch { /* Device defaults. */ }
  const deviceZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const resolveZone = value => {
    try { return value && value !== 'device' ? new Intl.DateTimeFormat(undefined, {timeZone:value}).resolvedOptions().timeZone : deviceZone; }
    catch { return deviceZone; }
  };
  const zone = resolveZone(preferences.timezone);
  const format = (instant, withZone = true, overrides = {}) => {
    const choices = {...preferences, ...overrides}, timeZone = resolveZone(choices.timezone);
    const date = new Date(instant);
    const options = {timeZone, year:'numeric', month:'short', day:'numeric', hour:'2-digit', minute:'2-digit',
      ...(choices.clock === '12' ? {hour12:true} : {hourCycle:'h23'}), ...(withZone ? {timeZoneName:'short'} : {})};
    if (choices.date_format === 'device') options.month = 'numeric';
    if (choices.date_format !== 'iso') return new Intl.DateTimeFormat(undefined, options).format(date);
    const parts = new Intl.DateTimeFormat('en-CA', {timeZone, year:'numeric',month:'2-digit',day:'2-digit'}).formatToParts(date);
    const part = key => parts.find(item => item.type === key).value;
    const time = new Intl.DateTimeFormat(undefined, {timeZone, hour:'2-digit', minute:'2-digit',
      ...(choices.clock === '12' ? {hour12:true} : {hourCycle:'h23'})}).format(date);
    return `${part('year')}-${part('month')}-${part('day')}, ${time}${withZone ? ' · ' + timeZone : ''}`;
  };
  const localInput = (date, timeZone = zone) => {
    const parts = new Intl.DateTimeFormat('en-CA', {timeZone, year:'numeric',month:'2-digit',day:'2-digit',
      hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date);
    const part = name => parts.find(p => p.type === name).value;
    return `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}:${part('second')}`;
  };
  function localize(root = document) {
    for (const input of root.querySelectorAll?.('input[data-input-zone]:not([data-localized-input])') || []) {
      if (input.dataset.instant) input.value = localInput(new Date(input.dataset.instant));
      input.dataset.localizedInput = 'true';
      input.title = 'Time in ' + zone;
    }
    const times = [...(root.matches?.('time[data-local-time]') ? [root] : []),
      ...root.querySelectorAll?.('time[data-local-time]') || []];
    for (const time of times) {
      const instant = new Date(time.dateTime);
      if (!Number.isFinite(instant.getTime())) continue;
      const table = time.closest('table');
      time.textContent = format(instant, !table);
      if (table && !table.querySelector('[data-table-time-zone]')) {
        const caption = table.caption || table.createCaption();
        caption.classList.add('caption-top', 'small', 'text-secondary');
        const label = document.createElement('span');
        label.dataset.tableTimeZone = '';
        label.textContent = (caption.textContent.trim() ? ' · ' : '') + 'Times: ' + zone;
        caption.append(label);
      }
    }
  }
  // These existing handlers expect wall time in their configured server zone.
  // Convert just for serialization, then keep the field readable in the browser's zone.
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    for (const input of form.querySelectorAll('input[data-input-zone]')) {
      if (!input.value) continue;
      const original = input.value;
      // Interpret the displayed wall time in the chosen zone, even on another device.
      const wanted = Date.parse(original + 'Z');
      let date = new Date(wanted);
      for (let pass = 0; pass < 3; pass++) date = new Date(date.getTime() + wanted - Date.parse(localInput(date) + 'Z'));
      if (!Number.isFinite(date.getTime())) continue;
      const parts = new Intl.DateTimeFormat('en-CA', {timeZone: input.dataset.inputZone,
        year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}).formatToParts(date);
      const part = name => parts.find(p => p.type === name).value;
      input.value = `${part('year')}-${part('month')}-${part('day')}T${part('hour')}:${part('minute')}:${part('second')}`;
      setTimeout(() => { input.value = original; }, 0);
    }
  }, true);
  window.SuiteTime = {format, localize, zone};
  localize();
  document.addEventListener('htmx:afterSwap', event => localize(event.detail?.target || document));
  document.addEventListener('htmx:oobAfterSwap', event => localize(event.detail?.target || document));
})();


// Only opted-in editor tables need client-side placeholder maintenance.
(() => {
  const template = document.getElementById('empty-row-template');
  if (!template) return;
  function empty(table) {
    const body = table.tBodies[0];
    if (!body) return;
    const rows = [...body.rows], placeholders = rows.filter(row => row.hasAttribute('data-empty-row'));
    if (rows.some(row => !row.hasAttribute('data-empty-row'))) {
      placeholders.forEach(row => row.remove());
    } else if (!placeholders.length) {
      const row = template.content.firstElementChild.cloneNode(true);
      row.firstElementChild.colSpan = [...(table.tHead?.rows[0]?.cells || [])].reduce((sum,cell) => sum + cell.colSpan, 0) || 1;
      row.firstElementChild.textContent = table.dataset.emptyMessage || 'No items to show.';
      body.append(row);
    }
  }
  function update() { document.querySelectorAll('table[data-empty-message]').forEach(empty); }
  update();
  let scheduled = false;
  new MutationObserver(records => {
    if (scheduled || !records.some(record => record.target.closest?.('table[data-empty-message]') ||
      [...record.addedNodes].some(node => node.nodeType === 1 && (node.matches?.('table[data-empty-message]') || node.querySelector?.('table[data-empty-message]'))))) return;
    scheduled = true;
    queueMicrotask(() => { scheduled = false; update(); });
  }).observe(document.body, {childList: true, subtree: true});
})();


// Independent polling/stream regions recover independently; one healthy region cannot hide another's failure.
(() => {
  const bar = document.getElementById('connection-status');
  if (!bar) return;
  const failures = new Map();
  const requests = new WeakMap();
  function status(source, okay, retrying = true) {
    if (okay) failures.delete(source);
    else failures.set(source, retrying);
    bar.hidden = failures.size === 0;
    bar.textContent = [...failures.values()].some(Boolean) ? 'Connection lost — retrying' : 'Connection lost. Try the action again.';
  }
  function source(detail) { return detail.elt?.id || detail.requestConfig?.path || 'console'; }
  function polling(detail) { return /(?:^|,)\s*every\s/.test(detail.elt?.getAttribute('hx-trigger') || ''); }
  // htmx redispatches afterRequest on an ancestor after replacing its source.
  // Preserve the original polling region for this XHR, including on recovery.
  const region = detail => requests.get(detail.xhr) || {source: source(detail), polling: polling(detail)};
  document.addEventListener('htmx:beforeRequest', event => {
    if (event.detail.xhr) requests.set(event.detail.xhr, region(event.detail));
  });
  document.addEventListener('htmx:sendError', event => {
    const request = region(event.detail);
    status(request.source, false, request.polling);
  });
  document.addEventListener('htmx:afterRequest', event => {
    const detail = event.detail, code = detail.xhr?.status, request = region(detail);
    if (code >= 200 && code < 400) status(request.source, true);
    else if (request.polling && (code === 0 || code >= 500)) status(request.source, false);
  });
  document.addEventListener('suite:connection', event => status(event.detail.source, event.detail.okay, event.detail.retrying));
})();

// Copy buttons: data-copy-target names the element whose text is copied. A page served over plain
// HTTP has no clipboard API, so there the text is selected in a box and copied the older way.
(() => {
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-copy-target]');
    if (!button) return;
    const text = document.getElementById(button.dataset.copyTarget)?.innerText ?? '';
    /** Say it was copied, then put the label back. */
    const said = () => { const was = button.textContent; button.textContent = 'Copied'; setTimeout(() => { button.textContent = was; }, 1200); };
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(said); return; }
    const box = Object.assign(document.createElement('textarea'), {value: text});
    document.body.append(box);
    box.select();
    document.execCommand('copy');
    box.remove();
    said();
  });
})();
