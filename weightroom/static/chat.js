// The chat page. An answer on its way is followed while it is written. Answers and code can be
// copied. Enter sends. Everything else on the page is plain forms and works without this.

const middle = document.getElementById("chat-main");
let composerHidden = false, lastScroll = scrollY, scrollDirection = 0, scrolled = 0;
// The composer can still be hidden after a short downward scroll. Following the answer
// depends on where the reader is, not on whether the entry box has reappeared yet.
/** Check whether the chat view is close enough to follow new output. */
const atEnd = () => {
  const page = document.scrollingElement;
  return page && page.scrollHeight - page.clientHeight - page.scrollTop <= 150;
};
/** Scroll to the newest visible chat content. */
const scrollToEnd = () => { document.scrollingElement.scrollTop = document.scrollingElement.scrollHeight; };

// Native Chromium select popups behave differently over their text and arrow.
// Keep the real select for forms, but give model choices one stable in-page listbox.
let pickerNumber = 0;
/** Replace native model selects with accessible searchable controls. */
function enhanceModelPickers() {
  for (const menu of document.querySelectorAll("body > .chat-model-menu")) {
    if (!menu._picker?.isConnected) menu.remove();
  }
  for (const select of middle.querySelectorAll("select[data-chat-model-picker]:not([data-picker-ready])")) {
    select.dataset.pickerReady = "true";
    const wrapper = Object.assign(document.createElement("div"), {className: "chat-model-picker"});
    const toggle = Object.assign(document.createElement("button"), {type: "button", className: "form-select text-start chat-model-picker-toggle" + (select.classList.contains("form-select-sm") ? " form-select-sm" : "")});
    const menu = Object.assign(document.createElement("div"), {className: "frame chat-model-menu", hidden: true, role: "listbox"});
    menu.id = `chat-model-menu-${++pickerNumber}`;
    toggle.disabled = select.disabled;
    toggle.setAttribute("aria-controls", menu.id);
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-haspopup", "listbox");
    toggle.setAttribute("aria-label", select.getAttribute("aria-label") || "Model");
    /** Hide the current model picker and reset its expanded state. */
    const close = focus => {
      menu.hidden = true;
      toggle.setAttribute("aria-expanded", "false");
      if (focus) toggle.focus();
    };
    /** Position the model menu within the available viewport. */
    const place = () => {
      const field = toggle.getBoundingClientRect();
      const below = innerHeight - field.bottom - 8, above = field.top - 8;
      const room = Math.max(120, Math.min(352, below >= above ? below : above));
      const width = Math.min(field.width, innerWidth - 16);
      menu.style.left = `${Math.max(8, Math.min(field.left, innerWidth - width - 8))}px`;
      menu.style.width = `${width}px`;
      menu.style.maxHeight = `${room}px`;
      menu.style.top = below >= above ? `${field.bottom + 2}px` : `${Math.max(8, field.top - Math.min(menu.scrollHeight, room) - 2)}px`;
    };
    /** Update the model picker label and selected option. */
    const sync = () => {
      toggle.textContent = select.selectedOptions[0]?.textContent || "Choose a model";
      for (const option of menu.children) {
        const selected = option.dataset.value === select.value;
        option.classList.toggle("selected", selected);
        option.setAttribute("aria-selected", String(selected));
      }
    };
    for (const source of select.options) {
      const option = Object.assign(document.createElement("button"), {type: "button", className: "chat-model-option", role: "option", textContent: source.textContent});
      option.dataset.value = source.value;
      option.onclick = () => {
        select.value = source.value;
        select.dispatchEvent(new Event("input", {bubbles: true}));
        select.dispatchEvent(new Event("change", {bubbles: true}));
        close(true);
      };
      menu.append(option);
    }
    toggle.onclick = () => {
      const opening = menu.hidden;
      for (const other of document.querySelectorAll("body > .chat-model-menu:not([hidden])")) {
        other.hidden = true;
        other._picker?.querySelector(".chat-model-picker-toggle")?.setAttribute("aria-expanded", "false");
      }
      menu.hidden = !opening;
      toggle.setAttribute("aria-expanded", String(opening));
      if (opening) {
        place();
        const selected = menu.querySelector('[aria-selected="true"]');
        if (selected) menu.scrollTop = Math.max(0, selected.offsetTop - (menu.clientHeight - selected.offsetHeight) / 2);
      }
    };
    /** Handle model-picker navigation, selection and dismissal keys. */
    const keyboard = event => {
      if (event.key === "Escape" && !menu.hidden) { event.preventDefault(); close(true); }
      if (["ArrowDown", "ArrowUp"].includes(event.key) && event.target === toggle) {
        event.preventDefault();
        if (menu.hidden) toggle.click();
        const choices = [...menu.children], selected = choices.findIndex(one => one.getAttribute("aria-selected") === "true");
        choices[Math.max(0, Math.min(choices.length - 1, selected + (event.key === "ArrowDown" ? 1 : -1)))]?.focus();
      }
    };
    wrapper.onkeydown = keyboard;
    menu.onkeydown = keyboard;
    select.before(wrapper);
    wrapper.append(select, toggle);
    document.body.append(menu);
    wrapper._menu = menu;
    menu._picker = wrapper;
    select.hidden = true;
    select._syncPicker = sync;
    select.addEventListener("input", sync);
    sync();
  }
}
document.addEventListener("click", event => {
  for (const picker of middle?.querySelectorAll(".chat-model-picker") || []) {
    if (!picker.contains(event.target) && !picker._menu.contains(event.target)) {
      picker._menu.hidden = true;
      picker.querySelector(".chat-model-picker-toggle").setAttribute("aria-expanded", "false");
    }
  }
});

