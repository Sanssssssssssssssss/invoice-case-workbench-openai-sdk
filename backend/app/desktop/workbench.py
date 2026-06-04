from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Any

from app.agents.patch_builder.agent import SYSTEM_PROMPT as CASE_PATCH_WRITER_PROMPT
from app.agents.evidence_reviewer.agent import SYSTEM_PROMPT as EVIDENCE_REVIEWER_PROMPT
from app.agents.materials_advisor.agent import SYSTEM_PROMPT as MATERIALS_ADVISOR_PROMPT
from app.agents.report_writer.agent import SYSTEM_PROMPT as REPORT_WRITER_PROMPT
from app.runtime.turn_runner import AgentRuntime
from app.prompt_loader import load_system_prompt
from app.memory_service import MemoryService
from app.state.case_store import CaseStore
from app.state.session_repository import SessionRepository
from app.state.schemas import AgentTurnRequest, Attachment, CaseState, timestamp_case_id
from app.desktop.trace_events import build_debug_timeline_events, load_debug_events
from app.desktop.theme import (
    COLORS,
    ROLE_META,
    STATUS_COLORS,
    TRACE_DETAIL_MODES,
    TRACE_FILTERS,
    TRACE_KIND_META,
)
from app.desktop.widgets import RoundedButton, RoundedFrame, ScrollableFrame, show_confirm, widget_bg

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:  # pragma: no cover - optional desktop capability
    DND_FILES = None
    TkinterDnD = None


DEFAULT_CASE_ID = timestamp_case_id()
MAX_DROPPED_FILES = 80


