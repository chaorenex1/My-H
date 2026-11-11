Your `yield` recorded; {{ count }} background job{% if multiple %}s{% endif %} you own {% if multiple %}are{% else %}is{% endif %} still running: {{ jobs }}.

This run completes only after jobs settle AND you submit a fresh `yield` that accounts for results. Job results arrive as follow-up messages; a result after your `yield` supersedes it — it will NOT be accepted as final report. Decide now:
- Job no longer needed? Call `process_manage` to kill it; re-yield.
- Otherwise stand by; when each result arrives, submit a fresh `yield` that incorporates it (repeat report unchanged if result does not affect it).
