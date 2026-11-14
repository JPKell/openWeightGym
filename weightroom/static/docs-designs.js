/* Standalone preview: navigation/search use current rendered documentation, with no service calls. */
(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('docs-data').textContent);
  const pages = data.pages, byUrl = new Map(pages.map(page => [page.url, page]));
  const $ = selector => document.querySelector(selector);
  const notes = {
    library: 'Search first, browse by subject, then read with a persistent contents rail.',
    guided: 'Three clear reading paths for getting started, connecting a client and running the system.',
    reference: 'A compact subject index and a focused reader for frequent technical lookups.',
  };
  let current = null, query = '', scope = '';
  const indexed = pages.map(page => ({...page, haystack: (page.title + ' ' + page.summary + ' ' + page.text).toLocaleLowerCase()}));
  /** Build a safe local route that also retains a deep section anchor. */
  function route(url, anchor = '') { return '#page=' + encodeURIComponent(url) + (anchor ? '&anchor=' + encodeURIComponent(anchor) : ''); }
  /** Move focus to the active content after deliberate navigation. */
  function focusContent() { $('#content').focus({preventScroll: true}); $('#content').scrollIntoView({block: 'start'}); }
  /** Make a text-only link for outline and related navigation. */
  function link(label, href) { const element = document.createElement('a'); element.textContent = label; element.href = href; return element; }
  /** Populate the reader with the unmodified rendered article body and dedicated contents. */
  function renderArticle(page, anchor) {
    $('#article').innerHTML = page.html;
    $('#reader-category').textContent = page.category;
    $('#reader-category').href = route(pages.find(item => item.section === page.section && item.overview).url);
    $('#reader-topic').textContent = page.overview ? '' : '/ ' + page.title;
    $('#reader-context').textContent = page.section === 'code' ? 'Code references · module links open the live console' : 'Guide & reference';
    $('#live-link').href = data.baseUrl + (page.url === '/docs/code' ? '/docs/modules' : page.liveUrl);
    $('#code-link').hidden = !page.codeUrl;
    if (page.codeUrl) $('#code-link').href = route(page.codeUrl);
    $('#outline-links').replaceChildren();
    $('#related-links').replaceChildren();
    $('#article').querySelectorAll('.docs-section[id]').forEach(section => {
      const title = section.querySelector('.card-heading');
      if (title) $('#outline-links').append(link(title.textContent, route(page.url, section.id)));
    });
    const siblings = pages.filter(item => item.section === page.section);
    siblings.forEach(item => {
      const element = link(item.overview ? 'Overview' : item.title, route(item.url));
      if (item.url === page.url) element.setAttribute('aria-current', 'page');
      $('#related-links').append(element);
    });
    const index = siblings.indexOf(page);
    for (const [selector, item, prefix] of [['#previous', siblings[index - 1], '← '], ['#next', siblings[index + 1], 'Next: ']]) {
      const element = $(selector); element.hidden = !item;
      if (item) { element.textContent = prefix + item.title + (selector === '#next' ? ' →' : ''); element.href = route(item.url); }
    }
    // Source choices are links to the real code browser, rather than nonfunctional preview controls.
    $('#article').querySelectorAll('a[href^="?module="]').forEach(element => {
      element.href = data.baseUrl + page.liveUrl + element.getAttribute('href'); element.target = '_blank'; element.rel = 'noopener';
    });
    if (anchor) {
      const target = document.getElementById(anchor);
      if (target?.closest('#article')) {
        for (let parent = target.parentElement; parent && parent.id !== 'article'; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true;
        requestAnimationFrame(() => target.scrollIntoView({block: 'start'}));
      }
    }
  }
  /** Keep navigation state, page visibility and accessible current markers in sync. */
  function render(anchor = '') {
    const searching = !!query.trim() || !!scope;
    document.body.dataset.view = searching ? 'search' : current ? 'article' : 'home';
    $('#home-heading').hidden = !!current || searching;
    $('#home-content').hidden = !!current || searching;
    $('#reader').hidden = !current || searching;
    $('#reader-outline').hidden = !current || searching;
    $('#search-results').hidden = !searching;
    $('#clear-search').hidden = !searching;
    if (current && !searching) renderArticle(current, anchor);
    if (searching) search();
    document.querySelectorAll('.library-rail a, .docs-topnav a').forEach(element => {
      const active = element.getAttribute('href') === current?.url;
      if (active) { element.setAttribute('aria-current', 'page'); const group = element.closest('details'); if (group) group.open = true; }
      else element.removeAttribute('aria-current');
    });
    document.title = (current?.title || 'Documentation') + ' · ' + ({library: 'Search-first library', guided: 'Guided hub', reference: 'Reference desk'})[document.body.dataset.design];
  }
  /** Match all query words across full article text, ranking titles and summaries first. */
  function search() {
    const terms = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
    const results = indexed.filter(page => (!scope || page.section === scope) && terms.every(term => page.haystack.includes(term)))
      .map(page => ({page, score: terms.reduce((score, term) => score + (page.title.toLocaleLowerCase().includes(term) ? 8 : 0) + (page.summary.toLocaleLowerCase().includes(term) ? 3 : 0), 0)}))
      .sort((a, b) => b.score - a.score || a.page.title.localeCompare(b.page.title));
    $('#results-list').replaceChildren();
    $('#result-count').textContent = results.length + (results.length === 1 ? ' page' : ' pages');
    $('#search-empty').hidden = results.length > 0;
    for (const {page} of results) {
      const result = $('#search-result-template').content.cloneNode(true);
      result.querySelector('a').href = route(page.url);
      result.querySelector('.result-category').textContent = page.category + (page.overview ? ' / Overview' : '');
      result.querySelector('.result-title').textContent = page.title;
      result.querySelector('.result-summary').textContent = page.summary;
      const text = page.text, first = terms.length ? text.toLocaleLowerCase().indexOf(terms[0]) : 0;
      const start = Math.max(0, first - 65), excerpt = (start ? '…' : '') + text.slice(start, start + 225) + (text.length > start + 225 ? '…' : '');
      const target = result.querySelector('.result-excerpt');
      // Only text nodes and mark elements: search strings never become HTML.
      const escaped = terms.map(term => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
      if (!escaped.length) target.textContent = excerpt;
      else {
        const pattern = new RegExp('(' + escaped.join('|') + ')', 'gi');
        excerpt.split(pattern).forEach((part, index) => { const node = index % 2 ? document.createElement('mark') : document.createTextNode(part); if (index % 2) node.textContent = part; target.append(node); });
      }
      $('#results-list').append(result);
    }
  }
  /** Restore a bookmark or browser-history entry without fetching anything. */
  function locationChanged(focus = false) {
    const params = new URLSearchParams(location.hash.slice(1));
    const url = params.get('page');
    current = byUrl.get(data.aliases[url] || url) || null;
    query = ''; scope = ''; $('#query').value = ''; $('#search-scope').value = '';
    document.body.dataset.menu = ''; $('#menu').setAttribute('aria-expanded', 'false');
    render(params.get('anchor') || '');
    if (focus && !params.get('anchor')) focusContent();
  }
  /** Clear query and scope while retaining the article that was being read. */
  function clearSearch() { query = ''; scope = ''; $('#query').value = ''; $('#search-scope').value = ''; render(); $('#query').focus(); }
  document.addEventListener('click', async event => {
    const design = event.target.closest('button[data-design]');
    if (design) {
      document.body.dataset.design = design.dataset.design;
      document.querySelectorAll('button[data-design]').forEach(button => { const active = button === design; button.setAttribute('aria-pressed', String(active)); button.classList.toggle('active', active); });
      $('#design-note').textContent = notes[design.dataset.design]; $('#recommendation').hidden = design.dataset.design !== 'library'; render(); return;
    }
    const copy = event.target.closest('[data-copy-target]');
    if (copy) {
      try { await navigator.clipboard.writeText(document.getElementById(copy.dataset.copyTarget).textContent); copy.textContent = 'Copied'; }
      catch { copy.textContent = 'Select code to copy'; }
      return;
    }
    const element = event.target.closest('a');
    if (!element || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const href = element.getAttribute('href');
    if (href === '#home') { event.preventDefault(); if (location.hash === '#home') locationChanged(true); else location.hash = 'home'; return; }
    if (href?.startsWith('/docs')) {
      const [raw, anchor] = href.split('#'), url = data.aliases[raw] || raw;
      if (byUrl.has(url)) { event.preventDefault(); const next = route(url, anchor); if (location.hash === next) locationChanged(true); else location.hash = next; return; }
    }
    if (href?.startsWith('#') && !href.startsWith('#page=') && href !== '#content') {
      if (current && document.getElementById(href.slice(1))?.closest('#article')) { event.preventDefault(); location.hash = route(current.url, href.slice(1)); }
    } else if (href?.startsWith('/') && !href.startsWith('//')) {
      element.href = data.baseUrl + href; element.target = '_blank'; element.rel = 'noopener';
    }
  });
  $('#docs-search').addEventListener('submit', event => { event.preventDefault(); $('#results-list a')?.focus(); });
  $('#query').addEventListener('input', event => { query = event.target.value; render(); });
  $('#search-scope').addEventListener('change', event => { scope = event.target.value; render(); });
  $('#clear-search').addEventListener('click', clearSearch); $('#reset-search').addEventListener('click', clearSearch);
  $('#menu').addEventListener('click', () => { const open = document.body.dataset.menu !== 'open'; document.body.dataset.menu = open ? 'open' : ''; $('#menu').setAttribute('aria-expanded', String(open)); });
  $('#theme').addEventListener('click', () => { const dark = document.documentElement.dataset.bsTheme !== 'dark'; $('#preview-mode').value = dark ? 'dark' : 'light'; $('#preview-mode').dispatchEvent(new Event('change', {bubbles: true})); $('#theme').textContent = dark ? 'Light mode' : 'Dark mode'; });
  document.addEventListener('keydown', event => {
    if (event.key === '/' && !event.ctrlKey && !event.metaKey && !event.target.matches('input, textarea, select, [contenteditable]')) { event.preventDefault(); $('#query').focus(); }
    if (event.key === 'Escape') { if (query || scope) clearSearch(); if (document.body.dataset.menu === 'open') $('#menu').focus(); document.body.dataset.menu = ''; $('#menu').setAttribute('aria-expanded', 'false'); }
  });
  addEventListener('hashchange', () => locationChanged(true));
  $('#preview-mode').value = 'light'; $('#preview-mode').dispatchEvent(new Event('change', {bubbles: true}));
  locationChanged();
})();
