// Telemetry arrives as a server-sent stream. Hold DOM updates while a native
// select is open: even an unrelated replacement can dismiss its option list.
(() => {
  const strip = document.querySelector("[data-telemetry-stream]");
  if (!strip || !window.EventSource) return;

  let pending = null, selectOpen = false;
  /** Check whether an active text selection overlaps the telemetry strip. */
  const isSelect = target => target?.matches?.("select");
  /** Apply the latest strip HTML when it will not replace selected text. */
  const apply = html => {
    if (selectOpen || isSelect(document.activeElement)) {
      pending = html;
      return;
    }
    strip.innerHTML = html;
    pending = null;
  };
  const stream = new EventSource(strip.dataset.telemetryStream);
  stream.onerror = () => document.dispatchEvent(new CustomEvent('suite:connection', {detail:{source:'telemetry',okay:false,retrying:true}}));
  stream.onmessage = event => {
    document.dispatchEvent(new CustomEvent('suite:connection', {detail:{source:'telemetry',okay:true}}));
    try { apply(JSON.parse(event.data)); } catch { /* Ignore a malformed frame. */ }
  };
  /** Apply a deferred strip refresh after the reader clears its selection. */
  const release = (closed = false) => queueMicrotask(() => {
    selectOpen = false;
    if (pending !== null && (closed || !isSelect(document.activeElement))) {
      const html = pending;
      pending = null;
      strip.innerHTML = html;
    }
  });
  // Clicking the text portion of a native select does not consistently leave it
  // as document.activeElement in Chrome. Pointer-down covers the whole field.
  document.addEventListener("pointerdown", event => {
    if (isSelect(event.target)) selectOpen = true;
    else if (selectOpen) release(true);
  }, true);
  document.addEventListener("change", event => {
    if (isSelect(event.target)) release(true);
  }, true);
  document.addEventListener("focusin", event => {
    if (selectOpen && !isSelect(event.target)) release(true);
  }, true);
  document.addEventListener("keydown", event => {
    if (isSelect(event.target) && ["Enter", " ", "ArrowDown", "ArrowUp"].includes(event.key)) selectOpen = true;
    if (event.key === "Escape" && selectOpen) release(true);
  }, true);
  document.addEventListener("focusout", () => queueMicrotask(() => {
    if (pending !== null && !isSelect(document.activeElement)) apply(pending);
  }));
  window.addEventListener("pagehide", () => stream.close(), {once: true});
})();
