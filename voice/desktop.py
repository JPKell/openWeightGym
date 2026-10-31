"""A small Tk voice client for Linux, Windows and macOS, authenticated by one API key.

Tk stays on the main thread. One worker owns the network loop and sound devices. The API key
lives in a separate local file; preferences contain no key, transcript or audio.
"""

import argparse
import asyncio
from contextlib import suppress
import json
import os
from pathlib import Path
import queue
import sys
import threading
import tempfile
from urllib.parse import urlsplit, urlunsplit, urlencode

from websockets.asyncio.client import connect

from voice import protocol
from voice.desktop_audio import Audio

DEFAULT_SERVER = "http://10.77.10.42:8447"
COLORS = dict(background="#141414", panel="#1c1c1c", field="#111111", border="#303030",
              text="#eeeae2", gold="#d4b474", active="#352e21", muted="#b7ad98", disabled="#777166")


def theme(root):
    """Use drawn ttk controls so native light borders cannot override the dark palette."""
    import tkinter as tk
    from tkinter import ttk
    c = COLORS
    root.configure(background=c["background"])
    # Classic widgets and the combobox popdown use Tk's option database, not ttk styles.
    for name, value in dict(background=c["panel"], foreground=c["text"],
                            activeBackground=c["active"], activeForeground=c["gold"],
                            selectBackground=c["active"], selectForeground=c["gold"],
                            highlightBackground=c["border"], highlightColor=c["border"],
                            troughColor=c["field"]).items():
        root.option_add("*" + name, value)
    for name, value in dict(background=c["field"], foreground=c["text"],
                            selectBackground=c["active"], selectForeground=c["gold"],
                            highlightThickness=0, borderWidth=0).items():
        root.option_add("*TCombobox*Listbox." + name, value)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=c["panel"], foreground=c["text"], fieldbackground=c["field"],
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"],
                    troughcolor=c["field"], selectbackground=c["active"], selectforeground=c["gold"],
                    focuscolor=c["border"], font=("TkDefaultFont", 10))
    style.map(".", foreground=[("disabled", c["disabled"])])
    style.configure("Shell.TFrame", background=c["background"])
    style.configure("Gold.TLabel", foreground=c["gold"], font=("TkDefaultFont", 12, "bold"))
    style.configure("Muted.TLabel", foreground=c["muted"])
    style.configure("TButton", padding=(6, 3), width=0, foreground=c["gold"], relief="flat")
    style.layout("TButton", [("Button.border", {"sticky": "nswe", "children": [
        ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})]})])
    style.map("TButton", background=[("disabled", c["panel"]), ("pressed", c["active"]), ("active", c["active"])],
              foreground=[("disabled", c["disabled"]), ("focus", c["text"])])
    for control in ("TEntry", "TCombobox"):
        style.configure(control, padding=3, insertcolor=c["gold"], arrowcolor=c["gold"])
        style.map(control, fieldbackground=[("disabled", c["panel"]), ("readonly", c["field"])],
                  foreground=[("disabled", c["disabled"]), ("readonly", c["text"])],
                  background=[("active", c["active"]), ("readonly", c["panel"])],
                  bordercolor=[("focus", c["border"])], lightcolor=[("focus", c["border"])],
                  darkcolor=[("focus", c["border"])], arrowcolor=[("disabled", c["disabled"]), ("active", c["gold"])])
    style.configure("ComboboxPopdownFrame", bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"])
    style.configure("TNotebook", background=c["background"], borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", padding=(9, 4), background=c["panel"],
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"])
    style.map("TNotebook.Tab", background=[("selected", c["active"]), ("active", c["panel"])],
              foreground=[("selected", c["gold"]), ("active", c["gold"])],
              padding=[("selected", (9, 4)), ("!selected", (9, 4))],
              lightcolor=[("selected", c["border"]), ("!selected", c["border"])])
    style.layout("TNotebook.Tab", [("Notebook.tab", {"sticky": "nswe", "children": [
        ("Notebook.padding", {"sticky": "nswe", "children": [("Notebook.label", {"sticky": "nswe"})]})]})])
    style.configure("Vertical.TScrollbar", background=c["border"], arrowcolor=c["gold"], borderwidth=0)
    style.map("Vertical.TScrollbar", background=[("active", c["active"]), ("pressed", c["active"])])
    images = []
    for selected in (False, True):
        icon = tk.PhotoImage(master=root, width=18, height=18)
        icon.put(c["border"], to=(0, 0, 18, 18))
        icon.put(c["field"], to=(1, 1, 17, 17))
        if selected:
            for x, y in ((4, 9), (5, 10), (6, 11), (7, 12), (8, 11), (9, 10), (10, 9), (11, 8), (12, 7), (13, 6)):
                icon.put(c["gold"], to=(x, y, x + 2, y + 2))
        images.append(icon)
    root._voice_check_images = images
    style.element_create("Voice.indicator", "image", images[0], ("selected", images[1]))
    style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [
        ("Voice.indicator", {"side": "left", "sticky": ""}),
        ("Checkbutton.label", {"side": "left", "sticky": "nswe"})]})])
    style.configure("TCheckbutton", padding=(2, 3))
    style.map("TCheckbutton", background=[("active", c["panel"])], foreground=[("disabled", c["disabled"]), ("focus", c["gold"]), ("active", c["gold"])])


