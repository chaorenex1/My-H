§ Memories

<memories>
<memories-guidance>{{ memories_guidance }}</memories-guidance>
<memories-global>
<memories-user>{{ memories_user }}</memories-user>
<memories-self>{{ memories_self }}</memories-self>
</memories-global>
</memories>
{% if memories_provider %}
{{ memories_provider }}
{% endif -%}
