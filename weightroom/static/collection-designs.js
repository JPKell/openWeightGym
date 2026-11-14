/* Offline, tab-local proposals. Never send sources or questions to a service. */
(() => {
  'use strict';
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const seed = JSON.parse($('#collection-samples').textContent);
  let data = structuredClone(seed);
  const state = {design: 'desk', page: 'overview', collection: 'procurement', workflow: 'supplier-brief', scenario: 'normal'};
  const drafts = new Map(), history = [];
  let selectedReference = 'approval', lastResult = null;
  const descriptions = {
    desk: '01 · Evaluation desk — business use cases in a catalogue, questions beside evidence, and a focused run review. Recommended for everyday testing.',
    ledger: '02 · Evidence ledger — compact inventories and a source-first workspace for detailed, repeatable inspection.',
    guide: '03 · Guided workbench — an explicit source-to-review path and workflow stages that make the next action clear.',
  };
  const labels = {ready_research: 'Ready for research', review_research: 'Review research', ready_draft: 'Ready for draft',
    review_draft: 'Review draft', failed_draft: 'Draft needs retry', completed: 'Completed'};
  const next = {ready_research: 'Start research', review_research: 'Accept research', ready_draft: 'Start draft',
    review_draft: 'Accept draft', failed_draft: 'Retry draft', completed: 'Review accepted output'};
  const category = run => run.state === 'completed' ? 'completed' : run.state.startsWith('failed') ? 'retry' : run.state.startsWith('review') ? 'review' : 'ready';
  const collection = () => data.collections.find(c => c.id === state.collection);
  const workflow = () => data.workflows.find(w => w.id === state.workflow);
  const reference = () => collection()?.questions.find(q => q.id === selectedReference);
  function clone(name) { return $(`#${name}-template`).content.firstElementChild.cloneNode(true); }
  function put(root, selector, value) { $(selector, root).textContent = value; }
  function say(message) { $('#action-status').textContent = message; }
  function badge(el, text) {
    el.textContent = text;
    el.className = `badge bg-${/Ready|Completed|Accepted|Matches|Research accepted/.test(text) ? 'success' : /attention|Failed|retry|review|Review/.test(text) ? 'orange' : /Indexing/.test(text) ? 'azure' : 'secondary'}-lt`;
  }
  function route() {
    const query = new URLSearchParams({design: state.design, page: state.page, collection: state.collection, workflow: state.workflow});
    window.history.replaceState(null, '', `#${query}`);
  }
  function saveDraft() {
    if (!collection()) return;
    drafts.set(state.collection, {
      question: $('#collection-question').value, model: $('#collection-answer-model').value,
      minimum: $('#collection-minimum').value, reference: selectedReference, result: lastResult,
      note: $('#review-note').value,
    });
  }
  function readRoute() {
    const query = new URLSearchParams(location.hash.slice(1));
    if (Object.hasOwn(descriptions, query.get('design'))) state.design = query.get('design');
    if (['overview', 'collection', 'workflows'].includes(query.get('page'))) state.page = query.get('page');
    if (data.collections.some(c => c.id === query.get('collection'))) state.collection = query.get('collection');
    if (data.workflows.some(w => w.id === query.get('workflow'))) state.workflow = query.get('workflow');
  }
  function renderShell() {
    $('#case-shell').dataset.design = state.design;
    $('#design-description').textContent = descriptions[state.design];
    $$('[data-design]').filter(el => el.tagName === 'BUTTON').forEach(el => {
      const active = el.dataset.design === state.design;
      el.classList.toggle('active', active); el.setAttribute('aria-pressed', String(active));
    });
    $$('[data-page]').forEach(el => {
      const active = el.dataset.page === state.page;
      el.classList.toggle('active', active); el.setAttribute('aria-pressed', String(active));
    });
    $$('[data-page-panel]').forEach(el => { el.hidden = el.dataset.pagePanel !== state.page; });
    $('#connection-error').hidden = state.scenario !== 'error';
    document.title = `${state.page === 'collection' ? collection()?.title || 'Collection' : state.page === 'workflows' ? 'Workflows' : 'Collections'} · ${state.design} · openWeightGym`;
    route();
  }
  function openPage(page, id) {
    saveDraft();
    if (page === 'collection' && state.scenario === 'empty') page = 'overview';
    if (page === 'collection' && id) state.collection = id;
    state.page = page;
    if (page === 'collection') renderCollection();
    if (page === 'workflows') renderWorkflows();
    renderShell();
    $('#workspace').focus({preventScroll: true});
  }
  function renderOverview() {
    const available = state.scenario === 'empty' ? [] : data.collections;
    const search = $('#collection-search').value.toLowerCase().trim(), filter = $('#collection-state').value;
    const list = available.filter(c => (!filter || c.state === filter) && `${c.title} ${c.domain} ${c.description}`.toLowerCase().includes(search));
    if ($('#collection-sort').value === 'name') list.sort((a, b) => a.title.localeCompare(b.title));
    if ($('#collection-sort').value === 'attention') list.sort((a, b) => (b.state === 'Needs attention') - (a.state === 'Needs attention'));
    $('#collection-list').replaceChildren(...list.map(c => {
      const item = clone('collection-item');
      put(item, '[data-initial]', c.domain.split(' ').map(x => x[0]).slice(0, 2).join(''));
      put(item, '[data-domain]', c.domain); put(item, 'h3 a', c.title); put(item, '[data-description]', c.description);
      put(item, '[data-doc-count]', `${c.documents.length} / 20`); put(item, '[data-chunk-count]', c.chunks);
      put(item, '[data-check-count]', c.questions.length); put(item, '[data-sensitivity]', c.level);
      badge($('[data-state]', item), c.state);
      $$('[data-open-collection]', item).forEach(a => {
        a.href = `#design=${state.design}&page=collection&collection=${c.id}`;
        a.addEventListener('click', event => { event.preventDefault(); openPage('collection', c.id); });
      });
      return item;
    }));
    $('#collection-count').textContent = `${list.length} of ${available.length} collections`;
    $('#collection-empty').hidden = !!list.length;
    $('#collection-empty-title').textContent = available.length ? 'No collections match' : 'Start with a business use case';
    $('#collection-empty-text').textContent = available.length ? 'Try a different name or status.' : 'Create a private collection and add the documents you want to test against.';
    $('#clear-collection-filters').hidden = !available.length;
    $('#collections-kpi').textContent = `${available.length} / 5`;
    $('#sources-kpi').textContent = available.reduce((sum, c) => sum + c.documents.length, 0);
    $('#ready-kpi').textContent = available.filter(c => c.state === 'Ready').length;
    $('#attention-kpi').textContent = available.filter(c => c.state === 'Needs attention').length;
  }
  function inspectSource(doc, text, page) {
    $('#source-name').textContent = doc.name;
    $('#source-meta').textContent = `${page ? `Page ${page}` : 'Lines 1–8'} · collection revision ${collection().revision} · sample provenance`;
    $('#source-text').textContent = text || doc.text;
    $('#source-dialog').showModal();
  }
  function renderDocuments() {
    const c = collection();
    $('#document-rows').replaceChildren(...c.documents.map(doc => {
      const row = clone('document-row');
      put(row, '[data-inspect-document]', doc.name); put(row, '[data-doc-type]', doc.name.endsWith('.pdf') ? 'Selectable PDF' : 'UTF-8 text');
      badge($('[data-state]', row), doc.state); put(row, '[data-chunks]', doc.chunks);
      $('[data-inspect-document]', row).addEventListener('click', () => inspectSource(doc, doc.text, doc.page));
      const index = $('[data-index-document]', row);
      index.textContent = doc.state === 'Ready' ? 'Reindex' : doc.state === 'Indexing' ? 'Cancel' : 'Index / retry';
      index.addEventListener('click', () => {
        if (doc.state === 'Indexing') { doc.state = 'Pending'; say('Sample indexing cancelled. Source retained.'); }
        else { doc.state = 'Ready'; doc.chunks = doc.chunks || 8; say('Sample indexing completed. No model was called.'); }
        c.chunks = c.documents.reduce((sum, d) => sum + d.chunks, 0);
        c.state = c.documents.some(d => d.state === 'Failed') ? 'Needs attention' : c.documents.some(d => d.state !== 'Ready') ? 'Indexing' : 'Ready';
        renderDocuments(); renderOverview();
      });
      return row;
    }));
    if (!c.documents.length) {
      $('#document-rows').append(clone('source-empty'));
    }
  }
  function renderCollection() {
    const c = collection();
    if (!c) return;
    $('#collection-heading h1').textContent = c.title;
    $('#collection-breadcrumb').textContent = c.title; $('#collection-summary').textContent = c.description;
    $('#meta-level').textContent = c.level; $('#meta-embedder').textContent = c.embedder;
    $('#collection-dimensions').textContent = `${c.dimensions || '—'} dimensions`;
    $('#collection-revision').textContent = `Revision ${c.revision}`;
    renderDocuments();
    $('#reference-question').replaceChildren(...c.questions.map(q => new Option(q.question, q.id)), new Option('Custom question', 'custom'));
    const draft = drafts.get(c.id);
    selectedReference = draft?.reference || c.questions[0]?.id || 'custom';
    $('#reference-question').value = selectedReference;
    $('#collection-question').value = draft?.question ?? reference()?.question ?? '';
    $('#collection-answer-model').value = draft?.model ?? '';
    $('#collection-minimum').value = draft?.minimum ?? '.65';
    $('#review-note').value = draft?.note ?? '';
    $('#collection-file').value = '';
    lastResult = draft?.result || null;
    renderResult(); renderHistory();
  }
  function renderResult() {
    const q = reference();
    $('#reference-panel').hidden = !q;
    $('#expected-answer').textContent = q?.expected || '';
    $('#result-placeholder').hidden = !!lastResult;
    $('#retrieval-result').hidden = !lastResult?.hits;
    $('#no-evidence').hidden = !lastResult || !!lastResult.hits;
    $('#review-controls').hidden = !lastResult?.hits || !lastResult.model || !q;
    $('#comparison-panel').hidden = !lastResult?.compare;
    $('#evidence-items').replaceChildren();
    if (lastResult?.hits) {
      $('#retrieval-count').textContent = '1 matching sample excerpt';
      $('#result-mode').textContent = lastResult.model ? `Sample answer · ${lastResult.model}` : 'Retrieved evidence only';
      $('#model-answer').hidden = !lastResult.model;
      $('#model-answer').textContent = lastResult.answer;
      const item = clone('evidence-item');
      put(item, '[data-open-source]', q.source); put(item, '[data-similarity]', 'Similarity 0.842');
      put(item, '[data-excerpt]', q.excerpt); put(item, '[data-location]', q.page ? `Page ${q.page} · cited source` : 'Lines 1–8 · cited source');
      $('[data-open-source]', item).addEventListener('click', () => inspectSource({name: q.source}, q.excerpt, q.page));
      $('#evidence-items').append(item);
    }
    if (lastResult?.compare) {
      $('#comparison-rows').replaceChildren(...data.models.map((model, i) => {
        const row = clone('comparison-row');
        put(row, '[data-model]', model); put(row, '[data-answer]', i ? q.alternate : q.expected);
        put(row, '[data-citation]', q.page ? `${q.source} · p. ${q.page}` : `${q.source} · lines 1–8`);
        badge($('[data-review]', row), i ? 'Review missing / changed facts' : 'Matches sample reference');
        return row;
      }));
    }
    $$('[data-assessment]').forEach(b => b.setAttribute('aria-pressed', String(lastResult?.assessment === b.dataset.assessment)));
    $$('#collection-steps li').forEach((el, i) => {
      el.removeAttribute('aria-current');
      if (i === (lastResult?.hits ? 2 : 1)) el.setAttribute('aria-current', 'step');
    });
  }
  function retrieve(compare = false) {
    if (!$('#query-form').reportValidity()) return;
    const q = reference();
    const exactReference = q && $('#collection-question').value.trim() === q.question;
    if (!exactReference) { say('No prepared result for a custom question. Choose a reference question to preview retrieval; your text is retained.'); return; }
    const hits = collection().documents.some(d => d.state === 'Ready' && d.name === q.source) && Number($('#collection-minimum').value) <= .842;
    if (compare && !hits) { say('No matching evidence at this threshold. Retrieve before comparing answers.'); }
    lastResult = {hits, compare: compare && hits, model: compare ? data.models[0] : $('#collection-answer-model').value,
      answer: q.expected, assessment: '', question: q.question, reference: q.id};
    renderResult(); saveDraft();
    say(hits ? 'Prepared sample ready. No model was called. Compare facts and source citations before assessing.' : 'No matching sample evidence. No answer-model request would be made.');
  }
  function renderHistory() {
    const entries = history.filter(h => h.collection === state.collection);
    $('#history-rows').replaceChildren(...entries.map(h => {
      const row = clone('history-row'); put(row, '[data-question]', h.question); put(row, '[data-model]', h.model); put(row, '[data-review]', h.assessment); return row;
    }));
    $('#history-empty').hidden = !!entries.length;
  }
  function renderWorkflows() {
    const available = state.scenario === 'empty' ? [] : data.workflows;
    const search = $('#workflow-search').value.toLowerCase().trim(), filter = $('#workflow-state').value;
    const runs = available.filter(r => (!filter || category(r) === filter) && `${r.title} ${data.collections.find(c => c.id === r.collection)?.title} ${r.model}`.toLowerCase().includes(search));
    $('#workflow-list').replaceChildren(); $$('[data-lane]').forEach(el => el.replaceChildren());
    runs.forEach(run => {
      const item = clone('workflow-item');
      item.dataset.runId = run.id;
      put(item, '[data-title]', run.title); put(item, '[data-collection]', data.collections.find(c => c.id === run.collection)?.title || 'Deleted collection');
      put(item, '[data-model]', run.model); put(item, '[data-next]', next[run.state]); badge($('[data-state]', item), labels[run.state]);
      $('.workflow-choice', item).setAttribute('aria-pressed', String(run.id === state.workflow));
      $('.workflow-choice', item).addEventListener('click', () => { state.workflow = run.id; renderWorkflows(); route(); });
      (state.design === 'guide' ? $(`[data-lane="${category(run)}"]`) : $('#workflow-list')).append(item);
    });
    $('#workflow-list').hidden = state.design === 'guide'; $('#workflow-board').hidden = state.design !== 'guide' || !runs.length;
    $$('[data-lane-count]').forEach(el => { const count = runs.filter(r => category(r) === el.dataset.laneCount).length; el.textContent = count; $(`[data-lane-empty="${el.dataset.laneCount}"]`).hidden = !!count; });
    $('#workflow-empty').hidden = !!runs.length;
    $('#runs-kpi').textContent = available.length;
    $('#review-kpi').textContent = available.filter(r => category(r) === 'review').length;
    $('#retry-kpi').textContent = available.filter(r => category(r) === 'retry').length;
    $('#completed-kpi').textContent = available.filter(r => category(r) === 'completed').length;
    if (!runs.some(r => r.id === state.workflow) && runs.length) state.workflow = runs[0].id;
    // Refresh the selected row after filters choose a new first run.
    $$('.workflow-choice').forEach(button => button.setAttribute('aria-pressed', String(button.closest('[data-run-id]').dataset.runId === state.workflow)));
    $('#workflow-detail').hidden = !runs.length;
    if (runs.length) renderRun();
  }
  function renderRun() {
    const run = workflow(), c = data.collections.find(c => c.id === run.collection), q = c.questions.find(q => q.question === run.question) || c.questions[0];
    $('#run-title').textContent = run.title; badge($('#run-status'), labels[run.state]); $('#run-question').textContent = run.question || q.question;
    $('#run-collection').textContent = c.title;
    $('#run-collection').href = `#design=${state.design}&page=collection&collection=${c.id}`;
    $('#run-model').textContent = run.model; $('#run-draft-model').textContent = run.draft_model;
    $('#run-prompt').textContent = `${run.prompt} · v${run.version}`; $('#run-revision').textContent = c.revision;
    const currentStep = {ready_research: 0, review_research: 1, ready_draft: 2, failed_draft: 2, review_draft: 3, completed: 4}[run.state];
    $$('#run-steps li').forEach((el, i) => { el.removeAttribute('aria-current'); el.toggleAttribute('data-complete', i < currentStep); if (i === currentStep) el.setAttribute('aria-current', 'step'); });
    $('#research-output').hidden = run.state === 'ready_research';
    $('#research-answer').textContent = q.expected;
    badge($('#research-state'), currentStep >= 2 ? 'Accepted' : 'Awaiting review');
    $('#run-source').textContent = q.page ? `${q.source} · page ${q.page}` : `${q.source} · lines 1–8`;
    $('#run-error').hidden = run.state !== 'failed_draft';
    $('#draft-output').hidden = !['review_draft', 'completed'].includes(run.state);
    $('#draft-answer').textContent = `Business brief: ${q.expected} Source: ${q.source}${q.page ? `, page ${q.page}` : ', lines 1–8'}.`;
    $('#run-action').hidden = run.state === 'completed'; $('#run-action').textContent = `${next[run.state]} · sample`;
    $('#run-attempts').textContent = `Sample attempts: ${run.state === 'failed_draft' ? 'Research 1 accepted; draft 1 failed. Retry starts draft attempt 2.' : `${run.attempts || (run.state === 'ready_research' ? 0 : 1)} retained step attempt(s).`} No live requests or usage records.`;
  }
  function createWorkflow() {
    const options = (state.scenario === 'empty' ? [] : data.collections).filter(c => c.documents.some(d => d.state === 'Ready') && c.questions.length);
    if (!options.length) { say('Add and index a collection before creating a workflow.'); return; }
    $('#workflow-collection').replaceChildren(...options.map(c => new Option(c.title, c.id)));
    if (options.some(c => c.id === state.collection)) $('#workflow-collection').value = state.collection;
    $('#workflow-question').value = $('#collection-question').value || options[0].questions[0].question;
    $('#workflow-dialog').showModal();
  }
  $$('[data-design]').filter(el => el.tagName === 'BUTTON').forEach(el => el.addEventListener('click', () => {
    saveDraft(); state.design = el.dataset.design; renderShell(); renderOverview(); renderWorkflows();
  }));
  $$('[data-page]').forEach(el => el.addEventListener('click', () => openPage(el.dataset.page)));
  $$('[data-go]').forEach(el => el.addEventListener('click', e => { e.preventDefault(); openPage(el.dataset.go); }));
  window.addEventListener('hashchange', () => { saveDraft(); readRoute(); renderCollection(); renderOverview(); renderWorkflows(); renderShell(); });
  ['collection-search', 'collection-state', 'collection-sort'].forEach(id => $(`#${id}`).addEventListener('input', renderOverview));
  ['workflow-search', 'workflow-state'].forEach(id => $(`#${id}`).addEventListener('input', () => { renderWorkflows(); route(); }));
  $('#clear-collection-filters').addEventListener('click', () => { $('#collection-search').value = ''; $('#collection-state').value = ''; renderOverview(); });
  $('#clear-workflow-filters').addEventListener('click', () => { $('#workflow-search').value = ''; $('#workflow-state').value = ''; renderWorkflows(); route(); });
  $('#reference-question').addEventListener('change', () => {
    selectedReference = $('#reference-question').value;
    $('#collection-question').value = reference()?.question || ''; $('#review-note').value = ''; lastResult = null; renderResult(); saveDraft();
  });
  $('#collection-question').addEventListener('input', () => {
    if (reference() && $('#collection-question').value !== reference().question) { selectedReference = 'custom'; $('#reference-question').value = 'custom'; }
    lastResult = null; renderResult(); saveDraft();
  });
  ['collection-answer-model', 'collection-minimum'].forEach(id => $(`#${id}`).addEventListener('change', () => { lastResult = null; renderResult(); saveDraft(); }));
  $('#query-form').addEventListener('submit', e => { e.preventDefault(); retrieve(); });
  $('#compare-models').addEventListener('click', () => retrieve(true));
  $$('[data-assessment]').forEach(button => button.addEventListener('click', () => {
    if (!lastResult?.model || !lastResult.hits) return;
    lastResult.assessment = button.dataset.assessment;
    const entry = {collection: state.collection, question: lastResult.question, reference: lastResult.reference,
      model: lastResult.model, assessment: lastResult.assessment, note: $('#review-note').value};
    const old = history.findIndex(h => h.collection === entry.collection && h.reference === entry.reference && h.model === entry.model);
    if (old >= 0) history[old] = entry; else history.push(entry);
    renderResult(); renderHistory(); saveDraft(); say('Your sample assessment is retained in this tab.');
  }));
  $('#review-note').addEventListener('input', () => {
    const entry = history.find(h => h.collection === state.collection && h.reference === selectedReference && h.model === lastResult?.model);
    if (entry) entry.note = $('#review-note').value; saveDraft();
  });
  $('#export-review').addEventListener('click', () => {
    const report = {sample: true, collection: collection().title, revision: collection().revision,
      reference_checks: collection().questions, assessments: history.filter(h => h.collection === state.collection)};
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: 'application/json'}));
    const a = document.createElement('a'); a.href = url; a.download = `${state.collection}-sample-review.json`; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000); say('Sample review exported with its authored references and your assessments.');
  });
  $('#collection-file').addEventListener('change', () => {
    const file = $('#collection-file').files[0]; if (!file) return;
    const pdf = file.name.toLowerCase().endsWith('.pdf');
    if (!/\.(txt|md|csv|json|pdf)$/i.test(file.name) || file.size > (pdf ? 1024 * 1024 : 200 * 1024)) { say('Choose UTF-8 text up to 200 KiB or a selectable PDF up to 1 MiB.'); $('#collection-file').value = ''; return; }
    if (collection().documents.length >= 20) { say('This collection already has 20 source documents.'); return; }
    collection().documents.push({id: `sample-upload-${Date.now()}`, name: file.name, state: 'Pending', chunks: 0, text: 'Local filename preview only. This prototype does not parse or index the uploaded file.', page: null});
    renderDocuments(); renderOverview(); say('Filename added to the sample list. Select Index / retry to simulate indexing. No file content was read or sent.');
  });
  $('#scenario').addEventListener('change', () => {
    saveDraft(); state.scenario = $('#scenario').value; renderOverview(); renderWorkflows(); renderShell();
    if (state.scenario === 'empty' && state.page === 'collection') { state.page = 'overview'; renderShell(); }
  });
  $('#retry-connection').addEventListener('click', () => { state.scenario = 'normal'; $('#scenario').value = 'normal'; renderShell(); say('Sample status restored. Your unsent work is retained.'); });
  $$('[data-close]').forEach(el => el.addEventListener('click', () => el.closest('dialog').close()));
  $$('[data-create-collection]').forEach(el => el.addEventListener('click', () => {
    const count = state.scenario === 'empty' ? 0 : data.collections.length;
    $('#collection-capacity').textContent = count >= 5 ? 'All 5 collection slots are in use. Choose Empty workspace to try the creation flow.' : `${count} of 5 collection slots used.`;
    $('#new-collection-form button[type="submit"]').disabled = count >= 5;
    $('#collection-dialog').showModal();
  }));
  $('#new-collection-form').addEventListener('submit', e => {
    e.preventDefault();
    if (state.scenario !== 'empty' && data.collections.length >= 5) return;
    if (state.scenario === 'empty') { data.collections = []; data.workflows = []; state.scenario = 'normal'; $('#scenario').value = 'normal'; }
    const c = {id: `sample-${Date.now()}`, title: $('#collection-title').value, domain: 'Business evaluation', description: 'Add sources to begin testing a business use case.',
      level: $('#new-collection-level').value, state: 'Needs attention', documents: [], questions: [], chunks: 0, embedder: $('#collection-embedder').value, dimensions: null, revision: 1};
    data.collections.push(c); $('#collection-dialog').close(); openPage('collection', c.id); renderOverview(); say('Sample collection created. Add a source, then explicitly index it.');
  });
  $('#collection-workflow').addEventListener('click', createWorkflow); $('#new-workflow').addEventListener('click', createWorkflow);
  $('#workflow-collection').addEventListener('change', () => { const c = data.collections.find(c => c.id === $('#workflow-collection').value); $('#workflow-question').value = c.questions[0].question; });
  $('#new-workflow-form').addEventListener('submit', e => {
    e.preventDefault();
    const c = data.collections.find(c => c.id === $('#workflow-collection').value);
    if (!c || !c.questions.some(q => q.question === $('#workflow-question').value.trim())) { say('Use a prepared reference question to simulate this run. Your custom question stays in the form.'); return; }
    const run = {id: `sample-run-${Date.now()}`, title: $('#workflow-title').value, collection: c.id, state: 'ready_research', question: $('#workflow-question').value,
      model: $('#research-model').value, draft_model: $('#draft-model').value, prompt: 'Evidence-first research', version: 2, attempts: 0};
    data.workflows.push(run); state.workflow = run.id; $('#workflow-dialog').close(); openPage('workflows'); say('Sample plan created. Review the configuration, then explicitly start research.');
  });
  $('#run-action').addEventListener('click', () => {
    const run = workflow();
    const transitions = {ready_research: 'review_research', review_research: 'ready_draft', ready_draft: 'review_draft', failed_draft: 'review_draft', review_draft: 'completed'};
    const previous = run.state; run.state = transitions[run.state] || run.state;
    if (['ready_research', 'ready_draft', 'failed_draft'].includes(previous)) run.attempts = (run.attempts || (previous === 'failed_draft' ? 2 : 0)) + 1;
    renderWorkflows(); route(); say(`Sample stage: ${labels[run.state]}. No model was called. Acceptance is a manual review decision.`);
  });
  $('#run-open-collection').addEventListener('click', () => openPage('collection', workflow().collection));
  $('#run-collection').addEventListener('click', e => { e.preventDefault(); openPage('collection', workflow().collection); });
  $('#run-source').addEventListener('click', () => {
    const c = data.collections.find(c => c.id === workflow().collection), q = c.questions.find(q => q.question === workflow().question) || c.questions[0];
    // Use this run's collection rather than the last collection opened in the other page.
    $('#source-name').textContent = q.source; $('#source-meta').textContent = `${q.page ? `Page ${q.page}` : 'Lines 1–8'} · collection revision ${c.revision} · sample provenance`;
    $('#source-text').textContent = q.excerpt; $('#source-dialog').showModal();
  });
  $('#open-prompts').addEventListener('click', () => $('#prompt-dialog').showModal());
  $('#theme').addEventListener('click', () => {
    const mode = $('[data-theme-form] [name="mode"]'); mode.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark'; mode.dispatchEvent(new Event('change', {bubbles: true})); updateThemeLabel();
  });
  function updateThemeLabel() { $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode'; }
  document.addEventListener('weightroom:theme', updateThemeLabel);
  $('#reset').addEventListener('click', () => {
    data = structuredClone(seed); drafts.clear(); history.length = 0; lastResult = null;
    state.collection = 'procurement'; state.workflow = 'supplier-brief'; state.scenario = 'normal'; $('#scenario').value = 'normal';
    ['collection-search', 'collection-state', 'workflow-search', 'workflow-state'].forEach(id => { $(`#${id}`).value = ''; });
    renderCollection(); renderOverview(); renderWorkflows(); renderShell(); say('Sample data and tab-local assessments reset.');
  });
  readRoute(); renderCollection(); renderOverview(); renderWorkflows(); renderShell(); updateThemeLabel();
})();
