# Local Ledger

Independent research on under-covered US microcaps, built from the local and state public records that move them.

Local Ledger ingests public records (SEC filings, building permits, council agendas, bank call reports, state oil and gas production, state gaming revenue), maps them to a 237-company universe, and turns them into ranked, **cited, fact-checked** signals. It's built for three uses:

1. **A public site** with a landing page, sample issue, methodology, pricing, disclosures, and waitlist and fund-pilot forms.
2. **A gated pilot app** with the universe table, company pages, a ranked signal feed, and a **multiplayer earnings war room** where a crew of agents writes a sourced post-earnings note while several people watch, redirect and approve.
3. **GTM tooling** for Andrew: a weekly newsletter draft, a 13F-based fund target list, personalized outreach drafts, and PDFs. Nothing is ever sent or published automatically.

Every signal carries a citation URL and a stored source document (`./raw/`, with sha256). A note with an uncited claim fails, and a fact-check pass drops any claim not supported verbatim or numerically by its source.

## Setup

Requires Python 3.11+ and `make`. Uses a local virtual environment in `./venv`.

```bash
make setup     # venv + deps, copies .env.example -> .env, refreshes analyst coverage via yfinance (warning only on failure)
make seed      # 237 companies + 19 archetypes from data/Microcap_Local_Gov_Universe*.xlsx; registers sources
make ingest    # every connector; a source that is down or blocked is marked unavailable and the run continues
make signals   # re-rank signals; analyst notes + fact-check for the top signals
make newsletter  # this week's issue -> out/newsletter/YYYY-Www.{md,html}   (never sent)
make demo      # earnings war room replay for PCYO
make serve     # web app at http://127.0.0.1:8000
```

### LLM access and cost controls

- LLM calls go through the **Portkey gateway** with the OpenAI SDK (`LLM_BASE_URL=https://api.portkey.ai/v1`). Set `PORTKEY_API_KEY` in your shell or `.env`; the key is never printed or logged.
- Models come from env: `LLM_MODEL_HEAVY` (war-room Moderator and Note writer) and `LLM_MODEL_FAST` (everything else). Both default to `gpt-5.6-luna`.
- **Budget cap:** `LLM_BUDGET_USD` (default `10`). Every call is recorded in the `llm_calls` ledger. Before a live call, its estimated cost is reserved against the cap; past the cap, LLM steps fall back to a deterministic, extractive mock instead of failing. Prices for the estimate are `LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK` (conservative defaults; set them to your Portkey pricing).
- Every prompt lives in `prompts/*.md`. Calls are cached on disk (`.cache/llm/`) by prompt name, prompt text, model and rendered inputs, so reruns cost nothing.
- Every command prints its token count and estimated cost. `ll costs` shows the ledger against the cap.
- No key at all? Everything still runs on the mock (`LLM_PROVIDER=mock` forces it).

## Commands

All commands are `ll <command>` (the console script lives at `venv/bin/ll`; `python -m localledger` also works).

| Command | What it does |
|---|---|
| `ll seed` | Import the universe workbook (prefers the coverage-checked copy) and register sources |
| `ll coverage` | Run `data/coverage_check.py` (yfinance analyst counts). Failure is a warning |
| `ll ingest [--source a,b] [--tickers T1,T2] [--limit N]` | Run connectors and write cited signals |
| `ll signals [--limit N] [--threshold X] [--tickers ..] [--redo]` | Re-rank, then analyst notes + fact-check for top signals |
| `ll newsletter [--week 2026-W42] [--no-new-notes]` | Weekly issue: 5–8 fact-checked signals, 1 deeper company note, "new to the universe", methodology, disclosures -> `out/newsletter/` |
| `ll warroom --ticker PCYO --replay` | Run the full crew on the latest real 8-K Ex. 99.1 vs the prior 10-Q, notes and signals |
| `ll warroom --session ID --redirect "..." [--agent bear] [--as NAME]` | Post a redirect into an existing room and rerun the affected agents |
| `ll leads` | Fund target list from the latest SEC 13F data set -> `out/leads.csv` (CUSIPs verified via OpenFIGI) |
| `ll outreach [--top 50]` | ~90-word personalized drafts -> `out/outreach/` (drafts only) |
| `ll pdfs` | `out/pdf/fund_one_pager.pdf` and `out/pdf/sample_issue.pdf` |
| `ll publish --note-id N [--disclose]` | Mark a note published after compliance checks; prints trading-blackout reminders |
| `ll check-citations [--limit N]` | HTTP-check every signal citation URL -> `out/citation_check.csv` |
| `ll stats` | Companies, documents, notes, and real signals by connector |
| `ll costs` | LLM spend vs `LLM_BUDGET_USD` |

Make targets: `setup seed ingest signals newsletter demo warroom leads outreach pdfs serve test lint check stats costs citations`.

## Connectors

