§ Memories

<memories>
<memories-guidance>{{ memories_guidance }}</memories-guidance>
<memories-global>
{% if user_profile_enabled %}<memories-user>{{ memories_user }}</memories-user>
{% endif %}<memories-self>{{ memories_self }}</memories-self>
<memories-work>{{ memories_work }}</memories-work>
</memories-global>
{% if memories_provider %}<memories_plugin_provider>{{ memories_provider }}</memories_plugin_provider>
{% endif %}</memories>
