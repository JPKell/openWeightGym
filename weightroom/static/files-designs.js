// Interactive proposals only: no requests, filesystem access, or persistent changes.
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const places = [
    {id: 'storage', name: 'Storage', role: 'Complete library', path: '/srv/models', capacity: 4000, other: 1240},
    {id: 'cache', name: 'Cache', role: 'Fast working copies', path: '/nvme/cache', capacity: 1000, other: 310},
    {id: 'atlas', name: 'Atlas', role: 'Compute · online', path: '/atlas/models', capacity: 2000, other: 950},
    {id: 'nova', name: 'Nova', role: 'Compute · online', path: '/nova/models', capacity: 1000, other: 640},
  ];
  const samples = [
    ['qwen3-32b-q6_k.gguf', 'Language · Q6_K', 26.4, ['cache', 'atlas'], ['atlas']],
    ['deepseek-r1-32b-q4.gguf', 'Reasoning · Q4_K_M', 18.5, ['cache', 'nova'], ['nova']],
    ['flux-dev-q8.gguf', 'Image · Q8_0', 12.6, ['atlas'], ['atlas']],
    ['llama-3.1-8b-q8.gguf', 'Language · Q8_0', 8.5, ['cache', 'atlas', 'nova'], []],
    ['mistral-7b-q4.gguf', 'Language · Q4_K_M', 4.4, ['cache', 'atlas'], []],
    ['whisper-large-v3.bin', 'Speech · FP16', 3.1, ['nova'], ['nova']],
    ['nomic-embed-v2.gguf', 'Embedding · F16', 0.6, ['cache', 'atlas'], ['atlas']],
    ['sdxl-base-1.0.safetensors', 'Image · FP16', 6.9, ['cache', 'nova'], []],
  ];
  const descriptions = {
    ledger: 'Recommended · A calm location overview. Open a row to manage its files in the same window.',
    matrix: 'For a whole-library view · See every file and every copy side by side.',
    desk: 'For frequent transfers · Keep locations, destination, and file selection together.',
  };
  let files, selected = new Set(), source = 'storage', layout = 'matrix', shared = false, browsing = false;
  const name = id => shared && id === 'storage' ? 'Storage + cache' : places.find(p => p.id === id).name;
  const amount = n => n >= 1000 ? `${(n / 1000).toFixed(2)} TB` : `${n.toFixed(1)} GB`;
  const activePlaces = () => places.filter(p => !shared || p.id !== 'cache');
  const present = (file, id) => file.copies.has(shared && id === 'cache' ? 'storage' : id);
  const used = (file, id) => id === 'storage' || id === 'cache' ? file.used.length > 0 : file.used.includes(id);
  const sum = rows => rows.reduce((total, file) => total + file.size, 0);
  const at = id => files.filter(file => present(file, id));
  const reclaimable = id => id === 'storage' || (id === 'cache' && shared) ? [] : at(id).filter(file => !used(file, id));
  const free = id => {
    const place = places.find(p => p.id === id);
    return place.capacity - place.other - sum(at(id));
  };
  const message = text => { $('#status').textContent = text; };
  function options(select, selectedValue) {
    select.replaceChildren(...activePlaces().map(place => new Option(name(place.id), place.id)));
    select.value = selectedValue;
  }
  function locationRows() {
    $('#locations').replaceChildren();
    activePlaces().forEach(place => {
      const row = $('#location-row').content.firstElementChild.cloneNode(true);
      row.dataset.active = String(browsing && source === place.id);
      const button = row.querySelector('[data-open]');
      button.textContent = name(place.id);
      button.dataset.open = place.id;
      button.setAttribute('aria-label', `Browse ${name(place.id)} files`);
      row.querySelector('[data-role]').textContent = place.role;
      row.querySelector('[data-count]').textContent = at(place.id).length;
      row.querySelector('[data-used]').textContent = `${amount(place.capacity - free(place.id))} / ${amount(place.capacity)}`;
      const percent = Math.round(100 * (place.capacity - free(place.id)) / place.capacity);
      const progress = row.querySelector('[role="progressbar"]');
      progress.setAttribute('aria-label', `${name(place.id)} drive usage`);
      progress.setAttribute('aria-valuenow', percent);
      progress.firstElementChild.style.width = `${percent}%`;
      row.querySelector('[data-free]').textContent = amount(free(place.id));
      row.querySelector('[data-reclaim]').textContent = place.id === 'storage' ? '—' : amount(sum(reclaimable(place.id)));
      const clean = row.querySelector('[data-clean]');
      clean.dataset.clean = place.id;
      clean.disabled = !reclaimable(place.id).length;
      clean.setAttribute('aria-label', `Clean unused ${name(place.id)} copies`);
      if (place.id === 'storage') {
        clean.hidden = !shared;
        clean.textContent = 'Cache cleanup disabled';
        clean.title = 'Storage and cache share a directory. The library is protected.';
        row.querySelector('[data-protected]').hidden = shared;
      }
      $('#locations').append(row);
    });
    const cleanupSize = activePlaces().reduce((n, p) => n + sum(reclaimable(p.id)), 0);
    $('#cleanup-amount').textContent = amount(cleanupSize);
    $('#cleanup').disabled = cleanupSize === 0;
    $('#library-total').textContent = `${files.length} original files · ${amount(sum(files))} in storage`;
  }
  function visibleFiles() {
    const query = $('#search').value.toLowerCase();
    return at(source).filter(file => file.name.toLowerCase().includes(query))
      .filter(file => $('#usage').value === 'all' || used(file, source) === ($('#usage').value === 'used'));
  }
  function fileRows() {
    const rows = visibleFiles();
    $('#file-rows').replaceChildren();
    rows.forEach(file => {
      const row = $('#file-row').content.firstElementChild.cloneNode(true);
      const box = row.querySelector('[data-select]');
      box.dataset.select = file.id;
      box.checked = selected.has(file.id);
      box.setAttribute('aria-label', `Select ${file.name}`);
      row.querySelector('[data-name]').textContent = file.name;
      row.querySelector('[data-kind]').textContent = file.kind;
      row.querySelector('[data-size]').textContent = amount(file.size);
      const usage = row.querySelector('[data-usage]');
      usage.textContent = used(file, source) ? 'In use' : 'Unused';
      usage.classList.add(used(file, source) ? 'bg-green-lt' : 'bg-secondary-lt');
      row.querySelector('[data-copies]').textContent = activePlaces().filter(p => p.id !== 'storage' && present(file, p.id)).map(p => name(p.id)).join(' · ') || 'Storage only';
      row.querySelectorAll('[data-presence]').forEach(cell => {
        const id = cell.dataset.presence;
        cell.textContent = shared && id === 'cache' ? 'Shared' : present(file, id) ? '●' : '—';
        cell.classList.add(present(file, id) ? 'text-success' : 'text-secondary');
        cell.setAttribute('aria-label', `${name(id)}: ${present(file, id) ? 'present' : 'absent'}`);
        cell.title = shared && id === 'cache' ? 'Same directory as storage' : present(file, id) ? 'Verified copy' : 'No copy';
      });
      $('#file-rows').append(row);
    });
    if (!rows.length) $('#file-rows').append($('#empty-files').content.cloneNode(true));
    $('#file-count').textContent = `${rows.length} of ${at(source).length} files`;
    $('#select-all').checked = rows.length > 0 && rows.every(file => selected.has(file.id));
    $('#select-all').indeterminate = rows.some(file => selected.has(file.id)) && !$('#select-all').checked;
    $('#select-all').disabled = rows.length === 0;
    selection();
  }
  function selection() {
    const chosen = files.filter(file => selected.has(file.id));
    const destination = $('#destination').value;
    const moveOption = $('#operation option[value="move"]');
    moveOption.disabled = source === 'storage';
    if (source === 'storage') $('#operation').value = 'copy';
    const move = $('#operation').value === 'move';
    const protectedFiles = move && chosen.some(file => used(file, source));
    const required = sum(chosen.filter(file => !present(file, destination)));
    const noRoom = required > free(destination);
    $('#selection').textContent = chosen.length ? `${chosen.length} selected · ${amount(sum(chosen))}` : 'Select files to transfer';
    $('#clear-selection').disabled = chosen.length === 0;
    $('#transfer').disabled = !chosen.length || source === destination || protectedFiles || noRoom;
    $('#transfer').textContent = `${move ? 'Move' : 'Copy'} selected${chosen.length ? ` (${chosen.length})` : ''}`;
    $('#transfer-note').textContent = source === destination ? 'Choose a different destination.'
      : protectedFiles ? 'Some selected files are in use here. Copy them, or select only unused files to move.'
      : noRoom ? `Not enough room at ${name(destination)}. ${amount(required)} required; ${amount(free(destination))} available.`
      : source === 'storage' ? 'Storage keeps every original. Transfers from storage create working copies.'
      : move ? 'Copies are verified before unused source copies are removed. Storage always stays.'
      : 'The source and original storage copies both stay.';
  }
  function render() {
    $('#workspace').dataset.layout = layout;
    $('#design-description').textContent = descriptions[layout];
    document.querySelectorAll('[data-design]').forEach(button => {
      button.classList.toggle('active', button.dataset.design === layout);
      button.setAttribute('aria-pressed', String(button.dataset.design === layout));
    });
    $('#locations-panel').hidden = layout === 'ledger' && browsing;
    $('#browser-panel').hidden = !browsing && layout === 'ledger';
    $('#overview').hidden = layout !== 'ledger' || !browsing;
    $('#back').hidden = layout !== 'ledger';
    $('#workspace-title').textContent = {ledger: browsing ? name(source) : 'Your locations', matrix: 'Library & distribution', desk: 'Transfer workspace'}[layout];
    $('#workspace-subtitle').textContent = `${activePlaces().length} locations · ${files.length} original files`;
    $('#browser-title').textContent = `${name(source)} files`;
    $('#browser-path').textContent = places.find(p => p.id === source).path;
    $('#shared-note').hidden = !shared;
    locationRows();
    fileRows();
  }
  function changeSource(id) {
    source = id;
    browsing = true;
    selected.clear();
    $('#search').value = '';
    $('#usage').value = 'all';
    $('#source').value = source;
    if ($('#destination').value === source) $('#destination').value = activePlaces().find(p => p.id !== source).id;
    render();
  }
  function reset() {
    files = samples.map(([name, kind, size, copies, used], id) => ({id: String(id), name, kind, size, copies: new Set(['storage', ...copies]), used}));
    shared = $('#shared').checked;
    selected.clear();
    source = 'storage';
    browsing = layout !== 'ledger';
    $('#search').value = '';
    $('#usage').value = 'all';
    options($('#source'), source);
    options($('#destination'), shared ? 'atlas' : 'cache');
    message('');
    render();
  }
  function clean(ids) {
    let count = 0, size = 0;
    ids.forEach(id => reclaimable(id).forEach(file => {
      file.copies.delete(id);
      count++;
      size += file.size;
    }));
    selected.clear();
    message(`Demo cleanup complete: ${count} unused copies removed, ${amount(size)} freed. All ${files.length} storage originals and all in-use copies were kept.`);
    render();
  }
  document.addEventListener('click', event => {
    const design = event.target.closest('[data-design]');
    if (design) {
      layout = design.dataset.design;
      browsing = layout !== 'ledger';
      history.replaceState(null, '', `#${layout}`);
      render();
    }
    const open = event.target.closest('[data-open]');
    if (open) changeSource(open.dataset.open);
    const cleanButton = event.target.closest('[data-clean]');
    if (cleanButton && !cleanButton.disabled) clean([cleanButton.dataset.clean]);
  });
  $('#source').addEventListener('change', event => changeSource(event.target.value));
  $('#destination').addEventListener('change', selection);
  $('#operation').addEventListener('change', selection);
  $('#search').addEventListener('input', () => { selected.clear(); fileRows(); });
  $('#usage').addEventListener('change', () => { selected.clear(); fileRows(); });
  $('#select-all').addEventListener('change', event => {
    visibleFiles().forEach(file => event.target.checked ? selected.add(file.id) : selected.delete(file.id));
    fileRows();
  });
  $('#file-rows').addEventListener('change', event => {
    const id = event.target.dataset.select;
    if (id !== undefined) {
      event.target.checked ? selected.add(id) : selected.delete(id);
      fileRows();
      $(`[data-select="${id}"]`).focus();
    }
  });
  $('#clear-selection').addEventListener('click', () => { selected.clear(); fileRows(); });
  $('#transfer').addEventListener('click', () => {
    selection();
    if ($('#transfer').disabled) return;
    const destination = $('#destination').value, move = $('#operation').value === 'move';
    let count = 0;
    files.filter(file => selected.has(file.id)).forEach(file => {
      if (!present(file, destination)) { file.copies.add(destination); count++; }
      if (move) file.copies.delete(source);
    });
    message(`Demo transfer complete: ${selected.size} files ${move ? 'moved' : 'processed'} from ${name(source)} to ${name(destination)} (${count} new copies). Storage originals kept.`);
    selected.clear();
    render();
  });
  const overview = () => { browsing = false; selected.clear(); render(); };
  $('#back').addEventListener('click', overview);
  $('#overview').addEventListener('click', overview);
  $('#cleanup').addEventListener('click', () => clean(activePlaces().map(p => p.id)));
  $('#shared').addEventListener('change', reset);
  $('#reset').addEventListener('click', reset);
  function appearance(mode) {
    const control = $('[data-theme-form] [name="mode"]');
    control.value = mode;
    control.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = mode === 'dark' ? 'Light mode' : 'Dark mode';
  }
  $('#theme').addEventListener('click', () => appearance(document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark'));
  if (Object.hasOwn(descriptions, location.hash.slice(1))) layout = location.hash.slice(1);
  appearance('light');
  reset();
})();
