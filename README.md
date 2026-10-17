# Local Ledger

Independent microcap research from local and state public records. The platform ingests public records (SEC filings, building permits, council agendas, call reports, state production and gaming data), maps them to 237 US-listed microcaps, and turns them into cited, fact-checked signals, notes, and a weekly newsletter.

> Status: under construction. See `PLAN.md` for the milestones. This README is updated at every milestone.

## Setup

```bash
make setup     # creates ./venv, installs deps, copies .env.example -> .env, refreshes analyst coverage (non-blocking)
make seed      # loads data/Microcap_Local_Gov_Universe.xlsx into the DB (237 companies, 19 archetypes)
make ingest    # runs every connector
```

LLM calls go through the Portkey gateway (OpenAI-compatible API). Set `PORTKEY_API_KEY` in your shell or `.env`. Without a key, every LLM step uses a deterministic mock, so nothing crashes. Spend is capped by `LLM_BUDGET_USD` (default 10).

## Commands (so far)

| Command | What it does |
|---|---|
| `ll seed` | Import the universe workbook |
| `ll coverage` | Run `data/coverage_check.py` (yfinance analyst counts); failure is only a warning |
| `ll ingest [--source edgar] [--tickers PCYO,TRC] [--limit N]` | Fetch public records and write cited signals |
| `ll stats` | Counts of companies, documents, notes and real signals by connector |
| `ll costs` | LLM spend ledger vs the budget cap |
