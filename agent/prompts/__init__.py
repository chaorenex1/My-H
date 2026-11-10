"""Prompt assets and the system-prompt section registry (ADR-18).

``system/`` holds the Markdown/Jinja2 leaf templates and ``manifest.json``;
``prompt_sections.py`` loads and renders them; ``renderer.py`` is the typed entry
surface for the stable head sections 00–02.
"""
