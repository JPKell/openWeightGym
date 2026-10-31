"""Optional real-Tk key lifecycle check; run under Xvfb when no display is available."""

import pytest

from voice.desktop import App, read_key


def test_key_autosave_restart_clear_and_close(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk needs a display (run with xvfb-run)")
    monkeypatch.setattr(App, "refresh_devices", lambda self: None)
    errors = []
    root.report_callback_exception = lambda *error: errors.append(error)
    settings = tmp_path / "voice-desktop.json"

    def pump(milliseconds=50):
        root.after(milliseconds, root.quit)
        root.mainloop()

    try:
        app = App(root, settings_path=settings)
        assert app.notebook.select() == str(app.settings_tab)
        app.key.set("first-test-key")
        pump(700)
        assert read_key(app.key_path) == "first-test-key"
        app.key.set("invalid key")
        pump(700)
        assert "not saved" in app.key_notice.get()
        assert read_key(app.key_path) == "first-test-key"
        app.key.set("close-test-key")
        app.close()  # close before the debounce fires still flushes the new key
        pump()
        assert read_key(app.key_path) == "close-test-key"

        root = tk.Tk()
        root.report_callback_exception = lambda *error: errors.append(error)
        app = App(root, settings_path=settings)
        assert app.key.get() == "close-test-key"
        assert app.notebook.select() == str(app.conversation_tab)
        assert app.inputs[1].cget("show") == "*"
        app.key.set("")
        pump(700)
        assert not app.key_path.exists()
        app.close()
        pump()
        assert not app.key_path.exists()
        assert not errors
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
