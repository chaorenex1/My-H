"""Verbatim front matter from omp's bundled ``scout``/``reviewer`` definitions.

Kept as fixtures, not shipped presets: omp declares ``tools:`` (unknown to Hermes) and no
``toolsets``/``enable_tools``, so installing them as presets would widen the shipped
tool surface. These blocks pin the parity acceptance: ``@smol``/``@slow`` selectors, the JTD-shaped
``output`` normalization, and ``spawns: scout``.
"""

from __future__ import annotations

SCOUT_FRONTMATTER = """---
name: scout
description: MUST be used for exploratory codebase research, rapid code analysis, and broad pattern searches. Fast read-only scout returning compressed context for handoff.
tools: read, find, grep, glob, ast_grep, web_search
model: "@smol"
thinking-level: medium
read-summarize: false
output:
  properties:
    summary:
      metadata:
        description: Brief summary of findings and conclusions
      type: string
    files:
      metadata:
        description: Files examined with relevant code references
      elements:
        properties:
          path:
            metadata:
              description: Project-relative path or paths to the most relevant code reference(s), optionally suffixed with line ranges like `:12-34` when relevant
            type: string
          description:
            metadata:
              description: Section contents
            type: string
    architecture:
      metadata:
        description: Brief explanation of how pieces connect
      type: string
  optionalProperties:
    report:
      metadata:
        description: The complete deliverable when the task asks for a report, table, enumeration, or per-item audit — full markdown at the depth requested (tables, path:line anchors, signatures, code excerpts). Never a summary of it; `summary` already covers that. Omit only for quick lookups.
      type: string
---
"""

REVIEWER_FRONTMATTER = """---
name: reviewer
description: "Code review specialist for quality/security analysis"
tools: read, find, grep, glob, bash, lsp, web_search, ast_grep
spawns: scout
model: "@slow"
output:
  properties:
    overall_correctness:
      metadata:
        description: Whether change correct (no bugs/blockers)
      enum: [correct, incorrect]
    explanation:
      metadata:
        description: Plain-text verdict summary, 1-3 sentences
      type: string
    confidence:
      metadata:
        description: Verdict confidence (0.0-1.0)
      type: number
  optionalProperties:
    findings:
      metadata:
        description: "Populate via incremental yield sections under type: [\\"findings\\"]; don't repeat it in a final payload."
      elements:
        properties:
          title:
            metadata:
              description: Imperative, ≤80 chars
            type: string
          body:
            metadata:
              description: "One paragraph: bug, trigger, impact"
            type: string
          priority:
            metadata:
              description: "P0-P3: 0 blocks release, 1 fix next cycle, 2 fix eventually, 3 nice to have"
            type: number
          confidence:
            metadata:
              description: Confidence it's real bug (0.0-1.0)
            type: number
          file_path:
            metadata:
              description: Path to affected file
            type: string
          line_start:
            metadata:
              description: First line (1-indexed)
            type: number
          line_end:
            metadata:
              description: Last line (1-indexed, ≤10 lines)
            type: number
---
"""

__all__ = ["REVIEWER_FRONTMATTER", "SCOUT_FRONTMATTER"]
