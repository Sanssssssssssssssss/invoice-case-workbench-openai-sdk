from __future__ import annotations


COLORS = {
    "bg": "#07080a",
    "panel": "#0d0e10",
    "panel_2": "#111316",
    "panel_3": "#171a1f",
    "border": "#24272d",
    "border_strong": "#353a43",
    "text": "#f4f4f6",
    "muted": "#c4cad3",
    "subtle": "#858b96",
    "accent": "#2dd4bf",
    "accent_2": "#8b8ff6",
    "warning": "#f5b83d",
    "danger": "#ff6678",
    "ok": "#59d499",
    "user": "#63b3ff",
    "assistant": "#59d499",
    "runtime": "#f5b83d",
    "input": "#090b0f",
    "bubble_user": "#101923",
    "bubble_assistant": "#0f1714",
    "bubble_runtime": "#181407",
    "chip": "#181c22",
    "hover": "#20242c",
    "violet": "#b490ff",
    "blue": "#57a8ff",
    "cyan": "#42c6ff",
    "rose": "#ff6678",
    "slate": "#9aa4b2",
    "teal_surface": "#0b211e",
    "violet_surface": "#18152b",
    "amber_surface": "#211807",
    "rose_surface": "#261014",
    "blue_surface": "#0c1b2b",
    "cyan_surface": "#0a1d28",
}


STATUS_COLORS = {
    "new": COLORS["muted"],
    "collecting_materials": COLORS["warning"],
    "ready_for_report": COLORS["accent_2"],
    "report_generated": COLORS["ok"],
}


ROLE_META = {
    "user": {
        "label": "You",
        "tone": COLORS["user"],
        "bg": COLORS["bubble_user"],
        "border": "#264864",
    },
    "assistant": {
        "label": "Agent",
        "tone": COLORS["assistant"],
        "bg": COLORS["bubble_assistant"],
        "border": "#273141",
    },
    "runtime": {
        "label": "Runtime",
        "tone": COLORS["runtime"],
        "bg": COLORS["bubble_runtime"],
        "border": "#4d3920",
    },
}


TRACE_KIND_META = {
    "Turn": {"color": COLORS["accent"], "bg": COLORS["teal_surface"], "label": "Turn"},
    "Phase": {"color": COLORS["accent"], "bg": COLORS["teal_surface"], "label": "Phase"},
    "Planner": {"color": COLORS["violet"], "bg": COLORS["violet_surface"], "label": "Planner"},
    "Role": {"color": COLORS["ok"], "bg": "#10251a", "label": "Role"},
    "Tool": {"color": COLORS["warning"], "bg": COLORS["amber_surface"], "label": "Tool"},
    "Model": {"color": COLORS["blue"], "bg": COLORS["blue_surface"], "label": "Model"},
    "Observation": {"color": COLORS["cyan"], "bg": COLORS["cyan_surface"], "label": "Observation"},
    "Artifact": {"color": COLORS["accent"], "bg": "#0e2524", "label": "Artifact"},
    "Checkpoint": {"color": "#818cf8", "bg": "#171933", "label": "Checkpoint"},
    "Error": {"color": COLORS["danger"], "bg": COLORS["rose_surface"], "label": "Error"},
}


TRACE_FILTERS = (
    "All Events",
    "Turn",
    "Planner",
    "Roles",
    "Tools",
    "Models",
    "Observations",
    "Artifacts",
    "Checkpoints",
    "Errors",
)

TRACE_DETAIL_MODES = ("Details", "Thought", "Input", "Output", "Artifacts", "Related", "Raw")