// Leave the composer's space in the document so hiding it cannot cause a scroll jump.
// Only the entry bubble hides; Stop remains available while an answer is being written.
/** Update composer visibility while preserving the current draft. */
function showComposer() {
  const composer = middle?.querySelector(".composer");
  if (!composer) return;
  const hidden = composerHidden && Boolean(composer.querySelector("textarea[data-enter]"));
  composer.classList.toggle("composer-hidden", hidden);
  composer.inert = hidden;
}

/** Check whether the page is scrolled to its very end, or does not scroll at all. */
const atBottom = () => {
  const page = document.scrollingElement;
  return page && page.scrollHeight - page.clientHeight - page.scrollTop <= 2;
};
// At the bottom of the page the composer is always shown. A page that has become shorter, as when
// Ask again's options close, may leave too little room to scroll down far enough to bring it back.
/** Show a hidden composer once the page is at its end. */
const shownAtBottom = () => {
  if (!composerHidden || !atBottom()) return false;
  composerHidden = false;
  scrolled = 0;
  showComposer();
  return true;
};

addEventListener("scroll", () => {
  const now = Math.max(0, scrollY);
  const delta = now - lastScroll;
  lastScroll = now;
  if (!delta) return;
  const direction = Math.sign(delta);
  if (direction !== scrollDirection) scrolled = 0;
  scrollDirection = direction;
  scrolled += Math.abs(delta);
  const line = parseFloat(getComputedStyle(document.body).lineHeight) || 24;
  if (scrolled >= line * 7) {
    composerHidden = direction < 0;
    scrolled = 0;
    showComposer();
  }
  shownAtBottom();
}, {passive: true});
// Content that shrinks without moving the scroll position, such as a page that no longer scrolls, sends no scroll event.
new ResizeObserver(shownAtBottom).observe(document.body);

