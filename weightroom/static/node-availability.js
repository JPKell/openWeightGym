/* Disable only management submissions for the unavailable target node. */
(() => {
  const notice = document.getElementById('node-availability');
  if (!notice) return;
  const offline = new Set(JSON.parse(notice.dataset.offlineNodes));
  /** Disable management submissions for their unavailable target nodes. */
  function update() {
    for (const form of document.querySelectorAll('form[method="post"]')) {
      const url = new URL(form.action, location.href);
      if (!/^\/(models(?:\/|$)|measure-models$|control$|unload|lock|compute-nodes\/(?:engines|resources|gpus|machine-key$)|settings\/(?:loadcoach|spotter)$)/.test(url.pathname)) continue;
      const fields = new FormData(form);
      const node = fields.get('machine') || fields.get('node') || url.searchParams.get('node') || new URL(location.href).searchParams.get('node') || new URL(location.href).searchParams.get('machine') || 'compute';
      if (!offline.has(node)) continue;
      form.dataset.nodeUnavailable = 'true';
      // form.elements also includes submit buttons associated through form="...".
      // Local editors and navigation remain usable while a node is offline.
      for (const control of [...form.elements].filter(field => field.type === 'submit')) {
        control.disabled = true;
        control.title = `${node} is unavailable`;
      }
    }
  }
  update();
  document.addEventListener('htmx:afterSwap', update);
  document.addEventListener('submit', event => {
    if (event.target.dataset.nodeUnavailable === 'true') event.preventDefault();
  });
})();
