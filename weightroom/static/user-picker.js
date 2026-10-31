// Searchable account dropdowns; the native select remains the form's submitted value.
(() => {
  /** Enhance user filters with accessible searchable dropdown controls. */
  function initialize() {
    for (const group of document.querySelectorAll('[data-users-groups]')) {
      if (group.dataset.ready) continue;
      group.dataset.ready = 'yes';
      const select = group.querySelector('[data-user-select]'), filter = group.querySelector('[data-group-select]');
      if (!select) continue;
      const options = [...select.options].map(option => option.cloneNode(true));
      const wrapper = select.parentElement, toggle = document.createElement('button'), panel = document.createElement('div');
      toggle.type = 'button'; toggle.className = 'form-select form-select-sm text-start';
      toggle.setAttribute('aria-haspopup', 'dialog'); toggle.setAttribute('aria-expanded', 'false');
      toggle.setAttribute('aria-label', 'Select user');
      panel.id = `${select.id}-panel`; panel.className = 'user-picker-panel bg-body border rounded shadow'; panel.hidden = true;
      panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-label', 'Select user'); toggle.setAttribute('aria-controls', panel.id);
      const search = document.createElement('input'), list = document.createElement('div');
      search.type = 'search'; search.placeholder = 'Search users…'; search.className = 'form-control form-control-sm mb-2';
      search.setAttribute('aria-label', 'Search users'); list.className = 'user-picker-results';
      panel.append(search, list); wrapper.append(toggle, panel); select.hidden = true;
      /** Hide the user picker and clear its expanded accessibility state. */
      const close = () => { panel.hidden = true; toggle.setAttribute('aria-expanded', 'false'); };
      /** Update the visible picker label from the selected native option. */
      const label = () => { toggle.textContent = select.selectedOptions[0]?.textContent || 'All users'; };
      /** Render user options matching the current search query. */
      function draw() {
        const needle = search.value.toLocaleLowerCase();
        list.replaceChildren(...[...select.options].filter(option => !option.value || option.textContent.toLocaleLowerCase().includes(needle)).map(option => {
          const button = document.createElement('button'); button.type = 'button';
          button.className = 'dropdown-item rounded'; button.textContent = option.textContent;
          button.classList.toggle('active', option.value === select.value);
          button.addEventListener('click', () => { select.value = option.value; label(); close(); toggle.focus(); select.dispatchEvent(new Event('change', {bubbles: true})); });
          return button;
        }));
      }
      /** Constrain the picker panel to the available viewport. */
      function constrain() {
        const value = select.value;
        select.replaceChildren(...options.filter(option => !option.value || !filter.value || JSON.parse(option.dataset.groups || '[]').includes(filter.value)).map(option => option.cloneNode(true)));
        select.value = [...select.options].some(option => option.value === value) ? value : '';
        label(); draw();
      }
      toggle.addEventListener('click', () => { panel.hidden = !panel.hidden; toggle.setAttribute('aria-expanded', String(!panel.hidden)); if (!panel.hidden) {search.value = ''; draw(); search.focus();} });
      search.addEventListener('input', draw);
      filter.addEventListener('change', constrain);
      select.addEventListener('change', label);
      wrapper.addEventListener('keydown', event => {
        if (event.key === 'Escape') {close(); toggle.focus();}
        if (event.key === 'ArrowDown' && event.target === search) {event.preventDefault(); list.querySelector('button')?.focus();}
        else if (['ArrowDown', 'ArrowUp'].includes(event.key) && event.target.parentElement === list) {
          event.preventDefault(); (event.key === 'ArrowDown' ? event.target.nextElementSibling : event.target.previousElementSibling)?.focus();
        }
        if (event.key === 'Enter' && event.target === search) {event.preventDefault(); list.querySelector('button')?.click();}
      });
      document.addEventListener('click', event => {if (!wrapper.contains(event.target)) close();});
      constrain();
    }
  }
  document.addEventListener('htmx:afterSwap', initialize); initialize();
})();
