# PLAN.md — Local Ledger GTM MVP

## Context
`microcap_mvp_claude_code_prompt.md` asks for a complete go-to-market MVP built from an empty folder: a public-records ingestion platform that maps signals to 237 microcap tickers, plus a newsletter, a multiplayer agent war room, a public site and GTM tooling. The build runs autonomously through 6 milestones until the prompt's Definition of Done (DoD, §11) is met, with a commit after each milestone. The folder currently holds only the prompt. It is not a git repo yet, and the inputs are in `~/Downloads`.

## Decisions (confirmed with Andrew)
- **LLM:** Portkey + OpenAI SDK (`OpenAI(api_key=os.environ["PORTKEY_API_KEY"], base_url="https://api.portkey.ai/v1")`), which replaces the prompt's Anthropic SDK. The model env vars are `LLM_MODEL_HEAVY` and `LLM_MODEL_FAST`, and both default to `gpt-5.6-luna`. The key is never printed, logged or committed.
- **Budget:** a **$10 project cap**, set by `LLM_BUDGET_USD=10` in `.env` (one line to change). A ledger table sums the estimated cost of uncached calls. Once the cap is hit, LLM steps fall back to the deterministic `mock` provider with a warning instead of crashing. Per-token prices come from env (`LLM_PRICE_IN_PER_MTOK` / `_OUT_`), with conservative defaults.
- **Brand:** "Local Ledger". User-Agent: `Local Ledger research aceamf25@gmail.com`, read from `CONTACT_EMAIL`. Analyst tier: **$99/mo**.
- **Environment:** a `python3 -m venv venv` in the project folder, per AGENTS.md. The only Python is 3.14.7, so `requires-python >=3.11`. `.env` is loaded via python-dotenv, and the shell env takes precedence.

