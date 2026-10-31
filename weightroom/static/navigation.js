// Desktop section rail and one keyboard-accessible mobile menu with both navigation levels.
(() => {
  const narrow = matchMedia('(max-width: 767.98px)');
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('site-sidebar');
  const menuButton = document.getElementById('mobile-menu-toggle');
  const menu = document.getElementById('mobile-navigation');
  const conversations = document.querySelector('.chat-sidebar');
  function showRail(open, remember = false) {
    if (!sidebar || !toggle) return;
    open = open && !narrow.matches;
    document.documentElement.dataset.sidebar = open ? 'open' : 'closed';
    toggle.setAttribute('aria-expanded', String(open));
    toggle.setAttribute('aria-label', open ? 'Collapse section pages' : 'Show section pages');
    sidebar.hidden = !open;
    if (remember && !narrow.matches) {
      try { localStorage.setItem('weightroom-sidebar', open ? 'open' : 'closed'); } catch { /* Use this page's state. */ }
    }
  }
  function placeNavigation() {
    let saved = 'open';
    try { saved = localStorage.getItem('weightroom-sidebar') || localStorage.getItem('frontdesk-sidebar') || 'open'; } catch { /* Use the default. */ }
    showRail(!narrow.matches && saved === 'open');
    if (!narrow.matches && menu?.open) menu.close();
    if (conversations) document.querySelector(narrow.matches ? '[data-mobile-conversations]' : '[data-desktop-conversations]')?.append(conversations);
  }
  toggle?.addEventListener('click', () => showRail(toggle.getAttribute('aria-expanded') !== 'true', true));
  menuButton?.addEventListener('click', () => {
    menu.showModal();
    menuButton.setAttribute('aria-expanded', 'true');
    menu.querySelector('[data-close-navigation]').focus();
  });
  menu?.querySelector('[data-close-navigation]').addEventListener('click', () => menu.close());
  menu?.addEventListener('close', () => {
    menuButton.setAttribute('aria-expanded', 'false');
    if (narrow.matches) menuButton.focus();
  });
  narrow.addEventListener('change', placeNavigation);
  placeNavigation();
})();
