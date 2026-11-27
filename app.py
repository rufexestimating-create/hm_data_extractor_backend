"""
HM Data Extractor — GUI Version
Double-click or run: python app.py
A window opens → paste directory URL → click Extract → data saved to CSV.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import threading
import asyncio
import os
import re
import sys
import json
import hashlib

def _suppress_playwright_cleanup_errors(unraisable):
    msg = str(unraisable.exc_value) if unraisable.exc_value else ""
    if "closed pipe" in msg or "unclosed transport" in str(unraisable.object):
        return
    sys.__unraisablehook__(unraisable)

sys.unraisablehook = _suppress_playwright_cleanup_errors
import time
import subprocess
import logging

logging.basicConfig(
    filename=os.path.join(os.path.dirname(__file__), "output", "debug.log"),
    level=logging.DEBUG,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    force=True,
)

from browser import PageFetcher
from directory_parser import find_listing_links, houzz_page_url, click_pagination_selector
import smart_directory as sd
from extractor import extract_all
from exporter import save_csv, save_excel
from website_crawler import enrich_records_with_websites
import config


class HMExtractorApp:
    # ------------------------------------------------------------------
    # Theme colors
    # ------------------------------------------------------------------
    THEMES = {
        "dark": {
            "bg": "#0d1117",
            "card": "#161b22",
            "card_hover": "#1c2129",
            "card_border": "#21262d",
            "input_bg": "#0d1117",
            "input_fg": "#e6edf3",
            "input_border": "#30363d",
            "placeholder": "#484f58",
            "accent": "#00c9a7",
            "accent_hover": "#00b396",
            "accent_glow": "#00c9a720",
            "accent_text": "#ffffff",
            "danger": "#f85149",
            "danger_hover": "#da3633",
            "success": "#00c9a7",
            "warning": "#d29922",
            "text": "#e6edf3",
            "text_dim": "#7d8590",
            "log_bg": "#010409",
            "log_fg": "#b1bac4",
            "mode_fast_fg": "#3fb950",
            "mode_deep_fg": "#58a6ff",
            "progress_bg": "#21262d",
            "progress_fg": "#00c9a7",
            "toggle_track_off": "#30363d",
            "toggle_track_on": "#00c9a7",
            "btn_disabled": "#21262d",
            "btn_disabled_fg": "#484f58",
        },
        "light": {
            "bg": "#f6f8fa",
            "card": "#ffffff",
            "card_hover": "#f3f6fa",
            "card_border": "#d0d7de",
            "input_bg": "#ffffff",
            "input_fg": "#1f2328",
            "input_border": "#d0d7de",
            "placeholder": "#6e7781",
            "accent": "#00a88a",
            "accent_hover": "#009478",
            "accent_glow": "#00a88a20",
            "accent_text": "#ffffff",
            "danger": "#cf222e",
            "danger_hover": "#a40e26",
            "success": "#00a88a",
            "warning": "#9a6700",
            "text": "#1f2328",
            "text_dim": "#656d76",
            "log_bg": "#ffffff",
            "log_fg": "#1f2328",
            "mode_fast_fg": "#1a7f37",
            "mode_deep_fg": "#0969da",
            "progress_bg": "#d0d7de",
            "progress_fg": "#00a88a",
            "toggle_track_off": "#6e7781",
            "toggle_track_on": "#00a88a",
            "btn_disabled": "#d0d7de",
            "btn_disabled_fg": "#656d76",
        },
    }

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("HM Data Extractor")
        self.root.geometry("800x640")
        self.root.resizable(True, True)
        self.root.minsize(700, 540)

        self._theme = "dark"
        self.is_running = False
        self.stop_event = threading.Event()
        self._last_save = 0.0
        self._hover_bindings = []

        self._build_ui()
        self._apply_theme()

        # Center window
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - 800) // 2
        y = (self.root.winfo_screenheight() - 640) // 2
        self.root.geometry(f"800x640+{x}+{y}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    @property
    def _c(self):
        return self.THEMES[self._theme]

    # ------------------------------------------------------------------
    # Toggle switch (canvas)
    # ------------------------------------------------------------------

    def _draw_toggle(self):
        t = self._toggle
        t.delete("all")
        c = self._c
        w, h = 46, 24
        r = h // 2
        is_light = self._theme == "light"
        track = c["toggle_track_on"] if is_light else c["toggle_track_off"]

        t.create_oval(0, 0, h, h, fill=track, outline="")
        t.create_oval(w - h, 0, w, h, fill=track, outline="")
        t.create_rectangle(r, 0, w - r, h, fill=track, outline="")

        kx = w - h + 3 if is_light else 3
        ks = h - 6
        t.create_oval(kx, 3, kx + ks, 3 + ks, fill="#ffffff", outline="")

    def _animate_toggle(self, step=0, total=6):
        """Slide the knob across the toggle track."""
        if step > total:
            self._apply_theme()
            self._draw_toggle()
            return
        t = self._toggle
        t.delete("all")
        c = self._c
        w, h = 46, 24
        r = h // 2
        is_light = self._theme == "light"

        frac = step / total
        old_c = self.THEMES["dark" if is_light else "light"]
        new_c = c
        track = new_c["toggle_track_on"] if is_light else new_c["toggle_track_off"]

        t.create_oval(0, 0, h, h, fill=track, outline="")
        t.create_oval(w - h, 0, w, h, fill=track, outline="")
        t.create_rectangle(r, 0, w - r, h, fill=track, outline="")

        start_x = 3 if is_light else w - h + 3
        end_x = w - h + 3 if is_light else 3
        kx = start_x + (end_x - start_x) * frac
        ks = h - 6
        t.create_oval(kx, 3, kx + ks, 3 + ks, fill="#ffffff", outline="")

        self.root.after(20, self._animate_toggle, step + 1, total)

    # ------------------------------------------------------------------
    # Hover effects
    # ------------------------------------------------------------------

    def _add_hover(self, widget, normal_bg, hover_bg):
        def on_enter(e):
            if str(widget["state"]) != "disabled":
                widget.config(bg=hover_bg)
        def on_leave(e):
            if str(widget["state"]) != "disabled":
                widget.config(bg=normal_bg)
        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)
        self._hover_bindings.append((widget, normal_bg, hover_bg))

    def _refresh_hovers(self):
        """Re-bind hover colors after a theme change."""
        c = self._c
        hover_map = {
            id(self.extract_btn): (c["accent"], c["accent_hover"]),
            id(self.stop_btn): (c["card_border"], c["danger"]),
            id(self.open_btn): (c["card_border"], c["card_hover"]),
        }
        new_bindings = []
        for widget, _, _ in self._hover_bindings:
            wid = id(widget)
            if wid in hover_map:
                n, h = hover_map[wid]
                widget.unbind("<Enter>")
                widget.unbind("<Leave>")
                self._hover_bindings = []
                self._add_hover(widget, n, h)
        # _add_hover appends, so new_bindings handled there

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        """Build the full GUI layout."""
        style = ttk.Style()
        style.theme_use("clam")

        self._main = tk.Frame(self.root)
        self._main.pack(fill="both", expand=True)

        # ---- Header (centered title, toggle floating top-right) ----
        self._header = tk.Frame(self._main)
        self._header.pack(fill="x", pady=(24, 0))

        self._title_label = tk.Label(self._header, text="HM Data Extractor",
                                     font=("Segoe UI", 22, "bold"))
        self._title_label.pack()

        self._subtitle = tk.Label(self._header,
                                   text="Extract business data from any directory page",
                                   font=("Segoe UI", 11))
        self._subtitle.pack(pady=(4, 16))

        # Toggle switch (floating top-right)
        toggle_area = tk.Frame(self._main)
        toggle_area.place(relx=1.0, y=20, anchor="ne", x=-28)
        self._toggle_area = toggle_area

        self._dark_icon = tk.Label(toggle_area, text="☾",
                                    font=("Segoe UI", 11))
        self._dark_icon.pack(side="left", padx=(0, 5))

        self._toggle = tk.Canvas(toggle_area, width=46, height=24,
                                  bd=0, highlightthickness=0, cursor="hand2")
        self._toggle.pack(side="left")
        self._toggle.bind("<Button-1>", lambda e: self._toggle_theme())

        self._light_icon = tk.Label(toggle_area, text="☀",
                                     font=("Segoe UI", 11))
        self._light_icon.pack(side="left", padx=(5, 0))

        # ---- URL Card ----
        self._url_card = tk.Frame(self._main, bd=0, highlightthickness=1)
        self._url_card.pack(fill="x", padx=32, pady=(0, 10))

        url_inner = tk.Frame(self._url_card)
        url_inner.pack(fill="x", padx=18, pady=16)

        self._url_label = tk.Label(url_inner, text="DIRECTORY URL",
                                    font=("Segoe UI", 8, "bold"))
        self._url_label.pack(anchor="w")

        url_input_frame = tk.Frame(url_inner, highlightthickness=1)
        url_input_frame.pack(fill="x", pady=(8, 0))
        self._url_input_frame = url_input_frame

        self.url_entry = tk.Entry(url_input_frame, font=("Segoe UI", 11),
                                  relief="flat", bd=0)
        self.url_entry.pack(fill="x", ipady=10, padx=10)
        self._url_placeholder = "Paste directory URL here..."
        self.url_entry.insert(0, self._url_placeholder)
        self.url_entry.bind("<FocusIn>", self._on_url_focus_in)
        self.url_entry.bind("<FocusOut>", self._on_url_focus_out)

        # ---- Options Card ----
        self._opt_card = tk.Frame(self._main, bd=0, highlightthickness=1)
        self._opt_card.pack(fill="x", padx=32, pady=(0, 14))

        opt_inner = tk.Frame(self._opt_card)
        opt_inner.pack(fill="x", padx=18, pady=14)

        self._opt_label = tk.Label(opt_inner, text="EXTRACTION MODE",
                                    font=("Segoe UI", 8, "bold"))
        self._opt_label.pack(anchor="w", pady=(0, 8))

        mode_row = tk.Frame(opt_inner)
        mode_row.pack(fill="x")

        # Fast mode
        fast_frame = tk.Frame(mode_row)
        fast_frame.pack(side="left")

        self.fast_mode_var = tk.BooleanVar(value=True)
        self.fast_check = tk.Checkbutton(
            fast_frame, text="  Fast Mode",
            variable=self.fast_mode_var, font=("Segoe UI", 10, "bold"),
            bd=0, highlightthickness=0, command=self._on_fast_toggle
        )
        self.fast_check.pack(side="left")

        self._fast_desc = tk.Label(fast_frame, text="Name • Website • Email • Phone",
                                    font=("Segoe UI", 8))
        self._fast_desc.pack(side="left", padx=(4, 0))

        # Separator
        self._mode_sep = tk.Frame(mode_row, width=1, height=20)
        self._mode_sep.pack(side="left", padx=16, fill="y")

        # Deep mode
        deep_frame = tk.Frame(mode_row)
        deep_frame.pack(side="left")

        self.deep_mode_var = tk.BooleanVar(value=False)
        self.deep_check = tk.Checkbutton(
            deep_frame, text="  Deep Mode",
            variable=self.deep_mode_var, font=("Segoe UI", 10, "bold"),
            bd=0, highlightthickness=0, command=self._on_deep_toggle
        )
        self.deep_check.pack(side="left")

        self._deep_desc = tk.Label(deep_frame, text="All Data + Website Crawl",
                                    font=("Segoe UI", 8))
        self._deep_desc.pack(side="left", padx=(4, 0))

        # Pages
        pages_frame = tk.Frame(mode_row)
        pages_frame.pack(side="right")

        self._pages_label = tk.Label(pages_frame, text="Pages",
                                      font=("Segoe UI", 9, "bold"))
        self._pages_label.pack(side="left", padx=(0, 6))

        pages_input = tk.Frame(pages_frame, highlightthickness=1)
        pages_input.pack(side="left")
        self._pages_input_frame = pages_input

        self.pages_entry = tk.Entry(pages_input, font=("Segoe UI", 10), width=5,
                                    relief="flat", bd=0, justify="center")
        self.pages_entry.pack(ipady=4, padx=4)
        self.pages_entry.insert(0, "Max")

        # ---- Action Buttons ----
        btn_frame = tk.Frame(self._main)
        btn_frame.pack(fill="x", padx=32, pady=(0, 8))

        self.extract_btn = tk.Button(
            btn_frame, text="▶  Start Extraction", font=("Segoe UI", 11, "bold"),
            relief="flat", bd=0, padx=28, pady=10, cursor="hand2",
            command=self._on_extract_click
        )
        self.extract_btn.pack(side="left")

        self.stop_btn = tk.Button(
            btn_frame, text="■  Stop & Save", font=("Segoe UI", 11, "bold"),
            relief="flat", bd=0, padx=20, pady=10, cursor="hand2",
            state="disabled", command=self._on_stop_click
        )
        self.stop_btn.pack(side="left", padx=(10, 0))

        self.count_label = tk.Label(btn_frame, text="",
                                    font=("Segoe UI", 11, "bold"))
        self.count_label.pack(side="right")

        # ---- Progress ----
        prog_frame = tk.Frame(self._main)
        prog_frame.pack(fill="x", padx=32, pady=(0, 2))

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var,
                                             maximum=100)
        self.progress_bar.pack(fill="x", ipady=1)

        self.status_label = tk.Label(self._main,
                                     text="Ready — paste a URL and click Start",
                                     font=("Segoe UI", 9))
        self.status_label.pack(anchor="w", padx=34, pady=(4, 2))

        # ---- Log ----
        log_outer = tk.Frame(self._main, highlightthickness=1)
        log_outer.pack(fill="both", expand=True, padx=32, pady=(4, 8))
        self._log_outer = log_outer

        self.log_text = tk.Text(log_outer, height=8, font=("Cascadia Code", 9),
                                relief="flat", bd=0, state="disabled",
                                wrap="word", padx=14, pady=12)
        scrollbar = ttk.Scrollbar(log_outer, orient="vertical", command=self.log_text.yview)
        self.log_text.config(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # ---- Footer ----
        footer = tk.Frame(self._main)
        footer.pack(fill="x", padx=32, pady=(0, 14))

        self.open_btn = tk.Button(
            footer, text="\U0001f4c2  Open Output Folder", font=("Segoe UI", 9),
            relief="flat", bd=0, padx=12, pady=5, cursor="hand2",
            command=self._open_output_folder,
            state="normal" if os.path.exists(config.OUTPUT_DIR) else "disabled"
        )
        self.open_btn.pack(side="left")

        self._credit = tk.Label(footer, text="HM Design Group",
                                 font=("Segoe UI", 8, "italic"))
        self._credit.pack(side="right")

    # ------------------------------------------------------------------
    # Theme engine
    # ------------------------------------------------------------------

    def _apply_theme(self):
        """Apply current theme colors to every widget."""
        c = self._c
        style = ttk.Style()

        self.root.configure(bg=c["bg"])
        self._main.configure(bg=c["bg"])

        # Header
        self._header.configure(bg=c["bg"])
        self._title_label.configure(bg=c["bg"], fg=c["accent"])
        self._subtitle.configure(bg=c["bg"], fg=c["text_dim"])

        # Toggle area
        self._toggle_area.configure(bg=c["bg"])
        self._dark_icon.configure(bg=c["bg"], fg=c["text_dim"])
        self._light_icon.configure(bg=c["bg"], fg=c["text_dim"])
        self._toggle.configure(bg=c["bg"])
        self._draw_toggle()

        # URL card
        self._url_card.configure(bg=c["card"], highlightbackground=c["card_border"])
        for w in self._url_card.winfo_children():
            w.configure(bg=c["card"])
        self._url_label.configure(bg=c["card"], fg=c["text_dim"])
        self._url_input_frame.configure(bg=c["input_bg"],
                                         highlightbackground=c["input_border"])
        self.url_entry.configure(bg=c["input_bg"], fg=c["input_fg"],
                                 insertbackground=c["input_fg"])
        if self.url_entry.get() == self._url_placeholder:
            self.url_entry.configure(fg=c["placeholder"])

        # Options card
        self._opt_card.configure(bg=c["card"], highlightbackground=c["card_border"])
        self._apply_card_bg(self._opt_card, c["card"])
        self._opt_label.configure(bg=c["card"], fg=c["text_dim"])
        self._mode_sep.configure(bg=c["card_border"])

        self.fast_check.configure(
            bg=c["card"], fg=c["mode_fast_fg"], selectcolor=c["card"],
            activebackground=c["card"], activeforeground=c["mode_fast_fg"]
        )
        self._fast_desc.configure(bg=c["card"], fg=c["text_dim"])
        self.deep_check.configure(
            bg=c["card"], fg=c["mode_deep_fg"], selectcolor=c["card"],
            activebackground=c["card"], activeforeground=c["mode_deep_fg"]
        )
        self._deep_desc.configure(bg=c["card"], fg=c["text_dim"])
        self._pages_label.configure(bg=c["card"], fg=c["text_dim"])
        self._pages_input_frame.configure(bg=c["input_bg"],
                                           highlightbackground=c["input_border"])
        self.pages_entry.configure(bg=c["input_bg"], fg=c["input_fg"],
                                    insertbackground=c["input_fg"])

        # Buttons
        btn_parent = self.extract_btn.master
        btn_parent.configure(bg=c["bg"])
        self.extract_btn.configure(
            bg=c["accent"], fg=c["accent_text"],
            activebackground=c["accent_hover"], activeforeground=c["accent_text"]
        )
        self.stop_btn.configure(
            bg=c["card_border"], fg=c["text_dim"],
            activebackground=c["danger"], activeforeground="#ffffff"
        )
        self.count_label.configure(bg=c["bg"], fg=c["success"])

        # Hover effects
        self._hover_bindings.clear()
        self._add_hover(self.extract_btn, c["accent"], c["accent_hover"])
        self._add_hover(self.stop_btn, c["card_border"], c["danger"])
        self._add_hover(self.open_btn, c["card_border"], c["card_hover"])

        # Progress
        self.progress_bar.master.configure(bg=c["bg"])
        style.configure("TProgressbar",
                         troughcolor=c["progress_bg"],
                         background=c["progress_fg"],
                         borderwidth=0, thickness=6)
        self.status_label.configure(bg=c["bg"], fg=c["text_dim"])

        # Log
        self._log_outer.configure(bg=c["card_border"],
                                   highlightbackground=c["card_border"])
        self.log_text.configure(bg=c["log_bg"], fg=c["log_fg"])

        # Footer
        footer = self.open_btn.master
        footer.configure(bg=c["bg"])
        self.open_btn.configure(bg=c["card_border"], fg=c["text_dim"],
                                activebackground=c["card_hover"],
                                activeforeground=c["text"])
        self._credit.configure(bg=c["bg"], fg=c["text_dim"])

    def _apply_card_bg(self, widget, color):
        """Recursively set bg on a card and all its children."""
        try:
            widget.configure(bg=color)
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            self._apply_card_bg(child, color)

    def _toggle_theme(self):
        self._theme = "light" if self._theme == "dark" else "dark"
        self._animate_toggle()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @staticmethod
    def _backup_name(url: str) -> str:
        """Generate a descriptive backup filename from the URL."""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        site = parsed.netloc.replace("www.", "").split(".")[0]
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        desc = "_".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "data")
        desc = re.sub(r"[^a-zA-Z0-9_-]", "_", desc)[:40]
        date = time.strftime("%Y%m%d_%H%M")
        return f"{site}_{desc}_{date}"

    def _on_url_focus_in(self, event):
        if self.url_entry.get() == self._url_placeholder:
            self.url_entry.delete(0, "end")
            self.url_entry.config(fg="#ffffff")

    def _on_url_focus_out(self, event):
        if not self.url_entry.get().strip():
            self.url_entry.insert(0, self._url_placeholder)
            self.url_entry.config(fg="#666666")

    def _on_fast_toggle(self):
        if self.fast_mode_var.get():
            self.deep_mode_var.set(False)
        else:
            self.deep_mode_var.set(True)

    def _on_deep_toggle(self):
        if self.deep_mode_var.get():
            self.fast_mode_var.set(False)
        else:
            self.fast_mode_var.set(True)

    def _log(self, msg: str):
        """Append a message to the log area (thread-safe)."""
        def _do():
            self.log_text.config(state="normal")
            self.log_text.insert("end", msg + "\n")
            self.log_text.see("end")
            self.log_text.config(state="disabled")
        self.root.after(0, _do)

    def _set_status(self, msg: str):
        self.root.after(0, lambda: self.status_label.config(text=msg))

    def _set_progress(self, pct: float):
        self.root.after(0, lambda: self.progress_var.set(pct))

    def _on_extract_click(self):
        url = self.url_entry.get().strip()
        if not url or url == self._url_placeholder:
            messagebox.showwarning("No URL", "Please paste a directory URL first!")
            return
        if not url.startswith("http"):
            messagebox.showwarning("Invalid URL", "URL should start with http:// or https://")
            return

        if self.is_running:
            return

        max_listings = None

        # Parse max pages
        pages_text = self.pages_entry.get().strip()
        max_pages = None
        if pages_text and pages_text.lower() not in ("all", "max"):
            try:
                max_pages = int(pages_text)
            except ValueError:
                messagebox.showwarning("Invalid", "Pages mein number likho ya 'Max' rakho (sab pages ke liye)")
                return

        # Resume a previous stopped run of this same URL?
        fast = self.fast_mode_var.get()
        state = self._load_checkpoint(url, fast)
        if state is not None:
            n = len(state.get("results", []))
            ans = messagebox.askyesnocancel(
                "Continue previous run?",
                f"This URL was stopped earlier with {n} records saved.\n\n"
                "Yes  = continue from where it stopped\n"
                "No   = start again from the beginning\n"
                "Cancel = do nothing")
            if ans is None:
                return
            if not ans:
                self._delete_checkpoint(url, fast)
                state = None
        if state is None:
            state = self._new_state(url, fast)

        self.is_running = True
        self.stop_event.clear()
        c = self._c
        self.extract_btn.config(state="disabled", bg=c["btn_disabled"],
                                fg=c["btn_disabled_fg"])
        self.stop_btn.config(state="normal", bg=c["danger"],
                             fg=c["accent_text"])
        self.open_btn.config(state="disabled")
        self.count_label.config(text="")
        self.progress_var.set(0)

        # Clear log
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

        # Run extraction in a background thread
        thread = threading.Thread(
            target=self._run_extraction,
            args=(state, False, max_listings, self.deep_mode_var.get(), max_pages),
            daemon=True
        )
        thread.start()

    # ------------------------------------------------------------------
    # Stop / Resume (checkpoint saved in output/.progress/)
    # ------------------------------------------------------------------

    @staticmethod
    def _checkpoint_path(url: str, fast: bool) -> str:
        key = hashlib.md5(f"{url}|{'fast' if fast else 'full'}".encode("utf-8")).hexdigest()[:12]
        return os.path.join(config.OUTPUT_DIR, ".progress", f"{key}.json")

    @staticmethod
    def _new_state(url: str, fast: bool) -> dict:
        return {
            "url": url, "fast": fast,
            "mode": None,            # "cards" | "normal"
            "stage": "pages",        # "pages" -> "profiles" -> "websites"
            "page_url": url, "page_num": 1, "seen_pages": [],
            "links": [], "done_links": [], "failed_links": [],
            "results": [],
        }

    def _load_checkpoint(self, url: str, fast: bool) -> dict | None:
        path = self._checkpoint_path(url, fast)
        if not os.path.exists(path):
            return None
        try:
            with open(path, encoding="utf-8") as f:
                state = json.load(f)
            if state.get("url") == url:
                return state
        except Exception:
            pass
        return None

    def _delete_checkpoint(self, url: str, fast: bool):
        try:
            os.remove(self._checkpoint_path(url, fast))
        except OSError:
            pass

    def _save_checkpoint(self, state: dict, force: bool = False, excel: bool = False):
        """Save progress + partial CSV. Throttled to once every 2s unless force=True."""
        now = time.time()
        if not force and now - self._last_save < 2:
            return
        self._last_save = now
        path = self._checkpoint_path(state["url"], state["fast"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
        if state["results"]:
            save_csv(state["results"])
            if excel:
                save_excel(state["results"])

    # ------------------------------------------------------------------
    # Extraction pipeline
    # ------------------------------------------------------------------

    def _run_extraction(self, state: dict, use_browser: bool, max_listings: int | None,
                        deep_crawl: bool = True, max_pages: int | None = None):
        """Runs the full extraction pipeline in a background thread."""
        try:
            asyncio.run(self._run_with_stop(state, use_browser, max_listings, deep_crawl, max_pages))
        except Exception as e:
            self._log(f"\n❌ ERROR: {e}")
            self._set_status(f"Error: {e}")
        finally:
            self.root.after(0, self._extraction_done)

    async def _run_with_stop(self, state: dict, *args):
        """Run the pipeline; cancel it the moment Stop is pressed, then save."""
        task = asyncio.create_task(self._extract_async(state, *args))
        while not task.done():
            if self.stop_event.is_set():
                task.cancel()
                break
            await asyncio.sleep(0.2)
        try:
            await task
        except asyncio.CancelledError:
            self._finish(state, stopped=True)
            return
        except Exception:
            self._save_checkpoint(state, force=True, excel=True)  # keep what we have
            self._log("💾 Progress saved — click Start again to continue.")
            raise
        self._finish(state, stopped=False)

    def _finish(self, state: dict, stopped: bool):
        results = state["results"]
        if stopped:
            self._save_checkpoint(state, force=True, excel=True)
            self._log(f"\n⏹ STOPPED. {len(results)} records saved so far.")
            if results:
                self._log(f"✅ CSV:   {os.path.abspath(os.path.join(config.OUTPUT_DIR, config.DEFAULT_FILENAME + '.csv'))}")
                self._log(f"✅ Excel: {os.path.abspath(os.path.join(config.OUTPUT_DIR, config.DEFAULT_FILENAME + '.xlsx'))}")
            self._log("▶ Click Start again with the same URL to continue from here.")
            self._set_status(f"⏹ Stopped — {len(results)} records saved. Start again to resume.")
            self.root.after(0, lambda: self.count_label.config(text=f"⏹ {len(results)} saved"))
            return

        self._delete_checkpoint(state["url"], state["fast"])
        if not results:
            return

        self._log(f"\n💾 Saving {len(results)} records...")

        # Save main output
        self._log(f"✅ CSV:   {save_csv(results)}")
        self._log(f"✅ Excel: {save_excel(results)}")

        # Save backup copy with descriptive name
        backup_name = self._backup_name(state["url"])
        backup_dir = os.path.join(config.OUTPUT_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        backup_csv = save_csv(results, os.path.join("backups", backup_name))
        self._log(f"📋 Backup: {backup_csv}")

        with_web = sum(1 for r in results if r.get("website"))
        with_email = sum(1 for r in results if r.get("emails"))
        with_phone = sum(1 for r in results if r.get("phones") or r.get("phone"))
        with_lic = sum(1 for r in results if r.get("license"))
        self._log(f"\n📊 Done! {len(results)} records extracted.")
        self._log(f"   🌐 With website: {with_web}")
        from collections import Counter
        for src, n in Counter(r.get("website_source") for r in results if r.get("website")).most_common():
            if src:
                self._log(f"        • {n} from {src}")
        if len(results) - with_web:
            self._log(f"   ➖ No website listed by the directory: {len(results) - with_web}")
        self._log(f"   ✉  With email:   {with_email}")
        self._log(f"   📞 With phone:   {with_phone}")
        if with_lic:
            self._log(f"   📜 With license: {with_lic}")
        if state["failed_links"]:
            self._log(f"   ⚠  Failed: {len(state['failed_links'])}")

        self._set_status(f"✅ Done! {len(results)} records saved to output folder.")
        self._set_progress(100)
        self.root.after(0, lambda: self.count_label.config(text=f"✅ {len(results)} records extracted"))

    async def _fetch_page(self, fetcher, page_url: str, use_browser: bool) -> str | None:
        html = await fetcher.fetch(page_url)
        if not html and not use_browser:
            html = await fetcher.fetch_browser(page_url)
        return html

    @staticmethod
    def _upgrade_state(state: dict):
        """Checkpoints from the older version: give them the new fields."""
        state.setdefault("cards", [])
        state.setdefault("chrome_hosts", [])
        state.setdefault("list_browser", False)
        state.setdefault("profile_browser", None)
        state.setdefault("pb_tries", 0)
        state.setdefault("dynamic", False)
        state.setdefault("links", [])
        if state["links"] and not state["cards"]:
            state["cards"] = [{"key": l, "name": "", "profile_url": l, "website": "", "website_score": 0,
                               "phone": "", "emails": [], "state": ""} for l in state["links"]]
        if state.get("mode") == "cards":
            state["mode"] = "normal"

    async def _extract_async(self, state: dict, use_browser: bool, max_listings: int | None,
                             deep_crawl: bool = True, max_pages: int | None = None):
        url, fast = state["url"], state["fast"]
        self._upgrade_state(state)
        self._log(f"🔗 Target: {url}")
        if state["seen_pages"] or state["results"] or state["cards"]:
            self._log(f"▶ RESUMING — {len(state['results'])} records already saved, continuing from where it stopped.")
        if fast:
            self._log("⚡ FAST MODE: Name, Website, Email, Phone, State")
        self._set_status("Starting directory crawler...")

        is_houzz = "houzz.com" in url
        workers = config.HOUZZ_WORKERS if is_houzz else config.CONCURRENT_WORKERS

        def on_rate_limit(seconds):
            self._log(f"  ⏳ Site says 'Too Many Requests' — pausing {seconds}s, then continuing automatically...")
            self._set_status(f"⏳ Rate limited by site — waiting {seconds}s...")

        async with PageFetcher(use_browser=use_browser, workers=workers, on_rate_limit=on_rate_limit) as fetcher:
            if fast:
                deep_crawl = False  # Fast Mode = website link only, don't open contractor sites
            if deep_crawl:
                self._log("🔍 DEEP CRAWL: will open contractor websites (10 parallel) to extract direct emails")

            if state["stage"] == "pages":
                await self._pages_stage(fetcher, state, use_browser, max_listings, max_pages)
                if not state["cards"]:
                    host = sd.host_of(url)
                    if host in fetcher.browser_hosts:
                        self._log("❌ This site blocks automatic access (bot-check / 'Just a moment...').")
                        self._log("   Open it in your own browser to check; the app does not get past bot-checks.")
                    else:
                        self._log("❌ No companies found on this page.")
                        self._log("   Paste the link of the page that LISTS the companies (category or search")
                        self._log("   results page), not the directory's home page.")
                    self._set_status("No companies found")
                    return
                state["stage"] = "profiles"
                self._save_checkpoint(state, force=True)

            if state["stage"] == "profiles":
                await self._profiles_stage(fetcher, state, fast, use_browser)
                self._fix_shared_websites(state)
                state["stage"] = "websites"
                self._save_checkpoint(state, force=True)

            if deep_crawl:
                await self._websites_stage(state, fetcher)

    # ------------------------------------------------------------------
    # Step 1: list pages -> companies (name, profile link, website on card)
    # ------------------------------------------------------------------

    @staticmethod
    def _norm_name(name: str) -> str:
        return "name:" + re.sub(r"[^a-z0-9]", "", (name or "").lower())

    def _add_cards(self, state: dict, records: list[dict], max_listings: int | None) -> int:
        """Add new companies from one list page. Returns how many were new."""
        keys = self._card_keys
        added = 0
        for r in records:
            name = (r.get("name") or "").strip()
            nkey = self._norm_name(name)
            prof = (r.get("profile_url") or "").split("#")[0]
            key = prof or (("web:" + sd.reg_domain(r["website"]) + "|" + nkey) if r.get("website") else nkey)
            if key in keys or (not prof and nkey in keys and nkey != "name:"):
                if r.get("website"):
                    for c in state["cards"]:          # same company seen again: keep its website
                        if c["key"] == key and not c["website"]:
                            c["website"] = r["website"]
                            c["website_score"] = r.get("website_score", 0)
                            break
                continue
            if max_listings and len(state["cards"]) >= max_listings:
                break
            keys.add(key)
            keys.add(nkey)
            state["cards"].append({
                "key": key, "name": name, "profile_url": prof,
                "website": r.get("website") or "", "website_score": r.get("website_score", 0),
                "phone": r.get("phone") or "", "emails": list(r.get("emails") or []),
                "state": r.get("state") or "",
            })
            added += 1
        return added

    async def _pages_stage(self, fetcher, state: dict, use_browser: bool,
                           max_listings: int | None, max_pages: int | None):
        """Step 1: read every list page and collect the companies (resumable)."""
        self._card_keys = {c["key"] for c in state["cards"]}
        self._card_keys |= {self._norm_name(r.get("name")) for r in state["results"] if r.get("name")}

        if "houzz.com" in state["url"]:
            await self._houzz_pages_stage(fetcher, state, use_browser, max_listings, max_pages, set(state["links"]))
            self._add_cards(state, [{"name": "", "profile_url": l, "website": ""} for l in state["links"]], None)
            return

        click_sel = click_pagination_selector(state["url"])
        if click_sel:
            await self._click_pages_stage(fetcher, state, click_sel, max_listings, max_pages)
            return
        if state.get("dynamic"):
            await self._dynamic_pages_stage(fetcher, state, max_listings, max_pages)
            return

        chrome = set(state["chrome_hosts"])
        while state["page_url"] and state["page_url"] not in state["seen_pages"]:
            page_url, page_num = state["page_url"], state["page_num"]
            if max_listings and len(state["cards"]) >= max_listings:
                self._log(f"  🎯 Reached maximum listings limit ({max_listings}).")
                break
            if max_pages and page_num > max_pages:
                self._log(f"  🎯 Reached maximum page limit ({max_pages}).")
                break

            self._log(f"📄 Reading list page {page_num}: {page_url}")
            self._set_status(f"Reading list page {page_num}...")
            html, via = await fetcher.fetch_ex(page_url, prefer_browser=use_browser or state["list_browser"])
            if not html and via == "http":
                html, via = await fetcher.fetch_browser(page_url), "browser"
            if not html:
                blocked = sd.host_of(page_url) in fetcher.browser_hosts
                self._log(f"  ❌ Page {page_num} did not open" + (" — the site shows a bot-check." if blocked else "."))
                break

            res = await asyncio.to_thread(sd.analyze_list, html, page_url, chrome)
            if not res["records"] and via == "http":
                self._log("  🌐 Companies are loaded by JavaScript here — opening the page in the browser...")
                bhtml = await fetcher.fetch_browser(page_url)
                if bhtml:
                    res2 = await asyncio.to_thread(sd.analyze_list, bhtml, page_url, chrome)
                    if res2["records"]:
                        res, html, via = res2, bhtml, "browser"
                        state["list_browser"] = True

            recs = res["records"]
            chrome |= res["chrome_hosts"]
            state["chrome_hosts"] = sorted(chrome)
            added = self._add_cards(state, recs, max_listings)
            with_web = sum(1 for r in recs if r["website"])
            self._log(f"  ✅ Page {page_num}: {len(recs)} companies ({with_web} show their website on the list), "
                      f"{added} new — total {len(state['cards'])}")
            state["seen_pages"].append(page_url)
            state["page_num"] = page_num + 1

            # ---- where is the next page? ----
            next_url, how = sd.find_next_page(html, page_url, len(recs))
            go_dynamic = False
            total = None
            if page_num == 2 and recs and added == 0:
                # page 2 by URL showed the same companies -> this list changes pages by JavaScript
                go_dynamic, next_url = True, None
                state["page_num"] = page_num
            elif not next_url and recs:
                total = sd.total_hint(html)
                if how == "js" or sd.has_load_more(html) or (total and total > len(state["cards"])):
                    go_dynamic = True
                    state["page_num"] = page_num + 1
            self._log(f"  🔍 Pagination: next_url={bool(next_url)}, how='{how}', total={total}, go_dynamic={go_dynamic}")
            if next_url and next_url not in state["seen_pages"] and (added or page_num == 1):
                state["page_url"] = next_url
            else:
                state["page_url"] = None
            self._save_checkpoint(state, force=True)

            if go_dynamic and not state.get("dynamic_tried"):
                state["dynamic"] = state["dynamic_tried"] = True
                state["page_url"] = state["url"]
                self._log(f"  ➡️ Entering dynamic pagination (browser)...")
                await self._dynamic_pages_stage(fetcher, state, max_listings, max_pages)
                break
            if not state["page_url"]:
                self._log(f"  🏁 Reached the last page (Page {page_num})!")
                break
            await asyncio.sleep(config.DELAY_BETWEEN_REQUESTS)

        if max_listings:
            del state["cards"][max_listings:]
        n_web = sum(1 for c in state["cards"] if c["website"])
        self._log(f"\n🎉 {len(state['cards'])} companies collected "
                  f"({n_web} already show their website on the list page).")

    async def _dynamic_pages_stage(self, fetcher, state: dict, max_listings: int | None, max_pages: int | None):
        """Lists whose next companies appear only by clicking Next / a page number / Load more, or scrolling."""
        chrome = set(state["chrome_hosts"])
        start = max(1, state["page_num"])
        self._log("🖱 The next companies appear only by clicking (Next / page number / Load more) "
                  f"or scrolling — doing that in the browser... (start_page={start})")
        pages = fetcher.iter_dynamic_pages(state["url"], start_page=start)
        stale, last = 0, start - 1
        try:
            async for num, html in pages:
                last = num
                if max_pages and num > max_pages:
                    self._log(f"  🎯 Reached maximum page limit ({max_pages}).")
                    break
                self._set_status(f"Reading list page {num} (browser)...")
                res = await asyncio.to_thread(sd.analyze_list, html, state["url"], chrome)
                chrome |= res["chrome_hosts"]
                state["chrome_hosts"] = sorted(chrome)
                added = self._add_cards(state, res["records"], max_listings)
                self._log(f"📄 Page {num}: {added} new companies (total {len(state['cards'])})")
                state["page_num"] = num + 1
                self._save_checkpoint(state, force=True)
                if max_listings and len(state["cards"]) >= max_listings:
                    self._log(f"  🎯 Reached maximum listings limit ({max_listings}).")
                    break
                stale = stale + 1 if added == 0 else 0
                if stale >= 3:
                    self._log(f"  🛑 3 stale pages in a row — stopping.")
                    break
            else:
                self._log(f"  🏁 Reached the last page (Page {last})!")
        except Exception as e:
            self._log(f"  ❌ Dynamic pagination error: {type(e).__name__}: {e}")
        finally:
            await pages.aclose()
        state["page_url"] = None
        if max_listings:
            del state["cards"][max_listings:]
        self._log(f"\n🎉 {len(state['cards'])} companies collected.")

    async def _click_pages_stage(self, fetcher, state: dict, link_selector: str,
                                 max_listings: int | None, max_pages: int | None):
        """List pages that change only by clicking the page number (e.g. theconstructionusa.com)."""
        if not state["page_url"]:
            return
        chrome = set(state["chrome_hosts"])
        start = state["page_num"]
        self._log("🖱 This directory changes pages by clicking the page number — opening it in the browser...")
        pages = fetcher.iter_click_pages(state["url"], link_selector, start_page=start)
        try:
            async for num, html in pages:
                if max_pages and num > max_pages:
                    self._log(f"  🎯 Reached maximum page limit ({max_pages}).")
                    break
                self._set_status(f"Crawling Directory Page {num}...")
                res = await asyncio.to_thread(sd.analyze_list, html, state["url"], chrome)
                chrome |= res["chrome_hosts"]
                state["chrome_hosts"] = sorted(chrome)
                added = self._add_cards(state, res["records"], max_listings)
                self._log(f"📄 Page {num}: {added} new companies (total {len(state['cards'])})")
                state["page_num"] = num + 1
                self._save_checkpoint(state, force=True)
                if max_listings and len(state["cards"]) >= max_listings:
                    self._log(f"  🎯 Reached maximum listings limit ({max_listings}).")
                    break
            else:
                self._log(f"  🏁 Reached the last page (Page {state['page_num'] - 1})!")
        finally:
            await pages.aclose()
        state["page_url"] = None

        if max_listings:
            del state["cards"][max_listings:]
        self._log(f"\n🎉 Total {len(state['cards'])} companies collected across {state['page_num'] - 1} page(s)!")

    HOUZZ_PAGE_BATCH = 3   # list pages fetched at the same time

    async def _houzz_pages_stage(self, fetcher, state: dict, use_browser: bool,
                                 max_listings: int | None, max_pages: int | None, seen_links: set):
        """Houzz list pages: same URL with fi=0,15,30... fetched 3 at a time."""
        links = state["links"]
        while state["page_url"]:
            if max_listings and len(links) >= max_listings:
                self._log(f"  🎯 Reached maximum listings limit ({max_listings}).")
                break
            first = state["page_num"]
            n = self.HOUZZ_PAGE_BATCH
            if max_pages:
                n = min(n, max_pages - first + 1)
                if n <= 0:
                    self._log(f"  🎯 Reached maximum page limit ({max_pages}).")
                    break
            batch = [houzz_page_url(state["page_url"], k) for k in range(n)]
            self._set_status(f"Crawling Houzz pages {first}–{first + n - 1}...")
            htmls = await asyncio.gather(*(self._fetch_page(fetcher, u, use_browser) for u in batch))

            stop = False
            for u, html in zip(batch, htmls):
                num = state["page_num"]
                if not html:
                    self._log(f"  ❌ Failed to load Page {num} — no more pages will be read.")
                    stop = True
                    break
                added = 0
                for l in find_listing_links(html, u):
                    if l not in seen_links:
                        seen_links.add(l)
                        links.append(l)
                        added += 1
                self._log(f"📄 Page {num}: {added} new pros (total {len(links)})")
                state["seen_pages"].append(u)
                state["page_num"] = num + 1
                state["page_url"] = houzz_page_url(u, 1)
                if added == 0 and num > 1:
                    self._log(f"  🏁 Reached the last page (Page {num - 1})!")
                    stop = True
                    break
            if stop:
                state["page_url"] = None
                self._save_checkpoint(state, force=True)
                break
            self._save_checkpoint(state, force=True)
            await asyncio.sleep(config.DELAY_BETWEEN_REQUESTS)

        if max_listings:
            del links[max_listings:]
        self._log(f"\n🎉 Total {len(links)} pros collected across {len(state['seen_pages'])} page(s)!")

    # ------------------------------------------------------------------
    # Step 2: each company -> its website
    # ------------------------------------------------------------------

    @staticmethod
    def _card_record(c: dict, fast: bool, list_url: str) -> dict:
        rec = {"name": c["name"], "page_url": c["profile_url"] or list_url, "website": c["website"],
               "emails": list(c.get("emails") or []), "state": c.get("state") or "", "license": ""}
        if fast:
            rec["phone"] = c.get("phone") or ""
        else:
            rec.update({"phones": [c["phone"]] if c.get("phone") else [], "address": "", "socials": {}})
        return rec

    def _finish_card(self, state: dict, c: dict, rec: dict, source: str):
        if rec.get("website") and not sd.valid_website(rec["website"]):
            rec["website"] = ""          # e.g. Houzz pro who typed only 'http://', or a Facebook page
        rec["website_source"] = source if rec.get("website") else ""
        if not rec.get("page_url"):
            rec["page_url"] = c["profile_url"] or state["url"]
        state["results"].append(rec)
        state["done_links"].append(c["key"])
        if c["key"] in state["failed_links"]:
            state["failed_links"].remove(c["key"])
        n, total = len(state["done_links"]), len(state["cards"])
        self._log(f"  ✅ [{n}/{total}] {rec.get('name') or '—'}  →  {rec.get('website') or '— (no website)'}")
        self._set_progress(15 + 55 * n / max(total, 1))
        self._set_status(f"⚡ Done {n}/{total} ({total - n} remaining)...")
        self._save_checkpoint(state)

    async def _website_from_email(self, fetcher, rec: dict, dir_url: str) -> str:
        """No website listed, but a business e-mail (info@abcbuilders.com): use abcbuilders.com
        only if that site really opens and belongs to the same company."""
        if not getattr(config, "INFER_WEBSITE_FROM_EMAIL", True):
            return ""
        guess = sd.website_from_email(rec.get("emails") or [], sd.reg_domain(dir_url))
        if not guess:
            return ""
        name = rec.get("name") or ""
        match = sd.name_matches_host(name, guess)
        ok, text = await fetcher.check_site(guess)
        if not ok:
            return ""
        if match == 2:                       # abcbuilders.com for 'ABC Builders'
            return guess
        low = " ".join(text.lower().split())
        if match == 1:                       # initials only (cb-gc.com): the site must show the name
            words = [w for w in re.findall(r"[a-z0-9]+", name.lower())
                     if len(w) >= 3 and w not in ("inc", "llc", "ltd", "corp", "the", "and", "company")]
            return guess if words and sum(w in low for w in words) >= max(1, round(len(words) * 0.6)) else ""
        toks = [t for t in sd._name_tokens(name) if len(t) >= 4]
        return guess if toks and any(t in low for t in toks) else ""

    async def _profiles_stage(self, fetcher, state: dict, fast: bool, use_browser: bool = False):
        """Step 2: website for every company. Fast Mode: a website already shown on the list
        page is final; otherwise the company's page inside the directory is opened."""
        done = set(state["done_links"])
        state["failed_links"] = []
        list_url = state["url"]
        chrome = set(state.get("chrome_hosts") or [])
        todo = [c for c in state["cards"] if c["key"] not in done]
        if done:
            self._log(f"▶ {len(done)} companies already done, {len(todo)} remaining.")

        direct = [c for c in todo if fast and c["website"]]
        need = [c for c in todo if not (fast and c["website"])]
        if direct:
            self._log(f"⚡ {len(direct)} companies show their website on the list page — taken from there.")
        for c in direct:
            self._finish_card(state, c, self._card_record(c, fast, list_url), "listing page")

        workers = config.HOUZZ_WORKERS if "houzz.com" in list_url else config.CONCURRENT_WORKERS
        if need:
            self._log(f"⚡ Opening {len(need)} company pages to find their website ({workers} at a time)...\n")
        self._set_progress(15)

        async def work(c: dict, retry: bool = False):
            if not c["profile_url"]:
                rec = self._card_record(c, fast, list_url)
                src = "listing page" if rec["website"] else ""
                if not rec["website"]:
                    web = await self._website_from_email(fetcher, rec, list_url)
                    if web:
                        rec["website"], src = web, "business e-mail domain (site checked)"
                self._finish_card(state, c, rec, src)
                return
            url = c["profile_url"]
            html, via = await fetcher.fetch_ex(url, prefer_browser=bool(use_browser or state.get("profile_browser") or retry))
            if not html:
                if not retry and c["key"] not in state["failed_links"]:
                    state["failed_links"].append(c["key"])
                    self._log(f"  ❌ Failed (will retry): {url[:70]}")
                return
            rec = await asyncio.to_thread(extract_all, html, url, fast, c["name"], chrome)
            src = "company page" if rec.get("website") else ""
            if not rec.get("website") and c["website"]:
                rec["website"], src = c["website"], "listing page"
            # No website in the plain download. Learn (from the first few pages) whether this
            # directory fills its company pages with JavaScript; if yes, use the browser.
            pb = state.get("profile_browser")
            if not rec.get("website") and via == "http" and (pb is None or (pb is not False and sd.looks_js_rendered(html))):
                bhtml = await fetcher.fetch_browser(url)
                if bhtml:
                    rec2 = await asyncio.to_thread(extract_all, bhtml, url, fast, c["name"], chrome)
                    fuller = await asyncio.to_thread(sd.page_is_fuller, bhtml, html)
                    if rec2.get("website") or fuller:
                        rec = rec2                     # the full page knows more (name, phone ...)
                        src = "company page (browser)" if rec2.get("website") else ""
                        if state.get("profile_browser") is not True:
                            state["profile_browser"] = True
                            self._log("  🌐 Company pages here are filled by JavaScript — "
                                      "opening them in the browser from now on.")
                    else:
                        state["pb_tries"] = state.get("pb_tries", 0) + 1
                        if state["pb_tries"] >= 3 and state.get("profile_browser") is None:
                            state["profile_browser"] = False    # plain pages are complete
            # 'Website' button that goes through the directory's own redirect page
            if not rec.get("website") and rec.get("_internal_website"):
                final = await fetcher.resolve_redirect(rec["_internal_website"])
                if final and not sd.is_bad_site_host(sd.host_of(final)) and sd.reg_domain(final) != sd.reg_domain(url):
                    rec["website"], src = final, "website button (redirect)"
            if not rec.get("website"):
                web = await self._website_from_email(fetcher, rec, list_url)
                if web:
                    rec["website"], src = web, "business e-mail domain (site checked)"
            if not rec.get("name"):
                rec["name"] = c["name"]
            if not rec.get("state") and c.get("state"):
                rec["state"] = c["state"]
            self._finish_card(state, c, rec, src)

        await asyncio.gather(*(work(c) for c in need))

        # One slower retry (in the browser) for pages that failed (timeouts / site hiccups)
        if state["failed_links"]:
            failed = set(state["failed_links"])
            self._log(f"\n🔁 Retrying {len(failed)} failed company page(s) once more...")
            await asyncio.sleep(5)
            await asyncio.gather(*(work(c, retry=True) for c in state["cards"] if c["key"] in failed))

    def _fix_shared_websites(self, state: dict):
        """One outside site on many different companies = a sponsor/ad, not their website."""
        from collections import defaultdict
        groups = defaultdict(list)
        for r in state["results"]:
            if r.get("website"):
                groups[sd.reg_domain(r["website"])].append(r)
        explicit = ("text says website", "labelled Website", "attribute says website", "text is a domain")
        shared = set()
        for dom, rs in groups.items():
            names = {self._norm_name(r.get("name")) for r in rs}
            if len(rs) < 3 or len(names) < 3:
                continue
            if sum(1 for r in rs if sd.name_matches_host(r.get("name", ""), r["website"]) == 2) >= len(rs) / 2:
                continue                      # branches of one company / franchise
            shared.add(dom)
        fixed = 0
        for dom in shared:
            for r in groups[dom]:
                if r.get("website_source") == "listing page" or any(w in explicit for w in r.get("_why", [])):
                    continue                  # the directory itself labels it as their website
                if sd.name_matches_host(r.get("name", ""), r["website"]) == 2:
                    continue
                alt = next((u for s, u in r.get("_cands", [])
                            if s >= 30 and sd.reg_domain(u) not in shared), "")
                r["website"] = alt
                r["website_source"] = "company page" if alt else ""
                fixed += 1
        if fixed:
            self._log(f"🧹 {fixed} website(s) cleared: the same outside link sat on many different companies' "
                      f"pages (a sponsor/ad), so it is not their website.")

    async def _websites_stage(self, state: dict, fetcher=None):
        """Step 3: open each business website -> Contact page URL + email. Already-visited ones are skipped."""
        todo = [r for r in state["results"] if r.get("website") and not r.get("_crawled")]
        if not todo:
            return
        self._log(f"\n🔍 Opening {len(todo)} business websites → Contact page + email (10 parallel)...")
        self._set_status(f"Opening {len(todo)} business websites for contact page + email...")

        def on_website_done(done, total_rec, rec, found_emails):
            rec["_crawled"] = True
            name = rec.get("name") or "Contractor"
            contact = rec.get("contact_page") or rec.get("website_status") or "no contact page"
            mail = ", ".join(found_emails[:2]) if found_emails else "no email on site"
            self._log(f"  ✉ [{done}/{total_rec}] {name}  |  {contact}  |  {mail}")
            self._set_progress(70 + 25 * done / total_rec)
            self._save_checkpoint(state)

        browser_fetch = fetcher.fetch_browser if fetcher else None
        await enrich_records_with_websites(
            todo, concurrency=10, progress_callback=on_website_done, browser_fetch=browser_fetch)

        # Network hiccups happen: give "not opening" websites one more try
        retry = [r for r in todo if r.get("website_status") == "website not opening"]
        if retry:
            self._log(f"\n🔁 Retrying {len(retry)} website(s) that didn't open...")
            await asyncio.sleep(10)
            await enrich_records_with_websites(
                retry, concurrency=5, progress_callback=on_website_done, browser_fetch=browser_fetch)

    def _extraction_done(self):
        """Re-enable buttons after extraction finishes."""
        self.is_running = False
        c = self._c
        self.extract_btn.config(state="normal", bg=c["accent"],
                                fg=c["accent_text"])
        self.stop_btn.config(state="disabled", bg=c["card_border"],
                             fg=c["text_dim"])
        self.open_btn.config(state="normal")

    def _on_stop_click(self):
        if not self.is_running:
            return
        self.stop_event.set()
        c = self._c
        self.stop_btn.config(state="disabled", bg=c["card_border"],
                             fg=c["text_dim"])
        self._set_status("Stopping — saving data collected so far...")

    def _on_close(self):
        """Closing the window mid-run = Stop & Save first, then close."""
        if not self.is_running:
            self.root.destroy()
            return
        self._on_stop_click()
        deadline = time.time() + 15

        def _wait():
            if self.is_running and time.time() < deadline:
                self.root.after(200, _wait)
            else:
                self.root.destroy()
        _wait()

    def _open_output_folder(self):
        """Open the output folder in Windows Explorer."""
        output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), config.OUTPUT_DIR)
        os.makedirs(output_path, exist_ok=True)
        subprocess.Popen(f'explorer "{output_path}"')

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = HMExtractorApp()
    app.run()
