/* Image workflow proposals: three layouts over one shared editor, with tab-local drafts and a simulated test run. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const data = id => JSON.parse($(id).textContent);
  const G = window.SuiteWorkflowGraph;
  const catalogue = data('workflow-catalogue'), models = data('workflow-models'), assets = data('workflow-assets'), upscalers = data('workflow-upscalers');
  const clone = value => JSON.parse(JSON.stringify(value));
  const NOTES = {
    canvas: 'Node canvas: a free canvas between the palette and the inspector. Drag nodes or use Alt + arrows; links follow Send to choices.',
    lanes: 'Stage lanes (selected): each lane runs after the ones to its left. Drag a card by its title, or press Alt and an arrow, to move it between lanes or reorder a lane; Tidy layout restores run order.',
    split: 'Split studio: a compact canvas above the test run, for tuning a saved workflow against real inputs.',
  };
  let workflows = [], current = 0, running = null, language = 'curl';
  const editorRoot = $('editor');

  function fresh() {
    workflows = data('workflow-samples').map((graph, index) => ({graph: clone(graph), version: 0, sample: true, published: true, id: 'sample-' + index}));
    current = 0;
  }
  fresh();
  const editor = SuiteWorkflowEditor.create(editorRoot, {catalogue, models, assets, upscalers, graph: workflows[0].graph,
    onChange: (graph, how = {}) => { workflows[current].graph = {...workflows[current].graph, nodes: graph.nodes, links: graph.links}; if (!how.layout) workflows[current].dirty = true; refresh(); }});

  function announce(text) { $('feedback').hidden = !text; $('feedback-text').textContent = text || ''; }

  function choices() {
    const select = $('workflow-choice');
    select.replaceChildren();
    workflows.forEach((one, index) => select.add(new Option((one.sample ? 'Sample · ' : '') + one.graph.name + (one.dirty ? ' (unsaved)' : ''), index)));
    select.value = String(current);
  }

  function header() {
    const one = workflows[current];
    $('workflow-name').value = one.graph.name; $('workflow-summary').value = one.graph.summary || '';
    $('api-name').textContent = 'workflow:' + G.slug(one.graph.name);
    $('workflow-version').textContent = one.sample ? 'Sample · read-only, duplicate to keep changes' : one.version ? 'Version ' + one.version + (one.dirty ? ' · unsaved changes' : '') : 'New · not saved';
    $('workflow-published').checked = one.published; $('workflow-published').disabled = one.sample;
  }

  function runInputs() { editor.runInputs(); }
  function apiExample() { editor.apiExample(language, {name: workflows[current].graph.name, base: 'https://gym.example'}); }

  function plan() {
    const stages = editor.stages(), loads = stages.filter(stage => stage.load).length;
    const errors = editor.problems().filter(problem => problem.level === 'error').length;
    editorRoot.querySelector('[data-wf-run-plan]').textContent = errors ? 'Fix the problems above to run.' :
      `${stages.length} step${stages.length === 1 ? '' : 's'} · ${loads} model load${loads === 1 ? '' : 's'}`;
    editorRoot.querySelector('[data-wf-run-button]').disabled = Boolean(errors) || Boolean(running);
  }

  function refresh() { header(); choices(); runInputs(); apiExample(); plan(); }

  function open(index) {
    current = index; editor.load(workflows[index].graph);
    workflows[index].dirty = false; editorRoot.querySelector('[data-wf-stages]').replaceChildren(); editorRoot.querySelector('[data-wf-results]').replaceChildren();
    refresh(); announce('');
  }

  /** A deterministic placeholder picture for a simulated result. */
  function picture(seed, label) {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 256;
    const context = canvas.getContext('2d');
    let state = 0; for (const char of String(seed) + label) state = (state * 31 + char.charCodeAt(0)) >>> 0;
    const random = () => ((state = (state * 1664525 + 1013904223) >>> 0) / 2 ** 32);
    const gradient = context.createLinearGradient(0, 0, 256, 256);
    gradient.addColorStop(0, `hsl(${random() * 360} 55% 55%)`); gradient.addColorStop(1, `hsl(${random() * 360} 60% 30%)`);
    context.fillStyle = gradient; context.fillRect(0, 0, 256, 256);
    for (let index = 0; index < 7; index++) {
      context.globalAlpha = .35; context.fillStyle = `hsl(${random() * 360} 70% 70%)`;
      context.beginPath(); context.arc(random() * 256, random() * 256, 20 + random() * 70, 0, Math.PI * 2); context.fill();
    }
    context.globalAlpha = 1; canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', 'Placeholder for ' + label);
    return canvas;
  }

  function run(event) {
    event.preventDefault();
    if (running) return;
    const graph = editor.graph(), stages = editor.stages();
    let values;
    try { values = editor.runValues(); } catch (error) { announce(error.message); return; }
    const seedNode = graph.nodes.find(node => node.type === 'seed');
    const seed = Number(values[(seedNode?.id || '') + '.value'] ?? seedNode?.values.value ?? Math.floor(Math.random() * 2 ** 31));
    const list = editorRoot.querySelector('[data-wf-stages]'), results = editorRoot.querySelector('[data-wf-results]');
    list.replaceChildren(); results.replaceChildren(); announce('');
    const rows = stages.map(stage => {
      const row = document.createElement('li'); row.className = 'wf-stage'; row.dataset.state = 'waiting';
      const name = document.createElement('span'); name.textContent = stage.node.title + (stage.model ? ' · ' + stage.model.label : '');
      const status = document.createElement('span'); status.className = 'small text-secondary'; status.textContent = stage.load ? 'loads model' : 'waiting';
      const bar = document.createElement('progress'); bar.max = 100; bar.value = 0; bar.setAttribute('aria-label', stage.node.title + ' progress');
      row.append(name, status, bar); list.append(row); return {stage, row, status, bar};
    });
    const images = {};
    running = {stop: false}; editorRoot.querySelector('[data-wf-run-stop]').hidden = false; plan();
    let index = 0, tick = 0;
    const timer = setInterval(() => {
      if (running.stop) {
        clearInterval(timer); rows.slice(index).forEach(one => { one.row.dataset.state = 'failed'; one.status.textContent = 'stopped'; });
        finish('Stopped. Completed steps keep their images.'); return;
      }
      const one = rows[index];
      if (!one) { clearInterval(timer); outputs(graph, images, results, seed); finish('Test run complete. Nothing was sent to a GPU; images are placeholders.'); return; }
      one.row.dataset.state = 'running'; tick += 25; one.bar.value = tick; one.status.textContent = `step ${Math.round(tick / 100 * (one.stage.node.values.steps || 1))}`;
      if (tick >= 100) {
        one.row.dataset.state = 'done'; one.status.textContent = 'done';
        images[one.stage.id] = picture(seed, one.stage.id);
        index++; tick = 0;
      }
    }, 60);
  }
  function source(graph, id, images) {  // the step whose image reaches this node, through plain components
    let link = graph.links.find(one => one.to[0] === id);
    while (link && !images[link.from[0]]) link = graph.links.find(one => one.to[0] === link.from[0]);
    return link ? link.from[0] : null;
  }
  function outputs(graph, images, results, seed) {
    for (const node of graph.nodes.filter(one => ['output', 'compare'].includes(one.type))) {
      const feeds = graph.links.filter(link => link.to[0] === node.id).map(link => images[link.from[0]] ? link.from[0] : source(graph, link.from[0], images)).filter(Boolean);
      for (const id of feeds) {
        const figure = document.createElement('figure'); figure.className = 'wf-result';
        const caption = document.createElement('figcaption');
        caption.textContent = node.values.label + ' · ' + (graph.nodes.find(one => one.id === id)?.title || id) + ' · seed ' + seed;
        figure.append(picture(seed, id), caption); results.append(figure);
      }
    }
  }
  function finish(text) {
    running = null; editorRoot.querySelector('[data-wf-run-stop]').hidden = true; plan(); announce(text);
  }

  function layout(name) {
    $('workspace').dataset.layout = name;
    if (editorRoot.dataset.wfView === 'outline') editorRoot.dataset.wfLayout = name === 'lanes' ? 'lanes' : 'canvas';
    else editor.view(name === 'lanes' ? 'lanes' : 'canvas');
    document.querySelectorAll('[data-design]').forEach(button => { const on = button.dataset.design === name; button.classList.toggle('active', on); button.setAttribute('aria-pressed', String(on)); });
    $('design-note').textContent = NOTES[name];
  }

  document.querySelectorAll('[data-design]').forEach(button => button.addEventListener('click', () => layout(button.dataset.design)));
  $('workflow-choice').addEventListener('change', () => open(Number($('workflow-choice').value)));
  $('workflow-name').addEventListener('input', () => { workflows[current].graph.name = $('workflow-name').value.trim() || 'Untitled workflow'; workflows[current].dirty = true; header(); choices(); apiExample(); });
  $('workflow-summary').addEventListener('input', () => { workflows[current].graph.summary = $('workflow-summary').value; workflows[current].dirty = true; });
  $('workflow-published').addEventListener('change', () => { workflows[current].published = $('workflow-published').checked; announce(workflows[current].published ? 'Published in this tab: callers who may use every model can run it.' : 'Unpublished: only workflow managers can see it.'); });
  $('new-workflow').addEventListener('click', () => {
    const graph = {schema: 1, name: 'Untitled workflow', summary: '', family: '', media: 'image', nodes: [], links: []};
    workflows.push({graph, version: 0, sample: false, published: false, id: 'draft-' + workflows.length});
    open(workflows.length - 1);
    ['prompt', 'model', 'seed', 'size', 'sampler', 'output'].forEach(type => editorRoot.querySelector(`[data-add="${type}"]`).click());
    announce('A new workflow starts with the usual components, already connected.');
  });
  $('duplicate-workflow').addEventListener('click', () => {
    const source = workflows[current], graph = {...clone(source.graph), ...editor.graph(), name: source.graph.name + ' copy'};
    workflows.push({graph, version: 0, sample: false, published: false, id: 'copy-' + workflows.length});
    open(workflows.length - 1); announce('Duplicated into an editable copy.');
  });
  $('save-workflow').addEventListener('click', () => {
    const one = workflows[current];
    if (one.sample) { announce('Samples are read-only. Duplicate it to save your own version.'); return; }
    const errors = editor.problems().filter(problem => problem.level === 'error');
    if (errors.length) { announce('Fix ' + errors.length + ' problem' + (errors.length === 1 ? '' : 's') + ' before saving a version.'); return; }
    one.version++; one.dirty = false; refresh(); announce(`Saved version ${one.version} in this tab. API callers can pin it as workflow:${G.slug(one.graph.name)}@${one.version}.`);
  });
  editorRoot.querySelector('[data-wf-run-form]').addEventListener('submit', run);
  editorRoot.querySelector('[data-wf-run-stop]').addEventListener('click', () => { if (running) running.stop = true; });
  editorRoot.querySelectorAll('[data-wf-api-language]').forEach(button => button.addEventListener('click', () => {
    language = button.dataset.wfApiLanguage;
    editorRoot.querySelectorAll('[data-wf-api-language]').forEach(one => { one.classList.toggle('active', one === button); one.setAttribute('aria-pressed', String(one === button)); });
    apiExample();
  }));
  editorRoot.querySelector('[data-wf-api-copy]').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(editorRoot.querySelector('[data-wf-api-code]').textContent); announce('Copied the API example.'); }
    catch { announce('Copy is unavailable here; select the example text instead.'); }
  });
  $('reset').addEventListener('click', () => { fresh(); layout('lanes'); open(0); });
  $('theme').addEventListener('click', () => {
    const dark = document.documentElement.dataset.bsTheme !== 'dark', control = document.querySelector('[data-theme-form] [name="mode"]');
    control.value = dark ? 'dark' : 'light'; control.dispatchEvent(new Event('change', {bubbles: true}));
    $('theme').textContent = dark ? 'Light mode' : 'Dark mode';
  });
  $('theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode';
  layout('lanes'); refresh();
})();
