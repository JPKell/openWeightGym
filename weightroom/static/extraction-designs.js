/* Prepared fixtures only. Nothing in this preview sends images to an inference service. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const fixtures = JSON.parse($('#extraction-samples').textContent);
  const baseSchema = JSON.parse($('#schema').value);
  const descriptions = {
    desk: '01 · A focused desk for one image. Choose a task, inspect the source and take the result.',
    review: '02 · Source and result at equal scale. Check reading order, locate fields and resolve unclear values.',
    batch: '03 · An inbox for repeated work. Keep settings per image and run selected samples together.'
  };
  const formats = {plain: ['text/plain', 'txt'], layout: ['text/plain', 'txt'], markdown: ['text/markdown', 'md'], json: ['application/json', 'json'], csv: ['text/csv', 'csv']};
  const hints = {plain: 'A plain Unicode string, in reading order.', layout: 'Approximate columns and spacing, shown in monospace.', markdown: 'Headings, paragraphs and Markdown tables.', json: 'Structured fields with explicit missing values.', csv: 'A table or receipt line items, suitable for a spreadsheet.'};
  let records, active = 'receipt', scenario = 'ready', view = 'preview', job = null, uploadUrl = null;
  const fieldNames = ['merchant', 'date', 'currency', 'subtotal', 'tax', 'total'];
  const cloneTemplate = id => $(id).content.cloneNode(true);
  const record = () => records[active];
  const announce = text => { $('#action-status').textContent = text; };
  function initialRecords() {
    return Object.fromEntries(Object.entries(fixtures).map(([key, sample]) => [key, {
      ...structuredClone(sample), key, selected: true, result: true, reviewed: false, edited: false,
      settings: {task: sample.task, format: sample.format, model: 'local', detail: 'concise', instructions: '', schema: JSON.stringify(baseSchema), keep_breaks: true, mark_unclear: true},
      completed: null
    }]));
  }
  function saveSettings() {
    const form = $('#extraction-form');
    for (const key of Object.keys(record().settings)) {
      const control = form.elements.namedItem(key);
      record().settings[key] = control.type === 'checkbox' ? control.checked : control.value;
    }
  }
  function loadSettings() {
    for (const [key, value] of Object.entries(record().settings)) {
      const control = $('#extraction-form').elements.namedItem(key);
      if (control.type === 'checkbox') control.checked = value;
      else control.value = value;
    }
    $('#schema-fields').hidden = $('#task').value !== 'custom';
    $('#format-hint').textContent = hints[$('#format').value];
    const tabular = ['table', 'receipt'].includes($('#task').value);
    $('#format option[value="csv"]').disabled = !tabular;
    $('#detail').disabled = $('#task').value !== 'describe';
  }
  function chooseSample(key) {
    if (job) cancelJob(false);
    saveSettings();
    active = key;
    scenario = 'ready';
    $('#scenario').value = 'ready';
    loadSettings();
    render();
    announce('');
  }
  function settingsUsed() { return record().completed || record().settings; }
  function dirty() { return record().result && record().completed && JSON.stringify(record().settings) !== JSON.stringify(record().completed); }
  function csv(rows) {
    return rows.map(row => row.map(cell => {
      let value = String(cell ?? '');
      // Spreadsheet exports treat all strings beginning with formula operators as text.
      if (typeof cell === 'string' && /^[=+\-@\t\r]/.test(value)) value = "'" + value;
      return '"' + value.replaceAll('"', '""') + '"';
    }).join(',')).join('\r\n');
  }
  function receiptData() {
    return record().receipt || {merchant: null, date: null, currency: null, subtotal: null, tax: null, total: null, items: []};
  }
  function totalsMismatch() {
    const data = receiptData();
    return [data.subtotal, data.tax, data.total].every(value => value != null) &&
      (Math.abs(data.subtotal + data.tax - data.total) >= .005 ||
       Math.abs(data.items.reduce((sum, item) => sum + item.amount, 0) - data.subtotal) >= .005);
  }
  function tableRows() {
    if (settingsUsed().task === 'receipt') {
      const data = receiptData();
      return [['Description', 'Quantity', 'Amount', 'Currency', 'Merchant', 'Date', 'Subtotal', 'Tax', 'Total'],
        ...data.items.map(item => [item.description, item.quantity, item.amount, data.currency, data.merchant, data.date, data.subtotal, data.tax, data.total])];
    }
    return record().table;
  }
  function output() {
    const item = record(), settings = settingsUsed(), data = receiptData();
    let text = '', structured;
    if (settings.task === 'describe') {
      text = settings.detail === 'detailed' ? item.detailed : item.description;
      structured = {description: text};
    } else if (settings.task === 'text') {
      text = settings.format === 'layout' ? item.layout : item.text;
      if (!settings.mark_unclear) text = text.replaceAll('[unclear]', '');
      if (!settings.keep_breaks && settings.format !== 'layout') text = text.replace(/\n+/g, ' ');
      structured = {text, blocks: item.regions.map((region, index) => ({order: index + 1, label: region.field, bbox_percent: region.box}))};
    } else if (settings.task === 'receipt') {
      structured = data;
      const amount = value => value == null ? '[unclear]' : Number(value).toFixed(2);
      text = `${data.merchant ?? '[not found]'}\n${data.date ?? '[not found]'}\n\n`;
      text += data.items.map(row => `${row.description.padEnd(14)} ${String(row.quantity).padStart(2)}   ${amount(row.amount).padStart(7)}`).join('\n');
      text += `\n\nSubtotal       ${amount(data.subtotal)}\nTax            ${amount(data.tax)}\nTotal          ${amount(data.total)} ${data.currency ?? ''}`;
    } else if (settings.task === 'table') {
      const rows = item.table;
      structured = {columns: rows[0] || [], rows: rows.slice(1)};
      text = rows.map(row => row.map(cell => String(cell).padEnd(16)).join('')).join('\n');
    } else {
      structured = {merchant: data.merchant, total: data.total};
      text = `Merchant: ${data.merchant ?? '[not found]'}\nTotal: ${data.total ?? '[not found]'}`;
    }
    if (settings.format === 'json') return JSON.stringify(structured, null, 2);
    if (settings.format === 'csv') return csv(tableRows());
    if (settings.format === 'markdown') {
      if (settings.task === 'table' && item.table.length) {
        const rows = item.table.map(row => '| ' + row.join(' | ') + ' |');
        rows.splice(1, 0, '| ' + item.table[0].map(() => '---').join(' | ') + ' |');
        return '## Setup checklist\n\n' + rows.join('\n');
      }
      if (settings.task === 'receipt') return `## ${data.merchant ?? 'Receipt'}\n\n` + text.split('\n').slice(1).join('\n').replace(/\n/g, '  \n');
      return `## ${settings.task === 'describe' ? 'Image description' : 'Extracted text'}\n\n${text}`;
    }
    return text;
  }
  function renderRegions() {
    $('#regions').replaceChildren();
    for (const region of record().regions) {
      const fragment = cloneTemplate('#region-template'), button = fragment.querySelector('button');
      button.dataset.region = region.field;
      button.setAttribute('aria-label', 'Locate ' + region.field + ' in source');
      button.style.left = region.box[0] + '%'; button.style.top = region.box[1] + '%';
      button.style.width = region.box[2] + '%'; button.style.height = region.box[3] + '%';
      button.addEventListener('click', () => locate(region.field, true));
      $('#regions').append(fragment);
    }
    $('#region-help').textContent = record().regions.length ? 'Select a field or a region to inspect its source.' : 'No text regions in this image.';
  }
  function locate(field, focus = false) {
    $$('.ex-region').forEach(button => button.toggleAttribute('data-active', button.dataset.region === field));
    $('#region-help').textContent = 'Source region: ' + field;
    if (focus) $('#receipt-fields input[data-field="' + field + '"]')?.focus();
    else $('#source-panel').scrollIntoView({block: 'nearest', behavior: 'instant'});
  }
  function fillTable(head, body, rows) {
    head.replaceChildren(); body.replaceChildren();
    rows.forEach((row, index) => {
      const fragment = cloneTemplate('#data-row-template'), tr = fragment.querySelector('tr');
      row.forEach(value => { const cell = document.createElement(index === 0 ? 'th' : 'td'); cell.textContent = value; if (!index) cell.scope = 'col'; tr.append(cell); });
      (index ? body : head).append(fragment);
    });
  }
  function renderReceipt() {
    const data = receiptData();
    $('#receipt-merchant').textContent = data.merchant || 'No receipt found';
    $('#receipt-fields').replaceChildren();
    for (const field of fieldNames) {
      const fragment = cloneTemplate('#receipt-field-template');
      const label = fragment.querySelector('label'), input = fragment.querySelector('input'), button = fragment.querySelector('button');
      label.htmlFor = 'field-' + field; label.textContent = field[0].toUpperCase() + field.slice(1);
      input.id = label.htmlFor; input.dataset.field = field;
      const numeric = ['subtotal', 'tax', 'total'].includes(field);
      input.type = numeric ? 'number' : field === 'date' ? 'date' : 'text';
      if (numeric) { input.step = '0.01'; input.min = '0'; }
      input.value = data[field] == null ? '' : numeric ? Number(data[field]).toFixed(2) : data[field];
      input.placeholder = 'Not readable'; input.disabled = !record().receipt;
      fragment.querySelector('.ex-receipt-field').toggleAttribute('data-unclear', data[field] == null);
      fragment.querySelector('[data-field-note]').textContent = data[field] == null ? 'Needs review · no value inferred' : 'From source · editable';
      button.setAttribute('aria-label', 'Locate ' + field); button.disabled = !record().regions.some(region => region.field === field);
      button.addEventListener('click', () => locate(field));
      input.addEventListener('focus', () => locate(field));
      input.addEventListener('change', () => {
        if (!input.checkValidity()) { input.reportValidity(); return; }
        record().receipt[field] = input.value === '' ? null : numeric ? Number(input.value) : input.value;
        record().edited = true; record().reviewed = false;
        renderResult(); announce('Correction kept in this tab. Raw output and downloads include your edit.');
      });
      $('#receipt-fields').append(fragment);
    }
    $('#receipt-items').replaceChildren();
    for (const item of data.items) {
      const fragment = cloneTemplate('#data-row-template'), tr = fragment.querySelector('tr');
      [item.description, item.quantity, item.amount.toFixed(2)].forEach((value, index) => {
        const cell = document.createElement('td'); cell.textContent = value; if (index) cell.className = 'text-end'; tr.append(cell);
      }); $('#receipt-items').append(fragment);
    }
    if (!data.items.length) $('#receipt-items').append(cloneTemplate('#empty-items-template'));
    let check = 'Totals cannot be checked until tax is confirmed.';
    if ([data.subtotal, data.tax, data.total].every(value => value != null)) {
      const itemsMatch = Math.abs(data.items.reduce((sum, item) => sum + item.amount, 0) - data.subtotal) < .005;
      check = Math.abs(data.subtotal + data.tax - data.total) < .005 && itemsMatch ? 'Checked · line items, subtotal, tax and total agree.' : 'Needs review · line items or totals do not agree.';
    }
    $('#reconciliation').textContent = check;
    $('#review-toggle').textContent = record().reviewed ? 'Reviewed ✓' : 'Mark reviewed';
    $('#review-toggle').setAttribute('aria-pressed', String(record().reviewed));
  }
  function renderResult() {
    const item = record(), settings = settingsUsed();
    const visible = item.result && scenario !== 'empty' && scenario !== 'error' && !item.upload;
    $('#result-empty').hidden = visible;
    $('#result-preview').hidden = !visible || view !== 'preview';
    $('#raw-output').hidden = !visible || view !== 'raw';
    $('#copy').disabled = $('#download').disabled = !visible;
    const missingReceipt = settings.task === 'receipt' && !item.receipt;
    const unclear = settings.task === 'receipt' && (missingReceipt || fieldNames.some(field => item.receipt[field] == null));
    const mismatch = settings.task === 'receipt' && totalsMismatch();
    $('#result-state').textContent = !visible ? 'Awaiting extraction' : dirty() ? 'Settings changed' : item.reviewed ? 'Reviewed' : unclear || mismatch ? 'Needs review' : item.edited ? 'Edited locally' : 'Sample result';
    $('#review-notice').hidden = !visible || (!unclear && !mismatch && settings.task !== 'text' && settings.task !== 'table' && !dirty());
    $('#review-notice').textContent = dirty() ? 'Settings changed. Run again to update this result.' : mismatch ? 'Line items or totals do not agree. Compare the corrected values with the original.' : unclear ? (missingReceipt ? 'No receipt was found in this image. Missing fields stay empty.' : 'One or more fields are unreadable. Check the source; missing values stay null.') : settings.task === 'table' && !item.table.length ? 'No table was found in this image.' : settings.task === 'text' && !item.text ? 'No visible text was found in this image.' : 'Reading order and spacing are approximate. Compare them with the original.';
    $('#receipt-preview').hidden = settings.task !== 'receipt';
    $('#table-preview').hidden = settings.task !== 'table' || !item.table.length;
    $('#text-preview').hidden = settings.task === 'receipt' || (settings.task === 'table' && item.table.length);
    if (visible) {
      const value = output();
      $('#raw-output').textContent = value;
      $('#text-preview').textContent = value || 'No visible text.';
      $('#text-preview').dataset.format = settings.format;
      if (settings.task === 'receipt') renderReceipt();
      if (settings.task === 'table') fillTable($('#table-head'), $('#table-body'), item.table);
    }
    $('#output-type').textContent = formats[settings.format][0];
    $('#result-provenance').textContent = `${item.edited ? 'Locally corrected' : 'Prepared sample'} · ${settings.task} v1 · no model called`;
    $$('[data-result-view]').forEach(button => { const on = button.dataset.resultView === view; button.classList.toggle('active', on); button.setAttribute('aria-pressed', String(on)); });
  }
  function renderInbox() {
    $('#inbox-list').replaceChildren();
    const query = $('#inbox-search').value.toLowerCase();
    for (const item of Object.values(records)) {
      const fragment = cloneTemplate('#inbox-item-template'), row = fragment.querySelector('.ex-inbox-item');
      row.hidden = !item.name.toLowerCase().includes(query);
      const select = fragment.querySelector('input');
      select.checked = item.selected; select.disabled = Boolean(item.upload) || Boolean(job);
      select.setAttribute('aria-label', 'Select ' + item.name);
      select.addEventListener('change', () => { item.selected = select.checked; renderInbox(); });
      const choice = fragment.querySelector('button');
      choice.setAttribute('aria-current', String(item.key === active));
      choice.addEventListener('click', () => chooseSample(item.key));
      fragment.querySelector('img').src = item.image;
      fragment.querySelector('[data-inbox-name]').textContent = item.name;
      fragment.querySelector('[data-inbox-detail]').textContent = item.settings.task + ' → ' + item.settings.format.toUpperCase();
      fragment.querySelector('[data-inbox-status]').textContent = item.upload ? 'Local' : item.result ? 'Ready' : 'Waiting';
      $('#inbox-list').append(fragment);
    }
    const selected = Object.values(records).filter(item => item.selected && !item.upload).length;
    $('#batch-count').textContent = selected + ' selected';
    $('#inbox-empty').hidden = $$('.ex-inbox-item').some(row => !row.hidden);
    $('#run-batch').disabled = !selected || Boolean(job) || scenario !== 'ready';
  }
  function render() {
    const item = record(), empty = scenario === 'empty';
    $('#source-empty').hidden = !empty; $('#image-stage').hidden = empty;
    $('#source-image').src = item.image;
    $('#source-image').alt = item.upload ? 'Locally uploaded image: ' + item.name : 'Prepared source sample: ' + item.name;
    $('#source-name').textContent = empty ? 'No image selected' : item.name;
    $('#source-size').textContent = empty ? '' : `${item.width} × ${item.height} · ${item.upload ? 'local upload' : 'sample'}`;
    $('#error-notice').hidden = scenario !== 'error'; $('#model-notice').hidden = scenario !== 'no-model';
    $('#run').disabled = empty || scenario === 'no-model' || Boolean(item.upload) || Boolean(job);
    $('#model').disabled = scenario === 'no-model' || Boolean(job);
    $('#run-hint').textContent = item.upload ? 'Image preview only. Real extraction requires the planned model connection.' : 'Uses a prepared sample result. Instructions are saved as a draft; sample wording stays fixed.';
    $$('[data-sample]').forEach(button => { const on = button.dataset.sample === active; button.classList.toggle('active', on); button.setAttribute('aria-pressed', String(on)); });
    renderRegions(); renderResult(); renderInbox();
  }
  function cancelJob(message = true) {
    if (job) clearTimeout(job.timer);
    job = null; $('#cancel').hidden = true; $('#run').textContent = 'Run sample extraction';
    $$('#extraction-form input, #extraction-form textarea, #extraction-form select').forEach(control => { control.disabled = false; });
    loadSettings(); render(); if (message) announce('Sample run cancelled. Previous results are kept.');
  }
  function runSamples(keys) {
    if (job) return;
    saveSettings();
    for (const key of keys) {
      const settings = records[key].settings;
      if (settings.task === 'custom') {
        try {
          const schema = JSON.parse(settings.schema);
          if (JSON.stringify(schema) !== JSON.stringify(baseSchema)) throw new Error('Use the prepared merchant/total schema in this preview. Custom schema execution is planned.');
        } catch (error) { announce('Schema: ' + error.message); return; }
      }
    }
    job = {timer: null}; $('#cancel').hidden = false; $('#run').textContent = 'Running sample…';
    $$('#extraction-form input, #extraction-form textarea, #extraction-form select').forEach(control => { control.disabled = true; });
    render(); announce(`Running ${keys.length} prepared sample${keys.length === 1 ? '' : 's'}…`);
    job.timer = setTimeout(() => {
      for (const key of keys) { records[key].result = true; records[key].completed = structuredClone(records[key].settings); records[key].reviewed = false; }
      scenario = 'ready'; $('#scenario').value = 'ready';
      cancelJob(false); announce(`${keys.length} sample${keys.length === 1 ? '' : 's'} ready. No model was called.`);
    }, 450);
  }
  async function upload(file) {
    if (!file) return;
    if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) { announce('Choose a PNG, JPEG or WebP image.'); return; }
    if (file.size > 5 * 1024 * 1024) { announce('Image exceeds the 5 MiB preview limit.'); return; }
    const url = URL.createObjectURL(file), image = new Image();
    try {
      image.src = url; await image.decode();
      if (image.naturalWidth * image.naturalHeight > 16_000_000) throw new Error('Image exceeds the 16 megapixel preview limit.');
      if (job) cancelJob(false);
      saveSettings(); if (uploadUrl) URL.revokeObjectURL(uploadUrl); uploadUrl = url;
      records.upload = {...structuredClone(records.receipt), key: 'upload', name: file.name, image: url, width: image.naturalWidth, height: image.naturalHeight,
        upload: true, selected: false, result: false, receipt: null, regions: [], text: '', table: []};
      active = 'upload'; scenario = 'ready'; $('#scenario').value = 'ready'; loadSettings(); render();
      announce('Image loaded in this tab. The preview cannot extract new images; choose a prepared sample to try outputs.');
    } catch (error) { URL.revokeObjectURL(url); announce(error.message || 'This image could not be read.'); }
    $('#image-file').value = '';
  }
  $$('[data-design]').forEach(button => button.addEventListener('click', () => {
    $('#extraction-workspace').dataset.layout = button.dataset.design;
    $('#design-description').textContent = descriptions[button.dataset.design];
    $$('[data-design]').forEach(other => { const on = other === button; other.classList.toggle('active', on); other.setAttribute('aria-pressed', String(on)); });
  }));
  $$('[data-sample]').forEach(button => button.addEventListener('click', () => chooseSample(button.dataset.sample)));
  $$('[data-result-view]').forEach(button => button.addEventListener('click', () => { view = button.dataset.resultView; renderResult(); }));
  $('#extraction-form').addEventListener('submit', event => { event.preventDefault(); if (!$('#run').disabled) runSamples([active]); });
  $('#extraction-form').addEventListener('change', () => {
    if (!['receipt', 'table'].includes($('#task').value) && $('#format').value === 'csv') $('#format').value = 'json';
    saveSettings(); loadSettings(); renderResult(); renderInbox();
  });
  $('#cancel').addEventListener('click', () => cancelJob());
  $('#run-batch').addEventListener('click', () => runSamples(Object.values(records).filter(item => item.selected && !item.upload).map(item => item.key)));
  $('#retry').addEventListener('click', () => runSamples([active]));
  $('#inbox-search').addEventListener('input', renderInbox);
  $('#review-toggle').addEventListener('click', () => { record().reviewed = !record().reviewed; renderResult(); announce(record().reviewed ? 'Marked reviewed. Missing fields remain explicit in the output.' : 'Review mark cleared.'); });
  $('#scenario').addEventListener('change', () => { if (job) cancelJob(false); scenario = $('#scenario').value; render(); announce(''); });
  $('#copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(output()); announce('Output copied.'); }
    catch { view = 'raw'; renderResult(); const range = document.createRange(); range.selectNodeContents($('#raw-output')); const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range); $('#raw-output').focus(); announce('Clipboard access is unavailable. Output selected; press Ctrl/⌘ C.'); }
  });
  $('#download').addEventListener('click', () => {
    const settings = settingsUsed(), [type, extension] = formats[settings.format];
    const url = URL.createObjectURL(new Blob([output()], {type: type + ';charset=utf-8'}));
    const link = document.createElement('a'); link.href = url; link.download = record().name.replace(/\.[^.]+$/, '') + '-' + settings.task + '.' + extension;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); announce('Downloaded ' + extension.toUpperCase() + ' output.');
  });
  ['#choose-image', '#empty-choose'].forEach(id => $(id).addEventListener('click', () => $('#image-file').click()));
  $('#image-file').addEventListener('change', event => upload(event.target.files[0]));
  $('#drop-zone').addEventListener('keydown', event => { if (event.target === $('#drop-zone') && ['Enter', ' '].includes(event.key)) { event.preventDefault(); $('#image-file').click(); } });
  $('#drop-zone').addEventListener('dragover', event => { event.preventDefault(); $('#drop-zone').dataset.dragging = ''; });
  $('#drop-zone').addEventListener('dragleave', () => delete $('#drop-zone').dataset.dragging);
  $('#drop-zone').addEventListener('drop', event => { event.preventDefault(); delete $('#drop-zone').dataset.dragging; upload(event.dataTransfer.files[0]); });
  document.addEventListener('paste', event => { const file = [...event.clipboardData.items].find(item => item.kind === 'file' && item.type.startsWith('image/'))?.getAsFile(); if (file) { event.preventDefault(); upload(file); } });
  $('#theme').addEventListener('click', () => {
    const mode = $('html').dataset.bsTheme === 'dark' ? 'light' : 'dark';
    const select = $('[data-theme-form] select[name="mode"]'); select.value = mode; select.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = mode === 'dark' ? 'Light mode' : 'Dark mode';
  });
  function reset() {
    if (job) cancelJob(false);
    if (uploadUrl) URL.revokeObjectURL(uploadUrl); uploadUrl = null;
    records = initialRecords(); active = 'receipt'; scenario = 'ready'; view = 'preview';
    for (const item of Object.values(records)) item.completed = structuredClone(item.settings);
    $('#scenario').value = 'ready'; $('#inbox-search').value = ''; loadSettings(); render(); announce('');
    $('#theme').textContent = $('html').dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  }
  $('#reset').addEventListener('click', reset);
  reset();
})();
