# Rules

Rules are constraints enforced while you work, not documents to load. The runtime matches
them against the files and arguments of each tool call; a matching rule arrives next to your
tool result. Treat that reminder as authoritative for the current change: fix the code, rerun
the check, or state why the rule does not apply.

Rules under `<generic-rules>` always apply in full. A `<domain-rules>` line names the rule,
its globs and its description; when a path matches, read the full rule{% if rule_view %} with
`rule_view`{% endif %}.
Path-only rules apply when you edit a file their globs match.

{% if generic_rules %}<generic-rules>
{{ generic_rules | join("\n\n") }}
</generic-rules>

{% endif %}{% if domain_rules %}<domain-rules>
{% for row in domain_rules %}- {{ row }}
{% endfor %}</domain-rules>
{% endif %}