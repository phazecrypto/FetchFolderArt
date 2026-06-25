#!/usr/bin/env python3
"""Windows GUI wrapper for fetch_folder_art.py."""

from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import shutil
import tempfile
import threading
import tkinter as tk
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

sys.dont_write_bytecode = True

try:
    from . import fetch_folder_art as core
except ImportError:
    import fetch_folder_art as core
from PIL import Image, ImageTk


DEFAULT_TIMEOUT = 10.0
DEFAULT_RETRIES = 1
DEFAULT_CANDIDATES = 5
DEFAULT_MIN_SCORE = 70
APP_VERSION = "1.0"
CACHE_VERSION = 1
PACKAGE_DIR = Path(__file__).resolve().parent
SOURCE_ROOT = PACKAGE_DIR.parent.parent
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else (
    SOURCE_ROOT if PACKAGE_DIR.parent.name == "src" else PACKAGE_DIR
)
DATA_DIR = APP_DIR / "data"
CACHE_DIR = DATA_DIR / "cache"
CACHE_MANIFEST = CACHE_DIR / "manifest.json"
CACHE_COVERS_DIR = CACHE_DIR / "covers"
LEGACY_TEMP_CACHE_DIR = Path(tempfile.gettempdir()) / "fetch_folder_art_gui_cache"

THEMES = {
    "light": {
        "bg": "#f4f6f8",
        "panel": "#ffffff",
        "panel_alt": "#eef2f6",
        "text": "#17212b",
        "muted": "#536170",
        "border": "#c8d0d8",
        "field": "#ffffff",
        "button": "#e7edf3",
        "button_active": "#d8e3ee",
        "select": "#2f6f9f",
        "select_text": "#ffffff",
        "log_bg": "#ffffff",
        "log_text": "#111827",
        "log_no_art": "#b42318",
        "log_error": "#9f1239",
        "insert": "#111827",
    },
    "dark": {
        "bg": "#15191f",
        "panel": "#1f2630",
        "panel_alt": "#252d38",
        "text": "#eef3f8",
        "muted": "#aab6c3",
        "border": "#3a4654",
        "field": "#11161c",
        "button": "#2f3945",
        "button_active": "#3a4755",
        "select": "#5aa2d6",
        "select_text": "#06111a",
        "log_bg": "#0f141a",
        "log_text": "#e9eef5",
        "log_no_art": "#ff6b6b",
        "log_error": "#ff8aa1",
        "insert": "#e9eef5",
    },
}


class QueueLogWriter:
    def __init__(self, work_queue: queue.Queue[tuple[str, object]]) -> None:
        self.work_queue = work_queue

    def write(self, text: str) -> int:
        if text:
            self.work_queue.put(("log", text))
        return len(text)

    def flush(self) -> None:
        pass


class FolderArtGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title("FetchFolderArt by devphaZe foundry")
        self.geometry("1050x720")
        self.minsize(920, 620)

        self.work_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_requested = threading.Event()
        self.preview_photo: ImageTk.PhotoImage | None = None
        self.preview_items: list[tuple[bytes, str]] = []
        self.preview_index = -1
        self.batch_roots: list[Path] = []
        self.batch_index = 0

        self.music_root = tk.StringVar(value="M:\\" if Path("M:\\").exists() else "")
        self.log_dir = tk.StringVar(value=str(DATA_DIR))
        self.automatic_commits = tk.BooleanVar(value=False)
        self.force = tk.BooleanVar(value=False)
        self.limit = tk.StringVar(value="")
        self.status = tk.StringVar(value="Ready")
        self.dark_mode = tk.BooleanVar(value=False)
        self.style = ttk.Style(self)

        self._build_menu()
        self._build_ui()
        self._apply_theme()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_queue)

    def _build_menu(self) -> None:
        menu_bar = tk.Menu(self)

        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="Start Scan", command=self._start)
        file_menu.add_command(label="Preview First Cover", command=self._preview_first)
        file_menu.add_separator()
        file_menu.add_command(label="Choose Music Folder...", command=self._choose_music_root)
        file_menu.add_command(label="Choose Log Directory...", command=self._choose_log_dir)
        file_menu.add_command(label="Open Data Folder", command=self._open_data_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self._on_close)
        menu_bar.add_cascade(label="File", menu=file_menu)

        view_menu = tk.Menu(menu_bar, tearoff=False)
        view_menu.add_checkbutton(
            label="Dark Mode",
            variable=self.dark_mode,
            command=self._apply_theme,
        )
        view_menu.add_separator()
        view_menu.add_command(label="Previous Preview", command=self._previous_preview)
        view_menu.add_command(label="Next Preview", command=self._next_preview)
        view_menu.add_separator()
        view_menu.add_command(label="Clear Output", command=self._clear_output)
        view_menu.add_command(label="Clear Preview Gallery", command=self._clear_preview)
        menu_bar.add_cascade(label="View", menu=view_menu)

        options_menu = tk.Menu(menu_bar, tearoff=False)
        options_menu.add_checkbutton(
            label="Automatic Commits",
            variable=self.automatic_commits,
        )
        options_menu.add_checkbutton(
            label="Force Replace Existing Art",
            variable=self.force,
        )
        options_menu.add_separator()
        options_menu.add_command(label="Discogs Token...", command=self._show_discogs_token_dialog)
        options_menu.add_command(label="Clean Temporary Files", command=self._clean_temp_from_menu)
        menu_bar.add_cascade(label="Options", menu=options_menu)

        help_menu = tk.Menu(menu_bar, tearoff=False)
        help_menu.add_command(label="Help Contents", command=self._show_help)
        help_menu.add_command(label="About FetchFolderArt", command=self._show_about)
        menu_bar.add_cascade(label="Help", menu=help_menu)

        self.configure(menu=menu_bar)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        controls = ttk.Frame(self, padding=12)
        controls.grid(row=0, column=0, sticky="ew")
        controls.columnconfigure(2, weight=1)

        ttk.Label(controls, text="Music folder").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Button(
            controls,
            text="^",
            width=2,
            style="PathTool.TButton",
            command=self._navigate_music_folder_up,
        ).grid(
            row=0, column=1, sticky="w", padx=(0, 6)
        )
        ttk.Entry(controls, textvariable=self.music_root).grid(row=0, column=2, sticky="ew")
        ttk.Button(controls, text="Browse...", command=self._choose_music_root).grid(
            row=0, column=3, padx=(8, 0)
        )

        ttk.Label(controls, text="Log directory").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
        ttk.Entry(controls, textvariable=self.log_dir).grid(row=1, column=2, sticky="ew", pady=(8, 0))
        ttk.Button(controls, text="Browse...", command=self._choose_log_dir).grid(
            row=1, column=3, padx=(8, 0), pady=(8, 0)
        )

        options = ttk.Frame(controls)
        options.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        options.columnconfigure(6, weight=1)

        ttk.Checkbutton(options, text="Automatic commits", variable=self.automatic_commits).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Checkbutton(options, text="Force replace existing art", variable=self.force).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        ttk.Label(options, text="Limit (blank = all)").grid(row=1, column=1, sticky="w", padx=(18, 6), pady=(6, 0))
        ttk.Entry(options, textvariable=self.limit, width=10).grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Button(options, text="Preview first cover", command=self._preview_first).grid(
            row=1, column=3, sticky="e", padx=(18, 0), pady=(6, 0)
        )
        self.start_button = ttk.Button(options, text="Start Scan", command=self._start)
        self.start_button.grid(row=1, column=4, sticky="e", padx=(8, 0), pady=(6, 0))
        self.stop_button = ttk.Button(options, text="Stop", command=self._stop, state="disabled")
        self.stop_button.grid(row=1, column=5, sticky="e", padx=(8, 0), pady=(6, 0))
        ttk.Checkbutton(
            options,
            text="Dark mode",
            variable=self.dark_mode,
            command=self._apply_theme,
        ).grid(row=1, column=6, sticky="e", padx=(18, 0), pady=(6, 0))

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))

        left = ttk.Frame(main)
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        main.add(left, weight=3)

        ttk.Label(left, textvariable=self.status).grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.log_text = tk.Text(left, wrap="word", height=18)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient=tk.VERTICAL, command=self.log_text.yview)
        scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        right = ttk.Frame(main, padding=(12, 0, 0, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        main.add(right, weight=2)

        ttk.Label(right, text="Cover preview").grid(row=0, column=0, sticky="w", pady=(0, 8))
        self.preview_frame = ttk.Frame(right, relief="solid", borderwidth=1, width=360, height=360)
        self.preview_frame.grid(row=1, column=0, sticky="nsew")
        self.preview_frame.grid_propagate(False)
        self.preview_frame.columnconfigure(0, weight=1)
        self.preview_frame.rowconfigure(0, weight=1)
        self.preview_label = ttk.Label(self.preview_frame, text="No cover loaded", anchor="center")
        self.preview_label.grid(row=0, column=0, sticky="nsew")

        nav = ttk.Frame(right)
        nav.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        nav.columnconfigure(1, weight=1)
        self.prev_preview_button = ttk.Button(nav, text="<", width=4, command=self._previous_preview)
        self.prev_preview_button.grid(row=0, column=0, sticky="w")
        self.preview_position = tk.StringVar(value="0 / 0")
        ttk.Label(nav, textvariable=self.preview_position, anchor="center").grid(
            row=0, column=1, sticky="ew", padx=8
        )
        self.next_preview_button = ttk.Button(nav, text=">", width=4, command=self._next_preview)
        self.next_preview_button.grid(row=0, column=2, sticky="e")

        self.preview_info = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.preview_info, wraplength=360, justify="left").grid(
            row=3, column=0, sticky="ew", pady=(10, 0)
        )

        footer = ttk.Frame(self, style="Status.TFrame", padding=(12, 4))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, textvariable=self.status, style="Status.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(footer, text=f"Version: {APP_VERSION}", style="Status.TLabel").grid(
            row=0, column=1, sticky="e"
        )
        self._update_preview_nav()

    def _apply_theme(self) -> None:
        palette = THEMES["dark" if self.dark_mode.get() else "light"]

        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass

        self.configure(bg=palette["bg"])
        self.style.configure(".", background=palette["bg"], foreground=palette["text"])
        self.style.configure("TFrame", background=palette["bg"])
        self.style.configure("TLabel", background=palette["bg"], foreground=palette["text"])
        self.style.configure("TCheckbutton", background=palette["bg"], foreground=palette["text"])
        self.style.configure("TRadiobutton", background=palette["bg"], foreground=palette["text"])
        self.style.configure("TPanedwindow", background=palette["bg"])
        self.style.configure("Status.TFrame", background=palette["panel_alt"])
        self.style.configure(
            "Status.TLabel",
            background=palette["panel_alt"],
            foreground=palette["muted"],
        )
        self.style.configure(
            "TButton",
            background=palette["button"],
            foreground=palette["text"],
            bordercolor=palette["border"],
            focusthickness=1,
            focuscolor=palette["select"],
            padding=(10, 5),
        )
        self.style.configure(
            "PathTool.TButton",
            background=palette["button"],
            foreground=palette["text"],
            bordercolor=palette["border"],
            padding=(4, 1),
        )
        self.style.map(
            "TButton",
            background=[("active", palette["button_active"]), ("disabled", palette["panel_alt"])],
            foreground=[("disabled", palette["muted"])],
        )
        self.style.map(
            "PathTool.TButton",
            background=[("active", palette["button_active"]), ("disabled", palette["panel_alt"])],
            foreground=[("disabled", palette["muted"])],
        )
        self.style.configure(
            "TEntry",
            fieldbackground=palette["field"],
            foreground=palette["text"],
            insertcolor=palette["insert"],
            bordercolor=palette["border"],
            lightcolor=palette["border"],
            darkcolor=palette["border"],
        )
        self.style.map(
            "TEntry",
            fieldbackground=[("readonly", palette["panel_alt"]), ("disabled", palette["panel_alt"])],
            foreground=[("disabled", palette["muted"])],
        )
        self.style.configure(
            "Vertical.TScrollbar",
            background=palette["button"],
            troughcolor=palette["panel_alt"],
            bordercolor=palette["border"],
            arrowcolor=palette["text"],
        )
        self.style.map(
            "TCheckbutton",
            background=[("active", palette["bg"])],
            foreground=[("disabled", palette["muted"])],
        )
        self.style.map(
            "TRadiobutton",
            background=[("active", palette["bg"])],
            foreground=[("disabled", palette["muted"])],
        )

        if hasattr(self, "log_text"):
            self.log_text.configure(
                background=palette["log_bg"],
                foreground=palette["log_text"],
                insertbackground=palette["insert"],
                selectbackground=palette["select"],
                selectforeground=palette["select_text"],
                relief="solid",
                borderwidth=1,
                highlightthickness=1,
                highlightbackground=palette["border"],
                highlightcolor=palette["select"],
            )
            self.log_text.tag_configure("no_art", foreground=palette["log_no_art"])
            self.log_text.tag_configure("error", foreground=palette["log_error"])
        if hasattr(self, "preview_label"):
            self.preview_label.configure(
                background=palette["panel"],
                foreground=palette["muted"],
            )
        if hasattr(self, "preview_frame"):
            self.preview_frame.configure(style="Preview.TFrame")
            self.style.configure(
                "Preview.TFrame",
                background=palette["panel"],
                bordercolor=palette["border"],
                lightcolor=palette["border"],
                darkcolor=palette["border"],
            )

    def _choose_music_root(self) -> None:
        roots = self._parse_music_roots()
        current = roots[-1] if roots else Path("M:\\")
        if not current.exists() or not current.is_dir():
            fallback = Path("M:\\")
            current = fallback if fallback.exists() and fallback.is_dir() else Path.home()

        selected = self._show_folder_list(current)
        if selected is not None:
            self._set_music_roots(selected)

    def _parse_music_roots(self) -> list[Path]:
        text = self.music_root.get().strip()
        if not text:
            return []
        parts = [part.strip().strip('"') for part in text.split(";")]
        return [Path(part).expanduser() for part in parts if part]

    def _set_music_roots(self, roots: list[Path]) -> None:
        unique: list[Path] = []
        seen: set[str] = set()
        for root in roots:
            key = str(root).casefold()
            if key not in seen:
                unique.append(root)
                seen.add(key)
        self.music_root.set("; ".join(str(root) for root in unique))

    def _navigate_music_folder_up(self) -> None:
        roots = self._parse_music_roots()
        current = roots[-1] if roots else Path("M:\\")
        if not current.exists():
            current = current.parent if str(current.parent) != "." else Path("M:\\")

        parent = current.parent
        if parent == current:
            parent = current

        selected = self._show_folder_list(parent, current.name)
        if selected is not None:
            self._set_music_roots(selected)

    def _show_folder_list(self, folder: Path, highlight_name: str = "") -> list[Path] | None:
        selected_paths: list[Path] | None = None
        current_folder = folder
        current_highlight = highlight_name

        dialog = tk.Toplevel(self)
        dialog.title("Choose Music Folder")
        dialog.geometry("640x480")
        dialog.minsize(520, 360)
        dialog.transient(self)
        dialog.grab_set()

        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(2, weight=1)

        folder_label = tk.StringVar(value=str(current_folder))
        ttk.Label(dialog, textvariable=folder_label).grid(
            row=0, column=0, sticky="ew", padx=12, pady=(12, 6)
        )
        ttk.Label(dialog, text="Select one or more folders, or use the current folder.").grid(
            row=1, column=0, sticky="w", padx=12, pady=(0, 6)
        )

        list_frame = ttk.Frame(dialog)
        list_frame.grid(row=2, column=0, sticky="nsew", padx=12)
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)

        listbox = tk.Listbox(
            list_frame,
            activestyle="dotbox",
            exportselection=False,
            selectmode=tk.EXTENDED,
        )
        listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=listbox.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        listbox.configure(yscrollcommand=scrollbar.set)

        buttons = ttk.Frame(dialog)
        buttons.grid(row=3, column=0, sticky="ew", padx=12, pady=12)
        buttons.columnconfigure(1, weight=1)

        folders: list[Path] = []

        def apply_list_theme() -> None:
            palette = THEMES["dark" if self.dark_mode.get() else "light"]
            dialog.configure(bg=palette["bg"])
            listbox.configure(
                background=palette["log_bg"],
                foreground=palette["log_text"],
                selectbackground=palette["select"],
                selectforeground=palette["select_text"],
                highlightbackground=palette["border"],
                highlightcolor=palette["select"],
            )

        def refresh(target: Path, name_to_highlight: str = "") -> None:
            nonlocal current_folder, current_highlight, folders
            current_folder = target
            current_highlight = name_to_highlight
            folder_label.set(str(current_folder))
            listbox.delete(0, "end")
            try:
                folders = sorted(
                    [path for path in current_folder.iterdir() if path.is_dir()],
                    key=lambda path: path.name.casefold(),
                )
            except OSError as exc:
                folders = []
                listbox.insert("end", f"Could not read folder: {exc}")
                return

            for path in folders:
                listbox.insert("end", path.name)

            if folders:
                highlight_index = 0
                for index, path in enumerate(folders):
                    if path.name.casefold() == current_highlight.casefold():
                        highlight_index = index
                        break
                listbox.selection_clear(0, "end")
                listbox.selection_set(highlight_index)
                listbox.activate(highlight_index)
                listbox.see(highlight_index)

        def selected_folders() -> list[Path]:
            selection = listbox.curselection()
            if not selection or not folders:
                return []
            selected: list[Path] = []
            for value in selection:
                index = int(value)
                if index < len(folders):
                    selected.append(folders[index])
            return selected

        def use_selected() -> None:
            nonlocal selected_paths
            selected_paths = selected_folders()
            if not selected_paths:
                messagebox.showinfo(
                    "Choose Music Folder",
                    "Select one or more folders in the list first.",
                    parent=dialog,
                )
                return
            dialog.destroy()

        def use_current() -> None:
            nonlocal selected_paths
            selected_paths = [current_folder]
            dialog.destroy()

        def up_again() -> None:
            next_parent = current_folder.parent
            if next_parent == current_folder:
                return
            refresh(next_parent, current_folder.name)

        def cancel() -> None:
            dialog.destroy()

        ttk.Button(buttons, text="Up", command=up_again).grid(row=0, column=0, sticky="w")
        ttk.Button(buttons, text="Use Current Folder", command=use_current).grid(
            row=0, column=1, sticky="e", padx=(8, 0)
        )
        ttk.Button(buttons, text="Use Selected Folder(s)", command=use_selected).grid(
            row=0, column=2, sticky="e", padx=(8, 0)
        )
        ttk.Button(buttons, text="Cancel", command=cancel).grid(row=0, column=3, sticky="e", padx=(8, 0))

        listbox.bind("<Double-Button-1>", lambda _event: use_selected())
        listbox.bind("<Return>", lambda _event: use_selected())
        dialog.bind("<Escape>", lambda _event: cancel())

        apply_list_theme()
        refresh(current_folder, current_highlight)
        dialog.wait_window()
        return selected_paths

    def _choose_log_dir(self) -> None:
        selected = filedialog.askdirectory(title="Choose log directory")
        if selected:
            self.log_dir.set(selected)

    def _open_data_folder(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(DATA_DIR)
        except OSError as exc:
            messagebox.showerror("Fetch Folder Art", f"Could not open data folder:\n{exc}")

    def _clean_temp_from_menu(self) -> None:
        cleanup_temporary_files()
        self._append_log("\nTemporary files cleaned.\n")
        self.status.set("Temporary files cleaned.")

    def _show_discogs_token_dialog(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("Discogs Token")
        dialog.geometry("520x260")
        dialog.minsize(460, 240)
        dialog.transient(self)
        dialog.grab_set()
        dialog.columnconfigure(0, weight=1)

        token = core.get_discogs_token()
        token_var = tk.StringVar(value=token)
        show_token = tk.BooleanVar(value=False)
        status_var = tk.StringVar(
            value="Discogs token is configured." if token else "Discogs token is not configured."
        )

        ttk.Label(
            dialog,
            text="Discogs Token",
            font=("Segoe UI", 11, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(14, 6))
        ttk.Label(
            dialog,
            text=(
                "Add your personal Discogs token to enable Discogs as the final artwork "
                "fallback source. The token is saved to your Windows user environment, "
                "not inside the app code."
            ),
            wraplength=480,
        ).grid(row=1, column=0, sticky="ew", padx=14)
        ttk.Label(dialog, textvariable=status_var).grid(row=2, column=0, sticky="w", padx=14, pady=(12, 4))

        entry = ttk.Entry(dialog, textvariable=token_var, show="*")
        entry.grid(row=3, column=0, sticky="ew", padx=14)

        def toggle_visibility() -> None:
            entry.configure(show="" if show_token.get() else "*")

        ttk.Checkbutton(
            dialog,
            text="Show token",
            variable=show_token,
            command=toggle_visibility,
        ).grid(row=4, column=0, sticky="w", padx=14, pady=(6, 0))

        buttons = ttk.Frame(dialog)
        buttons.grid(row=5, column=0, sticky="ew", padx=14, pady=(18, 14))
        buttons.columnconfigure(0, weight=1)

        def save_token() -> None:
            new_token = token_var.get().strip()
            if not new_token:
                messagebox.showinfo(
                    "Discogs Token",
                    "Enter a token to save, or use Remove Token.",
                    parent=dialog,
                )
                return
            core.set_discogs_token(new_token)
            status_var.set("Discogs token is configured.")
            self.status.set("Discogs token saved.")
            self._append_log("\nDiscogs token saved to the Windows user environment.\n")

        def remove_token() -> None:
            core.clear_discogs_token()
            token_var.set("")
            status_var.set("Discogs token is not configured.")
            self.status.set("Discogs token removed.")
            self._append_log("\nDiscogs token removed from the Windows user environment.\n")

        ttk.Button(buttons, text="Save Token", command=save_token).grid(row=0, column=1, padx=(0, 8))
        ttk.Button(buttons, text="Remove Token", command=remove_token).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(buttons, text="Close", command=dialog.destroy).grid(row=0, column=3)
        entry.focus_set()

    def _show_help(self) -> None:
        help_window = tk.Toplevel(self)
        help_window.title("FetchFolderArt Help")
        help_window.geometry("820x560")
        help_window.minsize(720, 460)
        help_window.transient(self)

        help_bg = "#d9d9d9"
        content_bg = "#f7f7f7"
        heading_blue = "#003399"

        help_window.configure(bg=help_bg)

        frame = tk.Frame(help_window, bg=help_bg, padx=10, pady=10)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(1, weight=1)

        title = tk.Label(
            frame,
            text="FetchFolderArt Help",
            bg=help_bg,
            fg=heading_blue,
            font=("Segoe UI", 12, "bold"),
            anchor="w",
        )
        title.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        toc_frame = tk.Frame(frame, bg=help_bg)
        toc_frame.grid(row=1, column=0, sticky="nsw", padx=(0, 10))
        tk.Label(
            toc_frame,
            text="Contents",
            bg=help_bg,
            fg=heading_blue,
            font=("Segoe UI", 10, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(0, 4))

        toc = tk.Listbox(
            toc_frame,
            width=26,
            height=18,
            activestyle="dotbox",
            exportselection=False,
            bg="#eeeeee",
            fg="#111111",
            selectbackground="#0a64ad",
            selectforeground="#ffffff",
            font=("Segoe UI", 9),
        )
        toc.pack(side="left", fill="y", expand=False)
        toc_scroll = ttk.Scrollbar(toc_frame, orient=tk.VERTICAL, command=toc.yview)
        toc_scroll.pack(side="right", fill="y")
        toc.configure(yscrollcommand=toc_scroll.set)

        content_frame = tk.Frame(frame, bg=help_bg)
        content_frame.grid(row=1, column=1, sticky="nsew")
        content_frame.rowconfigure(0, weight=1)
        content_frame.columnconfigure(0, weight=1)

        text = tk.Text(
            content_frame,
            wrap="word",
            bg=content_bg,
            fg="#111111",
            relief="sunken",
            borderwidth=1,
            padx=12,
            pady=10,
            font=("Segoe UI", 9),
        )
        text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(content_frame, orient=tk.VERTICAL, command=text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scroll.set)
        text.tag_configure("heading", foreground=heading_blue, font=("Segoe UI", 12, "bold"))
        text.tag_configure("body", foreground="#111111", font=("Segoe UI", 9))

        sections = self._help_sections()
        for heading, _body in sections:
            toc.insert("end", heading)

        def show_section(index: int) -> None:
            heading, body = sections[index]
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("end", heading + "\n\n", "heading")
            text.insert("end", body, "body")
            text.configure(state="disabled")
            text.see("1.0")

        def on_select(_event: object = None) -> None:
            selection = toc.curselection()
            if selection:
                show_section(int(selection[0]))

        toc.bind("<<ListboxSelect>>", on_select)
        toc.selection_set(0)
        toc.activate(0)
        show_section(0)

        button_row = tk.Frame(frame, bg=help_bg)
        button_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(button_row, text="Close", command=help_window.destroy).pack(
            side="right"
        )

    def _help_sections(self) -> list[tuple[str, str]]:
        return [
            (
                "Overview",
                "FetchFolderArt scans selected music folders recursively, reads album tags, "
                "finds missing folder artwork, previews the images, then asks whether to "
                "commit the queued folder.jpg files.",
            ),
            (
                "Music Folder",
                "Choose one or more root folders to scan. Mapped drives and UNC paths are "
                "supported, for example M:\\ or \\\\NAS\\music.\n\n"
                "Use the small ^ button beside the path to browse the parent folder with "
                "the current folder highlighted. In that folder list, use Ctrl or Shift "
                "to select multiple folders. Multiple selected roots appear in the field "
                "separated by semicolons.",
            ),
            (
                "Log Directory",
                "Choose where art_fetch_log.csv is saved. The default is "
                "D:\\FetchFolderArt\\data.",
            ),
            (
                "Automatic Commits",
                "When unchecked, each completed scan shows a commit popup before any "
                "folder.jpg files are written.\n\n"
                "When checked, the app still builds Matched Results first, then commits "
                "the cached changes automatically with no confirmation dialogs. For "
                "multiple selected directories, each directory is scanned, its queued "
                "folder.jpg files are committed automatically, and the app continues to "
                "the next selected directory.",
            ),
            (
                "Force Replace Existing Art",
                "When unchecked, album folders that already contain common artwork files "
                "such as folder.jpg, cover.jpg, front.jpg, album.jpg, or AlbumArt*.jpg "
                "are skipped.\n\n"
                "When checked, folder.jpg can be replaced during the commit step.",
            ),
            (
                "Discogs Token",
                "Discogs is optional and is used only as the final artwork fallback source. "
                "Open Options > Discogs Token to add, update, or remove a personal Discogs "
                "token.\n\n"
                "The token is saved to the current Windows user's DISCOGS_TOKEN environment "
                "variable. It is not written into the app source files, logs, cache manifest, "
                "or folder.jpg output. If you share the app with another person, they will "
                "need to enter their own Discogs token on their computer.",
            ),
            (
                "Limit",
                "Leave blank to scan all album folders. Enter a whole number to scan only "
                "the first matching album folders found.",
            ),
            (
                "Preview First Cover",
                "Searches for the first usable cover without committing changes. This is "
                "useful for checking folder paths and artwork matching before a full scan.",
            ),
            (
                "Start Scan",
                "Builds Matched Results first. Images that would be written are cached "
                "temporarily and shown in the preview gallery.\n\n"
                "When each selected root folder finishes, choose Yes in the commit popup "
                "to write that folder's queued folder.jpg files, or No to skip that folder "
                "and continue to the next selected root. If Automatic Commits is checked, "
                "the cached changes are committed without popup confirmation.",
            ),
            (
                "Preview Gallery",
                "Use the < and > buttons to review artwork found during the scan. The "
                "counter shows the current image and total queued images.",
            ),
            (
                "Dark Mode",
                "Switches the main app between light and dark color themes.",
            ),
            (
                "Artwork Sources",
                "Artwork is searched in this order:\n\n"
                "1. MusicBrainz / Cover Art Archive\n"
                "2. iTunes\n"
                "3. Deezer\n"
                "4. Discogs, only if a token is configured in Options > Discogs Token "
                "or DISCOGS_TOKEN is set for the current user",
            ),
            (
                "Safety",
                "The app only writes folder.jpg files after confirmation, or during the "
                "commit step when Automatic Commits is enabled. It does not "
                "embed artwork, retag audio, rename files, move files, or modify audio tracks.",
            ),
        ]

    def _help_text(self) -> str:
        return "\n\n".join(f"{heading}\n{body}" for heading, body in self._help_sections())

    def _show_about(self) -> None:
        messagebox.showinfo(
            "About FetchFolderArt",
            "FetchFolderArt by devphaZe foundry.\n\nVersion 1.0, 2026.",
        )

    def _parse_limit(self) -> int | None:
        text = self.limit.get().strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError("Limit must be blank or a whole number.") from exc
        if value < 1:
            raise ValueError("Limit must be 1 or greater.")
        return value

    def _settings(self) -> dict[str, object]:
        roots = self._parse_music_roots()
        log_dir = Path(self.log_dir.get().strip() or str(DATA_DIR)).expanduser()
        limit = self._parse_limit()

        if not roots:
            raise ValueError("Choose at least one music folder.")
        for root in roots:
            if not root.exists() or not root.is_dir():
                raise ValueError(f"Music folder does not exist:\n{root}")
        log_dir.mkdir(parents=True, exist_ok=True)

        return {
            "root": roots[0],
            "roots": roots,
            "log_path": log_dir / "art_fetch_log.csv",
            "dry_run": True,
            "automatic_commits": self.automatic_commits.get(),
            "force": self.force.get(),
            "limit": limit,
            "discogs_token": core.get_discogs_token(),
        }

    def _preview_first(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Fetch Folder Art", "A run is already in progress.")
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            messagebox.showerror("Fetch Folder Art", str(exc))
            return
        settings["root"] = settings["roots"][0]
        self._clear_preview()
        self._start_worker(preview_only=True, settings=settings)

    def _start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            messagebox.showerror("Fetch Folder Art", str(exc))
            return
        self.batch_roots = list(settings["roots"])
        self.batch_index = 0
        self._clear_preview()
        self._start_batch_folder(settings, clear_output=True)

    def _start_batch_folder(self, base_settings: dict[str, object], *, clear_output: bool) -> None:
        if self.batch_index >= len(self.batch_roots):
            self.status.set("All selected folders complete.")
            self._append_log("\nAll selected folders complete.\n")
            self._finish_worker()
            return

        root = self.batch_roots[self.batch_index]
        settings = dict(base_settings)
        settings["root"] = root
        settings["roots"] = list(self.batch_roots)
        settings["batch_index"] = self.batch_index
        settings["batch_total"] = len(self.batch_roots)
        settings["dry_run"] = True
        settings["prompt_to_commit"] = True
        settings["batch_header"] = (
            f"\n=== Folder {self.batch_index + 1} of {len(self.batch_roots)}: {root} ===\n"
        )
        self._clear_preview()
        self._start_worker(preview_only=False, settings=settings, clear_output=clear_output)

    def _start_worker(
        self,
        *,
        preview_only: bool,
        settings: dict[str, object],
        clear_output: bool = True,
    ) -> None:
        self.stop_requested.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set("Working...")
        if clear_output:
            self._clear_output()
        if settings.get("batch_header"):
            self._append_log(str(settings["batch_header"]))
        self._append_log("Starting preview...\n" if preview_only else "Starting scan...\n")

        self.worker = threading.Thread(
            target=self._worker_main,
            args=(settings, preview_only),
            daemon=True,
        )
        self.worker.start()

    def _stop(self) -> None:
        self.stop_requested.set()
        self.status.set("Stopping after the current folder...")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(
                "Fetch Folder Art",
                "A scan is still running. Stop it and close the app?",
            ):
                return
            self.stop_requested.set()
        cleanup_temporary_files()
        self.destroy()

    def _worker_main(self, settings: dict[str, object], preview_only: bool) -> None:
        log_writer = QueueLogWriter(self.work_queue)
        with contextlib.redirect_stdout(log_writer), contextlib.redirect_stderr(log_writer):
            self._worker_main_impl(settings, preview_only)

    def _worker_main_impl(self, settings: dict[str, object], preview_only: bool) -> None:
        try:
            if not core.load_dependencies():
                self.work_queue.put(
                    (
                        "error",
                        f"Missing Python dependencies. Run pip install -r {APP_DIR / 'requirements.txt'}.",
                    )
                )
                return

            cache_manifest = None
            if not preview_only and settings["dry_run"]:
                self._reset_dry_run_cache(settings)
                self.work_queue.put(("log", f"Matched Results cache: {CACHE_DIR}\n"))
            elif not preview_only and not settings["dry_run"]:
                cache_manifest = self._load_matching_cache(settings)
                if cache_manifest is not None:
                    self._apply_cached_dry_run(settings, cache_manifest)
                    return
                self.work_queue.put(("log", "No matching completed Matched Results cache found; scanning normally.\n"))

            session = core.requests.Session()
            session.headers.update(
                {"User-Agent": core.DEFAULT_USER_AGENT, "Accept": "application/json, image/*"}
            )
            rate_limiter = core.MusicBrainzRateLimiter()

            root = settings["root"]
            limit = settings["limit"]
            if preview_only and limit is None:
                limit = 25
            self.work_queue.put(("log", f"Scanning {root}\n"))
            album_folders = self._scan_album_folders(root, limit)
            self.work_queue.put(("log", f"Found {len(album_folders)} album folder(s).\n"))

            if not album_folders:
                message = "No album folders with supported audio files were found."
                if settings.get("batch_total"):
                    self.work_queue.put(("batch_next", {"message": message, "settings": settings}))
                else:
                    self.work_queue.put(("done", message))
                return

            counts: Counter[str] = Counter()
            stopped = False
            for index, album_folder in enumerate(album_folders, start=1):
                if self.stop_requested.is_set():
                    self.work_queue.put(("log", "Stopped by user.\n"))
                    stopped = True
                    break

                total = len(album_folders)
                self.work_queue.put(("status", f"{index}/{total}: {album_folder.path}"))
                status = self._process_album_folder(
                    album_folder,
                    session=session,
                    rate_limiter=rate_limiter,
                    log_path=settings["log_path"],
                    dry_run=True if preview_only else settings["dry_run"],
                    force=settings["force"],
                    preview_only=preview_only,
                    discogs_token=str(settings.get("discogs_token", "")),
                )
                counts[status] += 1
                if preview_only and status == "preview":
                    break

            summary = ", ".join(
                f"{core.display_status(name)}={count}" for name, count in sorted(counts.items())
            )
            if not preview_only and settings["dry_run"] and not stopped:
                self._mark_dry_run_cache_complete(counts)
            message = f"Done. {summary or 'Nothing processed.'}"
            if (
                not preview_only
                and settings["dry_run"]
                and not stopped
                and settings.get("prompt_to_commit")
            ):
                self.work_queue.put(("commit_prompt", {"message": message, "settings": settings}))
            else:
                self.work_queue.put(("done", message))
        except Exception as exc:
            self.work_queue.put(("error", str(exc)))

    def _scan_album_folders(self, root: Path, limit: int | None) -> list[core.AlbumFolder]:
        album_folders: list[core.AlbumFolder] = []
        for dirpath, _dirnames, filenames in os.walk(root):
            folder = Path(dirpath)
            audio_files = [folder / name for name in filenames if core.is_audio_file(Path(name))]
            if audio_files:
                album_folders.append(core.AlbumFolder(path=folder, audio_files=audio_files))
                if limit is not None and len(album_folders) >= limit:
                    break
        return album_folders

    def _process_album_folder(
        self,
        album_folder: core.AlbumFolder,
        *,
        session: object,
        rate_limiter: core.MusicBrainzRateLimiter,
        log_path: Path,
        dry_run: bool,
        force: bool,
        preview_only: bool,
        discogs_token: str,
    ) -> str:
        folder = album_folder.path
        target = folder / "folder.jpg"
        self.work_queue.put(("log", f"\n{folder}\n"))

        existing_art = core.existing_art_files(folder)
        if existing_art and not force:
            names = ", ".join(path.name for path in existing_art)
            self.work_queue.put(("log", f"  skipped; existing artwork found: {names}\n"))
            core.log_result(log_path, folder, "skipped_existing_art", message=names)
            return "skipped"

        metadata = core.read_album_metadata(album_folder)
        if not metadata.album:
            message = "no album name from tags or folder"
            self.work_queue.put(("log", f"  skipped; {message}\n"))
            core.log_result(log_path, folder, "skipped_missing_metadata", metadata, message=message)
            return "skipped"

        self.work_queue.put(
            (
                "log",
                f"  album: {metadata.artist or '(unknown artist)'} - {metadata.album}"
                f"{f' ({metadata.year})' if metadata.year else ''}\n",
            )
        )

        artwork, jpeg_bytes, last_error = core.find_artwork(
            session,
            metadata,
            timeout=DEFAULT_TIMEOUT,
            retries=DEFAULT_RETRIES,
            candidates=DEFAULT_CANDIDATES,
            min_score=DEFAULT_MIN_SCORE,
            rate_limiter=rate_limiter,
            discogs_token=discogs_token,
        )
        if artwork is None:
            message = f"NO_ART_FOUND: {last_error or 'no artwork found'}"
            self.work_queue.put(("log", f"  {message}\n"))
            core.log_result(log_path, folder, "NO_ART_FOUND", metadata, message=message)
            return "not_found"

        self.work_queue.put(
            (
                "log",
                f"  selected {artwork.source}: {artwork.matched_artist} - "
                f"{artwork.matched_album} (score {artwork.score})\n",
            )
        )
        preview_text = (
            f"{metadata.artist or '(unknown artist)'} - {metadata.album}\n"
            f"{folder}\n"
            f"Source: {artwork.source}"
            f"{f' | MBID: {artwork.mbid}' if artwork.mbid else ''}"
        )
        self.work_queue.put(("preview", (jpeg_bytes, preview_text)))

        if preview_only:
            core.log_result(
                log_path,
                folder,
                "preview",
                metadata,
                artwork_source=artwork.source,
                mbid=artwork.mbid,
                image_url=artwork.image_url,
                message=f"previewed {target}",
            )
            self.work_queue.put(("log", f"  preview loaded for {target}\n"))
            return "preview"

        if dry_run:
            message = f"would write {target} from {artwork.source}"
            self.work_queue.put(("log", f"  MATCHED RESULT: {message}\n"))
            self._cache_dry_run_result(
                folder=folder,
                metadata=metadata,
                artwork=artwork,
                jpeg_bytes=jpeg_bytes,
            )
            core.log_result(
                log_path,
                folder,
                "matched_result",
                metadata,
                artwork_source=artwork.source,
                mbid=artwork.mbid,
                image_url=artwork.image_url,
                message=message,
            )
            return "matched_result"

        try:
            with target.open("wb") as handle:
                handle.write(jpeg_bytes)
        except OSError as exc:
            message = f"could not write folder.jpg: {exc}"
            self.work_queue.put(("log", f"  {message}\n"))
            core.log_result(
                log_path,
                folder,
                "write_failed",
                metadata,
                artwork_source=artwork.source,
                mbid=artwork.mbid,
                image_url=artwork.image_url,
                message=message,
            )
            return "error"

        message = f"saved {target.name} from {artwork.source} ({len(jpeg_bytes):,} bytes)"
        self.work_queue.put(("log", f"  {message}\n"))
        core.log_result(
            log_path,
            folder,
            "saved",
            metadata,
            artwork_source=artwork.source,
            mbid=artwork.mbid,
            image_url=artwork.image_url,
            message=message,
        )
        return "saved"

    def _settings_signature(self, settings: dict[str, object]) -> dict[str, object]:
        return {
            "version": CACHE_VERSION,
            "root": str(settings["root"]),
            "force": bool(settings["force"]),
            "limit": settings["limit"],
            "candidates": DEFAULT_CANDIDATES,
            "min_score": DEFAULT_MIN_SCORE,
            "discogs_configured": bool(settings.get("discogs_token")),
        }

    def _reset_dry_run_cache(self, settings: dict[str, object]) -> None:
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        CACHE_COVERS_DIR.mkdir(parents=True, exist_ok=True)
        manifest = {
            "signature": self._settings_signature(settings),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "complete": False,
            "entries": [],
        }
        self._write_cache_manifest(manifest)

    def _read_cache_manifest(self) -> dict[str, object] | None:
        try:
            with CACHE_MANIFEST.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_cache_manifest(self, manifest: dict[str, object]) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        temp_path = CACHE_MANIFEST.with_suffix(".tmp")
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
        temp_path.replace(CACHE_MANIFEST)

    def _cache_dry_run_result(
        self,
        *,
        folder: Path,
        metadata: core.AlbumMetadata,
        artwork: core.ArtworkResult,
        jpeg_bytes: bytes,
    ) -> None:
        manifest = self._read_cache_manifest()
        if manifest is None:
            return

        entries = manifest.get("entries")
        if not isinstance(entries, list):
            entries = []
            manifest["entries"] = entries

        image_name = f"{len(entries) + 1:05d}.jpg"
        image_path = CACHE_COVERS_DIR / image_name
        try:
            CACHE_COVERS_DIR.mkdir(parents=True, exist_ok=True)
            image_path.write_bytes(jpeg_bytes)
        except OSError as exc:
            self.work_queue.put(("log", f"  cache write failed: {exc}\n"))
            return

        entries.append(
            {
                "folder": str(folder),
                "target": str(folder / "folder.jpg"),
                "image_file": image_name,
                "album": metadata.album,
                "artist": metadata.artist,
                "year": metadata.year,
                "metadata_source": metadata.source,
                "artwork_source": artwork.source,
                "mbid": artwork.mbid,
                "image_url": artwork.image_url,
                "bytes": len(jpeg_bytes),
            }
        )
        self._write_cache_manifest(manifest)

    def _mark_dry_run_cache_complete(self, counts: Counter[str]) -> None:
        manifest = self._read_cache_manifest()
        if manifest is None:
            return
        manifest["complete"] = True
        manifest["completed_at"] = datetime.now().isoformat(timespec="seconds")
        manifest["summary"] = dict(counts)
        entries = manifest.get("entries")
        entry_count = len(entries) if isinstance(entries, list) else 0
        self._write_cache_manifest(manifest)
        self.work_queue.put(("log", f"Cached {entry_count} Matched Results for commit.\n"))

    def _load_matching_cache(self, settings: dict[str, object]) -> dict[str, object] | None:
        manifest = self._read_cache_manifest()
        if manifest is None:
            return None
        if manifest.get("complete") is not True:
            return None
        if manifest.get("signature") != self._settings_signature(settings):
            return None
        entries = manifest.get("entries")
        if not isinstance(entries, list) or not entries:
            return None
        return manifest

    def _apply_cached_dry_run(
        self,
        settings: dict[str, object],
        manifest: dict[str, object],
    ) -> None:
        entries = manifest.get("entries", [])
        if not isinstance(entries, list):
            self.work_queue.put(("done", "Cached Matched Results were not readable."))
            return

        self.work_queue.put(
            (
                "log",
                f"Using cached Matched Results from {manifest.get('completed_at', 'previous run')}.\n"
                f"Writing {len(entries)} folder.jpg file(s) without rescanning.\n",
            )
        )
        counts: Counter[str] = Counter()
        log_path = settings["log_path"]

        for index, entry in enumerate(entries, start=1):
            if self.stop_requested.is_set():
                self.work_queue.put(("log", "Stopped by user.\n"))
                break
            if not isinstance(entry, dict):
                counts["error"] += 1
                continue

            target = Path(str(entry.get("target", "")))
            folder = Path(str(entry.get("folder", target.parent)))
            image_file = CACHE_COVERS_DIR / str(entry.get("image_file", ""))
            self.work_queue.put(("status", f"{index}/{len(entries)}: {target}"))

            if not image_file.exists():
                message = f"cached image missing: {image_file}"
                self.work_queue.put(("log", f"\n{folder}\n  {message}\n"))
                core.log_result(Path(log_path), folder, "cache_missing", message=message)
                counts["error"] += 1
                continue

            if target.exists() and not settings["force"]:
                message = f"skipped; {target.name} already exists"
                self.work_queue.put(("log", f"\n{folder}\n  {message}\n"))
                core.log_result(Path(log_path), folder, "skipped_existing_art", message=message)
                counts["skipped"] += 1
                continue

            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(image_file, target)
            except OSError as exc:
                message = f"could not write folder.jpg: {exc}"
                self.work_queue.put(("log", f"\n{folder}\n  {message}\n"))
                core.log_result(Path(log_path), folder, "write_failed", message=message)
                counts["error"] += 1
                continue

            metadata = core.AlbumMetadata(
                album=str(entry.get("album", "")),
                artist=str(entry.get("artist", "")),
                year=str(entry.get("year", "")),
                source=str(entry.get("metadata_source", "cache")),
            )
            message = f"saved {target.name} from Matched Results cache ({image_file.stat().st_size:,} bytes)"
            self.work_queue.put(("log", f"\n{folder}\n  {message}\n"))
            core.log_result(
                Path(log_path),
                folder,
                "saved_from_cache",
                metadata,
                artwork_source=str(entry.get("artwork_source", "")),
                mbid=str(entry.get("mbid", "")),
                image_url=str(entry.get("image_url", "")),
                message=message,
            )
            counts["saved"] += 1

        summary = ", ".join(
            f"{core.display_status(name)}={count}" for name, count in sorted(counts.items())
        )
        message = f"Done from Matched Results cache. {summary or 'Nothing written.'}"
        if settings.get("batch_total"):
            self.work_queue.put(("batch_next", {"message": message, "settings": settings}))
        else:
            self.work_queue.put(("done", message))

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.work_queue.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "status":
                    self.status.set(str(payload))
                elif kind == "preview":
                    image_bytes, text = payload
                    self._add_preview(image_bytes, str(text))
                elif kind == "done":
                    self.status.set(str(payload))
                    self._append_log(f"\n{payload}\n")
                    self._finish_worker()
                elif kind == "commit_prompt":
                    payload_dict = payload if isinstance(payload, dict) else {}
                    message = str(payload_dict.get("message", "Matched Results complete."))
                    settings = payload_dict.get("settings")
                    self.status.set(message)
                    self._append_log(f"\n{message}\n")
                    self._finish_worker()
                    if isinstance(settings, dict):
                        self._prompt_to_commit(settings)
                elif kind == "batch_next":
                    payload_dict = payload if isinstance(payload, dict) else {}
                    message = str(payload_dict.get("message", "Folder complete."))
                    settings = payload_dict.get("settings")
                    self.status.set(message)
                    self._append_log(f"\n{message}\n")
                    self._finish_worker()
                    if isinstance(settings, dict):
                        self._advance_to_next_batch_folder(settings)
                elif kind == "error":
                    self.status.set("Error")
                    self._append_log(f"\nError: {payload}\n")
                    messagebox.showerror("Fetch Folder Art", str(payload))
                    self._finish_worker()
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _finish_worker(self) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

    def _prompt_to_commit(self, settings: dict[str, object]) -> None:
        manifest = self._load_matching_cache(settings)
        entries = manifest.get("entries", []) if manifest else []
        cached_count = len(entries) if isinstance(entries, list) else 0
        if cached_count < 1:
            self._append_log("\nNo folder.jpg changes were found to commit.\n")
            self._advance_to_next_batch_folder(settings)
            return

        if settings.get("automatic_commits"):
            self._append_log(
                f"\nAutomatic commits enabled. Committing {cached_count} cached folder.jpg change(s).\n"
            )
        elif not messagebox.askyesno(
            "Commit Artwork",
            f"Matched Results found {cached_count} folder.jpg change(s). Commit them now?",
        ):
            self.status.set("Matched Results complete. Changes were not committed.")
            self._append_log("\nCommit skipped by user.\n")
            self._advance_to_next_batch_folder(settings)
            return

        commit_settings = dict(settings)
        commit_settings["dry_run"] = False
        commit_settings["prompt_to_commit"] = False
        self.status.set("Committing changes...")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self._append_log("\nCommitting cached folder.jpg changes...\n")
        self.worker = threading.Thread(
            target=self._worker_main,
            args=(commit_settings, False),
            daemon=True,
        )
        self.worker.start()

    def _advance_to_next_batch_folder(self, settings: dict[str, object]) -> None:
        if not settings.get("batch_total"):
            return
        self.batch_roots = list(settings.get("roots", self.batch_roots))
        self.batch_index = int(settings.get("batch_index", self.batch_index)) + 1
        if self.batch_index >= len(self.batch_roots):
            self.status.set("All selected folders complete.")
            self._append_log("\nAll selected folders complete.\n")
            self._finish_worker()
            return
        self._start_batch_folder(settings, clear_output=False)

    def _append_log(self, text: str) -> None:
        for line in text.splitlines(keepends=True):
            tag = self._log_tag_for_line(line)
            if tag:
                self.log_text.insert("end", line, tag)
            else:
                self.log_text.insert("end", line)
        self.log_text.see("end")

    def _log_tag_for_line(self, line: str) -> str:
        text = line.casefold()
        if "no_art_found" in text or "no artwork found" in text:
            return "no_art"
        if "error:" in text or "unexpected error" in text or "write_failed" in text:
            return "error"
        return ""

    def _clear_output(self) -> None:
        self.log_text.delete("1.0", "end")

    def _clear_preview(self) -> None:
        self.preview_photo = None
        self.preview_items = []
        self.preview_index = -1
        self.preview_label.configure(image="", text="No cover loaded")
        self.preview_info.set("")
        self._update_preview_nav()

    def _add_preview(self, image_bytes: bytes, text: str) -> None:
        self.preview_items.append((image_bytes, text))
        self.preview_index = len(self.preview_items) - 1
        self._show_preview_index()

    def _previous_preview(self) -> None:
        if self.preview_index > 0:
            self.preview_index -= 1
            self._show_preview_index()

    def _next_preview(self) -> None:
        if self.preview_index < len(self.preview_items) - 1:
            self.preview_index += 1
            self._show_preview_index()

    def _show_preview_index(self) -> None:
        if self.preview_index < 0 or self.preview_index >= len(self.preview_items):
            self.preview_photo = None
            self.preview_label.configure(image="", text="No cover loaded")
            self.preview_info.set("")
            self._update_preview_nav()
            return

        image_bytes, text = self.preview_items[self.preview_index]
        with Image.open(io.BytesIO(image_bytes)) as image:
            image.thumbnail((340, 340))
            self.preview_photo = ImageTk.PhotoImage(image.copy())
        self.preview_label.configure(image=self.preview_photo, text="")
        self.preview_info.set(text)
        self._update_preview_nav()

    def _update_preview_nav(self) -> None:
        total = len(self.preview_items)
        current = self.preview_index + 1 if self.preview_index >= 0 else 0
        if hasattr(self, "preview_position"):
            self.preview_position.set(f"{current} / {total}")
        if hasattr(self, "prev_preview_button"):
            self.prev_preview_button.configure(state="normal" if current > 1 else "disabled")
        if hasattr(self, "next_preview_button"):
            self.next_preview_button.configure(state="normal" if current < total else "disabled")


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_temporary_files()
    if not core.load_dependencies():
        messagebox.showerror(
            "Fetch Folder Art",
            f"Missing Python dependencies. Run: python -m pip install -r {APP_DIR / 'requirements.txt'}",
        )
        return 2
    app = FolderArtGui()
    app.mainloop()
    cleanup_temporary_files()
    return 0


def cleanup_temporary_files() -> None:
    for path in (CACHE_DIR, LEGACY_TEMP_CACHE_DIR, APP_DIR / "__pycache__", PACKAGE_DIR / "__pycache__"):
        shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
