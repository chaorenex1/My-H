# Internal URLs

`$HERMES_HOME` is this profile's home. Use these locations; do not guess a path or hardcode `~/.hermes` (a profile has its own home):

{% for url in internal_urls|default([], true) %}- {{ url }}
{% endfor %}{% if hermes_help_guidance|default('', true) %}
{{ hermes_help_guidance }}
{% endif %}{% if kanban_guidance|default('', true) %}
{{ kanban_guidance }}
{% endif -%}
