/* Image generation → Compare: the images of one run (a parameter sweep, or several images of one prompt) in the shared
   comparison grid (comparison-grid.js), filtered by the settings the run varied. Runs come from the studio's job
   list (images.js announces it as weightroom:image-jobs); a queue row's Compare button opens its run here. */
(() => {
  'use strict';
  const root = document.getElementById('mirrorwall');
  if (!root || !document.getElementById('mw-compare-view')) return;
  const $ = id => document.getElementById('mw-cmp-' + id), Grid = window.SuiteComparisonGrid;
  const grid = Grid.create({prefix: 'mw-cmp', store: 'image-comparison'});
  const running = status => ['validating', 'in_progress', 'finalizing', 'cancelling', 'paused'].includes(status);
  const NAMES = {seed: 'seed', steps: 'steps', cfg_scale: 'CFG', guidance: 'guidance', width: 'width', height: 'height', sampler: 'sampler', scheduler: 'scheduler'};
  let runs = [], open = null, timer = null, chosen = false;

  const say = text => { $('status').textContent = text; };
  const label = job => {
    const varied = Object.keys(job.settings?.sweep || {}).map(key => NAMES[key] || key).join(' × ') || 'images';
    const when = window.suiteFormatTime?.(job.created * 1000, false) ?? new Date(job.created * 1000).toLocaleString();
    return `${job.model || job.settings?.request?.model || 'Image run'} · ${varied} · ${Grid.total(job)} images · ${when}`;
  };
  /** The run chooser lists every saved run of more than one image, newest first, and keeps the open one chosen. */
  function list(jobs) {
    runs = jobs.filter(Grid.comparable);
    const select = $('run'), options = [['', runs.length ? 'Choose a run…' : 'No runs to compare yet'], ...runs.map(job => [job.id, label(job)])];
    if (JSON.stringify(options) !== JSON.stringify([...select.options].map(option => [option.value, option.text])))
      select.replaceChildren(...options.map(([value, text]) => new Option(text, value)));
    select.value = runs.some(job => job.id === open?.id) ? open.id : '';
    if (!runs.length && !open) say('Run a parameter sweep, or generate more than one image, to compare its images here.');
  }
  async function lines(identifier) {
    const answer = await fetch('/mirrorwall-api/jobs/' + encodeURIComponent(identifier) + '/lines', {cache: 'no-store'});
    if (answer.redirected) { location.href = '/sign-in'; throw new Error('Please sign in again.'); }
    const found = await answer.json().catch(() => ({}));
    if (!answer.ok) throw new Error(found.error?.message || `The image service answered ${answer.status}.`);
    return found;
  }
  /** Open a run: its varied settings become the filter groups and captions; images fill in as they finish. */
  async function show(identifier) {
    clearTimeout(timer);
    if (!identifier) { open = null; grid.reset(); say(''); return; }
    const job = runs.find(one => one.id === identifier) || {id: identifier};
    open = {id: identifier}; $('run').value = identifier;
    try {
      const found = await lines(identifier);
      if (open?.id !== identifier) return;
      const varied = Grid.axes(job.settings, found.lines);
      grid.reset(varied, varied);
      draw(found);
    } catch (error) { say(error.message); }
  }
  async function poll() {
    if (!open || root.dataset.view !== 'compare') return;
    const identifier = open.id;
    try { const found = await lines(identifier); if (open?.id === identifier) draw(found); }
    catch (error) { say(error.message); timer = setTimeout(poll, 4000); }
  }
  function draw(found) {
    const next = found.lines.find(line => line.state === 'waiting');
    for (const line of found.lines) grid.line(line, {active: running(found.status) && found.status !== 'paused' && line === next, progress: found.progress});
    const done = found.lines.filter(line => line.state === 'done').length, failed = found.lines.filter(line => line.state === 'failed').length;
    const summary = `${done} of ${found.lines.length} done` + (failed ? ` · ${failed} failed` : '');
    clearTimeout(timer);
    if (running(found.status)) {
      say((found.status === 'paused' ? 'Paused · ' : 'Running · ') + summary);
      timer = setTimeout(poll, found.status === 'paused' ? 4000 : 1500);
    } else say(summary + '.');
  }

  $('run').addEventListener('change', () => { chosen = true; show($('run').value); });
  document.addEventListener('weightroom:image-jobs', event => {
    list(event.detail.jobs || []);
    // Opening Compare with nothing chosen shows the newest run with a sweep, or else the newest run.
    if (!open && !chosen && root.dataset.view === 'compare' && runs.length)
      show((runs.find(job => Object.keys(job.settings?.sweep || {}).length) || runs[0]).id);
  });
  document.addEventListener('weightroom:image-compare', event => { chosen = true; show(event.detail.id); });
  document.addEventListener('weightroom:image-view', event => {
    if (event.detail.view !== 'compare') { clearTimeout(timer); return; }
    if (open) poll();
    else if (runs.length && !chosen) show((runs.find(job => Object.keys(job.settings?.sweep || {}).length) || runs[0]).id);
  });
})();
