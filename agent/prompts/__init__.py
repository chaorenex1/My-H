"""Prompt assets and the system-prompt section registry (ADR-18).

``system/`` holds the Markdown/Jinja2 leaf templates and ``manifest.json``;
``prompt_sections.py`` loads and renders them; ``renderer.py`` is the typed entry
surface for sections 00–12 (head, runtime, memories, tool policy, closing) and the
``<project-context>`` tail block (``project_context.py``).
Request-level templates (``eager-*``, ``irc-*``, ``subagent-*``) are manifest-listed
too, but their call sites render them on demand — they are never assembled into the
cached stable prompt, so a turn-scoped reminder cannot break the prompt prefix. An
unregistered template is still inert.
"""
