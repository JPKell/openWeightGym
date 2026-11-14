// Heading and sidebar actions share the owner-checked conversation handlers.
(() => {
  let opening;
  const scope = element => element.closest('[data-conversation], #chat-main');
  const error = form => scope(form).querySelector('[data-chat-rename-error]') || document.querySelector('[data-sidebar-error]');
  const display = container => container.querySelector('.chat-sidebar-row, .page-header');
  /** Open either title editor and select its saved name. */
  function rename(source) {
    clearTimeout(opening);
    const container = scope(source);
    display(container).hidden = true;
    const form = container.querySelector('[data-chat-rename]');
    error(form).textContent = '';
    form.hidden = false;
    form.elements.title.focus();
    form.elements.title.select();
  }
  /** Restore the title and keyboard focus after saving or cancelling. */
  function close(form) {
    const container = scope(form);
    form.hidden = true;
    display(container).hidden = false;
    form.reset();
    error(form).textContent = '';
    container.querySelector('[data-chat-title], [data-conversation-heading]').focus({preventScroll: true});
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('[data-chat-title]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || !event.detail) return;
    event.preventDefault();
    clearTimeout(opening);
    opening = setTimeout(() => location.assign(link.href), 350);
  });
  document.addEventListener('dblclick', event => {
    const title = event.target.closest('[data-chat-title], [data-conversation-heading]');
    if (!title) return;
    event.preventDefault();
    rename(title);
  });
  document.addEventListener('keydown', event => {
    if ((event.key === 'F2' && event.target.matches('[data-chat-title], [data-conversation-heading]')) ||
        (event.key === 'Enter' && event.target.matches('[data-conversation-heading]'))) {
      event.preventDefault(); rename(event.target);
    }
    if (event.key === 'Escape' && event.target.closest('[data-chat-rename]')) {
      event.preventDefault(); event.stopPropagation();
      const form = event.target.closest('[data-chat-rename]');
      if (!form.dataset.saving) close(form);
    }
  }, true);
  document.addEventListener('submit', async event => {
    const form = event.target.closest('[data-chat-delete], [data-chat-rename]');
    if (!form) return;
    event.preventDefault();
    if (form.dataset.saving) return;
    const button = event.submitter || form.querySelector('button');
    const values = new URLSearchParams(new FormData(form));
    if (form.hasAttribute('data-chat-delete')) values.set('do', 'delete');
    error(form).textContent = '';
    form.dataset.saving = 'true';
    button.disabled = true;
    try {
      const response = await fetch(form.action, {method: 'POST', body: values, headers: {accept: 'application/json'}});
      if (!response.headers.get('content-type')?.startsWith('application/json')) throw new Error('Sign in again to manage conversations.');
      const result = await response.json();
      if (!response.ok) throw new Error(result.error?.message || 'The conversation could not be changed.');
      const row = form.closest('[data-conversation]');
      if (form.hasAttribute('data-chat-delete')) {
        if (location.pathname === new URL(form.action).pathname) location.assign(form.dataset.chatBase || '/chat');
        else row.remove();
      } else {
        const container = scope(form);
        const ref = container.dataset.conversation || container.dataset.chatRef;
        for (const row of document.querySelectorAll('[data-conversation]')) {
          if (row.dataset.conversation !== ref) continue;
          row.querySelector('[data-chat-title]').textContent = result.title;
          row.dataset.filterText = result.title;
          row.querySelector('[data-chat-delete] button').setAttribute('aria-label', 'Delete ' + result.title);
          row.querySelector('[data-chat-rename]').elements.title.defaultValue = result.title;
        }
        const main = document.querySelector('#chat-main');
        if (main?.dataset.chatRef === ref) {
          main.querySelector('[data-conversation-heading]').textContent = result.title;
          main.querySelector('[data-chat-rename]').elements.title.defaultValue = result.title;
          document.title = result.title + ' · openWeightGym';
        }
        close(form);
      }
    } catch (problem) {
      error(form).textContent = problem.message;
    } finally {
      delete form.dataset.saving;
      button.disabled = false;
    }
  });
})();
