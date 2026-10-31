// The dry fit page: models ticked onto cards, each card drawn as a bar. Nothing here asks compute for anything
// but what the roles would pick, and nothing is loaded.
//
// A row of the list is a model. It carries the points its memory is drawn through, as [context, [MiB on each card]],
// and how many cards it needs. See fit_line() in app.py. Under the bars every such model has a line of its own, with
// its name and its slider, which is hidden until the model is ticked. That line carries the model's maximum context
// and the lowest the automatic fit may give it.

const table = document.getElementById("fit-table"), auto = document.getElementById("fit-auto");
const rooms = [...document.querySelectorAll(".fit-room")], margin = +table.dataset.margin;
/** List GPU model rows that have usable measured placement points. */
const models = () => [...table.tBodies[0].rows].filter(row => row.dataset.points);
/** List the catalogue rows representing CPU models. */
const cpuModels = () => [...table.tBodies[0].rows].filter(row => row.dataset.cpu);
const picks = document.getElementById("fit-picks");
/** Find the selected model's controls beneath the resource bars. */
const pick = row => picks.querySelector(`.fit-pick[data-model="${CSS.escape(row.dataset.model)}"]`);  // a model's line under the bars
/** Find the selected model's context-size slider. */
const slider = row => pick(row).querySelector(".fit-context");
/** Read the preview context size from its slider or fixed model limit. */
const context = row => slider(row) ? +slider(row).value : +pick(row).dataset.top;
/** Return whether the model is included in the current placement preview. */
const ticked = row => Boolean(row.querySelector(".fit-on:checked"));
/** Sum the modeled memory usage of the selected placement pieces. */
const sum = pieces => pieces.reduce((total, piece) => total + piece.mib, 0);
// A size in MiB as the rest of the site writes one.
/** Format preview memory in megabytes or gigabytes. */
const size = mib => mib >= 1000 ? (mib / 1024).toFixed(1) + " GB" : Math.round(mib) + " MB";
// What a card has for models: all of it, less the margin, less what other programs use. Without end for a card of unknown size.
/** Read usable device capacity after margin and external usage. */
const usable = room => room.hasAttribute("data-total") ? +room.dataset.total - +(room.dataset.margin ?? margin) - +room.dataset.others : Infinity;

// What a model takes on one of its cards at a context: on the straight line through the two points nearest to it.
// Below the first point and above the last, the nearest line is carried on.
/** Interpolate measured model memory at the requested context and device position. */
function takes(points, at, place) {
  if (points.length < 2) return points[0][1][place];
  let end = points.findIndex(point => point[0] >= at);
  end = end < 0 ? points.length - 1 : Math.max(1, end);
  const [context0, mib0] = points[end - 1], [context1, mib1] = points[end];
  return Math.max(0, mib0[place] + (mib1[place] - mib0[place]) * (at - context0) / (context1 - context0));
}

// Every card with the models ticked onto it, as { row, mib }. A model's first ticked card takes what its first card measured.
/** Compute the preview memory assigned to each selected device. */
function loads() {
  const on = Object.fromEntries(rooms.map(room => [room.dataset.card, []]));
  for (const row of models()) {
    const points = JSON.parse(row.dataset.points);
    [...row.querySelectorAll(".fit-on:checked")].forEach((box, place) => on[box.dataset.card].push({ row, mib: takes(points, context(row), place) }));
    if (on.__cpu__ && ticked(row) && +row.dataset.hostRamMib) on.__cpu__.push({row, mib: +row.dataset.hostRamMib});
  }
  for (const row of cpuModels()) if (on.__cpu__ && row.querySelector(".fit-cpu:checked")) {
    on.__cpu__.push({row, mib: +row.dataset.cpuMib});
  }
  return on;
}

// Whether what is ticked onto each of these cards fits on it.
/** Check modeled device usage against every selected device's usable capacity. */
const fits = judged => { const on = loads(); return judged.every(room => sum(on[room.dataset.card]) <= usable(room)); };

// One piece of a bar, as wide as its share of the bar.
/** Create one labeled memory segment for a resource bar. */
function piece(mib, scale, label, classes, colour = "") {
  const part = document.createElement("div");
  part.className = "progress-bar " + classes;
  part.style.width = 100 * mib / scale + "%";
  part.style.backgroundColor = colour;
  part.textContent = label;
  part.title = `${label}: ${size(mib)}`;
  return part;
}

