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

- LLM calls go through the **Portkey gateway** with the OpenAI SDK (`LLM_BASE_URL=https://api.portkey.ai/v1`). Set `PORTKEY_API_KEY` in your shell or a `.env` file; the key is never printed or logged.
- **`.env` lookup:** the app reads `.env` in the repo folder first, then `../.env` (the folder above the repo). Shell variables win over both. `.env.example` lists every key, with no real values.
- Models come from env: `LLM_MODEL_HEAVY` (war-room Moderator and Note writer) and `LLM_MODEL_FAST` (everything else). Both default to `gpt-6-luna` (the only model allowed on the course key).
- **Reasoning effort** is set per prompt in each file's front matter (`reasoning_effort: low|none`), chosen from `ll bench` runs; `LLM_REASONING_EFFORT` overrides every prompt. A call that runs out of output tokens retries once with less reasoning.
- **Budget cap:** `LLM_BUDGET_USD` (default `10`). Every call is recorded in the `llm_calls` ledger (input, cached input, output and reasoning tokens, latency, cost). Before a live call, its estimated cost is reserved against the cap; past the cap, LLM steps fall back to a deterministic, extractive mock instead of failing. Prices: `LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_CACHED_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK` (gpt-6-luna list prices by default).
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
| `ll models [--tickers ..] [--no-fetch] [--no-llm] [--max-llm N]` | Company models + valuation for all 237, then replay signals to record each one's impact |
| `ll signals [--limit N] [--threshold X] [--tickers ..] [--redo]` | Re-rank, then analyst notes + fact-check for top signals (model-moving signals first) |
| `ll newsletter [--week 2026-W42] [--no-new-notes]` | Weekly issue: 5–8 fact-checked signals, 1 deeper company note, "new to the universe", methodology, disclosures -> `out/newsletter/` |
| `ll warroom --ticker PCYO --replay` | Run the full crew on the latest real 8-K Ex. 99.1 vs the prior 10-Q, notes and signals |
| `ll warroom --session ID --redirect "..." [--agent bear] [--as NAME]` | Post a redirect into an existing room and rerun the affected agents |
| `ll leads` | Fund target list from the latest SEC 13F data set -> `out/leads.csv` (CUSIPs verified via OpenFIGI) |
| `ll outreach [--top 50]` | ~90-word personalized drafts -> `out/outreach/` (drafts only) |
| `ll deck [--fund NAME\|CIK] [--top N]` | 10-slide pitch deck from live data (backtest, tokenomics, red team, signals) -> `out/deck/*.html`, a single self-contained reveal.js file. `--fund` adds a slide with that fund's 13F holdings in the universe and the latest cited signal on each. No LLM calls; blocked if any slide fails the impersonal-language lint. Also at `/admin/deck` (per lead from the admin leads table) |
| `ll pdfs` | `out/pdf/fund_one_pager.pdf` and `out/pdf/sample_issue.pdf` |
| `ll publish --note-id N [--disclose]` | Mark a note published after compliance checks; prints trading-blackout reminders |
| `ll check-citations [--limit N]` | HTTP-check every signal citation URL -> `out/citation_check.csv` |
| `ll stats` | Companies, documents, notes, and real signals by connector |
| `ll costs` | LLM spend vs `LLM_BUDGET_USD` |
| `ll bench --label NAME [--effort prompt\|low\|none\|default]` | Token benchmark: a fixed live workload (8 notes + fact-check, 1 war room, 5 outreach drafts) in a sandbox copy of the DB with a fresh cache -> `out/bench/summary.md` (~$0.01-0.03 per run) |
| `ll tokenomics` | Unit costs from the bench runs and the ledger, rolled up per user and at 100 / 10,000 users -> `out/tokenomics/` |
| `ll backfill-agendas [--days 365]` | Re-run the agenda connectors over a year into `out/backtest/agenda_backfill.db` (the live feed is untouched) |
| `ll backtest [--no-fetch]` | Point-in-time backtest: forecast accuracy, call-report lead time, flagged events and the stock's move afterwards -> `out/backtest/` and `/app/backtest`. No LLM calls |
| `ll safety-demo` | Red-team demo: a poisoned agenda packet through the live analyst with and without the input filter, plus a simulated compromised model -> `out/safety/demo.md` and `/app/safety` |

Make targets: `setup seed ingest models signals newsletter demo warroom leads outreach pdfs serve test lint check stats costs citations`.

Web pages added for the course write-up: `/how-it-works` (agent diagram, every agent's role and settings, one real war-room run traced step by step), `/app/backtest`, `/app/costs` (tokenomics) and `/app/safety` (defenses, red-team results, safety event log). `python scripts/screenshots.py out/screenshots` captures every page from a running `make serve` (needs `pip install playwright` and Google Chrome).

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

## Company models and the "so what"

`ll models` (also run by `make signals`) gives every company a model and a valuation, then replays every signal through it:

- **History** comes from SEC XBRL company facts. Fiscal quarters are derived from period dates (Q4 = FY - 9M), and mis-tagged outlier quarters are dropped. Banks use NII + noninterest income. Foreign filers' IFRS facts are used when reported in USD. Royalty trusts use monthly 8-K distributions x units. Banks whose holding company isn't an SEC filer use their FFIEC call report.
- **Forecast** covers each unreported quarter, the current quarter and the next. The baseline blends seasonal growth with run-rate and is bounded on acquisition step-changes. The range is P10/P50/P90, with volatility backtested on the company's own history (seeded Monte Carlo), plus P(revenue up y/y). Discrete events (a land sale, an approval) carry probabilities by quarter.
- **Valuation** is NTM-based and set by archetype: banks P/TBV (peer range + ROTE-justified), insurers P/B (ROE-justified), others EV/EBITDA or EV/Revenue vs universe peers, trusts by distribution yield, and liquidation-basis companies by curated NAV. Land carried at historical cost shows "NAV pending" rather than a misleading book multiple. Values more than 5x away from the price are flagged low-confidence.
- **Impacts:** deterministic rules translate signals into model changes, and each rule states its assumptions. Examples: loan growth vs trend, deposit-mix funding cost, credit and CRE risk premia, monthly casino AGR vs trend, sequential production, permits elasticity, agenda approvals shifting event timing, and 8-K risk events. A capped, quote-verified LLM updater (`prompts/model_update.md`, `--max-llm`) handles news no rule covers, and it usually concludes "no quantifiable change". Each impact records the revenue estimate for the quarter the news lands in vs the next, P(up y/y), and value per share, before and after.
- **Where it shows:** the company page has a "Model & valuation" panel (gated: full valuation), the signal feed has a model-impact column, and each newsletter item gets a "So what for our model" line. The newsletter shows estimates and value sensitivity at the current multiple only: no fair values or targets.
- **Curated overlays** (`models/{TICKER}.yaml`) add events, NAVs and notes; Claude Code maintains them over time. Seeded so far: PCYO, TRC, MLP, GYRO, STHO, CPHC, CDZI.

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
