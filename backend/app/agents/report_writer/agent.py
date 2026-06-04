from __future__ import annotations

from app.prompt_loader import load_prompt, load_system_prompt


SYSTEM_PROMPT = (
    load_system_prompt("agents/report_writer/prompt.md").rstrip()
    + "\n\n---\n\n"
    + load_prompt("agents/report_writer/pdf_skill.md").lstrip()
)
