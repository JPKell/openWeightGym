// Portable design study: all filtering, evidence, exports and simulated controls stay in this tab.
(() => {
  'use strict';
  const nodeLabel = window.suiteNodeLabel || (name => name === 'front' ? 'Control node' : name);
  const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
  const records = JSON.parse($('#sample-data').textContent);
  const descriptions = {
    signal: 'Signal desk pairs a clear activity overview with an outcome-first request briefing. Start with the problem, then follow the evidence.',
    ledger: 'Selected for the live pages · Operations ledger keeps filters beside compact rows, and identity beside timing and diagnostic evidence.',
    explorer: 'Trace explorer · Select requests beside an instant preview. Open the full request to follow parent spans across services before inspecting compute stages.',
  };
  const fields = {q:'query', window:'window', status:'status', node:'requests-preview-node', model:'model',
    user:'requests-preview-user', group:'requests-preview-group', service:'service', caller:'caller',
    application:'application', role:'role', trace_id:'trace_id', ingress:'ingress', error_code:'error_code'};
  const snapshot = Date.parse('2026-11-13T19:00:00Z');
  const numeric = new Set(['id', 'total', 'waited', 'ttft', 'input', 'cost']);
  let layout = 'ledger', view = 'directory', scenario = 'normal', selected = records[0];
  let quick = 'all', sort = 'id', ascending = false, page = 0, filtered = [], evidence = 'diagnostics';
  let lastLink = null, stopArmed = false, messageTimer;
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const number = n => n == null ? '—' : Number(n).toLocaleString('en-US');
  const seconds = n => n == null ? '—' : `${n.toFixed(3)} s`;
  const time = at => new Date(at).toLocaleTimeString('en-GB', {timeZone:'America/Vancouver', hour12:false});
  const isError = r => ['Failed', 'Timeout'].includes(r.state);
  const ending = r => !r.active && r.state !== 'No ending';
  const color = state => isError({state}) || state === 'Error' ? 'red' : state === 'Done' ? 'green' : ['Running', 'Queued'].includes(state) ? 'azure' : 'secondary';
  function badge(el, state) { el.textContent = state; el.className = `badge bg-${color(state)}-lt`; }
  function fact(target, label, value) {
    const row = clone('#fact-row'); row.querySelector('dt').textContent = label;
    row.querySelector('dd').textContent = value == null || value === '' ? '—' : value; target.append(row);
  }
  function tell(message) { clearTimeout(messageTimer); $('#feedback').textContent = message; messageTimer = setTimeout(() => { $('#feedback').textContent = ''; }, 5000); }
  async function copy(value, message) {
    try { await navigator.clipboard.writeText(value); tell(message); }
    catch { tell('Clipboard unavailable. Use the export to save this evidence.'); }
  }
  function metadata(record) {
    const {prompt, answer, feedback, ...rest} = record;
    return rest;
  }
  function download(name, text, type) {
    const url = URL.createObjectURL(new Blob([text], {type}));
    const a = document.createElement('a'); a.href = url; a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function hash() {
    const params = new URLSearchParams({design:layout, view, id:String(selected.id), state:scenario, quick, sort, order:ascending?'asc':'desc', page:String(page), rows:$('#page-size').value, evidence});
    Object.entries(fields).forEach(([key,id]) => { if ($(`#${id}`).value) params.set(key, $(`#${id}`).value); });
    return `#${params}`;
  }
  function save(push = false) { history[push ? 'pushState' : 'replaceState'](null, '', hash()); }
  function percentile(values, fraction) {
    if (!values.length) return null;
    values.sort((a,b) => a-b); return values[Math.ceil(values.length*fraction)-1];
  }
  function renderPeek() {
    $('#peek-id').textContent = `REQUEST #${selected.id}`;
    $('#peek-model').textContent = selected.model; badge($('#peek-state'), selected.state);
    $('#peek-facts').replaceChildren();
    [['Node', nodeLabel(selected.node)], ['User / caller', `${selected.user} / ${selected.caller}`],
      ['Compute total', selected.active ? `Elapsed ${seconds(selected.elapsed)}` : seconds(selected.total)],
      ['Queue wait', seconds(selected.waited)], ['First token', seconds(selected.ttft)]].forEach(([k,v]) => fact($('#peek-facts'), k, v));
    $('#peek-reason').textContent = selected.error || (selected.legacy ? 'No detailed compute stages were recorded.' : 'Open the full record for measured stages, trace context and retained evidence.');
  }
  function renderMetrics() {
    const ended = filtered.filter(ending), errors = ended.filter(isError).length;
    const measured = ended.filter(r => r.total != null);
    $('#count').textContent = number(filtered.length);
    $('#active-count').textContent = filtered.filter(r => r.active).length;
    $('#error-rate').textContent = ended.length ? `${(errors/ended.length*100).toFixed(1)}%` : '—';
    $('#error-note').textContent = `${errors} server errors / ${ended.length} endings`;
    $('#p95').textContent = seconds(percentile(measured.map(r => r.total), .95));
    $('#p95-note').textContent = `${measured.length} measured / ${ended.length} endings`;
    $('#attention-errors').textContent = filtered.filter(isError).length;
    $('#attention-slow').textContent = filtered.filter(r => r.total > 10).length;
    $('#attention-active').textContent = filtered.filter(r => r.active).length;
    $('#service-rows').replaceChildren();
    [...new Set(ended.map(r => r.service))].forEach(service => {
      const rows = ended.filter(r => r.service === service), measured = rows.filter(r => r.total != null);
      const errors = rows.filter(isError).length, row = clone('#service-row');
      const values = {name:service, count:rows.length, errors:`${errors} / ${rows.length} (${(errors/rows.length*100).toFixed(1)}%)`,
        p50:seconds(percentile(measured.map(r => r.total), .5)), p95:seconds(percentile(measured.map(r => r.total), .95))};
      Object.entries(values).forEach(([key,value]) => { row.querySelector(`[data-service-${key}]`).textContent = value; });
      $('#service-rows').append(row);
    });
    $('#service-empty').hidden = ended.length > 0;
  }
  function renderRows() {
    const val = key => $(`#${fields[key]}`).value.trim().toLowerCase();
    const query = val('q');
    filtered = scenario === 'empty' ? [] : records.filter(r => {
      if (snapshot-Date.parse(r.at) > Number(val('window'))*60000) return false;
      if (query && ![r.id,r.request_id,r.trace_id,r.model,r.node,r.user,r.caller,r.error,r.error_code].join(' ').toLowerCase().includes(query)) return false;
      if (['status','node','model','user','group','service','caller','application','role'].some(key => val(key) && String(r[key === 'status' ? 'state' : key]).toLowerCase() !== val(key))) return false;
      if (['trace_id','ingress','error_code'].some(key => val(key) && !r[key === 'ingress' ? 'request_id' : key].toLowerCase().includes(val(key)))) return false;
      return quick === 'all' || quick === 'errors' && isError(r) || quick === 'slow' && r.total > 10 || quick === 'active' && r.active;
    });
    filtered.sort((a,b) => {
      const av = a[sort], bv = b[sort];
      if (av == null || bv == null) return av == null ? (bv == null ? 0 : 1) : -1;
      return (numeric.has(sort) ? av-bv : String(av).localeCompare(String(bv))) * (ascending?1:-1);
    });
    const size = Number($('#page-size').value), pages = Math.max(1, Math.ceil(filtered.length/size));
    page = Math.min(Math.max(page, 0), pages-1);
    $('#request-rows').replaceChildren();
    filtered.slice(page*size, (page+1)*size).forEach(r => {
      const row = clone('#request-row'); row.dataset.id = r.id;
      row.setAttribute('aria-selected', String(r.id === selected.id));
      const link = row.querySelector('[data-open]'); link.textContent = `#${r.id}`;
      const params = new URLSearchParams(hash().slice(1)); params.set('id', r.id); params.set('view','detail'); link.href = `#${params}`;
      link.addEventListener('click', event => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault(); lastLink = r.id; selected = r;
        if (layout === 'explorer') { renderRows(); save(); $(`#request-rows [data-id="${r.id}"] [data-open]`)?.focus(); }
        else openDetail();
      });
      const values = {at:time(r.at)+' PST', model:r.model, node:nodeLabel(r.node), user:r.user, caller:r.caller,
        total:r.active ? `${r.elapsed.toFixed(3)} elapsed` : r.total == null ? '—' : r.total.toFixed(3),
        waited:r.waited == null ? '—' : r.waited.toFixed(3), ttft:r.ttft == null ? '—' : r.ttft.toFixed(3),
        input:number(r.input), output:number(r.output), cost:r.cost == null ? '—' : r.cost.toFixed(5)};
      Object.entries(values).forEach(([field,value]) => { row.querySelector(`[data-field="${field}"]`).textContent = value; });
      badge(row.querySelector('[data-field="state"]'), r.state); $('#request-rows').append(row);
      row.querySelector('[data-field="level"]').replaceChildren(clone(`#security-${r.level}`));
    });
    $('#empty-rows').hidden = filtered.length > 0;
    $('#result-count').textContent = `${filtered.length} matching requests`;
    $('#page-summary').textContent = filtered.length ? `Showing ${page*size+1}–${Math.min((page+1)*size,filtered.length)} of ${filtered.length}` : '0 requests';
    $('#previous').disabled = page === 0; $('#next').disabled = page >= pages-1;
    $('#export-directory').disabled = !filtered.length;
    $('#table-hint').textContent = layout === 'explorer' ? 'Select a request to preview →' : 'Open a request to investigate ↗';
    $$('[data-quick]').forEach(button => { button.setAttribute('aria-pressed',String(button.dataset.quick === quick)); button.classList.toggle('active', button.dataset.quick === quick); });
    $$('[data-sort]').forEach(button => button.closest('th').setAttribute('aria-sort',button.dataset.sort === sort ? ascending?'ascending':'descending' : 'none'));
    renderMetrics(); renderPeek();
  }
  function chooseEvidence(name, persist = true) {
    evidence = ['diagnostics','content','activity','raw'].includes(name) ? name : 'diagnostics';
    $$('[data-panel]').forEach(el => { el.hidden = el.dataset.panel !== evidence; });
    $$('[data-evidence]').forEach(el => { el.classList.toggle('active',el.dataset.evidence === evidence); el.setAttribute('aria-pressed',String(el.dataset.evidence === evidence)); });
    if (persist) save();
  }
  function renderDetail() {
    const r = selected, legacy = scenario === 'legacy' || r.legacy;
    $('#detail-heading h1').textContent = `Request #${r.id}`;
    $('#detail-heading h1').tabIndex = -1;
    badge($('#detail-state'),r.state); $('#asked-at').textContent = time(r.at);
    $('#detail-model').textContent = r.model; $('#detail-node').textContent = `${nodeLabel(r.node)} · ${r.service}`;
    $('#detail-path').textContent = r.path;
    $('.rq-verdict').dataset.error = String(isError(r));
    $('#verdict-label').textContent = r.active ? 'IN PROGRESS / SAMPLE SNAPSHOT' : 'REQUEST OUTCOME';
    $('#verdict-title').textContent = legacy ? 'Detailed compute timing is unavailable.' :
      r.state === 'Failed' ? 'The request failed on compute.' : r.state === 'Timeout' ? 'The compute deadline was exceeded.' :
      r.state === 'Refused' ? 'The request was refused before generation.' : r.state === 'Cancelled' ? 'The request was cancelled.' :
      r.active ? (r.state === 'Queued' ? 'Waiting for compute capacity.' : 'The request is generating a response.') :
      r.load > 0 ? 'Model loading was the largest delay.' : 'The request completed successfully.';
    $('#verdict-note').textContent = legacy ? 'The ending or detailed stage history was not retained. Missing values remain unavailable; no latency is inferred.' :
      r.error || (r.active ? 'This is a retained sample of an active request. The elapsed measurement is partial; it is not a completed total.' :
      r.load > 0 ? `Loading took ${seconds(r.load)} of ${seconds(r.total)} on compute. Inspect the stages before attributing latency to generation.` :
      `Answered by ${r.model} on ${nodeLabel(r.node)}. Use the measured stages and service trace to inspect the path.`);
    $('#detail-total-label').textContent = r.active ? 'Elapsed on compute' : 'Compute total';
    $('#detail-total').textContent = legacy ? '—' : seconds(r.active?r.elapsed:r.total);
    $('#detail-wait').textContent = legacy ? '—' : seconds(r.waited);
    $('#detail-ttft').textContent = legacy ? '—' : seconds(r.ttft);
    $('#detail-rate').textContent = legacy || r.rate == null ? '—' : `${r.rate.toFixed(1)} tok/s`;
    $('#request-facts').replaceChildren();
    [['User',r.user],['Caller',r.caller],['Application',r.application],['Group',r.group],['Requested role',r.role],
      ['Asked at',time(r.at)+' PST'],['Ended at',ending(r)?time(new Date(Date.parse(r.at)+(r.total||r.elapsed)*1000).toISOString())+' PST':'—'],
      ['HTTP status',r.status],['Tokens in / out',`${number(r.input)} / ${number(r.output)}`],
      ['Recorded cost',r.cost == null ? '—' : `$${r.cost.toFixed(5)}`]].forEach(([k,v]) => fact($('#request-facts'),k,v));
    $('#ingress-id').textContent = r.request_id; $('#trace-id').textContent = r.trace_id;
    $('#phase-rows').replaceChildren(); $('#phase-empty').hidden = !legacy;
    const phases = legacy ? [] : r.phases.filter(p => r.state !== 'Queued' || p.label === 'Admission / queue');
    const duration = phases.reduce((sum,p) => sum+p.seconds,0);
    phases.forEach(p => {
      const row = clone('#phase-row'); row.querySelector('[data-phase-label]').textContent = p.label;
      row.querySelector('[data-phase-value]').textContent = seconds(p.seconds);
      const bar = row.querySelector('[data-phase-bar]'); bar.style.width = `${duration ? p.seconds/duration*100 : 0}%`; bar.dataset.zero = String(!p.seconds);
      row.querySelector('[data-phase-basis]').textContent = r.active ? 'Partial snapshot on compute' : p.basis;
      $('#phase-rows').append(row);
    });
    $('#timeline-total').textContent = legacy ? 'Unavailable' : `${seconds(duration)} ${r.active?'elapsed':'measured'}`;
    $('#timing-note').textContent = legacy ? 'Native and detailed wall-clock measurements were not recorded for this sample.' :
      r.active ? 'Active stage durations can grow. The sample stream below advances locally.' :
      'Zero model-loading time means no load stage was needed. Native engine timings overlap these stages and are not added to this total.';
    $('#span-rows').replaceChildren();
    r.spans.forEach((span,i) => {
      const row = clone('#span-row'), button = row.querySelector('[data-span-open]');
      button.textContent = `${'↳ '.repeat(span.depth)}${span.operation}`;
      row.querySelector('[data-span-host]').textContent = `${span.service} / ${nodeLabel(span.machine)}`;
      badge(row.querySelector('[data-span-outcome]'),span.outcome);
      row.querySelector('[data-span-time]').textContent = legacy && i===3 ? '—' : seconds(span.seconds);
      row.querySelector('[data-span-id]').textContent = span.id;
      row.querySelector('[data-span-parent]').textContent = `parent: ${span.parent}`;
      function inspect() {
        $$('#span-rows tr').forEach(el => el.setAttribute('aria-selected',String(el === row)));
        $('#span-detail').textContent = `${span.operation} · ${span.service} / ${nodeLabel(span.machine)} · ${span.outcome}. Span ${span.id}; parent ${span.parent}. ${span.seconds == null || legacy && i===3 ? 'No completed duration was recorded.' : `Locally measured duration: ${seconds(span.seconds)}.`} ${i===3 && r.error ? r.error : ''}`;
      }
      button.addEventListener('click',inspect); $('#span-rows').append(row); if (i === 3) inspect();
    });
    $('#failure-alert').hidden = !r.error; $('#failure-text').textContent = r.error;
    $('#diagnostic-facts').replaceChildren();
    [['Error code',r.error_code || 'None recorded'],['Route',r.hosted?'Hosted':'Local compute'],['Selected model',r.model],
      ['Selection reason',`Role ${r.role}: available model on ${nodeLabel(r.node)}`],['Context',`${number(r.context)} tokens`],
      ['Placement',r.cards.join(', ')],['Images sent','No'],['Tools offered','0'],['Tool execution','Not recorded'],
      ['Model fingerprint',r.fingerprint],['Engine build',r.service === 'LoadCoach'?r.build:'Not applicable'],
      ['Admission wait estimate','Not recorded']].forEach(([k,v]) => fact($('#diagnostic-facts'),k,v));
    const permitted = scenario !== 'restricted' && r.retained;
    $('#retention-note').textContent = scenario === 'restricted' ? 'Retained text is restricted. Reading another user’s request requires permission to read kept text.' :
      r.retained ? 'Illustrative retained text. Metadata and content have separate retention and permission rules.' : 'Request and response text are no longer retained. Metadata remains available.';
    $('#kept-content').hidden = !permitted;
    $('#prompt').textContent = permitted ? r.prompt : '';
    $('#answer').textContent = permitted ? r.answer || (r.active ? 'Generation is in progress. Use the sample stream below.' : 'No answer was retained for this request.') : '';
    $('#feedback-controls').hidden = r.state !== 'Done';
    $$('[data-rating]').forEach(el => el.setAttribute('aria-pressed',String(Number(el.dataset.rating) === r.feedback && r.feedback !== 0)));
    $('#activity-rows').replaceChildren();
    r.events.forEach(event => {
      const row = clone('#activity-row');
      Object.entries({at:time(event.at),kind:event.kind,what:event.what,detail:event.detail}).forEach(([k,v]) => { row.querySelector(`[data-event-${k}]`).textContent = v; });
      $('#activity-rows').append(row);
    });
    $('#raw-metadata').textContent = JSON.stringify(metadata(r),null,2);
    $('#stop-panel').hidden = !r.active || scenario === 'stale' || scenario === 'restricted';
    $('#stream-content').textContent = r.state === 'Queued' ? 'Waiting for admission…' : 'Checking the effective running source…';
    $('#stop').textContent = 'Stop sample request'; stopArmed = false;
    $('#previous-record').disabled = records.indexOf(r) <= 0;
    $('#next-record').disabled = records.indexOf(r) >= records.length-1;
    chooseEvidence(evidence, false);
  }
  function render() {
    $('#workspace').dataset.layout = layout; $('#design-note').textContent = descriptions[layout];
    $('#directory-view').hidden = view !== 'directory'; $('#detail-view').hidden = view !== 'detail';
    $('#source-alert').hidden = scenario !== 'stale';
    $$('[data-design]').forEach(el => { el.classList.toggle('active',el.dataset.design === layout); el.setAttribute('aria-pressed',String(el.dataset.design === layout)); });
    $$('[data-view]').forEach(el => { el.classList.toggle('active',el.dataset.view === view); el.setAttribute('aria-pressed',String(el.dataset.view === view)); });
    renderRows(); renderDetail();
  }
  function openDetail() { view = 'detail'; render(); save(true); $('#detail-heading h1').focus(); window.scrollTo(0,0); }
  function back() { view = 'directory'; render(); save(true); const link = $(`#request-rows [data-id="${lastLink||selected.id}"] [data-open]`); (link||$('#query')).focus(); }
  function reset() {
    $('#filters').reset(); quick = 'all'; page = 0;
    $$('#requests-preview-user option').forEach(option => { option.hidden = false; });
    if (scenario === 'empty') { scenario = 'normal'; $('#scenario').value = scenario; }
    render(); save();
  }
  function restore() {
    const p = new URLSearchParams(location.hash.slice(1));
    layout = Object.hasOwn(descriptions,p.get('design'))?p.get('design'):'ledger'; view = p.get('view')==='detail'?'detail':'directory';
    scenario = ['normal','empty','stale','restricted','legacy','running'].includes(p.get('state'))?p.get('state'):'normal';
    $('#scenario').value = scenario;
    quick = ['all','errors','slow','active'].includes(p.get('quick'))?p.get('quick'):'all';
    sort = [...$$('[data-sort]')].map(el => el.dataset.sort).includes(p.get('sort'))?p.get('sort'):'id'; ascending = p.get('order')==='asc';
    page = Math.max(0,Number.parseInt(p.get('page'),10)||0); $('#page-size').value = ['12','24','72'].includes(p.get('rows'))?p.get('rows'):'12';
    Object.entries(fields).forEach(([key,id]) => { const el = $(`#${id}`); el.value = p.get(key)||(key==='window'?'60':''); if (el.tagName==='SELECT' && el.selectedIndex<0) el.selectedIndex=0; });
    selected = records.find(r => String(r.id)===p.get('id'))||records[0];
    $('#missing-alert').hidden = !p.get('id') || records.some(r => String(r.id)===p.get('id'));
    if (!$('#missing-alert').hidden) view = 'directory';
    evidence = p.get('evidence')||'diagnostics'; render();
  }
  $$('[data-design]').forEach(el => el.addEventListener('click',() => { layout = el.dataset.design; render(); save(); }));
  $$('[data-view]').forEach(el => el.addEventListener('click',() => { if (el.dataset.view==='detail') openDetail(); else back(); }));
  $('#scenario').addEventListener('change',() => {
    scenario = $('#scenario').value;
    if (scenario==='running') selected = records.find(r => r.active);
    if (scenario==='legacy') selected = records.find(r => r.legacy);
    render(); save();
  });
  $('#filters').addEventListener('submit',event => event.preventDefault());
  $('#filters').addEventListener('input',event => {
    if (event.target.id==='requests-preview-group') $('#requests-preview-user').value = '';
    const group = $('#requests-preview-group').value;
    $$('#requests-preview-user option[data-groups]').forEach(option => { option.hidden = !!group && !JSON.parse(option.dataset.groups).includes(group); });
    page=0; renderRows(); save();
  });
  $$('[data-clear]').forEach(el => el.addEventListener('click',reset));
  $$('[data-quick]').forEach(el => el.addEventListener('click',() => { quick=el.dataset.quick; page=0; renderRows(); save(); }));
  $$('[data-sort]').forEach(el => el.addEventListener('click',() => { ascending=sort===el.dataset.sort?!ascending:true; sort=el.dataset.sort; renderRows(); save(); }));
  $('#page-size').addEventListener('change',() => { page=0; renderRows(); save(); });
  $('#previous').addEventListener('click',() => { page--; renderRows(); save(); });
  $('#next').addEventListener('click',() => { page++; renderRows(); save(); });
  $('#open-peek').addEventListener('click',openDetail);
  $('#back').addEventListener('click',event => { event.preventDefault(); back(); });
  $$('.rq-outline a').forEach(link => link.addEventListener('click',event => {
    event.preventDefault(); const target = $(link.getAttribute('href'));
    target.tabIndex = -1; target.focus({preventScroll:true}); target.scrollIntoView({block:'start'});
  }));
  ['previous','next'].forEach((direction,i) => { $(`#${direction}-record`).addEventListener('click',() => { selected=records[records.indexOf(selected)+(i?1:-1)]; openDetail(); }); });
  $$('[data-evidence]').forEach(el => el.addEventListener('click',() => chooseEvidence(el.dataset.evidence)));
  $('#jump-evidence').addEventListener('click',() => { chooseEvidence('diagnostics'); $('#evidence-panel').scrollIntoView({block:'start'}); });
  ['trace','ingress'].forEach(kind => { $(`#find-${kind}`).addEventListener('click',() => { $('#filters').reset(); quick='all'; page=0; $(`#${kind==='trace'?'trace_id':'ingress'}`).value=selected[kind==='trace'?'trace_id':'request_id']; $('#advanced-filters').open=true; back(); }); });
  $('#copy-link').addEventListener('click',() => copy(location.href,'Request link copied.'));
  $('#copy-metadata').addEventListener('click',() => copy(JSON.stringify(metadata(selected),null,2),'Request metadata copied.'));
  $('#export-record').addEventListener('click',() => download(`request-${selected.id}.json`,JSON.stringify(metadata(selected),null,2),'application/json'));
  $('#export-directory').addEventListener('click',() => {
    const columns=['id','at','state','model','node','user','caller','total','waited','ttft','input','output','cost','request_id','trace_id','error_code'];
    const cell=value => `"${String(value??'').replaceAll('"','""')}"`;
    download('requests.csv',[columns,...filtered.map(r => columns.map(k => r[k]))].map(row => row.map(cell).join(',')).join('\r\n'),'text/csv');
  });
  $$('[data-rating]').forEach(el => el.addEventListener('click',() => { selected.feedback=Number(el.dataset.rating); renderDetail(); tell('Sample feedback updated in this tab.'); }));
  $('#advance').addEventListener('click',() => { $('#stream-content').textContent += '\nConfirmed: the process imports code from its installed checkout.'; tell('Sample stream advanced.'); });
  $('#stop').addEventListener('click',() => {
    if (!stopArmed) { stopArmed=true; $('#stop').textContent='Confirm stop sample'; return; }
    selected.state='Cancelled'; selected.active=false; selected.total=selected.elapsed; selected.spans[3].outcome='Cancelled'; selected.spans[3].seconds=selected.elapsed;
    render(); save(); tell('Sample request cancelled in this tab.');
  });
  $('#theme').addEventListener('click',() => {
    const mode=document.documentElement.dataset.bsTheme==='dark'?'light':'dark';
    document.documentElement.dataset.bsTheme=mode; $('[name=mode]').value=mode;
    $('[data-theme-form]').dispatchEvent(new Event('input',{bubbles:true}));
    $('#theme').textContent=mode==='dark'?'Light mode':'Dark mode';
    document.dispatchEvent(new CustomEvent('weightroom:theme'));
  });
  const access = [{ip:'192.0.2.24', user:'jordan', first:'10:04:12', last:'10:59:48', accepted:28,rejected:0},
    {ip:'198.51.100.18', user:'research-bot', first:'10:02:41', last:'10:58:09',accepted:44,rejected:2},
    {ip:'203.0.113.7', user:'Unknown key', first:'10:18:33', last:'10:49:17', accepted:0,rejected:7}];
  access.forEach(a => { const row=clone('#access-row'); Object.entries(a).forEach(([k,v]) => { row.querySelector(`[data-access-${k}]`).textContent=v; }); $('#access-rows').append(row); });
  addEventListener('hashchange',restore); addEventListener('popstate',restore);
  restore(); $('#theme').textContent=document.documentElement.dataset.bsTheme==='dark'?'Light mode':'Dark mode';
})();
