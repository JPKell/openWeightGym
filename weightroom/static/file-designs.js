// Standalone review: sample operations never call the application or alter files.
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const descriptions = {
    dossier: 'Recommended · A complete file at a glance, with metadata and actions side by side.',
    inspector: 'Metadata first · A technical inspector with the file record and operations in compact side rails.',
    placement: 'Distribution first · Compare node copies and available space, then copy or move without leaving the page.',
  };
  const initialMetadata = $('#metadata-available').innerHTML;
  let scenario = 'model', busy = false, timer, copies, filename, path, removed = false;
  const notice = text => { $('#action-status').textContent = text; };
  const initialNodes = () => [
    {id: 'atlas', name: 'Atlas', role: 'Compute · online', free: 412, present: true},
    {id: 'nova', name: 'Nova', role: 'Compute · online', free: 186, present: false},
    {id: 'orion', name: 'Orion', role: 'Compute · offline', free: null, present: false},
  ];
  function selectedNodes() { return $$('[data-node-select]:checked').filter(e => !e.disabled); }
  function updateSelection() {
    const count = selectedNodes().length;
    $('#destination-count').textContent = `${count} node${count === 1 ? '' : 's'} selected`;
    $('#distribute').disabled = !count || busy || removed;
  }
  function renderNodes() {
    $('#destinations').replaceChildren();
    copies.forEach(node => {
      const row = $('#placement-row').content.firstElementChild.cloneNode(true);
      row.querySelector('[data-node-name]').textContent = node.name;
      row.querySelector('[data-node-role]').textContent = node.role;
      const state = row.querySelector('[data-copy-state]');
      state.textContent = node.present ? 'Present' : node.free === null ? 'Unknown' : 'Missing';
      state.classList.add(node.present ? 'bg-green-lt' : 'bg-secondary-lt');
      row.querySelector('[data-free]').textContent = node.free === null ? 'Unknown' : `${node.free} GiB`;
      const input = row.querySelector('[data-node-select]');
      input.value = node.id;
      input.disabled = node.present || node.free === null || busy || removed;
      input.checked = !input.disabled;
      input.setAttribute('aria-label', `Distribute to ${node.name}`);
      $('#destinations').append(row);
    });
    updateSelection();
  }
  function setupMetadata() {
    $('#metadata-available').innerHTML = initialMetadata;
    const details = $('#file-metadata');
    details.open = true;
    details.querySelector('.table-responsive').classList.add('metadata-scroll');
    details.insertBefore($('#metadata-tools').content.cloneNode(true), details.querySelector('.table-responsive'));
    $('#metadata tbody').append($('#metadata-empty').content.cloneNode(true));
    // Keyboard-operable sorting complements the existing sortable table's visual convention.
    $$('#metadata th').forEach((th, index) => {
      th.replaceChildren(Object.assign(document.createElement('button'), {
        type: 'button', className: 'btn btn-link btn-sm p-0', textContent: `${index ? 'Value' : 'Name'} ↕`,
      }));
      th.querySelector('button').addEventListener('click', () => {
        const ascending = th.getAttribute('aria-sort') !== 'ascending';
        $$('#metadata th').forEach(e => e.removeAttribute('aria-sort'));
        th.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
        const rows = $$('#metadata tbody tr:not(#no-metadata)');
        rows.sort((a, b) => a.cells[index].textContent.localeCompare(b.cells[index].textContent, undefined, {numeric: true}) * (ascending ? 1 : -1));
        rows.forEach(row => $('#metadata tbody').insertBefore(row, $('#no-metadata')));
      });
    });
    $('#metadata-search').addEventListener('input', filterMetadata);
    $('#metadata-group').addEventListener('change', filterMetadata);
  }
  function filterMetadata() {
    const query = $('#metadata-search').value.toLowerCase().trim(), group = $('#metadata-group').value;
    const rows = $$('#metadata tbody tr:not(#no-metadata)');
    let count = 0;
    rows.forEach(row => {
      row.hidden = !row.textContent.toLowerCase().includes(query) || (group !== 'all' && !row.cells[0].textContent.startsWith(group));
      if (!row.hidden) count++;
    });
    $('#no-metadata').hidden = count !== 0;
    $('#metadata-count').textContent = `${count} / ${rows.length} entries`;
  }
  function transferState() {
    const operation = $('#operation').value, destination = $('#place').value;
    $('#transfer').textContent = `${operation === 'copy' ? 'Copy' : 'Move'} file`;
    const blocked = destination === $('[data-fact="place"]').textContent;
    const tooSmall = destination === 'small' && !['adapter', 'projector'].includes(scenario);
    $('#transfer').disabled = busy || removed || blocked || tooSmall;
    $('#transfer-note').textContent = blocked ? `Choose a different place; this file is already on ${destination}.` : tooSmall ? 'Not enough space: this file needs 24.6 GiB; scratch has 12 GiB free.' : operation === 'copy' ? 'Copy keeps this original. Existing model paths stay the same.' : 'Move verifies the copy, updates model paths, then removes the original. Loaded models keep answering.';
    $('#operation').disabled = busy || removed;
    $('#place').disabled = busy || removed;
  }
  function runTransfer(label, finish) {
    busy = true;
    $('#transfer-progress').hidden = false;
    let percent = 0;
    $('#progress-label').textContent = label;
    const progress = $('[role="progressbar"]');
    const update = () => {
      $('#progress-percent').textContent = `${percent}%`;
      progress.setAttribute('aria-valuenow', percent);
      progress.firstElementChild.style.width = `${percent}%`;
    };
    update();
    transferState();
    $$('[data-node-select]').forEach(e => { e.disabled = true; });
    updateSelection();
    timer = setInterval(() => {
      percent = Math.min(100, percent + 25);
      $('#progress-label').textContent = percent >= 75 ? 'Verifying SHA-256…' : label;
      update();
      if (percent === 100) {
        clearInterval(timer); busy = false;
        $('#progress-label').textContent = 'SHA-256 verified · complete';
        finish(); renderNodes(); transferState();
      }
    }, matchMedia('(prefers-reduced-motion: reduce)').matches ? 10 : 300);
  }
  function showScenario() {
    clearInterval(timer); busy = false; removed = false;
    copies = initialNodes();
    const isPart = ['adapter', 'projector'].includes(scenario), unused = scenario === 'unused' || isPart;
    filename = scenario === 'adapter' ? 'Qwen3-32B-terse-lora.gguf' : scenario === 'projector' ? 'Qwen3-VL-32B-mmproj-f16.gguf' : 'Qwen3-32B-Q6_K.gguf';
    path = `/srv/models/cache/${filename}`;
    $('#file-heading .page-title').textContent = filename;
    $('#file-heading .eyebrow').textContent = 'FILES / FRONT / ACTIVE CACHE';
    $('#delete-name').disabled = false; $('#jump-transfer').disabled = false;
    $('#copy-path').disabled = false; $('#add-model').disabled = false;
    $('#file-path').textContent = path;
    $$('[data-path-input]').forEach(e => { e.value = path; });
    $('#file-kind').textContent = scenario === 'adapter' ? 'LoRA adapter' : scenario === 'projector' ? 'Vision projector' : 'Language model';
    $('#summary-size').textContent = isPart ? scenario === 'adapter' ? '384 MiB' : '1.2 GiB' : '24.6 GiB';
    $('#summary-format').textContent = isPart ? 'F16 · GGUF' : 'Q6_K · GGUF';
    $('#summary-parameters').textContent = isPart ? 'Companion' : '32B';
    $('#summary-layers').textContent = isPart ? 'Base model required' : '64 transformer layers';
    $('#summary-context').textContent = isPart ? 'Base model' : '40,960';
    $('#summary-usage').textContent = unused ? '0 models' : '2 models';
    $('#summary-protection').textContent = unused ? 'Available to configure' : 'File protected from deletion';
    $('[data-fact="last-used"]').textContent = unused ? 'Never' : '12 Nov 2026 · 10:42 PST';
    $('[data-fact="location"]').textContent = 'Front · active cache';
    $('[data-fact="place"]').textContent = 'nvme';
    $('#model-uses').hidden = unused;
    $('#add-model').hidden = !unused || isPart;
    $('#model-usage-note').textContent = unused ? 'No configured model references this file.' : 'Shared file · each model keeps its own settings.';
    $('#delete-note').textContent = unused ? `Permanently removes ${filename}. It can only be restored by downloading it again.` : 'Remove the two model references before deleting this file.';
    $('#delete-state').textContent = unused ? '· unused' : '· protected';
    $('#delete-form').hidden = !unused;
    $('#delete-name').value = '';
    $('#delete').disabled = true;
    $('#part-card').hidden = !isPart;
    $('#adapter-label').hidden = scenario !== 'adapter';
    $('#short').disabled = scenario !== 'adapter';
    const base = scenario === 'projector' ? 'qwen3-vl-32b' : 'qwen3-32b';
    $('#model').replaceChildren(new Option(base, base), new Option(`${base}-fast`, `${base}-fast`));
    $('#new').value = '';
    $('#new').placeholder = scenario === 'projector' ? 'qwen3-vl-32b-vision' : 'qwen3-32b-terse';
    $('#part-explanation').textContent = scenario === 'adapter' ? 'Create a new configuration with this adapter and a compatible base. Both configurations share the loaded base model.' : 'Create a vision configuration using a compatible base. Only the projector adds memory; the base configuration stays available.';
    $('#failure-notice').hidden = scenario !== 'failure';
    if (scenario === 'failure') copies[1].free = 12;
    $('#transfer-progress').hidden = true;
    $('#operation').value = 'copy'; $('#place').value = 'archive';
    notice(''); setupMetadata();
    $('#metadata-available').hidden = scenario === 'unreadable';
    $('#metadata-unavailable').hidden = scenario !== 'unreadable';
    $('#metadata-count').textContent = scenario === 'unreadable' ? 'Unavailable' : '22 entries';
    if (isPart) {
      // A companion's metadata is its own; never label base-model facts as companion facts.
      const values = scenario === 'adapter'
        ? ['Qwen3 terse adapter', 'LoRA', 'qwen3', 'Not stated', 'Not stated', 'apache-2.0', 'Not included', 'Base template', 'Base template']
        : ['Qwen3 VL projector', 'Vision projector', 'clip', 'Not stated', 'Not stated', 'apache-2.0', 'Not included', 'Base template', 'Base template'];
      $$('.file-metadata .facts dd').forEach((e, i) => { e.textContent = values[i]; });
      const rows = $$('#metadata tbody tr:not(#no-metadata)');
      const entries = scenario === 'adapter' ? [['general.name', 'Qwen3 terse adapter'], ['general.type', 'adapter'], ['adapter.type', 'lora'], ['adapter.lora.alpha', '16'], ['general.architecture', 'qwen3'], ['general.license', 'apache-2.0']] : [['general.name', 'Qwen3 VL projector'], ['general.type', 'projector'], ['general.architecture', 'clip'], ['clip.has_vision_encoder', 'true'], ['clip.projector_type', 'qwen3vl_merger'], ['general.license', 'apache-2.0']];
      rows.forEach((row, i) => {
        if (!entries[i]) row.remove();
        else { row.cells[0].firstElementChild.textContent = entries[i][0]; row.cells[1].textContent = entries[i][1]; }
      });
      $('#file-metadata summary').textContent = `Everything it says, ${entries.length} entries`;
      const group = $('#metadata-group');
      group.replaceChildren(new Option('All groups', 'all'), new Option('General', 'general.'), new Option(scenario === 'adapter' ? 'Adapter' : 'Vision encoder', scenario === 'adapter' ? 'adapter.' : 'clip.'));
      filterMetadata();
    }
    renderNodes(); transferState();
  }
  $$('[data-design]').forEach(button => button.addEventListener('click', () => {
    const layout = button.dataset.design;
    $('#file-workspace').dataset.layout = layout;
    $('#design-description').textContent = descriptions[layout];
    $$('[data-design]').forEach(e => {
      e.classList.toggle('active', e === button);
      e.setAttribute('aria-pressed', String(e === button));
    });
    history.replaceState(null, '', `#${layout}`);
  }));
  $('#scenario').addEventListener('change', () => { scenario = $('#scenario').value; showScenario(); });
  $('#reset').addEventListener('click', showScenario);
  $('#theme').addEventListener('click', () => {
    const mode = $('[data-theme-form] [name="mode"]');
    mode.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    mode.dispatchEvent(new Event('change', {bubbles: true}));
  });
  document.addEventListener('weightroom:theme', () => { $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode'; });
  $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  $('#copy-path').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(path); notice('Full path copied.'); }
    catch {
      const selection = getSelection(), range = document.createRange();
      range.selectNodeContents($('#file-path')); selection.removeAllRanges(); selection.addRange(range);
      notice('Path selected. Use your browser’s copy command.');
    }
  });
  $('#jump-transfer').addEventListener('click', () => {
    $('#distribution-card').scrollIntoView({block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
    const target = $$('[data-node-select]').find(e => !e.disabled) || $('#operation');
    target.focus({preventScroll: true});
  });
  $('#destinations').addEventListener('change', updateSelection);
  $('#distribute-form').addEventListener('submit', event => {
    event.preventDefault();
    if (busy || removed || !selectedNodes().length) return;
    const ids = selectedNodes().map(e => e.value), targets = copies.filter(n => ids.includes(n.id));
    if (targets.some(n => n.free < (scenario === 'adapter' ? .375 : scenario === 'projector' ? 1.2 : 24.6))) {
      $('#failure-notice').hidden = false; notice('Distribution failed. Nova has insufficient free space; the source file is retained.'); return;
    }
    runTransfer(`Distributing to ${targets.map(n => n.name).join(', ')}…`, () => {
      targets.forEach(n => { n.present = true; n.free = Math.round((n.free - (scenario === 'adapter' ? .375 : scenario === 'projector' ? 1.2 : 24.6)) * 10) / 10; });
      $('#failure-notice').hidden = true; notice(`Distributed to ${targets.map(n => n.name).join(', ')}. SHA-256 verified; source retained.`);
    });
  });
  $('#operation').addEventListener('change', transferState); $('#place').addEventListener('change', transferState);
  $('#transfer-form').addEventListener('submit', event => {
    event.preventDefault(); if ($('#transfer').disabled) return;
    const operation = $('#operation').value, destination = $('#place').value;
    runTransfer(`${operation === 'copy' ? 'Copying' : 'Moving'} to ${destination}…`, () => {
      if (operation === 'move') {
        path = `/srv/models/${destination}/${filename}`;
        $('#file-path').textContent = path;
        $$('[data-path-input]').forEach(e => { e.value = path; });
        $('[data-fact="place"]').textContent = destination;
        $('[data-fact="location"]').textContent = destination === 'archive' ? 'Front · storage' : 'Front · active cache';
        $('#file-heading .eyebrow').textContent = `FILES / FRONT / ${destination.toUpperCase()}`;
      }
      notice(operation === 'copy' ? `Copied to ${destination}. SHA-256 verified; original retained.` : `Moved to ${destination}. SHA-256 verified; model paths updated. Loaded models keep answering.`);
    });
  });
  $('#delete-name').addEventListener('input', () => { $('#delete').disabled = $('#delete-name').value !== filename; });
  $('#delete-form').addEventListener('submit', event => {
    event.preventDefault(); if ($('#delete').disabled || busy) return;
    removed = true; $('#delete').disabled = true; $('#delete-name').disabled = true;
    $('#summary-usage').textContent = 'Deleted'; $('#summary-protection').textContent = 'Sample file removed';
    transferState(); renderNodes(); $('#jump-transfer').disabled = true; $('#copy-path').disabled = true;
    $('#add-model').disabled = true;
    notice('Sample file deleted. Reset sample to restore it.');
  });
  function modelDialog(text) { $('#model-dialog-text').textContent = text; $('#model-dialog').showModal(); }
  $$('[data-model]').forEach(button => button.addEventListener('click', () => modelDialog(`${button.dataset.model} uses this file as its base weights. In the live application this opens that model’s configuration.`)));
  $('#add-model').addEventListener('click', () => modelDialog(`A new model would start with ${filename} as its file. The trained context is 40,960 tokens.`));
  $('#part-form').addEventListener('submit', event => {
    event.preventDefault(); notice(`Sample model “${$('#new').value}” created from ${$('#model').value} with this ${scenario}. The base configuration is retained.`);
  });
  showScenario();
  const requested = location.hash.slice(1);
  if (descriptions[requested]) $(`[data-design="${requested}"]`).click();
})();