/** Render current placements, capacity warnings and context controls. */
function draw() {
  // A colour for each ticked model, in the order of the list. Steps of 137.5 degrees round the circle keep neighbours well apart.
  models().filter(ticked).forEach((row, place) => pick(row).style.setProperty("--hue", place * 137.5 % 360));
  for (const row of models()) {
    const points = JSON.parse(row.dataset.points), line = pick(row);
    line.hidden = !ticked(row);
    line.querySelector(".fit-below").hidden = context(row) >= line.dataset.low;
    line.querySelector(".fit-tokens").textContent = row.dataset.fixed === "true" ? "" : context(row).toLocaleString("en-US");
    line.querySelector(".fit-memory").textContent = points[0][1].map((_, place) => size(takes(points, context(row), place))).join(" + ");
  }
  document.getElementById("fit-none").hidden = models().some(ticked) || cpuModels().some(row => row.querySelector(".fit-cpu:checked"));
  const on = loads();
  const exclusive = document.getElementById('fit-exclusive-warning');
  if (exclusive) {
    const conflicts = rooms.filter(room => room.dataset.card !== '__cpu__' && on[room.dataset.card].length > 1 && on[room.dataset.card].some(piece => piece.row.dataset.exclusive === 'true'));
    exclusive.hidden = !conflicts.length; exclusive.textContent = conflicts.length ? 'Exclusive placement conflict on ' + conflicts.map(room => room.dataset.card).join(', ') + ': these models require an empty card.' : '';
  }
  for (const room of rooms) {
    const mine = on[room.dataset.card], need = sum(mine), says = room.querySelector(".fit-says");
    const total = +room.dataset.total || 0, others = +room.dataset.others || 0, left = usable(room) - need;
    // A bar is as long as its card. With more ticked than fits it is as long as what is ticked, and a line marks where the card ends.
    const scale = Math.max(total, others + need + (total ? margin : 0)) || 1;
    const pieces = mine.map((one, place) => piece(one.mib, scale, one.row.dataset.model || one.row.dataset.cpu, "", `hsl(${place * 137.5 % 360} 60% 42%)`));
    if (room.hasAttribute("data-total")) {
      pieces.unshift(piece(others, scale, "other programs", "bg-secondary"));
      const reserved = +(room.dataset.margin ?? margin);
      pieces.push(piece(Math.max(0, left), scale, "", "bg-transparent"), piece(reserved, scale, reserved ? "kept free" : "", "progress-bar-striped bg-secondary"));
    }
    if (left < 0) {
      const limit = document.createElement("div");
      limit.className = "fit-limit";
      limit.style.left = 100 * (total - margin) / scale + "%";
      pieces.push(limit);
    }
    room.querySelector(".fit-bar").replaceChildren(...pieces);
    says.className = "fit-says" + (left < 0 ? " text-danger fw-bold" : "");
    says.textContent = !total ? `${size(need)} needed` : left < 0 ? `${size(need)} needed of ${size(usable(room))}: over by ${size(-left)}` : `${size(need)} needed of ${size(usable(room))}: ${size(left)} free`;
  }
}

// The largest contexts that fit on the cards in `judged`. `set` is given a share from 0 to 1 and moves sliders by it.
// The share that still fits is found by halving: twelve times is finer than a slider's steps.
/** Find the highest common context fraction that fits the selected placements. */
function highest(set, judged) {
  set(1);
  if (fits(judged)) return;
  let low = 0, high = 1;
  for (let turn = 0; turn < 12; turn++) {
    const middle = (low + high) / 2;
    set(middle);
    if (fits(judged)) low = middle; else high = middle;
  }
  set(low);
}

// First every ticked model gets the same share of its range, from its lowest context to its maximum. A model that is not
// on the fullest card could have more than that, so then each in turn takes what is left.
/** Adjust selected context sliders within measured limits to fit available memory. */
function autoFit() {
  /** Read the model's selected device checkboxes. */
  const cards = row => [...row.querySelectorAll(".fit-on:checked")].map(box => box.dataset.card);
  const chosen = models().filter(row => slider(row) && ticked(row));
  const floor = new Map(chosen.map(row => [row, +pick(row).dataset.low]));  // the lowest each may be given: at first its own minimum
  /** Assign each selected model its interpolated context fraction. */
  const place = (rows, share) => rows.forEach(row => { slider(row).value = floor.get(row) + share * (slider(row).max - floor.get(row)); });
  // With every model at its lowest: the cards that fit, and the models that are on such cards only.
  /** Check whether the selected models fit at their minimum contexts. */
  const lowest = () => {
    place(chosen, 0);
    const on = loads(), good = rooms.filter(room => sum(on[room.dataset.card]) <= usable(room));
    return [good, chosen.filter(row => cards(row).every(card => good.some(room => room.dataset.card === card)))];
  };
  // A card can be too full with every model at its minimum context. Its models may then go under their minimum, as far as
  // the slider goes: draw() says so on the model's line. A card that is too full even so cannot be helped by a context.
  // Its models stay at their lowest, and the other cards are fitted without it.
  let [good, moved] = lowest();
  for (const row of chosen) if (!moved.includes(row)) floor.set(row, +slider(row).min);
  [good, moved] = lowest();
  highest(share => place(moved, share), good);
  for (const row of moved) {
    const from = +slider(row).value;
    highest(share => { slider(row).value = from + share * (slider(row).max - from); }, good);
  }
}

