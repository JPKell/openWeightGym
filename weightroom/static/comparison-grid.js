/* Image comparison grid (comparison_parts.html), shared by Sampler comparison and the Image generation Compare view.
   Tiles come from the lines of an image job (/mirrorwall-api/jobs/<id>/lines). One zoom and one focus point apply to
   every image, so each shows the same region; the image size, Lock position, Selected only and a filter over the
   values the run varied act on the whole grid. */
(() => {
  'use strict';
  const NAMES = {seed: 'seed', steps: 'steps', cfg_scale: 'CFG', guidance: 'guidance', width: 'width', height: 'height'};
  const TITLES = {sampler: 'Samplers', scheduler: 'Schedulers', seed: 'Seeds', steps: 'Steps', cfg_scale: 'CFG scale', guidance: 'Guidance', width: 'Widths', height: 'Heights'};
  /** How one varied value reads in a caption: names stand alone, numbers carry their setting's name. */
  const part = (key, value) => NAMES[key] ? `${NAMES[key]} ${value}` : String(value);

  /** The settings a run varied, in sweep order, from its saved settings or, failing that, its lines. */
  function axes(settings, lines = []) {
    const sweep = Object.keys(settings?.sweep || {});
    if (sweep.length) return sweep;
    return Object.keys(NAMES).concat('sampler', 'scheduler').filter(key => new Set(lines.map(line => String(line[key] ?? ''))).size > 1);
  }

  /** The number of images an image job makes: its sweep's combinations times the images per combination. */
  const total = job => job?.request_counts?.total || (Number(job?.settings?.request?.n) || 1) *
    Object.values(job?.settings?.sweep || {}).reduce((all, values) => all * (Array.isArray(values) ? values.length : 1), 1);
  /** A saved image job of more than one image can be compared; a temporary (no-log) one keeps no lines. */
  const comparable = job => Boolean(job?.settings) && !String(job.id).startsWith('quiet_') && total(job) > 1;

  function create({prefix, store}) {
    const $ = id => document.getElementById(prefix + '-' + id);
    const grid = $('grid');
    let tiles = new Map(), keys = [], caption = [], zoom = 1, focus = {x: 50, y: 50};
    const template = name => document.getElementById(`${prefix}-${name}-template`).content.firstElementChild.cloneNode(true);

    // The image size is a per-browser convenience; the grid works the same without storage.
    function keep(key, value) { try { localStorage.setItem(store + ':' + key, JSON.stringify(value)); } catch {} }
    function kept(key) { try { return JSON.parse(localStorage.getItem(store + ':' + key)); } catch { return null; } }

    /** Start an empty grid for a run varying `varied` (filter groups, in that order); captions name `labels` in theirs. */
    function reset(varied = [], labels = varied) {
      tiles = new Map(); keys = [...varied]; caption = [...labels];
      grid.replaceChildren();
      $('filter-groups').replaceChildren(...keys.map(key => {
        const group = template('filter-group'); group.dataset.axis = key;
        group.querySelector('[data-title]').textContent = (TITLES[key] || key) + ' in this run';
        group.querySelector('[data-choices]').id = `${prefix}-filter-${key}s`;
        group.querySelector('[data-cg-filter-all]').dataset.cgFilterAll = key;
        group.querySelector('[data-cg-filter-none]').dataset.cgFilterNone = key;
        return group;
      }));
      $('filter-toggle').disabled = true; showFilter(false); $('filter-count').textContent = '';
      filter();
    }
    function offer(key, value) {
      const list = $(`filter-${key}s`);
      if (!list || [...list.querySelectorAll('input')].some(input => input.value === value)) return;
      const choice = template('filter'), input = choice.querySelector('input');
      input.name = `${prefix}-filter-${key}`; input.value = value;
      choice.querySelector('.form-check-label').textContent = part(key, value);
      list.append(choice);
      $('filter-toggle').disabled = false;
    }
    /** Draw one job line: its tile is made once, then its state, progress and image follow the line. */
    function line(row, {active = false, progress = null} = {}) {
      let tile = tiles.get(row.line);
      const label = caption.map(key => part(key, row[key] ?? '—')).join(' · ') || `image ${row.line + 1}`;
      if (!tile) {
        tile = template('tile'); tile.dataset.line = row.line;
        tile.values = Object.fromEntries(keys.map(key => [key, String(row[key] ?? '')]));
        keys.forEach(key => offer(key, tile.values[key]));
        const text = tile.querySelector('[data-caption]');
        caption.forEach((key, index) => {
          const one = document.createElement(index ? 'span' : 'strong'); one.dataset.axis = key; one.textContent = part(key, row[key] ?? '—');
          if (index) text.append(' · ');
          text.append(one);
        });
        if (!caption.length) text.textContent = label;
        tile.querySelector('[data-pick]').setAttribute('aria-label', 'Select ' + label);
        grid.append(tile); tiles.set(row.line, tile);
      }
      const state = row.state === 'waiting' && active ? 'running' : row.state;
      tile.dataset.state = state;
      const frame = tile.querySelector('.cg-frame'), image = tile.querySelector('img'), placeholder = tile.querySelector('[data-placeholder]');
      frame.setAttribute('aria-label', label + (state === 'failed' ? ', failed' : state === 'deleted' ? ', image deleted' : state === 'done' ? '' : ', not finished'));
      tile.querySelector('[data-detail]').textContent = [
        !caption.includes('seed') && `seed ${row.seed ?? '—'}`, !caption.includes('steps') && row.steps != null && `${row.steps} steps`,
        !caption.includes('cfg_scale') && row.cfg_scale != null && `CFG ${row.cfg_scale}`,
        !caption.includes('guidance') && row.guidance != null && `guidance ${row.guidance}`].filter(Boolean).join(' · ');
      if (row.state === 'done' && image.dataset.asset !== row.asset_id) {
        image.dataset.asset = row.asset_id;
        image.dataset.thumbnail = '/mirrorwall-api/assets/' + encodeURIComponent(row.asset_id) + '/thumbnail';
        image.dataset.full = '/mirrorwall-api/assets/' + encodeURIComponent(row.asset_id) + '/content';
        image.src = zoom > 1 ? image.dataset.full : image.dataset.thumbnail;
        image.alt = label + (caption.includes('seed') ? '' : `, seed ${row.seed}`);
        image.hidden = false; placeholder.hidden = true;
        if (row.width && row.height) grid.style.setProperty('--cg-aspect', row.width + ' / ' + row.height);
      } else if (row.state !== 'done') {
        // An image shown before it was deleted goes too, rather than staying behind the placeholder.
        image.hidden = true; image.removeAttribute('src'); delete image.dataset.asset;
        placeholder.hidden = false;
        placeholder.textContent = state === 'failed' ? (row.error || 'Failed') : state === 'deleted' ? 'Image deleted' : state === 'running' ? (progress?.stage === 'queued' ? 'Waiting for the GPU' : 'Generating' + (progress?.step ? ` · step ${progress.step}` : '')) : 'Waiting';
      }
      filter();
    }

    function setZoom(value) {
      zoom = Math.min(8, Math.max(1, value));
      $('zoom').value = zoom; $('zoom-value').textContent = zoom + '×';
      grid.style.setProperty('--cg-zoom', zoom);
      for (const image of grid.querySelectorAll('img[data-full]')) {  // full resolution only when zoomed in
        const wanted = zoom > 1 ? image.dataset.full : image.dataset.thumbnail;
        if (image.getAttribute('src') !== wanted) image.src = wanted;
      }
    }
    function setFocus(x, y) {
      focus = {x: Math.min(100, Math.max(0, x)), y: Math.min(100, Math.max(0, y))};
      grid.style.setProperty('--cg-x', focus.x + '%'); grid.style.setProperty('--cg-y', focus.y + '%');
    }
    /** Image size: the smallest tile width; the grid fits as many per row as it can. */
    function setSize(pixels) {
      $('size').value = pixels; $('size-value').textContent = pixels + ' px';
      grid.style.setProperty('--cg-tile', pixels + 'px');
      keep('size', pixels);
    }
    function showFilter(open) {
      $('filter').hidden = !open;
      $('filter-toggle').setAttribute('aria-expanded', String(open));
      $('filter-toggle').classList.toggle('active', open);
    }
    function filter() {
      const only = $('selected-only').checked;
      const shown = Object.fromEntries(keys.map(key => [key, new Set([...($(`filter-${key}s`)?.querySelectorAll('input:checked') || [])].map(input => input.value))]));
      let visible = 0;
      for (const tile of tiles.values()) {
        tile.hidden = (only && !tile.hasAttribute('data-picked')) || keys.some(key => !shown[key].has(tile.values[key]));
        if (!tile.hidden) visible++;
      }
      $('filter-count').textContent = tiles.size ? `Showing ${visible} of ${tiles.size} image${tiles.size === 1 ? '' : 's'}` : '';
      $('filter-toggle').textContent = visible < tiles.size && !only ? `Filter · ${visible}/${tiles.size}` : 'Filter';
    }

    $('zoom').addEventListener('input', () => setZoom(Number($('zoom').value)));
    $('size').addEventListener('input', () => setSize(Number($('size').value)));
    $('selected-only').addEventListener('change', filter);
    $('filter-toggle').addEventListener('click', () => showFilter($('filter').hidden));
    $('filter').addEventListener('change', filter);
    $('filter-reset').addEventListener('click', () => { $('filter').querySelectorAll('input').forEach(input => { input.checked = true; }); filter(); });
    $('filter').addEventListener('click', event => {
      const button = event.target.closest('[data-cg-filter-all],[data-cg-filter-none]'); if (!button) return;
      const key = button.dataset.cgFilterAll || button.dataset.cgFilterNone;
      $(`filter-${key}s`).querySelectorAll('input').forEach(input => { input.checked = 'cgFilterAll' in button.dataset; });
      filter();
    });
    // The pointer moves the shared focus unless Lock position is on; then only a click (or the arrow keys) moves it.
    const point = event => {
      const frame = event.target.closest('.cg-frame'); if (!frame || zoom <= 1) return;
      const box = frame.getBoundingClientRect();
      setFocus((event.clientX - box.left) / box.width * 100, (event.clientY - box.top) / box.height * 100);
    };
    grid.addEventListener('pointermove', event => { if (!$('lock').checked) point(event); });
    grid.addEventListener('click', event => { if ($('lock').checked) point(event); });
    $('lock').addEventListener('change', () => { grid.dataset.locked = String($('lock').checked); });
    grid.addEventListener('wheel', event => {
      if (!event.target.closest('.cg-frame') || !event.ctrlKey) return;  // Ctrl + wheel zooms; plain scrolling still scrolls
      event.preventDefault(); setZoom(zoom + (event.deltaY < 0 ? .5 : -.5));
    }, {passive: false});
    grid.addEventListener('keydown', event => {
      if (!event.target.classList.contains('cg-frame')) return;
      const moves = {ArrowLeft: [-5, 0], ArrowRight: [5, 0], ArrowUp: [0, -5], ArrowDown: [0, 5]};
      if (moves[event.key]) { event.preventDefault(); if (zoom === 1) setZoom(2); setFocus(focus.x + moves[event.key][0], focus.y + moves[event.key][1]); }
      else if (event.key === '+' || event.key === '=') { event.preventDefault(); setZoom(zoom + .5); }
      else if (event.key === '-') { event.preventDefault(); setZoom(zoom - .5); }
      else if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); event.target.closest('.cg-tile').querySelector('[data-pick]').click(); }
    });
    grid.addEventListener('change', event => {
      if (!event.target.matches('[data-pick]')) return;
      event.target.closest('.cg-tile').toggleAttribute('data-picked', event.target.checked);
      filter();
    });
    const size = Number(kept('size'));
    setSize(size >= 80 && size <= 800 ? size : 180);
    reset();
    return {reset, line, keep, kept, get size() { return tiles.size; }};
  }

  /** "3 of 4 done · 1 failed · 2 deleted": a line whose image was deleted after it finished is not waiting (QA-076). */
  function summary(lines) {
    const count = state => lines.filter(line => line.state === state).length, failed = count('failed'), deleted = count('deleted');
    return `${count('done')} of ${lines.length} done` + (failed ? ` · ${failed} failed` : '') + (deleted ? ` · ${deleted} deleted` : '');
  }

  window.SuiteComparisonGrid = {create, axes, part, total, comparable, summary};
})();
