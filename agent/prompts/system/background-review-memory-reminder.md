<system-reminder id="background-review-memory">
Background review of this session created or updated memories after an earlier turn. These writes already landed: do not repeat them, and treat the current memory files as authoritative.
{% for row in actions %}- {{ row }}
{% endfor %}</system-reminder>
