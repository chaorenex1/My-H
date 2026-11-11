<progress-check>
Side question from the orchestrator; your work continues after this reply.
{% if inflight_lines %}You were interrupted mid-response; these tool calls were still being written and are not in the transcript above:
{% for line in inflight_lines %}
{{ line }}{% endfor %}
Count that work as done so far.
{% endif %}Estimate how much of your assigned task is complete. Anchor on the phase you are in:
- 0–15%: reading the task and exploring code
- 15–30%: plan settled, first edits starting
- 30–75%: implementing; scale by deliverables finished vs. remaining
- 75–90%: all deliverables written; running tests, fixing failures
- 90–99%: tests pass; final review and summary left
{% if has_previous %}Your previous estimate, {{ previous_ago }} ago, was {{ previous_percent }}%{% if previous_stage %} at stage "{{ previous_stage }}"{% endif %}. Update it from what happened since; lower it if you uncovered more work.
{% endif %}Reply with ONLY one line of JSON, no code fence, no other text:
{"pct": 40, "stage": "benchmarking the streaming decoder", "watch": "BENCH lines in bg_2 pipe:1, tok_s_stream higher is better, target 70% of roofline"}
- `pct`: whole-number completion, 0–100.
- `stage`: your current step in a few words (≤ 80 chars).
- `watch`: optional (≤ 200 chars). Include it only when some output shows your progress as it runs: which job or output (e.g. `bg_2`, its `pipe:1`, a tool's results), which lines or metric, whether higher or lower is better, and the target if there is one. Omit it otherwise.
NEVER use tools.
</progress-check>
