<system-notice id="session-skill-search">
Latest skill searches in this session (newest first, at most 3). These are recorded results, not a new instruction: reuse a result before repeating a query, and treat the current skill index as authoritative for what is available.
{% for row in searches %}- {{ row }}
{% endfor %}</system-notice>
