// Read files as data; uploads never treat a filename as a path or execute document content.
for (const form of document.querySelectorAll('[data-collection-upload]')) {
  const file = form.querySelector('[type=file]');
  const button = form.querySelector('[type=submit]');
  const status = form.querySelector('[data-upload-status]');
  form.addEventListener('submit', () => {
    button.disabled = true;
    button.textContent = 'Uploading…';
    status.textContent = 'Uploading and checking the source…';
  });
  let revision = 0;
  file.addEventListener('change', async () => {
    const current = ++revision;
    button.disabled = true;
    form.elements.data.value = '';
    form.elements.name.value = '';
    const chosen = file.files[0];
    if (!chosen) return;
    if (!chosen.size || chosen.size > 1024 * 1024) {
      status.textContent = 'Choose a nonempty file of at most 1 MiB.';
      return;
    }
    try {
      const bytes = new Uint8Array(await chosen.arrayBuffer());
      if (current !== revision) return;
      let binary = '';
      for (let offset = 0; offset < bytes.length; offset += 16384) {
        binary += String.fromCharCode(...bytes.subarray(offset, offset + 16384));
      }
      form.elements.data.value = btoa(binary);
      form.elements.name.value = chosen.name;
      button.disabled = false;
      status.textContent = chosen.name + ' is ready to upload. Select Index / retry afterward.';
    } catch {
      if (current === revision) status.textContent = 'The file could not be read. Choose it again.';
    }
  });
}


// Refresh only document status while indexing; never replace a question, result or file input.
const documents = document.querySelector('[data-collection-documents]');
if (documents) {
  const status = documents.querySelector('[data-index-status]');
  /** Read the current document rows from the collection fragment. */
  const rows = () => [...documents.querySelectorAll('[data-document-id]')];
  /** Return whether any visible document is still being indexed. */
  const indexing = () => rows().some(row => row.dataset.documentState === 'indexing');
  let timer, failures = 0, busy = false;
  /** Check whether the reader has selected text inside the updated collection area. */
  const selected = element => {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed) return false;
    for (let i = 0; i < selection.rangeCount; i++) {
      if (selection.getRangeAt(i).intersectsNode(element)) return true;
    }
    return false;
  };
  /** Summarize ready, indexing and failed document counts. */
  const describe = () => {
    const states = rows().map(row => row.dataset.documentState);
    const active = states.filter(state => state === 'indexing').length;
    const ready = states.filter(state => state === 'ready').length;
    const failed = states.filter(state => state === 'failed').length;
    const pending = states.length - active - ready - failed;
    status.textContent = active ? `Indexing ${active} source(s)… Status updates automatically.` :
      !states.length ? 'Upload a source to begin.' :
      `${ready} ready` + (failed ? ` · ${failed} failed; see the reason below` : '') +
      (pending ? ` · ${pending} awaiting indexing or retry` : '') + '.';
    const current = document.querySelector('#collection-result')?.dataset.hasResult === 'yes' ? 2 : ready ? 1 : 0;
    document.querySelectorAll('#collection-steps li').forEach((step, index) => {
      if (index === current) step.setAttribute('aria-current', 'step');
      else step.removeAttribute('aria-current');
      step.toggleAttribute('data-complete', index < current);
    });
  };
  /** Schedule the next bounded collection-status refresh. */
  const schedule = () => {
    clearTimeout(timer);
    if (!document.hidden && indexing()) timer = setTimeout(refresh, Math.min(2000 * 2 ** failures, 15000));
  };
  /** Refresh document state while preserving active inputs and selected text. */
  async function refresh() {
    if (busy || document.hidden || !indexing()) return;
    busy = true;
    try {
      const response = await fetch(location.pathname, {credentials: 'same-origin', cache: 'no-store'});
      if (!response.ok || response.redirected) throw new Error('Status unavailable');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      const updated = page.querySelector('[data-collection-documents]');
      if (!updated) throw new Error('Status unavailable');
      for (const row of rows()) {
        const next = [...updated.querySelectorAll('[data-document-id]')]
          .find(item => item.dataset.documentId === row.dataset.documentId);
        if (!next || selected(row) || row.contains(document.activeElement)) continue;
        for (const field of ['[data-document-status]', '[data-document-chunks]']) {
          const before = row.querySelector(field), after = next.querySelector(field);
          if (before.innerHTML !== after.innerHTML) before.replaceChildren(...after.childNodes);
        }
        const button = row.querySelector('[data-document-index]');
        const nextButton = next.querySelector('[data-document-index]');
        document.dispatchEvent(new CustomEvent('suite:actionsChanged', {detail: {form: button.form}}));
        button.toggleAttribute('data-confirm-two', nextButton.hasAttribute('data-confirm-two'));
        button.textContent = nextButton.textContent;
        button.value = nextButton.value;
        button.className = nextButton.className;
        row.dataset.documentState = next.dataset.documentState;
      }
      const dimensions = document.querySelector('[data-collection-dimensions]');
      const nextDimensions = page.querySelector('[data-collection-dimensions]');
      if (dimensions && nextDimensions && !selected(dimensions)) dimensions.textContent = nextDimensions.textContent;
      failures = 0;
      describe();
    } catch {
      failures = Math.min(failures + 1, 3);
      status.textContent = 'Status temporarily unavailable; retrying automatically. Refresh status is also available.';
    } finally {
      busy = false;
      schedule();
    }
  }
  document.addEventListener('visibilitychange', schedule);
  document.addEventListener('selectionchange', () => { if (!timer && indexing()) schedule(); });
  describe();
  schedule();
}
