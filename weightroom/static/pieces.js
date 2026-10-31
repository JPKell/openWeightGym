// For a form or a link that changes one piece of the page: it carries hx-select, the piece to take out of what comes back.
// See `swaps` in pages/parts.html. A piece that only asks for itself again every few seconds has no hx-select, and is left alone.

// Every result of a button action uses the same temporary notice under the telemetry bar.
(() => {
  /** Initialize polling and selection protection for newly inserted fragments. */
  const arm = root => {
    if (!root) return;
    const notices = root.matches?.(".said-box") ? [root] : [...root.querySelectorAll?.(".said-box") ?? []];
    notices.forEach(notice => {
      if (notice.dataset.dismissArmed) return;
      notice.dataset.dismissArmed = "true";
      setTimeout(() => {
        notice.classList.add("said-box-leaving");
        setTimeout(() => notice.remove(), 250);
      }, 7000);
    });
  };
  arm(document);
  document.addEventListener("htmx:afterSwap", event => arm(event.detail?.target));
  document.addEventListener("htmx:oobAfterSwap", event => arm(event.detail?.target));
})();

// Polling must not replace selected text. Check before the request and again before the
// swap, since a selection may start while the response is in flight. Companion fragments
// share the same refresh request, including polls whose entire response is out of band.
(() => {
  /** Check whether a live text selection overlaps the fragment being refreshed. */
  const selected = target => {
    const selection = window.getSelection();
    if (!target || !selection?.rangeCount || selection.isCollapsed) return false;
    for (let index = 0; index < selection.rangeCount; index++) {
      if (selection.getRangeAt(index).intersectsNode(target)) return true;
    }
    return false;
  };
  /** Delay a polling replacement while the reader is selecting its current text. */
  const defer = detail => {
    const request = detail.requestConfig, source = request?.elt;
    if (request?.verb !== 'get' || !source?.getAttribute ||
        !/(?:^|,)\s*every\s/.test(source.getAttribute('hx-trigger') || '') ||
        request.triggeringEvent?.type) return false; // explicit clicks/submissions still apply
    const companions = source.getAttribute('data-refresh-companions');
    if (selected(detail.target) || (companions &&
      [...document.querySelectorAll(companions)].some(selected))) return true;
    // Also inspect the response: a new out-of-band target may not have been declared
    // on the poller, or the selection may have started after the request was sent.
    const selection = window.getSelection();
    if (!selection?.rangeCount || selection.isCollapsed || !detail.xhr?.responseText) return false;
    const response = new DOMParser().parseFromString(detail.xhr.responseText, 'text/html');
    return [...response.querySelectorAll('[hx-swap-oob], [data-hx-swap-oob]')].some(fragment => {
      const swap = fragment.getAttribute('hx-swap-oob') ?? fragment.getAttribute('data-hx-swap-oob');
      if (swap === 'false') return false;
      const colon = swap.indexOf(':');
      const targets = colon >= 0 ? document.querySelectorAll(swap.slice(colon + 1)) :
        [document.getElementById(fragment.id)];
      return [...targets].some(selected);
    });
  };
  document.addEventListener('htmx:beforeRequest', event => {
    if (defer(event.detail)) event.preventDefault();
  });
  document.addEventListener('htmx:beforeSwap', event => {
    if (defer(event.detail)) event.detail.shouldSwap = false;
  });
})();

// What came back is used if it is this page and has the piece, a refusal with its reason included. If it is another page,
// such as the sign-in page for someone signed out, the browser goes there the usual way.
document.addEventListener("htmx:beforeSwap", event => {
  const told = event.detail, wanted = told.requestConfig.elt.closest("[hx-select]")?.getAttribute("hx-select");
  if (!wanted) return;
  const came = new URL(told.xhr.responseURL).pathname, asked = new URL(told.requestConfig.path, location.href).pathname;
  const has = new DOMParser().parseFromString(told.xhr.responseText, "text/html").querySelector(wanted);
  told.shouldSwap = Boolean(has) && (came === location.pathname || came === asked);
  told.isError = false;
  if (!told.shouldSwap) location.assign(told.xhr.responseURL);
});

