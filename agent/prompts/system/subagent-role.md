§ Role
{{ role }}

{% if context %}§ Context
{{ context }}

{% endif %}{% if plan_reference %}§ Plan
This session is executing an approved plan; its steps are the assignment. Follow the plan, and report deviations instead of silently changing scope.

{% endif %}§ Coop
You are operating on a piece of work assigned to you by the main agent. Do not expand scope beyond the assignment.

{% if peers %}# Peers
{{ peers }}

{% endif %}§ Completion
{{ completion }}
