// Structured defaults retain unsupported keys in the Advanced JSON object.
(() => {
  'use strict';
  const fields = id => [...document.querySelectorAll('[data-json-source]')].filter(input => input.dataset.jsonSource === id);
  const parse = id => {
    const value = JSON.parse(document.getElementById(id).value || '{}');
    if (!value || Array.isArray(value) || typeof value !== 'object') throw new Error('Advanced settings must be a JSON object.');
    return value;
  };
  window.SuiteModelFields = {
    load(id, value) {
      const rest = {...value};
      fields(id).forEach(input => {
        const key = input.dataset.jsonField;
        if (input.type === 'checkbox') input.checked = Boolean(value[key]);
        else {
          if (input.tagName === 'SELECT' && value[key] !== undefined && ![...input.options].some(option => option.value === String(value[key]))) input.add(new Option(String(value[key]), String(value[key])));
          input.value = value[key] ?? '';
        }
        delete rest[key];
      });
      document.getElementById(id).value = JSON.stringify(rest, null, 2);
    },
    read(id) {
      const value = parse(id);
      fields(id).forEach(input => {
        if (Object.hasOwn(value, input.dataset.jsonField)) throw new Error('Use the labelled field for ' + input.dataset.jsonField + '.');
        if (input.type === 'checkbox') value[input.dataset.jsonField] = input.checked;
        else if (input.value !== '') value[input.dataset.jsonField] = input.dataset.scalar === 'boolean' ? input.value === 'true' : input.type === 'number' ? Number(input.value) : input.value;
      });
      return value;
    }
  };
})();
