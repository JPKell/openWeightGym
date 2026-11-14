// Offline design review. Definitions and field markup come from the live application.
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const definitions = JSON.parse($('#definitions').textContent);
  const original = Object.fromEntries(definitions.map(b => [b.id, {
    formula: b.formula, green: b.green, enabled: b.enabled, ...(b.quality ? {server: b.server} : {})
  }]));
  let saved = structuredClone(original), drafts = structuredClone(saved);
  let selected = 'json_schema', scenario = 'normal';
  const descriptions = {
    studio: ['01 / Recommended', 'A focused editor with a searchable benchmark library and measurements beside the formula.'],
    ledger: ['02 / Settings ledger', 'A compact catalogue above paired settings, with a full-width measurement dictionary below.'],
    desk: ['03 / Policy desk', 'Availability and execution first, with a prominent scoring policy and full-width reference.']
  };
  const dirty = id => JSON.stringify(drafts[id]) !== JSON.stringify(saved[id]);
  const say = message => { $('#feedback').textContent = message; };
  function snapshot() {
    const form = $('[data-settings-form]');
    if (!form) return;
    const fields = form.elements;
    drafts[selected] = {formula: fields.formula.value, green: fields.green.value, enabled: fields.enabled.checked,
      ...(fields.server ? {server: fields.server.value} : {})};
  }
  function status() {
    const empty = scenario === 'empty';
    $('#manage-grid').hidden = empty;
    $('#catalogue-empty').hidden = !empty;
    $('#readonly-note').hidden = scenario !== 'readonly';
    $('#total-count').textContent = empty ? '0' : definitions.length;
    $('#enabled-count').textContent = empty ? '0' : Object.values(saved).filter(b => b.enabled).length;
    $('#quality-count').textContent = empty ? '0' : definitions.filter(b => b.quality && !b.classifier).length;
    $$('[data-benchmark]').forEach(button => {
      button.setAttribute('aria-pressed', String(button.dataset.benchmark === selected));
      button.querySelector('[data-draft]').hidden = !dirty(button.dataset.benchmark);
      const dot = button.querySelector('[data-available]');
      dot.dataset.enabled = saved[button.dataset.benchmark].enabled;
      dot.setAttribute('aria-label', saved[button.dataset.benchmark].enabled ? 'Available' : 'Disabled');
    });
    const form = $('[data-settings-form]');
    if (!form) return;
    const changed = dirty(selected), readonly = scenario === 'readonly';
    $('#save-state').textContent = readonly ? 'View only' : changed ? 'Unsaved changes' : 'All changes saved';
    form.querySelectorAll('input, select, button').forEach(field => { field.disabled = readonly && field.id !== 'value-search'; });
    $('#save').disabled = readonly || !changed;
    $('#discard').disabled = readonly || !changed;
    validate();
  }
  // Match the server's arithmetic vocabulary without eval or executing user content.
  function formulaError(text, names) {
    if (!text.trim()) return 'Enter a score calculation.';
    if (text.length > 500) return 'Use no more than 500 characters.';
    const tokens = text.match(/(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|[A-Za-z_]\w*|[^\s]/g) || [];
    let position = 0, nodes = 1;
    function atom() {
      const token = tokens[position++];
      nodes++;
      if (token === '+' || token === '-') { nodes++; atom(); return; }
      if (token === '(') { expression(); if (tokens[position++] !== ')') throw Error('Close the parentheses.'); return; }
      if (!token) throw Error('Complete the calculation.');
      if (/^[A-Za-z_]\w*$/.test(token)) {
        if (!names.has(token)) throw Error(`No recorded value named ${token}.`);
        nodes++;
        return;
      }
      if (!/^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(token) || !Number.isFinite(Number(token))) {
        throw Error('Use recorded values, numbers, parentheses and + - * /.');
      }
    }
    function product() { atom(); while (['*', '/'].includes(tokens[position])) { position++; nodes += 2; atom(); } }
    function expression() { product(); while (['+', '-'].includes(tokens[position])) { position++; nodes += 2; product(); } }
    try {
      expression();
      if (position !== tokens.length) throw Error('Use recorded values, numbers, parentheses and + - * /.');
      if (nodes > 80) throw Error('Use a shorter score calculation.');
      return '';
    } catch (error) { return error.message; }
  }
  function validate() {
    const form = $('[data-settings-form]');
    const bench = definitions.find(b => b.id === selected);
    const names = new Set([...bench.kpis.map(k => k[0]), 'parameter_count', 'trained_context', 'configured_context', 'layers', 'gpu_vram_mib']);
    const message = formulaError(form.elements.formula.value, names);
    form.elements.formula.setCustomValidity(message);
    form.elements.formula.setAttribute('aria-invalid', String(Boolean(message)));
    form.elements.formula.setAttribute('aria-describedby', 'formula-validation');
    $('#formula-validation').textContent = message || 'Valid calculation · missing operands produce no score.';
    $('#formula-validation').classList.toggle('text-danger', Boolean(message));
    $('#formula-validation').classList.toggle('text-secondary', !message);
  }
  function filterValues() {
    const term = $('#value-search').value.trim().toLowerCase();
    const rows = $$('#values-panel tbody tr');
    rows.forEach(row => { row.hidden = !row.textContent.toLowerCase().includes(term); });
    const count = rows.filter(row => !row.hidden).length;
    $('#value-count').textContent = count ? `${count} of ${rows.length} values shown` : 'No matching values. Clear the search to see all measurements.';
  }
  function render() {
    const template = $(`template[data-editor="${selected}"]`);
    $('#selected-workspace').replaceChildren(template.content.cloneNode(true));
    const form = $('[data-settings-form]');
    for (const [key, value] of Object.entries(drafts[selected])) {
      if (key === 'enabled') form.elements[key].checked = value;
      else form.elements[key].value = value;
    }
    $$('#values-panel tbody th code, #values-panel tbody td:first-child code').forEach(code => {
      const button = $('#insert-value-template').content.firstElementChild.cloneNode(true);
      button.dataset.insert = code.textContent;
      button.append(code.cloneNode(true));
      code.replaceWith(button);
    });
    form.addEventListener('input', () => { snapshot(); status(); });
    form.addEventListener('change', () => { snapshot(); status(); });
    form.addEventListener('submit', event => {
      event.preventDefault();
      if (scenario === 'readonly') return;
      snapshot();
      validate();
      if (!form.reportValidity()) return;
      if (scenario === 'error') { say('Could not save sample settings. Your changes are retained. Choose Configured to retry.'); return; }
      saved[selected] = structuredClone(drafts[selected]);
      say(`${definitions.find(b => b.id === selected).name} settings saved in this preview.`);
      status();
    });
    $('#discard').addEventListener('click', () => {
      drafts[selected] = structuredClone(saved[selected]);
      render(); say('Changes discarded for this benchmark.');
    });
    form.addEventListener('click', event => {
      const button = event.target.closest('[data-insert]');
      if (!button || scenario === 'readonly') return;
      const field = form.elements.formula;
      field.setRangeText(button.dataset.insert, field.selectionStart, field.selectionEnd, 'end');
      field.focus(); snapshot(); status();
    });
    $('#value-search').addEventListener('input', filterValues);
    filterValues(); status();
  }
  function filterBenchmarks() {
    const term = $('#benchmark-search').value.toLowerCase().trim();
    $$('[data-benchmark]').forEach(button => {
      const b = definitions.find(b => b.id === button.dataset.benchmark);
      button.hidden = !`${b.name} ${b.id} ${b.summary}`.toLowerCase().includes(term);
    });
    $$('[data-benchmark-group]').forEach(group => { group.hidden = ![...group.querySelectorAll('[data-benchmark]')].some(b => !b.hidden); });
    $('#no-matches').hidden = $$('[data-benchmark]').some(b => !b.hidden);
  }
  $$('[data-benchmark]').forEach(button => button.addEventListener('click', () => {
    snapshot(); selected = button.dataset.benchmark; say(''); render();
  }));
  $$('[data-design]').forEach(button => button.addEventListener('click', () => {
    $('#shell').dataset.layout = button.dataset.design;
    $$('[data-design]').forEach(b => { b.setAttribute('aria-pressed', String(b === button)); b.classList.toggle('active', b === button); });
    const [number, description] = descriptions[button.dataset.design];
    $('#design-number').textContent = number;
    $('#design-description').textContent = description;
  }));
  $('#benchmark-search').addEventListener('input', filterBenchmarks);
  $('#scenario').addEventListener('change', event => { snapshot(); scenario = event.target.value; say(''); status(); });
  $('#restore-catalogue').addEventListener('click', () => { scenario = 'normal'; $('#scenario').value = scenario; status(); });
  $('#theme').addEventListener('click', () => {
    const mode = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    const input = $('[data-theme-form] [name=mode]');
    input.value = mode; input.dispatchEvent(new Event('change', {bubbles: true}));
    $('#theme').textContent = mode === 'dark' ? 'Light mode' : 'Dark mode';
  });
  $('#reset').addEventListener('click', () => {
    saved = structuredClone(original); drafts = structuredClone(original);
    selected = 'json_schema'; scenario = 'normal'; $('#scenario').value = scenario;
    $('#benchmark-search').value = ''; filterBenchmarks(); render(); say('Sample settings reset.');
  });
  $('.preview-sidebar').addEventListener('click', event => {
    const link = event.target.closest('a');
    if (link) { event.preventDefault(); say(`${link.textContent.trim()} is shown for navigation context. This preview contains the Manage page.`); }
  });
  document.addEventListener('weightroom:theme', () => { $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode'; });
  render();
})();
