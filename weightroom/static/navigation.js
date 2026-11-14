// Desktop section rail and one keyboard-accessible mobile menu with both navigation levels.
(() => {
  const narrow = matchMedia('(max-width: 767.98px)');
  const compactChat = matchMedia('(max-width: 1199.98px)');
  const toggle = document.getElementById('sidebar-toggle');
  const sidebar = document.getElementById('site-sidebar');
  const menuButton = document.getElementById('mobile-menu-toggle');
  const menu = document.getElementById('mobile-navigation');
  const conversations = document.querySelector('.chat-sidebar');
  const chatButton = document.querySelector('[data-open-chat-navigation]');
  const switchButton = menu?.querySelector('[data-switch-navigation]');
  const sitePanel = menu?.querySelector('#mobile-site-navigation');
  const chatPanel = menu?.querySelector('[data-mobile-conversations]');
  let chatSelected = Boolean(conversations);
  let opener;
  function showPanel(chat) {
    if (!conversations) return;
    chatSelected = chat;
    sitePanel.hidden = chat;
    chatPanel.hidden = !chat;
    menu.querySelector('#mobile-navigation-title').textContent = chat ? 'Chat options' : 'Navigation';
    switchButton.textContent = chat ? 'Main menu' : 'Chat options';
    switchButton.setAttribute('aria-label', chat ? 'Show main menu' : 'Show chat options');
  }
  function openMenu(button, chat = chatSelected) {
    opener = button;
    showPanel(chat);
    menu.showModal();
    button.setAttribute('aria-expanded', 'true');
    menu.querySelector('[data-close-navigation]').focus();
  }
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
    if (!(conversations ? compactChat.matches : narrow.matches) && menu?.open) menu.close();
    if (conversations) document.querySelector(compactChat.matches ? '[data-mobile-conversations]' : '[data-desktop-conversations]')?.append(conversations);
  }
  toggle?.addEventListener('click', () => showRail(toggle.getAttribute('aria-expanded') !== 'true', true));
  menuButton?.addEventListener('click', () => openMenu(menuButton));
  chatButton?.addEventListener('click', () => openMenu(chatButton, true));
  switchButton?.addEventListener('click', () => showPanel(!chatSelected));
  menu?.querySelector('[data-close-navigation]').addEventListener('click', () => menu.close());
  menu?.addEventListener('close', () => {
    opener?.setAttribute('aria-expanded', 'false');
    if (opener?.getClientRects().length) opener.focus();
  });
  narrow.addEventListener('change', placeNavigation);
  compactChat.addEventListener('change', placeNavigation);
  showPanel(chatSelected);
  placeNavigation();
})();
