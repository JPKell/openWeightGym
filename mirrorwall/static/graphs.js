/* Advanced image workflows (Models → Image workflows): the shared Stage lanes editor over /mirrorwall-api/graphs.
   Saved workflows, unsaved drafts and read-only samples share one chooser; drafts stay in page memory across switches. */
(() => {
  'use strict';
  const root = document.getElementById('graphs');
  if (!root) return;
  const G = window.SuiteWorkflowGraph, $ = id => document.getElementById('graph-' + id);
  const editorRoot = $('editor'), clone = value => JSON.parse(JSON.stringify(value));
  let library = null, models = [], editor = null, entries = [], current = -1, language = 'curl', busy = false, opening = false, drafts = 0;
  let running = null;  // the test run being watched: {id, rows, timer}

  function say(text, problem = false) {
    $('error').hidden = !problem; if (problem) $('error').textContent = text;
    $('status').textContent = problem ? '' : text;
  }

  async function api(path, {method = 'GET', body} = {}) {
    const answer = await fetch('/mirrorwall-api/' + path, {method, cache: 'no-store', headers: body ? {'content-type': 'application/json'} : {}, body: body ? JSON.stringify(body) : undefined});
    const found = await answer.json().catch(() => ({}));
    if (!answer.ok) throw new Error(found.error?.message || `The workflow service answered ${answer.status}.`);
    return found;
  }

  const entry = () => entries[current];
  const saved = row => ({key: 'saved:' + row.id, kind: 'saved', id: row.id, version: row.version, published: row.published,
    graph: clone(row.document), problems: row.problems || [], dirty: false});

  /** Read the library; unsaved drafts and edited saved workflows stay as they are. */
  async function load(prefer = null) {
    library = await api('graphs');
    models = (library.models || []).map(one => ({id: one.id, label: one.id, family: one.family, control: Boolean(one.control), exclusive: one.exclusive !== false}));
    const kept = new Map(entries.filter(one => one.dirty || one.kind === 'draft').map(one => [one.key, one]));
    const next = library.workflows.map(row => {
      const before = kept.get('saved:' + row.id);
      return before ? {...before, version: row.version, published: row.published, problems: row.problems || []} : saved(row);
    });
    entries.filter(one => one.kind === 'draft').forEach(one => next.push(one));
    (library.templates || []).forEach((graph, index) => next.push(kept.get('template:' + index) || {key: 'template:' + index, kind: 'template', graph: clone(graph), dirty: false}));
    const key = prefer || entry()?.key;
    entries = next;
    return Math.max(0, entries.findIndex(one => one.key === key));
  }

  function choices() {
    const select = $('choice'), groups = [['saved', 'Saved workflows'], ['draft', 'Not saved yet'], ['template', 'Samples · saving makes your own copy']];
    select.replaceChildren();
    for (const [kind, label] of groups) {
      const members = entries.map((one, index) => [one, index]).filter(([one]) => one.kind === kind);
      if (!members.length) continue;
      const group = document.createElement('optgroup'); group.label = label;
      members.forEach(([one, index]) => group.append(new Option(one.graph.name + (one.dirty ? ' (unsaved)' : '') + (one.kind === 'saved' && one.published ? ' · published v' + one.published : ''), index)));
      select.append(group);
    }
    select.value = String(current);
  }

  function header() {
    const one = entry(); if (!one) return;
    $('name').value = one.graph.name; $('summary').value = one.graph.summary || '';
    $('api-name').textContent = 'workflow:' + G.slug(one.graph.name);
    $('version').textContent = one.kind === 'template' ? 'Sample · read-only' : one.kind === 'draft' ? 'New · not saved' : 'Version ' + one.version + (one.dirty ? ' · unsaved changes' : '');
    $('published').hidden = !(one.kind === 'saved' && one.published);
    $('published').textContent = 'Published · v' + one.published;
    const isSaved = one.kind === 'saved';
    $('publish').hidden = !isSaved || one.published === one.version;
    $('publish').textContent = one.published ? 'Publish v' + one.version : 'Publish';
    $('unpublish').hidden = !isSaved || !one.published;
    $('delete').hidden = !isSaved;
    $('versions').hidden = !isSaved || !one.versions?.length;
    for (const control of ['choice', 'new', 'duplicate', 'save', 'name', 'summary', 'publish', 'unpublish', 'delete', 'versions']) $(control).disabled = busy || !editor;
  }

  function plan() {
    const stages = editor.stages(), loads = stages.filter(stage => stage.load).length;
    const errors = editor.problems().filter(problem => problem.level === 'error').length;
    editorRoot.querySelector('[data-wf-run-plan]').textContent = errors ? `Fix ${errors} problem${errors === 1 ? '' : 's'} before running or publishing.` :
      `Run plan: ${stages.length} step${stages.length === 1 ? '' : 's'} · ${loads} model load${loads === 1 ? '' : 's'}`;
    editorRoot.querySelector('[data-wf-run-button]').disabled = Boolean(errors) || Boolean(running) || busy;
    editorRoot.querySelector('[data-wf-run-stop]').hidden = !running;
  }
  function refresh() {
    header(); choices(); editor.runInputs(); plan();
    editor.apiExample(language, {name: entry().graph.name, base: location.origin, version: null});
  }

  async function versions(one) {
    if (one.kind !== 'saved' || one.versions) return;
    try { one.versions = (await api('graphs/' + encodeURIComponent(one.id))).versions; } catch (error) { one.versions = []; say(error.message, true); }
    if (entry() !== one) return;
    const select = $('versions'); select.replaceChildren(new Option('Versions…', ''));
    one.versions.forEach(row => select.add(new Option('Version ' + row.version + (row.published ? ' · published' : '') + (row.updated ? ' · ' + (window.suiteFormatTime?.(row.updated * 1000, false) ?? new Date(row.updated * 1000).toLocaleString()) : ''), row.version)));
    header();
  }

  function open(index, {quiet = false} = {}) {
    current = index;
    const one = entry();
    opening = true; editor.load(one.graph); opening = false;
    if (!running) { editorRoot.querySelector('[data-wf-stages]').replaceChildren(); editorRoot.querySelector('[data-wf-results]').replaceChildren(); }
    refresh();
    if (!quiet) say(one.kind === 'template' ? 'Samples are read-only templates. Saving creates your own copy.' : '');
    const address = new URL(location.href);
    if (one.kind === 'saved') address.searchParams.set('workflow', one.id); else address.searchParams.delete('workflow');
    address.searchParams.delete('preset');
    history.replaceState(null, '', address);
    versions(one);
  }

  function addDraft(graph, text) {
    drafts += 1;
    entries.push({key: 'draft:' + drafts, kind: 'draft', graph, dirty: true});
    open(entries.length - 1, {quiet: true}); say(text);
  }

  /** A new workflow starts with the usual steps, connected automatically. */
  function blank() {
    const graph = {schema: 1, name: 'Untitled workflow', summary: '', family: '', media: 'image', nodes: [], links: []};
    const catalogue = library.catalogue;
    G.add(graph, catalogue, 'prompt', {models});
    G.add(graph, catalogue, 'model', {values: {model: models[0]?.id || ''}, models});
    G.add(graph, catalogue, 'seed', {models}); G.add(graph, catalogue, 'size', {models});
    const family = models[0] && catalogue.defaults[models[0].family], values = {...(family || {})}; delete values.basis;
    G.add(graph, catalogue, 'sampler', {values, models}); G.add(graph, catalogue, 'output', {values: {label: 'Final'}, models});
    return graph;
  }

  async function act(work) {
    if (busy) return;
    busy = true; header(); if (editor) plan();
    try { await work(); } catch (error) { say(error.message, true); }
    finally { busy = false; header(); if (editor && entry()) plan(); }
  }

  async function save() {
    const one = entry(), document = {...clone(one.graph), ...editor.graph(), name: one.graph.name, summary: one.graph.summary || ''};
    delete document.id;
    const body = one.kind === 'saved' ? {id: one.id, version: one.version, document} : {document};
    const row = await api('graphs', {method: 'POST', body});
    // A saved draft leaves the drafts; an edited sample returns to its shipped state when the library reloads.
    if (one.kind === 'draft') entries.splice(current, 1);
    else one.dirty = false;
    current = await load('saved:' + row.id);
    open(current, {quiet: true});
    const errors = (row.problems || []).filter(problem => problem.level === 'error').length;
    say(`Saved version ${row.version}.` + (errors ? ` Fix ${errors} problem${errors === 1 ? '' : 's'} before publishing.` : row.published ? ` Callers still run version ${row.published} until you publish this one.` : ' Publish it when callers should use it.'));
  }

  async function publish(version) {
    const one = entry();
    if (one.dirty && version) throw new Error('Save your changes as a version before publishing.');
    const row = await api('graphs/' + encodeURIComponent(one.id) + '/publish', {method: 'POST', body: {version, expected_version: one.version}});
    one.published = row.published; one.versions = null;
    current = await load(one.key); open(current, {quiet: true});
    say(version ? `Published version ${version}. Callers who may use every model it uses can run workflow:${G.slug(one.graph.name)}.` : 'Unpublished. Only workflow managers can see it.');
  }

  async function remove() {
    const one = entry();
    await api('graphs/' + encodeURIComponent(one.id) + '?version=' + one.version, {method: 'DELETE'});
    entries.splice(current, 1);
    current = await load(entries[0]?.key); open(current, {quiet: true});
    say(`Deleted “${one.graph.name}”. Images and job records it produced stay available.`);
  }

  /** Start a test run: the saved version as it is, or the workflow on screen when it has unsaved changes. */
  async function run() {
    // While a save, publish or deletion settles, the workflow on screen is being replaced; wait for it (QA-080).
    if (running || busy || !entry()) return;
    let inputs;
    try { inputs = editor.runValues(); } catch (error) { say(error.message, true); return; }
    const one = entry(), saved = one.kind === 'saved' && !one.dirty;
    const body = saved ? {version: one.version, inputs} : {document: {...clone(one.graph), ...editor.graph(), name: one.graph.name, summary: one.graph.summary || ''}, inputs};
    delete body.document?.id;
    const stages = editor.stages(), list = editorRoot.querySelector('[data-wf-stages]');
    editorRoot.querySelector('[data-wf-results]').replaceChildren();
    const rows = Object.fromEntries(stages.map(stage => {
      const row = document.createElement('li'); row.className = 'wf-stage'; row.dataset.state = 'waiting'; row.dataset.step = stage.id;
      const name = document.createElement('span'); name.textContent = stage.node.title + (stage.model ? ' · ' + stage.model.label : '');
      const status = document.createElement('span'); status.className = 'small text-secondary'; status.textContent = stage.load ? 'loads model' : 'waiting';
      const bar = document.createElement('progress'); bar.max = 100; bar.value = 0; bar.setAttribute('aria-label', stage.node.title + ' progress');
      row.append(name, status, bar);
      return [stage.id, {row, status, bar}];
    }));
    list.replaceChildren(...Object.values(rows).map(one => one.row));
    running = {id: null, rows, shown: 0}; plan();
    try {
      const state = await api('graphs/' + (one.kind === 'saved' ? encodeURIComponent(one.id) : 'draft') + '/run', {method: 'POST', body});
      running.id = state.id;
      say(saved ? `Running version ${one.version}. Outputs are saved to your image library.` : 'Running the workflow as shown, unsaved changes included. Outputs are saved to your image library.');
      watch();
    } catch (error) { running = null; plan(); say(error.message, true); }
  }

  async function watch() {
    if (!running?.id) return;
    let state;
    try { state = await api('graph-runs/' + encodeURIComponent(running.id)); }
    catch (error) { running.timer = setTimeout(watch, 2000); return; }
    for (const step of Object.values(state.steps || {})) {
      const one = running.rows[step.step]; if (!one) continue;
      one.row.dataset.state = step.state === 'completed' ? 'done' : 'running';
      one.bar.value = step.state === 'completed' ? 100 : Math.max(one.bar.value, 5);
      one.status.textContent = step.state === 'completed' ? `done${step.width ? ' · ' + step.width + ' × ' + step.height : ''}${step.node ? ' · ' + step.node : ''}` : 'running';
    }
    const last = [...(state.events || [])].reverse().find(event => event.type === 'workflow.progress');
    const active = last && running.rows[last.step];
    if (active && active.row.dataset.state === 'running') {
      if (last.step_index !== undefined && last.total_steps) active.bar.value = Math.round(100 * last.step_index / last.total_steps);
      active.status.textContent = last.stage || 'running';
    }
    const results = editorRoot.querySelector('[data-wf-results]');
    for (const output of (state.outputs || []).slice(running.shown)) {
      const figure = document.createElement('figure'), link = document.createElement('a'), image = document.createElement('img'), caption = document.createElement('figcaption');
      figure.className = 'wf-result';
      link.href = '/mirrorwall-api/assets/' + encodeURIComponent(output.asset_id) + '/content'; link.target = '_blank'; link.rel = 'noopener';
      image.src = '/mirrorwall-api/assets/' + encodeURIComponent(output.asset_id) + '/thumbnail'; image.alt = output.output; image.loading = 'lazy';
      caption.textContent = output.output + (output.seed !== null && output.seed !== undefined ? ' · seed ' + output.seed : '') + ' · ' + output.width + ' × ' + output.height;
      link.append(image); figure.append(link, caption); results.append(figure);
    }
    running.shown = (state.outputs || []).length;
    if (state.status === 'running') { running.timer = setTimeout(watch, 700); return; }
    for (const one of Object.values(running.rows)) {
      if (one.row.dataset.state !== 'done') { one.row.dataset.state = state.status === 'completed' ? 'done' : 'failed'; one.status.textContent = state.status === 'completed' ? 'not needed' : state.status; }
    }
    running = null; plan();
    if (state.status === 'completed') say(`Test run complete: ${state.outputs.length} image${state.outputs.length === 1 ? '' : 's'} saved to your library.`);
    else if (state.status === 'stopped') say('Stopped. Images already finished stay in your library.');
    else say(state.error || 'The test run failed.', true);
  }

  async function stop() {
    if (!running?.id) return;
    try { await api('graph-runs/' + encodeURIComponent(running.id) + '/stop', {method: 'POST'}); } catch (error) { say(error.message, true); }
    clearTimeout(running.timer); watch();
  }

  function wire() {
    $('choice').addEventListener('change', () => open(Number($('choice').value)));
    $('name').addEventListener('input', () => {
      const one = entry(); one.graph.name = $('name').value.trim() || 'Untitled workflow'; one.dirty = true;
      header(); choices(); editor.apiExample(language, {name: one.graph.name, base: location.origin});
    });
    $('summary').addEventListener('input', () => { const one = entry(); one.graph.summary = $('summary').value; one.dirty = true; header(); choices(); });
    $('new').addEventListener('click', () => addDraft(blank(), 'A new workflow starts with the usual steps, already connected.'));
    $('duplicate').addEventListener('click', () => {
      const one = entry();
      addDraft({...clone(one.graph), ...editor.graph(), name: one.graph.name + ' copy'}, 'Duplicated into a new workflow. Save it to keep it.');
    });
    $('save').addEventListener('click', () => act(save));
    $('publish').addEventListener('click', () => act(() => publish(entry().version)));
    $('unpublish').addEventListener('click', () => act(() => publish(0)));
    $('delete').addEventListener('click', () => act(remove));
    $('versions').addEventListener('change', () => {
      const one = entry(), row = one.versions?.find(item => String(item.version) === $('versions').value);
      $('versions').value = '';
      if (!row) return;
      one.graph = clone(row.document); one.dirty = row.version !== one.version;
      opening = true; editor.load(one.graph); opening = false; refresh();
      say(row.version === one.version ? `Showing the latest version ${row.version}.` : `Opened version ${row.version}. Saving makes it version ${one.version + 1}; version ${row.version} stays in the history.`);
    });
    editorRoot.querySelector('[data-wf-run-form]').addEventListener('submit', event => { event.preventDefault(); run(); });
    editorRoot.querySelector('[data-wf-run-stop]').addEventListener('click', stop);
    editorRoot.querySelectorAll('[data-wf-api-language]').forEach(button => button.addEventListener('click', () => {
      language = button.dataset.wfApiLanguage;
      editorRoot.querySelectorAll('[data-wf-api-language]').forEach(one => { one.classList.toggle('active', one === button); one.setAttribute('aria-pressed', String(one === button)); });
      editor.apiExample(language, {name: entry().graph.name, base: location.origin});
    }));
    editorRoot.querySelector('[data-wf-api-copy]').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(editorRoot.querySelector('[data-wf-api-code]').textContent); say('Copied the API example.'); }
      catch { say('Copy is unavailable here; select the example text instead.'); }
    });
    window.addEventListener('beforeunload', event => { if (entries.some(one => one.dirty && one.kind !== 'template')) event.preventDefault(); });
  }

  async function start() {
    try {
      current = await load();
      if (!library.can_manage) throw new Error('Your account may not manage image workflows.');
      const opened = root.dataset.open ? entries.findIndex(one => one.key === 'saved:' + root.dataset.open) : -1;
      if (opened >= 0) current = opened;
      // Your reference images, for Load image steps in test runs; the page still works without them.
      const assets = await api('assets?kind=reference').then(found => (found.assets || []).map(one => ({id: one.id, label: one.filename || one.id}))).catch(() => []);
      editor = SuiteWorkflowEditor.create(editorRoot, {catalogue: library.catalogue, models, assets, upscalers: library.upscalers ?? null, graph: entry().graph,
        onChange: (graph, how = {}) => {
          if (opening) return;
          const one = entry(); one.graph = {...one.graph, nodes: graph.nodes, links: graph.links};
          if (!how.layout) one.dirty = true;
          header(); choices(); editor.runInputs(); plan(); editor.apiExample(language, {name: one.graph.name, base: location.origin});
        }});
      wire();
      open(current, {quiet: true});
      say(entries.some(one => one.kind === 'saved') ? '' : 'No saved workflows yet. Start from a sample or New workflow.');
      if (library.upscalers === null) say('The upscaler inventory could not be read, so installed upscalers are not checked or offered.');
      if (root.dataset.preset) {
        const presets = (await api('workflows')).presets || [], preset = presets.find(one => one.id === root.dataset.preset);
        if (!preset) throw new Error('That simple workflow is not available to you.');
        const converted = G.fromPreset(preset, library.catalogue, models), notes = converted.nodes.filter(one => one.type === 'note').length;
        addDraft(converted, `Converted “${preset.name}” into a ${converted.nodes.some(one => one.type === 'upscale') ? 'sampler and upscale' : 'one-sampler'} workflow.` +
          (notes ? ` Read its ${notes === 1 ? 'note' : notes + ' notes'} about settings it could not carry over.` : '') + ' Save it to keep it; the simple workflow is unchanged.');
      }
    } catch (error) { say(error.message, true); }
    finally { header(); }
  }
  start();
})();
