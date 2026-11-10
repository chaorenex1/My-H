"""Bundled skill discovery for section 06's ``<available_skills>`` list.

The bundled tree is ``get_bundled_skills_dir()``: ``HERMES_BUNDLED_SKILLS`` for packaged
installs, otherwise the checkout's ``skills/``. It is the same source ``tools.skills_sync``
seeds into ``$HERMES_HOME/skills`` and the same SKILL.md frontmatter the full index reads.
Rows are ``"name: description"``, platform-filtered and sorted — the shape
``agent/prompts/system/06-skills.md`` renders line by line.
"""

from __future__ import annotations

from pathlib import Path

from hermes_constants import get_bundled_skills_dir

from agent.skill_utils import (
    extract_skill_description,
    iter_skill_index_files,
    parse_frontmatter,
    skill_matches_platform,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def bundled_skills_dir() -> Path:
    """The built-in skills tree (packaged override first, else the checkout's ``skills/``)."""
    return get_bundled_skills_dir(_REPO_ROOT / "skills")


def bundled_skill_rows() -> list[str]:
    """Every bundled, platform-compatible skill as ``"name: description"``, sorted."""
    rows: list[str] = []
    for skill_file in iter_skill_index_files(bundled_skills_dir(), "SKILL.md"):
        try:
            content = skill_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        frontmatter, _body = parse_frontmatter(content)
        if not skill_matches_platform(frontmatter):
            continue
        name = str(frontmatter.get("name") or skill_file.parent.name).strip()
        description = extract_skill_description(frontmatter)
        rows.append(f"{name}: {description}" if description else name)
    return sorted(rows)


__all__ = ["bundled_skill_rows", "bundled_skills_dir"]
