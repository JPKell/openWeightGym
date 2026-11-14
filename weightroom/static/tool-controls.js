/** Shared readable MCP schemas, argument forms and exact result rendering. */
window.OWGTools = (() => {
  const field = id => document.getElementById(id);
  /** Render text, number, boolean and enum fields; retain raw JSON for arbitrary schemas. */
  function argumentsFor(prefix, schema = {}, values = {}) {
    const box = field(prefix + '-arguments');
    box.replaceChildren();
    field(prefix + '-raw').value = JSON.stringify(values, null, 2);
    const complex = Object.values(schema.properties || {}).some(p => !['string','integer','number','boolean'].includes(p.type) || p.oneOf || p.anyOf || p.$ref);
    field(prefix + '-raw-enabled').checked = complex;
    for (const [name, property] of Object.entries(schema.properties || {})) {
      const fragment = field('tool-argument-field').content.cloneNode(true), label = fragment.querySelector('label');
      let input = fragment.querySelector('input');
      const required = schema.required?.includes(name);
      label.textContent = `${name} (${property.type || 'JSON'}${required ? ', required' : ''})`;
      input.id = prefix === 'tools' ? `tool-argument-${name}` : `${prefix}-argument-${box.children.length}`; label.htmlFor = input.id;
      if (property.enum || property.type === 'boolean') {
        const select = document.createElement('select'); select.className = 'form-select'; select.id = input.id;
        select.append(new Option('Choose…',''));
        for (const value of property.enum || [true,false]) select.append(new Option(String(value),JSON.stringify(value)));
        input.replaceWith(select); input = select; input.dataset.json = 'true';
      }
      input.dataset.name = name; input.dataset.type = property.type || ''; input.required = !!required;
      if (['integer','number'].includes(property.type)) { input.type = 'number'; input.step = property.type === 'integer' ? '1' : 'any'; }
      const value = values[name] ?? property.default;
      if (value !== undefined) input.value = input.dataset.json ? JSON.stringify(value) : String(value);
      fragment.querySelector('p').textContent = [property.description, property.enum ? 'Values: ' + property.enum.map(v => JSON.stringify(v)).join(', ') : ''].filter(Boolean).join(' · ');
      box.append(fragment);
    }
  }
  /** Read typed arguments without coercing omitted values or losing explicit false and zero. */
  function read(prefix) {
    if (field(prefix + '-raw-enabled').checked) {
      const value = JSON.parse(field(prefix + '-raw').value);
      if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('Arguments must be a JSON object.');
      return value;
    }
    const args = {};
    for (const input of field(prefix + '-arguments').querySelectorAll('[data-name]')) {
      if (!input.reportValidity()) throw new Error('Complete the required arguments.');
      if (input.value !== '') args[input.dataset.name] = input.dataset.json || input.dataset.type !== 'string' ? JSON.parse(input.value) : input.value;
    }
    return args;
  }
  /** Show every content part and the complete unmodified MCP JSON, using text nodes only. */
  function result(prefix, value) {
    field(prefix + '-answer').textContent = (value.content || []).map(p => p.type === 'text' ? p.text : JSON.stringify(p,null,2)).join('\n');
    field(prefix + '-error-flag').textContent = `isError: ${value.isError === undefined ? 'not supplied' : String(value.isError)}`;
    field(prefix + '-result-json').textContent = JSON.stringify(value,null,2);
  }
  return {argumentsFor, read, result};
})();