/** Recompute automatic fit when enabled and redraw the resource preview. */
const settle = () => { if (auto.checked) autoFit(); draw(); };

// A model is on as many cards as it needs, or on none. Ticking one card of a model that needs two ticks another too,
// and ticking a second card of a model that needs one moves it there.
table.addEventListener("change", event => {
  const box = event.target;
  if (box.matches('.fit-cpu')) { settle(); htmx.trigger('#fit-roles', 'ticked'); return; }
  if (!box.matches(".fit-on")) return;
  const row = box.closest("tr"), boxes = [...row.querySelectorAll(".fit-on")];
  const wanted = box.checked ? [box, ...boxes.filter(other => other !== box && other.checked), ...boxes.filter(other => !other.checked)].slice(0, +row.dataset.need) : [];
  boxes.forEach(other => { other.checked = wanted.includes(other); });
  settle();
  htmx.trigger("#fit-roles", "ticked");  // what the roles would pick now. See pages/fit_roles.html
});
picks.addEventListener("input", () => {  // only the sliders are in there
  auto.checked = false;  // a context set by hand stays as it was set
  draw();
});
auto.addEventListener("change", settle);
settle();

// The save operation sends only the visible selection. Definitions and actual admission
// are validated again by compute; interpolated bar sizes never become memory evidence.
const saveFit = document.getElementById('fit-save');
if (saveFit) {
  const fitName = document.getElementById('fit-name'), replaceFit = document.getElementById('fit-replace');
  const fitMessage = document.getElementById('fit-selection-message');
  saveFit.addEventListener('submit', () => {
    const chosen = models().filter(row => ticked(row) && row.dataset.fixed !== 'true').map(row => ({model: row.dataset.model,
      cards: [...row.querySelectorAll('.fit-on:checked')].map(box => box.dataset.card), context: context(row)}));
    for (const row of table.querySelectorAll('[data-cpu]')) {
      if (row.dataset.fixed === 'true') continue;
      if (row.querySelector('.fit-cpu').checked) chosen.push({model: row.dataset.cpu, cards: [], context: +row.querySelector('.fit-cpu-context').value});
    }
    document.getElementById('fit-selection').value = JSON.stringify(chosen);
    const previous = [...document.querySelectorAll('[data-loadout-name]')].find(row => row.dataset.loadoutName === fitName.value.trim());
    document.getElementById('fit-version').value = replaceFit.checked && previous ? previous.dataset.version : '';
  });
  for (const button of document.querySelectorAll('[data-loadout-edit]')) button.addEventListener('click', () => {
    const saved = JSON.parse(button.dataset.loadoutEdit), warnings = [];
    for (const row of models()) {
      const selected = saved.models.find(one => one.model_id === row.dataset.modelId);
      row.querySelectorAll('.fit-on').forEach(box => { box.checked = Boolean(selected && selected.cards.includes(box.dataset.card)); });
      if (selected && slider(row)) {
        slider(row).value = selected.context;
        if (context(row) !== selected.context) warnings.push(`${row.dataset.model}: review the rounded slider context`);
      }
    }
    for (const row of table.querySelectorAll('[data-cpu]')) {
      const selected = saved.models.find(one => one.model_id === row.dataset.modelId);
      row.querySelector('.fit-cpu').checked = Boolean(selected);
      if (selected) row.querySelector('.fit-cpu-context').value = selected.context;
    }
    for (const selected of saved.models) {
      if (![...table.querySelectorAll('[data-model-id]')].some(row => row.dataset.modelId === selected.model_id)) warnings.push(`${selected.name || selected.label}: not available in these controls`);
    }
    auto.checked = false;
    fitName.value = saved.name;
    replaceFit.checked = true;
    settle();
    htmx.trigger('#fit-roles', 'ticked');
    fitMessage.textContent = warnings.length ? warnings.join('; ') : 'Saved selection is in the controls above. Review before saving a replacement.';
  });
}

// Filtering never changes the combined selection or its resource bars.
for (const pill of document.querySelectorAll('[data-fit-kind]')) pill.addEventListener('click', () => {
  for (const other of document.querySelectorAll('[data-fit-kind]')) {other.classList.toggle('active',other===pill);other.setAttribute('aria-pressed',String(other===pill));}
  for (const row of table.querySelectorAll('[data-fit-type]')) row.hidden=row.dataset.fitType!==pill.dataset.fitKind;
});
document.querySelector('[data-fit-kind="llm"]')?.click();