def validate_key(key):
    """Validate the same printable, bounded token for disk storage and the HTTP header."""
    if not key or len(key) > 512 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError("Enter a valid LoadCoach API key without spaces.")
    return key


def read_key(path):
    """Load a bounded key file; a missing file means this desktop has not been configured."""
    try:
        with path.open(encoding="utf-8") as file:
            return validate_key(file.read(514).strip())
    except FileNotFoundError:
        return ""


def save_key(path, key):
    """Atomically replace the local key with an owner-only temporary file; empty removes it."""
    key = key.strip()
    if not key:
        path.unlink(missing_ok=True)
        return
    validate_key(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", delete=False) as file:
            temporary = Path(file.name)
            file.write(key + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def endpoint(server, mode):
    """Keep credentials out of URLs and connect only to the dedicated native route."""
    parsed = urlsplit(server.strip())
    if (parsed.scheme not in ("http", "https", "ws", "wss") or not parsed.hostname or
            parsed.username is not None or parsed.password is not None or parsed.query or
            parsed.fragment or parsed.path not in ("", "/") or mode not in protocol.MODES):
        raise ValueError("Enter a server address such as http://10.77.10.42:8447, without a path or key.")
    scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
    return urlunsplit((scheme, parsed.netloc, "/desktop/stream", urlencode({"conversation_mode": mode}), ""))


def preferences_path():
    """Use each desktop's normal per-user application settings folder."""
    if sys.platform == "win32":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif sys.platform == "darwin":
        root = Path.home() / "Library/Application Support"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "loadcoach/voice-desktop.json"


def save_preferences(path, values):
    """Persist only explicitly allowed non-secret fields, using an atomic replacement."""
    safe = {name: values[name] for name in ("server", "mode", "input", "output", "echo_cancellation") if name in values}
    endpoint(safe.get("server", DEFAULT_SERVER), safe.get("mode", "direct"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(safe, indent=2), encoding="utf-8")
    temporary.replace(path)


async def conversation(server, key, mode, audio, emit, stop):
    """Run one API-key call, never replaying a turn or reconnecting automatically."""
    uri = endpoint(server, mode)
    validate_key(key)
    try:
        async with connect(uri, additional_headers={"authorization": "Bearer " + key},
                           proxy=None, open_timeout=10, close_timeout=3,
                           max_size=16384, max_queue=4, write_limit=4096) as socket:
            await asyncio.to_thread(audio.open)

            async def write(value):
                """Send one serialized voice control message over the active WebSocket."""
                async with asyncio.timeout(2):
                    await socket.send(value)

            async def send():
                """Forward ordered microphone audio frames to the voice gateway."""
                while not stop.is_set():
                    if audio.problem:
                        raise ValueError(audio.problem)
                    for report in audio.reports():
                        await write(json.dumps(report))
                    for source in (audio.controls, audio.incoming):
                        for _ in range(25):
                            try:
                                value = source.get_nowait()
                            except queue.Empty:
                                break
                            await write(value if isinstance(value, bytes) else json.dumps(value))
                    await asyncio.sleep(.01)
                await write(json.dumps(dict(type="session.cancel")))

            async def receive():
                """Receive voice events and route PCM to the playback queue."""
                async for value in socket:
                    if isinstance(value, bytes):
                        audio.receive(value)
                    else:
                        event = protocol.message(value)
                        audio.event(event)
                        emit(event)

            sending = asyncio.create_task(send())
            receiving = asyncio.create_task(receive())
            try:
                done, _ = await asyncio.wait({sending, receiving}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
            finally:
                sending.cancel()
                receiving.cancel()
                await asyncio.gather(sending, receiving, return_exceptions=True)
    finally:
        await asyncio.to_thread(audio.close)


class App:
    """The desktop window; its worker communicates only through a bounded event queue."""

    def __init__(self, root, audio_factory=Audio, settings_path=None):
        """Create desktop voice controls, saved settings and conversation state."""
        import tkinter as tk
        from tkinter import ttk

        self.root, self.audio_factory = root, audio_factory
        self.settings_path = settings_path or preferences_path()
        self.key_path = self.settings_path.with_name("voice-desktop.key")
        self.key_save_task = None
        self.events = queue.Queue(maxsize=256)
        self.worker = self.audio = None
        self.stop = threading.Event()
        self.closing = False
        try:
            settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if not isinstance(settings, dict):
                settings = {}
        except (OSError, ValueError):
            settings = {}
        key_error = ""
        try:
            stored_key = read_key(self.key_path)
        except (OSError, ValueError):
            stored_key = ""
            key_error = "The saved key could not be read. Enter it again to replace the file."
        theme(root)
        root.title("openWeightGym Voice")
        root.geometry("580x420")
        root.minsize(480, 280)
        self.server = tk.StringVar(value=settings.get("server", DEFAULT_SERVER))
        self.key = tk.StringVar(value=stored_key)
        self.mode = tk.StringVar(value=settings.get("mode", "direct"))
        self.status = tk.StringVar(value="Disconnected")
        self.notice = tk.StringVar()
        self.key_notice = tk.StringVar(value=key_error or ("Saved key loaded." if stored_key else "Enter an API key; it saves automatically."))
        self.settings_notice = tk.StringVar()
        self.muted = tk.BooleanVar()
        self.echo_cancellation = tk.BooleanVar(value=settings.get("echo_cancellation", True) is not False)
        self.input_choice = tk.StringVar(value=settings.get("input", "System default"))
        self.output_choice = tk.StringVar(value=settings.get("output", "System default"))
        shell = ttk.Frame(root, padding=4, style="Shell.TFrame")
        shell.pack(fill="both", expand=True)
        self.notebook = ttk.Notebook(shell)
        self.notebook.pack(fill="both", expand=True)
        self.conversation_tab = ttk.Frame(self.notebook, padding=6)
        self.settings_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.conversation_tab, text="Conversation")
        self.notebook.add(self.settings_tab, text="Settings")
        self.notebook.enable_traversal()

        talk = self.conversation_tab
        talk.columnconfigure(0, weight=1)
        talk.rowconfigure(3, weight=1)
        ttk.Label(talk, textvariable=self.status, style="Gold.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 2))
        notice = ttk.Label(talk, textvariable=self.notice, wraplength=660, style="Muted.TLabel")
        notice.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        notice.grid_remove()
        self.notice.trace_add("write", lambda *_: notice.grid() if self.notice.get() else notice.grid_remove())
        talk.bind("<Configure>", lambda event: notice.configure(wraplength=max(200, event.width - 12)))
        buttons = ttk.Frame(talk)
        buttons.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        self.start_button = ttk.Button(buttons, text="Start", command=self.start)
        self.start_button.pack(side="left", padx=(0, 3))
        self.hangup = ttk.Button(buttons, text="Hang up", command=self.stop.set, state="disabled")
        self.hangup.pack(side="left", padx=(0, 3))
        self.interrupt = ttk.Button(buttons, text="Interrupt", command=lambda: self.control("session.cancel", interrupt=True), state="disabled")
        self.interrupt.pack(side="left", padx=(0, 3))
        self.finish = ttk.Button(buttons, text="Finish speaking", command=lambda: self.control("input.end"), state="disabled")
        self.finish.pack(side="left", padx=(0, 3))
        self.mute = ttk.Checkbutton(buttons, text="Mute", variable=self.muted, command=self.set_mute, state="disabled")
        self.mute.pack(side="left")
        history = ttk.Frame(talk)
        history.grid(row=3, column=0, sticky="nsew")
        history.columnconfigure(0, weight=1)
        history.rowconfigure(0, weight=1)
        self.transcript = tk.Text(history, wrap="word", height=12, state="disabled", font=("TkDefaultFont", 11),
            background=COLORS["field"], foreground=COLORS["text"], insertbackground=COLORS["gold"],
            selectbackground=COLORS["active"], selectforeground=COLORS["text"],
            inactiveselectbackground=COLORS["active"], relief="flat", borderwidth=0,
            highlightthickness=1, highlightbackground=COLORS["border"], highlightcolor=COLORS["border"],
            padx=6, pady=5, spacing1=1, spacing3=2)
        self.transcript.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(history, orient="vertical", command=self.transcript.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.transcript.configure(yscrollcommand=scroll.set)
        self.transcript.tag_configure("speaker", foreground=COLORS["gold"], font=("TkDefaultFont", 11, "bold"))

        # Keep settings reachable on small screens and with larger system fonts.
        canvas = tk.Canvas(self.settings_tab, background=COLORS["panel"], borderwidth=0, highlightthickness=0)
        settings_scroll = ttk.Scrollbar(self.settings_tab, orient="vertical", command=canvas.yview)
        settings_scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        canvas.configure(yscrollcommand=settings_scroll.set)
        settings_frame = ttk.Frame(canvas, padding=6)
        settings_window = canvas.create_window(0, 0, anchor="nw", window=settings_frame)
        settings_frame.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        def resize_settings(event):
            """Keep the settings panel's scroll region aligned with its contents."""
            canvas.itemconfigure(settings_window, width=event.width)
            for widget in settings_frame.winfo_children():
                if isinstance(widget, ttk.Label) and int(widget.cget("wraplength") or 0) > 0:
                    widget.configure(wraplength=max(160, event.width - 120))
        canvas.bind("<Configure>", resize_settings)
        settings_frame.columnconfigure(1, weight=1)
        self.inputs = []
        ttk.Label(settings_frame, text="Connection", style="Gold.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        for row, label, variable in ((1, "Server", self.server), (2, "API key", self.key)):
            ttk.Label(settings_frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            field = ttk.Entry(settings_frame, textvariable=variable, show="*" if row == 2 else "")
            field.grid(row=row, column=1, sticky="ew", pady=3)
            self.inputs.append(field)
            if row == 2:
                field.bind("<FocusOut>", lambda _: self.persist_key())
                field.bind("<Return>", lambda _: self.persist_key())
        ttk.Label(settings_frame, textvariable=self.key_notice, style="Muted.TLabel", wraplength=500).grid(row=3, column=1, sticky="w", pady=(0, 6))
        self.load_key = ttk.Button(settings_frame, text="Import key file…", command=self.choose_key)
        self.load_key.grid(row=4, column=1, sticky="w", pady=(0, 4))
        ttk.Label(settings_frame, text=str(self.key_path), style="Muted.TLabel", wraplength=500).grid(row=5, column=1, sticky="w", pady=(0, 4))
        ttk.Label(settings_frame, text="Mode").grid(row=6, column=0, sticky="w", padx=(0, 8), pady=3)
        mode = ttk.Combobox(settings_frame, textvariable=self.mode, values=["direct", "hermes"], state="readonly")
        mode.grid(row=6, column=1, sticky="ew", pady=3)
        self.inputs.append(mode)
        ttk.Label(settings_frame, text="Audio", style="Gold.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=(6, 3))
        self.devices = {"System default": None}
        self.device_boxes = []
        for row, label, variable in ((8, "Microphone", self.input_choice), (9, "Speakers", self.output_choice)):
            ttk.Label(settings_frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=3)
            box = ttk.Combobox(settings_frame, textvariable=variable, values=["System default"], state="readonly")
            box.grid(row=row, column=1, sticky="ew", pady=3)
            self.device_boxes.append(box)
            self.inputs.append(box)
        audio_controls = ttk.Frame(settings_frame)
        audio_controls.grid(row=10, column=1, sticky="ew", pady=(3, 4))
        self.refresh = ttk.Button(audio_controls, text="Refresh devices", command=self.refresh_devices)
        self.refresh.pack(side="left", padx=(0, 6))
        self.echo_option = ttk.Checkbutton(audio_controls, text="Echo cancellation",
            variable=self.echo_cancellation, command=self.set_echo)
        self.echo_option.pack(side="left")
        ttk.Button(settings_frame, text="Save settings", command=self.save_settings).grid(row=11, column=1, sticky="w", pady=(3, 0))
        ttk.Label(settings_frame, textvariable=self.settings_notice, style="Muted.TLabel", wraplength=500).grid(row=12, column=1, sticky="w", pady=(3, 0))
        def scroll_settings(event):
            """Scroll the settings panel in response to a wheel event."""
            if settings_frame.winfo_height() > canvas.winfo_height():
                direction = -1 if event.num == 4 or event.delta > 0 else 1
                canvas.yview_scroll(direction * 3, "units")
            return "break"

        def reveal_setting(event):
            """Scroll the focused setting into the visible settings area."""
            top = event.widget.winfo_rooty() - settings_frame.winfo_rooty()
            bottom = top + event.widget.winfo_height()
            visible = canvas.canvasy(0)
            if top < visible:
                canvas.yview_moveto(max(0, top - 12) / settings_frame.winfo_height())
            elif bottom > visible + canvas.winfo_height():
                canvas.yview_moveto((bottom + 12 - canvas.winfo_height()) / settings_frame.winfo_height())

        def bind_scroll(widget):
            """Attach wheel-scrolling handlers to settings widgets."""
            widget.bind("<FocusIn>", reveal_setting)
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(sequence, scroll_settings)
            for child in widget.winfo_children():
                bind_scroll(child)
        bind_scroll(canvas)
        if not stored_key:
            self.notebook.select(self.settings_tab)
        self.key.trace_add("write", self.schedule_key_save)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_devices()
        root.after(25, self.poll)

    def schedule_key_save(self, *_):
        """Save shortly after typing stops, without rewriting the file on each keystroke."""
        if self.closing:
            return
        if self.key_save_task is not None:
            self.root.after_cancel(self.key_save_task)
        self.key_notice.set("Saving key…")
        self.key_save_task = self.root.after(600, self.persist_key)

    def persist_key(self):
        """Flush a pending edit, keeping write failures visible beside the masked entry."""
        if self.key_save_task is not None:
            self.root.after_cancel(self.key_save_task)
            self.key_save_task = None
        try:
            save_key(self.key_path, self.key.get())
        except (OSError, ValueError):
            self.key_notice.set("Key not saved. Check the key and that its folder is writable.")
            return False
        self.key_notice.set("Key saved on this computer." if self.key.get().strip() else "No saved key. Enter one to connect.")
        return True

    def save_settings(self):
        """Save connection/audio preferences and the key to their separate local files."""
        try:
            save_preferences(self.settings_path, dict(server=self.server.get().strip(), mode=self.mode.get(),
                input=self.input_choice.get(), output=self.output_choice.get(),
                echo_cancellation=self.echo_cancellation.get()))
        except (ValueError, OSError) as error:
            self.settings_notice.set(str(error))
            return False
        if not self.persist_key():
            return False
        self.settings_notice.set("Settings saved.")
        return True

    def choose_key(self):
        """Import an existing file using a themed path dialog, then save the app's own copy."""
        import tkinter as tk
        from tkinter import ttk
        dialog = tk.Toplevel(self.root, background=COLORS["panel"])
        dialog.title("Import API key")
        dialog.transient(self.root)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=8)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Path to your existing API key file", foreground=COLORS["gold"]).pack(anchor="w", pady=(0, 10))
        path = tk.StringVar()
        field = ttk.Entry(frame, textvariable=path, width=50)
        field.pack(fill="x", pady=(0, 10))
        message = tk.StringVar()
        ttk.Label(frame, textvariable=message, wraplength=440).pack(anchor="w")

        def load():
            """Read the selected private API key file into the desktop settings."""
            try:
                key = read_key(Path(os.path.expandvars(path.get().strip())).expanduser())
                validate_key(key)
            except (OSError, ValueError):
                message.set("That file could not be read as an API key.")
                return
            self.key.set(key)
            if self.persist_key():
                dialog.destroy()
            else:
                message.set(self.key_notice.get())

        ttk.Button(frame, text="Import", command=load).pack(anchor="e", pady=(10, 0))
        field.bind("<Return>", lambda _: load())
        dialog.bind("<Escape>", lambda _: dialog.destroy())
        dialog.grab_set()
        field.focus_set()

    def refresh_devices(self):
        """List the current sound devices, keeping selection independent of changing indices."""
        try:
            import sounddevice as sd
            choices = [["System default"], ["System default"]]
            self.devices = {"System default": None}
            hosts = sd.query_hostapis()
            for index, device in enumerate(sd.query_devices()):
                label = device["name"] + " (" + hosts[device["hostapi"]]["name"] + ")"
                self.devices[label] = index
                for side, direction in enumerate(("input", "output")):
                    if device["max_" + direction + "_channels"]:
                        choices[side].append(label)
            for box, variable, values in zip(self.device_boxes, (self.input_choice, self.output_choice), choices):
                box.configure(values=values)
                if variable.get() not in values:
                    variable.set("System default")
        except Exception:
            self.notice.set("Audio devices are unavailable. Install sounddevice and PortAudio, then refresh devices.")

    def start(self):
        """Snapshot Tk values before starting the worker; it never reads a Tk variable."""
        if self.worker and self.worker.is_alive():
            return
        server, key, mode = self.server.get().strip(), self.key.get().strip(), self.mode.get()
        try:
            endpoint(server, mode)
            validate_key(key)
        except ValueError as error:
            self.settings_notice.set(str(error))
            self.notebook.select(self.settings_tab)
            return
        if not self.save_settings():
            self.notebook.select(self.settings_tab)
            return
        self.notebook.select(self.conversation_tab)
        self.stop.clear()
        self.audio = self.audio_factory(self.devices.get(self.input_choice.get()), self.devices.get(self.output_choice.get()))
        self.set_echo()
        self.muted.set(False)
        self.status.set("Connecting")
        self.notice.set("")
        self.active(True)

        def work():
            """Run the voice connection outside the Tkinter event loop."""
            try:
                asyncio.run(conversation(server, key, mode, self.audio, self.events.put_nowait, self.stop))
            except Exception as error:
                if not self.stop.is_set():
                    message = str(error).replace(key, "[key]") if isinstance(error, ValueError) else (
                        "Connection or audio device failed (" + type(error).__name__ + "). Check the server, key and selected devices.")
                    with suppress(queue.Full):
                        self.events.put_nowait(dict(type="client.error", message=message))
            finally:
                with suppress(queue.Full):
                    self.events.put_nowait(dict(type="client.closed"))

        self.worker = threading.Thread(target=work, name="voice-desktop", daemon=True)
        self.worker.start()

    def active(self, on):
        """Update desktop controls to reflect whether a conversation is active."""
        from tkinter import ttk
        for widget in self.inputs:
            widget.configure(state="disabled" if on else "readonly" if isinstance(widget, ttk.Combobox) else "normal")
        for widget in (self.start_button, self.refresh, self.load_key):
            widget.configure(state="disabled" if on else "normal")
        self.hangup.configure(state="normal" if on else "disabled")
        if not on:
            for widget in (self.mute, self.finish, self.interrupt):
                widget.configure(state="disabled")

    def set_mute(self):
        """Apply microphone mute state and update the visible control."""
        if self.audio:
            self.audio.muted.set() if self.muted.get() else self.audio.muted.clear()

    def set_echo(self):
        """Allow echo processing to be disabled for a headset without changing voice interruption."""
        if self.audio:
            self.audio.echo_enabled.set() if self.echo_cancellation.get() else self.audio.echo_enabled.clear()

    def control(self, kind, **values):
        """Send the selected desktop voice control command."""
        if self.audio:
            self.audio.control(kind, **values)

    def line(self, who, text):
        """Append a status message to the desktop activity display."""
        self.transcript.configure(state="normal")
        self.transcript.insert("end", who.upper() + ": ", "speaker")
        self.transcript.insert("end", text + "\n")
        # The visible transcript is bounded too; remote conversation limits are independent.
        if int(self.transcript.index("end-1c").split(".")[0]) > 600:
            self.transcript.delete("1.0", "101.0")
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def poll(self):
        """Apply worker events on the Tk thread and finish shutdown after device cleanup."""
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event["type"]
            if kind == "session.ready":
                self.status.set("Listening")
                self.set_echo()
                for widget in (self.mute, self.finish, self.interrupt):
                    widget.configure(state="normal")
            elif kind == "session.state":
                self.status.set(event["state"].replace("_", " ").capitalize())
            elif kind == "transcript.final":
                self.line("You", event["text"])
            elif kind == "playback.segment":
                self.status.set("Speaking")
                if event.get("text"):
                    self.line("Assistant", event["text"])
            elif kind == "turn.started" and event["turn"] >= 0:
                self.status.set("Thinking")
            elif kind in ("playback.clear", "turn.completed") and self.audio and self.audio.capture.is_set():
                self.status.set("Listening")
            elif kind in ("session.error", "client.error"):
                self.notice.set(event.get("message") or {
                    "unauthorized": "The key was refused or lacks permission for this voice mode.",
                    "busy": "Voice is busy. Try again after the current call ends.",
                }.get(event.get("code"), "Voice is unavailable. Check compute and the server address. A new key must first be used while compute is awake."))
            elif kind == "client.closed":
                self.active(False)
                self.status.set("Disconnected")
        if self.worker and not self.worker.is_alive() and str(self.start_button["state"]) == "disabled":
            self.active(False)
            self.status.set("Disconnected")
        if self.closing and (not self.worker or not self.worker.is_alive()):
            self.key.set("")
            self.root.destroy()
        else:
            self.root.after(25, self.poll)

    def close(self):
        """Stop the conversation and close desktop audio and window resources."""
        self.persist_key()
        self.closing = True
        self.stop.set()
        self.status.set("Closing…")


def main():
    """Parse desktop options and launch the voice client."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", help="Gateway address; defaults to the saved address or " + DEFAULT_SERVER)
    args = parser.parse_args()
    try:
        import tkinter as tk
    except ImportError:
        raise SystemExit("Tk is missing. On Linux install python3-tk. On Windows/macOS use Python from python.org.")
    root = tk.Tk()
    app = App(root)
    if args.server:
        app.server.set(args.server)
    root.mainloop()


if __name__ == "__main__":
    main()
