// Viewing evidence uses GET navigation. Run and definition changes use the normal POST forms.
document.addEventListener("change", event => {
  if (event.target.matches('[data-benchmark-select]')) {
    const form = event.target.form;
    // A fast second choice may arrive before HTMX's settle callback initializes
    // the replaced form. Initialize it now so submission stays in the fragment.
    if (form.hasAttribute('hx-boost')) window.htmx?.process(form);
    form.requestSubmit();
  }
});

// The model name remains a normal keyboard-accessible link; clicking elsewhere in its row also opens details.
document.addEventListener("click", event => {
  const row = event.target.closest("[data-result-link]");
  if (row && !event.target.closest("a, button, input, select, label") && !getSelection().toString()) row.querySelector("a").click();
});

document.addEventListener("click", event => {
  const row = event.target.closest("[data-select-row]");
  if (!row || event.target.closest("input, select, a, button, label") || getSelection().toString()) return;
  const box = row.querySelector('input[name="run-model"], input[name="run-target"]');
  if (box && !box.disabled) { box.checked = !box.checked; box.dispatchEvent(new Event("change", { bubbles: true })); }
});

const benchmarkDrafts = new Map();
let benchmarkSetup = null;
let benchmarkSearch = '';
const targetBoxes = () => [...document.querySelectorAll('input[name="run-model"]:not(:disabled), input[name="run-target"]:not(:disabled)')];
const visibleTarget = box => !box.closest('[data-target-choice]')?.hidden && !box.closest('[data-model-row]')?.hidden;
const benchmarkId = () => document.querySelector('#start-benchmark [name="benchmark"]')?.value;
/** Update the selected benchmark targets and submission availability. */
function count(all) {
  const setup = document.querySelector('#start-benchmark');
  if (setup !== benchmarkSetup) {
    const draft = benchmarkDrafts.get(benchmarkId());
    if (draft) targetBoxes().forEach(box => { box.checked = draft.has(box.value); });
    benchmarkSetup = setup;
    const search = document.querySelector('[data-model-search]');
    if (search) { search.value = benchmarkSearch; filterBenchmarkModels(benchmarkSearch); }
    const historyBenchmark = document.querySelector('#benchmark-history [name="benchmark"]');
    if (historyBenchmark && benchmarkId()) historyBenchmark.value = benchmarkId();
  }
  const allModels = document.querySelector("[data-select-models]");
  const boxes = targetBoxes();
  if (all && allModels) boxes.filter(visibleTarget).forEach(box => { box.checked = allModels.checked; });
  const selected = boxes.filter(box => box.checked).length;
  const label = document.querySelector("[data-selected-count]");
  const lanes = !!document.querySelector('[data-machine-lane]');
  if (label) label.textContent = `${selected} ${lanes ? 'run' : document.querySelector('[data-select-node]') ? 'node' : 'model'}${selected === 1 ? '' : 's'} selected`;
  document.querySelectorAll('[data-queue-selected]').forEach(button => {
    button.disabled = !selected || selected > 100 || button.dataset.mayRun !== 'true';
  });
  const note = document.querySelector('[data-selection-note]');
  if (note) {
    const hidden = boxes.filter(box => box.checked && !visibleTarget(box)).length;
    note.textContent = selected > 100 ? 'Choose at most 100 runs for this batch.' : hidden ? `${hidden} selected outside this search. Review includes all selections.` : 'Exact targets · active pairs excluded · one to 100 runs per batch';
  }
  if (lanes) benchmarkDrafts.set(benchmarkId(), new Set(boxes.filter(box => box.checked).map(box => box.value)));
  if (allModels) {
    const visible = boxes.filter(visibleTarget), chosen = visible.filter(box => box.checked).length;
    allModels.checked = !!visible.length && chosen === visible.length;
    allModels.indeterminate = chosen > 0 && chosen < visible.length;
    allModels.disabled = !visible.length;
  }
  document.querySelectorAll('[data-select-node]').forEach(control => {
    const nodeBoxes = boxes.filter(box => box.dataset.node === control.dataset.selectNode && visibleTarget(box));
    const nodeSelected = nodeBoxes.filter(box => box.checked).length;
    control.checked = !!nodeBoxes.length && nodeSelected === nodeBoxes.length;
    control.indeterminate = nodeSelected > 0 && nodeSelected < nodeBoxes.length;
    control.disabled = !nodeBoxes.length;
    const nodeCount = control.closest('[data-machine-lane]')?.querySelector('[data-node-count]');
    if (nodeCount) nodeCount.textContent = nodeBoxes.length;
  });
  const rows = [...document.querySelectorAll('[data-model-row]')];
  rows.forEach(row => {
    const control = row.querySelector('[data-select-model]');
    const eligible = [...row.querySelectorAll('input[name="run-target"]:not(:disabled)')];
    const chosen = eligible.filter(box => box.checked).length;
    control.checked = !!eligible.length && chosen === eligible.length;
    control.indeterminate = chosen > 0 && chosen < eligible.length;
    control.disabled = !eligible.length;
  });
  const modelCount = document.querySelector('[data-model-count]');
  if (modelCount) {
    const visible = rows.filter(row => !row.hidden).length;
    modelCount.textContent = `${visible} of ${rows.length} models`;
    const empty = document.querySelector('[data-model-empty]');
    if (empty) empty.hidden = visible > 0;
  }
}
document.addEventListener("change", event => {
  if (event.target.matches('[data-select-model]')) {
    event.target.closest('[data-model-row]').querySelectorAll('input[name="run-target"]:not(:disabled)')
      .forEach(box => { box.checked = event.target.checked; });
  }
  if (event.target.matches('[data-select-node]')) {
    targetBoxes().filter(box => box.dataset.node === event.target.dataset.selectNode && visibleTarget(box))
      .forEach(box => { box.checked = event.target.checked; });
  }
  if (event.target.matches('[data-select-model], [data-select-models], [data-select-node], input[name="run-model"], input[name="run-target"]')) count(event.target.matches("[data-select-models]"));
});
document.addEventListener('htmx:afterSwap', () => count());
count();

