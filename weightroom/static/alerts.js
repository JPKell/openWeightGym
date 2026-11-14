/* Preserve queue position during desk navigation; every control has a native fallback. */
(() => {
  let pending = null;
  document.addEventListener('htmx:beforeSwap', event => {
    if (event.detail.target?.id !== 'alert-workspace') return;
    const list = document.querySelector('#episode-list');
    const trigger = event.detail.requestConfig?.elt;
    pending = list ? {key: list.dataset.listKey, top: list.scrollTop,
      inspect: !!trigger?.closest('[data-open]')} : null;
  });
  document.addEventListener('htmx:afterSettle', event => {
    if (event.detail.target?.id !== 'alert-workspace' || !pending) return;
    const list = document.querySelector('#episode-list');
    if (list?.dataset.listKey === pending.key) list.scrollTop = pending.top;
    if (pending.inspect) {
      if (matchMedia('(max-width: 850px)').matches) {
        const inspector = document.querySelector('#inspector');
        inspector?.focus({preventScroll: true});
        inspector?.scrollIntoView({block: 'start'});
      } else {
        list?.querySelector('[aria-current="true"]')?.focus({preventScroll: true});
      }
    }
    pending = null;
  });
  document.addEventListener('htmx:afterSwap', event => {
    if (event.detail.target?.id === 'alert-workspace') {
      // outerHTML swaps can leave event.detail.target pointing at the old desk.
      window.SuiteTime?.localize(document.querySelector('#alert-workspace') || document);
    }
  });
})();
