/** Private Tool chat uses shared schemas and exact results, with explicit approvals only. */
(() => {
  const root = document.getElementById('tool-chat'); if (!root) return;
  const $ = id => document.getElementById('tool-chat-' + id);
  let created = false, ref = root.dataset.ref, tools = [], pending = false, polling, lastHistory = '', lastProposals = '';
  /** Call the cookie-authenticated proxy; the caller's key stays on the server. */
  async function api(path = '', body) {
    const response = await fetch('/chat/tools/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: {'content-type':'application/json'}, body:body === undefined ? undefined : JSON.stringify(body)});
    const result = await response.json(); if (!response.ok) throw new Error(result.error?.message || 'Tool chat is unavailable.'); return result;
  }
  /** Keep current draft controls in place while updating history and pending proposals. */
  async function refresh() {
    if (!ref) return;
    try {
      const talk = await api(ref);
      pending = talk.pending; $('stop').hidden = !pending;
      const history = JSON.stringify(talk.messages);
      if (history !== lastHistory) {
        lastHistory = history; const nodes = [];
        for (const [index, message] of talk.messages.entries()) {
          const fragment = document.getElementById('tool-chat-message').content.cloneNode(true);
          fragment.querySelector('[data-role]').textContent = message.model || message.role;
          fragment.querySelector('[data-content]').textContent = message.exact ? '' : message.content;
          fragment.querySelector('[data-error]').textContent = message.error || '';
          const exact = fragment.querySelector('[data-exact]'); exact.hidden = !message.exact;
          if (message.exact) {
            const prefix = `history-${index}`;
            for (const element of exact.querySelectorAll('[id]')) element.id = element.id.replace('message-result',prefix);
            // Attach first so the shared result renderer can address this instance by its stable prefix.
            nodes.push({fragment, prefix, result:message.exact});
          } else nodes.push({fragment});
          const call = fragment.querySelector('[data-call]'); call.hidden = !message.tool_call;
          call.querySelector('pre').textContent = JSON.stringify({proposed:message.tool_call,executed:message.executed_call,decision:message.decision},null,2);
        }
        $('history').replaceChildren(...nodes.map(one => one.fragment));
        for (const one of nodes) if (one.result) OWGTools.result(one.prefix,one.result);
      }
      const proposals = talk.tool_state?.pending || [], signature = JSON.stringify(proposals);
      if (signature !== lastProposals) {
        lastProposals = signature;
        $('proposals').replaceChildren(...proposals.map(call => {
          const fragment = document.getElementById('tool-chat-proposal').content.cloneNode(true), form = fragment.querySelector('form');
          form.dataset.callId = call.id;
          form.querySelector('[data-name]').textContent = call.function.name;
          form.querySelector('[data-description]').textContent = tools.find(one => one.name === call.function.name)?.description || '';
          form.elements.arguments.value = call.function.arguments; form.elements.arguments.readOnly = true;
          form.addEventListener('submit',async event => {
            event.preventDefault(); const status = form.querySelector('[data-status]'), action = event.submitter?.value;
            if (action === 'edit') { form.elements.arguments.readOnly = false; form.elements.arguments.focus(); status.textContent = 'Edit arguments, then choose Run.'; return; }
            try {
              const args = action === 'decline' ? undefined : JSON.parse(form.elements.arguments.value);
              form.querySelectorAll('button').forEach(button => button.disabled = true);
              await api(ref + '/actions',{do:'decision',call_id:call.id,approve:action !== 'decline',arguments:args,reason:form.elements.reason.value});
              await refresh(); schedule();
            } catch (error) { status.textContent = error.message; form.querySelectorAll('button').forEach(button => button.disabled = false); }
          }); return fragment;
        }));
      }
      $('status').textContent = pending ? 'Working…' : proposals.length ? 'Review each proposed call before it runs.' : 'Ready.';
      $('composer').querySelector('button').disabled = pending || !!proposals.length || !$('composer').elements.model.options.length;
      $('direct').querySelector('button').disabled = pending || !!proposals.length || !tools.length;
      if (pending) schedule();
    } catch (error) { $('status').textContent = error.message; }
  }
  /** Poll only while a registered chat task is running, leaving proposal drafts untouched. */
  function schedule() { clearTimeout(polling); polling = setTimeout(refresh,700); }
  /** Render grouped searchable tool descriptions and their readable schema properties. */
  function listing() {
    const search = $('search').value.toLowerCase(), groups = new Map();
    for (const tool of tools.filter(one => (one.name+' '+(one.description || '')).toLowerCase().includes(search))) {
      const server = tool.name.split('__')[0]; if (!groups.has(server)) groups.set(server,[]); groups.get(server).push(tool);
    }
    const children = [];
    for (const [server, entries] of groups) {
      const heading = document.getElementById('tool-chat-server-heading').content.cloneNode(true); heading.querySelector('[data-server-heading]').textContent = server; children.push(heading);
      for (const tool of entries) {
        const fragment = document.getElementById('tool-chat-tool-row').content.cloneNode(true);
        fragment.querySelector('summary').textContent = tool.name;
        fragment.querySelector('[data-description]').textContent = tool.description || '';
        const schema = tool.inputSchema || {}, properties = fragment.querySelector('[data-properties]');
        for (const [name, property] of Object.entries(schema.properties || {})) {
          const pair = document.getElementById('tool-chat-property').content.cloneNode(true), term = pair.querySelector('dt'), detail = pair.querySelector('dd');
          term.textContent = `${name} · ${property.type || 'JSON'}${schema.required?.includes(name) ? ' · required' : ''}`;
          detail.textContent = [property.description,property.enum ? 'Values: '+property.enum.map(v => JSON.stringify(v)).join(', ') : ''].filter(Boolean).join(' · ');
          properties.append(pair);
        }
        fragment.querySelector('pre').textContent = JSON.stringify(schema,null,2); children.push(fragment);
      }
    }
    if (!children.length) { const empty = document.createElement('p'); empty.className = 'text-secondary'; empty.textContent = 'No tools match this search.'; children.push(empty); }
    $('list').replaceChildren(...children);
  }
  /** Select a tool using the shared schema form. */
  function select() {
    const tool = tools.find(one => one.name === $('tool').value);
    $('description').textContent = tool?.description || ''; OWGTools.argumentsFor('tool-chat',tool?.inputSchema || {});
  }
  /** Refresh exactly the caller-visible list for the selected narrowing set. */
  async function connect() {
    try {
      if (root.dataset.toolset && $('set').options.length === 1) { $('set').append(new Option(root.dataset.toolset)); $('set').value = root.dataset.toolset; }
      const result = await api('tools?toolset='+encodeURIComponent($('set').value)); tools = result.tools;
      for (const name of result.toolsets) if (![...$('set').options].some(option => option.value === name)) $('set').append(new Option(name));
      $('tool').replaceChildren(...tools.map(tool => new Option(tool.name))); listing(); select();
      $('status').textContent = tools.length ? 'Ready.' : 'No tools are visible in this set.';
      if (ref) await refresh();
    } catch (error) { $('status').textContent = error.message; $('direct').querySelector('button').disabled = true; }
  }
  /** Create the owner-private record lazily, then keep its bookmarkable URL. */
  async function ensure() {
    if (!ref) { ref = (await api('',{})).id; root.dataset.ref = ref; created = true; document.getElementById('chat-main').dataset.chatRef = ref; }
  }
  $('direct').addEventListener('submit',async event => {
    event.preventDefault();
    try { const args = OWGTools.read('tool-chat'); await ensure(); await api(ref+'/actions',{do:'direct',name:$('tool').value,arguments:args,toolset:$('set').value}); if (created) { location.assign('/chat/tools/'+ref); return; } await refresh(); schedule(); }
    catch (error) { $('status').textContent = error.message; }
  });
  $('composer').addEventListener('submit',async event => {
    event.preventDefault();
    try { await ensure(); await api(ref+'/actions',{do:'say',content:$('composer').elements.content.value,model:$('composer').elements.model.value,toolset:$('set').value}); if (created) { location.assign('/chat/tools/'+ref); return; } $('composer').elements.content.value = ''; await refresh(); schedule(); }
    catch (error) { $('status').textContent = error.message; }
  });
  $('stop').addEventListener('click',async () => { try { await api(ref+'/actions',{do:'stop'}); await refresh(); schedule(); } catch (error) { $('status').textContent = error.message; } });
  $('tool').addEventListener('change',select); $('search').addEventListener('input',listing); $('set').addEventListener('change',connect);
  connect();
})();
