// Click a column's heading to sort a table by it. Every table with the class "sortable" has this.
//
// A heading marked data-sort="value" is over amounts or times: the first click puts the largest
// first, the second the smallest, the third takes the column out of it again. A heading marked
// data-sort="text" is over words: first A to Z, then Z to A, then out.
//
// Several columns can be in it at once. The one clicked first counts most, and each heading
// shows its place in that order. With no column in it, the rows are as the server sent them.
//
// What was chosen is remembered for each table, by its id, for as long as the page stays open.
// So a table that htmx swaps for a fresh copy comes back sorted the same way.

const chosen = {};  // a table's id -> the columns it is sorted by, as [{ column, down }], most important first

// New console tables inherit sortable, keyboard-accessible headings.
function prepareSortableTables() {
  document.querySelectorAll('table.sortable').forEach((table, index) => {
    if (!table.tHead || !table.tBodies.length) return;
    table.id ||= 'console-table-' + index;
    [...(table.tHead.querySelector('[data-sort-columns]') || table.tHead.rows[0]).cells].forEach(head => {
      if (!head.textContent.trim() || head.querySelector('input, select, button')) return;
      head.dataset.sort ||= 'text';
      head.tabIndex = 0;
      head.setAttribute('aria-label', 'Sort by ' + head.textContent.trim());
    });
  });
}
prepareSortableTables();
document.addEventListener('htmx:afterSwap', prepareSortableTables);
document.addEventListener('keydown', event => {
  if (['Enter', ' '].includes(event.key) && event.target.matches('table.sortable thead th[data-sort]')) {
    event.preventDefault(); event.target.click();
  }
});

/** Apply stable multi-column sorting to the selected operator table. */
function sortTable(table) {
  const picks = chosen[table.id] || [];
  [...(table.tHead.querySelector('[data-sort-columns]') || table.tHead.rows[0]).cells].forEach((head, column) => {
    const place = picks.findIndex(pick => pick.column === column);
    head.dataset.mark = place < 0 ? "" : (picks[place].down ? "\u25BC" : "\u25B2") + (picks.length > 1 ? place + 1 : "");
  });

  const body = table.tBodies[0], rows = [...body.rows];
  rows.forEach((row, place) => { row.dataset.place ??= place; });  // where the server put it, kept for "out"
  // A cell may carry the plain amount behind what it shows, as data-value: 1500000000 behind "1.4 GB".
  /** Read a cell's explicit sort value or visible text. */
  const read = (row, column) => { const cell = row.cells[column]; return cell ? cell.dataset.value ?? cell.textContent.trim() : ""; };
  rows.sort((one, other) => {
    for (const { column, down } of picks) {
      const a = read(one, column), b = read(other, column);
      const numbers = a !== "" && b !== "" && !isNaN(a) && !isNaN(b);
      const order = numbers ? a - b : a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
      if (order) return down ? -order : order;
    }
    return one.dataset.place - other.dataset.place;
  });
  // Rows already in order are left where they are. Putting them in again empties the table for a moment, which
  // sends a table that scrolls back to its top, and every swap anywhere on the page comes through here.
  if (rows.some((row, place) => row !== body.rows[place])) body.append(...rows);
}

document.addEventListener("click", event => {
  const head = event.target.closest("table.sortable th[data-sort]");
  if (!head) return;
  prepareSortableTables();
  const table = head.closest("table"), column = head.cellIndex;
  const picks = chosen[table.id] ||= [];
  const place = picks.findIndex(pick => pick.column === column);
  const downFirst = head.dataset.sort === "value";
  if (place < 0) picks.push({ column, down: downFirst });                        // first click
  else if (picks[place].down === downFirst) picks[place].down = !downFirst;       // second: the other way
  else picks.splice(place, 1);                                                    // third: out
  sortTable(table);
});

// A table that htmx has just swapped in is a new one. Put its remembered order back on it.
document.addEventListener("htmx:afterSwap", () => {
  for (const table of document.querySelectorAll("table.sortable")) if (chosen[table.id]?.length) sortTable(table);
  rememberDiagnosticModel();
});