class DesktopWorkbench:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Invoice Agent Workbench")
        self.root.geometry("1380x860")
        self.root.minsize(1080, 680)
        self.root.configure(bg=COLORS["bg"])

        self.store = CaseStore()
        self.sessions = SessionRepository(self.store)
        self.memory = MemoryService(self.store)
        self.graph: AgentRuntime | None = None
        self.active_case_id = DEFAULT_CASE_ID
        self.case_messages: dict[str, list[tuple[str, str, str]]] = {}
        self.case_traces: dict[str, dict[str, Any]] = {}
        self.case_trace_runs: dict[str, list[dict[str, Any]]] = {}
        self.case_trace_turns: dict[str, list[dict[str, Any]]] = {}
        self.case_list_case_ids: list[str] = []
        self.case_cards: dict[str, tk.Frame] = {}
        self.trace_event_rows: list[dict[str, Any]] = []
        self.trace_event_cards: list[tk.Frame] = []
        self.trace_filter_buttons: dict[str, tk.Label] = {}
        self.selected_trace_event_index: int | None = None
        self.trace_metric_vars: dict[str, tk.StringVar] = {}
        self.trace_metric_caption_vars: dict[str, tk.StringVar] = {}
        self.trace_detail_buttons: dict[str, tk.Label] = {}
        self.artifact_rows: list[dict[str, Any]] = []
        self.evidence_rows: list[Any] = []
        self.pending_files: list[Path] = []
        self.message_content_labels: list[tk.Label] = []
        self.message_wrap = 620
        self.case_state: CaseState | None = None
        self.is_sending = False
        self.running_case_id = ""
        self.running_known_run_ids: set[str] = set()
        self.running_seen_run_id = ""
        self.running_started_at: datetime | None = None
        self.live_trace_after_id: str | None = None
        self.activity_tick = 0
        self.case_filter_var = tk.StringVar(value="")
        self.trace_filter_var = tk.StringVar(value="All Events")
        self.trace_search_var = tk.StringVar(value="")
        self.trace_detail_mode_var = tk.StringVar(value="Thought")
        self.selected_run_var = tk.StringVar(value="")
        self.trace_summary_var = tk.StringVar(value="No run selected")
        self.trace_turn_context_var = tk.StringVar(value="No conversation turn selected")
        self.agent_status_var = tk.StringVar(value="Ready")
        self.agent_status_detail_var = tk.StringVar(value="No active run")
        self.prompt_entries = self._load_prompt_entries()
        self.prompt_entry_by_name = {entry["name"]: entry for entry in self.prompt_entries}
        self.selected_prompt_name = self.prompt_entries[0]["name"] if self.prompt_entries else ""

        self._configure_style()
        self._build_layout()
        self._register_drop_target()
        self.switch_case(DEFAULT_CASE_ID)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self.root.option_add("*Font", ("Segoe UI", 10))

        style.configure("TFrame", background=COLORS["panel"])
        style.configure("Dark.TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Inset.TFrame", background=COLORS["panel_2"])
        style.configure("TLabel", background=COLORS["panel"], foreground=COLORS["text"])
        style.configure("Muted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"])
        style.configure("Status.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI", 10, "bold"))
        style.configure("Header.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Segoe UI", 16, "bold"))
        style.configure("Micro.TLabel", background=COLORS["panel"], foreground=COLORS["subtle"], font=("Segoe UI", 9))
        style.configure("TButton", padding=(12, 7), background=COLORS["panel_3"], foreground=COLORS["text"], bordercolor=COLORS["border"])
        style.map(
            "TButton",
            background=[("active", COLORS["border_strong"]), ("disabled", COLORS["panel_2"])],
            foreground=[("disabled", COLORS["subtle"])],
        )
        style.configure(
            "Primary.TButton",
            padding=(14, 8),
            background=COLORS["accent"],
            foreground="#061018",
            bordercolor=COLORS["accent"],
            font=("Segoe UI", 10, "bold"),
        )
        style.map("Primary.TButton", background=[("active", "#70d8f5"), ("disabled", COLORS["panel_2"])])
        style.configure(
            "Workbench.Treeview",
            background=COLORS["panel_2"],
            fieldbackground=COLORS["panel_2"],
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            rowheight=27,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Workbench.Treeview.Heading",
            background=COLORS["panel_3"],
            foreground=COLORS["muted"],
            bordercolor=COLORS["border"],
            font=("Segoe UI", 9, "bold"),
        )
        style.map("Workbench.Treeview", background=[("selected", COLORS["border_strong"])])
        style.configure("TNotebook", background=COLORS["panel"], borderwidth=0)
        style.configure(
            "TNotebook.Tab",
            background=COLORS["panel_2"],
            foreground=COLORS["muted"],
            padding=(12, 7),
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", COLORS["panel_3"]), ("active", COLORS["border"])],
            foreground=[("selected", COLORS["text"]), ("active", COLORS["text"])],
        )
        style.configure(
            "TCombobox",
            fieldbackground=COLORS["input"],
            background=COLORS["panel_3"],
            foreground=COLORS["text"],
            arrowcolor=COLORS["muted"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["panel_3"],
            darkcolor=COLORS["panel_3"],
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", COLORS["input"])],
            foreground=[("readonly", COLORS["text"])],
            background=[("readonly", COLORS["panel_3"]), ("active", COLORS["hover"])],
        )
        style.configure(
            "Vertical.TScrollbar",
            background=COLORS["panel_3"],
            troughcolor=COLORS["input"],
            bordercolor=COLORS["input"],
            arrowcolor=COLORS["muted"],
            lightcolor=COLORS["panel_3"],
            darkcolor=COLORS["panel_3"],
        )
        style.map("Vertical.TScrollbar", background=[("active", COLORS["hover"])])

    def _build_layout(self) -> None:
        outer = tk.PanedWindow(
            self.root,
            orient=tk.HORIZONTAL,
            bg=COLORS["bg"],
            bd=0,
            sashwidth=7,
            sashrelief=tk.FLAT,
            showhandle=False,
        )
        outer.pack(fill=tk.BOTH, expand=True)

        self.case_frame = self._panel(outer, width=300)
        self.chat_frame = self._panel(outer)
        self.detail_frame = self._panel(outer, width=560)
        outer.add(self.case_frame, minsize=280, width=300)
        outer.add(self.chat_frame, minsize=420)
        outer.add(self.detail_frame, minsize=520, width=560)

        self._build_case_rail()
        self._build_chat_panel()
        self._build_detail_panel()

    def _build_case_rail(self) -> None:
        header = tk.Frame(self.case_frame, bg=COLORS["panel"])
        header.pack(fill=tk.X, padx=16, pady=(16, 12))
        tk.Label(header, text="Invoice Agent", bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 17, "bold")).pack(
            anchor=tk.W
        )
        tk.Label(header, text="local case workbench", bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9)).pack(
            anchor=tk.W, pady=(2, 0)
        )

        entry_shell = RoundedFrame(
            self.case_frame,
            bg=COLORS["panel"],
            fill=COLORS["input"],
            outline=COLORS["border"],
            radius=10,
            min_height=42,
        )
        entry_shell.pack(fill=tk.X, padx=16, pady=(0, 8))
        entry_body = entry_shell.inner
        self.case_id_var = tk.StringVar(value=self.active_case_id)
        self.case_entry = tk.Entry(
            entry_body,
            textvariable=self.case_id_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief=tk.FLAT,
            bd=0,
            font=("Cascadia Mono", 10),
        )
        self.case_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=9, padx=(10, 6))
        self.case_entry.bind("<Return>", lambda _event: self._switch_from_entry())
        self._flat_button(entry_body, "Go", self._switch_from_entry, width=5).pack(side=tk.RIGHT, padx=(0, 6), pady=5)

        self._flat_button(self.case_frame, "+ New case", self._new_case).pack(fill=tk.X, padx=16, pady=(0, 8))

        session_actions = tk.Frame(self.case_frame, bg=COLORS["panel"])
        session_actions.pack(fill=tk.X, padx=16, pady=(0, 12))
        self._flat_button(session_actions, "Delete", self._delete_current_case).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._flat_button(session_actions, "Clear chat", self._clear_current_conversation).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0)
        )

        tk.Label(
            self.case_frame,
            text="Search cases",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9, "bold"),
        ).pack(anchor=tk.W, padx=16, pady=(0, 6))
        search_shell = RoundedFrame(
            self.case_frame,
            bg=COLORS["panel"],
            fill=COLORS["input"],
            outline=COLORS["border"],
            radius=10,
            min_height=38,
        )
        search_shell.pack(fill=tk.X, padx=16, pady=(0, 12))
        search_body = search_shell.inner
        search_entry = tk.Entry(
            search_body,
            textvariable=self.case_filter_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief=tk.FLAT,
            bd=0,
            font=("Segoe UI", 9),
        )
        search_entry.pack(fill=tk.X, ipady=8, padx=10)
        search_entry.insert(0, "")
        search_entry.bind("<KeyRelease>", lambda _event: self._refresh_case_list())

        session_header = tk.Frame(self.case_frame, bg=COLORS["panel"])
        session_header.pack(fill=tk.X, padx=16, pady=(0, 7))
        tk.Label(session_header, text="Sessions", bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9, "bold")).pack(
            side=tk.LEFT
        )
        self.case_count_var = tk.StringVar(value="")
        tk.Label(session_header, textvariable=self.case_count_var, bg=COLORS["panel"], fg=COLORS["subtle"], font=("Segoe UI", 8)).pack(
            side=tk.RIGHT
        )
        self.case_scroller = ScrollableFrame(self.case_frame, bg=COLORS["panel"], border=COLORS["panel"])
        self.case_scroller.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 14))
        self.case_list_body = self.case_scroller.body

        footer = tk.Frame(self.case_frame, bg=COLORS["panel"])
        footer.pack(fill=tk.X, padx=16, pady=(0, 16))
        self.run_state_var = tk.StringVar(value="Ready")
        tk.Label(footer, textvariable=self.run_state_var, bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9)).pack(
            side=tk.LEFT
        )

    def _build_chat_panel(self) -> None:
        header = tk.Frame(self.chat_frame, bg=COLORS["panel"])
        self.chat_header = header
        header.pack(fill=tk.X, padx=18, pady=(16, 10))
        header.columnconfigure(0, weight=1)
        header.columnconfigure(1, weight=0)
        title_group = tk.Frame(header, bg=COLORS["panel"])
        title_group.grid(row=0, column=0, sticky=tk.EW, padx=(0, 12))
        self.title_label = tk.Label(
            title_group,
            text=DEFAULT_CASE_ID,
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Segoe UI", 16, "bold"),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=520,
        )
        self.title_label.pack(anchor=tk.W, fill=tk.X)
        self.trace_subtitle_var = tk.StringVar(value="No run trace yet")
        self.trace_subtitle_label = tk.Label(
            title_group,
            textvariable=self.trace_subtitle_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=520,
        )
        self.trace_subtitle_label.pack(anchor=tk.W, fill=tk.X, pady=(2, 0))
        self.status_label = tk.Label(
            header,
            text="new",
            bg=COLORS["panel_3"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9, "bold"),
            padx=11,
            pady=5,
        )
        self.status_label.grid(row=0, column=1, sticky=tk.NE, pady=(2, 0))
        header.bind("<Configure>", self._chat_header_configured)

        self.message_scroller = ScrollableFrame(
            self.chat_frame,
            bg=COLORS["input"],
            border=COLORS["border"],
        )
        self.message_scroller.pack(fill=tk.BOTH, expand=True, padx=18, pady=(0, 10))
        self.message_body = self.message_scroller.body
        self.message_canvas = self.message_scroller.canvas
        self.message_canvas.bind("<Configure>", self._message_canvas_configured, add="+")

        self._build_attachment_panel()
        self._build_composer()

    def _build_attachment_panel(self) -> None:
        self.attach_frame = tk.Frame(
            self.chat_frame,
            bg=COLORS["panel_2"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        self.attach_frame.pack(fill=tk.X, padx=18, pady=(0, 8))

        attach_header = tk.Frame(self.attach_frame, bg=COLORS["panel_2"])
        attach_header.pack(fill=tk.X, padx=10, pady=(8, 6))
        tk.Label(
            attach_header,
            text="Context",
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            font=("Segoe UI", 10, "bold"),
        ).pack(side=tk.LEFT)
        self.file_count_var = tk.StringVar(value="0 staged")
        tk.Label(
            attach_header,
            textvariable=self.file_count_var,
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
        ).pack(side=tk.LEFT, padx=(8, 0))
        self._flat_button(attach_header, "Clear", self._clear_files).pack(side=tk.RIGHT, padx=(8, 0))
        self._flat_button(attach_header, "Add", self._choose_files).pack(side=tk.RIGHT)

        self.drop_label_var = tk.StringVar(value="Drop files or folders here.")
        self.drop_label = tk.Label(
            self.attach_frame,
            textvariable=self.drop_label_var,
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            anchor=tk.W,
            padx=10,
            pady=0,
        )
        self.drop_label.pack(fill=tk.X, padx=0, pady=(0, 6))

        self.file_chip_frame = tk.Frame(self.attach_frame, bg=COLORS["panel_2"])
        self.file_chip_frame.pack(fill=tk.X, padx=10, pady=(0, 9))

    def _build_composer(self) -> None:
        composer = tk.Frame(self.chat_frame, bg=COLORS["panel"])
        composer.pack(fill=tk.X, padx=18, pady=(0, 18))
        self.activity_frame = RoundedFrame(
            composer,
            bg=COLORS["panel"],
            fill=COLORS["panel_2"],
            outline=COLORS["border"],
            radius=10,
            min_height=40,
        )
        self.activity_frame.pack(fill=tk.X, pady=(0, 8))
        activity_body = self.activity_frame.inner
        self.activity_dot = tk.Canvas(activity_body, width=14, height=14, bg=COLORS["panel_2"], bd=0, highlightthickness=0)
        self.activity_dot.pack(side=tk.LEFT, padx=(10, 8), pady=12)
        self.activity_dot_id = self.activity_dot.create_oval(4, 4, 10, 10, fill=COLORS["subtle"], outline=COLORS["subtle"])
        tk.Label(
            activity_body,
            textvariable=self.agent_status_var,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT, pady=10)
        self.activity_detail_label = tk.Label(
            activity_body,
            textvariable=self.agent_status_detail_var,
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=980,
        )
        self.activity_detail_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(10, 10), pady=10)
        input_shell = RoundedFrame(
            composer,
            bg=COLORS["panel"],
            fill=COLORS["input"],
            outline=COLORS["border_strong"],
            radius=12,
            min_height=88,
        )
        input_shell.pack(fill=tk.X)
        input_body = input_shell.inner
        self.input_box = tk.Text(
            input_body,
            height=4,
            wrap=tk.WORD,
            font=("Segoe UI", 10),
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief=tk.FLAT,
            bd=0,
            padx=12,
            pady=10,
        )
        send_shell = tk.Frame(input_body, bg=COLORS["input"])
        send_shell.pack(side=tk.RIGHT, fill=tk.Y, padx=10, pady=10)
        self.send_button = self._accent_button(send_shell, "Send", self.send_turn)
        self.send_button.pack(anchor=tk.NE)
        self.input_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.input_box.bind("<Control-Return>", lambda _event: self.send_turn())

    def _build_detail_panel(self) -> None:
        header = tk.Frame(self.detail_frame, bg=COLORS["panel"])
        header.pack(fill=tk.X, padx=16, pady=(16, 10))
        tk.Label(header, text="Inspector", bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 16, "bold")).pack(
            side=tk.LEFT
        )
        self.reload_trace_button = self._flat_button(header, "Reload trace", self._reload_latest_trace)
        self.reload_trace_button.pack(side=tk.RIGHT)

        self.detail_tabs = ttk.Notebook(self.detail_frame)
        self.detail_tabs.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 16))

        self.state_tab = tk.Frame(self.detail_tabs, bg=COLORS["panel"])
        self.trace_tab = tk.Frame(self.detail_tabs, bg=COLORS["panel"])
        self.artifacts_tab = tk.Frame(self.detail_tabs, bg=COLORS["panel"])
        self.prompts_tab = tk.Frame(self.detail_tabs, bg=COLORS["panel"])
        self.detail_tabs.add(self.state_tab, text="State")
        self.detail_tabs.add(self.trace_tab, text="Trace")
        self.detail_tabs.add(self.artifacts_tab, text="Artifacts")
        self.detail_tabs.add(self.prompts_tab, text="Prompts")

        self._build_state_tab()
        self._build_trace_tab()
        self._build_artifacts_tab()
        self._build_prompts_tab()
        self.detail_tabs.bind("<<NotebookTabChanged>>", self._detail_tab_changed)

    def _build_state_tab(self) -> None:
        self.summary_var = tk.StringVar(value="No state loaded")
        tk.Label(
            self.state_tab,
            textvariable=self.summary_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=350,
        ).pack(fill=tk.X, pady=(12, 12))
        self.state_metrics_var = tk.StringVar(value="")
        tk.Label(
            self.state_tab,
            textvariable=self.state_metrics_var,
            bg=COLORS["input"],
            fg=COLORS["accent_2"],
            font=("Cascadia Mono", 9, "bold"),
            anchor=tk.W,
            padx=9,
            pady=7,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        ).pack(fill=tk.X, pady=(0, 12))

        tk.Label(self.state_tab, text="Requirements", bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor=tk.W
        )
        self.requirements_tree = ttk.Treeview(
            self.state_tab,
            columns=("status", "evidence"),
            show="tree headings",
            height=7,
            style="Workbench.Treeview",
        )
        self.requirements_tree.heading("#0", text="Material")
        self.requirements_tree.heading("status", text="Status")
        self.requirements_tree.heading("evidence", text="Evidence")
        self.requirements_tree.column("#0", width=126)
        self.requirements_tree.column("status", width=82)
        self.requirements_tree.column("evidence", width=106)
        self.requirements_tree.pack(fill=tk.X, pady=(6, 14))

        tk.Label(
            self.state_tab,
            text="Risks / next questions",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor=tk.W)
        self.notes = self._readonly_text(self.state_tab, height=5)
        self.notes.pack(fill=tk.X, pady=(6, 12))

        tk.Label(self.state_tab, text="Recent evidence", bg=COLORS["panel"], fg=COLORS["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor=tk.W
        )
        self.evidence_tree = ttk.Treeview(
            self.state_tab,
            columns=("type", "credibility"),
            show="tree headings",
            height=4,
            style="Workbench.Treeview",
        )
        self.evidence_tree.heading("#0", text="ID")
        self.evidence_tree.heading("type", text="Type")
        self.evidence_tree.heading("credibility", text="Credibility")
        self.evidence_tree.column("#0", width=66)
        self.evidence_tree.column("type", width=138)
        self.evidence_tree.column("credibility", width=88)
        self.evidence_tree.pack(fill=tk.X, pady=(6, 0))
        self.evidence_tree.bind("<<TreeviewSelect>>", self._evidence_selected)
        self.evidence_detail = self._readonly_text(self.state_tab, height=6, monospace=True)
        self.evidence_detail.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.evidence_detail.tag_configure("evidence_heading", foreground=COLORS["accent"], font=("Cascadia Mono", 10, "bold"))
        self.evidence_detail.tag_configure("evidence_dim", foreground=COLORS["muted"])

    def _build_trace_tab(self) -> None:
        self.trace_header_var = tk.StringVar(value="No run trace yet")
        top = tk.Frame(self.trace_tab, bg=COLORS["panel"])
        top.pack(fill=tk.X, pady=(12, 8))
        tk.Label(
            top,
            textvariable=self.trace_header_var,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=420,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._flat_button(top, "Copy JSON", self._copy_trace_json).pack(side=tk.RIGHT, padx=(8, 0))
        self._flat_button(top, "Copy event", self._copy_selected_trace_event).pack(side=tk.RIGHT)

        run_controls = tk.Frame(self.trace_tab, bg=COLORS["panel"])
        run_controls.pack(fill=tk.X, pady=(0, 8))
        tk.Label(run_controls, text="Turn", bg=COLORS["panel"], fg=COLORS["muted"], font=("Segoe UI", 9, "bold")).pack(
            side=tk.LEFT
        )
        self.run_selector = ttk.Combobox(
            run_controls,
            textvariable=self.selected_run_var,
            values=(),
            state="readonly",
            width=30,
        )
        self.run_selector.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        self.run_selector.bind("<<ComboboxSelected>>", self._run_selected)

        turn_card = RoundedFrame(
            self.trace_tab,
            bg=COLORS["panel"],
            fill=COLORS["panel_2"],
            outline=COLORS["border"],
            radius=10,
            min_height=62,
        )
        turn_card.pack(fill=tk.X, pady=(0, 10))
        tk.Label(
            turn_card.inner,
            textvariable=self.trace_turn_context_var,
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=470,
        ).pack(fill=tk.X, padx=10, pady=9)

        self.trace_metric_frame = tk.Frame(self.trace_tab, bg=COLORS["panel"])
        self.trace_metric_frame.pack(fill=tk.X, pady=(0, 10))
        for label in ("events", "phases", "checkpoints", "tools", "errors"):
            self._add_trace_metric_tile(label)

        trace_controls = tk.Frame(self.trace_tab, bg=COLORS["panel"])
        trace_controls.pack(fill=tk.X, pady=(0, 5))
        self.trace_filter_buttons = {}
        filter_grid = tk.Frame(trace_controls, bg=COLORS["panel"])
        filter_grid.pack(fill=tk.X)
        for index, label in enumerate(TRACE_FILTERS):
            button = self._trace_filter_button(filter_grid, label)
            button.grid(row=0, column=index, sticky=tk.EW, padx=(0, 5), pady=(0, 5))
            filter_grid.columnconfigure(index, weight=1)
            self.trace_filter_buttons[label] = button

        search_shell = RoundedFrame(
            self.trace_tab,
            bg=COLORS["panel"],
            fill=COLORS["input"],
            outline=COLORS["border"],
            radius=9,
            min_height=35,
        )
        search_shell.pack(fill=tk.X, pady=(0, 8))
        search_body = search_shell.inner
        tk.Label(search_body, text="Filter events...", bg=COLORS["input"], fg=COLORS["subtle"], font=("Segoe UI", 8, "bold")).pack(
            side=tk.LEFT, padx=(8, 4)
        )
        search_entry = tk.Entry(
            search_body,
            textvariable=self.trace_search_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief=tk.FLAT,
            bd=0,
            width=17,
            font=("Segoe UI", 9),
        )
        search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5, padx=(0, 8))
        search_entry.bind("<KeyRelease>", lambda _event: self._refresh_trace())

        self.trace_summary = tk.Label(
            self.trace_tab,
            textvariable=self.trace_summary_var,
            bg=COLORS["input"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=360,
            padx=9,
            pady=7,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        self.trace_summary.pack(fill=tk.X, pady=(0, 8))

        self.trace_scroller = ScrollableFrame(self.trace_tab, bg=COLORS["input"], border=COLORS["border"])
        self.trace_scroller.pack(fill=tk.BOTH, expand=False)
        self.trace_scroller.canvas.configure(height=150)
        self.trace_timeline_body = self.trace_scroller.body
        self.trace_tree = ttk.Treeview(
            self.trace_tab,
            columns=("kind", "name", "detail"),
            show="headings",
            height=0,
            style="Workbench.Treeview",
        )
        self.trace_tree.heading("kind", text="Kind")
        self.trace_tree.heading("name", text="Name")
        self.trace_tree.heading("detail", text="Detail")
        self.trace_tree.column("kind", width=74, stretch=False)
        self.trace_tree.column("name", width=132, stretch=False)
        self.trace_tree.column("detail", width=220)
        self.trace_tree.bind("<<TreeviewSelect>>", self._trace_event_selected)
        self.trace_tree.tag_configure("error", foreground=COLORS["danger"])
        self.trace_tree.tag_configure("planner", foreground=COLORS["accent_2"])
        self.trace_tree.tag_configure("model", foreground=COLORS["accent"])
        self.trace_tree.tag_configure("tool", foreground=COLORS["warning"])

        self.trace_selected_title_var = tk.StringVar(value="No event selected")
        trace_text_shell = tk.Frame(
            self.trace_tab,
            bg=COLORS["input"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
        )
        trace_text_shell.pack(fill=tk.BOTH, expand=True, pady=(8, 0))
        detail_header = tk.Frame(trace_text_shell, bg=COLORS["input"])
        detail_header.pack(fill=tk.X, padx=10, pady=(8, 0))
        tk.Label(
            detail_header,
            textvariable=self.trace_selected_title_var,
            bg=COLORS["input"],
            fg=COLORS["text"],
            font=("Segoe UI", 10, "bold"),
            anchor=tk.W,
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        detail_modes = tk.Frame(trace_text_shell, bg=COLORS["input"])
        detail_modes.pack(fill=tk.X, padx=10, pady=(8, 0))
        self.trace_detail_buttons = {}
        for mode in TRACE_DETAIL_MODES:
            button = self._trace_detail_mode_button(detail_modes, mode)
            button.pack(side=tk.LEFT, padx=(0, 6))
            self.trace_detail_buttons[mode] = button
        self.trace_text = tk.Text(
            trace_text_shell,
            wrap=tk.WORD,
            height=10,
            state=tk.DISABLED,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=10,
            font=("Cascadia Mono", 9),
        )
        trace_text_scroll = ttk.Scrollbar(trace_text_shell, orient=tk.VERTICAL, command=self.trace_text.yview)
        self.trace_text.configure(yscrollcommand=trace_text_scroll.set)
        self.trace_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(10, 0), pady=10)
        trace_text_scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=10)
        self.trace_text.tag_configure("trace_heading", foreground=COLORS["accent"], font=("Cascadia Mono", 10, "bold"))
        self.trace_text.tag_configure("trace_dim", foreground=COLORS["muted"])
        self.trace_text.tag_configure("trace_error", foreground=COLORS["danger"])
        self.trace_text.tag_configure("trace_ok", foreground=COLORS["ok"])
        self.trace_text.tag_configure("trace_warn", foreground=COLORS["warning"])
        self.trace_text.tag_configure("trace_thought", foreground=COLORS["violet"])

    def _add_trace_metric_tile(self, label: str) -> None:
        meta = {
            "events": ("Total events", COLORS["accent"], "View all"),
            "phases": ("Phases", COLORS["violet"], "View phases"),
            "checkpoints": ("Checkpoints", COLORS["blue"], "View checkpoints"),
            "tools": ("Tools", COLORS["warning"], "View tools"),
            "errors": ("Errors", COLORS["danger"], "View errors"),
        }.get(label, (label.title(), COLORS["muted"], ""))
        shell = RoundedFrame(
            self.trace_metric_frame,
            bg=COLORS["panel"],
            fill=COLORS["panel_2"],
            outline=COLORS["border"],
            radius=10,
            padding=(0, 0, 0, 0),
            min_height=80,
        )
        column = len(self.trace_metric_vars)
        shell.grid(row=0, column=column, sticky=tk.EW, padx=(0, 6))
        self.trace_metric_frame.columnconfigure(column, weight=1, uniform="trace_metric")
        body = shell.inner
        value_var = tk.StringVar(value="-")
        caption_var = tk.StringVar(value=meta[2])
        self.trace_metric_vars[label] = value_var
        self.trace_metric_caption_vars[label] = caption_var
        tk.Label(
            body,
            text=meta[0],
            bg=COLORS["panel_2"],
            fg=meta[1],
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor=tk.W, padx=9, pady=(7, 0))
        tk.Label(
            body,
            textvariable=value_var,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            font=("Cascadia Mono", 14, "bold"),
        ).pack(anchor=tk.W, padx=9, pady=(3, 0))
        tk.Label(
            body,
            textvariable=caption_var,
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            font=("Segoe UI", 8),
        ).pack(anchor=tk.W, padx=9, pady=(1, 7))

    def _trace_filter_button(self, master: tk.Misc, label: str) -> tk.Widget:
        short_label = {
            "All Events": "All",
            "Planner": "Plan",
            "Roles": "Role",
            "Tools": "Tool",
            "Models": "Model",
            "Observations": "Obs",
            "Artifacts": "Art",
            "Checkpoints": "Ckpt",
            "Errors": "Err",
        }.get(label, label)
        button = RoundedButton(
            master,
            text=short_label,
            bg=COLORS["panel_3"],
            fg=COLORS["muted"],
            parent_bg=COLORS["panel"],
            width_chars=max(4, len(short_label)),
            height=29,
            radius=8,
            outline=COLORS["border"],
            font=("Segoe UI", 8, "bold"),
        )
        button.bind("<Button-1>", lambda _event, value=label: self._set_trace_filter(value))
        button.bind(
            "<Enter>",
            lambda _event, widget=button: widget.set_colors(COLORS["hover"], COLORS["text"], outline=COLORS["border_strong"]),  # type: ignore[attr-defined]
        )
        button.bind("<Leave>", lambda _event: self._sync_trace_filter_buttons())
        return button

    def _trace_detail_mode_button(self, master: tk.Misc, mode: str) -> tk.Widget:
        display = {
            "Details": "Details",
            "Thought": "Thought",
            "Input": "Input",
            "Output": "Output",
            "Artifacts": "Files",
            "Related": "Links",
            "Raw": "Raw",
        }.get(mode, mode)
        button = RoundedButton(
            master,
            text=display,
            bg=COLORS["panel_3"],
            fg=COLORS["muted"],
            parent_bg=COLORS["input"],
            width_chars=max(3, len(display)),
            height=29,
            radius=8,
            outline=COLORS["border"],
            font=("Segoe UI", 8, "bold"),
        )
        button.bind("<Button-1>", lambda _event, value=mode: self._set_trace_detail_mode(value))
        return button

    def _set_trace_detail_mode(self, mode: str) -> None:
        self.trace_detail_mode_var.set(mode)
        self._sync_trace_detail_buttons()
        event = self._selected_trace_event()
        if event:
            self._show_trace_event(event)

    def _sync_trace_detail_buttons(self) -> None:
        selected = self.trace_detail_mode_var.get()
        for mode, button in self.trace_detail_buttons.items():
            active = mode == selected
            bg = COLORS["teal_surface"] if active else COLORS["panel_3"]
            fg = COLORS["accent"] if active else COLORS["muted"]
            outline = COLORS["accent"] if active else COLORS["border"]
            if hasattr(button, "set_colors"):
                button.set_colors(bg, fg, outline=outline)  # type: ignore[attr-defined]
            else:
                button.configure(bg=bg, fg=fg)

    def _set_trace_filter(self, value: str) -> None:
        self.trace_filter_var.set(value)
        self._refresh_trace()

    def _sync_trace_filter_buttons(self) -> None:
        selected = self.trace_filter_var.get()
        for label, button in self.trace_filter_buttons.items():
            active = label == selected
            meta = self._trace_kind_meta_for_filter(label)
            bg = meta["bg"] if active else COLORS["panel_3"]
            fg = meta["color"] if active else COLORS["muted"]
            outline = meta["color"] if active else COLORS["border"]
            if hasattr(button, "set_colors"):
                button.set_colors(bg, fg, outline=outline)  # type: ignore[attr-defined]
            else:
                button.configure(bg=bg, fg=fg)

    def _trace_kind_meta_for_filter(self, label: str) -> dict[str, str]:
        kind = {
            "All Events": "Phase",
            "Planner": "Planner",
            "Roles": "Role",
            "Tools": "Tool",
            "Models": "Model",
            "Observations": "Observation",
            "Artifacts": "Artifact",
            "Checkpoints": "Checkpoint",
            "Errors": "Error",
        }.get(label, "Phase")
        return TRACE_KIND_META[kind]

    def _build_artifacts_tab(self) -> None:
        tk.Label(
            self.artifacts_tab,
            text="Debug artifacts, attachment manifests, and raw run payloads for the selected run. Case evidence is in State.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9),
            anchor=tk.W,
            justify=tk.LEFT,
            wraplength=410,
        ).pack(fill=tk.X, pady=(12, 8))

        artifact_actions = tk.Frame(self.artifacts_tab, bg=COLORS["panel"])
        artifact_actions.pack(fill=tk.X, pady=(0, 8))
        self._flat_button(artifact_actions, "Open", self._open_selected_artifact).pack(side=tk.LEFT)
        self._flat_button(artifact_actions, "Copy path", self._copy_selected_artifact_path).pack(side=tk.LEFT, padx=(8, 0))
        self._flat_button(artifact_actions, "Copy JSON", self._copy_selected_artifact_json).pack(side=tk.RIGHT)

        artifact_shell = tk.Frame(self.artifacts_tab, bg=COLORS["panel"], highlightthickness=1, highlightbackground=COLORS["border"])
        artifact_shell.pack(fill=tk.BOTH, expand=True)
        self.artifact_tree = ttk.Treeview(
            artifact_shell,
            columns=("type", "name", "summary"),
            show="headings",
            height=10,
            style="Workbench.Treeview",
        )
        self.artifact_tree.heading("type", text="Type")
        self.artifact_tree.heading("name", text="Name")
        self.artifact_tree.heading("summary", text="Summary")
        self.artifact_tree.column("type", width=90, stretch=False)
        self.artifact_tree.column("name", width=150, stretch=False)
        self.artifact_tree.column("summary", width=210)
        self.artifact_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        artifact_scroll = ttk.Scrollbar(artifact_shell, orient=tk.VERTICAL, command=self.artifact_tree.yview)
        artifact_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.artifact_tree.configure(yscrollcommand=artifact_scroll.set)
        self.artifact_tree.bind("<<TreeviewSelect>>", self._artifact_selected)

        self.artifact_text = self._readonly_text(self.artifacts_tab, height=12, monospace=True)
        self.artifact_text.pack(fill=tk.BOTH, expand=True, pady=(10, 0))
        self.artifact_text.tag_configure("artifact_heading", foreground=COLORS["accent_2"], font=("Cascadia Mono", 10, "bold"))
        self.artifact_text.tag_configure("artifact_dim", foreground=COLORS["muted"])

    def _build_prompts_tab(self) -> None:
        body = tk.Frame(self.prompts_tab, bg=COLORS["panel"])
        body.pack(fill=tk.BOTH, expand=True, pady=(12, 0))

        self.prompt_list = tk.Listbox(
            body,
            height=5,
            exportselection=False,
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            selectbackground=COLORS["border_strong"],
            selectforeground=COLORS["text"],
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            relief=tk.FLAT,
            bd=0,
            font=("Cascadia Mono", 9),
        )
        self.prompt_list.pack(fill=tk.X, pady=(0, 10))
        for entry in self.prompt_entries:
            self.prompt_list.insert(tk.END, entry["label"])
        self.prompt_list.bind("<<ListboxSelect>>", self._prompt_selected)

        self.prompt_text = self._readonly_text(body, height=30, monospace=True)
        self.prompt_text.pack(fill=tk.BOTH, expand=True)
        self.prompt_text.tag_configure("prompt_title", foreground=COLORS["accent_2"], font=("Cascadia Mono", 10, "bold"))
        if self.prompt_entries:
            self.prompt_list.selection_set(0)
            self._show_prompt(self.prompt_entries[0]["name"])

    def _register_drop_target(self) -> None:
        if DND_FILES is None or not hasattr(self.attach_frame, "drop_target_register"):
            self.drop_label_var.set("Drag support is unavailable in this runtime. Use Add files.")
            return
        registered = False
        for widget in (self.attach_frame, self.drop_label, self.file_chip_frame, self.input_box):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._drop_files)
                registered = True
            except tk.TclError:
                continue
        if not registered:
            self.drop_label_var.set("Drag support is unavailable in this runtime. Use Add files.")

    def _switch_from_entry(self) -> None:
        try:
            self.switch_case(self.case_id_var.get().strip() or DEFAULT_CASE_ID)
        except Exception as exc:
            messagebox.showerror("Cannot switch case", f"{type(exc).__name__}: {exc}")

    def _new_case(self) -> None:
        case_id = timestamp_case_id()
        self.switch_case(case_id)
        self.input_box.focus_set()

    def _delete_current_case(self) -> None:
        if self.is_sending:
            messagebox.showinfo("Agent is working", "Wait for the current run to finish before deleting this session.")
            return
        case_id = self.active_case_id
        if not show_confirm(
            self.root,
            title="Delete Session",
            message=(
                f"Delete '{case_id}' and its local workspace?\n\n"
                "Review what will be removed before confirming."
            ),
            details=self._delete_session_details(case_id),
            note="This cannot be undone. Attachments, traces, reports, and conversation for this session will be removed.",
            confirm_text="Delete",
            destructive=True,
        ):
            return
        try:
            self.sessions.clear_session(case_id)
            self.memory.clear_case(case_id)
            case_dir = self.store.case_dir(case_id)
            if case_dir.exists():
                shutil.rmtree(case_dir)
            self.case_messages.pop(case_id, None)
            self.case_traces.pop(case_id, None)
            self.case_trace_runs.pop(case_id, None)
            self.case_trace_turns.pop(case_id, None)
            self.pending_files = []
            remaining = [item for item in self._known_case_ids(include_active=False) if item != case_id]
            self.case_state = None
            self.switch_case(remaining[0] if remaining else DEFAULT_CASE_ID)
        except Exception as exc:
            messagebox.showerror("Delete failed", f"{type(exc).__name__}: {exc}")

    def _delete_session_details(self, case_id: str) -> list[tuple[str, str]]:
        root = self.store.case_dir(case_id)
        status = "unknown"
        evidence_count = 0
        summary = ""
        try:
            state = self.store.load(case_id)
            status = state.status
            evidence_count = len(state.evidence_items)
            summary = self._single_line(state.summary or "", max_chars=90)
        except Exception:
            status = "unreadable"
        try:
            messages = len(self.sessions.get_conversation_items(case_id, limit=10000))
        except Exception:
            messages = 0
        trace_root = root / "traces"
        try:
            trace_runs = len([path for path in trace_root.glob("run_*.json") if path.is_file()])
        except Exception:
            trace_runs = 0
        file_count, total_bytes = self._folder_stats(root)
        rows = [
            ("Session", case_id),
            ("Status", status),
            ("Evidence", str(evidence_count)),
            ("Messages", str(messages)),
            ("Trace runs", str(trace_runs)),
            ("Workspace", f"{file_count} file(s), {self._format_bytes(total_bytes)}"),
            ("Path", str(root)),
        ]
        if summary:
            rows.insert(2, ("Summary", summary))
        return rows

    def _folder_stats(self, root: Path) -> tuple[int, int]:
        if not root.exists():
            return 0, 0
        count = 0
        total = 0
        try:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                count += 1
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        except OSError:
            return count, total
        return count, total

    def _format_bytes(self, value: int) -> str:
        size = float(max(0, value))
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                if unit == "B":
                    return f"{int(size)} {unit}"
                return f"{size:.1f} {unit}"
            size /= 1024

    def _clear_current_conversation(self) -> None:
        if self.is_sending:
            messagebox.showinfo("Agent is working", "Wait for the current run to finish before clearing chat.")
            return
        case_id = self.active_case_id
        if not show_confirm(
            self.root,
            title="Clear Chat",
            message=(
                f"Clear only the visible chat history for '{case_id}'?\n\n"
                "Case state, attachments, reports, and traces are kept."
            ),
            confirm_text="Clear",
        ):
            return
        try:
            self.sessions.clear_session(case_id)
            self.case_messages[case_id] = []
            self._refresh_messages()
            self._refresh_case_list()
        except Exception as exc:
            messagebox.showerror("Clear failed", f"{type(exc).__name__}: {exc}")

    def _case_selected(self, _event: tk.Event[Any]) -> None:
        case_list = getattr(self, "case_list", None)
        if case_list is None:
            return
        selection = case_list.curselection()
        if not selection:
            return
        index = selection[0]
        if index >= len(self.case_list_case_ids):
            return
        case_id = self.case_list_case_ids[index]
        if case_id and case_id != self.active_case_id:
            self.switch_case(case_id)

    def switch_case(self, case_id: str) -> None:
        case_id = self.store.validate_case_id(case_id)
        self.active_case_id = case_id
        self.case_id_var.set(case_id)
        self.case_messages.setdefault(case_id, self._conversation_rows(case_id))
        self.pending_files = []
        try:
            self.case_state = self.store.load(case_id)
        except Exception as exc:
            self.case_state = None
            self._append_message("runtime", f"Failed to load case state: {type(exc).__name__}: {exc}")
        self._load_latest_trace(case_id)
        self._refresh_all()

    def send_turn(self) -> None:
        if self.is_sending:
            return
        message = self.input_box.get("1.0", tk.END).strip()
        if not message:
            self.input_box.focus_set()
            return
        try:
            case_id = self.store.validate_case_id(self.case_id_var.get().strip() or self.active_case_id)
        except Exception as exc:
            messagebox.showerror("Invalid case id", f"{type(exc).__name__}: {exc}")
            return
        if case_id != self.active_case_id:
            self.switch_case(case_id)
        attachments = [self._attachment_for_path(path) for path in self.pending_files]
        existing_runs = self._load_case_runs(case_id, force=True)
        self._append_message("user", self._user_message_preview(message, attachments))
        self.input_box.delete("1.0", tk.END)
        self._set_sending(True, "Agent is working")
        self._start_live_trace_poll(case_id, existing_runs)

        thread = threading.Thread(
            target=self._run_agent_turn,
            args=(case_id, message, attachments),
            daemon=True,
        )
        thread.start()

    def _run_agent_turn(self, case_id: str, message: str, attachments: list[Attachment]) -> None:
        try:
            if self.graph is None:
                self.graph = AgentRuntime(store=self.store)
            response = self.graph.run_turn(
                AgentTurnRequest(case_id=case_id, message=message, attachments=attachments)
            )
        except Exception as exc:
            self.root.after(0, lambda: self._agent_failed(case_id, exc))
            return
        self.root.after(
            0,
            lambda: self._agent_completed(case_id, response.reply, response.case_state, response.trace),
        )

    def _agent_completed(self, case_id: str, reply: str, case_state: CaseState, trace: dict[str, Any]) -> None:
        if case_id == self.active_case_id:
            try:
                self.case_state = self.store.load(case_id)
            except Exception:
                self.case_state = case_state
            self.pending_files = []
        runs = self._load_case_runs(case_id, force=True)
        self.case_trace_turns.pop(case_id, None)
        run_id = str(trace.get("run_id") or "")
        full_trace = next((item.get("trace") for item in runs if item.get("run_id") == run_id), None)
        self.case_traces[case_id] = full_trace if isinstance(full_trace, dict) and full_trace else trace
        if run_id:
            self.selected_run_var.set(self._run_label_for_id(case_id, run_id) or run_id)
        self._append_message("assistant", reply, case_id=case_id)
        self._stop_live_trace_poll(status="Ready", detail=self._completed_trace_status(trace))
        self._set_sending(False, "Ready")
        self._refresh_all()

    def _agent_failed(self, case_id: str, exc: Exception) -> None:
        self._append_message("runtime", f"Agent run failed: {type(exc).__name__}: {exc}", case_id=case_id)
        self._stop_live_trace_poll(status="Failed", detail=f"{type(exc).__name__}: {exc}")
        self._set_sending(False, "Failed")
        self._load_latest_trace(case_id, force=True)
        self._refresh_all()

    def _set_sending(self, value: bool, label: str) -> None:
        self.is_sending = value
        self.run_state_var.set(label)
        self._set_action_button_enabled(self.send_button, not value)

    def _start_live_trace_poll(self, case_id: str, existing_runs: list[dict[str, Any]]) -> None:
        self.running_case_id = case_id
        self.running_known_run_ids = {str(item.get("run_id") or "") for item in existing_runs if item.get("run_id")}
        self.running_seen_run_id = ""
        self.running_started_at = datetime.now()
        self.activity_tick = 0
        self.trace_header_var.set("Run in progress - waiting for first checkpoint.")
        self.trace_subtitle_var.set("Live trace will update as checkpoints are written.")
        self.trace_summary_var.set("Waiting for planner output...")
        self._set_activity_status("Thinking", "Planner is starting; waiting for the first checkpoint.", COLORS["warning"])
        self._schedule_live_trace_poll(delay_ms=350)

    def _stop_live_trace_poll(self, *, status: str, detail: str) -> None:
        if self.live_trace_after_id is not None:
            try:
                self.root.after_cancel(self.live_trace_after_id)
            except tk.TclError:
                pass
        self.live_trace_after_id = None
        self.running_case_id = ""
        self.running_known_run_ids = set()
        self.running_seen_run_id = ""
        self.running_started_at = None
        color = COLORS["danger"] if status == "Failed" else COLORS["ok"]
        self._set_activity_status(status, detail, color)

    def _schedule_live_trace_poll(self, *, delay_ms: int = 900) -> None:
        if self.live_trace_after_id is not None:
            try:
                self.root.after_cancel(self.live_trace_after_id)
            except tk.TclError:
                pass
        self.live_trace_after_id = self.root.after(delay_ms, self._poll_live_trace)

    def _poll_live_trace(self) -> None:
        self.live_trace_after_id = None
        if not self.is_sending or not self.running_case_id:
            return
        self.activity_tick += 1
        case_id = self.running_case_id
        trace = self._load_running_trace(case_id)
        if trace:
            if case_id == self.active_case_id:
                self._refresh_trace()
                self._refresh_artifacts()
            self._set_activity_status("Thinking", self._live_trace_status(trace), COLORS["accent"])
        else:
            self._set_activity_status("Thinking", self._waiting_for_trace_status(), COLORS["warning"])
        self._schedule_live_trace_poll(delay_ms=900)

    def _load_running_trace(self, case_id: str) -> dict[str, Any] | None:
        runs = self._load_case_runs(case_id, force=True)
        selected: dict[str, Any] | None = None
        if self.running_seen_run_id:
            selected = next((item for item in runs if item.get("run_id") == self.running_seen_run_id), None)
        if selected is None:
            selected = next((item for item in runs if str(item.get("run_id") or "") not in self.running_known_run_ids), None)
        if selected is None:
            return None
        trace = selected.get("trace") or {}
        if trace.get("error"):
            return None
        run_id = str(selected.get("run_id") or trace.get("run_id") or "")
        self.running_seen_run_id = run_id
        self.case_traces[case_id] = trace
        if case_id == self.active_case_id:
            self.selected_run_var.set(str(selected.get("label") or run_id))
        return trace

    def _waiting_for_trace_status(self) -> str:
        elapsed = self._running_elapsed()
        dots = "." * ((self.activity_tick % 3) + 1)
        return f"Waiting for planner checkpoint{dots}  elapsed {elapsed}\nTrace will show planner/reviewer visible steps as soon as events are written."

    def _live_trace_status(self, trace: dict[str, Any]) -> str:
        step_count = trace.get("step_count", 0)
        max_steps = trace.get("max_steps", "?")
        phase = str(trace.get("phase") or "running")
        elapsed = self._running_elapsed(trace)
        header = f"step {step_count}/{max_steps} - {phase} - elapsed {elapsed}"
        stream = self._visible_thought_stream(trace, limit=3)
        if stream:
            return header + "\n" + "\n".join(stream)
        latest = self._latest_trace_activity(trace)
        if latest:
            return header + "\n" + latest
        return header

    def _completed_trace_status(self, trace: dict[str, Any]) -> str:
        if not trace:
            return "Run finished; no trace payload was returned."
        step_count = trace.get("step_count", 0)
        max_steps = trace.get("max_steps", "?")
        errors = sum(1 for event in self._build_trace_events(trace) if event.get("status") == "error")
        suffix = f"{errors} error(s)" if errors else "no errors"
        return f"Run complete - steps {step_count}/{max_steps}, {suffix}."

    def _latest_trace_activity(self, trace: dict[str, Any]) -> str:
        reasoning = self._latest_visible_reasoning(trace)
        if reasoning:
            return reasoning
        for collection, label in (
            ("observations", "observation"),
            ("role_calls", "role"),
            ("tool_calls", "tool"),
            ("planner_actions", "planner"),
            ("model_calls", "model"),
            ("trace_checkpoints", "checkpoint"),
            ("checkpoints", "checkpoint"),
        ):
            items = trace.get(collection) or []
            if not items:
                continue
            item = items[-1]
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("role") or item.get("tool") or item.get("action") or item.get("checkpoint_id") or label
            summary = item.get("summary") or item.get("result_preview") or item.get("output_preview") or item.get("next_action_hint") or ""
            text = f"{label}: {name}"
            if summary:
                text = f"{text} - {self._single_line(str(summary), max_chars=56)}"
            return self._single_line(text, max_chars=96)
        return ""

    def _latest_visible_reasoning(self, trace: dict[str, Any]) -> str:
        debug_events = self._trace_debug_events(trace)
        if not debug_events:
            return ""
        for event in reversed(build_debug_timeline_events(debug_events)):
            kind = str(event.get("kind") or "")
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if kind == "Planner":
                action = payload.get("action") or "-"
                target = payload.get("role") or payload.get("tool") or "final"
                reason = payload.get("reason") or payload.get("plan_progress") or ""
                if reason:
                    return self._single_line(f"planner: {action}->{target} - {reason}", max_chars=96)
            if kind == "Role" and str(payload.get("role") or "") == "evidence_reviewer":
                result = payload.get("result")
                if isinstance(result, dict):
                    reason = result.get("reason") or result.get("reply_to_user") or payload.get("result_preview")
                    if reason:
                        return self._single_line(f"evidence reviewer: {reason}", max_chars=96)
            if kind == "Model" and str(payload.get("role") or "") in {"planner", "evidence_reviewer"}:
                summary = _reason_from_model_payload(payload)
                if summary:
                    return self._single_line(f"{payload.get('role')}: {summary}", max_chars=96)
        return ""

    def _visible_thought_stream(self, trace: dict[str, Any], *, limit: int = 3) -> list[str]:
        debug_events = self._trace_debug_events(trace)
        if not debug_events:
            return []
        lines: list[str] = []
        for event in reversed(build_debug_timeline_events(debug_events)):
            line = self._visible_thought_line(event)
            if not line or line in lines:
                continue
            lines.append(line)
            if len(lines) >= limit:
                break
        return list(reversed(lines))

    def _visible_thought_line(self, event: dict[str, Any]) -> str:
        kind = str(event.get("kind") or "")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return ""
        if kind == "Planner":
            action = payload.get("action") or "-"
            target = payload.get("role") or payload.get("tool") or "final"
            reason = payload.get("reason") or payload.get("plan_progress") or ""
            if reason:
                return self._single_line(f"Planner: {action}->{target} | {reason}", max_chars=140)
            return self._single_line(f"Planner: {action}->{target}", max_chars=140)
        if kind == "Tool" and str(payload.get("tool") or "") == "read_attachment":
            result = payload.get("result")
            if isinstance(result, dict):
                attachments = result.get("attachments") or []
                dossier_count = 0
                field_count = 0
                names: list[str] = []
                for item in attachments[:4]:
                    if not isinstance(item, dict):
                        continue
                    if item.get("extraction_ref"):
                        dossier_count += 1
                    field_count += len(item.get("field_inventory") or [])
                    names.append(str(item.get("name") or "-"))
                if attachments:
                    return self._single_line(
                        f"Attachment extraction: {len(attachments)} file(s), dossiers={dossier_count}, fields={field_count}, files={', '.join(names)}",
                        max_chars=150,
                    )
        if kind == "Role" and str(payload.get("role") or "") == "evidence_reviewer":
            result = payload.get("result")
            if isinstance(result, dict):
                metadata_rows = []
                patch = result.get("suggested_patch") or {}
                for evidence in list(patch.get("add_evidence") or patch.get("evidence_items") or [])[:6]:
                    if isinstance(evidence, dict):
                        metadata = evidence.get("metadata") or {}
                        metadata_rows.append(metadata)
                chain_count = sum(len(row.get("evidence_chain") or []) for row in metadata_rows if isinstance(row, dict))
                field_count = len(result.get("extracted_fields") or {})
                reason = result.get("reason") or result.get("reply_to_user") or ""
                if chain_count or field_count:
                    return self._single_line(
                        f"Evidence reviewer: fields={field_count}, chain_rows={chain_count} | {reason}",
                        max_chars=150,
                    )
                if reason:
                    return self._single_line(f"Evidence reviewer: {reason}", max_chars=150)
        if kind == "Model" and str(payload.get("role") or "") in {"planner", "evidence_reviewer"}:
            summary = _reason_from_model_payload(payload)
            if summary:
                return self._single_line(f"{payload.get('role')}: {summary}", max_chars=140)
        return ""

    def _running_elapsed(self, trace: dict[str, Any] | None = None) -> str:
        started = self.running_started_at
        now = datetime.now()
        if trace and trace.get("started_at"):
            try:
                parsed = datetime.fromisoformat(str(trace.get("started_at")).replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    started = parsed
                    now = datetime.now()
                else:
                    started = parsed
                    now = datetime.now(timezone.utc)
            except ValueError:
                started = self.running_started_at
        if started is None:
            return "0s"
        seconds = max(0, int((now - started).total_seconds()))
        if seconds < 60:
            return f"{seconds}s"
        return f"{seconds // 60}m {seconds % 60}s"

    def _set_activity_status(self, status: str, detail: str, color: str) -> None:
        self.agent_status_var.set(status)
        self.agent_status_detail_var.set(detail)
        self.run_state_var.set(status)
        if hasattr(self, "activity_dot"):
            self.activity_dot.itemconfigure(self.activity_dot_id, fill=color, outline=color)

    def _choose_files(self) -> None:
        paths = filedialog.askopenfilenames(title="Choose evidence files")
        self._add_files([Path(path) for path in paths])

    def _drop_files(self, event: tk.Event[Any]) -> None:
        try:
            raw_paths = self.root.tk.splitlist(event.data)
        except Exception:
            raw_paths = str(event.data or "").split()
        self._add_files([Path(path) for path in raw_paths])

    def _add_files(self, paths: list[Path]) -> None:
        existing = {path.resolve() for path in self.pending_files if path.exists()}
        added = 0
        skipped = 0
        for path in self._expand_paths(paths):
            try:
                resolved = path.expanduser().resolve()
            except Exception:
                skipped += 1
                continue
            if not resolved.is_file() or resolved in existing:
                skipped += 1
                continue
            self.pending_files.append(resolved)
            existing.add(resolved)
            added += 1
        if not added and paths:
            messagebox.showinfo("No files added", "Only existing files can be attached.")
        elif skipped:
            self.drop_label_var.set(f"Staged {added} file(s). Skipped duplicates, folders overflow, or unreadable paths.")
        self._refresh_files()

    def _expand_paths(self, paths: list[Path]) -> list[Path]:
        expanded: list[Path] = []
        for path in paths:
            try:
                resolved = path.expanduser().resolve()
            except Exception:
                continue
            if resolved.is_file():
                expanded.append(resolved)
                continue
            if resolved.is_dir():
                for child in resolved.rglob("*"):
                    if child.is_file():
                        expanded.append(child)
                        if len(expanded) >= MAX_DROPPED_FILES:
                            return expanded
        return expanded[:MAX_DROPPED_FILES]

    def _remove_selected_file(self) -> None:
        if self.pending_files:
            self.pending_files.pop()
        self._refresh_files()

    def _remove_file(self, index: int) -> None:
        if 0 <= index < len(self.pending_files):
            self.pending_files.pop(index)
            self._refresh_files()

    def _clear_files(self) -> None:
        self.pending_files = []
        self._refresh_files()

    def _attachment_for_path(self, path: Path) -> Attachment:
        content_type = mimetypes.guess_type(path.name)[0] or "text/plain"
        return Attachment(name=path.name, path=str(path), content_type=content_type)

    def _user_message_preview(self, message: str, attachments: list[Attachment]) -> str:
        if not attachments:
            return message
        names = ", ".join(item.name for item in attachments)
        return f"{message}\n\nAttachments: {names}"

    def _append_message(self, role: str, content: str, *, case_id: str | None = None) -> None:
        timestamp = datetime.now().strftime("%H:%M")
        target_case_id = case_id or self.active_case_id
        self.case_messages.setdefault(target_case_id, []).append((role, timestamp, content))
        self._refresh_messages()
        self._refresh_case_list()

    def _refresh_all(self) -> None:
        self.title_label.configure(text=self._case_display_name(self.active_case_id))
        self._refresh_case_list()
        self._refresh_files()
        self._refresh_details()
        self._refresh_trace()
        self._refresh_artifacts()
        self._refresh_messages()

    def _detail_tab_changed(self, _event: tk.Event[Any]) -> None:
        selected = self.detail_tabs.select()
        if selected == str(self.trace_tab):
            self.root.after_idle(self._refresh_visible_trace_tab)
        elif selected == str(self.artifacts_tab):
            self.root.after_idle(self._refresh_artifacts)

    def _refresh_visible_trace_tab(self) -> None:
        self._refresh_trace()
        self._sync_trace_timeline_canvas()

    def _sync_trace_timeline_canvas(self) -> None:
        self.trace_scroller.body.update_idletasks()
        self.trace_scroller.canvas.update_idletasks()
        self.trace_scroller.canvas.configure(scrollregion=self.trace_scroller.canvas.bbox("all"))
        self.trace_scroller.canvas.yview_moveto(0.0)

    def _refresh_case_list(self) -> None:
        known = self._known_case_ids()
        query = self.case_filter_var.get().strip().lower()
        visible: list[str] = []
        for case_id in known:
            if not query or query in case_id.lower():
                visible.append(case_id)
                continue
            try:
                state = self.store.load(case_id)
                if query in state.status.lower() or query in state.summary.lower():
                    visible.append(case_id)
            except Exception:
                if query in "unreadable":
                    visible.append(case_id)
        if self.active_case_id not in visible:
            visible.insert(0, self.active_case_id)
        self.case_list_case_ids = visible
        self.case_cards = {}
        for child in self.case_list_body.winfo_children():
            child.destroy()
        self.case_count_var.set(f"{len(visible)} shown")
        for index, case_id in enumerate(visible):
            try:
                state = self.store.load(case_id)
                status = state.status
                evidence_count = len(state.evidence_items)
                summary = state.summary
            except Exception:
                status = "unreadable"
                evidence_count = 0
                summary = ""
            self._add_case_card(case_id, status, evidence_count, summary, selected=case_id == self.active_case_id)
        self.case_scroller.canvas.configure(scrollregion=self.case_scroller.canvas.bbox("all"))

    def _refresh_messages(self) -> None:
        for child in self.message_body.winfo_children():
            child.destroy()
        self.message_content_labels = []
        rows = self.case_messages.get(self.active_case_id, [])
        if not rows:
            self._insert_message_block(
                "assistant",
                "--",
                "Start by asking what materials are needed, submit evidence, or request a report.",
            )
        for role, timestamp, content in rows:
            self._insert_message_block(role, timestamp, content)
        self._schedule_message_scroll_to_bottom()

    def _refresh_files(self) -> None:
        for child in self.file_chip_frame.winfo_children():
            child.destroy()
        count = len(self.pending_files)
        self.file_count_var.set(f"{count} file{'s' if count != 1 else ''}")
        if count:
            self.drop_label.configure(fg=COLORS["accent_2"])
            if "Staged" not in self.drop_label_var.get():
                self.drop_label_var.set("Files are staged for the next turn.")
            for index, path in enumerate(self.pending_files[:6]):
                self._add_file_chip(index, path)
            if count > 6:
                tk.Label(
                    self.file_chip_frame,
                    text=f"+{count - 6} more",
                    bg=COLORS["panel_2"],
                    fg=COLORS["muted"],
                    font=("Segoe UI", 9, "bold"),
                    padx=6,
                    pady=4,
                ).pack(side=tk.LEFT, pady=(0, 2))
        else:
            self.drop_label.configure(fg=COLORS["muted"])
            self.drop_label_var.set("Drop files or folders here.")
            empty = tk.Label(
                self.file_chip_frame,
                text="No context files staged",
                bg=COLORS["panel_2"],
                fg=COLORS["subtle"],
                font=("Segoe UI", 9),
            )
            empty.pack(side=tk.LEFT)

    def _add_case_card(self, case_id: str, status: str, evidence_count: int, summary: str, *, selected: bool) -> None:
        status_color = STATUS_COLORS.get(status, COLORS["muted"])
        bg = COLORS["panel_3"] if selected else COLORS["panel_2"]
        border = status_color if selected else COLORS["border"]
        card = RoundedFrame(
            self.case_list_body,
            bg=COLORS["panel"],
            fill=bg,
            outline=border,
            radius=10,
            min_height=76,
            cursor="hand2",
        )
        card.pack(fill=tk.X, padx=4, pady=(0, 8))
        self.case_cards[case_id] = card
        surface = card.inner

        rail = tk.Frame(surface, bg=status_color, width=4)
        rail.pack(side=tk.LEFT, fill=tk.Y)
        body = tk.Frame(surface, bg=bg)
        body.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=9, pady=8)

        top = tk.Frame(body, bg=bg)
        top.pack(fill=tk.X)
        tk.Label(
            top,
            text=self._case_display_name(case_id),
            bg=bg,
            fg=COLORS["text"] if selected else COLORS["muted"],
            anchor=tk.W,
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Label(
            top,
            text=self._short_status(status),
            bg=self._status_surface(status),
            fg=status_color,
            padx=6,
            pady=2,
            font=("Segoe UI", 7, "bold"),
        ).pack(side=tk.RIGHT)

        meta = tk.Frame(body, bg=bg)
        meta.pack(fill=tk.X, pady=(5, 0))
        tk.Label(
            meta,
            text=f"{evidence_count} evidence",
            bg=bg,
            fg=COLORS["subtle"],
            font=("Segoe UI", 8),
        ).pack(side=tk.LEFT)
        if summary:
            tk.Label(
                body,
                text=self._single_line(summary, max_chars=34),
                bg=bg,
                fg=COLORS["subtle"],
                anchor=tk.W,
                font=("Segoe UI", 8),
            ).pack(fill=tk.X, pady=(3, 0))

        for widget in (card, card.canvas, surface, rail, body, top, meta):
            widget.bind("<Button-1>", lambda _event, item=case_id: self.switch_case(item))
        for container in (top, meta, body):
            for child in container.winfo_children():
                child.bind("<Button-1>", lambda _event, item=case_id: self.switch_case(item))
        for widget in (card, card.canvas, surface):
            widget.bind("<Enter>", lambda _event, frame=card, item=case_id: self._hover_case_card(frame, item, True))
            widget.bind("<Leave>", lambda _event, frame=card, item=case_id: self._hover_case_card(frame, item, False))

    def _hover_case_card(self, card: tk.Frame, case_id: str, hover: bool) -> None:
        if case_id == self.active_case_id:
            return
        outline = COLORS["border_strong"] if hover else COLORS["border"]
        if hasattr(card, "set_colors"):
            card.set_colors(outline=outline)  # type: ignore[attr-defined]
        else:
            card.configure(highlightbackground=outline)

    def _case_display_name(self, case_id: str) -> str:
        display = case_id
        try:
            summary = str(self.store.load(case_id).summary or "").strip()
            if summary:
                display = summary
        except Exception:
            display = case_id
        if len(display) <= 30:
            return display
        return f"{display[:24]}...{display[-4:]}"

    def _status_surface(self, status: str) -> str:
        return {
            "new": COLORS["panel_3"],
            "collecting_materials": COLORS["amber_surface"],
            "ready_for_report": COLORS["violet_surface"],
            "report_generated": COLORS["teal_surface"],
            "unreadable": COLORS["rose_surface"],
        }.get(status, COLORS["panel_3"])

    def _refresh_details(self) -> None:
        try:
            self.case_state = self.store.load(self.active_case_id)
        except Exception as exc:
            if self.case_state is None:
                self.summary_var.set(f"Failed to load state: {type(exc).__name__}: {exc}")
                return
        state = self.case_state
        status_color = STATUS_COLORS.get(state.status, COLORS["muted"])
        self.status_label.configure(text=self._short_status(state.status), fg=status_color)
        satisfied = sum(1 for item in state.requirements if item.status in {"accepted", "satisfied"})
        run_count = len(self._load_case_runs(state.case_id))
        self.summary_var.set(
            f"{state.case_id} - {state.status}\n"
            f"Evidence: {len(state.evidence_items)} - Missing: {len(state.missing_materials)}"
        )
        self.state_metrics_var.set(
            f"requirements {satisfied}/{len(state.requirements)}   evidence {len(state.evidence_items)}   runs {run_count}   risks {len(state.risk_flags)}"
        )

        self._replace_tree(
            self.requirements_tree,
            [
                (
                    requirement.label,
                    _display_requirement_status(requirement),
                    ", ".join(requirement.evidence_ids) or "-",
                )
                for requirement in state.requirements
            ],
        )

        notes: list[str] = []
        if state.risk_flags:
            notes.append("Risk flags:")
            notes.extend(f"- {item}" for item in state.risk_flags)
        if state.next_questions:
            notes.append("Next questions:")
            notes.extend(f"- {item}" for item in state.next_questions)
        if state.missing_materials:
            notes.append("Missing materials:")
            notes.extend(f"- {item}" for item in state.missing_materials)
        self._set_text(self.notes, "\n".join(notes) if notes else "No risk flags or next questions yet.")

        self.evidence_rows = list(state.evidence_items[-10:])
        self.evidence_tree.delete(*self.evidence_tree.get_children())
        for index, item in enumerate(self.evidence_rows):
            self.evidence_tree.insert("", tk.END, iid=str(index), text=item.id, values=(item.type, item.credibility))
        if self.evidence_rows:
            self.evidence_tree.selection_set("0")
            self.evidence_tree.focus("0")
            self._show_evidence(self.evidence_rows[0])
        else:
            self._set_text(self.evidence_detail, "No evidence submitted yet.")

    def _refresh_trace(self) -> None:
        self._refresh_run_selector()
        trace = self.case_traces.get(self.active_case_id)
        if not trace:
            self.trace_header_var.set("No run trace loaded for this case.")
            self.trace_subtitle_var.set("No run trace yet")
            self.trace_summary_var.set("No run selected")
            self._refresh_trace_metrics({}, [])
            self._sync_trace_filter_buttons()
            self._sync_trace_detail_buttons()
            self._clear_trace_tree()
            self.trace_selected_title_var.set("No event selected")
            self._set_text(self.trace_text, "Send a turn or use Reload trace after a run.")
            return

        run_id = str(trace.get("run_id") or "unknown")
        step_count = trace.get("step_count", 0)
        max_steps = trace.get("max_steps", "?")
        planner_actions = trace.get("planner_actions") or []
        role_calls = trace.get("role_calls") or []
        tool_calls = trace.get("tool_calls") or []
        model_calls = trace.get("model_calls") or []
        self.trace_header_var.set(
            f"{run_id} - steps {step_count}/{max_steps} - "
            f"{len(planner_actions)} planner / {len(role_calls)} role / {len(tool_calls)} tool / {len(model_calls)} model calls"
        )
        self.trace_subtitle_var.set(f"Latest trace: {run_id} - {len(planner_actions)} planner step(s)")
        all_events = self._build_trace_events(trace)
        self.trace_summary_var.set(self._run_summary(trace))
        self._refresh_trace_metrics(trace, all_events)
        self._sync_trace_filter_buttons()
        self._sync_trace_detail_buttons()
        events = self._filtered_trace_events(all_events)
        self.trace_event_rows = events
        self._populate_trace_tree(events)
        if events:
            self._select_trace_event_index(self._default_trace_event_index(events))
        else:
            self.trace_selected_title_var.set("No event selected")
            self._set_text(self.trace_text, "No trace events match the current filter.")

    def _refresh_trace_metrics(self, trace: dict[str, Any], events: list[dict[str, Any]]) -> None:
        if not trace:
            for var in self.trace_metric_vars.values():
                var.set("-")
            return
        planner_actions = trace.get("planner_actions") or []
        tool_calls = trace.get("tool_calls") or []
        checkpoints = trace.get("trace_checkpoints") or trace.get("checkpoints") or []
        errors = sum(1 for event in events if event.get("status") == "error")
        phases = len(trace.get("phase_history") or [])
        values = {
            "events": str(len(events)),
            "phases": str(phases or len(planner_actions)),
            "checkpoints": str(len(checkpoints)),
            "tools": str(len(tool_calls)),
            "errors": str(errors),
        }
        captions = {
            "events": f"steps {trace.get('step_count', 0)}/{trace.get('max_steps', '?')}",
            "phases": "planner flow",
            "checkpoints": "saved states",
            "tools": "tool calls",
            "errors": "needs attention" if errors else "no errors",
        }
        for label, var in self.trace_metric_vars.items():
            var.set(values.get(label, "-"))
        for label, var in self.trace_metric_caption_vars.items():
            var.set(captions.get(label, ""))

    def _refresh_run_selector(self) -> None:
        turns = self._load_case_turns(self.active_case_id)
        labels = [str(item.get("label") or item.get("run_id") or "") for item in turns]
        self.run_selector.configure(values=labels)
        current = self.selected_run_var.get()
        if not labels:
            self.selected_run_var.set("")
            self.trace_turn_context_var.set("No conversation turn trace for this case.")
            self.case_traces.pop(self.active_case_id, None)
            return
        selected = next((item for item in turns if item.get("label") == current), turns[0])
        self.selected_run_var.set(str(selected.get("label") or ""))
        trace = selected.get("trace") or {}
        if trace:
            self.case_traces[self.active_case_id] = trace
        self.trace_turn_context_var.set(self._turn_context_text(selected))

    def _run_selected(self, _event: tk.Event[Any] | None = None) -> None:
        label = self.selected_run_var.get()
        for item in self.case_trace_turns.get(self.active_case_id, []):
            if item.get("label") == label:
                self.case_traces[self.active_case_id] = item.get("trace") or {}
                self.trace_turn_context_var.set(self._turn_context_text(item))
                self._refresh_trace()
                self._refresh_artifacts()
                return

    def _run_summary(self, trace: dict[str, Any]) -> str:
        observations = trace.get("observations") or []
        artifacts = [item for item in observations if isinstance(item, dict) and item.get("artifact_ref")]
        checkpoints = trace.get("trace_checkpoints") or trace.get("checkpoints") or []
        parts: list[str] = []
        if trace.get("phase"):
            parts.append(f"Phase {trace.get('phase')}")
        if trace.get("session_id"):
            parts.append(f"session {trace.get('session_id')}")
        if trace.get("turn_id"):
            parts.append(f"turn {trace.get('turn_id')}")
        parts.extend(
            [
                f"{len(observations)} observation(s)",
                f"{len(artifacts)} artifact(s)",
                f"{len(checkpoints)} checkpoint(s)",
                "compacted" if trace.get("session_compacted") else "not compacted",
            ]
        )
        return " - ".join(parts)

    def _build_trace_events(self, trace: dict[str, Any]) -> list[dict[str, Any]]:
        debug_events = self._trace_debug_events(trace)
        if debug_events:
            return self._with_turn_boundary_events(trace, build_debug_timeline_events(debug_events))

        events: list[dict[str, Any]] = []
        for index, item in enumerate(list(trace.get("phase_history") or []), start=1):
            events.append(
                {
                    "kind": "Phase",
                    "name": f"{index}. {item.get('phase', '-')}",
                    "detail": item.get("detail") or f"step {item.get('step_count', '-')}",
                    "status": "ok",
                    "payload": item,
                    "tag": "planner",
                }
            )
        for index, action in enumerate(list(trace.get("planner_actions") or []), start=1):
            target = action.get("role") or action.get("tool") or action.get("action") or "-"
            detail = action.get("plan_progress") or action.get("reason") or ""
            events.append(
                {
                    "kind": "Planner",
                    "name": f"{index}. {target}",
                    "detail": detail,
                    "status": "ok",
                    "payload": action,
                    "tag": "planner",
                }
            )
        for call in list(trace.get("role_calls") or []):
            events.append(
                {
                    "kind": "Role",
                    "name": str(call.get("role") or "-"),
                    "detail": call.get("result_preview") or call.get("ts") or "",
                    "status": "error" if call.get("error") else "ok",
                    "payload": call,
                    "tag": "error" if call.get("error") else "normal",
                }
            )
        for call in list(trace.get("tool_calls") or []):
            events.append(
                {
                    "kind": "Tool",
                    "name": str(call.get("tool") or "-"),
                    "detail": call.get("result_preview") or call.get("ts") or "",
                    "status": "error" if call.get("error") else "ok",
                    "payload": call,
                    "tag": "error" if call.get("error") else "tool",
                }
            )
        for call in list(trace.get("model_calls") or []):
            name = f"{call.get('role', '-')}"
            if call.get("model"):
                name = f"{name} / {call.get('model')}"
            events.append(
                {
                    "kind": "Model",
                    "name": name,
                    "detail": call.get("output_preview") or call.get("input_preview") or "",
                    "status": "error" if call.get("error") else "ok",
                    "payload": call,
                    "tag": "error" if call.get("error") else "model",
                }
            )
        for index, observation in enumerate(list(trace.get("observations") or []), start=1):
            if not isinstance(observation, dict):
                continue
            has_error = bool(observation.get("error"))
            events.append(
                {
                    "kind": "Observation",
                    "name": f"{index}. {observation.get('kind', '-')}/{observation.get('name', '-')}",
                    "detail": observation.get("summary") or observation.get("next_action_hint") or "",
                    "status": "error" if has_error else "ok",
                    "payload": observation,
                    "tag": "error" if has_error else "normal",
                }
            )
            if observation.get("artifact_ref"):
                events.append(
                    {
                        "kind": "Artifact",
                        "name": str(observation.get("artifact_ref")),
                        "detail": observation.get("summary") or "",
                        "status": "saved",
                        "payload": observation,
                        "tag": "tool",
                    }
                )
        for item in list(trace.get("trace_checkpoints") or trace.get("checkpoints") or []):
            events.append(
                {
                    "kind": "Checkpoint",
                    "name": str(item.get("checkpoint_id") or "-"),
                    "detail": f"step {item.get('step_count', '-')}, action {item.get('planner_action', '-')}",
                    "status": "saved",
                    "payload": item,
                    "tag": "normal",
                }
            )
        for manifest in self._context_manifest_rows(trace):
            events.append(
                {
                    "kind": "Artifact",
                    "name": manifest.get("name", "context_manifest"),
                    "detail": manifest.get("summary", ""),
                    "status": "saved",
                    "payload": manifest,
                    "tag": "tool",
                }
            )
        return self._with_turn_boundary_events(trace, events)

    def _with_turn_boundary_events(self, trace: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        turn = self._selected_trace_turn(trace)
        if not turn:
            return events
        start_event = self._conversation_turn_event(turn, "user")
        end_event = self._conversation_turn_event(turn, "assistant")
        wrapped: list[dict[str, Any]] = []
        if start_event:
            wrapped.append(start_event)
        wrapped.extend(events)
        if end_event:
            wrapped.append(end_event)
        return wrapped

    def _selected_trace_turn(self, trace: dict[str, Any]) -> dict[str, Any] | None:
        label = self.selected_run_var.get()
        run_id = str(trace.get("run_id") or "")
        turns = self.case_trace_turns.get(self.active_case_id) or []
        for item in turns:
            if item.get("label") == label:
                return item
        if run_id:
            for item in turns:
                if str(item.get("run_id") or "") == run_id:
                    return item
        return None

    def _conversation_turn_event(self, turn: dict[str, Any], role: str) -> dict[str, Any] | None:
        trace = turn.get("trace") or {}
        is_user = role == "user"
        content = str(turn.get("user" if is_user else "assistant") or "")
        if not content:
            content = str(trace.get("user_message_summary" if is_user else "final_answer") or "")
        if not content:
            return None
        ts = str(turn.get("user_ts" if is_user else "assistant_ts") or "")
        if not ts:
            ts = str(trace.get("started_at" if is_user else "completed_at") or "")
        label = "User question" if is_user else "Assistant answer"
        payload = {
            "kind": "conversation_turn",
            "role": role,
            "event": "question" if is_user else "answer",
            "content": content,
            "summary": self._single_line(content, max_chars=220),
            "case_id": turn.get("case_id") or trace.get("case_id") or self.active_case_id,
            "turn_id": turn.get("turn_id") or trace.get("turn_id") or "",
            "run_id": turn.get("run_id") or trace.get("run_id") or "",
            "ts": ts,
        }
        return {
            "kind": "Turn",
            "name": label,
            "detail": payload["summary"],
            "status": "ok",
            "payload": payload,
            "tag": "normal",
        }

    def _trace_debug_events(self, trace: dict[str, Any]) -> list[dict[str, Any]]:
        case_id = str(trace.get("case_id") or self.active_case_id)
        return load_debug_events(self.store.ensure_case_dirs(case_id), trace)

    def _filtered_trace_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        selected = self.trace_filter_var.get()
        filtered = events
        if selected == "Errors":
            filtered = [event for event in filtered if event.get("status") == "error"]
        elif selected != "All Events":
            wanted = {
                "Turn": "Turn",
                "Planner": "Planner",
                "Roles": "Role",
                "Tools": "Tool",
                "Models": "Model",
                "Observations": "Observation",
                "Artifacts": "Artifact",
                "Checkpoints": "Checkpoint",
            }.get(selected)
            if wanted is not None:
                filtered = [event for event in filtered if event.get("kind") == wanted]
        query = self.trace_search_var.get().strip().lower()
        if not query:
            return filtered
        return [event for event in filtered if query in self._trace_search_blob(event)]

    def _trace_search_blob(self, event: dict[str, Any]) -> str:
        payload = event.get("payload") or {}
        parts = [str(event.get("kind") or ""), str(event.get("name") or ""), str(event.get("detail") or ""), str(event.get("status") or "")]
        if isinstance(payload, dict):
            for key in (
                "role",
                "tool",
                "model",
                "action",
                "event",
                "content",
                "summary",
                "reason",
                "plan_progress",
                "result_preview",
                "raw_response",
                "output_preview",
                "artifact_ref",
                "error",
            ):
                value = payload.get(key)
                if value not in (None, "", [], {}):
                    parts.append(str(value))
        return " ".join(parts).lower()

    def _populate_trace_tree(self, events: list[dict[str, Any]]) -> None:
        self._clear_trace_tree()
        self.trace_event_rows = events
        for index, event in enumerate(events):
            status = str(event.get("status") or "-")
            detail = self._single_line(f"[{status}] {event.get('detail') or ''}", max_chars=88)
            tag = str(event.get("tag") or "normal")
            self.trace_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    event.get("kind", ""),
                    self._single_line(str(event.get("name") or ""), max_chars=24),
                    detail,
                ),
                tags=(tag,),
            )
            self._add_trace_event_card(index, event)
        self._sync_trace_timeline_canvas()

    def _clear_trace_tree(self) -> None:
        self.trace_event_rows = []
        self.trace_event_cards = []
        self.selected_trace_event_index = None
        for child in self.trace_timeline_body.winfo_children():
            child.destroy()
        self.trace_tree.delete(*self.trace_tree.get_children())

    def _trace_event_selected(self, _event: tk.Event[Any]) -> None:
        selection = self.trace_tree.selection()
        if not selection:
            return
        try:
            index = int(selection[0])
        except (ValueError, IndexError):
            return
        self._select_trace_event_index(index)

    def _add_trace_event_card(self, index: int, event: dict[str, Any]) -> None:
        meta = self._trace_event_meta(event)
        card = RoundedFrame(
            self.trace_timeline_body,
            bg=COLORS["input"],
            fill=COLORS["panel_2"],
            outline=COLORS["border"],
            radius=9,
            min_height=58,
            cursor="hand2",
        )
        card.pack(fill=tk.X, padx=8, pady=(7, 0))
        self.trace_event_cards.append(card)
        surface = card.inner
        surface.grid_columnconfigure(3, weight=1)

        tk.Label(
            surface,
            text=self._trace_event_time(event),
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            font=("Cascadia Mono", 8),
            width=7,
            anchor=tk.W,
        ).grid(row=0, column=0, sticky=tk.NS, padx=(8, 0), pady=8)
        axis = tk.Canvas(surface, width=20, height=48, bg=COLORS["panel_2"], bd=0, highlightthickness=0)
        axis.grid(row=0, column=1, sticky=tk.NS, pady=2)
        axis.create_line(10, 0, 10, 48, fill=COLORS["border_strong"], width=1)
        axis.create_oval(5, 19, 15, 29, outline=meta["color"], fill=meta["bg"], width=2)
        tk.Label(
            surface,
            text=self._trace_kind_label(event),
            bg=meta["bg"],
            fg=meta["color"],
            padx=7,
            pady=3,
            font=("Segoe UI", 8, "bold"),
        ).grid(row=0, column=2, sticky=tk.W, padx=(6, 8), pady=8)
        title_frame = tk.Frame(surface, bg=COLORS["panel_2"])
        title_frame.grid(row=0, column=3, sticky=tk.EW, pady=7)
        tk.Label(
            title_frame,
            text=self._single_line(self._trace_event_title(event), max_chars=42),
            bg=COLORS["panel_2"],
            fg=COLORS["text"],
            font=("Segoe UI", 9),
            anchor=tk.W,
        ).pack(anchor=tk.W, fill=tk.X)
        detail = self._single_line(self._trace_card_detail(event), max_chars=44)
        if detail:
            tk.Label(
                title_frame,
                text=detail,
                bg=COLORS["panel_2"],
                fg=COLORS["muted"],
                font=("Segoe UI", 8),
                anchor=tk.W,
            ).pack(anchor=tk.W, fill=tk.X, pady=(2, 0))
        tk.Label(
            surface,
            text=self._single_line(self._trace_event_actor(event), max_chars=12),
            bg=COLORS["panel_2"],
            fg=COLORS["muted"],
            font=("Segoe UI", 8),
            width=10,
            anchor=tk.W,
        ).grid(row=0, column=4, sticky=tk.NS, padx=(6, 0), pady=8)
        tk.Label(
            surface,
            text=self._trace_event_duration(event),
            bg=COLORS["panel_2"],
            fg=COLORS["subtle"],
            font=("Cascadia Mono", 8),
            width=5,
            anchor=tk.E,
        ).grid(row=0, column=5, sticky=tk.NS, padx=(3, 6), pady=8)
        tk.Label(
            surface,
            text=self._trace_event_status_label(event),
            bg=COLORS["panel_2"],
            fg=COLORS["danger"] if event.get("status") == "error" else COLORS["ok"],
            font=("Cascadia Mono", 8, "bold"),
            width=3,
            anchor=tk.E,
        ).grid(row=0, column=6, sticky=tk.NS, padx=(0, 8), pady=8)

        for widget in (card, card.canvas, surface, axis, title_frame):
            widget.bind("<Button-1>", lambda _event, row=index: self._select_trace_event_index(row))
        for child in surface.winfo_children():
            child.bind("<Button-1>", lambda _event, row=index: self._select_trace_event_index(row))
        for child in title_frame.winfo_children():
            child.bind("<Button-1>", lambda _event, row=index: self._select_trace_event_index(row))

    def _trace_kind_label(self, event: dict[str, Any]) -> str:
        if event.get("status") == "error":
            return "ERROR"
        return {
            "Turn": "TURN",
            "Observation": "OBS",
            "Checkpoint": "CKPT",
        }.get(str(event.get("kind") or ""), str(event.get("kind") or "EVENT")).upper()

    def _trace_event_title(self, event: dict[str, Any]) -> str:
        payload = event.get("payload") or {}
        kind = str(event.get("kind") or "")
        if isinstance(payload, dict):
            if kind == "Turn":
                return str(event.get("name") or payload.get("event") or "turn")
            if kind == "Planner":
                action = payload.get("action") or "planner"
                target = payload.get("role") or payload.get("tool") or "next step"
                return f"{action} -> {target}"
            if kind == "Role":
                return f"{payload.get('role') or event.get('name') or 'role'}"
            if kind == "Tool":
                return f"{payload.get('tool') or event.get('name') or 'tool'}"
            if kind == "Model":
                return f"{payload.get('role') or 'model'} / {payload.get('model') or 'model'}"
            if kind == "Observation":
                return str(payload.get("summary") or payload.get("name") or event.get("name") or "observation")
            if kind == "Checkpoint":
                return f"Checkpoint {payload.get('checkpoint_id') or event.get('name') or '-'}"
            if kind == "Artifact":
                return str(payload.get("artifact_ref") or payload.get("name") or event.get("name") or "artifact")
        return str(event.get("name") or event.get("detail") or "event")

    def _trace_event_time(self, event: dict[str, Any]) -> str:
        payload = event.get("payload") or {}
        value = ""
        if isinstance(payload, dict):
            value = str(payload.get("ts") or payload.get("completed_at") or payload.get("started_at") or "")
        if not value:
            return "--:--:--"
        if "T" in value and len(value) >= 19:
            return value[11:19]
        return value[:8] if len(value) >= 8 else value

    def _trace_event_actor(self, event: dict[str, Any]) -> str:
        payload = event.get("payload") or {}
        if isinstance(payload, dict):
            for key in ("role", "tool", "model", "name", "kind"):
                value = payload.get(key)
                if value:
                    return str(value)
        return str(event.get("name") or event.get("kind") or "-")

    def _trace_event_duration(self, event: dict[str, Any]) -> str:
        payload = event.get("payload") or {}
        if isinstance(payload, dict):
            for key in ("duration_ms", "latency_ms", "elapsed_ms"):
                value = payload.get(key)
                if isinstance(value, (int, float)):
                    return f"{int(value)}ms"
            for key in ("duration_s", "elapsed_s", "seconds"):
                value = payload.get(key)
                if isinstance(value, (int, float)):
                    return f"{value:.1f}s"
            usage = payload.get("usage") or payload.get("token_usage")
            if isinstance(usage, dict):
                total = usage.get("total_tokens") or usage.get("total")
                if total:
                    return f"{total} tok"
        return "--"

    def _trace_event_status_label(self, event: dict[str, Any]) -> str:
        status = str(event.get("status") or "-")
        if status == "error":
            return "ERR"
        if status == "saved":
            return "SAVE"
        return "OK"

    def _select_trace_event_index(self, index: int) -> None:
        if not (0 <= index < len(self.trace_event_rows)):
            return
        already_selected = self.selected_trace_event_index == index
        self.selected_trace_event_index = index
        if not already_selected and self.trace_tree.exists(str(index)):
            self.trace_tree.selection_set(str(index))
            self.trace_tree.focus(str(index))
        for row_index, card in enumerate(self.trace_event_cards):
            event = self.trace_event_rows[row_index] if row_index < len(self.trace_event_rows) else {}
            meta = self._trace_event_meta(event)
            selected = row_index == index
            if hasattr(card, "set_colors"):
                card.set_colors(fill=COLORS["panel_2"], outline=meta["color"] if selected else COLORS["border"])  # type: ignore[attr-defined]
            else:
                card.configure(bg=COLORS["input"], highlightbackground=meta["color"] if selected else COLORS["border"])
        self._show_trace_event(self.trace_event_rows[index])

    def _trace_event_meta(self, event: dict[str, Any]) -> dict[str, str]:
        if event.get("status") == "error":
            return TRACE_KIND_META["Error"]
        kind = str(event.get("kind") or "Phase")
        return TRACE_KIND_META.get(kind, TRACE_KIND_META["Phase"])

    def _trace_payload_hint(self, event: dict[str, Any]) -> str:
        payload = event.get("payload") or {}
        if isinstance(payload, dict):
            for key in ("summary", "reason", "plan_progress", "result_preview", "raw_response", "output_preview", "input_preview"):
                if payload.get(key):
                    return str(payload.get(key))
        return ""

    def _trace_card_detail(self, event: dict[str, Any]) -> str:
        payload = event.get("payload") or {}
        kind = str(event.get("kind") or "")
        if not isinstance(payload, dict):
            return str(event.get("detail") or "")
        if kind == "Turn":
            return str(payload.get("summary") or event.get("detail") or "")
        if kind == "Planner":
            target = payload.get("role") or payload.get("tool") or payload.get("action") or event.get("name")
            return str(payload.get("plan_progress") or payload.get("reason") or f"planner chose {target}")
        if kind == "Role":
            if payload.get("error"):
                return f"role failed: {payload.get('error')}"
            return str(payload.get("result_preview") or f"{payload.get('role', 'role')} completed")
        if kind == "Model":
            role = payload.get("role") or event.get("name") or "model"
            model = payload.get("model") or "model"
            if payload.get("error"):
                return f"{role} / {model} failed"
            return f"{role} / {model} response captured"
        if kind == "Observation":
            if payload.get("error"):
                return str(payload.get("summary") or payload.get("error"))
            return str(payload.get("summary") or payload.get("next_action_hint") or "observation recorded")
        if kind == "Checkpoint":
            return f"checkpoint after step {payload.get('step_count', '-')}, action {payload.get('planner_action', '-')}"
        return str(event.get("detail") or self._trace_payload_hint(event))

    def _show_trace_event(self, event: dict[str, Any]) -> None:
        title = f"{self._trace_kind_label(event)}  {self._trace_event_title(event)}"
        self.trace_selected_title_var.set(self._single_line(title, max_chars=72))
        self._sync_trace_detail_buttons()
        rows = self._trace_detail_rows_for_mode(event, self.trace_detail_mode_var.get())
        self._set_trace_text(rows)

    def _trace_detail_rows_for_mode(self, event: dict[str, Any], mode: str) -> list[tuple[str, str]]:
        if mode == "Thought":
            return self._trace_thought_rows(event)
        if mode == "Input":
            return self._trace_payload_section_rows(
                event,
                "Input",
                ("content", "input", "input_preview", "prompt", "system_prompt", "short_plan", "case_patch"),
            )
        if mode == "Output":
            return self._trace_payload_section_rows(
                event,
                "Output",
                ("content", "output", "raw_response", "output_preview", "result", "result_preview", "final_answer", "error"),
            )
        if mode == "Artifacts":
            return self._trace_payload_section_rows(
                event,
                "Artifacts",
                ("artifact_ref", "path", "must_preserve_refs", "extracted_fields", "context_manifest"),
            )
        if mode == "Related":
            return self._trace_payload_section_rows(
                event,
                "Related events",
                ("observations", "planner_action", "checkpoint_id", "run_id", "case_id", "next_action_hint"),
            )
        if mode == "Raw":
            payload = event.get("payload") or event
            return [("trace_heading", "Raw event payload\n"), ("normal", self._preview(payload, max_chars=2400))]
        status_tag = "trace_error" if event.get("status") == "error" else "trace_ok"
        rows: list[tuple[str, str]] = [("trace_heading", "Details\n")]
        rows.extend(self._trace_story_rows(event))
        rows.extend(
            [
                ("trace_heading", "\nEvent metadata\n"),
                ("trace_dim", f"Type: {event.get('kind', 'Event')}\n"),
                (status_tag, f"Status: {event.get('status') or '-'}\n"),
                ("trace_dim", f"Time: {self._trace_event_time(event)}\n"),
                ("trace_dim", f"Actor: {self._trace_event_actor(event)}\n"),
                ("trace_dim", f"Duration: {self._trace_event_duration(event)}\n\n"),
            ]
        )
        payload = event.get("payload") or {}
        if isinstance(payload, dict):
            keys = self._trace_key_values(event)
            if keys:
                rows.append(("trace_heading", "\nKey fields\n"))
                for key, value in keys:
                    rows.append(("trace_dim", f"{key}: {value}\n"))
        if event.get("status") == "error":
            raw_preview = self._trace_raw_preview(event)
            if raw_preview:
                rows.append(("trace_heading", "\nError payload preview\n"))
                rows.append(("trace_error", raw_preview))
        else:
            rows.append(("trace_dim", "\nUse Raw or Copy event for the full JSON payload.\n"))
        return rows

    def _trace_payload_section_rows(self, event: dict[str, Any], heading: str, keys: tuple[str, ...]) -> list[tuple[str, str]]:
        payload = event.get("payload") or {}
        rows: list[tuple[str, str]] = [("trace_heading", f"{heading}\n")]
        if not isinstance(payload, dict):
            return rows + [("normal", self._preview(payload, max_chars=1800))]
        found = False
        for key in keys:
            value = payload.get(key)
            if value in (None, "", [], {}):
                continue
            found = True
            tag = "trace_error" if key == "error" else "normal"
            rows.append(("trace_dim", f"{key}\n"))
            rows.append((tag, f"{self._preview(value, max_chars=1400)}\n\n"))
        if not found:
            fallback = self._trace_payload_hint(event) or self._trace_card_detail(event)
            if fallback:
                rows.append(("normal", f"{fallback}\n"))
            else:
                rows.append(("trace_dim", "No structured data for this view.\n"))
        return rows

    def _trace_thought_rows(self, event: dict[str, Any]) -> list[tuple[str, str]]:
        payload = event.get("payload") or {}
        rows: list[tuple[str, str]] = [("trace_heading", "Thought\n")]
        if not isinstance(payload, dict):
            return rows + [("normal", self._preview(payload, max_chars=1800))]
        kind = str(event.get("kind") or "")
        role = str(payload.get("role") or event.get("name") or "")
        if kind == "Planner":
            action = payload.get("action") or "-"
            target = payload.get("role") or payload.get("tool") or "-"
            rows.append(("trace_dim", f"action: {action} -> {target}\n\n"))
            thought = payload.get("thought") or payload.get("reason")
            if thought:
                rows.append(("trace_thought", f"{thought}\n"))
            if payload.get("plan_progress"):
                rows.append(("trace_dim", f"\nplan_progress: {payload.get('plan_progress')}\n"))
            short_plan = payload.get("short_plan")
            if short_plan not in (None, "", [], {}):
                rows.append(("trace_dim", "\nshort_plan\n"))
                rows.append(("normal", f"{self._preview(short_plan, max_chars=1200)}\n"))
            if not thought and not payload.get("plan_progress") and short_plan in (None, "", [], {}):
                rows.append(("trace_dim", "No visible planner thought was recorded for this event.\n"))
            return rows
        if kind == "Role" and role == "evidence_reviewer":
            result = payload.get("result")
            if isinstance(result, dict):
                thought = result.get("thought") or result.get("reason")
                if thought:
                    rows.append(("trace_thought", f"{thought}\n"))
                for key in ("evidence_type", "support_level", "source_traceability", "should_accept", "credibility"):
                    value = result.get(key)
                    if value not in (None, "", [], {}):
                        rows.append(("trace_dim", f"{key}: {value}\n"))
                for key in ("extracted_fields", "risk_flags", "conflicts"):
                    value = result.get(key)
                    if value not in (None, "", [], {}):
                        rows.append(("trace_dim", f"\n{key}\n"))
                        rows.append(("normal", f"{self._preview(value, max_chars=1600)}\n"))
                patch = result.get("suggested_patch") or {}
                metadata_rows: list[dict[str, Any]] = []
                if isinstance(patch, dict):
                    for evidence in list(patch.get("add_evidence") or patch.get("evidence_items") or [])[:4]:
                        if isinstance(evidence, dict) and isinstance(evidence.get("metadata"), dict):
                            metadata_rows.append(evidence["metadata"])
                for key in ("field_inventory", "page_review", "evidence_chain", "claim_to_source_refs"):
                    rows_for_key: list[Any] = []
                    for metadata in metadata_rows:
                        rows_for_key.extend(list(metadata.get(key) or [])[:8])
                    if rows_for_key:
                        rows.append(("trace_dim", f"\nmetadata.{key}\n"))
                        rows.append(("normal", f"{self._preview(rows_for_key[:12], max_chars=1800)}\n"))
                if not thought:
                    rows.append(("trace_dim", "No visible evidence reviewer thought was recorded in the result.\n"))
                return rows
            preview = payload.get("result_preview") or payload.get("summary")
            if preview:
                rows.append(("trace_thought", f"{preview}\n"))
            else:
                rows.append(("trace_dim", "No visible evidence reviewer thought was recorded for this event.\n"))
            return rows
        if kind == "Model" and role.split(" / ", 1)[0] in {"planner", "evidence_reviewer"}:
            output = payload.get("raw_response") or payload.get("output") or payload.get("output_preview")
            if output:
                rows.append(("trace_thought", f"{self._preview(output, max_chars=2200)}\n"))
            else:
                rows.append(("trace_dim", "No model output was recorded for this event.\n"))
            return rows
        rows.append(("trace_dim", "Thought view is available for Planner and evidence_reviewer events.\n"))
        return rows

    def _default_trace_event_index(self, events: list[dict[str, Any]]) -> int:
        if self.trace_detail_mode_var.get() != "Thought":
            return 0
        for index in range(len(events) - 1, -1, -1):
            if self._event_has_visible_thought(events[index]):
                return index
        return 0

    def _event_has_visible_thought(self, event: dict[str, Any]) -> bool:
        return any(tag == "trace_thought" and value.strip() for tag, value in self._trace_thought_rows(event))

    def _role_context_rows(self, payload: dict[str, Any]) -> list[tuple[str, str]]:
        role_input = payload.get("input")
        if not isinstance(role_input, dict):
            return []
        rows: list[tuple[str, str]] = [("trace_heading", "Role context\n")]
        user_message = role_input.get("user_message")
        if user_message:
            rows.append(("trace_dim", f"user_message: {self._single_line(str(user_message), max_chars=150)}\n"))
        case_state = role_input.get("case_state")
        if isinstance(case_state, dict):
            status = case_state.get("status") or "-"
            evidence_items = case_state.get("evidence_items") or []
            missing = case_state.get("missing_materials") or []
            risks = case_state.get("risk_flags") or []
            rows.append(
                (
                    "trace_dim",
                    f"case_state: status={status}; evidence={len(evidence_items)}; missing={len(missing)}; risks={len(risks)}\n",
                )
            )
            requirements = case_state.get("requirements") or []
            req_summary = self._requirements_summary(requirements)
            if req_summary:
                rows.append(("trace_dim", f"requirements: {req_summary}\n"))
            if case_state.get("summary"):
                rows.append(("trace_dim", f"case_summary: {self._single_line(str(case_state.get('summary')), max_chars=150)}\n"))
        attachments = role_input.get("attachment_context") or []
        if isinstance(attachments, list) and attachments:
            names = [str(item.get("name") or item.get("path") or "-") for item in attachments[:4] if isinstance(item, dict)]
            rows.append(("trace_dim", f"attachments: {', '.join(names)}\n"))
        rag_context = role_input.get("rag_context") or []
        if isinstance(rag_context, list) and rag_context:
            rows.append(("trace_dim", f"rag_context: {len(rag_context)} item(s)\n"))
        role_result = role_input.get("role_result")
        if isinstance(role_result, dict):
            rows.append(("trace_dim", f"prior_role_result: {self._role_result_summary(role_result)}\n"))
        return rows

    def _role_result_rows(self, payload: dict[str, Any]) -> list[tuple[str, str]]:
        result = payload.get("result")
        if not isinstance(result, dict):
            preview = payload.get("result_preview")
            return [("trace_dim", f"Result: {preview}\n")] if preview else []
        rows: list[tuple[str, str]] = [("trace_heading", "Role output\n")]
        summary = self._role_result_summary(result)
        if summary:
            rows.append(("trace_dim", f"{summary}\n"))
        extracted = result.get("extracted_fields")
        if isinstance(extracted, dict) and extracted:
            field_bits: list[str] = []
            for key, value in list(extracted.items())[:8]:
                if isinstance(value, dict):
                    actual = value.get("value") or value.get("status") or value
                else:
                    actual = value
                field_bits.append(f"{key}={self._single_line(str(actual), max_chars=32)}")
            rows.append(("trace_dim", f"extracted_fields: {', '.join(field_bits)}\n"))
        supports = result.get("supports") or []
        if isinstance(supports, list) and supports:
            support_bits = []
            for item in supports[:6]:
                if isinstance(item, dict):
                    support_bits.append(f"{item.get('requirement', '-')}: {item.get('support_level', '-')}")
            if support_bits:
                rows.append(("trace_dim", f"supports: {', '.join(support_bits)}\n"))
        patch = result.get("suggested_patch") or {}
        if isinstance(patch, dict):
            chain_rows = []
            for evidence in list(patch.get("add_evidence") or patch.get("evidence_items") or [])[:4]:
                if isinstance(evidence, dict) and isinstance(evidence.get("metadata"), dict):
                    chain_rows.extend(list(evidence["metadata"].get("evidence_chain") or [])[:6])
            if chain_rows:
                rows.append(("trace_dim", f"evidence_chain: {self._preview(chain_rows[:10], max_chars=900)}\n"))
        if result.get("reply_to_user"):
            rows.append(("trace_dim", f"reply_to_user: {self._single_line(str(result.get('reply_to_user')), max_chars=180)}\n"))
        return rows

    def _requirements_summary(self, requirements: Any) -> str:
        if not isinstance(requirements, list):
            return ""
        parts: list[str] = []
        for item in requirements[:8]:
            if not isinstance(item, dict):
                continue
            req_id = item.get("id") or item.get("label") or "-"
            status = item.get("status") or "-"
            evidence_ids = item.get("evidence_ids")
            if isinstance(evidence_ids, list) and evidence_ids:
                parts.append(f"{req_id}={status}({len(evidence_ids)})")
            else:
                parts.append(f"{req_id}={status}")
        return ", ".join(parts)

    def _role_result_summary(self, result: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("evidence_type", "credibility", "support_level", "source_traceability", "should_accept"):
            value = result.get(key)
            if value not in (None, "", [], {}):
                parts.append(f"{key}={value}")
        suggested_patch = result.get("suggested_patch")
        if isinstance(suggested_patch, dict):
            add_evidence = suggested_patch.get("add_evidence") or []
            if isinstance(add_evidence, list) and add_evidence:
                parts.append(f"add_evidence={len(add_evidence)}")
        if result.get("reason"):
            parts.append(f"reason={self._single_line(str(result.get('reason')), max_chars=110)}")
        if not parts and result.get("summary"):
            parts.append(self._single_line(str(result.get("summary")), max_chars=140))
        return "; ".join(parts)

    def _tool_result_summary(self, result: Any) -> str:
        if not isinstance(result, dict):
            return self._single_line(self._preview(result, max_chars=220), max_chars=180)
        parts: list[str] = []
        for key in ("name", "path", "content_type", "chars", "attachment_count", "status", "message"):
            value = result.get(key)
            if value not in (None, "", [], {}):
                parts.append(f"{key}={self._single_line(str(value), max_chars=64)}")
        attachments = result.get("attachments")
        if isinstance(attachments, list) and attachments:
            names = [str(item.get("name") or item.get("path") or "-") for item in attachments[:4] if isinstance(item, dict)]
            parts.append(f"attachments={', '.join(names)}")
        if not parts:
            parts = [self._single_line(self._preview({key: result.get(key) for key in list(result.keys())[:5]}, max_chars=260), max_chars=180)]
        return "; ".join(parts)

    def _trace_story_rows(self, event: dict[str, Any]) -> list[tuple[str, str]]:
        payload = event.get("payload") or {}
        kind = str(event.get("kind") or "")
        status = str(event.get("status") or "-")
        rows: list[tuple[str, str]] = [("trace_dim", f"status: {status}\n")]
        if not isinstance(payload, dict):
            detail = str(event.get("detail") or "")
            return rows + ([("normal", f"{detail}\n")] if detail else [])
        if kind == "Turn":
            role = payload.get("role") or "-"
            content = payload.get("content") or payload.get("summary") or event.get("detail") or ""
            rows.append(("normal", f"{str(role).title()}: {content}\n"))
            if payload.get("turn_id") or payload.get("run_id"):
                rows.append(("trace_dim", f"turn_id: {payload.get('turn_id') or '-'}; run_id: {payload.get('run_id') or '-'}\n"))
        elif kind == "Planner":
            action = payload.get("action") or "-"
            target = payload.get("role") or payload.get("tool") or "-"
            rows.append(("normal", f"Planner action: {action} -> {target}\n"))
            if payload.get("reason"):
                rows.append(("trace_dim", f"Reason: {payload.get('reason')}\n"))
            if payload.get("plan_progress"):
                rows.append(("trace_dim", f"Progress: {payload.get('plan_progress')}\n"))
            short_plan = payload.get("short_plan") or []
            if isinstance(short_plan, list) and short_plan:
                rows.append(("trace_dim", f"Plan: {' -> '.join(str(item) for item in short_plan[:6])}\n"))
        elif kind == "Role":
            rows.append(("normal", f"Role: {payload.get('role') or event.get('name') or '-'}\n"))
            if payload.get("error"):
                rows.append(("trace_error", f"Error: {payload.get('error')}\n"))
            rows.extend(self._role_context_rows(payload))
            rows.extend(self._role_result_rows(payload))
        elif kind == "Tool":
            rows.append(("normal", f"Tool call: {payload.get('tool') or event.get('name') or '-'}\n"))
            if payload.get("error"):
                rows.append(("trace_error", f"Error: {payload.get('error')}\n"))
            tool_input = payload.get("input")
            if tool_input not in (None, "", [], {}):
                rows.append(("trace_dim", f"Input: {self._single_line(self._preview(tool_input, max_chars=220), max_chars=180)}\n"))
            if payload.get("result") not in (None, "", [], {}):
                rows.append(("trace_dim", f"Result: {self._tool_result_summary(payload.get('result'))}\n"))
        elif kind == "Model":
            rows.append(("normal", f"Model call: {payload.get('role') or '-'} / {payload.get('model') or '-'}\n"))
            model_reason = _reason_from_model_payload(payload)
            if model_reason:
                rows.append(("trace_thought", f"Thought: {model_reason}\n"))
            if payload.get("prompt_version"):
                rows.append(("trace_dim", f"Prompt: {payload.get('prompt_version')}\n"))
            usage = payload.get("usage") or payload.get("token_usage")
            if isinstance(usage, dict):
                prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens")
                completion_tokens = usage.get("completion_tokens") or usage.get("output_tokens")
                total = usage.get("total_tokens") or usage.get("total")
                rows.append(("trace_dim", f"Tokens: in={prompt_tokens or '-'} out={completion_tokens or '-'} total={total or '-'}\n"))
            if payload.get("error"):
                rows.append(("trace_error", f"Error: {payload.get('error')}\n"))
            elif payload.get("input_preview"):
                rows.append(("trace_dim", f"Input: {self._single_line(str(payload.get('input_preview')), max_chars=180)}\n"))
            elif payload.get("output_preview"):
                rows.append(("trace_dim", f"Output: {self._single_line(str(payload.get('output_preview')), max_chars=180)}\n"))
        elif kind == "Observation":
            summary = payload.get("summary") or payload.get("next_action_hint") or event.get("detail") or ""
            rows.append(("trace_error" if payload.get("error") else "normal", f"Observation: {summary}\n"))
            key_facts = payload.get("key_facts") or []
            if isinstance(key_facts, list) and key_facts:
                rows.append(("trace_dim", f"Key facts: {', '.join(str(item) for item in key_facts[:6])}\n"))
            if payload.get("artifact_ref"):
                rows.append(("trace_dim", f"Artifact: {payload.get('artifact_ref')}\n"))
        elif kind == "Checkpoint":
            rows.append(("normal", f"Saved checkpoint after step {payload.get('step_count', '-')}\n"))
            if payload.get("planner_action"):
                action = payload.get("planner_action")
                if isinstance(action, dict):
                    target = action.get("role") or action.get("tool") or "-"
                    rows.append(("trace_dim", f"Planner action: {action.get('action') or '-'} -> {target}\n"))
                    if action.get("plan_progress"):
                        rows.append(("trace_dim", f"Progress: {action.get('plan_progress')}\n"))
                else:
                    rows.append(("trace_dim", f"Planner action: {action}\n"))
            observations = payload.get("observations") or []
            if isinstance(observations, list):
                rows.append(("trace_dim", f"Observations captured: {len(observations)}\n"))
        else:
            detail = str(event.get("detail") or self._trace_payload_hint(event) or "")
            if detail:
                rows.append(("normal", f"{detail}\n"))
        return rows

    def _trace_raw_preview(self, event: dict[str, Any]) -> str:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return self._preview(payload, max_chars=900)
        compact = {
            key: payload.get(key)
            for key in ("action", "role", "tool", "model", "error", "summary", "result_preview", "raw_response", "output_preview", "artifact_ref")
            if payload.get(key) not in (None, "", [], {})
        }
        if not compact:
            compact = {key: payload.get(key) for key in list(payload.keys())[:6]}
        return self._preview(compact, max_chars=1200)

    def _copy_selected_trace_event(self) -> None:
        event = self._selected_trace_event()
        if not event:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(json.dumps(event.get("payload") or event, ensure_ascii=False, indent=2, default=str))
        self.run_state_var.set("Trace event copied")

    def _selected_trace_event(self) -> dict[str, Any] | None:
        if self.selected_trace_event_index is not None and 0 <= self.selected_trace_event_index < len(self.trace_event_rows):
            return self.trace_event_rows[self.selected_trace_event_index]
        selection = self.trace_tree.selection()
        if not selection:
            return None
        try:
            return self.trace_event_rows[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def _trace_key_values(self, event: dict[str, Any]) -> list[tuple[str, str]]:
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            return []
        kind = str(event.get("kind") or "")
        candidate_keys = {
            "Turn": ("role", "event", "turn_id", "run_id", "ts", "summary"),
            "Phase": ("phase", "step_count", "detail", "ts"),
            "Planner": ("action", "role", "tool", "reason", "plan_progress"),
            "Role": ("role", "ts", "error", "result_preview"),
            "Tool": ("tool", "ts", "error", "result_preview"),
            "Model": ("role", "model", "prompt_version", "finish_reason", "error", "output_preview"),
            "Observation": ("kind", "name", "summary", "next_action_hint", "artifact_ref", "error"),
            "Artifact": ("name", "summary", "path", "artifact_ref"),
            "Checkpoint": ("checkpoint_id", "run_id", "step_count", "phase", "ts"),
        }.get(kind, ())
        rows: list[tuple[str, str]] = []
        for key in candidate_keys:
            value = payload.get(key)
            if value in (None, "", [], {}):
                continue
            rows.append((key, self._single_line(self._preview(value, max_chars=300), max_chars=96)))
        return rows

    def _copy_trace_json(self) -> None:
        trace = self.case_traces.get(self.active_case_id)
        if not trace:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(json.dumps(trace, ensure_ascii=False, indent=2, default=str))
        self.run_state_var.set("Trace copied")

    def _refresh_artifacts(self) -> None:
        trace = self.case_traces.get(self.active_case_id) or {}
        self.artifact_rows = self._build_artifact_rows(trace)
        self.artifact_tree.delete(*self.artifact_tree.get_children())
        for index, row in enumerate(self.artifact_rows):
            self.artifact_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    row.get("type", ""),
                    self._single_line(str(row.get("name") or ""), max_chars=28),
                    self._single_line(str(row.get("summary") or ""), max_chars=80),
                ),
            )
        if self.artifact_rows:
            self.artifact_tree.selection_set("0")
            self.artifact_tree.focus("0")
            self._show_artifact(self.artifact_rows[0])
        else:
            self._set_text(self.artifact_text, "No artifacts or context manifests for this run.")

    def _build_artifact_rows(self, trace: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for observation in trace.get("observations") or []:
            if not isinstance(observation, dict):
                continue
            ref = str(observation.get("artifact_ref") or "")
            if not ref or ref in seen:
                continue
            seen.add(ref)
            rows.append(
                {
                    "type": "artifact",
                    "name": Path(ref).name,
                    "summary": observation.get("summary") or observation.get("next_action_hint") or ref,
                    "path": str(self.store.case_dir(self.active_case_id) / ref),
                    "payload": observation,
                }
            )
        for manifest in self._context_manifest_rows(trace):
            key = str(manifest.get("path") or manifest.get("name"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(manifest)
        run_id = str(trace.get("run_id") or "")
        if run_id:
            artifact_root = self.store.case_dir(self.active_case_id) / "traces" / "artifacts" / run_id
            if artifact_root.exists():
                for path in sorted(artifact_root.glob("*.json")):
                    key = str(path)
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(
                        {
                            "type": "artifact",
                            "name": path.name,
                            "summary": f"{path.stat().st_size} bytes",
                            "path": str(path),
                            "payload": self._read_json_file(path),
                        }
                    )
        return rows

    def _context_manifest_rows(self, trace: dict[str, Any]) -> list[dict[str, Any]]:
        run_id = str(trace.get("run_id") or "")
        if not run_id:
            return []
        root = self.store.case_dir(self.active_case_id) / "traces" / run_id
        if not root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in sorted(root.glob("context_manifest_*.json")):
            payload = self._read_json_file(path)
            target = payload.get("target") if isinstance(payload, dict) else ""
            rows.append(
                {
                    "type": "manifest",
                    "name": path.name,
                    "summary": f"{target or 'context'}; {path.stat().st_size} bytes",
                    "path": str(path),
                    "payload": payload,
                }
            )
        return rows

    def _artifact_selected(self, _event: tk.Event[Any]) -> None:
        selection = self.artifact_tree.selection()
        if not selection:
            return
        try:
            row = self.artifact_rows[int(selection[0])]
        except (ValueError, IndexError):
            return
        self._show_artifact(row)

    def _show_artifact(self, row: dict[str, Any]) -> None:
        self.artifact_text.configure(state=tk.NORMAL)
        self.artifact_text.delete("1.0", tk.END)
        self.artifact_text.insert(tk.END, f"{row.get('type', 'artifact')} - {row.get('name', '-')}\n", "artifact_heading")
        self.artifact_text.insert(tk.END, f"path: {row.get('path', '-')}\n", "artifact_dim")
        summary = str(row.get("summary") or "")
        if summary:
            self.artifact_text.insert(tk.END, f"summary: {summary}\n", "artifact_dim")
        self.artifact_text.insert(tk.END, "\n")
        self.artifact_text.insert(tk.END, self._preview(row.get("payload"), max_chars=7000))
        self.artifact_text.configure(state=tk.DISABLED)
        self.artifact_text.see("1.0")

    def _selected_artifact_row(self) -> dict[str, Any] | None:
        selection = self.artifact_tree.selection()
        if not selection:
            return None
        try:
            return self.artifact_rows[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def _open_selected_artifact(self) -> None:
        row = self._selected_artifact_row()
        if not row:
            return
        path = Path(str(row.get("path") or ""))
        if not path.exists():
            messagebox.showinfo("Artifact not found", "The selected artifact file no longer exists.")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showerror("Open failed", f"{type(exc).__name__}: {exc}")

    def _copy_selected_artifact_path(self) -> None:
        row = self._selected_artifact_row()
        if not row:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(str(row.get("path") or ""))
        self.run_state_var.set("Artifact path copied")

    def _copy_selected_artifact_json(self) -> None:
        row = self._selected_artifact_row()
        if not row:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(json.dumps(row.get("payload") or row, ensure_ascii=False, indent=2, default=str))
        self.run_state_var.set("Artifact JSON copied")

    def _evidence_selected(self, _event: tk.Event[Any]) -> None:
        selection = self.evidence_tree.selection()
        if not selection:
            return
        try:
            item = self.evidence_rows[int(selection[0])]
        except (ValueError, IndexError):
            return
        self._show_evidence(item)

    def _show_evidence(self, item: Any) -> None:
        self.evidence_detail.configure(state=tk.NORMAL)
        self.evidence_detail.delete("1.0", tk.END)
        self.evidence_detail.insert(tk.END, f"{item.id} - {item.type}\n", "evidence_heading")
        self.evidence_detail.insert(tk.END, f"credibility: {item.credibility}; source: {item.source}\n", "evidence_dim")
        if item.summary:
            self.evidence_detail.insert(tk.END, f"summary: {item.summary}\n\n")
        review_result = getattr(item, "review_result", {}) or {}
        if isinstance(review_result, dict) and review_result:
            source_doc_id = review_result.get("source_doc_id") or "-"
            traceability = review_result.get("source_traceability") or "-"
            support_level = review_result.get("support_level") or "-"
            self.evidence_detail.insert(
                tk.END,
                f"review: source_doc={source_doc_id}; traceability={traceability}; support={support_level}\n",
                "evidence_dim",
            )
            extracted_fields = review_result.get("extracted_fields") or {}
            if extracted_fields:
                self.evidence_detail.insert(tk.END, "extracted_fields:\n", "evidence_dim")
                self.evidence_detail.insert(tk.END, self._preview(extracted_fields, max_chars=1400) + "\n")
            risk_flags = review_result.get("risk_flags") or []
            if risk_flags:
                self.evidence_detail.insert(tk.END, "review_risks:\n", "evidence_dim")
                for risk in risk_flags:
                    self.evidence_detail.insert(tk.END, f"- {risk}\n")
            self.evidence_detail.insert(tk.END, "\n")
        metadata = getattr(item, "metadata", {}) or {}
        if isinstance(metadata, dict) and metadata:
            original_ref = metadata.get("original_ref")
            preview_paths = metadata.get("preview_paths") or []
            dossier_ref = metadata.get("dossier_ref") or metadata.get("extraction_ref")
            if original_ref or preview_paths or dossier_ref:
                self.evidence_detail.insert(tk.END, "source_refs:\n", "evidence_dim")
                if original_ref:
                    self.evidence_detail.insert(tk.END, f"- original_ref: {original_ref}\n")
                if dossier_ref:
                    self.evidence_detail.insert(tk.END, f"- dossier_ref: {dossier_ref}\n")
                for path in list(preview_paths)[:4]:
                    self.evidence_detail.insert(tk.END, f"- preview: {path}\n")
            field_rows = metadata.get("field_inventory") or []
            if field_rows:
                self.evidence_detail.insert(tk.END, "field_crops:\n", "evidence_dim")
                for index, row in enumerate(list(field_rows)[:10], start=1):
                    if not isinstance(row, dict):
                        continue
                    field = _evidence_field_label(row.get("field") or "-")
                    value = row.get("value") or row.get("source_quote") or ""
                    crop = row.get("crop_path") or row.get("crop_status") or "no_crop"
                    locator = row.get("locator") or "-"
                    proof = row.get("proof_label") or ""
                    self.evidence_detail.insert(
                        tk.END,
                        f"- {index}. {field}: {self._single_line(str(value), max_chars=90)} | {crop} | {locator} | {proof}\n",
                    )
            chain_rows = metadata.get("evidence_chain") or []
            if chain_rows:
                self.evidence_detail.insert(tk.END, "evidence_chain:\n", "evidence_dim")
                for row in list(chain_rows)[:8]:
                    if isinstance(row, dict):
                        claim = row.get("claim") or row.get("proof_label") or row.get("field") or "-"
                        crop = row.get("crop_path") or row.get("preview_path") or ""
                        locator = row.get("locator") or row.get("block_or_table_or_region") or ""
                        self.evidence_detail.insert(tk.END, f"- {claim} | {crop} | {locator}\n")
            block_crops = metadata.get("block_crops") or []
            if block_crops:
                self.evidence_detail.insert(tk.END, "block_crops:\n", "evidence_dim")
                for row in list(block_crops)[:8]:
                    if isinstance(row, dict):
                        self.evidence_detail.insert(
                            tk.END,
                            f"- {row.get('crop_id')}: {row.get('crop_path')} | {row.get('proves') or row.get('text')}\n",
                        )
            self.evidence_detail.insert(tk.END, "\n")
        if item.supports:
            self.evidence_detail.insert(tk.END, "supports:\n", "evidence_dim")
            for support in item.supports:
                self.evidence_detail.insert(
                    tk.END,
                    f"- {support.requirement}: {support.support_level}; {support.quoted_text}\n",
                )
        if item.conflicts:
            self.evidence_detail.insert(tk.END, "conflicts:\n", "evidence_dim")
            for conflict in item.conflicts:
                self.evidence_detail.insert(tk.END, f"- {conflict}\n")
        if item.reviewer_notes:
            self.evidence_detail.insert(tk.END, f"\nreviewer_notes: {item.reviewer_notes}\n")
        self.evidence_detail.configure(state=tk.DISABLED)
        self.evidence_detail.see("1.0")

    def _replace_tree(self, tree: ttk.Treeview, rows: list[tuple[str, ...]]) -> None:
        tree.delete(*tree.get_children())
        for row in rows:
            tree.insert("", tk.END, text=row[0], values=row[1:])

    def _reload_latest_trace(self) -> None:
        loaded = self._load_latest_trace(self.active_case_id, force=True)
        self._refresh_trace()
        self._refresh_artifacts()
        if not loaded:
            messagebox.showinfo("No trace", "No run trace file was found for this case.")

    def _load_latest_trace(self, case_id: str, *, force: bool = False) -> bool:
        if not force and case_id in self.case_traces:
            return True
        turns = self._load_case_turns(case_id, force=force)
        if not turns:
            return False
        label = self.selected_run_var.get()
        selected = next((item for item in turns if item.get("label") == label), turns[0])
        self.case_traces[case_id] = selected.get("trace") or {}
        self.selected_run_var.set(str(selected.get("label") or selected.get("run_id") or ""))
        self.trace_turn_context_var.set(self._turn_context_text(selected))
        return True

    def _load_case_turns(self, case_id: str, *, force: bool = False) -> list[dict[str, Any]]:
        if not force and case_id in self.case_trace_turns:
            return self.case_trace_turns[case_id]
        runs = self._load_case_runs(case_id, force=force)
        runs_by_id = {str(item.get("run_id") or ""): item for item in runs}
        turns: list[dict[str, Any]] = []
        try:
            records = self.sessions.get_conversation_items(case_id, limit=120)
        except Exception:
            records = []
        pending_user: dict[str, Any] | None = None
        turn_index = 0
        used_runs: set[str] = set()
        for record in records:
            role = str(record.get("role") or "")
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            if role == "user":
                pending_user = record
                continue
            if role != "assistant":
                continue
            run_id = str(metadata.get("run_id") or "")
            run = runs_by_id.get(run_id)
            if not run:
                continue
            turn_index += 1
            used_runs.add(run_id)
            turns.append(self._turn_entry(case_id, turn_index, pending_user, record, run))
            pending_user = None
        for run in runs:
            run_id = str(run.get("run_id") or "")
            if run_id in used_runs:
                continue
            turn_index += 1
            turns.append(self._turn_entry(case_id, turn_index, None, None, run))
        turns.sort(key=lambda item: str((item.get("trace") or {}).get("started_at") or item.get("assistant_ts") or ""), reverse=True)
        for index, item in enumerate(turns, start=1):
            item["label"] = self._turn_label(index, item)
        self.case_trace_turns[case_id] = turns
        return turns

    def _turn_entry(
        self,
        case_id: str,
        turn_index: int,
        user_record: dict[str, Any] | None,
        assistant_record: dict[str, Any] | None,
        run: dict[str, Any],
    ) -> dict[str, Any]:
        trace = run.get("trace") or {}
        user_metadata = user_record.get("metadata") if isinstance(user_record, dict) and isinstance(user_record.get("metadata"), dict) else {}
        return {
            "case_id": case_id,
            "turn_index": turn_index,
            "turn_id": trace.get("turn_id") or user_metadata.get("turn_id") or "",
            "run_id": run.get("run_id") or trace.get("run_id") or "",
            "trace": trace,
            "run": run,
            "user": str((user_record or {}).get("content") or trace.get("user_message_summary") or ""),
            "assistant": str((assistant_record or {}).get("content") or trace.get("final_answer") or ""),
            "user_ts": str((user_record or {}).get("ts") or ""),
            "assistant_ts": str((assistant_record or {}).get("ts") or ""),
        }

    def _turn_label(self, index: int, item: dict[str, Any]) -> str:
        trace = item.get("trace") or {}
        run_id = str(item.get("run_id") or trace.get("run_id") or "-")
        status = "done" if trace.get("completed_at") else "open"
        steps = trace.get("step_count", 0)
        user = self._single_line(str(item.get("user") or "turn"), max_chars=34)
        return f"{index:02d}. {status} steps:{steps}  {user}  [{run_id}]"

    def _turn_context_text(self, item: dict[str, Any]) -> str:
        trace = item.get("trace") or {}
        run_id = str(item.get("run_id") or trace.get("run_id") or "-")
        turn_id = str(item.get("turn_id") or trace.get("turn_id") or "-")
        user = self._single_line(str(item.get("user") or trace.get("user_message_summary") or "-"), max_chars=92)
        assistant = self._single_line(str(item.get("assistant") or trace.get("final_answer") or "-"), max_chars=92)
        return f"Q: {user}\nA: {assistant}\nturn: {turn_id}   run: {run_id}"

    def _load_case_runs(self, case_id: str, *, force: bool = False) -> list[dict[str, Any]]:
        if not force and case_id in self.case_trace_runs:
            return self.case_trace_runs[case_id]
        try:
            trace_root = self.store.ensure_case_dirs(case_id) / "traces"
            candidates = [
                path
                for path in trace_root.glob("run_*.json")
                if path.is_file() and path.name != "case_audit.jsonl"
            ]
            if not candidates:
                self.case_trace_runs[case_id] = []
                return []
            runs: list[dict[str, Any]] = []
            for path in sorted(candidates, key=lambda item: item.stat().st_mtime, reverse=True):
                trace = self._read_json_file(path)
                run_id = str(trace.get("run_id") or path.stem)
                runs.append(
                    {
                        "run_id": run_id,
                        "label": self._run_label(trace, path),
                        "path": str(path),
                        "trace": trace,
                    }
                )
            self.case_trace_runs[case_id] = runs
            return runs
        except Exception:
            self.case_trace_runs[case_id] = []
            return []

    def _run_label(self, trace: dict[str, Any], path: Path) -> str:
        run_id = str(trace.get("run_id") or path.stem)
        phase = str(trace.get("phase") or "-")
        steps = trace.get("step_count", 0)
        status = "done" if trace.get("completed_at") else "open"
        ts = str(trace.get("completed_at") or trace.get("started_at") or "")
        clock = ts[11:19] if len(ts) >= 19 else ""
        return f"{run_id}  {status}  {phase}  steps:{steps}  {clock}"

    def _run_label_for_id(self, case_id: str, run_id: str) -> str:
        for item in self.case_trace_runs.get(case_id, []):
            if item.get("run_id") == run_id:
                return str(item.get("label") or "")
        return ""

    def _conversation_rows(self, case_id: str) -> list[tuple[str, str, str]]:
        try:
            records = self.sessions.get_conversation_items(case_id, limit=40)
        except Exception:
            return []
        rows: list[tuple[str, str, str]] = []
        for record in records:
            role = str(record.get("role") or "runtime")
            content = str(record.get("content") or "")
            rows.append((role, self._format_record_time(record.get("ts")), content))
        return rows

    def _known_case_ids(self, *, include_active: bool = True) -> list[str]:
        known = set(self.case_messages)
        if include_active:
            known.add(self.active_case_id)
        try:
            for child in self.store.workspace_root.iterdir():
                if child.is_dir() and (child / "case_state.json").exists():
                    try:
                        known.add(self.store.validate_case_id(child.name))
                    except Exception:
                        continue
        except Exception:
            pass
        return sorted(known)

    def _format_trace(self, trace: dict[str, Any]) -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = []
        rows.append(("trace_heading", f"Run {trace.get('run_id', 'unknown')}\n"))
        if trace.get("started_at") or trace.get("completed_at"):
            rows.append(("trace_dim", f"started: {trace.get('started_at', '-')}\ncompleted: {trace.get('completed_at', '-')}\n\n"))

        planner_actions = list(trace.get("planner_actions") or [])
        if planner_actions:
            rows.append(("trace_heading", "Planner timeline\n"))
            for index, action in enumerate(planner_actions, start=1):
                target = action.get("role") or action.get("tool") or ""
                reason = action.get("reason") or ""
                progress = action.get("plan_progress") or ""
                rows.append(("normal", f"[{index}] {action.get('action', 'unknown')} {target}\n"))
                if action.get("short_plan"):
                    for item in action.get("short_plan") or []:
                        rows.append(("trace_dim", f"    plan: {item}\n"))
                if progress:
                    rows.append(("trace_dim", f"    progress: {progress}\n"))
                if reason:
                    rows.append(("trace_dim", f"    reason: {reason}\n"))
                if action.get("input"):
                    rows.append(("trace_dim", f"    input: {self._preview(action.get('input'))}\n"))
                if action.get("case_patch"):
                    rows.append(("trace_dim", f"    case_patch: {self._preview(action.get('case_patch'))}\n"))
                rows.append(("normal", "\n"))

        role_calls = list(trace.get("role_calls") or [])
        if role_calls:
            rows.append(("trace_heading", "Role calls\n"))
            for call in role_calls:
                tag = "trace_error" if call.get("error") else "normal"
                rows.append((tag, f"- {call.get('role', 'role')}  {call.get('ts', '')}\n"))
                if call.get("error"):
                    rows.append(("trace_error", f"  error: {call.get('error')}\n"))
                rows.append(("trace_dim", f"  input: {self._preview(call.get('input'))}\n"))
                rows.append(("trace_dim", f"  result: {call.get('result_preview', '')}\n\n"))

        tool_calls = list(trace.get("tool_calls") or [])
        if tool_calls:
            rows.append(("trace_heading", "Tool calls\n"))
            for call in tool_calls:
                tag = "trace_error" if call.get("error") else "normal"
                rows.append((tag, f"- {call.get('tool', 'tool')}  {call.get('ts', '')}\n"))
                if call.get("error"):
                    rows.append(("trace_error", f"  error: {call.get('error')}\n"))
                rows.append(("trace_dim", f"  input: {self._preview(call.get('input'))}\n"))
                rows.append(("trace_dim", f"  result: {call.get('result_preview', '')}\n\n"))

        model_calls = list(trace.get("model_calls") or [])
        if model_calls:
            rows.append(("trace_heading", "Model calls\n"))
            for call in model_calls:
                tag = "trace_error" if call.get("error") else "normal"
                rows.append((tag, f"- {call.get('role', 'model')} / {call.get('model', '')} / {call.get('prompt_version', '')}\n"))
                if call.get("error"):
                    rows.append(("trace_error", f"  error: {call.get('error')}\n"))
                rows.append(("trace_dim", f"  input_preview: {call.get('input_preview', '')}\n"))
                rows.append(("trace_dim", f"  output_preview: {call.get('output_preview', '')}\n\n"))

        checkpoints = list(trace.get("trace_checkpoints") or trace.get("checkpoints") or [])
        if checkpoints:
            rows.append(("trace_heading", "Checkpoints\n"))
            for item in checkpoints:
                action = item.get("planner_action") or item.get("action") or "-"
                rows.append(("trace_dim", f"- {item.get('checkpoint_id', '-')} step {item.get('step_count', '-')} action {action}\n"))

        observations = list(trace.get("observations") or [])
        if observations:
            rows.append(("\ntrace_heading", "\nObservations\n"))
            rows.append(("trace_dim", self._preview(observations, max_chars=1800) + "\n"))

        return rows

    def _load_prompt_entries(self) -> list[dict[str, str]]:
        planner_prompt = load_system_prompt("agents/planner/prompt.md")
        return [
            {"name": "planner", "label": "planner", "content": planner_prompt},
            {"name": "materials_advisor", "label": "materials", "content": MATERIALS_ADVISOR_PROMPT},
            {"name": "evidence_reviewer", "label": "evidence", "content": EVIDENCE_REVIEWER_PROMPT},
            {"name": "case_patch_writer", "label": "patcher", "content": CASE_PATCH_WRITER_PROMPT},
            {"name": "report_writer", "label": "report", "content": REPORT_WRITER_PROMPT},
        ]

    def _prompt_selected(self, _event: tk.Event[Any]) -> None:
        selection = self.prompt_list.curselection()
        if not selection:
            return
        index = selection[0]
        if index >= len(self.prompt_entries):
            return
        self._show_prompt(self.prompt_entries[index]["name"])

    def _show_prompt(self, name: str) -> None:
        entry = self.prompt_entry_by_name.get(name)
        if not entry:
            return
        self.selected_prompt_name = name
        self.prompt_text.configure(state=tk.NORMAL)
        self.prompt_text.delete("1.0", tk.END)
        self.prompt_text.insert(tk.END, f"{entry['name']} system prompt\n\n", "prompt_title")
        self.prompt_text.insert(tk.END, entry["content"])
        self.prompt_text.configure(state=tk.DISABLED)
        self.prompt_text.see("1.0")

    def _insert_message_block(self, role: str, timestamp: str, content: str) -> None:
        role_key = role if role in ROLE_META else "runtime"
        meta = ROLE_META[role_key]
        outer = tk.Frame(self.message_body, bg=COLORS["input"])
        outer.pack(fill=tk.X, padx=14, pady=(8, 2))

        row = tk.Frame(outer, bg=COLORS["input"])
        row.pack(fill=tk.X)
        if role_key == "user":
            tk.Frame(row, bg=COLORS["input"]).pack(side=tk.LEFT, fill=tk.X, expand=True)
            card = tk.Frame(row, bg=meta["bg"], bd=0, highlightthickness=1, highlightbackground=meta["border"])
            card.pack(side=tk.RIGHT, anchor=tk.E, padx=(92, 0))
            content_parent = card
        else:
            card = tk.Frame(row, bg=meta["bg"], bd=0, highlightthickness=1, highlightbackground=meta["border"])
            card.pack(side=tk.LEFT, fill=tk.X, expand=True, anchor=tk.W, padx=(0, 18))
            rail = tk.Frame(card, bg=meta["tone"], width=3)
            rail.pack(side=tk.LEFT, fill=tk.Y)
            content_parent = tk.Frame(card, bg=meta["bg"])
            content_parent.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        header = tk.Frame(content_parent, bg=meta["bg"])
        header.pack(fill=tk.X, padx=12, pady=(8, 3))
        tk.Label(
            header,
            text=meta["label"],
            bg=meta["bg"],
            fg=meta["tone"],
            font=("Segoe UI", 9, "bold"),
        ).pack(side=tk.LEFT)
        tk.Label(
            header,
            text=timestamp,
            bg=meta["bg"],
            fg=COLORS["subtle"],
            font=("Cascadia Mono", 8),
        ).pack(side=tk.RIGHT)
        self._flat_button(header, "Copy", lambda text=content: self._copy_text(text), width=6).pack(side=tk.RIGHT, padx=(0, 8))

        body, attachment_names = self._split_attachment_preview(content)
        if role_key == "user":
            body_label = tk.Label(
                content_parent,
                text=body.strip() or "(empty)",
                bg=meta["bg"],
                fg=COLORS["text"],
                justify=tk.LEFT,
                anchor=tk.W,
                wraplength=max(240, self.message_wrap - 120),
                font=("Microsoft YaHei UI", 10),
            )
            body_label.pack(fill=tk.BOTH, padx=12, pady=(0, 10))
            setattr(body_label, "_wrap_offset", 120)
            self.message_content_labels.append(body_label)
        else:
            self._add_message_text(content_parent, body, meta["bg"], role_key)

        if attachment_names:
            chip_row = tk.Frame(content_parent, bg=meta["bg"])
            chip_row.pack(fill=tk.X, padx=12, pady=(0, 11))
            for name in attachment_names[:6]:
                chip = tk.Label(
                    chip_row,
                    text=name,
                    bg=COLORS["chip"],
                    fg=COLORS["accent_2"],
                    padx=8,
                    pady=3,
                    font=("Segoe UI", 8, "bold"),
                )
                chip.pack(side=tk.LEFT, padx=(0, 6), pady=(0, 2))
            if len(attachment_names) > 6:
                tk.Label(
                    chip_row,
                    text=f"+{len(attachment_names) - 6}",
                    bg=COLORS["chip"],
                    fg=COLORS["muted"],
                    padx=8,
                    pady=3,
                    font=("Segoe UI", 8, "bold"),
                ).pack(side=tk.LEFT, pady=(0, 2))

    def _add_message_text(self, master: tk.Misc, text: str, bg: str, role_key: str) -> None:
        cleaned_lines = self._message_display_lines(text)
        body = tk.Frame(master, bg=bg)
        body.pack(fill=tk.X, padx=12, pady=(0, 10))
        for line, tag in cleaned_lines:
            if line == "":
                tk.Frame(body, bg=bg, height=6).pack(fill=tk.X)
                continue
            style = self._message_line_style(tag, role_key)
            label = tk.Label(
                body,
                text=line,
                bg=bg,
                fg=style["fg"],
                justify=tk.LEFT,
                anchor=tk.W,
                wraplength=max(260, self.message_wrap - 28),
                font=style["font"],
            )
            label.pack(fill=tk.X, pady=style["pady"])
            setattr(label, "_wrap_offset", 28)
            self.message_content_labels.append(label)

    def _message_line_style(self, tag: str, role_key: str) -> dict[str, Any]:
        if role_key == "runtime" and tag == "body":
            return {"fg": COLORS["runtime"], "font": ("Microsoft YaHei UI", 10), "pady": (1, 1)}
        if tag == "heading":
            return {"fg": COLORS["text"], "font": ("Microsoft YaHei UI", 10, "bold"), "pady": (7, 2)}
        if tag == "table":
            return {"fg": COLORS["cyan"], "font": ("Cascadia Mono", 8), "pady": (1, 1)}
        if tag == "danger":
            return {"fg": COLORS["danger"], "font": ("Microsoft YaHei UI", 10), "pady": (1, 1)}
        if tag == "muted":
            return {"fg": COLORS["muted"], "font": ("Microsoft YaHei UI", 10), "pady": (1, 1)}
        return {"fg": COLORS["text"], "font": ("Microsoft YaHei UI", 10), "pady": (1, 1)}

    def _message_display_lines(self, text: str) -> list[tuple[str, str]]:
        raw_lines = (text.strip() or "(empty)").splitlines()
        rows: list[tuple[str, str]] = []
        for raw_line in raw_lines:
            stripped = raw_line.strip()
            if not stripped:
                rows.append(("", "body"))
                continue
            tag = "body"
            line = stripped
            if line.startswith("### "):
                line = line[4:].strip()
                tag = "heading"
            elif line.startswith("## "):
                line = line[3:].strip()
                tag = "heading"
            elif line.startswith("# "):
                line = line[2:].strip()
                tag = "heading"
            elif line.startswith("|"):
                tag = "table"
            elif line.startswith("- "):
                tag = "muted"
            elif line[:2].isdigit() and ". " in line[:5]:
                tag = "muted"
            if "error" in line.lower() or "failed" in line.lower():
                tag = "danger"
            rows.append((self._strip_inline_markdown(line), tag))
        return rows

    def _strip_inline_markdown(self, text: str) -> str:
        cleaned = text.replace("**", "").replace("__", "").replace("`", "")
        return cleaned.replace("---", "").strip() if cleaned.strip("-") else cleaned

    def _message_canvas_configured(self, event: tk.Event[Any]) -> None:
        self.message_wrap = max(320, event.width - 140)
        for label in self.message_content_labels:
            offset = int(getattr(label, "_wrap_offset", 28))
            label.configure(wraplength=max(240, self.message_wrap - offset))
        self.root.after_idle(self._sync_message_canvas)

    def _chat_header_configured(self, event: tk.Event[Any]) -> None:
        wrap = max(240, event.width - 130)
        self.title_label.configure(wraplength=wrap)
        self.trace_subtitle_label.configure(wraplength=wrap)

    def _schedule_message_scroll_to_bottom(self) -> None:
        self.root.after_idle(self._scroll_messages_to_bottom)
        self.root.after(120, self._scroll_messages_to_bottom)

    def _sync_message_canvas(self) -> None:
        self.message_scroller.body.update_idletasks()
        self.message_scroller.canvas.update_idletasks()
        self.message_scroller.canvas.configure(scrollregion=self.message_scroller.canvas.bbox("all"))

    def _scroll_messages_to_bottom(self) -> None:
        self._sync_message_canvas()
        self.message_scroller.canvas.yview_moveto(1.0)

    def _split_attachment_preview(self, content: str) -> tuple[str, list[str]]:
        marker = "\n\nAttachments: "
        if marker not in content:
            return content, []
        body, raw_names = content.rsplit(marker, 1)
        names = [name.strip() for name in raw_names.split(",") if name.strip()]
        return body, names

    def _copy_text(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.run_state_var.set("Copied")

    def _add_file_chip(self, index: int, path: Path) -> None:
        chip = RoundedFrame(
            self.file_chip_frame,
            bg=COLORS["panel_2"],
            fill=COLORS["chip"],
            outline=COLORS["border"],
            radius=9,
            min_height=30,
        )
        chip.pack(side=tk.LEFT, padx=(0, 7), pady=(0, 2))
        body = chip.inner
        tk.Label(
            body,
            text=path.name,
            bg=COLORS["chip"],
            fg=COLORS["text"],
            font=("Segoe UI", 9, "bold"),
            padx=8,
            pady=4,
        ).pack(side=tk.LEFT)
        remove = tk.Label(
            body,
            text="x",
            bg=COLORS["chip"],
            fg=COLORS["muted"],
            font=("Segoe UI", 9, "bold"),
            padx=7,
            pady=4,
            cursor="hand2",
        )
        remove.pack(side=tk.LEFT)
        remove.bind("<Button-1>", lambda _event, item_index=index: self._remove_file(item_index))
        remove.bind("<Enter>", lambda _event: remove.configure(fg=COLORS["danger"]))
        remove.bind("<Leave>", lambda _event: remove.configure(fg=COLORS["muted"]))

    def _panel(self, master: tk.Misc, *, width: int | None = None) -> tk.Frame:
        frame = tk.Frame(master, bg=COLORS["panel"], bd=0, highlightthickness=1, highlightbackground=COLORS["border"])
        if width is not None:
            frame.configure(width=width)
            frame.pack_propagate(False)
        return frame

    def _readonly_text(self, master: tk.Misc, *, height: int, monospace: bool = False) -> scrolledtext.ScrolledText:
        font = ("Cascadia Mono", 9) if monospace else ("Segoe UI", 9)
        widget = scrolledtext.ScrolledText(
            master,
            wrap=tk.WORD,
            height=height,
            state=tk.DISABLED,
            bg=COLORS["input"],
            fg=COLORS["text"],
            insertbackground=COLORS["text"],
            relief=tk.FLAT,
            bd=0,
            padx=10,
            pady=10,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            font=font,
        )
        try:
            widget.vbar.configure(  # type: ignore[attr-defined]
                bg=COLORS["panel_3"],
                activebackground=COLORS["hover"],
                troughcolor=COLORS["input"],
                relief=tk.FLAT,
                bd=0,
                highlightthickness=0,
            )
        except tk.TclError:
            pass
        return widget

    def _flat_button(self, master: tk.Misc, text: str, command: Any, *, width: int | None = None) -> tk.Widget:
        return self._action_button(master, text, command, variant="ghost", width=width)

    def _accent_button(self, master: tk.Misc, text: str, command: Any) -> tk.Widget:
        return self._action_button(master, text, command, variant="primary")

    def _action_button(
        self,
        master: tk.Misc,
        text: str,
        command: Any,
        *,
        variant: str,
        width: int | None = None,
    ) -> tk.Widget:
        colors = self._button_colors(variant, enabled=True, hover=False)
        button = RoundedButton(
            master,
            text=text,
            bg=colors["bg"],
            fg=colors["fg"],
            parent_bg=widget_bg(master),
            width_chars=width,
            height=36 if variant == "primary" else 31,
            radius=10 if variant == "primary" else 8,
            outline=colors["outline"],
            font=("Segoe UI", 10 if variant == "primary" else 9, "bold"),
        )
        setattr(button, "_button_command", command)
        setattr(button, "_button_variant", variant)
        setattr(button, "_button_enabled", True)
        button.bind("<Button-1>", lambda _event, widget=button: self._invoke_action_button(widget))
        button.bind("<Enter>", lambda _event, widget=button: self._hover_action_button(widget, True))
        button.bind("<Leave>", lambda _event, widget=button: self._hover_action_button(widget, False))
        return button

    def _button_colors(self, variant: str, *, enabled: bool, hover: bool) -> dict[str, str]:
        if not enabled:
            return {"bg": COLORS["panel_2"], "fg": COLORS["subtle"], "outline": COLORS["panel_2"]}
        if variant == "primary":
            return {"bg": "#3fe0cf" if hover else COLORS["accent"], "fg": "#061018", "outline": "#5eeadd" if hover else COLORS["accent"]}
        return {
            "bg": COLORS["hover"] if hover else COLORS["panel_3"],
            "fg": COLORS["text"],
            "outline": COLORS["border_strong"] if hover else COLORS["border"],
        }

    def _apply_button_colors(self, widget: tk.Widget, colors: dict[str, str]) -> None:
        if hasattr(widget, "set_colors"):
            widget.set_colors(colors["bg"], colors["fg"], outline=colors.get("outline"))  # type: ignore[attr-defined]
        else:
            widget.configure(bg=colors["bg"], fg=colors["fg"])

    def _invoke_action_button(self, widget: tk.Widget) -> None:
        if not getattr(widget, "_button_enabled", True):
            return
        command = getattr(widget, "_button_command", None)
        if callable(command):
            command()

    def _hover_action_button(self, widget: tk.Widget, hover: bool) -> None:
        variant = str(getattr(widget, "_button_variant", "ghost"))
        enabled = bool(getattr(widget, "_button_enabled", True))
        colors = self._button_colors(variant, enabled=enabled, hover=hover)
        self._apply_button_colors(widget, colors)

    def _set_action_button_enabled(self, widget: tk.Widget, enabled: bool) -> None:
        if not hasattr(widget, "_button_variant"):
            try:
                widget.configure(state=tk.NORMAL if enabled else tk.DISABLED)
            except tk.TclError:
                pass
            return
        setattr(widget, "_button_enabled", enabled)
        variant = str(getattr(widget, "_button_variant", "ghost"))
        colors = self._button_colors(variant, enabled=enabled, hover=False)
        self._apply_button_colors(widget, colors)
        widget.configure(cursor="hand2" if enabled else "arrow")

    def _set_text(self, widget: scrolledtext.ScrolledText, text: str) -> None:
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text)
        widget.configure(state=tk.DISABLED)

    def _set_trace_text(self, rows: list[tuple[str, str]]) -> None:
        self.trace_text.configure(state=tk.NORMAL)
        self.trace_text.delete("1.0", tk.END)
        for tag, text in rows:
            clean_tag = tag.strip()
            if clean_tag == "normal":
                self.trace_text.insert(tk.END, text)
            else:
                self.trace_text.insert(tk.END, text, clean_tag)
        self.trace_text.configure(state=tk.DISABLED)
        self.trace_text.see("1.0")

    def _preview(self, value: Any, *, max_chars: int = 900) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        return text if len(text) <= max_chars else text[:max_chars] + "..."

    def _single_line(self, text: str, *, max_chars: int) -> str:
        compact = " ".join(str(text or "").split())
        return compact if len(compact) <= max_chars else compact[: max_chars - 3] + "..."

    def _format_record_time(self, value: Any) -> str:
        raw = str(value or "")
        if not raw:
            return "--"
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%H:%M")
        except Exception:
            return raw[:5] if len(raw) >= 5 else raw

    def _short_status(self, status: str) -> str:
        return {
            "collecting_materials": "collecting",
            "ready_for_report": "ready",
            "report_generated": "report",
        }.get(status, status[:10])

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            return f"Failed to read {path}: {type(exc).__name__}: {exc}"

    def _read_json_file(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"value": data}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}", "path": str(path)}


def _reason_from_model_payload(payload: dict[str, Any]) -> str:
    raw = payload.get("raw_response") or payload.get("output") or payload.get("output_preview")
    if raw in (None, "", [], {}):
        return ""
    if isinstance(raw, dict):
        data = raw
    else:
        text = str(raw).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return " ".join(text.split())[:220]
    if not isinstance(data, dict):
        return ""
    for key in ("reason", "plan_progress", "reply_to_user", "final_answer"):
        value = data.get(key)
        if value not in (None, "", [], {}):
            return " ".join(str(value).split())[:220]
    return ""


def _display_requirement_status(requirement: Any) -> str:
    status = str(getattr(requirement, "status", "") or "")
    required = bool(getattr(requirement, "required", True))
    if not required and status in {"missing", "weak"}:
        return "optional"
    if not required and status == "satisfied":
        return "satisfied*"
    return status


def _evidence_field_label(field: Any) -> str:
    labels = {
        "invoice_number": "发票编号",
        "supplier": "供应商",
        "buyer": "购买方",
        "invoice_date": "发票日期",
        "amount_total": "总金额",
        "currency_tax": "币种/税额",
        "currency": "币种",
        "tax_amount": "税额",
        "tax_details": "税额明细",
        "line_items_product_title": "商品/服务行项目",
        "signature_or_authorized_signatory": "签名/授权签章",
        "visual_signature_mark": "签名/授权签章",
        "source_traceability": "来源可追溯性",
        "template_match": "模板匹配",
    }
    value = str(field or "").strip()
    return labels.get(value, value or "-")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local desktop invoice case workbench")
    parser.add_argument("--check", action="store_true", help="Import-check the desktop module without opening a window")
    args = parser.parse_args()
    if args.check:
        print("desktop module ok")
        return

    root_cls = TkinterDnD.Tk if TkinterDnD is not None else tk.Tk
    root = root_cls()
    DesktopWorkbench(root)
    root.mainloop()


if __name__ == "__main__":
    main()
