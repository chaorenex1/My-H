<system-reminder>
{% if forced %}Before substantive work, create a phased todo.

You MUST call `todo_list` first in this turn. This forced call is the one exception to `todo_list` never taking a turn of its own; later updates go beside real tool calls.
You MUST write the full plan in that first call: ordered phases, each with its ordered items (`merge=false`).
You MUST cover the entire request from investigation through implementation and verification — not just the next immediate step.
Task descriptions MUST be concise, specific 5-10 word labels.
`todo_list` phases are `{phase, items}` objects; item content is the label; do not invent extra metadata fields.

After `todo_list` succeeds, continue the request in the same turn.
NEVER call `todo_list` again unless task state has materially changed.
{% else %}Consider calling `todo_list` first to lay out a phased plan with a single `merge=false` call. A good list covers the whole request — investigation through implementation and verification — not just the next step, with specific task descriptions a future turn could execute without re-planning.
A useful list keeps each task to a concise, specific 5-10 word label; `todo_list` expects `phases` entries of `{phase, items}`, so don't invent extra metadata fields.
If you create the list, continue the request in the same turn and avoid re-calling `todo_list` unless task state materially changes.
{% endif %}
</system-reminder>
