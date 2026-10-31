// Delegated handlers continue working when htmx replaces a table or form.
(() => {
  const armed = new Map();
  const submitters = new WeakMap();
  const working = new Map();
  const formRequests = new WeakMap();
  const dialog = document.getElementById('danger-confirmation');
  let pending = null;
  /** Ask for explicit data-loss acknowledgement without adding fields to the original POST. */
  window.suiteConfirmLoss = description => new Promise(resolve => {
    if (!dialog || pending) { resolve(false); return; }
    pending = resolve;
    dialog.querySelector('[data-confirm-description]').textContent = description;
    const check = dialog.querySelector('[data-loss-confirm]');
    check.checked = false;
    check.labels[0].textContent = 'Yes, ' + description.charAt(0).toLowerCase() + description.slice(1);
    dialog.returnValue = '';
    dialog.showModal();
    dialog.querySelector('[value="cancel"]').focus();
  });
  dialog?.addEventListener('close', () => {
    const resolve = pending;
    pending = null;
    resolve?.(dialog.returnValue === 'confirm' && dialog.querySelector('[data-loss-confirm]').checked);
  });
  /** Restore a button after cancellation, editing or its confirmation timeout. */
  function reset(form) {
    for (const [button, saved] of armed) {
      if (form && button.form !== form) continue;
      button.innerHTML = saved.html;
      if (saved.label === null) button.removeAttribute('aria-label');
      else button.setAttribute('aria-label', saved.label);
      clearTimeout(saved.timer);
      armed.delete(button);
    }
  }
  document.addEventListener('submit', event => {
    const form = event.target;
    if (working.has(form)) { event.preventDefault(); event.stopImmediatePropagation(); return; }
    if (!(form instanceof HTMLFormElement) || form.matches('[data-confirm-dialog]')) return;
    const button = event.submitter;
    if (!button) return;
    submitters.set(form, button);
    if (button.matches('[data-confirm-loss]') && !button.dataset.lossApproved &&
        !form.querySelector('input[data-loss-confirm][required]:checked')) {
      event.preventDefault();
      event.stopImmediatePropagation();
      const description = button.getAttribute('aria-label') || button.textContent.trim();
      window.suiteConfirmLoss(description).then(yes => {
        if (!yes || !form.isConnected || !button.isConnected) return;
        button.dataset.lossApproved = 'yes';
        form.requestSubmit(button);
        delete button.dataset.lossApproved;
      });
      return;
    }
    if (button.matches('[data-confirm-loss]')) return;
    if (!button.matches('[data-confirm-two]') && !form.matches('[data-confirm-two]')) return;
    if (armed.has(button)) { reset(form); return; }
    event.preventDefault();
    event.stopImmediatePropagation();
    reset(form);
    const saved = {html: button.innerHTML, label: button.getAttribute('aria-label')};
    armed.set(button, saved);
    saved.timer = setTimeout(() => reset(form), 10000);
    button.setAttribute('aria-label', 'Are you sure? ' + button.textContent.trim());
    button.textContent = 'Are you sure?';
  }, true);
  document.addEventListener('input', event => reset(event.target.closest('form')));
  document.addEventListener('suite:actionsChanged', event => reset(event.detail.form));
  document.addEventListener('keydown', event => { if (event.key === 'Escape') reset(); });
  addEventListener('pageshow', () => reset());
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
    if (!(form instanceof HTMLFormElement) || form.matches('[data-confirm-dialog]') || form.method === 'dialog') return;
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
  addEventListener('pageshow', () => { for (const form of working.keys()) finishWorking(form); });
  document.addEventListener('change', event => {
    if (event.target.matches('[data-engine-picker]')) {
      const url = new URL(window.location.href);
      url.searchParams.set('engine', event.target.value);
      const node = event.target.form.querySelector('[name="node"]');
      if (node) url.searchParams.set('node', node.value);
      const picker = event.target.form.querySelector('[data-model-file]');
      if (picker?.value) url.searchParams.set('file', JSON.parse(picker.value).path);
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


// Recorded instants stay UTC in markup; display and table labels use this browser's zone.
(() => {
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const options = {year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false};
  const format = (instant, withZone = true) => new Intl.DateTimeFormat(undefined,
    {...options, ...(withZone ? {timeZoneName: 'short'} : {})}).format(new Date(instant));
  const localInput = date => {
    const parts = new Intl.DateTimeFormat('en-CA', {year:'numeric',month:'2-digit',day:'2-digit',
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
      const original = input.value, date = new Date(original);
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