// Keep live logs at their newest row without scrolling the document. Table viewports use
// the measured header and first ten rows, so wrapped queue reasons still count as one row.
const benchmarkScroll = new Map();
/** Refresh benchmark selections and controls after fragment replacement. */
function refreshBenchmarkPanels() {
  document.querySelectorAll("[data-benchmark-rows]").forEach(viewport => {
    if (!viewport.getClientRects().length) return;
    const table = viewport.querySelector("table");
    const rows = [...table.tBodies[0].rows].slice(0, Number(viewport.dataset.benchmarkRows));
    const height = table.tHead.getBoundingClientRect().height + rows.reduce((sum, row) => sum + row.getBoundingClientRect().height, 0);
    viewport.style.maxHeight = `${Math.min(Math.ceil(height) + 1, Number(viewport.dataset.benchmarkMaxHeight) || Infinity)}px`;
    if (benchmarkScroll.has(viewport.id)) viewport.scrollTop = benchmarkScroll.get(viewport.id);
  });
  document.querySelectorAll(".benchmark-log").forEach(log => { log.scrollTop = log.scrollHeight; });
}
document.addEventListener("scroll", event => {
  if (event.target.matches?.("[data-benchmark-rows]")) benchmarkScroll.set(event.target.id, event.target.scrollTop);
}, true);
for (const name of ["htmx:afterSettle", "htmx:oobAfterSwap"]) {
  document.addEventListener(name, () => requestAnimationFrame(refreshBenchmarkPanels));
}
document.addEventListener("toggle", () => requestAnimationFrame(refreshBenchmarkPanels), true);
window.addEventListener("resize", refreshBenchmarkPanels);
refreshBenchmarkPanels();

/** Keep polling on this workspace, with its current setup test and history filters and page.
 * htmx fixes a poller's hx-get when it first processes it, so the path is rewritten per request. */
document.addEventListener('htmx:configRequest', event => {
  const poller = event.detail.elt;
  if (!poller?.matches?.('[data-benchmark-poller]')) return;
  const url = new URL(event.detail.path, location.href);
  const query = document.querySelector('#benchmark-history')?.dataset.liveQuery || url.search;
  const params = new URLSearchParams(query);
  if (benchmarkId()) params.set('benchmark', benchmarkId());
  event.detail.path = url.pathname + '?' + params;
});
function filterBenchmarkModels(value) {
  const query = value.trim().toLocaleLowerCase();
  const rows = document.querySelectorAll('[data-model-row]');
  if (rows.length) rows.forEach(row => { row.hidden = !row.dataset.search.includes(query); });
  else document.querySelectorAll('[data-target-choice]').forEach(choice => { choice.hidden = !choice.dataset.search.includes(query); });
  const list = document.getElementById('benchmark-models');
  if (list) list.scrollTop = 0;
}
document.addEventListener('input', event => {
  if (!event.target.matches('[data-model-search]')) return;
  benchmarkSearch = event.target.value;
  filterBenchmarkModels(benchmarkSearch);
  count();
});
document.addEventListener('click', event => {
  const button = event.target.closest('[data-select-gaps], [data-clear-targets], [data-review-close]');
  if (!button) return;
  if (button.hasAttribute('data-review-close')) { button.closest('dialog').close(); return; }
  targetBoxes().forEach(box => {
    if (button.hasAttribute('data-clear-targets')) box.checked = false;
    else if (visibleTarget(box) && box.dataset.missing === 'true') box.checked = true;
  });
  count();
});
let reviewedTargets = '';
// Intercept before native/HTMX submission and shared submission feedback.
document.addEventListener('submit', event => {
  if (!event.target.matches('[data-review-targets]')) return;
  const boxes = targetBoxes().filter(box => box.checked), dialog = document.getElementById('benchmark-review');
  const signature = JSON.stringify([benchmarkId(), boxes.map(box => box.value).sort()]);
  if (event.submitter?.matches('[data-confirm-batch]') && reviewedTargets === signature && boxes.length && boxes.length <= 100) return;
  event.preventDefault(); event.stopImmediatePropagation();
  if (!boxes.length || boxes.length > 100 || !dialog) return;
  const body = dialog.querySelector('[data-review-rows]'); body.replaceChildren();
  boxes.forEach(box => {
    const target = JSON.parse(box.value), row = document.querySelector('#benchmark-review-row').content.firstElementChild.cloneNode(true);
    row.querySelector('[data-model]').textContent = target.model; row.querySelector('[data-node]').textContent = target.machine; body.append(row);
  });
  const models = new Set(boxes.map(box => JSON.parse(box.value).model)), nodes = new Set(boxes.map(box => box.dataset.node));
  dialog.querySelector('[data-review-summary]').textContent = `${boxes.length} runs · ${models.size} models · ${nodes.size} nodes · ${document.querySelector('#benchmark option:checked').textContent}`;
  reviewedTargets = signature;
  if (!dialog.open) dialog.showModal();
}, true);
