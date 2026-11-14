/* Browser-local review fixtures. No network, service actions or persistent mutations. */
(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const $$ = selector => [...document.querySelectorAll(selector)];
  const seed = JSON.parse($('#alert-data').textContent);
  const clone = id => $(id).content.firstElementChild.cloneNode(true);
  const state = {episodes: [], sources: [], layout: 'desk', selected: null, page: 1};
  const pageSize = 6;
  const fullTime = new Intl.DateTimeFormat(undefined, {dateStyle: 'medium', timeStyle: 'short'});
  const shortTime = new Intl.DateTimeFormat(undefined, {hour: '2-digit', minute: '2-digit'});
  const time = (value, short = false) => value ? (short ? shortTime : fullTime).format(new Date(value * 1000)) : '—';
  const open = row => row.resolved_at === null;
  const descriptions = {
    desk: 'A focused queue beside the evidence. Recommended for daily operations.',
    board: 'Group episodes by attention and certainty. Keep unknown recovery visible.',
    ledger: 'A compact, searchable record for comparing open and closed episodes.'
  };
  function setText(root, selector, value) { root.querySelector(selector).textContent = value; }
  function say(message) { $('#feedback').textContent = message; }
  function filtered() {
    const search = $('#search').value.trim().toLowerCase(), scope = $('#scope').value, source = $('#source').value;
    return state.episodes.filter(row => {
      const match = scope === 'all' || (scope === 'open' && open(row)) ||
        (scope === 'history' && !open(row)) || (scope === 'unseen' && open(row) && !row.acknowledged_at) ||
        (scope === 'review' && open(row) && row.manual) || (scope === 'unknown' && open(row) && row.state === 'unknown');
      return match && (source === 'all' || source === row.source) &&
        [row.title, row.source, row.explanation, row.recovery, row.acknowledged_by || ''].join(' ').toLowerCase().includes(search);
    }).sort((a, b) => (b.resolved_at || b.first_at) - (a.resolved_at || a.first_at) || a.id.localeCompare(b.id));
  }
  function badge(root, row) {
    const element = root.querySelector('[data-state]');
    const label = !open(row) ? row.state : row.manual ? 'Review required' : row.state === 'unknown' ? 'Recovery unknown' : 'Active';
    element.textContent = label;
    element.className = 'badge ' + (!open(row) ? 'bg-secondary-lt' : row.state === 'unknown' || row.manual ? 'bg-warning-lt' : 'bg-danger-lt');
  }
  function attention(row) { return row.acknowledged_at ? `Seen by ${row.acknowledged_by}` : open(row) ? 'Unacknowledged' : 'Not acknowledged'; }
  function summary(root, row) {
    setText(root, '[data-title]', row.title);
    setText(root, '[data-source]', row.source);
    setText(root, '[data-attention]', attention(row));
    badge(root, row);
    const button = root.matches('[data-open]') ? root : root.querySelector('[data-open]');
    button.dataset.open = row.id;
    button.setAttribute('aria-pressed', String(state.selected === row.id));
    if (root.tagName === 'TR') {
      root.dataset.selected = String(state.selected === row.id);
      setText(root, '[data-first-at]', time(row.first_at));
      setText(root, '[data-resolved-at]', time(row.resolved_at));
    } else {
      root.dataset.attentionState = row.acknowledged_at ? 'seen' : 'unseen';
      setText(root, '[data-explanation]', row.explanation);
      setText(root, '[data-last-at]', time(row.resolved_at || row.last_at, true));
    }
    return root;
  }
  function renderSources() {
    $('#source-list').replaceChildren(...state.sources.map(source => {
      const element = clone('#alert-source-template');
      element.dataset.available = String(source.available);
      element.querySelector('[data-source-status]').className = source.available ? 'small text-secondary' : 'small text-body fw-semibold';
      setText(element, '[data-source-name]', source.source);
      setText(element, '[data-source-status]', (source.available ? 'Available' : 'Unavailable; recovery unknown') + ' · ' + time(source.checked_at, true));
      return element;
    }));
    const available = state.sources.filter(row => row.available).length;
    $('#source-count').textContent = state.sources.length ? `${available} / ${state.sources.length} available` : 'No readings yet';
    $('#source-list').hidden = !state.sources.length;
  }
  function renderDetails() {
    const row = state.episodes.find(row => row.id === state.selected);
    $('#detail-extra').hidden = !row;
    if (!row) { $('#detail-content').replaceChildren(clone('#alert-detail-empty-template')); return; }
    const element = clone('#alert-episode-template');
    for (const key of ['title', 'explanation', 'source', 'recovery']) setText(element, `[data-${key}]`, row[key]);
    for (const key of ['first', 'last', 'checked', 'resolved', 'acknowledged']) setText(element, `[data-${key}-at]`, time(row[key + '_at']));
    badge(element, row);
    setText(element, '[data-acknowledged-by]', row.acknowledged_by || '');
    element.querySelector('[data-acknowledgement]').hidden = !row.acknowledged_at;
    element.querySelector('[data-unknown]').hidden = row.state !== 'unknown';
    element.querySelector('[data-closure]').hidden = open(row);
    element.querySelector('[data-demo-actions]').hidden = !open(row);
    element.querySelector('[value="acknowledge"]').hidden = !!row.acknowledged_at;
    element.querySelector('[value="review"]').hidden = !row.manual;
    $('#detail-content').replaceChildren(element);
    $('#detail-rule').textContent = row.manual ? 'Memory-kill incidents close only after explicit review. Review records investigation; it does not prove memory recovery.' : row.state === 'unknown' ? 'Missing or stale evidence cannot close this episode. Wait for a fresh reading that establishes recovery.' : 'Acknowledgement is recorded for this episode. Recovery is established separately by source evidence.';
    const events = [['First observed', row.first_at], ['Last observed', row.last_at]];
    if (row.acknowledged_at) events.push([`Acknowledged by ${row.acknowledged_by}`, row.acknowledged_at]);
    if (row.resolved_at) events.push([row.state === 'reviewed' ? 'Incident reviewed' : 'Recovery confirmed', row.resolved_at]);
    events.push([row.state === 'unknown' ? 'Checked · condition unavailable' : 'Source checked', row.checked_at]);
    $('#timeline').replaceChildren(...events.sort((a, b) => a[1] - b[1]).map(([title, at]) => {
      const entry = clone('#alert-timeline-template');
      setText(entry, '[data-event-title]', title);
      setText(entry, '[data-event-time]', time(at));
      entry.querySelector('time').dateTime = new Date(at * 1000).toISOString();
      return entry;
    }));
  }
  function render() {
    const opened = state.episodes.filter(open);
    $('#open-count').textContent = opened.length;
    $('#unseen-count').textContent = opened.filter(row => !row.acknowledged_at).length;
    $('#review-count').textContent = opened.filter(row => row.manual).length;
    $('#unknown-count').textContent = opened.filter(row => row.state === 'unknown').length;
    const rows = filtered(), pages = Math.max(1, Math.ceil(rows.length / pageSize));
    state.page = Math.min(state.page, pages);
    if (!rows.some(row => row.id === state.selected)) state.selected = rows[0]?.id || null;
    const visible = rows.slice((state.page - 1) * pageSize, state.page * pageSize);
    $('#result-count').textContent = `${rows.length} matching episode${rows.length === 1 ? '' : 's'} · times in ${Intl.DateTimeFormat().resolvedOptions().timeZone}`;
    $('#page-summary').textContent = rows.length ? `${(state.page - 1) * pageSize + 1}–${Math.min(state.page * pageSize, rows.length)} of ${rows.length}` : '0 episodes';
    $('#previous').disabled = state.page === 1;
    $('#next').disabled = state.page === pages;
    $('#episode-list').hidden = state.layout !== 'desk' || !rows.length;
    $('#episode-board').hidden = state.layout !== 'board' || !rows.length;
    $('#episode-ledger').hidden = state.layout !== 'ledger' || !rows.length;
    $('#inventory-label').textContent = state.layout === 'board' ? 'Grouped on this page' : 'Newest first';
    $('#episode-list').replaceChildren(...visible.map(row => summary(clone('#alert-choice-template'), row)));
    $('#ledger-rows').replaceChildren(...visible.map(row => summary(clone('#alert-ledger-template'), row)));
    const groups = $('#scope').value === 'history' ? [['recovered', 'Recovered'], ['reviewed', 'Reviewed'], ['other', 'Other closed']] :
      [['attention', 'Needs attention'], ['seen', $('#scope').value === 'all' ? 'Seen / closed' : 'Acknowledged'], ['unknown', 'Recovery unknown']];
    const groupFor = row => !open(row) ? ($('#scope').value === 'history' ? row.state : 'seen') : row.state === 'unknown' ? 'unknown' : row.acknowledged_at && !row.manual ? 'seen' : 'attention';
    $('#episode-board').replaceChildren(...groups.map(([key, title]) => {
      const lane = clone('#alert-lane-template'), matches = visible.filter(row => groupFor(row) === key);
      lane.dataset.lane = key;
      setText(lane, '[data-lane-title]', title);
      setText(lane, '[data-lane-count]', matches.length);
      lane.querySelector('[data-lane-items]').replaceChildren(...matches.map(row => summary(clone('#alert-choice-template'), row)));
      lane.querySelector('[data-lane-empty]').hidden = !!matches.length;
      return lane;
    }));
    $('#empty-slot').replaceChildren();
    if (!rows.length) {
      const empty = clone('#alert-empty-template');
      const message = !state.episodes.length ? 'There are no recorded episodes yet. Source availability will appear after collection.' :
        !opened.length && $('#scope').value === 'open' ? 'No open alerts. Check source availability before assuming everything is healthy; closed episodes are retained in History.' : 'Try a different source, search or episode scope.';
      setText(empty, '[data-empty-message]', message);
      $('#empty-slot').append(empty);
    }
    renderDetails();
    renderSources();
  }
  function clear() { $('#search').value = ''; $('#source').value = 'all'; $('#scope').value = 'open'; state.page = 1; render(); }
  function scenario() {
    $('#snapshot-time').textContent = time(seed.now) + ' · ' + Intl.DateTimeFormat().resolvedOptions().timeZone;
    state.episodes = structuredClone(seed.episodes);
    state.sources = structuredClone(seed.sources);
    const value = $('#scenario').value;
    if (value === 'healthy') { state.episodes = state.episodes.filter(row => !open(row)); state.sources.forEach(row => row.available = true); }
    if (value === 'unavailable') {
      state.sources.forEach(row => row.available = false);
      state.episodes.filter(row => open(row) && !row.manual).forEach(row => row.state = 'unknown');
    }
    if (value === 'empty') { state.episodes = []; state.sources = []; }
    $('#briefing-title').textContent = {normal: 'A clear view of what needs you.', healthy: 'No open episodes. Keep the evidence in view.', unavailable: 'Readings unavailable. Recovery remains unknown.', empty: 'Ready for the first observation.'}[value];
    state.selected = null;
    clear();
    say('Sample data · actions affect this tab only. Reload or Reset demo restores the fixtures.');
  }
  document.addEventListener('click', event => {
    const choice = event.target.closest('[data-open]');
    if (choice) {
      state.selected = choice.dataset.open; render();
      if (matchMedia('(max-width: 850px)').matches) { $('#inspector').scrollIntoView({behavior: 'instant', block: 'start'}); $('#inspector').setAttribute('tabindex', '-1'); $('#inspector').focus({preventScroll: true}); }
    }
    if (event.target.closest('[data-clear]')) clear();
    const design = event.target.closest('[data-design]');
    if (design) {
      state.layout = design.dataset.design;
      $('#workspace').dataset.layout = state.layout;
      $$('[data-design]').forEach(button => { const active = button === design; button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active)); });
      $('#design-description').textContent = descriptions[state.layout];
      render();
    }
  });
  document.addEventListener('submit', event => {
    if (!event.target.matches('[data-demo-actions]')) return;
    event.preventDefault();
    const row = state.episodes.find(row => row.id === state.selected);
    if (!row || !open(row)) return;
    if (event.submitter?.value === 'acknowledge') {
      row.acknowledged_at = seed.now; row.acknowledged_by = 'jordan';
      say('Sample acknowledgement recorded by jordan. The condition remains open.');
    } else if (event.submitter?.value === 'review' && row.manual) {
      row.state = 'reviewed'; row.resolved_at = seed.now;
      row.recovery = 'Reviewed by jordan; this records review, not proof of memory recovery.';
      say('Sample incident marked reviewed. It is retained in Closed history; this does not prove memory recovery.');
    }
    render();
  });
  for (const id of ['search', 'scope', 'source']) $(('#' + id)).addEventListener(id === 'search' ? 'input' : 'change', () => { state.page = 1; render(); });
  $('#previous').addEventListener('click', () => { state.page--; state.selected = null; render(); });
  $('#next').addEventListener('click', () => { state.page++; state.selected = filtered()[(state.page - 1) * pageSize]?.id; render(); });
  $('#scenario').addEventListener('change', scenario);
  $('#reset').addEventListener('click', () => { $('#scenario').value = 'normal'; scenario(); });
  $('#refresh').addEventListener('click', () => { render(); say('Sample snapshot refreshed. No live services were contacted; local acknowledgements are retained.'); });
  $('#export').addEventListener('click', () => {
    const payload = {sample: true, snapshot_at: new Date(seed.now * 1000).toISOString(), scope: $('#scope').value,
      source: $('#source').value, search: $('#search').value, episodes: filtered(), sources: state.sources};
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'}));
    const link = document.createElement('a'); link.href = url; link.download = 'alerts-sample.json'; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    say(`Exported ${payload.episodes.length} matching sample episodes and source availability.`);
  });
  const themeLabel = () => { $('#theme').textContent = document.documentElement.dataset.bsTheme === 'dark' ? 'Light mode' : 'Dark mode'; };
  $('#theme').addEventListener('click', () => {
    const control = $('[data-theme-form] [name="mode"]');
    control.value = document.documentElement.dataset.bsTheme === 'dark' ? 'light' : 'dark';
    control.dispatchEvent(new Event('change', {bubbles: true})); themeLabel();
  });
  document.addEventListener('weightroom:theme', themeLabel);
  themeLabel(); scenario();
})();
