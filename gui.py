"""
Tkinter GUI for the Procare downloader.

Covers the interactive parts only -- login, the top-level mode menu, and the
scope/class picker -- mirroring the existing terminal guided() / choose_scope()
flow exactly. Progress reporting stays print()-based for now (shown in a log
panel here); real progress bars are a deliberate follow-up, not done in this
pass. See CLAUDE.md's GUI note for the full rationale.

run()'s business logic is untouched: this module supplies already-decided
values on `args` (email, password, scrapbook/scrapbook_only, a scope-resolver
callback) so run()'s existing "args.X or input(...)" guards are simply never
reached in GUI mode.
"""
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

import requests

import procare_download as pd

MODE_CHOICES = [
    ("Download photos & videos AND build the scrapbook (recommended)", "both"),
    ("Download photos & videos only", "media_only"),
    ("Rebuild the scrapbook only (no re-downloading)", "scrapbook_only"),
]


class GuiIO:
    """Thread-safe stdout-like adapter. run()'s print() calls land here
    instead of the real stdout; the Tk mainloop drains the queue on a timer
    to update the log panel. Also carries the mid-run scope-picker hand-off
    (ask_scope) over the same queue -- one plumbing mechanism for both."""

    def __init__(self):
        self.q = queue.Queue()

    def write(self, s):
        if s:
            self.q.put(("log", s))

    def flush(self):
        pass

    def ask_scope(self, records):
        """Called from the worker thread (as args._scope_resolver). Blocks
        that thread only -- not the Tk mainloop -- until the GUI thread has
        shown the picker and the user has answered."""
        items = sorted(pd.class_spans(records).items(), key=lambda kv: kv[1][0])
        resp_q = queue.Queue(maxsize=1)
        self.q.put(("ask_scope", items, resp_q))
        return resp_q.get()

    def done(self, exc):
        self.q.put(("done", exc))


def _scrapbook_only_feed_exists(args):
    """Mirrors run()'s own check for its no-login fast path, so the GUI can
    skip the Login screen in the same case the CLI already skips the prompt."""
    out_dir = os.path.abspath(args.out)
    feed_path = os.path.join(out_dir, "Scrapbook", "feed.json")
    legacy_feed = os.path.join(out_dir, "feed.json")
    return os.path.exists(feed_path) or os.path.exists(legacy_feed)


