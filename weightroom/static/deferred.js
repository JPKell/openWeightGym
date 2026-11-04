// Keep delayed data failures inside their section while the navigation stays usable.
(() => {
  document.addEventListener('htmx:beforeSwap', event => {
    const detail = event.detail;
    if (!detail.requestConfig?.elt.closest('[data-deferred-loader]') || detail.xhr.status < 400) return;
    const response = new DOMParser().parseFromString(detail.xhr.responseText, 'text/html');
    if (response.getElementById(detail.target.id)?.hasAttribute('data-load-error')) {
      detail.shouldSwap = true;
      detail.isError = false;
    }
  });
  /** Replace a stalled loading message with an explicit retry after a transport failure. */
  const failed = event => {
    const target = event.detail?.target;
    const message = target?.querySelector('[data-loading-message]');
    if (!message) return;
    target.setAttribute('aria-busy', 'false');
    message.textContent = 'Could not load this section. Check the connection and retry.';
    target.querySelector('[data-load-retry]').hidden = false;
  };
  document.addEventListener('htmx:sendError', failed);
  document.addEventListener('htmx:timeout', failed);
  document.addEventListener('htmx:responseError', failed);
})();