document.addEventListener("click", event => {
  const link = event.target.closest("[data-chat-jump]");
  if (!link) return;
  const target = document.getElementById(link.hash.slice(1));
  if (!target) return;
  event.preventDefault();
  target.tabIndex = -1;
  target.focus({preventScroll: true});
  target.scrollIntoView({block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth"});
});

// Follow the answer that is on its way, if there is one. The server sends all of it so far,
// already made into HTML, each time it has grown. When it is kept, the middle of the page is
// asked for again, which brings the finished answer and the box to say more.
/** Follow streamed chat output when the reader remains at the end. */
function follow() {
  const live = document.querySelector("[data-stream]");
  if (!live) return;
  const coming = new EventSource(live.dataset.stream);
  coming.onerror = () => document.dispatchEvent(new CustomEvent('suite:connection', {detail:{source:'chat-stream',okay:false,retrying:true}}));
  coming.onmessage = step => {
    document.dispatchEvent(new CustomEvent('suite:connection', {detail:{source:'chat-stream',okay:true}}));
    const stay = atEnd();  // someone who has scrolled up to read is left where they are
    live.innerHTML = step.data;
    if (stay) scrollToEnd();
  };
  coming.addEventListener("done", async () => {
    coming.close();
    const path = location.pathname;
    const stay = atEnd();
    async function refresh() {
      if (location.pathname !== path || !middle.isConnected) return;
      try {
        const response = await fetch(path + "?part=main");
        if (response.redirected && new URL(response.url).pathname === '/sign-in') {
          location.assign(response.url);
          return;
        }
        if (!response.ok) throw new Error('Reply refresh unavailable');
        const html = await response.text();
        document.dispatchEvent(new CustomEvent('suite:connection', {detail:{source:'chat-stream',okay:true}}));
        middle.innerHTML = html;
        made();
        if (stay) scrollToEnd();
      } catch {
        document.dispatchEvent(new CustomEvent('suite:connection', {detail:{source:'chat-stream',okay:false,retrying:true}}));
        setTimeout(refresh, 1500);
      }
    }
    await refresh();
  });
}

// What is done to the middle of the page each time it is made: a Copy button on each block of
// code, the model last chosen put back, and a look for an answer on its way.
/** Apply chat enhancements after new content is inserted. */
function made() {
  showComposer();
  for (const box of middle.querySelectorAll("[data-uploads]")) prepareFiles(box);
  for (const block of middle.querySelectorAll(".answer pre")) {
    const button = Object.assign(document.createElement("button"), {type: "button", className: "btn btn-outline-secondary btn-sm copy-code", textContent: "Copy"});
    button.dataset.copy = "code";
    block.prepend(button);
  }
  if (draft) applyOptions(draft);
  else {
    try {
      const remembered = new URL(location.href).searchParams.get("model") || localStorage.getItem(modelKey());
      const choice = middle.querySelector('select[name="model"][data-chat-setting]');
      if (choice && [...choice.options].some(option => option.value === remembered)) applyOptions({model: remembered});
    } catch { /* Storage may be unavailable. */ }
  }
  syncPluginConversation();
  enhanceModelPickers();
  follow();
}

// Inline payloads follow the existing form and authenticated request path. Compute validates
// every byte again; nothing is uploaded to an outside storage service.
/** Validate selected attachments and prepare the bounded message payload. */
function prepareFiles(box) {
  const input = box.querySelector("[data-files]"), hidden = box.querySelector('[name="attachments"]');
  const list = box.querySelector("[data-file-list]"), error = box.querySelector("[data-file-error]");
  let selected;
  try { selected = JSON.parse(hidden.value); } catch { selected = []; }
  /** Render the selected attachment list and validation state. */
  const draw = () => {
    hidden.value = JSON.stringify(selected);
    box.querySelector("[data-file-count]").textContent = selected.length ? `(${selected.length} selected)` : "";
    if (selected.length) box.closest("[data-composer-options]").open = true;
    list.replaceChildren();
    selected.forEach((file, i) => {
      const row = document.createElement("div");
      row.append(document.createTextNode(file.name + " "));
      const remove = Object.assign(document.createElement("button"), {type: "button", className: "btn btn-link btn-sm", textContent: "Remove"});
      remove.onclick = () => { selected.splice(i, 1); draw(); };
      row.append(remove); list.append(row);
    });
  };
  input.disabled = false;
  const choose = box.querySelector('[data-choose-files]');
  choose.disabled = false;
  choose.onclick = () => input.click();
  input.onchange = async () => {
    error.textContent = "";
    const files = [...input.files];
    if (files.length > 4 || files.some(f => f.size > 5 * 1024 * 1024) || files.reduce((n, f) => n + f.size, 0) > 10 * 1024 * 1024) {
      error.textContent = "Select up to four files, at most 5 MiB each and 10 MiB total."; input.value = ""; return;
    }
    box.dataset.reading = "yes";
    try {
      selected = await Promise.all(files.map(file => new Promise((resolve, reject) => {
        const reader = new FileReader(); reader.onerror = reject;
        reader.onload = () => resolve({name: file.name, data: reader.result.split(",")[1]});
        reader.readAsDataURL(file);
      })));
      draw();
    } catch { error.textContent = "Could not read these files. Select them again."; }
    finally { delete box.dataset.reading; input.value = ""; }
  };
  box.closest("form").addEventListener("submit", event => {
    if (box.dataset.reading) { event.preventDefault(); error.textContent = "Wait for the files to finish loading."; }
  });
  draw();
}

// Copying. A page that is not served over HTTPS is given no clipboard to write to, so there
// the older way is used: put the text in a box, select it, and ask the browser to copy.
/** Copy the requested chat text and report the clipboard outcome. */
function copy(text, button) {
  /** Display a plain-text status message beside the active chat control. */
  const say = () => { const was = button.textContent; button.textContent = "Copied"; setTimeout(() => button.textContent = was, 1200); };
  if (navigator.clipboard) return navigator.clipboard.writeText(text).then(say);
  const box = Object.assign(document.createElement("textarea"), {value: text});
  document.body.append(box);
  box.select();
  document.execCommand("copy");
  box.remove();
  say();
}

document.addEventListener("click", event => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  if (button.dataset.copy === "code") copy(button.parentElement.querySelector("code")?.innerText ?? "", button);
  else copy(button.closest(".turn").querySelector(".answer").innerText, button);
});

// The two Options panels edit one draft. Prompt settings save through the existing
// conversation API; only the model selector is remembered in browser storage.
let draft = null, saving = Promise.resolve(), saveTimer, revision = 0, lastSaved = '';
/** Build the browser preference key for the selected model. */
function modelKey() {
  return 'weightroom-chat-model:' + document.documentElement.dataset.themeUser + ':' + (middle?.dataset.chatRef || 'new');
}
/** Read saved per-model chat preferences with safe defaults. */
function readOptions() {
  const values = {};
  for (const field of middle.querySelectorAll('[data-chat-setting]:not(:disabled)')) values[field.name] = field.type === 'checkbox' ? field.checked : field.value;
  return values;
}
/** Apply the selected model's saved prompt and sampling preferences. */
function applyOptions(values) {
  for (const field of middle.querySelectorAll('[data-chat-setting]')) {
    if (!(field.name in values)) continue;
    if (field.type === 'checkbox') field.checked = values[field.name];
    else field.value = values[field.name];
    field._syncPicker?.();
  }
}
/** Show agent-session controls only for the selected plugin model. */
function syncPluginConversation() {
  for (const label of middle?.querySelectorAll('[data-plugin-conversation]') || []) {
    const model = label.closest('form').querySelector('select[name="model"]');
    const plugin = model ? model.selectedOptions[0]?.dataset.pluginModel === 'true' : label.dataset.pluginActive === 'true';
    label.classList.toggle('d-none', !plugin);
    label.classList.toggle('d-block', plugin);
    label.hidden = !plugin;
    label.querySelector('[name="session_mode"]').disabled = !plugin;
  }
}
/** Update the visible prompt-options status message. */
function optionsStatus(message, failed = false) {
  for (const label of middle.querySelectorAll('[data-options-status]')) {
    label.textContent = message;
    label.classList.toggle('text-danger', failed);
  }
}
/** Persist the current model's chat preferences in browser storage. */
function saveOptions() {
  clearTimeout(saveTimer);
  if (!middle?.dataset.chatRef || !draft) return saving;
  const version = revision, values = {...draft};
  saving = saving.catch(() => {}).then(async () => {
    if (version !== revision) return;
    const body = new URLSearchParams({do: 'options', tuning: '1'});
    for (const name of ['system', 'temperature', 'top_k', 'top_p', 'min_p', 'session_mode']) {
      if (name in values) body.set(name, values[name]);
    }
    if (values.thinking) body.set('thinking', 'on');
    if (body.toString() === lastSaved) return;
    optionsStatus('Saving…');
    const response = await fetch('/chat/' + encodeURIComponent(middle.dataset.chatRef), {method: 'POST', body, keepalive: true});
    if (!response.headers.get('content-type')?.startsWith('application/json')) throw new Error('Sign in again to save options.');
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || 'Could not save options.');
    lastSaved = body.toString();
    if (version === revision) optionsStatus('Saved.');
  }).catch(problem => optionsStatus(problem.message, true));
  return saving;
}
document.addEventListener('input', event => {
  const field = event.target.closest('[data-chat-setting]');
  if (!field || !middle?.contains(field)) return;
  applyOptions({[field.name]: field.type === 'checkbox' ? field.checked : field.value});
  draft = readOptions();
  revision += 1;
  if (field.name === 'model') {
    try { localStorage.setItem(modelKey(), field.value); } catch { /* The form still works. */ }
    syncPluginConversation();
  }
  clearTimeout(saveTimer);
  saveTimer = setTimeout(saveOptions, 400);
});
document.addEventListener('focusout', event => {
  if (event.target.matches('[data-chat-setting]')) saveOptions();
});
document.addEventListener('submit', async event => {
  const form = event.target.closest('form[data-chat-form]');
  if (!form || event.defaultPrevented || form.dataset.optionsReady) return;
  if (!middle?.dataset.chatRef || !draft) return;
  event.preventDefault();
  const button = event.submitter;
  await saveOptions();
  // A cached save can finish inside the original submit event's microtask checkpoint.
  // Let that event finish before asking the browser to submit the form again.
  await new Promise(resolve => setTimeout(resolve, 0));
  form.dataset.optionsReady = 'yes';
  form.requestSubmit(button);
  delete form.dataset.optionsReady;
});

// Enter sends and Shift with Enter makes a new line, where there is a keyboard. On a phone Enter makes a new line.
document.addEventListener("keydown", event => {
  if (event.key !== "Enter" || event.shiftKey || !event.target.matches?.("textarea[data-enter]") || matchMedia("(pointer: coarse)").matches) return;
  event.preventDefault();
  event.target.form.requestSubmit(event.target.form.querySelector('button[type="submit"], button:not([type])'));
});

if (middle) {
  made();
  if (document.getElementById("messages")) scrollToEnd();  // a conversation opens at its latest words
}
