(() => {
  'use strict';
  const $ = name => document.getElementById('mw-' + name);
  const el = (tag, text, classes) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (classes) node.className = classes;
    return node;
  };
  const message = error => { $('message').textContent = error || ''; $('message').hidden = !error; };
  async function api(path, body) {
    const answer = await fetch(path, {method: body ? 'POST' : 'GET', headers: body ? {'content-type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined, cache: 'no-store'});
    if (answer.redirected) { location.href = '/sign-in'; throw new Error('Please sign in again.'); }
    const value = await answer.json();
    if (!answer.ok) throw new Error(value.error?.message || 'Discovery is unavailable. Please retry.');
    return value;
  }
  const safe = callback => async event => {
    try { message(''); await callback(event); } catch (error) { message(error.message); }
  };
  let pages = [], current = 0, revision = 0, busy = false, query = '';
  const previous = $('paging').querySelector('a[href="#previous"]');
  const next = $('paging').querySelector('a[href="#next"]');
  function controls() {
    previous.hidden = current === 0;
    next.hidden = !pages[current] || !(current < pages.length - 1 || pages[current].next);
    for (const link of [previous, next]) {
      link.setAttribute('aria-disabled', String(busy));
      link.tabIndex = busy ? -1 : 0;
    }
    $('found').setAttribute('aria-busy', String(busy));
    $('search').querySelector('button').disabled = busy;
  }
  function empty(title, description) {
    $('empty').hidden = false;
    $('empty').querySelector('[data-empty-title]').textContent = title;
    $('empty').querySelector('[data-empty-description]').textContent = description;
  }
  async function refreshPreferences() {
    const value = await api('/mirrorwall-api/preferences');
    if ($('adult')) {
      $('adult-control').hidden = !value.adult_previews_allowed;
      $('adult').checked = Boolean(value.adult_previews);
    }
  }
  if ($('adult')) $('adult').onchange = safe(async () => {
    ++revision; pages = []; current = 0; busy = false; controls();
    $('found').replaceChildren(); $('paging').hidden = true;
    empty('Preview preference changed', 'Search again to refresh model previews.');
    try { await api('/mirrorwall-api/preferences', {adult_previews: $('adult').checked}); }
    finally { await refreshPreferences(); }
    $('status').textContent = 'Preview preference saved. Search to refresh results.';
  });
  function renderResults() {
    const page = pages[current], rows = page?.rows || [];
    $('found').replaceChildren(); $('empty').hidden = Boolean(rows.length);
    if (!rows.length) empty('No matching model versions', 'Try another search or broaden the model type and base model filters.');
    $('status').textContent = rows.length + ' model version' + (rows.length === 1 ? '' : 's') + ' on this page';
    $('paging').hidden = pages.length === 0;
    $('page-label').textContent = 'Page ' + (current + 1) + ' · Up to 12 versions per page';
    rows.forEach(one => {
      const column = el('div', undefined, 'col-sm-6 col-xl-4');
      const card = $('discovery-card').content.firstElementChild.cloneNode(true);
      card.querySelector('.card-heading').textContent = one.name || 'Unnamed model version';
      const placeholder = $('discovery-card').content.querySelector('[data-model-placeholder]').cloneNode(true);
      card.prepend(placeholder);
      if (one.preview) {
        const image = el('img'); image.className = 'discovery-model-media'; image.src = one.preview;
        image.alt = one.name; image.referrerPolicy = 'no-referrer'; image.loading = 'lazy';
        image.onerror = () => image.replaceWith(placeholder);
        placeholder.replaceWith(image);
      }
      card.querySelector('[data-model-meta]').textContent = (one.kind || 'Type unavailable') + ' · ' + (one.base_model || 'Base model unavailable');
      if (one.trigger_words?.length) {
        const triggers = card.querySelector('[data-model-triggers]'); triggers.hidden = false;
        triggers.textContent = 'Trigger words: ' + one.trigger_words.join(', ');
      }
      const source = card.querySelector('[data-model-source]');
      // Provider metadata is untrusted; links must remain external HTTPS source links.
      try { const url = new URL(one.url); if (url.protocol !== 'https:') throw new Error(); source.href = url.href; }
      catch { source.hidden = true; }
      const details = card.querySelector('[data-model-choose]'), files = card.querySelector('[data-model-files]');
      details.onclick = safe(async () => {
        if (!files.hidden) { files.hidden = true; details.setAttribute('aria-expanded', 'false'); return; }
        details.disabled = true; details.textContent = 'Loading…';
        try {
          const value = await api('/mirrorwall-discovery?repository=' + encodeURIComponent(one.repository));
          const choices = value.files.filter(file => !$('format').value || file.file.toLowerCase().endsWith($('format').value));
          files.replaceChildren(); files.hidden = false; details.setAttribute('aria-expanded', 'true');
          if (!choices.length) { files.append(el('p', 'No supported file with a SHA-256 checksum is available for this format.', 'small text-secondary mb-0')); return; }
          const place = el('select', undefined, 'form-select form-select-sm mt-1');
          value.places.filter(row => ['storage_image', 'storage_encoders'].includes(row.name)).forEach(row =>
            place.add(new Option(({storage_image: 'Image storage', storage_encoders: 'Encoder storage'})[row.name], row.name)));
          place.setAttribute('aria-label', 'Download destination');
          const label = el('label', 'Download destination', 'form-label small w-100'); label.append(place);
          files.append(label, el('p', place.options.length ? 'Downloads go into storage, never the cache. Files are verified before use.' : 'Downloads go into model storage, never the cache. Set a model storage folder in Settings → Model file locations to download files.', 'small text-secondary'));
          choices.forEach(file => {
            const button = el('button', file.file + ' · ' + (file.size / 2**30).toFixed(2) + ' GiB', 'btn btn-outline-secondary btn-sm w-100 mb-2 text-break');
            button.type = 'button'; button.disabled = !place.options.length;
            button.onclick = safe(async () => {
              button.disabled = true;
              try {
                await api('/mirrorwall-discovery', {repository: one.repository, files: [file.file], place: place.value});
                $('success').textContent = 'Download started. Progress appears below and on Files.'; $('success').hidden = false;
              } finally { button.disabled = !place.options.length; }
            });
            files.append(button);
          });
        } finally { details.disabled = false; details.textContent = 'Choose file'; }
      });
      column.append(card); $('found').append(column);
    });
    controls();
  }
  async function load(cursor = '', reset = false) {
    const ticket = ++revision;
    busy = true; controls(); $('status').textContent = 'Searching Civitai…';
    if (reset) { $('found').replaceChildren(); $('paging').hidden = true; empty('Searching Civitai…', 'Finding model versions that match your search.'); }
    try {
      await refreshPreferences();
      if (ticket !== revision) return;
      const params = new URLSearchParams({q: query, paged: '1', cursor, kind: $('type').value, base: $('base').value});
      const value = await api('/mirrorwall-discovery?' + params);
      if (ticket !== revision) return;
      if (!Array.isArray(value.found) || value.found.some(one => !one || typeof one.name !== 'string' || typeof one.repository !== 'string' || !one.repository.startsWith('civitai/'))) {
        throw new Error('Civitai returned incomplete model data. Please retry the search.');
      }
      for (const [name, key] of [['type', 'kind'], ['base', 'base_model']]) {
        for (const option of new Set(value.found.map(row => row[key]).filter(Boolean))) {
          if (![...$(name).options].some(row => row.value === option)) $(name).add(new Option(option, option));
        }
      }
      // A provider page can contain several versions per model. Keep each visible page bounded.
      const rows = value.found.filter(one => (!$('type').value || one.kind === $('type').value) && (!$('base').value || one.base_model === $('base').value));
      const added = [];
      for (let start = 0; start < rows.length; start += 12) added.push({rows: rows.slice(start, start + 12), next: ''});
      if (!added.length) added.push({rows: [], next: ''});
      added.at(-1).next = value.next_cursor && value.next_cursor !== cursor ? value.next_cursor : '';
      current = reset ? 0 : pages.length;
      pages = reset ? added : [...pages, ...added];
      renderResults();
    } catch (error) {
      if (ticket !== revision) return;
      if (reset) { pages = []; current = 0; empty('Search could not be completed', 'Please retry your search. Your filters are ready.'); }
      $('status').textContent = 'Search failed. Please retry.';
      throw error;
    } finally { if (ticket === revision) { busy = false; controls(); } }
  }
  previous.onclick = safe(async event => {
    event.preventDefault(); if (busy || current === 0) return;
    current--; renderResults(); $('status').scrollIntoView({block: 'nearest'});
  });
  next.onclick = safe(async event => {
    event.preventDefault(); if (busy) return;
    if (current < pages.length - 1) { current++; renderResults(); }
    else if (pages[current]?.next) await load(pages[current].next);
    $('status').scrollIntoView({block: 'nearest'});
  });
  for (const name of ['type', 'base']) $(name).onchange = safe(async () => { if (query) await load('', true); });
  $('format').onchange = () => { if (pages.length) renderResults(); };
  $('search').onsubmit = safe(async event => {
    event.preventDefault(); query = $('q').value.trim(); if (!query) return;
    await load('', true);
  });
  controls(); refreshPreferences().catch(error => message(error.message));
})();
