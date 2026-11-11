// Shared placement and model form controls, including htmx replacements.
(() => {
  'use strict';
  const modeOf = box => box.querySelector('[data-hardware-mode]:checked')?.value || 'auto';
  function updateHardware(box, changed = false) {
    const mode = modeOf(box), pins = box.querySelector('[data-hardware-pins]');
    box.querySelector('[data-hardware-choice]').hidden = false;
    const fallback = box.querySelector('[data-hardware-fallback]');
    if (fallback) fallback.hidden = true;
    pins.hidden = mode !== 'pin'; pins.disabled = mode !== 'pin';
    const cpu = box.querySelector('[data-placement-device="cpu"]'), gpu = box.querySelector('[data-placement-device="gpu"]');
    if (cpu) cpu.checked = mode === 'cpu';
    if (gpu) gpu.checked = mode !== 'cpu';
    if (changed) {
      box.querySelectorAll('[data-gpu-compat]').forEach(input => { input.checked = false; });
      delete box.dataset.automaticGpus;
    }
    const split = box.querySelector('[data-hardware-split]');
    if (split) { split.hidden = mode !== 'auto'; split.querySelector('input').disabled = mode !== 'auto'; }
    const choices = [...pins.querySelectorAll('input')];
    if (choices.length) choices[0].setCustomValidity(mode === 'pin' && !choices.some(input => input.checked) ? 'Choose at least one GPU.' : '');
  }
  // The image/audio editors supply their live node inventories after the page renders.
  window.SuiteHardware = {
    set(prefix, gpus, cards = []) {
      const box = document.querySelector(`[data-hardware-prefix="${prefix}"]`);
      if (!box) return;
      const pinned = Array.isArray(gpus) ? gpus : [];
      const mode = gpus === 0 ? 'cpu' : pinned.length ? 'pin' : 'auto';
      const area = box.querySelector('[data-hardware-cards]'); area.replaceChildren();
      [...new Set([...cards, ...pinned])].forEach(card => {
        const label = box.querySelector('[data-hardware-card]').content.firstElementChild.cloneNode(true);
        const input = label.querySelector('input'); input.value = card; input.checked = pinned.includes(card);
        label.querySelector('.form-check-label').textContent = card;
        area.append(label);
      });
      box.querySelector(`[data-hardware-mode][value="${mode}"]`).checked = true;
      box.querySelector('[data-hardware-empty]').hidden = area.children.length > 0;
      if (typeof gpus === 'number' && gpus > 0) box.dataset.automaticGpus = String(gpus);
      else delete box.dataset.automaticGpus;
      updateHardware(box);
    },
    read(prefix, automatic = 1) {
      const box = document.querySelector(`[data-hardware-prefix="${prefix}"]`), mode = modeOf(box);
      if (mode === 'cpu') return 0;
      if (mode === 'pin') {
        const cards = [...box.querySelectorAll('[data-gpu-card]:checked')].map(input => input.value);
        if (!cards.length) throw new Error('Choose at least one GPU.');
        return cards;
      }
      return Number(box.dataset.automaticGpus) || automatic;
    }
  };
  // Each node column of the matrix has its own parallel controls; data-prefix names its fields.
  function updateParallel() {
    document.querySelectorAll('[data-parallel-switch]').forEach(box => {
      (box.getAttribute('aria-controls') || '').split(/\s+/).forEach(id => {
        const field = id && document.getElementById(id);
        if (field && field !== box) field.disabled = !box.checked;
      });
    });
    document.querySelectorAll('[data-context-summary]').forEach(summary => {
      const form = summary.closest('form'), prefix = summary.dataset.prefix || '', field = name => form?.elements[prefix + name];
      const box = field('parallel_enabled');
      if (!box || !field('ctx')) return;
      const count = box.checked ? Math.max(1, Number(field('parallel_count').value)) : 1;
      const total = Math.max(1, Number(field('ctx').value));
      const shared = field('parallel_shared').checked;
      const limit = box.checked ? Math.min(total, Math.max(1, Number(field('parallel_context').value)), shared ? total : Math.floor(total / count)) : total;
      summary.textContent = `${count.toLocaleString()} request${count === 1 ? '' : 's'}, up to ${limit.toLocaleString()} tokens each`;
    });
  }
  document.addEventListener('change', event => {
    const box = event.target.closest('[data-hardware]');
    if (box && event.target.matches('[data-hardware-mode], [data-gpu-card]')) updateHardware(box, event.target.matches('[data-hardware-mode]'));
    if (box && event.target.matches('[data-placement-device]')) {
      box.querySelector(`[data-hardware-mode][value="${event.target.value === 'yes' && event.target.name === 'cpu' && event.target.checked ? 'cpu' : 'auto'}"]`).checked = true;
      updateHardware(box, true);
    }
    updateParallel();
  });
  document.addEventListener('input', event => { if (event.target.closest('[data-model-settings]')) updateParallel(); });
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-add-row]');
    if (!button) return;
    const form = button.closest('form'), name = button.dataset.addRow;
    const row = form.querySelector(`[data-row-template="${name}"]`).content.cloneNode(true);
    const first = row.querySelector('input');
    const rows = form.querySelector(`[data-${name}-rows]`), anchor = rows.querySelector(':scope > [data-row-anchor]');
    if (anchor) anchor.before(row); else rows.append(row);
    first?.focus();
  });
  const update = () => { document.querySelectorAll('[data-hardware]').forEach(box => updateHardware(box)); updateParallel(); };
  document.addEventListener('htmx:afterSwap', update);
  update();
})();
