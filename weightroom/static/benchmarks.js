// Viewing evidence uses GET navigation. Run and definition changes use the normal POST forms.
document.addEventListener("change", event => {
  if (event.target.matches("[data-benchmark-select]")) event.target.form.requestSubmit();
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

// The box that ticks every model, and the count under the table. Both are found afresh each time, so that a table
// htmx has just put in for another benchmark works as the first did.
/** Update the selected benchmark targets and submission availability. */
function count(all) {
  const allModels = document.querySelector("[data-select-models]");
  const boxes = [...document.querySelectorAll('input[name="run-model"]:not(:disabled), input[name="run-target"]:not(:disabled)')];
  if (all && allModels) boxes.forEach(box => { box.checked = allModels.checked; });
  const selected = boxes.filter(box => box.checked).length;
  const label = document.querySelector("[data-selected-count]");
  if (label) label.textContent = `${selected} ${document.querySelector('[data-select-node]') ? "node" : "model"}${selected === 1 ? "" : "s"} selected`;
  if (allModels) {
    allModels.checked = !!boxes.length && selected === boxes.length;
    allModels.indeterminate = selected > 0 && selected < boxes.length;
  }
  document.querySelectorAll('[data-select-node]').forEach(control => {
    const nodeBoxes = boxes.filter(box => box.dataset.node === control.dataset.selectNode);
    const nodeSelected = nodeBoxes.filter(box => box.checked).length;
    control.checked = !!nodeBoxes.length && nodeSelected === nodeBoxes.length;
    control.indeterminate = nodeSelected > 0 && nodeSelected < nodeBoxes.length;
  });
}
document.addEventListener("change", event => {
  if (event.target.matches('[data-select-node]')) {
    [...document.querySelectorAll('input[name="run-target"][data-node]:not(:disabled), input[name="run-model"][data-node]:not(:disabled)')].filter(box => box.dataset.node === event.target.dataset.selectNode)
      .forEach(box => { box.checked = event.target.checked; });
  }
  if (event.target.matches('[data-select-models], [data-select-node], input[name="run-model"], input[name="run-target"]')) count(event.target.matches("[data-select-models]"));
});
document.addEventListener("htmx:afterSwap", () => count());
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
    viewport.style.maxHeight = `${Math.ceil(height) + 1}px`;
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

document.addEventListener('change', event => {
  if (!event.target.matches('[data-target-node]')) return;
  const row=event.target.closest('tr'), box=row.querySelector('input[name="run-target"]');
  box.value=event.target.value;
  row.querySelector('[data-target-reason]').textContent='Target: '+JSON.parse(box.value).machine;
});
