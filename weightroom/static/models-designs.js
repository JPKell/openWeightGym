/* Three inventory layouts, one sample dataset. No service calls or persistent changes. */
(() => {
  'use strict';
  const data = JSON.parse(document.getElementById('inventory-data').textContent);
  const $ = selector => document.querySelector(selector);
  const clone = name => $(`#inventory-${name}-template`).content.firstElementChild.cloneNode(true);
  const labels = {llm:'LLM', image:'Image', audio:'Audio'};
  const states = {'in-use':'In use', loaded:'Loaded', installed:'Installed', offline:'Unreachable'};
  const view = {design:'catalogue', scope:'all', node:'atlas', kind:'all', search:'', status:'all', sort:'name', scenario:'normal'};
  const descriptions = {
    catalogue:'01 · Model first. Find a model once, then see every node where it is installed.',
    matrix:'02 · Placement at a glance. Compare every model against every node in one fleet view.',
    explorer:'03 · Node first. A dedicated inventory for each machine, with room for 30 models.'
  };
  const nodeById = id => data.nodes.find(node => node.id === id);
  const models = () => view.scenario === 'empty' ? [] : data.models;
  const currentNode = () => view.design === 'explorer' ? view.node : view.scope;
  const stateOf = placement => view.scenario === 'offline' || !nodeById(placement.node).online ? 'offline' : placement.state;
  const placements = model => model.placements.filter(p => currentNode() === 'all' || p.node === currentNode());
  const active = p => ['in-use','loaded'].includes(stateOf(p));
  const txt = (root, selector, value) => { root.querySelector(selector).textContent = value; };

  function shownModels() {
    const query = view.search.trim().toLowerCase();
    return models().filter(model => {
      const ps = placements(model);
      const stateMatch = ps.some(p => view.status === 'all' || (view.status === 'active' ? active(p) : stateOf(p) === view.status));
      return ps.length && stateMatch && (view.kind === 'all' || model.kind === view.kind) &&
        `${model.name} ${model.family} ${model.quant} ${model.role} ${ps.map(p => p.node).join(' ')}`.toLowerCase().includes(query);
    }).sort((a,b) => (view.sort === 'coverage' ? placements(b).length - placements(a).length :
      view.sort === 'size' ? (a.disk ?? Infinity) - (b.disk ?? Infinity) : 0) || a.name.localeCompare(b.name, undefined, {numeric:true}));
  }

  function statusTag(placement) {
    const tag = clone('status');
    tag.dataset.state = stateOf(placement);
    txt(tag, '[data-label]', states[stateOf(placement)]);
    return tag;
  }

  function placementButton(model, placement, matrix = false) {
    const button = clone('placement'), status = stateOf(placement);
    button.dataset.placement = `${model.id}/${placement.node}`;
    button.dataset.state = status;
    txt(button, '[data-node-name]', matrix ? states[status] : placement.node);
    txt(button, '[data-status]', matrix ? '' : ` · ${states[status]}`);
    button.setAttribute('aria-label', `${model.name} on ${placement.node}: ${states[status]}. View installation`);
    button.title = `${placement.node} · ${states[status]}${status === 'offline' ? ' · Last known installation' : ''}`;
    return button;
  }

  function subjectRow(model) {
    const row = clone('row');
    row.dataset.id = model.id;
    row.querySelector('[data-model]').dataset.model = model.id;
    txt(row, '[data-initial]', model.name[0]);
    txt(row, '[data-name]', model.name);
    txt(row, '[data-meta]', `${labels[model.kind]} · ${model.quant} · ${model.role}`);
    return row;
  }

  function cell(row, name, content) {
    const td = clone('cell');
    td.dataset.column = name;
    if (typeof content === 'string') td.textContent = content;
    else td.append(content);
    row.append(td);
    return td;
  }

  function renderNodes() {
    $('#node-list').replaceChildren(...data.nodes.map(node => {
      const button = clone('node'), installed = models().filter(model => model.placements.some(p => p.node === node.id));
      const count = installed.length;
      const loaded = installed.flatMap(m => m.placements).filter(p => p.node === node.id && active(p)).length;
      button.dataset.node = node.id;
      button.setAttribute('aria-pressed', String(view.node === node.id));
      button.setAttribute('aria-label', `${node.id}, ${count} models${node.online ? '' : ', unreachable'}`);
      txt(button, '[data-name]', node.id);
      txt(button, '[data-count]', `${count} models`);
      txt(button, '[data-hardware]', node.hardware);
      txt(button, '[data-state]', view.scenario === 'offline' || !node.online ? 'Unreachable' : 'Online');
      txt(button, '[data-active]', `${loaded} in memory`);
      return button;
    }));
    const node = nodeById(view.node);
    $('#node-title').textContent = node.id;
    $('#node-hardware').textContent = `${node.hardware} · ${node.memory}`;
    const count = models().filter(m => m.placements.some(p => p.node === node.id)).length;
    $('#node-summary').textContent = `${count} installed · ${view.scenario === 'offline' || !node.online ? 'Unreachable' : 'Online'}`;
  }

  function renderTable(list) {
    const matrix = view.design === 'matrix', explorer = view.design === 'explorer';
    const nodes = data.nodes.filter(node => currentNode() === 'all' || node.id === currentNode());
    const tr = document.createElement('tr');
    const names = matrix ? ['Model'] : explorer ? ['Model','Now','Max context','File size','Last used'] : ['Model','Installed on','Max context','File size','In memory'];
    for (const name of names) {
      const th = document.createElement('th'); th.scope = 'col'; th.textContent = name; tr.append(th);
    }
    if (matrix) for (const node of nodes) {
      const th = clone('heading');
      const button = th.querySelector('[data-node]');
      button.dataset.node = node.id;
      button.textContent = node.id;
      button.title = `Explore models on ${node.id}`;
      const count = models().filter(m => m.placements.some(p => p.node === node.id)).length;
      txt(th, '[data-count]', `${count} models${!node.online || view.scenario === 'offline' ? ' · offline' : ''}`);
      tr.append(th);
    }
    $('#inventory-head').replaceChildren(tr);
    const rows = list.map(model => {
      const row = subjectRow(model), ps = placements(model);
      if (matrix) {
        for (const node of nodes) {
          const p = ps.find(p => p.node === node.id);
          const td = cell(row, node.id, p ? placementButton(model,p,true) : '—');
          if (!p) { td.className = 'text-secondary'; td.setAttribute('aria-label', `Not installed on ${node.id}`); }
        }
      } else {
        if (explorer) cell(row,'state',statusTag(ps[0]));
        else {
          const chips = document.createElement('div'); chips.className = 'inventory-placements';
          chips.append(...ps.map(p => placementButton(model,p)));
          cell(row,'placements',chips);
        }
        const contexts = [...new Set(ps.map(p => p.context).filter(Boolean))];
        cell(row,'context',contexts.length ? contexts.map(n => `${(n/1024).toLocaleString()}K`).join(' / ') : '—');
        cell(row,'disk',model.disk == null ? 'Not measured' : `${model.disk.toFixed(1)} GiB`);
        if (explorer) cell(row,'used',ps[0].last_used);
        else {
          const loaded = ps.filter(active);
          cell(row,'active',loaded.length ? `${loaded.length} node${loaded.length === 1 ? '' : 's'}` : '—');
        }
      }
      return row;
    });
    if (!rows.length) {
      const empty = clone('empty');
      empty.querySelector('td').colSpan = tr.children.length;
      const noModels = !models().length, nodeEmpty = currentNode() !== 'all' && !models().some(m => m.placements.some(p => p.node === currentNode()));
      txt(empty,'[data-title]', noModels ? 'Your model library starts here' : nodeEmpty ? `No models on ${currentNode()} yet` : 'No matching models');
      txt(empty,'[data-note]', noModels || nodeEmpty ? 'Add a model to give this inventory its first entry.' : 'Try another name, node, type or availability.');
      empty.querySelector('[data-clear]').hidden = noModels || nodeEmpty;
      rows.push(empty);
    }
    $('#inventory-rows').replaceChildren(...rows);
    $('#table-caption').textContent = matrix ? 'Model installation matrix by compute node' : explorer ? `Installed models on ${view.node}` : 'Installed models and all their compute nodes';
    $('#inventory-title').textContent = matrix ? 'Fleet placement' : explorer ? 'Node inventory' : 'Installed models';
    $('#inventory-scope').textContent = currentNode() === 'all' ? 'ACROSS YOUR FLEET' : `ON ${currentNode().toUpperCase()}`;
    $('#table-total').textContent = `${list.length} models · ${list.reduce((n,m) => n + placements(m).length,0)} installations`;
    $('#table-note').textContent = matrix ? 'Select a cell for installation details. Select a node heading to explore it.' : 'Select a model or node chip to inspect its installation.';
  }

  function render() {
    $('#workspace').dataset.layout = view.design;
    $('#design-note').textContent = descriptions[view.design];
    $('#node-panel').hidden = $('#explorer-heading').hidden = view.design !== 'explorer';
    $('#scope-panel').hidden = view.design === 'explorer';
    $('#matrix-key').hidden = view.design !== 'matrix';
    $('#connection-note').hidden = view.scenario !== 'offline';
    for (const [key, value] of Object.entries({design:view.design, scope:view.scope, kind:view.kind})) {
      document.querySelectorAll(`[data-${key}]`).forEach(button => {
        const selected = button.dataset[key] === value;
        button.classList.toggle('active',selected); button.setAttribute('aria-pressed',String(selected));
      });
    }
    const ms = models(), allPs = ms.flatMap(m => m.placements), inUse = allPs.filter(p => stateOf(p) === 'in-use').length;
    $('#total-models').textContent = ms.length;
    $('#total-placements').textContent = allPs.length;
    $('#total-nodes').textContent = `${data.nodes.filter(n => allPs.some(p => p.node === n.id)).length} / ${data.nodes.length}`;
    $('#total-active').textContent = view.scenario === 'offline' ? '—' : allPs.filter(active).length;
    $('#active-note').textContent = view.scenario === 'offline' ? 'Live availability unverified' : `${inUse} in use · ${allPs.filter(p => stateOf(p) === 'loaded').length} loaded`;
    const list = shownModels();
    $('#result-count').textContent = `${list.length} of ${ms.filter(m => placements(m).length).length} models${currentNode() === 'all' ? ' across all nodes' : ` on ${currentNode()}`}`;
    renderNodes(); renderTable(list);
  }

  function openDetails(id, node) {
    const model = data.models.find(m => m.id === id);
    if (!model) return;
    $('#detail-title').textContent = model.name;
    $('#detail-kind').textContent = labels[model.kind];
    $('#detail-quant').textContent = model.quant;
    const facts = [['Family',model.family],['File size',model.disk == null ? 'Not measured' : `${model.disk} GiB`],['Role',model.role],['Fingerprint',model.fingerprint],['List order',String(model.order)],['Runtime',model.placements[0].engine]];
    $('#detail-facts').replaceChildren(...facts.map(([label,value]) => {
      const fact = clone('fact'); txt(fact,'[data-label]',label); txt(fact,'[data-value]',value); return fact;
    }));
    $('#detail-note').textContent = `${model.placements.length} installation${model.placements.length === 1 ? '' : 's'}${node ? ` · inspecting ${node}` : ''}. Loaded means held in memory; installed means configured on the node.`;
    $('#detail-rows').replaceChildren(...model.placements.map(p => {
      const row = clone('detail-row');
      row.classList.toggle('is-focused',p.node === node);
      txt(row,'[data-node]',p.node);
      row.querySelector('[data-state]').append(statusTag(p));
      txt(row,'[data-context]',p.context ? `${p.min_context.toLocaleString()} / ${p.context.toLocaleString()}` : 'Not applicable');
      txt(row,'[data-memory]',p.memory == null ? 'Not measured' : `${p.memory} GiB`);
      txt(row,'[data-used]',p.last_used);
      return row;
    }));
    $('#model-dialog').showModal();
  }

  document.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.hasAttribute('data-close')) button.closest('dialog').close();
    else if (button.dataset.design) {
      if (button.dataset.design === 'explorer' && view.scope !== 'all') view.node = view.scope;
      if (view.design === 'explorer' && button.dataset.design !== 'explorer') view.scope = 'all';
      view.design = button.dataset.design; render();
    } else if (button.dataset.scope) { view.scope = button.dataset.scope; render(); }
    else if (button.dataset.kind) { view.kind = button.dataset.kind; render(); }
    else if (button.dataset.node) { view.node = button.dataset.node; view.design = 'explorer'; render(); }
    else if (button.dataset.model) openDetails(button.dataset.model,view.design === 'explorer' ? view.node : null);
    else if (button.dataset.placement) { const [id,node] = button.dataset.placement.split('/'); openDetails(id,node); }
    else if (button.hasAttribute('data-clear')) {
      view.search = ''; view.status = 'all'; view.kind = 'all';
      $('#model-search').value = ''; $('#status-filter').value = 'all'; render();
    }
  });
  $('#model-search').addEventListener('input',event => { view.search = event.target.value; render(); });
  for (const [id,key] of [['status-filter','status'],['sort','sort'],['scenario','scenario']]) {
    $(`#${id}`).addEventListener('change',event => { view[key] = event.target.value; $('#notice').hidden = true; render(); });
  }
  function themeLabel() { $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode'; }
  $('#theme').addEventListener('click',() => {
    const select = $('[data-theme-form] [name=mode]');
    select.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    select.dispatchEvent(new Event('change',{bubbles:true}));
  });
  document.addEventListener('weightroom:theme',themeLabel);
  $('#add-model').addEventListener('click',() => {
    $('#add-form').reset();
    $('#add-name').setCustomValidity('');
    const node = currentNode() === 'all' ? 'atlas' : currentNode();
    $('#add-node').value = nodeById(node).online ? node : 'atlas';
    $('#add-dialog').showModal();
  });
  $('#add-form').addEventListener('submit',event => {
    event.preventDefault();
    const form = new FormData(event.target), name = form.get('name').trim(), kind = form.get('kind'), node = form.get('node');
    if (!name) { $('#add-name').setCustomValidity('Enter a model name.'); $('#add-name').reportValidity(); return; }
    const id = `preview-${data.models.length}`;
    data.models.push({id,name,kind,family:name.split(' ')[0],quant:'F16',role:'General',disk:null,fingerprint:'Pending file metadata',order:data.models.length+1,
      placements:[{node,state:'installed',context:kind === 'llm' ? 32768 : null,min_context:kind === 'llm' ? 4096 : null,memory:null,engine:kind === 'llm' ? 'llama.cpp' : kind === 'image' ? 'stable-diffusion.cpp' : 'audio.cpp',last_used:'Never'}]});
    view.scenario = 'normal'; view.kind = 'all'; view.status = 'all'; view.search = '';
    view.node = node; view.scope = node;
    $('#model-search').value = ''; $('#status-filter').value = 'all'; $('#scenario').value = 'normal';
    $('#add-dialog').close();
    $('#notice-text').textContent = `${name} added to ${node} in this preview. Reload to reset sample data.`;
    $('#notice').hidden = false; render();
  });
  $('#add-name').addEventListener('input',event => event.target.setCustomValidity(''));
  render(); themeLabel();
})();
