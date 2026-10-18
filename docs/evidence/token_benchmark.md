# Token benchmark

Workload: 8 analyst notes + fact-check, 1 war-room replay (PCYO), 5 outreach drafts. Every call live (fresh cache). Costs at the ledger prices in .env.

| run | effort | calls | input tok | cached in | output tok | reasoning | cost | wall s | notes passed | claims kept/written | war-room note |
|---|---|---|---|---|---|---|---|---|---|---|---|
| a_baseline | default | 31 | 85,179 | 0 | 45,112 | 34,262 | $0.0311 | 283.8 | 8/8 | 22/27 | passed (6 kept) |
| a_baseline_r2 | default | 30 | 75,953 | 29,810 | 40,488 | 28,013 | $0.0252 | 288.3 | 8/8 | 25/29 | passed (6 kept) |
| b_low | low | 30 | 73,600 | 0 | 16,248 | 5,213 | $0.0155 | 121.2 | 7/8 | 23/26 | passed (4 kept) |
| c_none | none | 30 | 75,612 | 0 | 11,135 | 0 | $0.0131 | 94.1 | 6/8 | 18/24 | passed (6 kept) |
| d_tuned | prompt | 30 | 74,287 | 24,919 | 15,267 | 4,074 | $0.0128 | 120.9 | 7/8 | 20/27 | passed (6 kept) |
| d_tuned_r2 | prompt | 30 | 73,746 | 29,810 | 15,298 | 3,941 | $0.0123 | 113.2 | 7/8 | 21/26 | passed (3 kept) |