// Preferences belong to this signed-in account in this browser, including closed cards.
(() => {
  const user = document.documentElement.dataset.themeUser || "anonymous";
  const storageKey = `weightroom-cards-v1:${encodeURIComponent(user)}`;
  const legacyKey = `frontdesk-cards-v1:${encodeURIComponent(user)}`;
  let kept = {};
  try { kept = JSON.parse(localStorage.getItem(storageKey) ?? localStorage.getItem(legacyKey) ?? "{}"); } catch (_) {}
  if (!kept || typeof kept !== "object" || Array.isArray(kept)) kept = {};
  /** Build the scoped browser-storage key for a collapsible section. */
  const key = (detail, scoped = true) => {
    if (detail.dataset.cardKey) return detail.dataset.cardKey;
    const heading = detail.querySelector(":scope > summary");
    const parent = detail.parentElement.closest("details[id]")?.id || "";
    const scope = (scoped && detail.closest("[data-card-scope]")?.dataset.cardScope) || location.pathname;
    return `${scope}:${parent}:${detail.id || heading?.textContent.trim()}`;
  };
  /** Restrict remembered expansion state to supported main-page sections. */
  const eligible = detail => detail.matches("main details") && !detail.matches("[data-add-model-card]") && !detail.matches(".user-menu");
  /** Persist bounded section expansion choices when browser storage is available. */
  const save = () => { try { localStorage.setItem(storageKey, JSON.stringify(kept)); } catch (_) {} };
  /** Restore remembered section expansion state after fragment updates. */
  const restore = () => {
    let migrated = false;
    document.querySelectorAll("main details").forEach(detail => {
      if (!eligible(detail)) return;
      const shared = key(detail), previous = key(detail, false);
      // Seed a new shared scope from this page's old preference once. Shared choices
      // then take precedence over conflicting preferences on other model pages.
      if (typeof kept[shared] !== "boolean" && typeof kept[previous] === "boolean") {
        kept[shared] = kept[previous];
        migrated = true;
      }
      // Setting an already-correct `open` property can dismiss an unrelated native
      // select popup. Polling swaps must leave unchanged panels completely untouched.
      if (typeof kept[shared] === "boolean" && detail.open !== kept[shared]) detail.open = kept[shared];
    });
    if (migrated) save();
  };
  document.addEventListener("toggle", event => {
    if (!eligible(event.target)) return;
    kept[key(event.target)] = event.target.open;
    save();
  }, true);
  /** Record currently expanded sections before page content changes. */
  const capture = () => {
    document.querySelectorAll("main details").forEach(detail => {
      if (eligible(detail)) kept[key(detail)] = detail.open;
    });
    save();
  };
  document.addEventListener("htmx:beforeSwap", capture);
  window.addEventListener("pagehide", capture);
  window.addEventListener("beforeunload", capture);
  for (const event of ["htmx:afterSwap", "htmx:oobAfterSwap"]) document.addEventListener(event, restore);
  /** Reveal the target section when navigating to a page anchor. */
  function anchor() {
    let id;
    try { id = decodeURIComponent(location.hash.slice(1)); } catch (_) { return; }
    const target = document.getElementById(id);
    if (!target) return;
    for (let node = target; node; node = node.parentElement) if (node.tagName === "DETAILS") node.open = true;
    requestAnimationFrame(() => target.scrollIntoView({ block: "start" }));
  }
  window.addEventListener("hashchange", anchor);
  document.addEventListener("click", event => {
    const link = event.target.closest('a[href^="#"]');
    if (link && link.hash === location.hash) anchor();
  });
  restore();
  if (location.hash) anchor();
})();
