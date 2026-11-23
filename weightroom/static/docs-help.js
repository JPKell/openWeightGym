/* Tab-local questions and progressive enhancement of the native HTMX Ask form. */
(() => {
  'use strict';
  /** Bind a private tab conversation once after navigation or a fragment replacement. */
  function init() {
    const root = document.getElementById('docs-help-workspace');
    const form = root?.querySelector('#docs-ask-form');
    if (!form || form.dataset.initialized) return;
    form.dataset.initialized = 'true';
    const question = form.elements.question, historyField = form.elements.history;
    const list = root.querySelector('#docs-help-history'), progress = root.querySelector('#docs-ask-progress');
    const key = 'owg.docs.ask.' + root.dataset.owner;
    let rows = [], draft = '', controller, generation = 0;
    /** Bound browser state and accept text messages only, just as the server does. */
    function bounded(value) {
      if (!Array.isArray(value)) return [];
      const valid = value.filter(row => row && ['user', 'assistant'].includes(row.role) && typeof row.content === 'string')
        .slice(-12).map(row => ({role: row.role, content: row.content.slice(0, 6000)}));
      while (valid.reduce((sum, row) => sum + row.content.length, 0) > 16000) valid.shift();
      return valid;
    }
    /** Save only this tab's draft and text history; storage denial leaves the native form usable. */
    function save() {
      historyField.value = JSON.stringify(rows);
      try { sessionStorage.setItem(key, JSON.stringify({rows, draft: question.value.slice(0, 2000)})); } catch (_) { /* Private modes may disable storage. */ }
    }
    /** Render restored history using text nodes and local citation anchors, never model HTML. */
    function history(showAll = false) {
      list.replaceChildren();
      for (const row of (showAll ? rows : rows.slice(0, -2))) {
        const paragraph = document.createElement('p'); paragraph.className = 'text-break';
        const label = document.createElement('strong'); label.textContent = (row.role === 'user' ? 'You' : 'Documentation') + ': ';
        paragraph.append(label);
        let start = 0;
        // A Markdown link, or a bare bracketed docs path as some models write it (QA-083).
        for (const match of row.content.matchAll(/\[([^\]]+)\]\((\/docs\/[a-zA-Z0-9_./?=%:#-]+)\)|\[(\/docs\/[a-zA-Z0-9_./?=%:#-]+)\](?!\()|(?<=\[\^[^\]]+\]: ?)(\/docs\/[a-zA-Z0-9_./?=%:#-]+)/g)) {
          paragraph.append(document.createTextNode(row.content.slice(start, match.index)));
          const anchor = document.createElement('a'); anchor.textContent = match[1] || match[4] || 'Source'; anchor.href = match[2] || match[3] || match[4]; paragraph.append(anchor);
          start = match.index + match[0].length;
        }
        paragraph.append(document.createTextNode(row.content.slice(start))); list.append(paragraph);
      }
    }
    try { const saved = JSON.parse(sessionStorage.getItem(key) || '{}'); rows = bounded(saved.rows); draft = typeof saved.draft === 'string' ? saved.draft.slice(0, 2000) : ''; } catch (_) { /* Start an empty tab. */ }
    const answer = root.querySelector('#docs-answer');
    const hasAnswer = !!answer?.dataset.helpText;
    if (hasAnswer) { rows = bounded(JSON.parse(answer.dataset.helpHistory || '[]')); question.value = ''; }
    else if (!question.value) question.value = draft;
    history(!hasAnswer); save();
    question.addEventListener('input', save);
    /** Replace only a server-rendered shared fragment and update this tab after a complete answer. */
    function replace(html) {
      const parsed = new DOMParser().parseFromString(html, 'text/html').querySelector('#docs-answer');
      if (!parsed) throw new Error('Help is unavailable. Use Search or retry.');
      const target = root.querySelector('#docs-answer');
      if (window.htmx?.swap) window.htmx.swap(target, parsed.outerHTML, {swapStyle: 'outerHTML'});
      else target.replaceWith(parsed);
      if (parsed.dataset.helpError !== 'true' && parsed.dataset.helpText) {
        rows = bounded(JSON.parse(parsed.dataset.helpHistory || '[]')); question.value = ''; history(); save();
      }
    }
    /** Consume the server's progress events and final safe HTML fragment over the ordinary form. */
    async function submit(event) {
      if (event.target !== form) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (!form.reportValidity()) return;
      controller?.abort(); controller = new AbortController(); const turn = ++generation;
      save(); progress.textContent = 'Searching documentation…'; form.setAttribute('aria-busy', 'true');
      const button = form.querySelector('button[type=submit]'); button.disabled = true;
      try {
        const response = await fetch(form.action, {method: 'POST', body: new URLSearchParams(new FormData(form)), credentials: 'same-origin',
          headers: {'HX-Request': 'true', 'Accept': 'text/event-stream'}, signal: controller.signal});
        if (turn !== generation || !root.isConnected) return;
        if (!(response.headers.get('content-type') || '').includes('text/event-stream')) { progress.textContent = ''; replace(await response.text()); return; }
        const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '', complete = false;
        while (true) {
          const {value, done} = await reader.read(); if (done) break;
          if (turn !== generation || !root.isConnected) return;
          buffer += decoder.decode(value, {stream: true}).replace(/\r/g, '');
          if (buffer.length > 2 ** 21) throw new Error('Help returned an oversized event. Use Search.');
          while (buffer.includes('\n\n')) {
            const boundary = buffer.indexOf('\n\n'), frame = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 2);
            const data = frame.split('\n').filter(line => line.startsWith('data: ')).map(line => line.slice(6)).join('\n');
            if (!data) continue;
            const item = JSON.parse(data);
            if (frame.startsWith('event: progress')) progress.textContent = item.text;
            else if (frame.startsWith('event: fragment')) { replace(item.html); complete = true; }
          }
        }
        if (!complete) throw new Error('The answer stream was interrupted. Use Search or retry.');
        progress.textContent = '';
      } catch (error) { if (error.name !== 'AbortError' && turn === generation) progress.textContent = error.message || 'Help is unavailable. Use Search.'; }
      finally { if (turn === generation) { form.removeAttribute('aria-busy'); button.disabled = false; } }
    }
    root.addEventListener('submit', submit, true);
    // The form remains a normal HTMX POST if this streaming enhancement is unavailable.
    form.addEventListener('htmx:beforeRequest', event => { if (event.detail.elt === form) event.preventDefault(); });
    root.querySelector('[data-docs-new]').addEventListener('click', event => {
      event.preventDefault();
      controller?.abort(); generation++; rows = []; question.value = ''; historyField.value = '[]'; list.replaceChildren();
      root.querySelector('#docs-answer').replaceChildren(); progress.textContent = ''; form.removeAttribute('aria-busy');
      form.querySelector('button[type=submit]').disabled = false;
      try { sessionStorage.removeItem(key); } catch (_) { /* No tab storage to clear. */ }
      question.focus();
    });
    addEventListener('pagehide', () => controller?.abort(), {once: true});
  }
  document.addEventListener('DOMContentLoaded', init); document.addEventListener('htmx:load', init); init();
})();
