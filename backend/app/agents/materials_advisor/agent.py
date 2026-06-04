from __future__ import annotations

from app.prompt_loader import load_prompt, load_system_prompt


SYSTEM_PROMPT = "\n\n---\n\n".join(
    [
        load_system_prompt("agents/materials_advisor/prompt.md").rstrip(),
        load_prompt("agents/materials_advisor/task_skill.md").lstrip(),
    ]
)
