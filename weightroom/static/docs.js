/* Progressive enhancement: native GET search and article links remain usable without JavaScript. */
(() => {
  'use strict';
  /** Initialize a fresh documentation fragment once, including after console navigation. */
  function init() {
    const root = document.getElementById('docs-workspace');
    if (!root || root.dataset.initialized) return;
    root.dataset.initialized = 'true';
    const form = root.querySelector('#docs-search'), query = form.elements.q, scope = form.elements.scope;
    const reading = root.querySelector('#docs-reading'), hero = root.querySelector('.docs-hero');
    const articleUrl = new URL(location.href), home = root.dataset.docsHome === 'true';
    // Fragments never reach the server; retain old map and inline-source bookmarks here.
    if (home && location.hash === '#system-map' && document.querySelector('a[href="/docs/system"]')) {
      location.replace('/docs/system#system-map'); return;
    }
    const codeLink = root.querySelector('.docs-code-reference a');
    if (codeLink && ['#source', '#source-title'].includes(location.hash)) {
      location.replace(codeLink.getAttribute('href') + location.hash); return;
    }
    let timer, controller, generation = 0;
    /** Highlight matches as text nodes, preserving server escaping. */
    function highlight() {
      const terms = query.value.trim().split(/\s+/).filter(Boolean).map(term => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
      if (!terms.length) return;
      const pattern = new RegExp('(' + terms.join('|') + ')', 'gi');
      root.querySelectorAll('.result-excerpt').forEach(element => {
        const parts = element.textContent.split(pattern);
        element.replaceChildren(...parts.map((part, index) => {
          if (!(index % 2)) return document.createTextNode(part);
          const mark = document.createElement('mark'); mark.textContent = part; return mark;
        }));
      });
    }
    /** Show or clear results without replacing the article or its scroll and disclosure state. */
    function setSearching(searching) {
      root.dataset.docsSearching = String(searching);
      reading.hidden = searching;
      if (hero) hero.hidden = searching;
      root.querySelector('#docs-search-results').hidden = !searching;
      form.querySelector('[data-docs-clear]').hidden = !searching;
    }
    /** Request server-scoped results, dropping stale responses and retaining native retry behavior. */
    async function search(push = false) {
      clearTimeout(timer); controller?.abort(); const turn = ++generation;
      const searching = !!query.value.trim() || !!scope.value;
      if (!searching) {
        form.removeAttribute('aria-busy');
        if (root.dataset.initialReadingAbsent === 'true') { location.assign('/docs'); return; }
        setSearching(false); history.replaceState(null, '', home ? '/docs' : articleUrl); return;
      }
      const url = new URL('/docs', location.origin);
      url.searchParams.set('q', query.value.trim()); if (scope.value) url.searchParams.set('scope', scope.value);
      const fragment = new URL(url); fragment.searchParams.set('part', 'search');
      controller = new AbortController(); form.setAttribute('aria-busy', 'true');
      try {
        const response = await fetch(fragment, {signal: controller.signal, credentials: 'same-origin', headers: {'HX-Request': 'true'}});
        const html = await response.text();
        if (turn !== generation || !root.isConnected) return;
        const documentResult = new DOMParser().parseFromString(html, 'text/html');
        const result = documentResult.querySelector('#docs-search-results');
        if (!result) throw new Error('Search unavailable');
        root.querySelector('#docs-search-results').replaceWith(result);
        setSearching(true); highlight();
        if (push) history.pushState(null, '', url); else history.replaceState(null, '', url);
      } catch (error) {
        if (error.name !== 'AbortError') {
          // A normal navigation presents the server's failure or sign-in page and a native retry.
          if (push) location.assign(url);
          else {
            const count = root.querySelector('#docs-result-count');
            count.textContent = 'Search unavailable. Press Search to retry.';
            setSearching(true);
          }
        }
      } finally { if (turn === generation) form.removeAttribute('aria-busy'); }
    }
    if (root.dataset.docsSearching === 'true') root.dataset.initialReadingAbsent = 'true';
    form.addEventListener('submit', event => { event.preventDefault(); search(true); });
    query.addEventListener('input', () => { clearTimeout(timer); controller?.abort(); generation++; timer = setTimeout(() => search(), 250); });
    scope.addEventListener('change', () => search());
    root.addEventListener('click', event => {
      if (!event.target.closest('[data-docs-clear]') || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); query.value = ''; scope.value = ''; search(); query.focus();
    });
    /** Keep keyboard shortcuts local to documentation and leave input editing intact. */
    root.addEventListener('keydown', event => {
      if (event.key === 'Escape' && (query.value || scope.value)) { event.preventDefault(); query.value = ''; scope.value = ''; search(); query.focus(); }
    });
    highlight();
  }
  document.addEventListener('keydown', event => {
    if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.altKey && !event.target.matches('input, textarea, select, [contenteditable]')) {
      const input = document.getElementById('docs-query'); if (input) { event.preventDefault(); input.focus(); }
    }
  });
  addEventListener('popstate', () => { if (document.getElementById('docs-workspace')) location.reload(); });
  document.addEventListener('DOMContentLoaded', init); document.addEventListener('htmx:load', init); init();
})();
