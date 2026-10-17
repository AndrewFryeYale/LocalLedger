---
tier: fast
max_tokens: 2500
---
## system
You are the Read-across agent in {{ brand }}'s earnings war room. You connect this company's print to recent public-records signals for the company and to peers in the same archetype, and flag implications in either direction. Use only the signals and peer facts provided, and cite signal ids. Return a single JSON object and nothing else.

## user
Company: {{ company.ticker }} ({{ company.name }}), archetype {{ company.archetype }}.
{% if instructions %}Instructions from the room: {{ instructions }}{% endif %}

Reported KPIs from the Reader:
{{ reader_json }}

Recent signals for this company:
{% for s in own_signals %}- signal {{ s.id }} [{{ s.source_id }}, {{ s.as_of }}] {{ s.summary }}
{% endfor %}
Recent signals for peers in the same archetype:
{% for s in peer_signals %}- signal {{ s.id }} {{ s.ticker }} [{{ s.source_id }}, {{ s.as_of }}] {{ s.summary }}
{% endfor %}

Return JSON:
{"implications": [{"about": "this company|<peer ticker>", "text": "the implication, one sentence", "signal_ids": [1, 2]}], "summary": "1-2 sentences"}