## Environment facts verified
- Inputs: `~/Downloads/Microcap_Local_Gov_Universe.xlsx` (Universe tab header row 4, rows 5–241 = 237 names, cols A–N; formula cells K/L/M/N have cached values) and `coverage_check.py`. Both get **copied** (not moved) into `./data/`.
- Mix: 110 community banks, 21 oil and gas/royalty, 18 commercial property, 14 land, 14 mining, plus a long tail. Top 5 by priority: TRC, MLP, CPHC, SDHC, AII. PCYO, AXR, ARTNA, SJT, CNTY, FLL and GYRO are all present.
- Reachable: SEC `company_tickers.json` + `data.sec.gov/submissions`, Census BPS county dir, FFIEC CDR bulk page, NM OCD, and the SEC 13F data-sets page. Colorado SBG returned **403** (don't circumvent it; try another URL, or switch to a property-level state such as MO, IL or IN). Legistar `shakopee` isn't a client (Shakopee goes in the PDF-agenda connector instead).
- `coverage_check.py` writes cached values only, so seeding **recomputes** K/L/M/N in Python using the same formulas.
- Not installed: docker (the Dockerfile gets written but can't be test-built) and uv (pip + pyproject instead).

## Repo layout (package `localledger`, CLI `ll` = `venv/bin/ll`)
```
pyproject.toml  Makefile  .env.example  .gitignore  Dockerfile  render.yaml
.github/workflows/weekly.yml          # disabled (if: false)
PLAN.md README.md SOURCES.md BLOCKERS.md LEGAL_REVIEW.md LAUNCH_CHECKLIST.md
data/  (xlsx, coverage_check.py, cusip_map.csv)   raw/ (gitignored)   out/ (gitignored)
config/ geo_map.yaml  agenda_sources.yaml  legistar_clients.yaml  bank_map.yaml  holdings.yaml  watchlists/{TICKER}.yaml
prompts/*.md        theses/{TRC,MLP,CPHC,SDHC,AII,PCYO}.md      models/{TICKER}.yaml
src/localledger/
  config.py db.py models.py cli.py seed.py
  http.py            # rate limiter, robots.txt cache, UA, raw-doc store (sha256 → raw/)
  llm/ client.py cache.py ledger.py mock.py prompts.py
  connectors/ base.py edgar.py census_bps.py legistar.py ffiec.py og_state.py gaming_state.py pdf_agenda.py
  signals/ scoring.py rules.py
  research/ analyst.py factcheck.py retrieval.py   # BM25 over 10-K/10-Q chunks
  warroom/ crew.py agents.py board.py
  gtm/ newsletter.py leads13f.py outreach.py pdfs.py
  compliance.py
  web/ app.py auth.py routes_public.py routes_pilot.py routes_warroom.py routes_admin.py templates/ static/
tests/ fixtures/<connector>/...  test_*.py
```

## Data model (SQLModel, SQLite WAL, Postgres-ready via `DATABASE_URL`)
`companies` (all xlsx columns + recomputed priority/size band + CIK), `sources` (name, type, access_method, cadence, terms_note, status available/unavailable, last_error), `documents` (source_id, ticker, url, fetched_at, sha256, path, content_type, origin=real|fixture), `signals` (ticker, source_id, as_of, metric, value, prior_value, delta, significance, summary, **citation_url NOT NULL, document_id FK NOT NULL**, rank_score), `notes` (ticker, kind=signal|company|newsletter|warroom, body_md, claims_json, status draft/approved/published, factcheck_log, disclosure_flag), `warroom_sessions`, `warroom_events` (session_id, agent, type, payload_json, actor, created_at), `agent_threads` (session, agent, claimed_by), `presence`, `subscribers`, `pilot_requests`, `leads` (fund, CIK, overlap tickers, overlap value, contact_status), `llm_calls` (command, model, tokens, est_cost, cached), `newsletter_issues`.
Rule: one `make_signal()` constructor raises when citation_url or document_id is missing. No other code path writes signals.

## Connectors (`discover/fetch/parse/to_signals`, shared `http.py`)
Every connector gets rate limiting, robots.txt checks and the declared UA. Each has an offline fixture test. On failure it marks the source unavailable, logs to BLOCKERS.md and continues. The access method actually found at build time goes in SOURCES.md.
1. **EDGAR** (backbone): ticker→CIK from `company_tickers.json`; submissions; 8-K Ex. 99.1; 10-K/10-Q primary-doc text (chunked for retrieval); XBRL `companyfacts` for y/y metric deltas. Signals: earnings 8-K filed, KPI deltas, material 8-K items, late filings. 10 req/s cap.
2. **Census BPS:** monthly county files (`www2.census.gov/econ/bps/County/`), plus place-level where available. `geo_map.yaml` seeded for PCYO (Arapahoe CO), AXR (Sandoval NM / Rio Rancho), ARTNA (Sussex/New Castle DE), MLP (Maui HI) and SDHC (its Southeast metros). Signals: units/1-unit permits y/y and YTD.
3. **Legistar Web API:** `webapi.legistar.com/v1/{client}/events|matters`. Probe candidate clients from the Note geographies (e.g. Kern, Maui County, Loudoun) and record which work or need tokens. A per-ticker keyword watchlist (company, project and parcel terms, seeded from 10-K text) produces an agenda-match signal.
4. **FFIEC call reports:** the CDR bulk single-period zip. If the form post isn't workable, fall back to the FDIC BankFind financials API, which is derived from call reports, has no key, and is documented as a fallback. `bank_map.yaml` (ticker→FDIC cert/RSSD) is auto-matched on holding-company name and state, with manual overrides. Signals: loan growth, deposit mix, CRE concentration, NPAs, each q/q.
5. **State oil & gas (one state): NM OCD** for SJT (Hilcorp, San Juan Basin). Production trend → implied next distribution vs the last one (last distribution taken from SJT's 8-K).
6. **State gaming (one state):** Colorado first (CNTY/FLL; town-level data). If Colorado stays blocked, or only town-level data exists, use the first accessible property-level state (MO/IL/IN). Signal: revenue y/y.
7. **PDF agenda (generic):** `agenda_sources.yaml`, one line per town (url or index+link pattern, tickers); pdfplumber + the same watchlist. Seeded with Smithtown NY (GYRO), Shakopee MN (CPHC) and one more from the Notes (e.g. Santa Paula CA/LMNR or Asbury Park NJ/STHO).

## Signal → research pipeline
- `rank_score = w·significance + (1−w)·priority_norm`. Significance comes from rules first (thresholded % change / z-score per metric); an optional fast-LLM adjustment is bounded to ±0.15.
- **Analyst agent** (fast model, JSON schema `{summary, claims:[{text, citation:{document_id,url,quote}}]}`): runs on signals above the threshold (max N per run to protect the budget) and cites both the signal document and a BM25-retrieved 10-K/10-Q passage.
- **Fact-check:** a deterministic gate (each quote appears verbatim in the normalized doc text, and every number in a claim appears in its quote or doc), then a fast-LLM support check. Unsupported claims are dropped and logged to `notes.factcheck_log`. A note with any uncited claim raises `CitationError`.
- **Impersonal-language lint:** flags buy/sell/price-target or "you should" phrasing, which blocks approval.

## War room
- An asyncio DAG: Reader → {Model, Thesis, Read-across} → {Bull, Bear} → Moderator → Note writer → Fact-check. Each agent has its own `prompts/warroom_*.md`. Heavy model: Moderator and Note writer. Fast model: the rest.
- Every step writes to `warroom_events`. SSE (`/warroom/{id}/stream`) tails the events table by last id, so CLI runs and web runs share one board.
- Multiplayer (passcode + display-name signed cookie): a presence heartbeat; **redirect** (an event that reruns the targeted agent plus everything downstream, with the redirect text in context); **claim/hand back** (a claimed thread pauses that agent and the human's edit becomes its output); **approve** (runs the compliance check, then sets status approved).
- `ll warroom --ticker PCYO --replay` uses the latest real 8-K Ex. 99.1, the prior 10-Q, prior notes and signals. The second ticker is a top-priority name with a recent Ex. 99.1, chosen at build time. Live audio sits behind `FEATURE_LIVE_AUDIO=0`, with a provider interface stub.

## Web (FastAPI + Jinja2 + HTMX + Tailwind Play CDN, all pinned versions)
- Public: Landing (headline, 3 proof points, a real signal card with its citation, CTA), Sample issue, Methodology & sources, Pricing (Free / Analyst $99/mo / Fund pilot), Waitlist + Pilot forms (written to the DB, CSV export in admin), Disclosures.
- Gated: Universe table (archetype, coverage, size band and priority filters via HTMX), Company page, Signal feed, War room list/session.
- Admin: waitlist and pilot counts, issues generated, leads-contacted toggle, blackout reminders.
- A dense analyst UI, mobile-readable, no images. The disclosure footer partial is in the base template.

## GTM
- `ll newsletter --week YYYY-WW`: top 5–8 fact-checked signals, 1 deeper company note, a "new to the universe" item and a methodology footer → `out/newsletter/{week}.md` + inline-styled `.html`. It is never sent.
- `ll leads`: downloads the latest SEC 13F data set zip (INFOTABLE/SUBMISSION/COVERPAGE) and maps CUSIP→ticker by fuzzy issuer-name match against the universe, cached in the hand-editable `data/cusip_map.csv`. It lists funds with ≥2 overlapping names, ranked by overlap count then value; mega index managers are flagged/excluded. Output: `out/leads.csv`.
- `ll outreach`: 90-word drafts for the top 50 leads, each citing the fund's actual overlapping holdings plus one recent signal → `out/outreach/*.md`. Deterministic template fallback when no LLM is available.
- PDFs (xhtml2pdf, from the same Jinja templates): `out/pdf/fund_one_pager.pdf` and `out/pdf/sample_issue.pdf`.

## Compliance (in code)
- The disclosure footer (independent, no issuer compensation, impersonal and not advice, holdings policy, data may be wrong) appears on every page, issue and PDF, and a test asserts it.
- `config/holdings.yaml` + `compliance.can_publish(ticker)` raise `PublishBlocked` unless the disclosure flag is set. A blackout reminder (N days, configurable) prints on publish and shows in admin.
- Every connector checks robots.txt, and blocked sources are not circumvented. SOURCES.md is the source register. LEGAL_REVIEW.md lists the open questions only (publisher's exclusion vs RIA registration, disclosure language, scalping) and draws no conclusions.

## LLM layer
`llm/client.py` wraps the Portkey/OpenAI client (JSON-mode, retries), with a disk cache at `.cache/llm/{sha256(prompt_name+prompt_text+model+input)}.json`, the budget ledger, and `mock` (extractive, deterministic, used in tests and when there's no key or the budget is exhausted). Prompts load from `prompts/*.md` only. Each CLI command prints its token and estimated cost summary.

## Milestones (commit after each, pytest + ruff green first; messages end with the Co-Authored-By line)
0. `git init`, copy inputs to `./data/`, write `PLAN.md` (milestones, file tree, data model, risks).
1. Skeleton + config + DB + `ll seed` (237 companies) + `make setup` running coverage_check non-blocking + http layer + **EDGAR** + LLM layer.
2. **Vertical slice:** Census BPS → signals → analyst note → fact-check → company page → newsletter.
3. **Remaining connectors.** As the prompt asks, 3 parallel subagents build them against `connectors/base.py`, each owning separate files and tests: (FFIEC), (Legistar + PDF agenda), (NM OCD + gaming). The main thread integrates and commits.
4. **War room:** crew, board, SSE, multiplayer, replays for PCYO and the second ticker.
5. **Public site + GTM:** pages, forms, admin, leads, outreach, PDFs.
6. **Polish:** README (every command), SOURCES/BLOCKERS/LEGAL_REVIEW, Dockerfile + render.yaml (not deployed), the disabled GH workflow, LAUNCH_CHECKLIST.md (accounts, env vars, deploy command, first 10 outreach targets from leads.csv, newsletter platform, a time estimate per step).

## Hard stops (I will ask first)
Deploying, sending any email or message, spending money beyond the $10 LLM cap or creating paid accounts, deleting anything in `./data/` or `./raw/`, publishing anything publicly.

## Key risks and mitigations
Endpoint changes or blocks: probe at build time, fall back, log in BLOCKERS.md, and target ≥4 connectors where 6 exist. FFIEC bulk form: FDIC API fallback. 13F CUSIP mapping: fuzzy match plus a manual CSV. Python 3.14 wheels (pdfplumber/pypdfium2, curl_cffi for yfinance): if an install fails, get Python 3.12 via `brew install python@3.12`, which is free. Budget: heavy model only in the war room, capped notes per run, caching everywhere.

## Verification (the DoD checklist, run at the end)
1. On a fresh clone in the scratchpad: `make setup && make seed && make ingest && make signals && make newsletter && make demo`. Repeat with `PORTKEY_API_KEY` unset to confirm graceful degradation.
2. `ll stats`: 237 companies; ≥4 connectors with `origin=real` signals; ≥25 distinct tickers; `ll check-citations` passes HTTP checks on every citation URL.
3. The newsletter issue is in `out/newsletter/`, fact-check passed (log shows the dropped claims), and disclosures are present.
4. `ll warroom --ticker PCYO --replay` and the second ticker complete. A two-client script (two cookie sessions on SSE): both receive identical events, and client A's redirect changes the bear/note output that client B sees. Then a manual check with two browser windows.
5. curl the public pages (200 + disclosure marker) and POST the waitlist/pilot forms (rows in DB, CSV export).
6. `out/leads.csv` exists and `out/outreach/` holds ≥20 drafts; both PDFs render.
7. `pytest` green (connector fixtures, signal math, citation enforcement, compliance block, fact-check, cache/budget, routes, SSE multiplayer with the mock LLM); `ruff check` clean; all docs present.
8. `ll costs` shows the ledger total under $10.

---

## Status at end of build (2026-10-17)

All six milestones were built and committed (`git log`). Deviations from the original prompt, each deliberate:
- **LLM:** Portkey + OpenAI SDK with `gpt-5.6-luna` (Andrew's standing instruction), not the Anthropic SDK. Heavy and fast tiers are separate env vars. There's a $10 budget cap with a mock fallback.
- **Census BPS:** built, but automated fetching is off because www2.census.gov's robots.txt disallows generic agents under RFC 9309. A manual inbox works meanwhile (BLOCKERS.md).
- **Oil and gas:** New Mexico C-115 data comes via NM Tech GO-TECH; OCD's own report pages are behind Cloudflare Turnstile.
- **Gaming:** Indiana (property-level PDFs, FLL Rising Star) instead of Colorado (CloudFront 403).
- **Second war-room ticker:** TRC.

## Definition of Done: verification (2026-10-17)
- Fresh clone (`git clone` into a scratch dir, **no PORTKEY_API_KEY**): `make setup && make seed && make ingest && make signals && make newsletter && make demo` exited 0. Census degraded with its robots.txt message, and the LLM steps ran on the mock.
- `ll stats`: 237 companies; 6 connectors with real signals (edgar, ffiec, legistar, pdf_agenda, og_state, gaming_state) across 226 tickers. `ll check-citations`: 559/559 citation URLs return HTTP 200.
- Newsletter 2026-W42 was built from real signals with the live LLM: 8 items, a deep company note, new-to-the-universe, methodology and disclosures. Fact-check passed.
- War rooms on PCYO (#2) and TRC (#3) completed with the live LLM. Two logged-in clients on room #2 received an identical SSE event sequence; Alice's redirect ("rerun bear case assuming lower volumes") changed the Bear's output and reran Moderator and Note writer, and Bob saw it.
- Public pages render with disclosures; the waitlist and pilot forms write to the DB (tests).
- `out/leads.csv` has 860 funds (97 research buyers after filters); `out/outreach/` has 50 drafts.
- `pytest`: 113 passed; `ruff check`: clean.

---

## Milestone 7: company models, valuation and the "so what" (requested 2026-10-17)

Goal: every company has a model and a valuation, and every signal states how it moves them: revenue for the quarter the news lands in vs the next, the probability revenue grows year over year, and value per share.

Decisions (Andrew): full valuation inside the gated app; the newsletter shows only the impact on estimates and value sensitivity at the current multiple (no fair values or targets; the impersonal-research lint stays). Claude Code builds and curates the base models over time; the Portkey LLM only *updates* them when news arrives. The budget cap stays at $10.

Design:
- `modeling/history.py`: SEC XBRL company facts (`data.sec.gov/api/xbrl/companyfacts`) -> quarterly series derived from period start/end dates (Q4 = FY - 9M YTD), latest balance sheet, shares. Banks use NII + noninterest income as revenue.
- `modeling/engine.py`: deterministic base model per company. Quarters are fiscal: unreported-ended, current, next. Seasonal + run-rate baseline, volatility from a backtest of the same method, seeded Monte Carlo -> P10/P50/P90, P(revenue up y/y). Discrete events carry probabilities by quarter. Valuation by archetype (banks: P/TBV peer + ROTE-justified; insurers/land: P/B; others: EV/EBITDA or EV/Revenue vs universe peers; pre-revenue: not modeled) and NTM-based.
- `models/{TICKER}.yaml`: curated overlays (drivers, events with probabilities, valuation overrides), maintained by Claude Code over time.
- `modeling/impacts.py`: signal -> adjustments. Deterministic rules first (call-report loan growth vs trend, deposit-mix funding cost, credit risk premia, monthly gaming AGR vs trend, production vs trend, permits elasticity, agenda approvals shift event timing, 8-K risk events). Then a bounded, quote-verified LLM updater (`prompts/model_update.md`) for news no rule covers. Each impact records before/after per quarter and per share.
- Tables `company_models` and `model_impacts`; `ll models` builds base models, then replays signals.
- Surfaces: a company-page model and valuation panel (gated), a model-impact column in the signal feed, a "So what for our model" line in each newsletter item (impact only), and model context for the war-room Model agent.

### Milestone 7 status (2026-10-17)
Built and verified:
- **Valuation coverage:** 237 company models; 228 valued. The other 9 each carry an explicit reason (BLOCKERS.md).
- **Signal replay:** 1,070 signals replayed, of which about 180 moved a model. The LLM updater ran within its cap and is mostly conservative.
- **Newsletter:** the 2026-W42 issue was regenerated with "So what for our model" lines and passed fact-check.
- **Tests:** 127 passing.

Forecast guards were added after review:
- outlier-quarter removal;
- median-centred ranges with capped volatility;
- bounded seasonal ratios;
- step-change (acquisition) detection;
- a sequential production rule;
- materiality thresholds (0.5%).
