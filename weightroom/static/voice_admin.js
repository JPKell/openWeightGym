/* Generate explicit previews without leaving the administrator's form. */
(() => {
  let previous = '';
  document.addEventListener('submit', async event => {
    const form = event.target.closest('[data-voice-preview]');
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector('button');
    const error = form.querySelector('[data-preview-error]');
    const audio = form.querySelector('[data-preview-audio]');
    button.disabled = true;
    error.textContent = 'Generating preview…';
    try {
      const response = await fetch(form.action, {method: 'POST', body: new URLSearchParams(new FormData(form))});
      if (!response.ok || !response.headers.get('content-type')?.startsWith('audio/wav')) {
        throw new Error(response.status === 200 ? 'Your session may have expired. Sign in and try again.' : await response.text());
      }
      const sound = await response.blob();
      if (previous) URL.revokeObjectURL(previous);
      previous = URL.createObjectURL(sound);
      audio.src = previous;
      audio.hidden = false;
      error.textContent = '';
      await audio.play().catch(() => {});
    } catch (problem) {
      error.textContent = problem.message || 'Preview could not be generated.';
    } finally {
      button.disabled = false;
    }
  });
})();
