"""Loads versioned prompt files from prompts/. Prompts are the safety logic —
they live in files, never inline in Python, so they can be diffed and versioned."""

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    return path.read_text().strip()
