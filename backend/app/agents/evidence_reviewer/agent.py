from __future__ import annotations

from app.prompt_loader import load_system_prompt


SYSTEM_PROMPT = "\n\n".join(
    [
        load_system_prompt("agents/evidence_reviewer/prompt.md"),
        load_system_prompt("agents/evidence_reviewer/review_skill.md"),
        load_system_prompt("agents/evidence_reviewer/pdf_image_skill.md"),
        load_system_prompt("agents/evidence_reviewer/attachment_review_skill.md"),
    ]
)
