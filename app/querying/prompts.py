"""Load query planning instructions; the planner appends its generated vocabulary."""

from pathlib import Path

SYSTEM = (Path(__file__).parent / "prompts" / "planning.md").read_text(encoding="utf-8")
