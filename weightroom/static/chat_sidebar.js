// Sidebar actions use the same owner-checked conversation handlers as the chat page.
(() => {
  let opening;
  /** Display a conversation-list error without inserting active markup. */
  const error = () => document.querySelector('[data-sidebar-error]');
  /** Submit the selected conversation's new title and update its visible row. */
  function rename(link) {
    clearTimeout(opening);
    const row = link.closest('[data-conversation]');
    row.querySelector('.chat-sidebar-row').hidden = true;
    const form = row.querySelector('[data-chat-rename]');
    form.hidden = false;
    form.elements.title.focus();
    form.elements.title.select();
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-chat-title]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || !event.detail) return;
    event.preventDefault();
    clearTimeout(opening);
    opening = setTimeout(() => location.assign(link.href), 350);
  });
  document.addEventListener('dblclick', event => {
    const link = event.target.closest('[data-chat-title]');
    if (!link) return;
    event.preventDefault();
    rename(link);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'F2' && event.target.matches('[data-chat-title]')) {
      event.preventDefault(); rename(event.target);
    }
    if (event.key === 'Escape' && event.target.closest('[data-chat-rename]')) {
      event.preventDefault(); event.stopPropagation();
      const form = event.target.form;
      form.hidden = true;
      form.closest('[data-conversation]').querySelector('.chat-sidebar-row').hidden = false;
      form.reset();
    }
  }, true);
  document.addEventListener('submit', async event => {
    const form = event.target.closest('[data-chat-delete], [data-chat-rename]');
    if (!form) return;
    event.preventDefault();
    const button = event.submitter || form.querySelector('button');
    const values = new URLSearchParams(new FormData(form));
    if (form.hasAttribute('data-chat-delete')) values.set('do', 'delete');
    error().textContent = '';
    button.disabled = true;
    try {
      const response = await fetch(form.action, {method: 'POST', body: values, headers: {accept: 'application/json'}});
      if (!response.headers.get('content-type')?.startsWith('application/json')) throw new Error('Sign in again to manage conversations.');
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || 'The conversation could not be changed.');
      const row = form.closest('[data-conversation]');
      if (form.hasAttribute('data-chat-delete')) {
        if (location.pathname === new URL(form.action).pathname) location.assign('/chat');
        else row.remove();
      } else {
        row.querySelector('[data-chat-title]').textContent = result.title;
        row.dataset.filterText = result.title;
        row.querySelector('[data-chat-delete] button').setAttribute('aria-label', 'Delete ' + result.title);
        row.querySelector('.chat-sidebar-row').hidden = false;
        form.hidden = true;
        form.elements.title.defaultValue = result.title;
        if (location.pathname === new URL(form.action).pathname) {
          document.querySelector('[data-conversation-heading]').textContent = result.title;
          document.title = result.title + ' · openWeightGym';
        }
      }
    } catch (problem) {
      error().textContent = problem.message;
    } finally {
      button.disabled = false;
    }
  });
})();
