// Enhance native evidence disclosures; all evidence and forms work without JavaScript.
(() => {
  if (window.reliabilityInstalled) return;
  window.reliabilityInstalled = true;
  let current = null, trigger = null;
  function close() {
    const dialog = document.getElementById('reliability-dialog');
    if (!dialog) return;
    if (current) {
      const body = dialog.querySelector('#reliability-dialog-body').firstElementChild;
      if (body && current.isConnected) current.append(body);
      current = null;
    }
    dialog.close();
    if (trigger?.isConnected) trigger.focus({preventScroll: true});
  }
  document.addEventListener('click', event => {
    if (event.target.closest('[data-reliability-close]')) { close(); return; }
    const link = event.target.closest('[data-reliability-open]');
    const summary = event.target.closest('[data-reliability-detail] > summary');
    if (!link && !summary) return;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const details = summary?.parentElement || document.getElementById(link.dataset.reliabilityOpen);
    const dialog = document.getElementById('reliability-dialog');
    if (!details || !dialog?.showModal) return;
    event.preventDefault();
    if (current) close();
    current = details; trigger = summary || link;
    dialog.querySelector('#reliability-dialog-body').append(details.querySelector(':scope > div'));
    dialog.showModal();
    dialog.querySelector('[data-reliability-close]').focus();
  });
  document.addEventListener('cancel', event => {
    if (event.target.id !== 'reliability-dialog') return;
    event.preventDefault(); close();
  }, true);
  document.addEventListener('change', event => {
    if (event.target.matches('[data-reliability-sort]')) event.target.form.requestSubmit();
  });
  document.addEventListener('htmx:beforeSwap', event => {
    if (current && event.detail.target?.contains(document.getElementById('reliability-dialog'))) close();
  });
})();