Each connector implements `discover() / fetch() / parse() / to_signals()` (`src/localledger/connectors/base.py`) and uses the shared HTTP layer: declared User-Agent `Local Ledger research <CONTACT_EMAIL>`, per-host rate limits, robots.txt (RFC 9309), retries, and raw-document storage. Each has offline fixture tests. Access methods verified at build time are recorded in `SOURCES.md`; anything blocked is in `BLOCKERS.md`.

| id | Source | Status (Oct 2026) | Config |
|---|---|---|---|
| `edgar` | SEC EDGAR: submissions, 8-K Ex. 99.1, 10-K/10-Q text, XBRL frames (y/y revenue, or total interest income for banks, and net income) | live: ~600 signals, 218 tickers | — |
| `census_bps` | Census Building Permits Survey (county/place, monthly) | built and tested; **automated fetch blocked by www2.census.gov robots.txt** (RFC 9309 reading); reads the manual inbox | `config/geo_map.yaml`, `data/inbox/census_bps/` |
| `ffiec` | FFIEC CDR bulk call reports: loan growth, deposit mix, CRE concentration, noncurrent loans | live: 440 signals, all 110 banks | `config/bank_map.yaml` |
| `legistar` | Legistar Web API agendas + keyword watchlists (Maui, Arapahoe, San Bernardino, Westchester, Maricopa, Boston) | live | `config/legistar_clients.yaml`, `config/watchlists/*.yaml` |
| `pdf_agenda` | Agenda PDFs for non-Legistar towns (Smithtown, Asbury Park ×2, Cortlandt, LA County BOS). **Adding a town is a one-line edit** | live (Shakopee blocked by robots.txt) | `config/agenda_sources.yaml` |
| `og_state` | New Mexico C-115 production (via NM Tech GO-TECH; OCD's own pages are behind a bot challenge) -> SJT/MTR distribution direction, EP/PED production | live | `config/og_map.yaml` |
| `gaming_state` | Indiana Gaming Commission property-level AGR (FLL Rising Star). Colorado is CloudFront-blocked; see BLOCKERS.md | live | `config/gaming_map.yaml` |

## Web app

`make serve`, then:

- Public: `/` (landing), `/sample`, `/methodology`, `/pricing`, `/disclosures`, plus the waitlist and fund-pilot forms (stored in the DB).
- Pilot (log in at `/login` with `PILOT_PASSCODE` and your name): `/app/universe`, `/app/company/{TICKER}`, `/app/signals`, `/warroom`.
- Admin (log in with `ADMIN_PASSCODE`): `/admin` shows waitlist and pilot counts, issues generated, the 13F leads with a contacted toggle, LLM spend, and blackout reminders. CSV exports: `/admin/export/waitlist.csv`, `/admin/export/pilot_requests.csv`.

### Earnings war room

Crew: Reader → {Model, Thesis, Read-across} (concurrent) → {Bull, Bear} (concurrent) → Moderator → Note writer → fact-check. Each agent has its own prompt file. Everything goes to `warroom_events`, and the board streams over Server-Sent Events. Anyone in the room sees who's present, can **redirect** an agent (it and everything downstream rerun), **claim** a thread (the agent stops overwriting it and the human edits the output), hand it back, and **approve** the final note (approval runs the fact-check gate, the impersonal-language lint and the holdings publish block). Theses live in `theses/{TICKER}.md` (drafts for TRC, MLP, CPHC, SDHC, AII, PCYO). Driver models live in `models/{TICKER}.yaml`; updated versions are written to `out/models/`. Live audio transcription is behind `FEATURE_LIVE_AUDIO=0` (stretch, off).

To try multiplayer: `make demo`, then `make serve`, open `/warroom/<id>` in two browsers (log in with different names), and send "rerun bear case assuming lower volumes" from one. Both boards update.

## Compliance guardrails (in code)

- Disclosure footer on every page, issue and PDF (`compliance.disclosures()`); tests assert it.
- `config/holdings.yaml` + `compliance.check_publish()`: notes on held tickers are blocked unless the disclosure flag is set. A blackout reminder (`BLACKOUT_DAYS`, default 3) prints on publish and approval and shows in admin.
- An impersonal-research lint rejects recommendation language in claims, notes, issues and outreach.
- Public data only, robots.txt honored, no logins. See `SOURCES.md`, `BLOCKERS.md` and `LEGAL_REVIEW.md` (questions for counsel).

## Development

```bash
make test      # pytest (offline: fixtures + mock LLM)
make lint      # ruff
```

Data layout: `data/` holds the inputs and `cusip_map.csv` (committed). `raw/` holds raw fetched documents, `out/` generated drafts and PDFs, and `.cache/` the LLM cache; all three are gitignored. The DB is `localledger.db` (SQLite, WAL); set `DATABASE_URL` for Postgres (`pip install -e ".[postgres]"`). Deploy files (`Dockerfile`, `render.yaml`) are included but nothing is deployed. See `LAUNCH_CHECKLIST.md`.
