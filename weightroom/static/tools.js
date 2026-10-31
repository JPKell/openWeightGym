(() => {
  /** Find one MCP exerciser control by its prefixed element ID. */
  const $ = id => document.getElementById(`tools-${id}`);
  if (!$('console')) return;
  let tools = [], session = '', number = 0;
  /** Send an authenticated MCP request and validate its JSON-RPC response. */
  async function ask(method, params = {}) {
    const response = await fetch('/tools/message', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: {jsonrpc: '2.0', id: ++number, method, params}, toolset: $('set').value,
        'mcp-session-id': session, 'x-loadcoach-trace': $('trace').value, 'x-loadcoach-sensitivity': $('level').value})});
    const answer = await response.json();
    if (answer.error) throw new Error(typeof answer.error === 'string' ? answer.error : answer.error.message);
    if (!response.ok) throw new Error(`ToolLocker said ${response.status}.`);
    if (method === 'initialize') session = response.headers.get('mcp-session-id') || '';
    return answer.result;
  }
  /** Render the selected tool's schema and argument controls. */
  function show() {
    const tool = tools.find(tool => tool.name === $('tool').value);
    $('description').textContent = tool?.description || '';
    $('call').disabled = !tool;
    const schema = tool?.inputSchema || {};
    $('arguments').replaceChildren(...Object.entries(schema.properties || {}).map(([name, property]) => {
      const box = document.createElement('div'), label = document.createElement('label'), input = document.createElement('input');
      box.className = 'col-md-6'; label.className = 'form-label'; input.className = 'form-control';
      input.id = `tool-argument-${name}`; label.htmlFor = input.id;
      label.textContent = `${name} (${property.type || 'JSON'}${schema.required?.includes(name) ? ', required' : ''})`;
      input.required = schema.required?.includes(name) || false;
      input.dataset.name = name; input.dataset.type = property.type || ''; input.title = property.description || '';
      box.append(label, input); return box;
    }));
  }
  /** Initialize the MCP session and retrieve its permitted tool catalogue. */
  async function connect() {
    session = ''; tools = []; $('call').disabled = true; $('connect').disabled = true;
    $('state').textContent = 'Connecting…'; $('answer').textContent = ''; $('status').textContent = '';
    try {
      await ask('initialize', {protocolVersion: '2025-06-18', capabilities: {}, clientInfo: {name: 'WeightRoom', version: '1'}});
      tools = (await ask('tools/list')).tools || [];
      $('state').textContent = `${tools.length} tools available to your account.`;
    } catch (error) { $('state').textContent = error.message; }
    finally { $('connect').disabled = false; }
    $('tool').replaceChildren(...tools.map(tool => new Option(tool.name))); show();
  }
  $('connect').addEventListener('click', connect); $('set').addEventListener('change', connect);
  $('tool').addEventListener('change', show);
  $('call').addEventListener('click', async () => {
    const args = {};
    try {
      for (const input of $('arguments').querySelectorAll('input')) {
        if (!input.reportValidity()) return;
        if (input.value) args[input.dataset.name] = input.dataset.type === 'string' ? input.value : JSON.parse(input.value);
      }
    } catch { $('status').textContent = 'Enter valid JSON for non-text arguments.'; return; }
    $('call').disabled = true; $('connect').disabled = true; $('set').disabled = true; $('status').textContent = 'Waiting…';
    try {
      const result = await ask('tools/call', {name: $('tool').value, arguments: args});
      $('answer').textContent = (result.content || []).map(piece => piece.text || JSON.stringify(piece)).join('\n');
      $('status').textContent = result.isError ? 'Tool failed or refused.' : 'Returned.';
    } catch (error) { $('status').textContent = error.message; }
    finally { $('call').disabled = false; $('connect').disabled = false; $('set').disabled = false; }
  });
})();
