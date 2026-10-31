(() => {
  const input = document.getElementById('case-file');
  if (!input) return;
  input.onchange = async () => {
    const file = input.files[0];
    const status = document.getElementById('case-import-status');
    if (!file) return;
    if (file.size > 1024 * 1024) {
      status.textContent = 'This file is larger than 1 MiB.';
      input.value = '';
      return;
    }
    try {
      document.getElementById('case-dataset').value = await file.text();
      status.textContent = `${file.name} loaded for import. The server validates it when submitted.`;
    } catch (_) {
      status.textContent = 'The file could not be read. Try again or paste its JSON.';
    }
  };
})();
