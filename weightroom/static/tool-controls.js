/** Shared readable MCP schemas, argument forms and exact result rendering. */
window.OWGTools = (() => {
  const field = id => document.getElementById(id);
  /** Render text, number, boolean and enum fields; retain raw JSON for arbitrary schemas. */
  function argumentsFor(prefix, schema = {}, values = {}) {
    const box = field(prefix + '-arguments'), raw = field(prefix + '-raw-enabled');
    const mode = () => {
      for (const input of box.querySelectorAll('[data-name]')) input.disabled = raw.checked;
      field(prefix + '-raw').disabled = !raw.checked;
    };
    raw.onchange = mode;
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
      const limits = bound(input, property);
      const value = values[name] ?? property.default;
      if (value !== undefined) input.value = input.dataset.json ? JSON.stringify(value) : String(value);
      input.dispatchEvent(new Event('input'));  // a default outside the bounds is marked at once
      fragment.querySelector('p').textContent = [property.description, property.enum ? 'Values: ' + property.enum.map(v => JSON.stringify(v)).join(', ') : '', limits].filter(Boolean).join(' · ');
      box.append(fragment);
    }
    mode();
  }
  const finite = value => typeof value === 'number' && Number.isFinite(value);
  /** Apply the schema's numeric range, string length and pattern to a typed field, and describe them.

  Only these constraints are checked here; the server still validates the whole schema. */
  function bound(input, property) {
    const said = [];
    if (['integer','number'].includes(property.type)) {
      const low = finite(property.exclusiveMinimum) ? property.exclusiveMinimum : property.minimum;
      const high = finite(property.exclusiveMaximum) ? property.exclusiveMaximum : property.maximum;
      const lowOpen = finite(property.exclusiveMinimum), highOpen = finite(property.exclusiveMaximum);
      if (finite(low)) {
        if (lowOpen && property.type === 'integer') input.min = String(Math.floor(low) + 1); else if (!lowOpen) input.min = String(low);
        said.push((lowOpen ? 'more than ' : 'at least ') + low);
      }
      if (finite(high)) {
        if (highOpen && property.type === 'integer') input.max = String(Math.ceil(high) - 1); else if (!highOpen) input.max = String(high);
        said.push((highOpen ? 'less than ' : 'at most ') + high);
      }
      if (lowOpen || highOpen) input.addEventListener('input', () => {
        const number = Number(input.value);
        input.setCustomValidity(input.value !== '' && ((lowOpen && number <= low) || (highOpen && number >= high)) ? 'Must be ' + said.join(' and ') + '.' : '');
      });
    } else if (property.type === 'string') {
      if (Number.isSafeInteger(property.minLength) && property.minLength > 0) { input.minLength = property.minLength; said.push('at least ' + property.minLength + ' characters'); }
      if (Number.isSafeInteger(property.maxLength)) { input.maxLength = property.maxLength; said.push('at most ' + property.maxLength + ' characters'); }
      if (typeof property.pattern === 'string') {
        let pattern = null;
        try { pattern = new RegExp(property.pattern, 'u'); } catch { /* an unreadable pattern is left to the server */ }
        if (pattern) {
          said.push('matching ' + property.pattern);
          input.addEventListener('input', () => input.setCustomValidity(input.value && !pattern.test(input.value) ? 'Must match ' + property.pattern + '.' : ''));
        }
      }
    }
    return said.length ? 'Allowed: ' + said.join(', ') : '';
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
      if (!input.reportValidity()) throw new Error(input.validity.valueMissing ? 'Complete the required arguments.' : `Correct the ${input.dataset.name} argument: ${input.validationMessage}`);
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
  /** Empty all three result panels, so no earlier call's result outlives its session (QA-053). */
  function clear(prefix) {
    for (const part of ['-answer', '-error-flag', '-result-json']) field(prefix + part).textContent = '';
  }
  return {argumentsFor, read, result, clear};
})();
