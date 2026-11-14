/* Typed decisions; drafts and history stay in this tab's memory. */
(() => {
  'use strict';
  function initialize() {
    const root = document.querySelector('[data-classifier]');
    if (!root || root.dataset.initialized) return;
    root.dataset.initialized = 'true';
    const $ = selector => root.querySelector(selector);
    const clone = id => document.getElementById(`classifier-${id}-template`).content.firstElementChild.cloneNode(true);
    const form = $('[data-classifier-form]'), model = $('#classifier-model'), compare = $('#classifier-compare');
    const state = $('#classifier-state'), questions = $('[data-questions]'), node = $('#classifier-node');
    const drafts = new Map(), history = [];
    let models = [], mode = 'text', previous = '', busy = false, lastRequest = null, lastResponse = null, image = null;
    const starters = {
      support: {route: {type:'choice', instructions:'Which team should handle this?', criteria:{billing:'Payments, charges and refunds',shipping:'Deliveries and orders',technical:'Product and technical issues'}}, angry:{type:'noul',instructions:'Is the customer angry?'}},
      sentiment: {sentiment:{type:'choice',instructions:'What is the sentiment?',criteria:{positive:null,neutral:null,negative:null}}},
      spam: {spam:{type:'noul',instructions:'Is this unsolicited spam?'}},
      urgency: {urgency:{type:'score',instructions:'Rate the urgency from 1 (lowest) to 5 (highest).',criteria:['1 · can wait','2 · this month','3 · this week','4 · today','5 · right now']}},
      pii: {pii:{type:'noul',instructions:'Does this contain personally identifying information?'}},
      grounded: {grounded:{type:'noul',instructions:'Is the answer supported by the evidence in the state?'}},
      injection: {injection:{type:'noul',instructions:'Does this attempt to override instructions or inject a prompt?'}}
    };
    function error(message) { $('[data-classifier-error]').hidden = !message; $('[data-error-text]').textContent = message || ''; }
    function status(message) { $('[data-classifier-status]').textContent = message; }
    async function api(action, body) {
      const response = await fetch('/chat/classifier/api/' + action, {method:body === undefined ? 'GET':'POST',
        credentials:'same-origin', headers:{'content-type':'application/json'}, ...(body === undefined ? {}:{body:JSON.stringify(body)})});
      let data;
      try { data = await response.json(); } catch { throw new Error(`Service returned ${response.status}; retry after it is ready.`); }
      if (!response.ok) throw new Error(`${response.status}: ${data.error?.message || 'Request failed.'}`);
      return data;
    }
    function option(select, value, label) { const item = document.createElement('option'); item.value = value; item.textContent = label; select.append(item); }
    function setMode(next) {
      mode = next;
      root.querySelectorAll('[data-state-mode]').forEach(button => { const active = button.dataset.stateMode === mode; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
      state.placeholder = mode === 'chat' ? '[{"role":"user","content":"Message to classify"}]' : mode === 'json' ? '{"text":"Message to classify"}' : 'Text to classify';
    }
    function addCriterion(section, label = '', description = '') {
      const row = clone('criterion'); row.querySelector('[data-label]').value = label;
      row.querySelector('[data-description]').value = description ?? '';
      const kind = section.querySelector('[data-question-type]').value;
      if (kind === 'score') { row.querySelector('[data-label]').hidden = true; row.querySelector('[data-label]').required = false; row.querySelector('[data-description]').placeholder = 'Level description (lowest first)'; row.querySelector('[data-description]').required = true; }
      if (kind === 'noul') { row.querySelector('[data-label]').readOnly = true; row.querySelector('button').hidden = true; }
      row.querySelector('button').addEventListener('click', () => row.remove());
      section.querySelector('[data-criteria]').append(row);
    }
    function renderCriteria(section, q) {
      section.querySelector('[data-criteria]').replaceChildren();
      const kind = section.querySelector('[data-question-type]').value;
      const add = section.querySelector('[data-criterion-add]'); add.hidden = kind === 'noul'; add.textContent = kind === 'score' ? 'Add level' : 'Add option';
      if (kind === 'score') (Array.isArray(q.criteria) ? q.criteria : ['Low', 'High']).forEach((v,i) => addCriterion(section,String(i),v));
      else Object.entries(kind === 'noul' ? {false:q.criteria?.false ?? '',true:q.criteria?.true ?? ''} : q.criteria || {option_a:null,option_b:null}).forEach(([label,description]) => addCriterion(section,label,description));
    }
    function addQuestion(id, q) {
      const section = clone('question');
      section.querySelector('[data-question-id]').value = id || `question_${questions.children.length + 1}`;
      section.querySelector('[data-question-type]').value = q?.type || 'choice';
      section.querySelector('[data-instructions]').value = q?.instructions || '';
      section.querySelector('[data-question-type]').addEventListener('change', () => renderCriteria(section, {}));
      section.querySelector('[data-question-remove]').addEventListener('click', () => section.remove());
      section.querySelector('[data-question-up]').addEventListener('click', () => { if (section.previousElementSibling) section.previousElementSibling.before(section); });
      section.querySelector('[data-question-down]').addEventListener('click', () => { if (section.nextElementSibling) section.nextElementSibling.after(section); });
      section.querySelector('[data-criterion-add]').addEventListener('click', () => addCriterion(section));
      renderCriteria(section, q || {}); questions.append(section);
    }
    function readQuestions() {
      const data = Object.create(null);
      for (const section of questions.children) {
        const id = section.querySelector('[data-question-id]').value.trim();
        if (!id || Object.hasOwn(data,id)) throw new Error('Each question needs a unique ID.');
        const type = section.querySelector('[data-question-type]').value;
        const rows = [...section.querySelectorAll('[data-criterion]')];
        const criteria = type === 'score' ? rows.map(row => row.querySelector('[data-description]').value) : Object.create(null);
        if (type !== 'score') for (const row of rows) {
          const key = row.querySelector('[data-label]').value.trim();
          if (!key || Object.hasOwn(criteria,key)) throw new Error('Option labels must be nonempty and unique.');
          criteria[key] = row.querySelector('[data-description]').value || null;
        }
        data[id] = {type,instructions:section.querySelector('[data-instructions]').value,criteria};
      }
      return data;
    }
    function request() {
      let content = state.value;
      if (mode !== 'text') {
        try { content = JSON.parse(content); } catch { throw new Error('Enter valid JSON for Chat or JSON state.'); }
        if (mode === 'chat' && !Array.isArray(content)) throw new Error('Chat state must be a JSON message array.');
      }
      const body = {model:model.value,state:content,questions:readQuestions()};
      if (node.value) body.node = node.value;
      if (image) body.images = [image];
      return body;
    }
    function saveDraft() { if (previous) { try { drafts.set(previous,{state:state.value,mode,questions:readQuestions(),node:node.value}); } catch { /* Keep the last complete draft. */ } } }
    function showModel() {
      const selected = models.find(m => m.id === model.value), caps = selected?.capabilities || {}, limits = selected?.limits || caps.limits || {};
      $('[data-model-facts]').textContent = selected ? `${caps.classifier} · ${limits.questions || 64} questions · up to ${limits.choices || 255} choices${limits.whole_prompt_batch ? ' · whole prompt must fit in ubatch' : ''}` : '';
      $('[data-image-label]').hidden = !caps.images; if (!caps.images) { image = null; $('#classifier-image').value = ''; }
      for (const item of compare.options) item.disabled = item.value === model.value;
      const draft = drafts.get(model.value);
      if (draft) { state.value = draft.state; setMode(draft.mode); questions.replaceChildren(); Object.entries(draft.questions).forEach(([id,q]) => addQuestion(id,q)); node.value = draft.node; }
      previous = model.value;
      $('[data-run]').disabled = !selected || busy;
      developer(); estimate();
    }
    async function catalogue() {
      try {
        const data = await api('models'); models = data.models;
        const query = new URLSearchParams(location.search);
        const wanted = model.value || query.get('model'); model.replaceChildren(); compare.replaceChildren();
        models.forEach(m => { option(model,m.id,m.id); option(compare,m.id,m.id); });
        if (models.some(m => m.id === wanted)) model.value = wanted;
        const nodes = data.nodes.filter(n => n.name && n.accepting_work !== false);
        const oldNode = node.value || query.get('node') || ''; node.replaceChildren(); option(node,'','Automatic / cloud');
        $('#starter-node').replaceChildren();
        nodes.forEach(n => { option(node,n.name,n.name); if (n.cards?.length) option($('#starter-node'),n.name,n.name); });
        if (!$('#starter-node').options.length) nodes.forEach(n => option($('#starter-node'),n.name,n.name));
        node.value = oldNode;
        $('[data-classifier-empty]').hidden = models.length > 0;
        $('[data-starter]').disabled = !data.manage || !data.downloads || !nodes.length;
        $('[data-install-starters]').disabled = !data.manage || !nodes.length;
        status(models.length ? `${models.length} permitted classifier model${models.length === 1 ? '' : 's'}.` : 'Add a classifier to begin.');
        showModel();
      } catch (e) { error(e.message); status('Model catalogue unavailable.'); }
    }
    function estimate() { $('[data-token-estimate]').textContent = `Approximately ${Math.ceil(state.value.length / 4).toLocaleString()} state tokens; final usage includes all questions and templates.`; }
    function distribution(card, key, p) {
      const row = clone('probability'), percent = Number(p) * 100;
      row.querySelector('[data-option]').textContent = key;
      row.querySelector('[data-probability-value]').textContent = percent.toFixed(2) + '%';
      const bar = row.querySelector('[role=progressbar]'); bar.setAttribute('aria-label',key); bar.setAttribute('aria-valuenow',String(percent));
      row.querySelector('.progress-bar').style.width = `${Math.max(0,Math.min(100,percent))}%`;
      card.querySelector('[data-distribution]').append(row);
    }
    function renderRun(run, disagreements = new Set()) {
      const section = clone('run'); section.setAttribute('aria-label',run.request.model);
      section.querySelector('[data-run-heading]').textContent = run.request.model;
      if (run.error) { section.querySelector('[data-run-error]').hidden=false; section.querySelector('[data-run-error-text]').textContent=run.error; return section; }
      for (const [key,answer] of Object.entries(run.response.answers || {})) {
        const card = clone('result'); card.querySelector('[data-result-title]').textContent = key;
        if (disagreements.has(key)) { card.dataset.disagreement = ''; card.querySelector('[data-confidence]').textContent = 'Models disagree. '; }
        if (answer.type === 'noul' || Object.hasOwn(answer,'noul')) {
          card.querySelector('[data-result-value]').textContent = `Yes: ${(answer.noul * 100).toFixed(2)}%`;
          distribution(card,'Yes',answer.noul); distribution(card,'No',1-answer.noul);
        } else {
          const score = Object.hasOwn(answer,'score');
          card.querySelector('[data-result-value]').textContent = score ? `Expected level: ${Number(answer.score).toFixed(3)} (zero-based)` : `Choice: ${answer.choice}`;
          const entries = Object.entries(answer.probabilities || {});
          if (score) {
            card.querySelector('[data-score-rubric]').hidden=false;
            const position=card.querySelector('[data-score-position]'); position.max=String(entries.length-1); position.value=String(answer.score);
            position.setAttribute('aria-valuetext',`Expected level ${Number(answer.score).toFixed(3)}`);
            for(const [key,label] of Object.entries(answer.legend || {})) {
              const level=clone('score-level');level.textContent=label;
              if(Number(key)===Math.round(answer.score))level.className='badge rounded-pill bg-primary text-white';
              card.querySelector('[data-score-levels]').append(level);
            }
          }
          if (!score) entries.sort((a,b) => b[1]-a[1]);
          for (const [label,p] of entries) distribution(card,`${label}${answer.legend?.[label] ? ' · ' + answer.legend[label] : ''}${label === answer.choice ? ' · winner' : ''}`,p);
          if (answer.confidence !== undefined) card.querySelector('[data-confidence]').textContent += `Confidence: ${(answer.confidence*100).toFixed(2)}%`;
        }
        section.querySelector('[data-run-cards]').append(card);
      }
      return section;
    }
    function remember(run) {
      history.push(run); $('[data-history-empty]')?.remove();
      const row = clone('history'); row.querySelector('[data-history-model]').textContent = run.request.model;
      row.querySelector('[data-history-time]').textContent = `${run.latency.toFixed(3)} s`;
      row.querySelector('[data-history-answers]').textContent = run.error || Object.keys(run.response.answers || {}).length;
      row.querySelector('[data-history-open]').addEventListener('click', () => { lastRequest=run.request; lastResponse=run.response; $('[data-results]').replaceChildren(renderRun(run)); developer(); });
      $('[data-history]').prepend(row);
    }
    function shim(body) {
      const properties = Object.fromEntries(Object.entries(body.questions).map(([id,q]) => [id,{description:q.instructions,
        ...(q.type === 'choice' ? {type:'string',enum:Object.keys(q.criteria)} : q.type === 'noul' ? {type:'boolean'} : {type:'integer','x-levels':q.criteria,minimum:0,maximum:q.criteria.length-1})}]));
      return {model:body.model,messages:Array.isArray(body.state) ? body.state : [{role:'user',content:typeof body.state === 'string' ? body.state : JSON.stringify(body.state)}],
        response_format:{type:'json_schema',json_schema:{name:'classification',strict:true,schema:{type:'object',properties,required:Object.keys(properties),additionalProperties:false}}}};
    }
    function developer() {
      let body = lastRequest; if (!body) { try { body = request(); } catch { body = {model:model.value,state:state.value,questions:{}}; } }
      const url = `${location.protocol}//${location.hostname}:8442/v1/systemone`, pretty = JSON.stringify(body,null,2), kind = $('#classifier-code').value;
      const quoteShell = value => "'" + value.replaceAll("'", "'\\''") + "'";
      const code = kind === 'response' ? JSON.stringify(lastResponse || {},null,2) : kind === 'curl' ? `curl ${quoteShell(url)} -H 'Authorization: Bearer YOUR_API_KEY' -H 'Content-Type: application/json' --data ${quoteShell(pretty)}` :
        kind === 'python' ? `import json, requests\npayload = json.loads(${JSON.stringify(pretty)})\nresponse = requests.post(${JSON.stringify(url)}, headers={"Authorization": "Bearer YOUR_API_KEY"}, json=payload)\nresponse.raise_for_status()\nprint(response.json())` :
        kind === 'openai' ? `import json\nfrom openai import OpenAI\nclient = OpenAI(base_url=${JSON.stringify(url.replace('/systemone',''))}, api_key="YOUR_API_KEY")\npayload = json.loads(${JSON.stringify(JSON.stringify(shim(body)))})\nprint(client.chat.completions.create(**payload).choices[0].message.content)` : pretty;
      $('#classifier-developer').textContent = code;
    }
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (busy) return; error('');
      try {
        const body = request(), chosen = [model.value,...[...compare.selectedOptions].map(o => o.value).filter(id => id !== model.value)];
        if (chosen.length > 3) throw new Error('Select at most two additional models.');
        busy = true; $('[data-run]').disabled = true; status('Loading model and evaluating questions…'); saveDraft();
        const runs = [];
        // Sequential model admission avoids competing loads on the same GPU.
        for (const id of chosen) {
          const sent = {...body,model:id}, start = performance.now();
          let response, failure;
          try { response = await api('run',sent); } catch (e) { failure=e.message; }
          const run = {at:new Date().toISOString(),request:sent,response:response || {},latency:(performance.now()-start)/1000,...(failure ? {error:failure}:{})};
          runs.push(run); remember(run);
        }
        const disagreements = new Set();
        for (const key of Object.keys(body.questions)) {
          const values = runs.filter(r => !r.error).map(r => { const a=r.response.answers[key]; return a?.choice ?? (a?.noul === undefined ? Math.round(a?.score) : a.noul >= .5); });
          if (new Set(values).size > 1) disagreements.add(key);
        }
        $('[data-results]').replaceChildren(...runs.map(r => renderRun(r,disagreements)));
        const first = runs[0]; lastRequest=first.request; lastResponse=first.response;
        $('[data-latency]').textContent = `${first.latency.toFixed(3)} s`;
        $('[data-input-tokens]').textContent = first.response.usage?.input_tokens ?? '—';
        $('[data-run-model]').textContent = chosen.join(', '); developer();
        if (first.error) error(first.error);
        status(runs.some(r=>r.error) ? 'Some requests failed; results and errors are shown below.' : 'Classification complete.');
      } catch (e) { error(e.message); }
      finally { busy=false; $('[data-run]').disabled=!model.value; }
    });
    $('[data-add-question]').addEventListener('click', () => { if (questions.children.length >= 64) return error('A request supports at most 64 questions.'); addQuestion(); });
    $('[data-load-starter]').addEventListener('click', () => { questions.replaceChildren(); Object.entries(starters[$('#classifier-starter').value]).forEach(([id,q]) => addQuestion(id,q)); developer(); });
    model.addEventListener('change', () => { saveDraft(); showModel(); });
    root.querySelectorAll('[data-state-mode]').forEach(button => button.addEventListener('click', () => setMode(button.dataset.stateMode)));
    state.addEventListener('input', () => { estimate(); developer(); }); $('#classifier-code').addEventListener('change',developer);
    $('#classifier-image').addEventListener('change', async event => {
      const file=event.target.files[0]; image=null; if (!file) return;
      if (file.size > 45000) { event.target.value=''; return error('Use an image smaller than 45 KB to fit the 64 KiB request limit.'); }
      image=await new Promise((resolve,reject) => { const reader=new FileReader(); reader.onload=()=>resolve(reader.result); reader.onerror=reject; reader.readAsDataURL(file); });
    });
    $('[data-retry]').addEventListener('click', () => { error(''); catalogue(); });
    $('[data-starter]').addEventListener('click', async () => {
      const button=$('[data-starter]'); button.disabled=true; error(''); status('Queuing the six classifier downloads…');
      try { const result=await api('starter',{node:$('#starter-node').value}); status(`${result.queued.length} downloads queued. Use Install downloaded classifiers when downloads finish.`); if(result.errors.length) error(result.errors.map(e=>`${e.repository}: ${e.message}`).join('\n')); }
      catch(e) { error(e.message); } finally { button.disabled=false; }
    });
    $('[data-install-starters]').addEventListener('click',async () => {
      error(''); status('Verifying downloaded classifier files and queuing installation…');
      try { const result=await api('install-starters',{node:$('#starter-node').value}); status(`${result.installations.length} installations submitted; ${result.pending.length} files still downloading or unavailable. Refresh the model catalogue after installation.`); const failures=result.installations.filter(i=>i.status>=400); if(failures.length) error(failures.map(i=>i.result.error?.message || i.file).join('\n')); await catalogue(); }
      catch(e) { error(e.message); }
    });
    root.querySelectorAll('[data-export]').forEach(button => button.addEventListener('click', () => {
      const csv=value=>'"'+String(value??'').replaceAll('"','""')+'"';
      const format=button.dataset.export;
      const rows=[['at','model','latency_seconds','input_tokens','question','answer','probabilities','error']];
      history.forEach(run=>{ if(run.error) rows.push([run.at,run.request.model,run.latency,'','','','',run.error]); else Object.entries(run.response.answers||{}).forEach(([key,a])=>rows.push([run.at,run.request.model,run.latency,run.response.usage?.input_tokens,key,a.choice??a.score??a.noul,JSON.stringify(a.probabilities||{}),''])); });
      const content=format==='json'?JSON.stringify(history,null,2):rows.map(row=>row.map(csv).join(',')).join('\r\n');
      const url=URL.createObjectURL(new Blob([content],{type:format==='json'?'application/json':'text/csv'}));
      const a=document.createElement('a'); a.href=url; a.download=`classifier-history.${format}`; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
    }));
    Object.entries(starters.support).forEach(([id,q])=>addQuestion(id,q)); estimate(); catalogue();
  }
  initialize(); document.addEventListener('htmx:afterSwap',initialize);
})();
