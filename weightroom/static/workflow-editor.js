/* Image workflow editor: lane and canvas layouts, Send to / Use from inspector, Outline, palette, dragging and undo.
   Markup comes from workflow_editor_parts.html; graph rules from workflow-graph.js. Shared by the live page and previews. */
window.SuiteWorkflowEditor = (() => {
  'use strict';
  const G = window.SuiteWorkflowGraph;
  const clone = value => JSON.parse(JSON.stringify(value));
  const make = (tag, text, className) => { const one = document.createElement(tag); if (text !== undefined && text !== null) one.textContent = text; if (className) one.className = className; return one; };
  const fromTemplate = id => document.getElementById(id).content.firstElementChild.cloneNode(true);

  /** `upscalers` lists installed upscaler files as {id, label, scale, nodes}; null skips the installed check. */
  function create(root, {catalogue, models, assets = [], upscalers = null, graph, onChange = () => {}, onSelect = () => {}}) {
    const $ = name => root.querySelector('[data-wf-' + name + ']');
    let state = clone(graph), selected = null, undo = [], redo = [], drag = null;
    const surface = $('surface'), nodesBox = $('nodes'), links = $('links'), inspector = $('inspector'), outline = $('outline');
    const problems = () => G.validate(state, catalogue, models, upscalers);
    const upscalerLabel = id => (upscalers || []).find(one => one.id === id)?.label || id;
    const title = id => G.find(state, id)?.title || id;
    const portLabel = (node, name, side) => (side === 'in' ? G.inputs : G.outputs)(catalogue, node).find(port => port.name === name)?.label || name;

    function commit(change, {inspect = true} = {}) {
      undo.push(JSON.stringify(state)); redo = [];
      if (undo.length > 100) undo.shift();
      try { change(); } catch (error) { state = JSON.parse(undo.pop()); announce(error.message, 'danger'); return false; }
      render(inspect); onChange(state);
      return true;
    }
    function announce(text, level = 'info') {
      const box = $('status'); if (!box) return;
      box.textContent = text; box.dataset.level = level;
    }
    function step(from, to) {
      if (!from.length) return;
      to.push(JSON.stringify(state)); state = JSON.parse(from.pop());
      if (selected && !G.find(state, selected)) selected = null;
      render(true); onChange(state);
    }

    /** One line describing a node's most important values. */
    function summary(node) {
      const v = node.values;
      if (node.type === 'prompt' || node.type === 'negative' || node.type === 'note') return (v.text || 'Empty').slice(0, 90);
      if (node.type === 'model') return models.find(model => model.id === v.model)?.label || 'Choose a model';
      if (node.type === 'seed') return v.mode === 'random' ? 'Random each run' : v.mode + ' · ' + v.value;
      if (node.type === 'size' || node.type === 'resize') return v.width + ' × ' + v.height + (node.type === 'resize' ? ' · ' + v.mode : '');
      if (node.type === 'sampler') return [v.steps + ' steps', v.sampler + ' / ' + v.scheduler, 'CFG ' + v.cfg_scale,
        G.linksInto(state, node.id, 'init').length ? 'strength ' + v.strength : '', v.hires ? 'hires ×' + v.hires_scale : ''].filter(Boolean).join(' · ');
      if (node.type === 'upscale') return '×' + v.factor + ' · ' + (v.upscaler ? upscalerLabel(v.upscaler) : 'Choose an upscaler');
      if (node.type === 'load_image') return assets.find(asset => asset.id === v.asset)?.label || 'Supplied when run';
      if (node.type === 'output' || node.type === 'compare') return 'Saved as “' + v.label + '”';
      if (node.type === 'control_image') return v.kind + ' · strength ' + v.strength;
      if (node.type === 'tiled_detail') return 'strength ' + v.strength + ' · ' + v.tile + ' px tiles';
      return G.spec(catalogue, node.type).help || '';
    }

    function renderNodes(issues) {
      const placed = G.lanes(state);
      nodesBox.replaceChildren(links);
      for (const node of state.nodes) {
        const card = fromTemplate('wf-node-template'), definition = G.spec(catalogue, node.type);
        card.dataset.node = node.id; card.dataset.type = node.type;
        card.style.left = node.at[0] + 'px'; card.style.top = node.at[1] + 'px';
        card.dataset.lane = placed[node.id][0]; card.dataset.row = placed[node.id][1];
        card.classList.toggle('is-selected', node.id === selected);
        card.setAttribute('aria-label', node.title + ', ' + definition.label);
        card.querySelector('[data-title]').textContent = node.title;
        card.querySelector('[data-kind]').textContent = node.title === definition.label ? '' : definition.label;
        card.querySelector('[data-label]').textContent = node.id;
        const phase = card.querySelector('[data-phase]'); phase.hidden = definition.phase <= 4; phase.textContent = 'Phase ' + definition.phase;
        card.querySelector('[data-summary]').textContent = summary(node);
        const mine = issues.filter(issue => issue.node === node.id);
        const flag = card.querySelector('[data-issues]');
        flag.hidden = !mine.length; flag.textContent = mine.length + (mine.some(one => one.level === 'error') ? ' to fix' : ' note' + (mine.length > 1 ? 's' : ''));
        flag.dataset.level = mine.some(one => one.level === 'error') ? 'error' : 'warning';
        const ports = card.querySelector('[data-ports]');
        let spare = 0;
        for (const input of G.inputs(catalogue, node)) {
          if (!input.required && !G.linksInto(state, node.id, input.name).length) { spare++; continue; }
          const row = make('div', undefined, 'wf-port wf-port-in'); row.dataset.port = 'in:' + input.name; row.dataset.type = input.type;
          const from = G.linksInto(state, node.id, input.name).map(link => title(link.from[0]));
          row.append(make('span', '', 'wf-dot'), make('span', input.label + (input.required ? '' : ''), 'wf-port-name'),
            make('span', from.length ? '← ' + from.join(', ') : input.required ? 'needs a source' : '', 'wf-port-source' + (!from.length && input.required ? ' is-missing' : '')));
          ports.append(row);
        }
        if (spare) ports.append(make('div', spare + ' optional input' + (spare === 1 ? '' : 's') + ' · Use from in settings', 'wf-port-spare'));
        for (const output of G.outputs(catalogue, node)) {
          const row = make('div', undefined, 'wf-port wf-port-out'); row.dataset.port = 'out:' + output.name; row.dataset.type = output.type;
          const count = G.linksFrom(state, node.id, output.name).length;
          row.append(make('span', count ? '→ ' + count + ' destination' + (count === 1 ? '' : 's') : 'not used yet', 'wf-port-source'), make('span', output.label, 'wf-port-name'), make('span', '', 'wf-dot'));
          ports.append(row);
        }
        nodesBox.append(card);
      }
      if (isLanes()) placeLanes(placed);
      size();
    }

    /** Lanes: each lane is a column stacked by rendered card heights, independent of the other lanes. */
    const LANE = {pad: 16, width: 280, gap: 70, space: 16};
    let laneBox = {width: 600, height: 400};
    const isLanes = () => root.dataset.wfLayout === 'lanes';
    function placeLanes(placed = G.lanes(state)) {
      const tops = {}, cards = [...nodesBox.querySelectorAll('[data-node]')].sort((a, b) => placed[a.dataset.node][0] - placed[b.dataset.node][0] || placed[a.dataset.node][1] - placed[b.dataset.node][1]);
      for (const card of cards) {
        const [lane] = placed[card.dataset.node], top = tops[lane] ?? LANE.pad;
        card.style.left = LANE.pad + lane * (LANE.width + LANE.gap) + 'px'; card.style.top = top + 'px';
        tops[lane] = top + card.offsetHeight + LANE.space;
      }
      const lanes = Object.keys(tops).map(Number);
      laneBox = {width: LANE.pad * 2 + (Math.max(0, ...lanes) + 1) * (LANE.width + LANE.gap) - LANE.gap, height: Math.max(LANE.pad, ...Object.values(tops)) + 120};
    }

    /** Scale the canvas; the sizer keeps scroll extents equal to the scaled graph. */
    let zoom = 1;
    const scale = () => isLanes() ? 1 : zoom;
    function size() {
      const width = isLanes() ? laneBox.width : Math.max(...state.nodes.map(node => node.at[0] + 320), 600);
      const height = isLanes() ? laneBox.height : Math.max(...state.nodes.map(node => node.at[1] + 300), 400);
      nodesBox.style.setProperty('--wf-width', width + 'px'); nodesBox.style.setProperty('--wf-height', height + 'px');
      nodesBox.style.transform = scale() === 1 ? '' : `scale(${scale()})`;
      const sizer = $('sizer');
      if (sizer) { sizer.style.width = width * scale() + 'px'; sizer.style.height = height * scale() + 'px'; }
      const label = root.querySelector('[data-wf-zoom-level]'); if (label) label.textContent = Math.round(zoom * 100) + '%';
    }
    function setZoom(value) { zoom = Math.min(1.5, Math.max(.4, Math.round(value * 100) / 100)); size(); requestAnimationFrame(drawLinks); }
    function fit() {
      const width = Math.max(...state.nodes.map(node => node.at[0] + 300), 300), height = Math.max(...state.nodes.map(node => node.at[1] + 300), 300);
      setZoom(Math.min(1, (surface.clientWidth - 20) / width, Math.max(.6, (surface.clientHeight - 20) / height)));
    }

    /** Arrange nodes in columns by run depth, stacked by their rendered heights. */
    function arrange() {
      const depth = G.depths(state), next = {};
      for (const id of G.order(state)) {
        const card = nodesBox.querySelector(`[data-node="${CSS.escape(id)}"]`), column = depth[id];
        const height = card ? card.offsetHeight : 200;
        next[column] ??= 20;
        G.find(state, id).at = [20 + column * 340, next[column]];
        next[column] += height + 24;
      }
    }
    function overlapping() {
      const cards = [...nodesBox.querySelectorAll('[data-node]')].map(card => [card.offsetLeft, card.offsetTop, card.offsetWidth, card.offsetHeight]);
      return cards.some((a, i) => cards.some((b, j) => j > i && a[0] < b[0] + b[2] && b[0] < a[0] + a[2] && a[1] < b[1] + b[3] && b[1] < a[1] + a[3]));
    }
    /** Tidy: lanes return to run order; the canvas re-arranges positions by depth. */
    function tidy() {
      if (isLanes()) { if (commit(() => G.clearLanes(state), {inspect: false})) announce('Lanes follow the run order again.'); return; }
      if (commit(() => arrange(), {inspect: false})) requestAnimationFrame(fit);
    }
    function tidyIfNeeded() {
      if (isLanes() || !surface.offsetParent || !overlapping()) return;
      arrange(); render(false); onChange(state, {layout: true});
    }

    /** Draw links from the rendered port positions, so every layout shares one renderer. */
    function drawLinks() {
      if (!links || links.closest('[hidden]') || !surface.offsetParent) return;
      const box = nodesBox.getBoundingClientRect();
      links.setAttribute('width', nodesBox.scrollWidth); links.setAttribute('height', nodesBox.scrollHeight);
      links.replaceChildren();
      const point = (id, port) => {
        const dot = nodesBox.querySelector(`[data-node="${CSS.escape(id)}"] [data-port="${port}"] .wf-dot`);
        if (!dot) return null;
        const rect = dot.getBoundingClientRect();
        return [(rect.left + rect.width / 2 - box.left) / scale(), (rect.top + rect.height / 2 - box.top) / scale()];
      };
      for (const link of state.links) {
        const a = point(link.from[0], 'out:' + link.from[1]), b = point(link.to[0], 'in:' + link.to[1]);
        if (!a || !b) continue;
        const bend = Math.max(40, Math.abs(b[0] - a[0]) / 2);
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', `M${a[0]},${a[1]} C${a[0] + bend},${a[1]} ${b[0] - bend},${b[1]} ${b[0]},${b[1]}`);
        const port = G.inputs(catalogue, G.find(state, link.to[0])).find(one => one.name === link.to[1]);
        path.setAttribute('class', 'wf-link wf-type-' + (port?.type || 'text') + (link.auto ? ' is-auto' : '') +
          (selected && (link.from[0] === selected || link.to[0] === selected) ? ' is-active' : ''));
        links.append(path);
      }
    }

    function renderOutline(issues) {
      if (!outline) return;
      outline.replaceChildren();
      for (const id of G.order(state)) {
        const node = G.find(state, id), item = fromTemplate('wf-outline-template');
        const button = item.querySelector('button');
        button.dataset.node = id; button.classList.toggle('active', id === selected);
        button.setAttribute('aria-current', id === selected ? 'true' : 'false');
        item.querySelector('[data-title]').textContent = node.title;
        item.querySelector('[data-kind]').textContent = G.spec(catalogue, node.type).label;
        item.querySelector('[data-summary]').textContent = summary(node);
        const into = state.links.filter(link => link.to[0] === id).map(link => title(link.from[0]) + ' → ' + portLabel(node, link.to[1], 'in'));
        const out = state.links.filter(link => link.from[0] === id).map(link => title(link.to[0]) + ' · ' + portLabel(G.find(state, link.to[0]), link.to[1], 'in'));
        item.querySelector('[data-from]').textContent = into.length ? 'From: ' + into.join('; ') : '';
        item.querySelector('[data-to]').textContent = out.length ? 'To: ' + out.join('; ') : '';
        const count = issues.filter(issue => issue.node === id).length;
        item.querySelector('[data-issues]').textContent = count ? count + ' to review' : '';
        outline.append(item);
      }
    }

    function renderProblems(issues) {
      const box = $('problems'); if (!box) return;
      box.replaceChildren();
      const errors = issues.filter(issue => issue.level === 'error');
      box.dataset.state = errors.length ? 'error' : issues.length ? 'warning' : 'ready';
      const head = make('strong', errors.length ? `${errors.length} to fix before running` : issues.length ? 'Ready, with notes' : 'Ready to run');
      box.append(head);
      if (!issues.length) return;
      const list = make('ul', undefined, 'wf-problem-list');
      for (const issue of issues) {
        const item = make('li'); item.dataset.level = issue.level;
        if (issue.node) {
          const go = make('button', title(issue.node), 'btn btn-link btn-sm p-0 align-baseline'); go.type = 'button';
          go.onclick = () => select(issue.node, true); item.append(go, ': ');
        }
        item.append(issue.text); list.append(item);
      }
      box.append(list);
    }

    function fieldControl(node, field) {
      const id = 'wf-field-' + node.id + '-' + field.name, value = node.values[field.name];
      const wrap = make('div', undefined, 'wf-field' + (field.kind === 'textarea' ? ' wf-field-wide' : ''));
      let control;
      if (field.kind === 'textarea') { control = make('textarea', undefined, 'form-control'); control.rows = 3; control.value = value ?? ''; control.maxLength = field.max || 24000; }
      else if (['choice', 'model', 'asset', 'upscaler'].includes(field.kind)) {
        control = make('select', undefined, 'form-select');
        const installed = (upscalers || []).map(one => [one.id, one.label + (one.nodes?.length ? ' · ' + one.nodes.join(', ') : '')]);
        const choices = field.kind === 'model' ? [['', 'Choose a model'], ...models.map(model => [model.id, model.label + ' · ' + (catalogue.families[model.family]?.label || model.family)])]
          : field.kind === 'asset' ? [['', 'Supplied when run'], ...assets.map(asset => [asset.id, asset.label])]
          : field.kind === 'upscaler' ? [['', installed.length ? 'Choose an upscaler' : 'No upscaler files installed'], ...installed] : field.choices.map(choice => [choice, choice]);
        choices.forEach(([key, label]) => control.add(new Option(label, key)));
        if (field.upscalers && installed.length) {
          const group = document.createElement('optgroup'); group.label = 'Installed upscalers';
          installed.forEach(([key, label]) => group.append(new Option(label, key))); control.append(group);
        }
        // A saved value that is no longer offered stays visible, so saving never changes it silently.
        if (value && ![...control.options].some(option => option.value === value)) control.add(new Option(value + ' (not installed)', value));
        control.value = value ?? '';
      } else if (field.kind === 'toggle') {
        wrap.className = 'wf-field form-check form-switch';
        control = make('input', undefined, 'form-check-input'); control.type = 'checkbox'; control.checked = Boolean(value);
      } else {
        control = make('input', undefined, 'form-control'); control.type = field.kind === 'number' ? 'number' : 'text';
        if (field.kind === 'number') { control.min = field.min; control.max = field.max; control.step = field.step ?? 'any'; }
        if (field.max && field.kind !== 'number') control.maxLength = field.max;
        control.value = value ?? '';
      }
      control.id = id; control.dataset.field = field.name;
      const label = make('label', field.label, field.kind === 'toggle' ? 'form-check-label' : 'form-label'); label.htmlFor = id;
      if (field.kind === 'toggle') wrap.append(control, label); else wrap.append(label, control);
      const read = () => field.kind === 'toggle' ? control.checked : field.kind === 'number' ? (control.value === '' ? null : Number(control.value)) : control.value;
      control.addEventListener('change', () => commit(() => { G.find(state, node.id).values[field.name] = read(); }, {inspect: ['hires', 'model'].includes(field.name) || field.kind === 'model'}));
      if (['textarea', 'text'].includes(field.kind) || field.kind === 'number') control.addEventListener('input', () => { G.find(state, node.id).values[field.name] = read(); render(false); onChange(state); });
      if (G.spec(catalogue, node.type).exposed !== undefined || ['prompt', 'negative', 'seed', 'size', 'load_image', 'combine'].includes(node.type)) {
        const expose = make('label', undefined, 'form-check wf-expose');
        const box = make('input', undefined, 'form-check-input'); box.type = 'checkbox'; box.checked = (node.exposed || []).includes(field.name);
        box.addEventListener('change', () => commit(() => { const one = G.find(state, node.id); one.exposed = box.checked ? [...new Set([...(one.exposed || []), field.name])] : (one.exposed || []).filter(name => name !== field.name); }, {inspect: false}));
        expose.append(box, make('span', 'Callers set this', 'form-check-label')); wrap.append(expose);
      }
      if (field.overridable) {
        const range = (node.overridable || {})[field.name];
        const allow = make('label', undefined, 'form-check wf-expose');
        const box = make('input', undefined, 'form-check-input'); box.type = 'checkbox'; box.checked = Boolean(range);
        allow.append(box, make('span', 'Callers may override', 'form-check-label')); wrap.append(allow);
        if (range && field.kind === 'number') {
          const bounds = make('div', undefined, 'wf-bounds');
          ['From', 'To'].forEach((word, index) => {
            const input = make('input', undefined, 'form-control form-control-sm'); input.type = 'number';
            input.min = field.min; input.max = field.max; input.step = field.step ?? 'any'; input.value = range[index];
            input.setAttribute('aria-label', field.label + ' override ' + word.toLowerCase());
            input.addEventListener('change', () => commit(() => { G.find(state, node.id).overridable[field.name][index] = Number(input.value); }, {inspect: false}));
            bounds.append(input);
          });
          wrap.append(bounds);
        }
        box.addEventListener('change', () => commit(() => {
          const one = G.find(state, node.id); one.overridable = {...(one.overridable || {})};
          if (box.checked) one.overridable[field.name] = field.kind === 'number' ? [field.min, field.max] : field.choices || []; else delete one.overridable[field.name];
        }));
      }
      return wrap;
    }

    function visibleField(node, field) {
      if (field.needs === 'init' && !G.linksInto(state, node.id, 'init').length) return false;
      if (field.needs === 'hires' && !node.values.hires) return false;
      if (field.family) {
        const model = G.modelOf(state, node.id, models), family = model && catalogue.families[model.family];
        if (!family || !family[field.family]) return false;
      }
      return true;
    }

    function renderInspector() {
      if (!inspector) return;
      inspector.replaceChildren();
      const node = selected && G.find(state, selected);
      if (!node) { inspector.append(fromTemplate('wf-inspector-empty-template')); return; }
      const panel = fromTemplate('wf-inspector-template'), definition = G.spec(catalogue, node.type);
      panel.querySelector('[data-kind]').textContent = definition.label + (definition.phase > 4 ? ' · Phase ' + definition.phase : '');
      panel.querySelector('[data-help]').textContent = definition.help || '';
      const name = panel.querySelector('[data-node-title]'); name.value = node.title;
      name.addEventListener('change', () => commit(() => { G.find(state, node.id).title = name.value.trim().slice(0, 60) || definition.label; }, {inspect: false}));
      const label = panel.querySelector('[data-node-label]'); label.value = node.id;
      label.addEventListener('change', () => {
        const next = label.value.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-').slice(0, 40);
        if (!next || next === node.id) { label.value = node.id; return; }
        if (G.find(state, next)) { announce('Another component already uses the label ' + next + '.', 'danger'); label.value = node.id; return; }
        commit(() => {
          const one = G.find(state, node.id); one.id = next;
          state.links.forEach(link => { if (link.from[0] === node.id) link.from[0] = next; if (link.to[0] === node.id) link.to[0] = next; });
          selected = next;
        });
      });
      const fields = panel.querySelector('[data-fields]'), advanced = panel.querySelector('[data-advanced-fields]');
      for (const field of definition.fields || []) if (visibleField(node, field)) (field.advanced ? advanced : fields).append(fieldControl(node, field));
      panel.querySelector('[data-advanced]').hidden = !advanced.children.length;
      const ins = panel.querySelector('[data-inputs]');
      for (const input of G.inputs(catalogue, node)) {
        const row = fromTemplate('wf-use-from-template'), id = 'wf-from-' + node.id + '-' + input.name;
        const caption = row.querySelector('label'); caption.textContent = input.label + (input.required ? ' (required)' : ''); caption.htmlFor = id;
        const select = row.querySelector('select'); select.id = id;
        const current = G.linksInto(state, node.id, input.name);
        select.add(new Option(input.multiple ? 'Add a source…' : 'No source', ''));
        for (const one of G.sources(state, catalogue, node.id, input.name)) {
          const option = new Option(one.node.title + ' · ' + one.output.label, one.node.id + '.' + one.output.name);
          if (!input.multiple && current.some(link => link.from[0] === one.node.id && link.from[1] === one.output.name)) option.selected = true;
          if (input.multiple && current.some(link => link.from[0] === one.node.id && link.from[1] === one.output.name)) option.disabled = true;
          select.add(option);
        }
        select.addEventListener('change', () => commit(() => {
          if (!input.multiple) state.links = state.links.filter(link => !(link.to[0] === node.id && link.to[1] === input.name));
          if (select.value) G.connect(state, catalogue, select.value.split('.'), [node.id, input.name]);
        }));
        const chips = row.querySelector('[data-chips]');
        if (input.multiple) current.forEach((link, index) => {
          const chip = make('button', (index + 1) + '. ' + title(link.from[0]) + ' ×', 'btn btn-sm btn-outline-secondary'); chip.type = 'button';
          chip.setAttribute('aria-label', 'Remove ' + title(link.from[0]) + ' from ' + input.label);
          chip.onclick = () => commit(() => G.disconnect(state, link.from, link.to)); chips.append(chip);
        });
        if (current.some(link => link.auto)) row.querySelector('[data-auto]').hidden = false;
        ins.append(row);
      }
      panel.querySelector('[data-inputs-section]').hidden = !ins.children.length;
      const outs = panel.querySelector('[data-outputs]');
      for (const output of G.outputs(catalogue, node)) {
        const group = fromTemplate('wf-send-to-template');
        group.querySelector('[data-output]').textContent = 'Send ' + output.label.toLowerCase() + ' to';
        const list = group.querySelector('[data-destinations]'), rows = G.destinations(state, catalogue, node.id, output.name);
        if (!rows.length) list.append(make('p', 'No component has a matching input yet. Add one from the palette or with Add after.', 'small text-secondary mb-0'));
        for (const row of rows) {
          const line = make('label', undefined, 'form-check wf-destination'), box = make('input', undefined, 'form-check-input');
          box.type = 'checkbox'; box.checked = row.linked; box.disabled = Boolean(row.disabled);
          box.dataset.destination = row.node.id + '.' + row.input.name;
          const text = make('span', undefined, 'form-check-label');
          text.append(make('strong', row.node.title), ' · ' + row.input.label);
          if (row.disabled) text.append(make('small', ' — ' + row.disabled, 'text-secondary'));
          else if (row.replaces) text.append(make('small', ' — replaces ' + row.replaces, 'text-secondary'));
          box.addEventListener('change', () => commit(() => box.checked ? G.connect(state, catalogue, [node.id, output.name], [row.node.id, row.input.name])
            : G.disconnect(state, [node.id, output.name], [row.node.id, row.input.name])));
          line.append(box, text); list.append(line);
        }
        outs.append(group);
      }
      panel.querySelector('[data-outputs-section]').hidden = !outs.children.length;
      const after = panel.querySelector('[data-add-after-type]');
      (catalogue.order || Object.keys(catalogue.components)).forEach(type => { const one = catalogue.components[type]; if ((one.inputs || []).length) after.add(new Option(one.label, type)); });
      panel.querySelector('[data-add-after]').onclick = () => commit(() => { selected = G.insertAfter(state, catalogue, node.id, after.value, models).id; });
      panel.querySelector('[data-duplicate]').onclick = () => commit(() => {
        const copy = G.add(state, catalogue, node.type, {at: [node.at[0] + 30, node.at[1] + 30], values: clone(node.values), models});
        state.links.filter(link => link.to[0] === node.id).forEach(link => G.connect(state, catalogue, link.from, [copy.id, link.to[1]]));
        selected = copy.id;
      });
      panel.querySelector('[data-remove]').onclick = () => { const gone = node.title; commit(() => { G.remove(state, node.id); selected = null; }); announce(gone + ' removed. Undo restores it.'); };
      inspector.append(panel);
    }

    function render(inspect = true) {
      const issues = problems();
      renderNodes(issues); renderOutline(issues); renderProblems(issues);
      if (inspect) renderInspector();
      requestAnimationFrame(drawLinks);
      const undoButton = $('undo'), redoButton = $('redo');
      if (undoButton) undoButton.disabled = !undo.length;
      if (redoButton) redoButton.disabled = !redo.length;
    }

    function select(id, focus = false) {
      selected = id; render(true); onSelect(id);
      if (focus) {
        const card = nodesBox.querySelector(`[data-node="${CSS.escape(id)}"]`);
        if (card?.offsetParent) { card.focus({preventScroll: true}); card.scrollIntoView({block: 'nearest', inline: 'nearest'}); }
        else outline?.querySelector(`[data-node="${CSS.escape(id)}"]`)?.focus();
      }
    }

    function palette() {
      const box = $('palette'); if (!box) return;
      box.replaceChildren();
      const groups = {};
      (catalogue.order || Object.keys(catalogue.components)).forEach(type => { const one = catalogue.components[type]; (groups[one.group] ||= []).push([type, one]); });
      for (const [group, items] of Object.entries(groups)) {
        const section = make('div', undefined, 'wf-palette-group');
        section.append(make('span', group, 'wf-overline'));
        for (const [type, one] of items) {
          const button = make('button', undefined, 'wf-palette-item'); button.type = 'button'; button.dataset.add = type;
          button.append(make('span', one.label), make('small', one.phase > 4 ? 'Phase ' + one.phase : one.help.split('.')[0], 'text-secondary'));
          button.title = one.help;
          button.onclick = () => commit(() => { selected = G.add(state, catalogue, type, {near: selected, models}).id; });
          section.append(button);
        }
        box.append(section);
      }
    }

    /** The test-run form: inputs a caller may set (exposed fields and overridable bounds); typed values survive re-rendering. */
    function runInputs() {
      const box = $('run-inputs'); if (!box) return;
      const before = Object.fromEntries([...box.querySelectorAll('[data-key]')].map(input => [input.dataset.key, input.value]));
      box.replaceChildren();
      for (const node of state.nodes) {
        const definition = G.spec(catalogue, node.type);
        for (const name of node.exposed || []) {
          const field = (definition.fields || []).find(one => one.name === name); if (!field) continue;
          box.append(runInput(node, field, node.title + (field.label === node.title ? '' : ' · ' + field.label), node.id + (node.type === 'load_image' ? '' : '.' + name), before));
        }
        for (const [name, range] of Object.entries(node.overridable || {})) {
          const field = (definition.fields || []).find(one => one.name === name); if (!field) continue;
          box.append(runInput(node, {...field, min: range[0], max: range[1]}, node.title + ' · ' + field.label + ' (' + range.join('–') + ')', node.id + '.' + name, before));
        }
      }
      if (!box.children.length) box.append(make('p', 'This workflow exposes nothing; it always runs with its saved values.', 'small text-secondary mb-0'));
    }
    function runInput(node, field, label, key, before) {
      const wrap = make('div', undefined, 'wf-field' + (field.kind === 'textarea' ? ' wf-field-wide' : '')), id = 'run-' + key.replace(/\W/g, '-');
      const caption = make('label', label, 'form-label'); caption.htmlFor = id;
      let control;
      if (field.kind === 'textarea') { control = make('textarea', undefined, 'form-control'); control.rows = 2; }
      else if (['choice', 'asset', 'upscaler'].includes(field.kind)) {
        control = make('select', undefined, 'form-select');
        (field.kind === 'asset' ? [['', 'Choose an image'], ...assets.map(one => [one.id, one.label])]
          : field.kind === 'upscaler' ? (upscalers || []).map(one => [one.id, one.label]) : field.choices.map(one => [one, one])).forEach(([value, text]) => control.add(new Option(text, value)));
      } else {
        control = make('input', undefined, 'form-control'); control.type = field.kind === 'number' ? 'number' : 'text';
        if (field.kind === 'number') { control.min = field.min; control.max = field.max; control.step = field.step ?? 'any'; }
      }
      control.id = id; control.dataset.key = key;
      control.value = before[key] ?? node.values[field.name] ?? '';
      wrap.append(caption, control);
      return wrap;
    }
    /** Values typed into the test-run form, by caller key; an override outside its bounds raises with the reason. */
    function runValues() {
      // Numbers go as numbers; an image left unchosen is left out, so the run says which one it needs.
      const values = Object.fromEntries([...root.querySelectorAll('[data-wf-run-inputs] [data-key]')].filter(input => input.value !== '' || input.tagName === 'TEXTAREA')
        .map(input => [input.dataset.key, input.type === 'number' ? Number(input.value) : input.value]));
      for (const [key, value] of Object.entries(values)) {
        const [id, name] = key.split('.'), node = G.find(state, id), range = name && node?.overridable?.[name];
        if (range && (Number(value) < range[0] || Number(value) > range[1])) throw new Error(`${node.title} · ${name} must be from ${range[0]} to ${range[1]}.`);
      }
      return values;
    }
    /** The API call for this workflow in `language` (curl or python). */
    function apiExample(language, options = {}) {
      const code = $('api-code'); if (!code) return '';
      code.textContent = G.examples(state, catalogue, options)[language];
      return code.textContent;
    }

    nodesBox.addEventListener('click', event => { const card = event.target.closest('[data-node]'); if (card) select(card.dataset.node); });
    nodesBox.addEventListener('keydown', event => {
      const card = event.target.closest('[data-node]'); if (!card || event.target !== card) return;
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); select(card.dataset.node); inspector?.querySelector('input,select,textarea')?.focus(); }
      const moves = {ArrowLeft: [-20, 0], ArrowRight: [20, 0], ArrowUp: [0, -20], ArrowDown: [0, 20]};
      if (event.altKey && moves[event.key]) {
        event.preventDefault();
        const id = card.dataset.node;
        if (isLanes()) {
          const [lane, row] = G.lanes(state)[id], [dx, dy] = moves[event.key].map(Math.sign);
          moveLane(id, lane + dx, row + dy);
        } else commit(() => { const node = G.find(state, id); node.at = [Math.max(0, node.at[0] + moves[event.key][0]), Math.max(0, node.at[1] + moves[event.key][1])]; }, {inspect: false});
        nodesBox.querySelector(`[data-node="${CSS.escape(id)}"]`)?.focus();
      }
    });
    /** Move a card to a lane and row; a lane before one of its sources is refused with the reason. */
    function moveLane(id, lane, row) {
      let used = lane;
      const before = JSON.stringify(state);
      commit(() => { used = G.moveToLane(state, id, lane, row); }, {inspect: false});
      if (JSON.stringify(state) === before) undo.pop();
      if (used > lane) {
        const placed = G.lanes(state), source = state.links.filter(link => link.to[0] === id).map(link => link.from[0]).sort((a, b) => placed[b][0] - placed[a][0])[0];
        announce(`${title(id)} uses ${title(source)}, so it stays in lane ${used + 1} or later.`);
      } else announce(`${title(id)} is in lane ${used + 1}, position ${G.lanes(state)[id][1] + 1}.`);
    }

    /** Where a lane drag would land: the lane under the pointer (never before a source) and the row between cards. */
    function laneTarget(event, id) {
      const box = nodesBox.getBoundingClientRect(), placed = G.lanes(state);
      const lastLane = Math.max(0, ...Object.values(placed).map(one => one[0])) + 1;
      const pointed = Math.min(lastLane, Math.max(0, Math.floor((event.clientX - box.left - LANE.pad + LANE.gap / 2) / (LANE.width + LANE.gap))));
      const lane = Math.max(pointed, G.earliestLane(state, id, placed));
      const members = Object.keys(placed).filter(one => one !== id && placed[one][0] === lane).sort((a, b) => placed[a][1] - placed[b][1])
        .map(one => nodesBox.querySelector(`[data-node="${CSS.escape(one)}"]`)).filter(Boolean);
      const row = members.filter(card => { const rect = card.getBoundingClientRect(); return rect.top + rect.height / 2 < event.clientY; }).length;
      const next = members[row], last = members[members.length - 1];
      const top = next ? next.offsetTop - 10 : last ? last.offsetTop + last.offsetHeight + 6 : LANE.pad;
      return {lane, pointed, row, refused: lane > pointed, left: LANE.pad + lane * (LANE.width + LANE.gap), top};
    }
    function dropMarker(target) {
      let marker = nodesBox.querySelector('.wf-lane-drop');
      if (!target) { marker?.remove(); return; }
      if (!marker) { marker = make('div', undefined, 'wf-lane-drop'); marker.setAttribute('aria-hidden', 'true'); nodesBox.append(marker); }
      marker.style.left = target.left + 'px'; marker.style.top = target.top + 'px';
      marker.textContent = 'Lane ' + (target.lane + 1) + (target.refused ? ' · earliest after its sources' : '');
    }

    nodesBox.addEventListener('pointerdown', event => {
      const handle = event.target.closest('[data-drag]'), card = handle?.closest('[data-node]');
      if (!card || event.button !== 0) return;
      const node = G.find(state, card.dataset.node);
      drag = {id: node.id, x: event.clientX, y: event.clientY, at: [...node.at], before: JSON.stringify(state), card,
        lanes: isLanes(), moved: false};
      handle.setPointerCapture(event.pointerId);
    });
    nodesBox.addEventListener('pointermove', event => {
      if (!drag) return;
      const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      drag.moved = true;
      if (drag.lanes) {
        drag.card.classList.add('is-dragging'); drag.card.style.transform = `translate(${dx}px, ${dy}px)`;
        drag.target = laneTarget(event, drag.id); dropMarker(drag.target);
      } else {
        const node = G.find(state, drag.id);
        node.at = [Math.max(0, Math.round((drag.at[0] + dx / zoom) / 10) * 10), Math.max(0, Math.round((drag.at[1] + dy / zoom) / 10) * 10)];
        drag.card.style.left = node.at[0] + 'px'; drag.card.style.top = node.at[1] + 'px';
      }
      drawLinks();
    });
    function endDrag() {
      if (!drag) return;
      const finished = drag; drag = null;
      dropMarker(null); finished.card.classList.remove('is-dragging'); finished.card.style.transform = '';
      if (finished.lanes && finished.moved && finished.target) moveLane(finished.id, finished.target.pointed, finished.target.row);
      else if (finished.before !== JSON.stringify(state)) { undo.push(finished.before); redo = []; onChange(state); render(false); }
      select(finished.id);
    }
    nodesBox.addEventListener('pointerup', endDrag);
    nodesBox.addEventListener('pointercancel', endDrag);
    outline?.addEventListener('click', event => { const button = event.target.closest('[data-node]'); if (button) select(button.dataset.node); });
    root.querySelector('[data-wf-tidy]')?.addEventListener('click', tidy);
    root.querySelectorAll('[data-wf-zoom]').forEach(button => button.addEventListener('click', () => {
      const how = button.dataset.wfZoom; if (how === 'fit') fit(); else setZoom(zoom + (how === 'in' ? .1 : -.1));
    }));
    $('undo')?.addEventListener('click', () => step(undo, redo));
    $('redo')?.addEventListener('click', () => step(redo, undo));
    root.addEventListener('keydown', event => {
      if (!(event.ctrlKey || event.metaKey) || event.target.matches('input,textarea,select')) return;
      if (event.key.toLowerCase() === 'z') { event.preventDefault(); step(event.shiftKey ? redo : undo, event.shiftKey ? undo : redo); }
      if (event.key.toLowerCase() === 'y') { event.preventDefault(); step(redo, undo); }
    });
    window.addEventListener('resize', () => requestAnimationFrame(drawLinks));
    document.addEventListener('weightroom:theme', () => requestAnimationFrame(drawLinks));
    /** Canvas draws the graph; Outline lists it in run order for keyboards, screen readers and narrow screens. */
    function view(name) {
      // Lanes and Canvas draw the same graph; a toolbar offering Lanes switches the layout too.
      if (name === 'lanes' || (name === 'canvas' && root.querySelector('[data-wf-view=lanes]'))) root.dataset.wfLayout = name;
      root.dataset.wfView = name;
      surface.hidden = name === 'outline';
      if (outline) outline.hidden = name !== 'outline';
      root.querySelectorAll('[data-wf-view]').forEach(button => {
        button.classList.toggle('active', button.dataset.wfView === name);
        button.setAttribute('aria-pressed', button.dataset.wfView === name ? 'true' : 'false');
      });
      if (name !== 'outline') render(false);
      requestAnimationFrame(name === 'outline' ? drawLinks : () => { tidyIfNeeded(); fit(); });
    }
    root.querySelectorAll('[data-wf-view]').forEach(button => button.addEventListener('click', () => view(button.dataset.wfView)));
    palette(); render(true);
    view(window.matchMedia('(max-width: 700px)').matches ? 'outline' : root.querySelector('[data-wf-view]')?.dataset.wfView || 'canvas');
    requestAnimationFrame(() => { tidyIfNeeded(); fit(); });

    return {
      graph: () => clone(state),
      load(next) { state = clone(next); selected = null; undo = []; redo = []; zoom = 1; render(true); onChange(state); requestAnimationFrame(() => { tidyIfNeeded(); fit(); }); },
      fit, setZoom, tidy,
      select, view, redraw: () => requestAnimationFrame(drawLinks), problems, render,
      stages: () => G.stages(state, catalogue, models),
      runInputs, runValues, apiExample,
      selected: () => selected,
    };
  }
  return {create};
})();
