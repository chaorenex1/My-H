# Rules

Rules are constraints enforced while you work, not documents to load. The runtime matches
them against the files and arguments of each tool call; a matching rule arrives next to your
tool result. Treat that reminder as authoritative for the current change: fix the code, rerun
the check, or state why the rule does not apply. Path-only rules apply when you edit a file
their globs match. Each line names the rule, what it enforces, and where its source lives:

{% for rule in rules|default([], true) %}- {{ rule }}
{% endfor -%}
