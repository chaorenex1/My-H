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
    extract_skill_conditions,
    extract_skill_description,
    get_disabled_skill_names,
    iter_skill_index_files,
    parse_frontmatter,
    skill_matches_apps,
    skill_matches_environment,
    skill_matches_platform,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def bundled_skills_dir() -> Path:
    """The built-in skills tree (packaged override first, else the checkout's ``skills/``)."""
    return get_bundled_skills_dir(_REPO_ROOT / "skills")


def bundled_skill_rows() -> list[str]:
    """Every bundled, platform-compatible skill as ``"name: description"``, sorted."""
    return skill_rows(bundled_skills_dir())


def skill_rows(
    skills_dir: Path | None = None,
    *,
    available_tools: set[str] | None = None,
    available_toolsets: set[str] | None = None,
    platform: str | None = None,
) -> list[str]:
    """Every offerable skill under *skills_dir* as ``"name: description"``, sorted.

    The session passes its own ``<home>/skills`` so user- and agent-created skills stay
    discoverable; omitted, the bundled tree is scanned. The same visibility gates the full
    index applies run here: disabled names, platform/environment/app requirements and the
    ``requires_*`` / ``fallback_for_*`` conditions. With no tool info the conditional gates
    fail open (show), matching ``_skill_should_show``.
    """
    from agent.prompt_builder import _skill_should_show

    root = Path(skills_dir) if skills_dir is not None else bundled_skills_dir()
    resolved_platform = (platform or "").strip().lower() or None
    disabled = get_disabled_skill_names(resolved_platform)
    rows: list[str] = []
    for skill_file in iter_skill_index_files(root, "SKILL.md"):
        try:
            content = skill_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        frontmatter, _body = parse_frontmatter(content)
        name = str(frontmatter.get("name") or skill_file.parent.name).strip()
        if not name or name in disabled:
            continue
        if not (skill_matches_platform(frontmatter) and skill_matches_environment(frontmatter)
                and skill_matches_apps(frontmatter)):
            continue
        if not _skill_should_show(extract_skill_conditions(frontmatter), available_tools,
                                  available_toolsets, resolved_platform):
            continue
        description = extract_skill_description(frontmatter)
        rows.append(f"{name}: {description}" if description else name)
    return sorted(rows)


__all__ = ["bundled_skill_rows", "bundled_skills_dir", "skill_rows"]
