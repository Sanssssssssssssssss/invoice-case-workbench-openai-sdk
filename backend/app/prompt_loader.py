from __future__ import annotations

from functools import lru_cache
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
GLOBAL_POLICY_PATH = "agents/global_policy.md"


@lru_cache(maxsize=32)
def load_prompt(relative_path: str) -> str:
    path = APP_DIR / relative_path
    return path.read_text(encoding="utf-8")


@lru_cache(maxsize=32)
def load_system_prompt(relative_path: str) -> str:
    prompt = load_prompt(relative_path)
    if relative_path == GLOBAL_POLICY_PATH:
        return prompt
    global_policy = load_prompt(GLOBAL_POLICY_PATH)
    return f"{global_policy.rstrip()}\n\n---\n\n{prompt.lstrip()}"
