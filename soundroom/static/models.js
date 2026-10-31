/* Audio models are selected from managed files; runtime contracts describe compatibility. */
(() => {
  'use strict';
  if (!document.getElementById('soundroom-models')) return;
  const $ = name => document.getElementById('am-' + name);
  const params = new URLSearchParams(location.search), node = params.get('node') || 'compute';
  const element = (tag, text, className) => { const one = document.createElement(tag); if (text !== undefined) one.textContent = text; if (className) one.className = className; return one; };
  const message = error => { $('message').hidden = !error; $('message').textContent = error || ''; };
  let inventory = {}, nodes = [], choices = [], selected = null;
  async function call(path, body) {
    if (path === '/soundroom-models') path += '?node=' + encodeURIComponent(node);
    const response = await fetch(path, {method: body === undefined ? 'GET' : 'POST', cache:'no-store',
      headers: body === undefined ? {} : {'content-type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
    if (response.redirected) { location.href = '/sign-in'; throw new Error('Please sign in again.'); }
    const value = await response.json();
    if (!response.ok) throw new Error(value.error?.message || 'The operation failed.');
    return value;
  }
  const safe = action => async event => { try { await action(event); message(''); } catch (error) { message(error.message); } };
  const chosen = () => $('file').value === '' ? null : choices[Number($('file').value)];
  function families() {
    const list = [...(inventory.catalogue?.families || [])];
    for (const file of choices) if (file.facts?.family && !list.some(one => one.family === file.facts.family))
      list.push({family:file.facts.family, name:file.facts.family, kinds:file.facts.kinds || [], description:'Contract embedded in this model file; the runtime must implement its architecture.'});
    if (selected && !list.some(one => one.family === selected.audiocpp.family)) list.push({family:selected.audiocpp.family, name:selected.audiocpp.family, kinds:[selected.audiocpp.kind], description:'Current model family; runtime catalogue unavailable.'});
    return list;
  }
  function filterFamilies() {
    const file = chosen(), previous = $('family').value;
    const known = file?.facts?.family;
    const choices = families().filter(one => one.kinds.length && (!known || one.family === known));
    $('family').replaceChildren(new Option('Choose a compatible model family',''), ...choices.map(one => new Option(one.name || one.family, one.family)));
    $('family').value = known || (choices.some(one => one.family === previous) ? previous : '');
  }
  function defaultFields(family) {
    let values = {};
    try { values = SuiteModelFields.read('am-defaults'); } catch { return; }
    $('default-fields').replaceChildren();
    const spec = family?.options?.request || [];
    for (const option of spec) {
      const column = element('div', undefined, 'col-md-6'), label = element('label', option.name.replace(/_/g, ' '), 'form-label');
      const input = element(option.values?.length || ['bool','boolean'].includes(option.type) ? 'select' : 'input');
      input.className = input.tagName === 'SELECT' ? 'form-select' : 'form-control';
      input.id = 'am-option-' + option.name; label.htmlFor = input.id;
      input.dataset.jsonSource = 'am-defaults'; input.dataset.jsonField = Object.hasOwn(values, family.family + '.' + option.name) ? family.family + '.' + option.name : option.name;
      if (input.tagName === 'SELECT') {
        input.append(new Option('Runtime default', ''));
        for (const value of option.values || [true, false]) input.add(new Option(String(value), String(value)));
        if (['bool','boolean'].includes(option.type)) input.dataset.scalar = 'boolean';
      } else if (['int','integer','float','number'].includes(option.type)) {
        input.type = 'number'; input.step = ['int','integer'].includes(option.type) ? '1' : 'any';
        if (option.min !== undefined) input.min = option.min;
        if (option.max !== undefined) input.max = option.max;
      }
      column.append(label, input);
      if (option.description) { const more = element('details', undefined, 'more'); more.append(element('summary','Why?'), element('p',option.description,'form-hint')); column.append(more); }
      $('default-fields').append(column);
    }
    $('defaults-empty').hidden = spec.length > 0;
    $('defaults-empty').textContent = spec.length ? '' : 'No request options are declared; additional defaults remain in Advanced.';
    SuiteModelFields.load('am-defaults', values);
  }
  function exclusive() {
    const required = ['music','sound'].includes($('kind').value);
    $('alone').disabled = required; if (required) $('alone').checked = true;
    $('exclusive-hint').hidden = !required;
  }
  function familyChanged() {
    const family = families().find(one => one.family === $('family').value);
    const previous = $('kind').value;
    $('kind').replaceChildren(...(family?.kinds || []).map(kind => new Option(kind,kind)));
    if (family?.kinds.includes(previous)) $('kind').value = previous;
    $('family-note').textContent = [family?.description, family?.note].filter(Boolean).join(' ');
    $('session').title = family?.note || ''; defaultFields(family); exclusive();
  }
  function companions() {
    const file = chosen(), root = $('root').value.replace(/\/$/, '');
    const previous = new Set(Array.from($('companions').selectedOptions).map(option => option.value));
    const source = nodes.find(one => one.name === file?.machine);
    const available = (source?.files || []).filter(one => !one.moving && one.path !== file.path && one.path.startsWith(root + '/'));
    $('companions').replaceChildren(...available.map(one => { const option = new Option(one.path.slice(root.length + 1),one.path); option.selected = previous.has(one.path); return option; }));
  }
  function fileChanged() {
    const file = chosen();
    if (!file) return;
    $('root').value = file.path.slice(0,file.path.lastIndexOf('/'));
    filterFamilies();
    familyChanged(); companions();
    if (!selected && !$('name').value) $('name').value = file.path.split('/').pop().replace(/\.[^.]+$/, '').toLowerCase().replace(/[^a-z0-9._-]/g,'-');
  }
  function renderProfiles() {
    $('profiles').replaceChildren(...inventory.models.map(row => {
      const tr = element('tr'), cell = element('td'), link = element('a',row.name);
      link.href = '/models/' + encodeURIComponent(row.name) + '?node=' + encodeURIComponent(node); cell.append(link);
      const device = row.gpus === 0 ? 'CPU' : Array.isArray(row.gpus) ? row.gpus.join(', ') : 'one GPU';
      tr.append(cell,element('td',row.audiocpp.family),element('td',row.audiocpp.kind),element('td',device),
        element('td',row.loaded || 'idle'),element('td',row.measured?.mib?.map(value => value + ' MiB').join(', ') || '—'));
      const action = element('td');
      if (['music','sound'].includes(row.audiocpp.kind)) {
        const button = element('button','Test','btn btn-outline-secondary btn-sm');
        button.addEventListener('click',safe(() => runTest(row,button))); action.append(button);
      }
      tr.append(action); return tr;
    }));
  }
  async function refresh() {
    const [models,files] = await Promise.all([call('/soundroom-models'),call('/model-files')]);
    inventory = models; nodes = files.nodes || [];
    selected = inventory.models.find(row => row.name === params.get('model')) || null;
    choices = nodes.flatMap(source => source.files.filter(file => file.facts?.what === 'audio' && !file.moving && /\.(gguf|safetensors|pth|bin|onnx)$/i.test(file.path)).map(file => ({...file,machine:source.name})));
    if (selected && !choices.some(file => file.machine === node && file.path === selected.flags.model))
      choices.push({path:selected.flags.model,machine:node,tier:'current',facts:{family:selected.audiocpp.family,kinds:[selected.audiocpp.kind]}});
    $('file').replaceChildren(new Option('Choose an audio model file',''),...choices.map((file,index) => {
      const option = new Option(file.path + ' · ' + file.machine + ' · ' + file.tier,String(index));
      option.disabled = Boolean(selected && (file.machine !== node || file.tier === 'storage') && file.path !== selected.flags.model); return option;
    }));
    filterFamilies();
    $('node').replaceChildren(...nodes.map(one => new Option(one.name + (one.answering ? '' : ' · not reachable'),one.name))); $('node').value = node;
    SuiteHardware.set('am', selected?.gpus ?? 1, inventory.cards || []);
    const runtime = (inventory.engines?.engines || []).find(one => one.id === 'audiocpp');
    $('runtime').textContent = runtime?.available ? 'audio.cpp is installed on this node. Choose any compatible model file from cache/storage.' : runtime?.why || '';
    renderProfiles();
    if (selected) {
      const profile = selected.audiocpp;
      $('file').value = String(choices.findIndex(file => file.path === selected.flags.model && file.machine === node)); fileChanged();
      $('family').value = profile.family; familyChanged(); $('kind').value = profile.kind; exclusive();
      $('name').value = selected.name; $('name').readOnly = true;
      $('alone').checked = selected.alone ?? true; exclusive();
      $('seconds').value = profile.limits?.max_seconds || 600; $('takes').value = profile.limits?.max_takes || 8;
      $('text').value = profile.limits?.max_text_chars ?? 20000; $('audio').value = profile.limits?.max_audio_mib ?? 32;
      $('heading').textContent = selected.name;
      $('ram').value = profile.host_ram_mib || 8192; $('session').value = JSON.stringify(profile.session_options || {},null,2);
      SuiteModelFields.load('am-defaults', profile.defaults || {});
      $('root').value = profile.path.endsWith('.gguf') ? profile.path.slice(0,profile.path.lastIndexOf('/')) : profile.path;
      companions();
      for (const option of $('companions').options) option.selected = (profile.files || []).includes(option.value.slice($('root').value.length + 1));
      $('form').querySelector('button[type="submit"]').textContent = 'Save model';
    }
  }
  async function runTest(row,button) {
    button.disabled = true; $('test').hidden = false; $('test-title').textContent = 'Testing ' + row.name + '…'; $('test-result').textContent = ''; $('test-audio').removeAttribute('src');
    try {
      const answer = await call('/soundroom-test',{model:row.name,prompt:row.audiocpp.kind === 'music' ? 'short bright piano melody, gentle drums' : 'a single door knock',duration:10});
      $('test-title').textContent = row.name + ' · ' + answer.seconds + ' s'; $('test-result').textContent = JSON.stringify(answer.take?.metrics || answer,null,2);
      if (answer.take?.asset_id) $('test-audio').src = '/soundroom-api/assets/' + encodeURIComponent(answer.take.asset_id) + '/content';
    } finally { button.disabled = false; }
  }
  $('file').addEventListener('change',fileChanged); $('family').addEventListener('change',familyChanged); $('root').addEventListener('change',companions); $('kind').addEventListener('change',exclusive);
  $('form').addEventListener('submit',safe(async event => {
    event.preventDefault(); const file = chosen(), family = $('family').value, root = $('root').value.trim().replace(/\/$/,'');
    if (!file || !family) throw new Error('Choose an audio model file and its compatible runtime family.');
    if (!file.path.startsWith(root + '/')) throw new Error('The model file must be inside its package folder.');
    const parse = field => { try { return JSON.parse($(field).value || '{}'); } catch { throw new Error(field + ' must be valid JSON.'); } };
    const device = SuiteHardware.read('am');
    const settings = {engine:'audiocpp',flags:{model:file.path,'ctx-size':0},gpus:device,
      alone:['music','sound'].includes($('kind').value) || $('alone').checked,
      audiocpp:{...selected?.audiocpp,family,kind:$('kind').value,package:selected?.audiocpp.package || '',
        path:file.path.endsWith('.gguf') ? file.path : root, files:Array.from($('companions').selectedOptions).map(option => option.value.slice(root.length + 1)),
        session_options:parse('session'), defaults:SuiteModelFields.read('am-defaults'),
        limits:{...selected?.audiocpp.limits,max_seconds:Number($('seconds').value),max_takes:Number($('takes').value),max_text_chars:Number($('text').value),max_audio_mib:Number($('audio').value)},host_ram_mib:Number($('ram').value)}};
    const answer = await call('/soundroom-models',{name:$('name').value.trim(),settings,new:!selected,source_machine:file.machine});
    if (answer.kind === 'model-install') { location.href = '/models?kind=audio&install=' + encodeURIComponent(answer.id); return; }
    message('');
    params.set('model', answer.name || $('name').value.trim());
    await refresh(); $('saved').textContent = 'Saved.';
  }));
  refresh().catch(error => message(error.message));
})();
