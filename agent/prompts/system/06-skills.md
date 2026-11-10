# Skills
{% if skill_view|default(true) %}
Skills are reusable procedures stored on disk. A task that a skill covers MUST start with `skill_view(name)`; follow the loaded instructions instead of improvising.
{% endif %}{% if skill_manage|default(true) %}
After a non-trivial workflow, record it with `skill_manage` for reuse.
{% endif %}{% if skill_view|default(true) and skills|default([], true) %}
<available_skills>
{% for skill in skills %}- {{ skill }}
{% endfor %}</available_skills>
{% endif %}{% if skills_list|default(true) %}
Skills demoted to a name-only line still exist. Use `skills_list(query="<what you need>")` to find them by intent{% if skill_view|default(true) %}, then load the match with `skill_view`{% endif %}.
{% endif -%}
