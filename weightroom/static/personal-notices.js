/* In-app delivery remains private and does not ask inference nodes to wake. */
(() => {
  'use strict';
  const badge = document.querySelector('[data-notice-count]');
  if (!badge) return;
  let pending = false;
  async function refresh() {
    if (document.hidden || pending) return;
    pending = true;
    try {
      const response = await fetch('/account/notices', {cache:'no-store'});
      if (response.redirected || !response.ok) throw new Error('unavailable');
      const data = await response.json();
      badge.hidden = !data.unread; badge.textContent = data.unread > 49 ? '50+' : String(data.unread);
      badge.closest('a').setAttribute('aria-label', data.unread ? `Notices: ${data.unread} unread` : 'Notices');
      badge.closest('a').removeAttribute('title');
    } catch {
      badge.hidden = true;
      badge.closest('a').title = 'Notices are temporarily unavailable. Open to retry.';
    } finally { pending = false; }
  }
  refresh(); setInterval(refresh, 60000);
  document.addEventListener('visibilitychange', refresh);
})();
