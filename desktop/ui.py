"""The launcher window.

The only module here that imports tkinter. Everything with a decision in it -
which state to show, how many log lines to keep, when the server is really
stopped - lives in the sibling modules so it can be tested without a display;
this file is the paint job and the event wiring.
"""

from __future__ import annotations

import logging
import os
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

from .logs import LogRing
from .paths import asset, log_dir
from .server import STOP_TIMEOUT_S
from .state import ServerPhase, derive_state

log = logging.getLogger("lapscope.desktop")

TICK_MS = 100
# Status is recomputed every second, but a phase change repaints immediately -
# pressing Stop has to look like it did something. One second is fine for the
# rest because the staleness threshold it has to resolve is 2.5 s.
STATUS_EVERY = 10

# app/static/css/style.css, so the window and the dashboard are the same product.
BG = "#06080c"
PANEL = "#0b0f15"
EDGE = "#1d2634"
TEXT = "#e8eef6"
MUTED = "#8494a7"
ACCENT = "#00d4ff"
TONE = {"ok": "#2fe6a8", "warn": "#ffbe3d", "error": "#ff5d5d", "neutral": "#8494a7"}


class LauncherWindow:
    def __init__(self, controller, sink, log_warning: str | None = None) -> None:
        self.ctl = controller
        self.sink = sink
        self.ring = LogRing()
        self._ticks = 0
        self._last_phase: ServerPhase | None = None
        self._closing = False
        self._asking = False
        self._opened_once = False
        self._after_id: str | None = None

        self.root = tk.Tk()
        self.root.title("LapScope")
        self.root.geometry("760x540")
        self.root.minsize(560, 380)
        self.root.configure(bg=BG)
        # tkinter's default handler prints to sys.stderr, which is None in a
        # windowed build - the traceback would raise inside the Tcl callback.
        self.root.report_callback_exception = self._on_tk_error
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._set_icon()
        self._style()
        self._build()
        self._bind_keys()

        if log_warning:
            log.warning(log_warning)

    # ---- chrome -------------------------------------------------------------

    def _set_icon(self) -> None:
        try:
            # default=: also covers the message boxes we raise later.
            self.root.iconbitmap(default=str(asset("lapscope.ico")))
        except tk.TclError:
            pass  # X11 wants XBM; a source run on Linux simply gets no icon

    def _style(self) -> None:
        st = ttk.Style(self.root)
        # clam honours background/foreground configuration; the native Windows
        # themes ignore most of it and would leave grey widgets on a dark panel.
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("TFrame", background=BG)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        st.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 9))
        st.configure("Pill.TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 14, "bold"))
        st.configure("Dot.TLabel", background=BG, foreground=MUTED, font=("Segoe UI", 15))
        st.configure("Warn.TLabel", background="#2a1d12", foreground="#ffbe3d",
                     font=("Segoe UI", 9))
        st.configure("TButton", background="#18202c", foreground=TEXT, bordercolor=EDGE,
                     focuscolor=ACCENT, font=("Segoe UI", 9), padding=(10, 6))
        st.map("TButton",
               background=[("active", "#222d3d"), ("pressed", "#0f151d"), ("disabled", "#0d1219")],
               foreground=[("disabled", "#4d5a6b")])
        st.configure("Link.TButton", padding=(6, 2), font=("Segoe UI", 8))
        st.configure("TEntry", fieldbackground=PANEL, foreground=MUTED, bordercolor=EDGE,
                     insertcolor=TEXT)
        st.map("TEntry", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", ACCENT)])

    def _build(self) -> None:
        r = self.root
        r.columnconfigure(0, weight=1)
        pad = {"padx": 16}

        head = ttk.Frame(r)
        head.grid(row=0, column=0, sticky="ew", pady=(14, 0), **pad)
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="LapScope", font=("Segoe UI", 11, "bold")).grid(row=0, column=0,
                                                                            sticky="w")
        ttk.Label(head, text=f"v{self.ctl.version}", style="Muted.TLabel").grid(row=0, column=1,
                                                                               sticky="e")

        pill = ttk.Frame(r)
        pill.grid(row=1, column=0, sticky="ew", pady=(14, 0), **pad)
        self.dot = ttk.Label(pill, text="●", style="Dot.TLabel")
        self.dot.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.label = ttk.Label(pill, text="Starting…", style="Pill.TLabel")
        self.label.grid(row=0, column=1, sticky="w")

        self.detail = ttk.Label(r, text="", style="Muted.TLabel", wraplength=700, justify="left")
        self.detail.grid(row=2, column=0, sticky="ew", pady=(4, 0), **pad)

        # Port conflict lives inline rather than in a modal: a modal on a
        # double-clicked exe is a dead end, a row with a Retry button is not.
        self.conflict = ttk.Frame(r, style="Panel.TFrame")
        self.conflict.columnconfigure(0, weight=1)
        self.conflict_msg = ttk.Label(self.conflict, text="", style="Warn.TLabel",
                                      wraplength=520, justify="left")
        self.conflict_msg.grid(row=0, column=0, sticky="w", padx=10, pady=8)
        ttk.Button(self.conflict, text="Open dashboard", style="Link.TButton",
                   command=self._open_dashboard).grid(row=0, column=1, padx=(0, 6), pady=6)
        ttk.Button(self.conflict, text="Retry", style="Link.TButton",
                   command=self._try_start).grid(row=0, column=2, padx=(0, 10), pady=6)

        urlrow = ttk.Frame(r)
        urlrow.grid(row=4, column=0, sticky="ew", pady=(14, 0), **pad)
        urlrow.columnconfigure(1, weight=1)
        ttk.Label(urlrow, text="Dashboard", style="Muted.TLabel").grid(row=0, column=0,
                                                                      padx=(0, 8))
        self.url = ttk.Entry(urlrow)
        self.url.insert(0, self.ctl.url)
        # readonly, not disabled: it stays focusable and Ctrl+C still copies.
        self.url.configure(state="readonly")
        self.url.selection_clear()
        self.url.grid(row=0, column=1, sticky="ew")

        btns = ttk.Frame(r)
        btns.grid(row=5, column=0, sticky="ew", pady=(12, 0), **pad)
        self.b_open = ttk.Button(btns, text="Open Dashboard", underline=0,
                                 command=self._open_dashboard)
        self.b_run = ttk.Button(btns, text="Stop", underline=0, command=self._toggle)
        self.b_restart = ttk.Button(btns, text="Restart", underline=0, command=self._restart)
        self.b_data = ttk.Button(btns, text="Open Data Folder", underline=5,
                                 command=self._open_data)  # underline 5 = the D of Data
        for i, b in enumerate((self.b_open, self.b_run, self.b_restart, self.b_data)):
            b.grid(row=0, column=i, padx=(0, 8))
        # Otherwise focus lands on the URL field, which paints itself as a
        # selected blue block on open. The primary action is a better first
        # Tab stop anyway.
        self.b_open.focus_set()

        loghead = ttk.Frame(r)
        loghead.grid(row=6, column=0, sticky="ew", pady=(18, 4), **pad)
        loghead.columnconfigure(0, weight=1)
        ttk.Label(loghead, text="Log", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(loghead, text="Copy", style="Link.TButton",
                   command=self._copy_log).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(loghead, text="Open log folder", style="Link.TButton",
                   command=self._open_log_file).grid(row=0, column=2)

        wrap = ttk.Frame(r)
        wrap.grid(row=7, column=0, sticky="nsew", pady=(0, 14), **pad)
        r.rowconfigure(7, weight=1)
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        # takefocus so the pane can be reached by Tab and scrolled with the
        # arrow keys even though it is read-only.
        # Wrapped, not clipped: the end of a log line is the part worth reading,
        # and a horizontal scrollbar hides it behind a gesture nobody makes.
        # Wrapping is display-only, so the ring's line arithmetic is unaffected.
        self.log = tk.Text(wrap, state="disabled", wrap="word", relief="flat", takefocus=True,
                           bg=PANEL, fg=MUTED, insertbackground=TEXT, selectbackground=ACCENT,
                           selectforeground="#001018", highlightthickness=1,
                           highlightbackground=EDGE, highlightcolor=ACCENT,
                           font=("Consolas", 9), padx=8, pady=6)
        self.log.grid(row=0, column=0, sticky="nsew")
        bar = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=bar.set)

    def _bind_keys(self) -> None:
        # underline= draws the mnemonic but Windows Tk does not bind it, so the
        # Alt keys are wired by hand.
        def accel(fn):
            def handler(_event, f=fn):
                f()
                return "break"  # don't let Tk also insert the key somewhere
            return handler

        for seq, fn in (
            ("<Control-o>", self._open_dashboard), ("<Alt-o>", self._open_dashboard),
            ("<Control-s>", self._toggle), ("<Alt-s>", self._toggle),
            ("<Control-r>", self._restart), ("<Alt-r>", self._restart),
            ("<Alt-d>", self._open_data),
            ("<Control-l>", self.log.focus_set),
            ("<Control-Shift-C>", self._copy_log),
            ("<Control-w>", self._on_close),
        ):
            self.root.bind(seq, accel(fn))

    # ---- actions ------------------------------------------------------------

    def _open_dashboard(self, _e=None) -> None:
        try:
            webbrowser.open(self.ctl.url)
        except OSError as exc:
            log.warning("Could not open a browser (%s). The dashboard is at %s", exc, self.ctl.url)

    def _open_data(self, _e=None) -> None:
        self._reveal(os.environ.get("DATA_DIR", ""))

    def _open_log_file(self, _e=None) -> None:
        self._reveal(str(log_dir()))

    def _reveal(self, path: str) -> None:
        if not path:
            return
        try:
            if hasattr(os, "startfile"):
                os.startfile(path)  # noqa: S606 - a directory we chose ourselves
            else:
                webbrowser.open("file://" + path)  # source runs on Linux
        except OSError as exc:
            log.warning("Could not open %s (%s)", path, exc)

    def _copy_log(self, _e=None) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))

    def _toggle(self, _e=None) -> None:
        if self.ctl.phase is ServerPhase.RUNNING:
            self.ctl.request_stop()
        elif self.ctl.phase in (ServerPhase.STOPPED, ServerPhase.FAILED):
            self._try_start()
        self._render(force=True)

    def _restart(self, _e=None) -> None:
        if self.ctl.phase is ServerPhase.RUNNING:
            self.ctl.restart()
            self._render(force=True)

    def _try_start(self, _e=None) -> None:
        self._hide_conflict()
        try:
            self.ctl.start()
        except OSError:
            self._show_conflict()
        self._render(force=True)

    def _show_conflict(self) -> None:
        self.conflict_msg.configure(
            text=f"HTTP port {self.ctl.port} is already in use — another program (or a second "
                 f"LapScope window) has it. If LapScope is already running, use its dashboard.")
        self.conflict.grid(row=3, column=0, sticky="ew", padx=16, pady=(10, 0))

    def _hide_conflict(self) -> None:
        self.conflict.grid_remove()

    # ---- the one loop -------------------------------------------------------

    def _tick(self) -> None:
        self.ctl.poll()
        if not self._closing:
            # Not while closing: the restart would finish after the stop that
            # is closing the window and hand us a freshly started server we
            # have no way left to shut down.
            try:
                self.ctl.poll_restart()
            except OSError:
                # The same conflict the first start can hit, if something took
                # the port during the gap. Letting it escape would stop the
                # tick rescheduling below and freeze the window.
                self._show_conflict()
        # Once, on the first successful start: double-clicking the exe should
        # still land you on the dashboard the way it always did. Not on every
        # Restart - that would fling a tab at someone who is already looking
        # at one.
        if self.ctl.phase is ServerPhase.RUNNING and not self._opened_once:
            self._opened_once = True
            self._open_dashboard()
        lines = self.sink.drain()
        if lines:
            self._append(lines)
        self._ticks += 1
        self._render(force=self.ctl.phase is not self._last_phase)
        if self._closing and self._closing_tick():
            return  # the window is gone; there is nothing left to reschedule on
        self._after_id = self.root.after(TICK_MS, self._tick)

    def _closing_tick(self) -> bool:
        """Returns True once the window has been destroyed."""
        if self.ctl.phase in (ServerPhase.STOPPED, ServerPhase.FAILED):
            self._destroy()
            return True
        if self.ctl.stop_overdue() and not self._asking:
            self._asking = True
            try:
                give_up = messagebox.askyesno(
                    "LapScope",
                    "LapScope hasn't finished stopping.\n\n"
                    "Force quit? The lap in progress may not be saved.",
                    parent=self.root)
            finally:
                self._asking = False
            if give_up:
                logging.shutdown()  # flush the file handler before we vanish
                os._exit(1)
            self.ctl.extend_stop_deadline()
        return False

    def _append(self, lines: list[str]) -> None:
        at_bottom = self.log.yview()[1] >= 0.999
        text = "\n".join(lines) + "\n"
        self.log.configure(state="normal")
        self.log.insert("end", text)
        drop = self.ring.add_text(text)
        if drop:
            self.log.delete("1.0", f"{drop + 1}.0")
        self.log.configure(state="disabled")
        # Only follow if they were already at the bottom, so scrolling back
        # through the log isn't yanked away on the next line.
        if at_bottom:
            self.log.see("end")

    def _render(self, force: bool = False) -> None:
        if not force and self._ticks % STATUS_EVERY:
            return
        state = derive_state(self.ctl.snapshot())
        self.dot.configure(foreground=TONE[state.tone])
        self.label.configure(text=state.label)
        self.detail.configure(text=state.detail)
        self.root.title(f"LapScope — {state.label}")
        phase = self.ctl.phase
        self._last_phase = phase
        running = phase is ServerPhase.RUNNING
        settled = phase in (ServerPhase.STOPPED, ServerPhase.FAILED)
        self.b_open.state(["!disabled"] if running else ["disabled"])
        self.b_restart.state(["!disabled"] if running else ["disabled"])
        self.b_run.configure(text="Stop" if running else "Start")
        self.b_run.state(["!disabled"] if (running or settled) and not self._closing
                         else ["disabled"])

    # ---- shutdown -----------------------------------------------------------

    def _on_close(self) -> None:
        """Stop the server, then close - no confirmation prompt.

        A graceful stop finalises and saves the lap in progress, so there is
        nothing to warn about; an "are you sure?" here would be a warning about
        a risk we just removed. The window stays up and repainting while it
        happens rather than freezing on a join.
        """
        if self._closing:
            return
        if self.ctl.phase in (ServerPhase.STOPPED, ServerPhase.FAILED):
            self._destroy()
            return
        self._closing = True
        # Closing during a Restart: the stop is already in flight, but the
        # restart behind it has to be called off or the server comes back up
        # under a window that is on its way out.
        self.ctl.cancel_restart()
        self.ctl.request_stop()
        self._render(force=True)

    def _destroy(self) -> None:
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        self.root.destroy()

    def _on_tk_error(self, exc, val, tb) -> None:
        log.error("Unhandled error in the launcher window", exc_info=(exc, val, tb))

    def run(self) -> None:
        self._try_start()
        self._tick()
        try:
            self.root.mainloop()
        finally:
            # However we got here - closed window, crash, Ctrl+W - the session
            # in progress still gets finalised before the process goes away.
            self.ctl.stop(timeout=STOP_TIMEOUT_S)
            logging.shutdown()


def run(controller, sink, log_warning: str | None = None) -> None:
    LauncherWindow(controller, sink, log_warning).run()
