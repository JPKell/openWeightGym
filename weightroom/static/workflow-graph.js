/* Image workflow graphs: typed links, cycle checks, automatic binding, validation, stage order and API examples.
   Pure functions over a graph document and the catalogue from mirrorwall/graph.py; shared by the editor and previews. */
window.SuiteWorkflowGraph = (() => {
  'use strict';
  const spec = (catalogue, type) => catalogue.components[type] || {label: type, inputs: [], outputs: [], fields: []};
  const inputs = (catalogue, node) => spec(catalogue, node.type).inputs || [];
  const outputs = (catalogue, node) => spec(catalogue, node.type).outputs || [];
  const find = (graph, id) => graph.nodes.find(node => node.id === id);
  const same = (a, b) => a[0] === b[0] && a[1] === b[1];
  const linksInto = (graph, id, port) => graph.links.filter(link => link.to[0] === id && link.to[1] === port);
  const linksFrom = (graph, id, port) => graph.links.filter(link => link.from[0] === id && (!port || link.from[1] === port));
  const portOf = (list, name) => list.find(port => port.name === name);

  /** True when `to` can already reach `from`, so a link from→to would close a loop. */
  function reaches(graph, start, goal) {
    const seen = new Set(), queue = [start];
    while (queue.length) {
      const id = queue.shift();
      if (id === goal) return true;
      if (seen.has(id)) continue;
      seen.add(id);
      graph.links.filter(link => link.from[0] === id).forEach(link => queue.push(link.to[0]));
    }
    return false;
  }

  /** Every input a node's output could feed, with its state: linked, replaceable or refused and why. */
  function destinations(graph, catalogue, id, port) {
    const source = find(graph, id), type = portOf(outputs(catalogue, source), port)?.type, rows = [];
    for (const node of graph.nodes) {
      if (node.id === id) continue;
      for (const input of inputs(catalogue, node)) {
        if (input.type !== type) continue;
        const current = linksInto(graph, node.id, input.name);
        const linked = current.some(link => same(link.from, [id, port]));
        const cycle = !linked && reaches(graph, node.id, id);
        const other = !input.multiple && !linked && current[0] ? find(graph, current[0].from[0]) : null;
        rows.push({node, input, linked, disabled: cycle ? 'Would create a loop' : '', replaces: other ? other.title : ''});
      }
    }
    return rows;
  }

  /** Every output that could feed one input, excluding loops. */
  function sources(graph, catalogue, id, port) {
    const target = find(graph, id), type = portOf(inputs(catalogue, target), port)?.type, rows = [];
    for (const node of graph.nodes) {
      if (node.id === id) continue;
      for (const output of outputs(catalogue, node)) {
        if (output.type === type && !reaches(graph, id, node.id)) rows.push({node, output});
      }
    }
    return rows;
  }

  /** Link an output to an input; a single-source input drops its previous source. */
  function connect(graph, catalogue, from, to, auto = false) {
    const target = find(graph, to[0]), input = portOf(inputs(catalogue, target), to[1]);
    const source = find(graph, from[0]), output = portOf(outputs(catalogue, source), from[1]);
    if (!input || !output || input.type !== output.type) throw new Error('Those ports carry different kinds of data.');
    if (from[0] === to[0] || reaches(graph, to[0], from[0])) throw new Error('That link would create a loop.');
    if (graph.links.some(link => same(link.from, from) && same(link.to, to))) return graph;
    if (!input.multiple) graph.links = graph.links.filter(link => !same(link.to, to));
    graph.links.push({from: [...from], to: [...to], ...(auto ? {auto: true} : {})});
    return graph;
  }

  function disconnect(graph, from, to) {
    graph.links = graph.links.filter(link => !(same(link.from, from) && same(link.to, to)));
    return graph;
  }

  function uniqueId(graph, type) {
    let index = 1;
    while (graph.nodes.some(node => node.id === type + index)) index++;
    return type + index;
  }

  /** Bind a new node's empty inputs: required ones, seed and size from the only or earliest source,
      then one input from the `preferred` node, skipping inputs the step's model family cannot use. */
  function autoBind(graph, catalogue, node, preferred, models = []) {
    for (const input of inputs(catalogue, node)) {
      if (!(input.required || ['seed', 'size', 'model'].includes(input.type)) || linksInto(graph, node.id, input.name).length) continue;
      const choices = sources(graph, catalogue, node.id, input.name);
      const pick = choices.find(one => one.node.id === preferred) || (choices.length === 1 ? choices[0] : null) ||
        choices.find(one => !spec(catalogue, one.node.type).inputs?.length);
      if (pick) connect(graph, catalogue, [pick.node.id, pick.output.name], [node.id, input.name], true);
    }
    if (!preferred || linksFrom(graph, preferred).some(link => link.to[0] === node.id)) return;
    const model = modelOf(graph, node.id, models), family = model && catalogue.families[model.family];
    for (const input of inputs(catalogue, node)) {
      if (linksInto(graph, node.id, input.name).length) continue;
      if (family && ((input.name === 'init' && !family.init) || (input.name === 'references' && !family.references))) continue;
      const pick = sources(graph, catalogue, node.id, input.name).find(one => one.node.id === preferred);
      if (pick) { connect(graph, catalogue, [pick.node.id, pick.output.name], [node.id, input.name], true); return; }
    }
  }

  /** Add a component with its default values and automatic links. */
  function add(graph, catalogue, type, {near = null, at = null, values = {}, models = []} = {}) {
    const definition = spec(catalogue, type), id = uniqueId(graph, type);
    const anchor = near ? find(graph, near) : null;
    const node = {id, type, title: definition.label + (graph.nodes.some(one => one.type === type) ? ' ' + id.replace(type, '') : ''),
      values: Object.fromEntries((definition.fields || []).map(field => [field.name, field.default])),
      exposed: [...(definition.exposed || [])], overridable: {},
      at: at || (anchor ? [anchor.at[0] + 340, anchor.at[1] + 40] : [40 + 40 * (graph.nodes.length % 8), 40 + 30 * (graph.nodes.length % 8)])};
    Object.assign(node.values, values);
    graph.nodes.push(node);
    autoBind(graph, catalogue, node, near, models);
    return node;
  }

  /** Insert a component after `id`: it takes `id`'s output and passes it on to `id`'s former destinations. */
  function insertAfter(graph, catalogue, id, type, models = []) {
    const before = find(graph, id), node = add(graph, catalogue, type, {near: id, models});
    for (const output of outputs(catalogue, before)) {
      const into = inputs(catalogue, node).find(input => input.type === output.type);
      const out = outputs(catalogue, node).find(one => one.type === output.type);
      if (!into || !out) continue;
      const moved = linksFrom(graph, id, output.name).filter(link => link.to[0] !== node.id);
      graph.links = graph.links.filter(link => !moved.includes(link));
      connect(graph, catalogue, [id, output.name], [node.id, into.name]);
      moved.forEach(link => graph.links.push({from: [node.id, out.name], to: link.to}));
      for (const later of graph.nodes) if (later.at[0] >= node.at[0] && later.id !== node.id && moved.some(link => link.to[0] === later.id)) later.at = [later.at[0] + 340, later.at[1]];
    }
    return node;
  }

  function remove(graph, id) {
    graph.nodes = graph.nodes.filter(node => node.id !== id);
    graph.links = graph.links.filter(link => link.from[0] !== id && link.to[0] !== id);
    return graph;
  }

  /** Topological order; nodes in a loop (which links refuse) are appended last. */
  function order(graph) {
    const incoming = new Map(graph.nodes.map(node => [node.id, 0]));
    graph.links.forEach(link => incoming.set(link.to[0], (incoming.get(link.to[0]) || 0) + 1));
    const ready = graph.nodes.filter(node => !incoming.get(node.id)).map(node => node.id), result = [];
    while (ready.length) {
      const id = ready.shift(); result.push(id);
      linksFrom(graph, id).forEach(link => { incoming.set(link.to[0], incoming.get(link.to[0]) - 1); if (!incoming.get(link.to[0])) ready.push(link.to[0]); });
    }
    return [...result, ...graph.nodes.map(node => node.id).filter(id => !result.includes(id))];
  }

  /** Column of each node for lane layouts: one more than its deepest source. */
  function depths(graph) {
    const depth = {};
    for (const id of order(graph)) depth[id] = Math.max(0, ...graph.links.filter(link => link.to[0] === id).map(link => (depth[link.from[0]] ?? 0) + 1));
    return depth;
  }

  /** Lane and row of each node: a node stays in the lane its author chose (`lane[0]`), but never
      before or beside a source; rows follow the author's order (`lane[1]`), then run order. */
  function lanes(graph) {
    const column = {}, ids = order(graph);
    for (const id of ids) {
      const earliest = Math.max(0, ...graph.links.filter(link => link.to[0] === id).map(link => (column[link.from[0]] ?? 0) + 1));
      const chosen = find(graph, id)?.lane?.[0];
      column[id] = Math.max(earliest, Number.isFinite(chosen) ? chosen : 0);
    }
    const rank = Object.fromEntries(ids.map((id, index) => [id, index])), result = {}, byColumn = {};
    ids.forEach(id => (byColumn[column[id]] ||= []).push(id));
    for (const [lane, members] of Object.entries(byColumn)) {
      const key = id => { const row = find(graph, id)?.lane?.[1]; return Number.isFinite(row) ? row : rank[id]; };
      members.sort((a, b) => key(a) - key(b) || rank[a] - rank[b]).forEach((id, index) => { result[id] = [Number(lane), index]; });
    }
    return result;
  }

  /** The earliest lane a node may occupy: one after its deepest source. */
  function earliestLane(graph, id, placed = lanes(graph)) {
    return Math.max(0, ...graph.links.filter(link => link.to[0] === id).map(link => placed[link.from[0]][0] + 1));
  }

  /** Move a node to a lane and row; rows in that lane are renumbered so the order is explicit.
      Returns the lane actually used, which is never before the node's sources. */
  function moveToLane(graph, id, lane, row) {
    const placed = lanes(graph), target = Math.max(earliestLane(graph, id, placed), Math.max(0, Math.round(lane)));
    const members = Object.keys(placed).filter(one => one !== id && placed[one][0] === target).sort((a, b) => placed[a][1] - placed[b][1]);
    members.splice(Math.max(0, Math.min(members.length, Math.round(row))), 0, id);
    members.forEach((one, index) => { const node = find(graph, one); node.lane = [one === id ? target : node.lane?.[0] ?? null, index]; });
    return target;
  }

  function clearLanes(graph) { graph.nodes.forEach(node => { delete node.lane; }); return graph; }

  /** The model a step uses, through its Model input. */
  function modelOf(graph, id, models) {
    const link = linksInto(graph, id, 'model')[0], source = link && find(graph, link.from[0]);
    return source ? models.find(model => model.id === source.values.model) || null : null;
  }

  /** Run order of executable steps, keeping steps on one model together where dependencies allow. */
  function stages(graph, catalogue, models) {
    const executable = new Set(graph.nodes.filter(node => spec(catalogue, node.type).stage).map(node => node.id));
    const needs = id => {  // executable steps this one waits for, through any chain of plain nodes
      const found = new Set(), queue = graph.links.filter(link => link.to[0] === id).map(link => link.from[0]);
      while (queue.length) { const one = queue.shift(); if (found.has(one)) continue; found.add(one); queue.push(...graph.links.filter(link => link.to[0] === one).map(link => link.from[0])); }
      return [...found].filter(one => executable.has(one));
    };
    const waiting = new Map([...executable].map(id => [id, needs(id)])), done = [], result = [];
    let current = null;
    while (waiting.size) {
      const ready = [...waiting.keys()].filter(id => waiting.get(id).every(one => done.includes(one)));
      if (!ready.length) break;
      ready.sort((a, b) => order(graph).indexOf(a) - order(graph).indexOf(b));
      const next = ready.find(id => (modelOf(graph, id, models)?.id || null) === current) || ready[0];
      const model = modelOf(graph, next, models);
      result.push({id: next, node: find(graph, next), model, load: Boolean(model && model.id !== current)});
      if (model) current = model.id;
      done.push(next); waiting.delete(next);
    }
    return result;
  }

  /** Problems that block a run (error) or deserve attention (warning), per node. */
  function validate(graph, catalogue, models, upscalers = null) {
    const latent = catalogue.hires_upscalers || [];
    const problems = [], push = (node, level, text) => problems.push({node: node?.id || null, level, text});
    if (graph.nodes.length > catalogue.limits.nodes) push(null, 'error', `Use at most ${catalogue.limits.nodes} components.`);
    if (graph.links.length > catalogue.limits.links) push(null, 'error', `Use at most ${catalogue.limits.links} links.`);
    if (!graph.nodes.some(node => ['output', 'compare'].includes(node.type))) push(null, 'error', 'Add an Output so the run saves something.');
    const labels = new Set();
    for (const node of graph.nodes) {
      const definition = spec(catalogue, node.type);
      if (labels.has(node.id)) push(node, 'error', 'Two components share the label ' + node.id + '.');
      labels.add(node.id);
      if (definition.phase > (catalogue.runnable_phase ?? 4)) push(node, 'warning', `${definition.label} is planned for Phase ${definition.phase}; it cannot run yet.`);
      for (const input of definition.inputs || []) {
        if (input.required && !linksInto(graph, node.id, input.name).length) push(node, 'error', `${input.label} needs a source.`);
      }
      if (node.type === 'model' && !models.some(model => model.id === node.values.model)) push(node, 'error', 'Choose an installed image model.');
      if (node.type === 'upscale' && !node.values.upscaler) push(node, 'error', 'Choose an upscaler.');
      const wanted = node.type === 'upscale' ? node.values.upscaler : node.type === 'sampler' && node.values.hires ? node.values.hires_upscaler : '';
      if (upscalers && wanted && !latent.includes(wanted) && !upscalers.some(one => one.id === wanted)) push(node, 'error', 'That upscaler is not installed on any node.');
      if (node.type === 'sampler' && node.values.hires && !latent.includes(node.values.hires_upscaler)) push(node, 'error', 'A hires pass with an upscaler file cannot run yet; choose a latent method or add an Upscale step.');
      if (node.type === 'tiled_detail') {
        const model = modelOf(graph, node.id, models), family = model && catalogue.families[model.family];
        if (family && !family.init) push(node, 'error', `${family.label} has no image-to-image pass here; tiled detail needs an SDXL model.`);
        else if (family && node.values.tile % family.alignment) push(node, 'error', `${family.label} needs a tile size in multiples of ${family.alignment}.`);
        if (node.values.overlap * 2 >= node.values.tile) push(node, 'error', 'The overlap must be less than half the tile size.');
      }
      if (node.type === 'sampler') {
        const model = modelOf(graph, node.id, models), family = model && catalogue.families[model.family];
        if (!family) continue;
        if (linksInto(graph, node.id, 'control').length && !model.control) push(node, 'error', 'This model has no ControlNet. Add one to an exclusive SDXL model entry under Models → Images, or remove the control image.');
        const references = linksInto(graph, node.id, 'references').length;
        if (references > family.references) push(node, 'error', family.references ? `${family.label} uses at most ${family.references} reference images.` : `${family.label} does not use reference images here.`);
        if (linksInto(graph, node.id, 'init').length && !family.init) push(node, 'error', `${family.label} has no start-image pass here; use reference images or an SDXL model.`);
        if (linksInto(graph, node.id, 'negative').length && (!family.negative || node.values.cfg_scale <= 1)) push(node, 'warning', 'The negative prompt has no effect at CFG 1 or with this family.');
        if (node.values.hires && linksInto(graph, node.id, 'init').length) push(node, 'warning', 'The hires pass applies to text-to-image passes only.');
        const size = linksInto(graph, node.id, 'size')[0], sized = size && find(graph, size.from[0]);
        if (sized && (sized.values.width % family.alignment || sized.values.height % family.alignment)) push(node, 'error', `${family.label} needs width and height in multiples of ${family.alignment}.`);
      }
    }
    if (stages(graph, catalogue, models).length > catalogue.limits.stages) push(null, 'error', `Use at most ${catalogue.limits.stages} steps.`);
    return problems;
  }

  /** A one-sampler graph from a simple per-model workflow (preset): its model, size, sampler settings and
      negative prompt. LoRA strengths stay with the model's configuration and are noted on the canvas. */
  function fromPreset(preset, catalogue, models) {
    const settings = preset.settings || {}, model = models.find(one => one.id === preset.model) || null;
    const family = model?.family, defaults = {...(catalogue.defaults[family] || {})}; delete defaults.basis;
    const sampler = catalogue.sampler_aliases?.[settings.sampler] || settings.sampler;
    const [width, height] = String(settings.size || '').split('x').map(Number);
    const graph = {schema: 1, name: (preset.name || 'Workflow') + ' (advanced)', summary: 'Converted from the simple workflow “' + (preset.name || '') + '”.',
      family: family || '', media: 'image', nodes: [], links: []};
    const node = (type, values = {}, title = null) => {
      const one = add(graph, catalogue, type, {values, models});
      if (title) one.title = title;
      return one;
    };
    const known = (name, value) => value !== undefined && value !== null && value !== '' && (catalogue.components.sampler.fields.find(field => field.name === name) ? true : false);
    node('prompt'); node('model', {model: preset.model || ''}); node('seed', {mode: 'random'});
    node('size', {width: width || settings.width || 1024, height: height || settings.height || 1024});
    if (settings.negative_prompt) node('negative', {text: settings.negative_prompt});
    const values = {...defaults};
    for (const name of ['steps', 'scheduler', 'cfg_scale', 'guidance', 'strength']) if (known(name, settings[name])) values[name] = settings[name];
    if (sampler && catalogue.samplers.includes(sampler)) values.sampler = sampler;
    const base = node('sampler', values, 'Base');
    if (settings.negative_prompt) connect(graph, catalogue, ['negative1', 'text'], [base.id, 'negative']);
    node('output', {label: 'Final'});
    if (settings.loras && Object.keys(settings.loras).length) node('note', {text: 'The simple workflow set LoRA strengths (' + Object.entries(settings.loras).map(([name, scale]) => name + ' ' + scale).join(', ') + '). Advanced workflows do not set LoRA strengths yet, so the model\'s configured strengths apply.'});
    graph.links.forEach(link => { delete link.auto; });
    return graph;
  }

  const slug = name => (name || 'workflow').toLowerCase().replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 80) || 'workflow';

  /** The API request a caller would send for this workflow's exposed inputs and overridable fields. */
  function request(graph, catalogue, {name = graph.name, version = null} = {}) {
    const body = {model: 'workflow:' + slug(name) + (version ? '@' + version : '')}, extra = {};
    for (const node of graph.nodes) {
      for (const field of node.exposed || []) {
        const value = node.values[field];
        if (node.type === 'prompt' && field === 'text' && !body.prompt) body.prompt = value || 'Describe the image';
        else if (node.type === 'negative' && field === 'text' && !body.negative_prompt) body.negative_prompt = value || '';
        else if (node.type === 'seed' && field === 'value' && body.seed === undefined) body.seed = Number(value);
        else if (node.type === 'load_image' && field === 'asset') extra[node.id] = '<image file or asset id>';
        else extra[node.id + '.' + field] = value;
      }
      for (const [field, range] of Object.entries(node.overridable || {})) extra[node.id + '.' + field] = node.values[field] ?? range[0];
    }
    const size = graph.nodes.find(node => node.type === 'size' && (node.exposed || []).length);
    if (size) body.size = size.values.width + 'x' + size.values.height;
    body.n = 1;
    if (Object.keys(extra).length) body.workflow_inputs = extra;
    return body;
  }

  function examples(graph, catalogue, options = {}) {
    const body = request(graph, catalogue, options), base = options.base || 'https://your-site';
    const json = JSON.stringify(body, null, 2);
    return {
      body,
      curl: `curl ${base}/v1/images/generations \\\n  -H "Authorization: Bearer $API_KEY" \\\n  -H "Content-Type: application/json" \\\n  -d '${json.replace(/'/g, "'\\''")}'`,
      python: `import requests\n\nanswer = requests.post(\n    "${base}/v1/images/generations",\n    headers={"Authorization": f"Bearer {API_KEY}"},\n    json=${json.replace(/\btrue\b/g, 'True').replace(/\bfalse\b/g, 'False').replace(/\bnull\b/g, 'None')},\n    timeout=None,\n)\nfor image in answer.json()["data"]:\n    print(image["output"], image["seed"])`,
    };
  }

  return {spec, inputs, outputs, find, linksInto, linksFrom, reaches, destinations, sources, connect, disconnect, add,
    insertAfter, remove, order, depths, fromPreset, lanes, earliestLane, moveToLane, clearLanes, modelOf, stages, validate, request, examples, slug};
})();
