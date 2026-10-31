// Site palettes and account-scoped browser preferences. Run in the head, before paint.
(() => {
  "use strict";
  const palettes = [
    {id: "slate", name: "Slate", note: "Cool blue and clean neutrals", light: ["#f3f6fb", "#ffffff", "#e8edf6", "#1e293b", "#526079", "#cbd5e1", "#315a9b", "#25477d", "#e2eafb"], dark: ["#101722", "#182231", "#202d40", "#e6edf8", "#acbad0", "#35455d", "#9ab9f5", "#315a9b", "#223650"]},
    {id: "ocean", name: "Ocean", note: "Deep navy and clear azure", light: ["#f0f7fc", "#ffffff", "#e1edf7", "#142c44", "#4d6379", "#bfd2e4", "#09629c", "#064b79", "#deeffc"], dark: ["#0b1926", "#122637", "#1a3247", "#e0effb", "#a5bdd1", "#304f68", "#7ec8ff", "#09629c", "#193e59"]},
    {id: "lagoon", name: "Lagoon", note: "Bright teal on coastal tones", light: ["#eff8f8", "#ffffff", "#dfeeee", "#173536", "#4b6667", "#bdd7d7", "#08736f", "#065a57", "#d8f0ed"], dark: ["#0d1d20", "#152c2f", "#1d393c", "#e0f2f0", "#a7c7c5", "#36595b", "#75d6cb", "#08736f", "#1c4544"]},
    {id: "forest", name: "Forest", note: "Evergreen and moss", light: ["#f1f7f2", "#ffffff", "#e2ece4", "#203629", "#526959", "#c1d4c6", "#286342", "#1d4d32", "#dcecdf"], dark: ["#111d17", "#1a2b21", "#23382c", "#e4f0e7", "#acc8b5", "#3b5544", "#91d7aa", "#286342", "#284638"]},
    {id: "sage", name: "Sage", note: "Quiet olive and soft stone", light: ["#f5f6ef", "#fffffb", "#e9ecdf", "#303729", "#616a54", "#d0d6c1", "#586a33", "#435127", "#e7edda"], dark: ["#1a1e17", "#252d20", "#323b2b", "#edf0e2", "#bbc5a9", "#4c5840", "#becf8f", "#586a33", "#374529"]},
    {id: "clay", name: "Clay", note: "Terracotta and warm linen", light: ["#faf4ef", "#fffcfa", "#f1e4dc", "#422c25", "#785a50", "#dfc9bd", "#9a472d", "#7b3520", "#f7e3d9"], dark: ["#241915", "#34241f", "#443029", "#f6e9e1", "#d2b5a6", "#63493d", "#eeb08d", "#9a472d", "#503024"]},
    {id: "rose", name: "Rose", note: "Muted berry and blush", light: ["#fbf3f6", "#fffbfd", "#f2e3ea", "#422635", "#785668", "#e0c4d1", "#9d365e", "#7d2849", "#f7dfea"], dark: ["#23151d", "#34212c", "#442c38", "#f8e5ee", "#d0adc0", "#644356", "#f0a4c2", "#9d365e", "#51283e"]},
    {id: "orchid", name: "Orchid", note: "Plum and gentle lavender", light: ["#f8f3fc", "#fefbff", "#eee3f5", "#352342", "#6c557c", "#d7c5e3", "#79439c", "#60327e", "#eddef8"], dark: ["#1e1526", "#2d2038", "#3b2b49", "#f1e6fb", "#c3aed7", "#57426b", "#c8a6ed", "#79439c", "#432c59"]},
    {id: "indigo", name: "Indigo", note: "Ink blue and periwinkle", light: ["#f3f4fd", "#fdfdff", "#e5e7f8", "#272a4b", "#595e80", "#c9cde8", "#514eaa", "#3e3b87", "#e4e3fb"], dark: ["#161729", "#23243d", "#30314f", "#e9e9fc", "#b6b8de", "#484c77", "#b4b3ff", "#514eaa", "#333260"]},
    {id: "coffee", name: "Coffee", note: "Cocoa and creamy espresso", light: ["#f7f3ee", "#fffcf8", "#ece3d9", "#382c25", "#6d5f53", "#d5c7b7", "#71523b", "#573e2c", "#ede1d3"], dark: ["#201b18", "#2e2722", "#3c322b", "#f3e9df", "#c5b5a5", "#57483b", "#d6b594", "#71523b", "#433326"]},
    {id: "graphite", name: "Graphite", note: "Charcoal and silver", light: ["#f4f4f5", "#ffffff", "#e7e7ea", "#27272d", "#60606b", "#ceced5", "#535360", "#3e3e48", "#e7e7ed"], dark: ["#17171b", "#24242b", "#31313a", "#ededf2", "#babac7", "#4a4a58", "#c3c3d7", "#535360", "#383844"]},
    {id: "snow", name: "Snow", note: "Clean neutrals and sky blue", light: ["#f9fbff", "#ffffff", "#edf2f8", "#1d2b3d", "#526277", "#ccd5e1", "#225da0", "#16467c", "#e1edfb"], dark: ["#121a25", "#1c2735", "#273448", "#edf3fc", "#b5c3d7", "#3d5069", "#a4caff", "#225da0", "#233d5e"]},
    {id: "sunflower", name: "Sunflower", note: "Sunny yellow and golden highlights", light: ["#fffbe5", "#fffef6", "#fff1b8", "#453915", "#756329", "#e6d48c", "#806400", "#614a00", "#fff0a3"], dark: ["#221f0b", "#312c12", "#40391b", "#fbf2c5", "#d3c388", "#625629", "#f4d75e", "#806400", "#483d16"]},
    {id: "carbon-cyan", name: "Carbon Cyan", note: "Neutral grey with icy cyan text and highlights", light: ["#f4f4f5", "#ffffff", "#e5e7e9", "#264348", "#50696e", "#cbced2", "#126b78", "#0b505b", "#daeff2"], dark: ["#101112", "#1a1c1f", "#272a2e", "#cff2f7", "#9dbac0", "#44484e", "#6dd9e8", "#126b78", "#193337"]},
    {id: "carbon-green", name: "Carbon Green", note: "Neutral grey with soft green text and highlights", light: ["#f4f4f5", "#ffffff", "#e5e7e9", "#38472c", "#5d6d52", "#cbced2", "#416723", "#2f4d18", "#e2efd8"], dark: ["#101112", "#1a1c1f", "#272a2e", "#dcefcf", "#a8bba0", "#44484e", "#a4e878", "#416723", "#283322"]},
    {id: "carbon-amber", name: "Carbon Amber", note: "Neutral grey with warm amber text and highlights", light: ["#f4f4f5", "#ffffff", "#e5e7e9", "#493f23", "#756648", "#cbced2", "#7c5b15", "#5d430d", "#f3e8cf"], dark: ["#101112", "#1a1c1f", "#272a2e", "#f4e5be", "#bfaf89", "#44484e", "#f1ca69", "#7c5b15", "#38301c"]},
    {id: "carbon-violet", name: "Carbon Violet", note: "Neutral grey with violet text and highlights", light: ["#f4f4f5", "#ffffff", "#e5e7e9", "#3c2e51", "#6b5c7c", "#cbced2", "#6543a0", "#4c317b", "#eae0f8"], dark: ["#101112", "#1a1c1f", "#272a2e", "#e8e0f8", "#b5a4c8", "#44484e", "#c3b0ff", "#6543a0", "#30273f"]},
  ];
  const order = [
    "sunflower", "clay", "coffee", "sage", "forest", "lagoon", "ocean", "snow", "slate",
    "indigo", "orchid", "rose", "graphite", "carbon-cyan", "carbon-green", "carbon-amber", "carbon-violet",
  ];
  const root = document.documentElement;
  const device = matchMedia("(prefers-color-scheme: dark)");
  const identity = root.dataset.themeUser || "";
  const key = identity ? `weightroom-theme-v1:${encodeURIComponent(identity)}` : null;
  const legacyKey = identity ? `frontdesk-theme-v1:${encodeURIComponent(identity)}` : null;
  const defaults = {palette: "slate", mode: "system"};
  const properties = ["bg", "surface", "subtle", "text", "muted", "border", "accent", "button", "accent-bg"];
  /** Normalize stored palette and appearance choices against supported values. */
  function valid(value) {
    const theme = palettes.find(theme => theme.id === value?.palette) || palettes.find(theme => theme.id === defaults.palette);
    return {palette: theme.id,
      mode: ["system", "light", "dark"].includes(value?.mode) ? value.mode : defaults.mode};
  }
  /** Read this account's saved theme choices from browser storage. */
  function read() {
    try { return valid(key && JSON.parse(localStorage.getItem(key) ?? localStorage.getItem(legacyKey))); }
    catch { return {...defaults}; }
  }
  let saved = read(), current = {...saved};
  /** Resolve system appearance to the currently effective light or dark mode. */
  function mode() { return current.mode === "system" ? (device.matches ? "dark" : "light") : current.mode; }
  /** Read the selected palette's colors for the effective appearance. */
  function colors(theme) { return theme[mode()]; }
  /** Apply the selected palette and appearance to the document. */
  function apply() {
    current = valid(current);
    const theme = palettes.find(theme => theme.id === current.palette);
    root.dataset.bsTheme = mode();
    root.dataset.siteTheme = theme.id;
    root.style.colorScheme = mode();
    colors(theme).forEach((color, index) => {
      root.style.setProperty(`--theme-${properties[index]}`, color);
      root.style.setProperty(`--theme-${properties[index]}-rgb`, color.slice(1).match(/../g).map(hex => parseInt(hex, 16)).join(", "));
    });
    const button = mode() === "dark" ? theme.light[7] : theme.light[6];
    root.style.setProperty("--theme-button-hover", button);
    render();
    document.dispatchEvent(new CustomEvent("weightroom:theme", {detail: {palette: theme.id, mode: mode()}}));
  }
  /** Display a plain-text theme preference status message. */
  function notice(text) {
    const status = document.getElementById("theme-status");
    if (status) status.textContent = text;
  }
  /** Render supported palette choices and their previews. */
  function render() {
    const form = document.querySelector("[data-theme-form]");
    if (!form) return;
    form.hidden = false;
    const options = form.querySelector("[data-theme-options]");
    if (!options.children.length) {
      for (const id of order) {
        const theme = palettes.find(theme => theme.id === id);
        const label = document.createElement("label");
        label.className = "theme-option";
        label.dataset.palette = theme.id;
        label.innerHTML = `<input type="radio" name="palette" value="${theme.id}" aria-label="${theme.name}"><span class="theme-name">${theme.name}</span><span class="theme-preview" aria-hidden="true"><span class="theme-preview-top"><span></span><span></span><span></span></span><span class="theme-preview-body"><span class="theme-preview-rail"></span><span class="theme-preview-card"><span class="theme-preview-title">Aa</span><span class="theme-preview-lines"></span><span class="theme-preview-button"></span></span></span></span><span class="theme-note">${theme.note}</span>`;
        options.append(label);
      }
    }
    for (const theme of palettes) {
      const label = options.querySelector(`[data-palette="${theme.id}"]`);
      colors(theme).forEach((color, index) => label.style.setProperty(`--preview-${properties[index]}`, color));
      label.querySelector("input").checked = theme.id === current.palette;
    }
    const modeChoice = form.querySelector("[name=mode]");
    modeChoice.value = current.mode;
  }
  /** Persist the selected account-specific browser theme preferences. */
  function save() {
    try {
      if (!key) throw new Error("Sign in to save a theme.");
      localStorage.setItem(key, JSON.stringify(current));
      saved = {...current};
      notice("Theme saved for your account in this browser.");
    } catch { notice("Theme applied for this page. Your browser could not save the preference."); }
  }
  document.addEventListener("change", event => {
    if (!event.target.closest("[data-theme-form]")) return;
    if (event.target.name === "palette") current.palette = valid({palette: event.target.value}).palette;
    if (event.target.name === "mode") current.mode = valid({mode: event.target.value}).mode;
    apply();
    notice("Previewing your selection. Save theme to keep it.");
  });
  document.addEventListener("submit", event => {
    if (!event.target.matches("[data-theme-form]")) return;
    event.preventDefault();
    save();
  });
  document.addEventListener("click", event => {
    if (event.target.closest("[data-theme-cancel]")) {
      current = {...saved}; apply(); notice("Preview reset to your saved theme.");
    }
    if (event.target.closest("[data-theme-default]")) {
      current = {...defaults}; apply(); save();
    }
  });
  device.addEventListener("change", () => { if (current.mode === "system") apply(); });
  window.addEventListener("storage", event => { if (key && event.key === key) { saved = read(); current = {...saved}; apply(); } });
  document.addEventListener("DOMContentLoaded", render);
  document.addEventListener("htmx:load", render);
  apply();
})();
