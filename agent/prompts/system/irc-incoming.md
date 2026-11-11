<irc>
Incoming IRC message from agent `{{ from }}`{% if reply_to %} (reply to {{ reply_to }}){% endif %}{% if progress %} (progress; its reply follows later){% endif %}:

{{ message }}

{% if interrupting %}Sent while waiting/working. Active interruptible wait stopped early for immediate reading.
{% endif %}
{% if not progress %}{% if relay_on_stop %}If response expected, reply via `agent_message` (`to: "{{ from }}"`, `content: "…"`) when available; otherwise what you `yield` or say last this turn is delivered to `{{ from }}` when you stop.{% else %}If response expected, reply via `agent_message` (`to: "{{ from }}"`, `content: "…"`); may finish current step first. No one replies on your behalf.{% endif %} An acknowledgement or progress note before the real answer goes to `agent_message` with `to: "{{ from }}"` and `progress: true`, so `{{ from }}` keeps waiting for the answer.{% endif %}
</irc>
