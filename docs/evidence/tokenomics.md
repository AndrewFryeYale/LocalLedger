# Tokenomics

Model gpt-6-luna; prices per million tokens: input $0.1, cached input $0.01, output $0.5.

| unit (measured, mean of runs) | baseline | tuned | saving |
|---|---|---|---|
| analyst note + fact-check | $0.0018 | $0.0007 | 58% |
| war room, full run (8 agents + fact-check) | $0.0123 | $0.0060 | 51% |
| outreach draft | $0.0003 | $0.0001 | 59% |
| benchmark workload | $0.0281 | $0.0126 | 55% |
| output tokens per workload | 42,800 | 15,282 | |
| ...of which reasoning | 31,138 | 4,008 | |
| wall time per workload | 286s | 117s | |

| scale (tuned) | LLM cost / month | per user / month |
|---|---|---|
| 1 users | $0.0974 | $0.0974 |
| 100 users | $0.1909 | $0.0019 |
| 10,000 users | $9.54 | $0.0010 |

Weekly research pipeline: $0.0206; fund pilot variable cost $0.0944/month.
Model updates handled without an LLM: 99.3% of 1070 replayed signals. Disk-cache hit rate across all live calls: 17%.