// Keep the model used to inspect logs for this signed-in account in this browser. Log and
// service-output pages share the same choice, while an unavailable old choice falls back to
// the first model the account may currently use.
/** Restore and persist the selected diagnostic model per account. */
function rememberDiagnosticModel() {
  const identity = document.documentElement.dataset.themeUser;
  if (!identity) return;
  const key = `weightroom-diagnostic-model-v1:${identity}`;
  let saved = "";
  try { saved = localStorage.getItem(key) || ""; } catch { /* private storage may be unavailable */ }
  for (const select of document.querySelectorAll("select[data-diagnostic-model]")) {
    if (saved && [...select.options].some(option => option.value === saved)) select.value = saved;
  }
}
rememberDiagnosticModel();
document.addEventListener("change", event => {
  if (!event.target.matches?.("select[data-diagnostic-model]")) return;
  const identity = document.documentElement.dataset.themeUser;
  if (!identity) return;
  try { localStorage.setItem(`weightroom-diagnostic-model-v1:${identity}`, event.target.value); } catch { /* keep the form usable */ }
});

// Type in a box to keep only the rows that hold what was typed. A box says which table it is
// for: <input type="search" data-filter="THE TABLE'S ID">. A row stays if it holds every word
// typed, anywhere in it, in any order, capitals or not. An element marked data-count="THE
// TABLE'S ID" is kept saying how many rows are showing. Sorting leaves hidden rows hidden.
document.addEventListener("input", event => {
  const table = document.getElementById(event.target.dataset.filter || "");
  if (!table) return;
  const words = event.target.value.toLowerCase().split(/\s+/).filter(Boolean);
  let showing = 0;
  for (const row of table.tBodies[0].rows) {
    const text = (row.dataset.filterText ?? row.textContent).toLowerCase();
    row.hidden = !words.every(word => text.includes(word));
    showing += !row.hidden;
  }
  for (const count of document.querySelectorAll(`[data-count="${table.id}"]`)) count.textContent = words.length ? `${showing} of ${table.tBodies[0].rows.length}` : "";
});

// Enter in such a box does nothing. Without this, a box that sits inside a form would send the form.
document.addEventListener("keydown", event => {
  if (event.key === "Enter" && event.target.dataset.filter) event.preventDefault();
});

// A box marked data-tail="THE TABLE'S ID" says how many characters of that table's lines are
// sent to a model, counted from the end. While the box is in use, the rows that would be sent
// are marked, and an element marked data-tail-says="THE TABLE'S ID" says how many they are.
// Whole lines only, as tail() in app.py cuts them: a line that does not fit entirely is left
// out. The last line is always sent, cut if it must be. Rows hidden by the finding box count
// too, since they are sent all the same.
for (const type of ["focusin", "input", "focusout"]) document.addEventListener(type, event => {
  const table = document.getElementById(event.target.dataset?.tail || "");
  if (!table) return;
  const rows = [...table.tBodies[0].rows].reverse();
  let room = Number(event.target.value) || 0, sent = 0;
  rows.forEach((row, back) => {
    room -= row.textContent.length + (back ? 1 : 0);  // and the line end that joins it to the next
    const goes = room >= 0 || back === 0;
    sent += goes;
    row.classList.toggle("sent", goes && type !== "focusout");
  });
  for (const says of document.querySelectorAll(`[data-tail-says="${table.id}"]`)) says.textContent = `The last ${sent} of ${rows.length} lines.`;
});

// The bar at the top stays in view while a page is scrolled. How tall it is is kept where the
// style sheet can read it, as --top, for whatever must stay just below it.
const bar = document.querySelector("header");
if (bar) {
  /** Update the CSS header offset from its actual rendered height. */
  const measure = () => document.documentElement.style.setProperty("--top", bar.offsetHeight + "px");
  measure();
  new ResizeObserver(measure).observe(bar);
}

// The menu under a person's name closes again when something outside it is clicked.
document.addEventListener("click", event => {
  for (const menu of document.querySelectorAll("details.user-menu[open]")) if (!menu.contains(event.target)) menu.open = false;
});