class App:
    def __init__(self, root, args, app_version):
        self.root = root
        self.args = args
        self.app_version = app_version
        self.gui_io = GuiIO()
        self.stats = {}
        self.downloading = False
        root.title(f"Procare Downloader v{app_version}")
        root.geometry("560x440")
        root.minsize(480, 360)
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.container = tk.Frame(root)
        self.container.pack(fill="both", expand=True)
        # Same order as the terminal flow: guided() picks a mode before run()
        # ever prompts for a login -- and run() skips the login prompt
        # entirely for "rebuild scrapbook only" when a feed.json already
        # exists, so the GUI must offer the mode choice first too.
        self._show_mode()

    # ---- screen management ----
    def _clear(self):
        for w in self.container.winfo_children():
            w.destroy()
        self.root.unbind("<Return>")

    def _on_close(self):
        if self.downloading:
            if not messagebox.askyesno("Quit?", "A download is in progress. Quit anyway?"):
                return
        self.root.destroy()

    # ---- Screen: Mode ----
    def _show_mode(self):
        self._clear()
        f = self.container
        tk.Label(f, text="Procare Downloader", font=("", 16, "bold")).pack(pady=(20, 2))
        tk.Label(f, text=f"version {self.app_version}", fg="gray").pack()
        tk.Label(f, text="What would you like to do?", font=("", 12, "bold")).pack(pady=(24, 10))
        self.mode_var = tk.StringVar(value="both")
        for label, value in MODE_CHOICES:
            tk.Radiobutton(f, text=label, variable=self.mode_var, value=value,
                          anchor="w", justify="left", wraplength=460).pack(fill="x", padx=30, pady=4)
        btn = tk.Button(f, text="Next", width=14, command=self._submit_mode)
        btn.pack(pady=20)
        self.root.bind("<Return>", lambda e: btn.invoke())

    def _submit_mode(self):
        choice = self.mode_var.get()
        # Mirrors guided()'s exact mapping -- keep in sync if that menu changes.
        if choice == "media_only":
            self.args.scrapbook = False
        elif choice == "scrapbook_only":
            self.args.scrapbook_only = True
        else:
            self.args.scrapbook = True
        if self.args.scrapbook_only and _scrapbook_only_feed_exists(self.args):
            self._start_run()  # run() takes its no-login fast path
        else:
            self._show_login()

    # ---- Screen: Login ----
    def _show_login(self):
        self._clear()
        f = self.container
        tk.Label(f, text="Log in to Procare", font=("", 14, "bold")).pack(pady=(24, 10))
        form = tk.Frame(f)
        form.pack(pady=10)
        tk.Label(form, text="Procare email:").grid(row=0, column=0, sticky="e", padx=6, pady=6)
        email_entry = tk.Entry(form, width=32)
        email_entry.grid(row=0, column=1, pady=6)
        if self.args.email:
            email_entry.insert(0, self.args.email)
        tk.Label(form, text="Password:").grid(row=1, column=0, sticky="e", padx=6, pady=6)
        pw_entry = tk.Entry(form, width=32, show="*")
        pw_entry.grid(row=1, column=1, pady=6)
        error_label = tk.Label(f, text="", fg="red", wraplength=460, justify="left")
        error_label.pack(padx=20)
        btn = tk.Button(f, text="Continue", width=14,
                        command=lambda: self._submit_login(email_entry, pw_entry, btn, error_label))
        btn.pack(pady=10)
        email_entry.focus_set()
        self.root.bind("<Return>", lambda e: btn.invoke())

    def _submit_login(self, email_entry, pw_entry, btn, error_label):
        email = email_entry.get().strip()
        password = pw_entry.get()
        if not email or not password:
            error_label.config(text="Enter both an email and a password.")
            return
        btn.config(state="disabled")
        error_label.config(text="Checking your login...", fg="gray")

        def worker():
            try:
                session = requests.Session()
                pd.authenticate(session, email, password)
                self.root.after(0, lambda: self._login_ok(email, password))
            except SystemExit as e:
                msg = str(e.code) if e.code is not None else "Login failed."
                self.root.after(0, lambda: self._login_failed(msg, btn, error_label))
            except Exception as e:
                self.root.after(0, lambda: self._login_failed(str(e), btn, error_label))

        threading.Thread(target=worker, daemon=True).start()

    def _login_failed(self, message, btn, error_label):
        btn.config(state="normal")
        error_label.config(text=message, fg="red")

    def _login_ok(self, email, password):
        self.args.email = email
        self.args.password = password
        self._start_run()

    # ---- Screen: Running (log) ----
    def _start_run(self):
        self._clear()
        self._build_running_screen()

        self.args._interactive = True
        self.args._scope_resolver = self.gui_io.ask_scope
        self.args._on_stats = lambda stats: self.root.after(0, lambda s=stats: self._remember_stats(s))
        self.downloading = True

        def worker():
            old_stdout = sys.stdout
            sys.stdout = self.gui_io
            exc = None
            try:
                pd.run(self.args)
            except SystemExit as e:
                exc = str(e.code) if e.code is not None else None
            except Exception as e:
                exc = str(e)
            finally:
                sys.stdout = old_stdout
                self.gui_io.done(exc)

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self._poll_queue)

    def _build_running_screen(self):
        f = self.container
        tk.Label(f, text="Working...", font=("", 13, "bold")).pack(pady=(14, 4))
        tk.Label(f, text="This can take several minutes to tens of minutes for a large "
                        "library -- please keep this window open.",
                fg="gray", wraplength=500, justify="left").pack(padx=10)
        self.progress = ttk.Progressbar(f, mode="indeterminate", length=460)
        self.progress.pack(pady=8)
        self.progress.start(12)
        self.log = ScrolledText(f, width=70, height=16, state="disabled")
        self.log.pack(fill="both", expand=True, padx=10, pady=10)

    def _remember_stats(self, stats):
        self.stats = stats

    def _poll_queue(self):
        try:
            while True:
                msg = self.gui_io.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._append_log(msg[1])
                elif kind == "ask_scope":
                    self._show_scope_picker(msg[1], msg[2])
                    return  # scope screen owns the mainloop until it hands back
                elif kind == "done":
                    self._finish(msg[1])
                    return
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _append_log(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.config(state="disabled")

    # ---- Screen: Scope picker (shown mid-run, via the queue hand-off) ----
    def _show_scope_picker(self, items, resp_q):
        self._clear()
        f = self.container
        tk.Label(f, text="What would you like to download?", font=("", 13, "bold")).pack(pady=(20, 10))
        choice_var = tk.StringVar(value="1")
        tk.Radiobutton(f, text="Everything (all available history)", variable=choice_var,
                      value="1", anchor="w").pack(fill="x", padx=30, pady=2)
        for i, (name, (d0, d1, _)) in enumerate(items, start=2):
            tk.Radiobutton(f, text=f'Just "{name}"  ({d0} to {d1})', variable=choice_var,
                          value=str(i), anchor="w", wraplength=460,
                          justify="left").pack(fill="x", padx=30, pady=2)
        custom = len(items) + 2
        tk.Radiobutton(f, text="A custom date range", variable=choice_var,
                      value=str(custom), anchor="w").pack(fill="x", padx=30, pady=2)
        date_frame = tk.Frame(f)
        date_frame.pack(pady=6)
        tk.Label(date_frame, text="Start (YYYY-MM-DD):").grid(row=0, column=0, padx=4)
        start_entry = tk.Entry(date_frame, width=12)
        start_entry.grid(row=0, column=1, padx=4)
        tk.Label(date_frame, text="Finish (YYYY-MM-DD):").grid(row=0, column=2, padx=4)
        finish_entry = tk.Entry(date_frame, width=12)
        finish_entry.grid(row=0, column=3, padx=4)

        def submit():
            result = pd.resolve_scope(items, choice_var.get(), start_entry.get(), finish_entry.get())
            resp_q.put(result)
            self._clear()
            self._build_running_screen()
            self.root.after(100, self._poll_queue)

        tk.Button(f, text="Continue", width=14, command=submit).pack(pady=16)

    # ---- Screen: Done ----
    def _finish(self, exc):
        self.downloading = False
        self._clear()
        f = self.container
        if exc:
            tk.Label(f, text="Something went wrong", font=("", 14, "bold"), fg="#a33").pack(pady=(24, 8))
            msg = tk.Text(f, width=64, height=8, wrap="word")
            msg.insert("1.0", str(exc))
            msg.config(state="disabled")
            msg.pack(padx=14, pady=6)
        else:
            tk.Label(f, text="Done!", font=("", 16, "bold")).pack(pady=(24, 8))
            if self.stats:
                for line in (f"Downloaded: {self.stats.get('downloaded', 0)}",
                            f"Skipped (already had them): {self.stats.get('skipped_exist', 0)}",
                            f"Failed: {self.stats.get('failed', 0)}"):
                    tk.Label(f, text=line).pack()
            landing = os.path.join(os.path.abspath(self.args.out), "Open Scrapbook.html")
            if os.path.exists(landing):
                tk.Button(f, text="Open Scrapbook", width=18,
                         command=lambda: pd.open_path(landing)).pack(pady=10)
        tk.Button(f, text="Close", width=14, command=self.root.destroy).pack(pady=6)


def launch_gui(args, app_version):
    """Entry point called from procare_download.main(). Blocks until the
    window is closed. All outcomes (success/failure) are shown in-window --
    there's no console to fall back on under a --windowed build."""
    try:
        root = tk.Tk()
    except Exception:
        _native_error_fallback(
            "Procare Downloader couldn't open its window. Try running it from a "
            "terminal with --no-gui instead, or reinstall the app.")
        return
    try:
        App(root, args, app_version)
        root.mainloop()
    except Exception:
        try:
            root.destroy()
        except Exception:
            pass
        _native_error_fallback(
            "Procare Downloader hit an unexpected error and had to close. "
            "Try running it from a terminal with --no-gui to see details.")


def _native_error_fallback(message):
    """Dependency-free, no-Tk-required error box for the case where Tk itself
    won't even initialize -- otherwise a --windowed build with a broken Tk
    would exit with no console and no window, worse than today's traceback."""
    try:
        if sys.platform.startswith("win"):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, "Procare Downloader", 0x10)
        elif sys.platform == "darwin":
            import subprocess
            subprocess.run(["osascript", "-e",
                           f'display alert "Procare Downloader" message "{message}"'], check=False)
        else:
            print(message, file=sys.stderr)
    except Exception:
        pass
