# Tool Inventory

Available tool groups for this agent, two levels: the group, then each tool. When `tool_search` is active, tools outside your schema are found there, not by guessing names:

{% for group in tool_groups|default([], true) %}{{ group }}
{% endfor -%}
