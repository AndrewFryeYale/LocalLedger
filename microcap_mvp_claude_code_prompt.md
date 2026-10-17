# One-shot Claude Code prompt — Microcap Local-Gov Research MVP

> **How to use:** Create an empty folder, put `Microcap_Local_Gov_Universe.xlsx` and `coverage_check.py` in `./data/`, open Claude Code there (plan mode first is recommended), and paste everything below the line. Fill in the `[BRACKETED]` items first.

---

You are building the go-to-market MVP of an independent microcap research business from an empty repo. Work autonomously through every milestone below until the Definition of Done is met. Plan first, then build in vertical slices, commit after each milestone, and keep going without check-ins unless you hit one of the hard stops listed under "Ask before".

## 1. Context

**Founder:** Andrew, a Yale SOM MBA student and former small-cap long/short buy-side analyst. He has limited time, a near-zero budget, and wants revenue during the MBA.

**Thesis:** Hundreds of US-listed microcaps have little or no sell-side coverage. Their fundamentals are driven by local and state government decisions and public records that nobody reads systematically: planning-board votes, building permits, utility rate cases, state oil & gas production, state gaming revenue, bank call reports, insurance rate filings. Agents can read all of it, map it to tickers, and turn it into independent research.

**Business model (in order):**
1. A platform that ingests public records and turns them into ticker-level signals.
2. A weekly newsletter for fund managers built on those signals. This is the GTM wedge.
3. Paid access to the platform itself once there is pull: fund pilots, then subscriptions.

**Brand:** `[BRAND NAME — placeholder "Local Ledger" if blank]`. The research is **independent**: never issuer-paid, public data only.

**Inputs already in `./data/`:**
- `Microcap_Local_Gov_Universe.xlsx`: 237 US-listed names under $500M. The `Universe` tab has these columns: Ticker, Company, Mkt cap, Archetype, Local-gov driver, Public data to ingest, Note, Analyst coverage, scores, and Priority. The `Archetypes` tab maps 19 archetypes to drivers and datasets.
- `coverage_check.py`: fills in analyst coverage via yfinance. Run it during setup, and treat a failure as a non-blocking warning.

## 2. What "GTM MVP" means here

An outsider can do all of the following:
1. Land on a public site, understand the product in 10 seconds, read a sample issue, and join the waitlist or request a fund pilot.
2. Log in as a pilot user and browse the coverage universe, company pages, and a live signal feed with source citations.
3. Open an earnings war room for a covered company and watch a crew of agents produce a sourced post-earnings note, with multiple humans in the same session.

Andrew can, with one command each:
- Generate this week's newsletter draft from new signals, review it, and export it to paste into his email platform.
- Produce a fund target list and personalized (unsent) outreach drafts.

## 3. Stack (keep it boring, one language where possible)

- **Python 3.11+**, `uv` or `pip` + `pyproject.toml`, `ruff`, `pytest`.
- **Backend:** FastAPI + SQLModel. Use SQLite locally and make it Postgres-ready through `DATABASE_URL`.
- **Frontend:** server-rendered Jinja2 + HTMX + Tailwind (CDN or standalone CLI). No SPA. Use Server-Sent Events for live war-room updates.
- **Jobs:** a Typer CLI (`ll ingest`, `ll signals`, `ll newsletter`, `ll warroom`, `ll leads`) plus a `make` target per job. Scheduling is cron-friendly; add a sample GitHub Actions workflow but leave it disabled.
- **LLM:** the Anthropic Python SDK. Read `ANTHROPIC_API_KEY`, plus optional `ANTHROPIC_BASE_URL` and headers so a Portkey gateway works.
  - Read model IDs from env: `CLAUDE_MODEL_HEAVY` (default `claude-opus-5-5`) and `CLAUDE_MODEL_FAST` (default `claude-haiku-5-5`).
  - Put every prompt in `/prompts/*.md`, never inline.
  - Cache LLM calls on disk, keyed by prompt and input hash, so reruns are cheap.
- **Auth:** simple email magic link or a shared pilot passcode, whichever is faster. Keep the public pages public.
- **Deploy:** add a `Dockerfile` and `fly.toml` or `render.yaml`, but **do not deploy**.

## 4. Data layer

### Schema (minimum)

- `companies`: from the xlsx, including archetype, priority, and coverage.
- `sources`: name, type, access method, cadence, terms note.
- `documents`: raw fetched item, URL, fetched_at, sha256, and a local file path. Store raw files under `./raw/` so every claim is reproducible.
- `signals`: ticker, source, as_of date, metric, value, prior value, delta, significance 0–1, one-line summary, citation URL, document id.
- `notes`: research notes and newsletter items, with status draft/approved/published.
- `warroom_sessions` and `warroom_events`.
- `subscribers` and `pilot_requests`.
- `leads`: fund name, CIK, overlapping holdings, contact status.

### Connectors

Build these 6 first. Each is a class with `discover() / fetch() / parse() / to_signals()`. Each one also needs:
- **Rate limiting**, robots.txt respect, and a declared User-Agent of `[BRAND] research [CONTACT EMAIL]`. The SEC requires a declared User-Agent.
- **Fixture-based tests** using saved sample responses, so tests run offline.
- **Graceful degradation:** if access fails or needs a key that isn't set, log it, mark the source unavailable, and continue.
- **Verify the real access method at build time.** Endpoints change. Record what you found in `SOURCES.md`.

The connectors:
1. **SEC EDGAR:** company submissions and filings, 8-K earnings exhibits (Ex. 99.1), and 10-K/10-Q text for every ticker. This is the backbone; build it first.
2. **Census Building Permits Survey:** monthly county/place permits. Map each to the relevant land/utility/homebuilder tickers via a hand-maintained `geo_map.yaml`, seeded for PCYO, AXR, ARTNA, MLP, and SDHC.
3. **Legistar Web API:** meetings and matters for municipalities that use it. Run a keyword watchlist per ticker (company name, project names, parcel/project terms from 10-Ks) and output signals when a match appears on an agenda.
4. **FFIEC call reports (bulk):** quarterly bank-level data for the ~110 community banks. Signals include loan growth, deposit mix, CRE concentration, NPAs, and changes vs. the prior quarter.
5. **State oil & gas production:** start with **one** state that covers the royalty trusts (e.g., New Mexico OCD for SJT). Signal: production trend → implied next distribution vs. last.
6. **State gaming revenue:** monthly property-level reports for **one** state (e.g., Colorado for CNTY/FLL). Signal: property revenue y/y.

Also add a **generic "PDF agenda" connector** for non-Legistar towns. It takes a list of URLs in `agenda_sources.yaml`, extracts text with pdfplumber, and runs the same keyword watchlist. Seed it with 3 towns from the Note column (e.g., Smithtown NY for GYRO, Shakopee MN for CPHC). This long tail is the moat, so make adding a town a one-line YAML edit.

**Mapping rule:** every signal must carry a citation URL and document id. If a signal can't be traced to a document, it doesn't exist.

## 5. Signal → research pipeline

- **Scoring:** combine `significance` (rules first, LLM second) with the company `priority` to rank a weekly feed.
- **Agent "analyst" per signal:** for any signal above a threshold, a fast-model agent writes a 3–5 sentence "what changed and why it matters" note. It must cite the source document and the relevant 10-K/10-Q passage. Use a strict output schema (JSON) with `claims[]` → each claim has `citation`.
- **Fact-check pass:** a second agent re-reads the cited documents and rejects any claim not supported verbatim or numerically. Rejected claims are dropped and logged. This is non-negotiable; credibility is the product.

## 6. Multiplayer multi-agent earnings war room

This is the flagship demo. It runs on real public inputs: the 8-K Ex. 99.1 press release, the prior 10-Q, prior notes, and signals. An uploaded transcript or audio is optional.

**Crew** (each a separate agent with its own prompt file, sharing one session "board" in the DB):
- **Reader:** extracts reported KPIs and guidance into a structured table vs. prior period.
- **Model agent:** updates a simple driver model (YAML/CSV per company) and shows deltas.
- **Thesis agent:** checks results against the company's stored thesis and kill criteria (`/theses/{TICKER}.md`, seeded with drafts for the top 5 priority names).
- **Read-across agent:** pulls recent signals and peer filings in the same archetype and flags implications.
- **Bull and Bear agents:** each argues the print in ≤150 words. A **Moderator** reconciles them and records the open disagreements.
- **Note writer:** produces the post-earnings note, then passes it through the fact-check pass.

**Orchestration:** run agents concurrently where independent, using asyncio. Every agent writes events to `warroom_events`, and the UI streams them via SSE.

**Multiplayer:** any logged-in user in the session sees the same live board, can post a redirect ("rerun bear case assuming lower volumes"), can claim or hand back an agent thread, and can approve the final note. Show who is present.

**Demo command:** `ll warroom --ticker PCYO --replay` replays the most recent real quarter end-to-end.

**Stretch (feature-flagged off):** live audio → transcript streaming via a pluggable transcription provider.

## 7. Web app pages

**Public:**
- **Landing:** headline, 3 proof points, a sample signal card with its citation, CTA.
- **Sample issue.**
- **Methodology & data sources.**
- **Pricing:** Free newsletter / Analyst $[X]/mo / Fund pilot — contact.
- **Waitlist form and Fund pilot request form**, both stored in the DB with CSV export.
- **Disclosures page** (see §9).

**Pilot (gated):**
- **Universe table:** filter by archetype, coverage, size band, priority.
- **Company page:** drivers, data sources, signal timeline, notes, thesis, filings.
- **Signal feed:** ranked, with citations.
- **War room list and session view.**

**Design:** clean and dense, built for analysts rather than as a marketing site. Mobile-readable. No stock photos.

## 8. GTM engine

- `ll newsletter --week YYYY-WW` builds the weekly issue: top 5–8 signals, 1 deeper company note, a "new to the universe" item, and a methodology footer. Output Markdown and email-safe HTML to `./out/newsletter/`. It is **never sent automatically.**
- `ll leads` builds the fund target list from **SEC Form 13F data sets**: funds that hold ≥2 names in the universe, ranked by overlap count and position size. Output `./out/leads.csv`.
- `ll outreach` drafts a personalized 90-word email per top-50 lead. Each draft references that fund's actual overlapping holdings and one relevant recent signal. Output to `./out/outreach/` as drafts only.
- Build a fund one-pager (PDF) and a sample-issue PDF from the same templates.
- Track simple metrics in the DB and an admin page: waitlist count, pilot requests, newsletter issues generated, leads contacted (manual toggle).

## 9. Compliance guardrails (build them in, don't just document them)

- Public data only. No expert calls, no scraping behind logins, honor ToS/robots.txt, and keep a source register (`SOURCES.md`) with access method and terms notes.
- Every page and every newsletter carries a disclosure footer:
  - The research is independent and receives no issuer compensation.
  - It is impersonal and not investment advice.
  - It states the author's holdings policy.
  - It states that data may be wrong.
- Add a `holdings.yaml` and a check that blocks publishing any note on a ticker the author holds, unless a disclosure flag is set. Add a trading blackout reminder for N days around publication.
- No personalized recommendations to individual readers anywhere in the product.
- Add `LEGAL_REVIEW.md` listing the questions for a securities lawyer: publisher's exclusion vs. adviser registration, disclosure language, and scalping rules. Do not give legal conclusions.

## 10. Process rules for you (Claude Code)

1. Start by writing `PLAN.md`: milestones, file tree, data model, and risks. Then execute it.
2. Order of work, finishing each before moving on:
   1. Repo skeleton + DB + xlsx import + EDGAR.
   2. One full vertical slice: one connector → signals → agent note → fact-check → company page → newsletter.
   3. Remaining connectors.
   4. War room.
   5. Public site + GTM commands.
   6. Polish.
3. Commit after every milestone with a clear message. Keep `README.md` current with setup and every command.
4. Write tests as you go. At minimum: connector parsing on fixtures, signal math, citation enforcement (a note with an uncited claim must fail), and the compliance publish-block. Keep `pytest` green before each commit.
5. When something is blocked (a source moved, a key is missing), don't stall. Log it in `BLOCKERS.md` with what you tried and the next step, stub it behind an interface, and continue.
6. Use subagents for independent connector builds if available. Keep the main thread on integration.
7. Keep costs sane: use the fast model for extraction and summaries, the heavy model only for war-room synthesis and the note writer. Cache everything. Print an estimated token cost per command.

**Ask before** (hard stops): deploying anything, sending any email or message, spending money or creating paid accounts, deleting data in `./data/` or `./raw/`, and publishing anything publicly.

## 11. Definition of Done

All of the following must be true:
- `make setup && make seed && make ingest && make signals && make newsletter && make demo` runs end-to-end on a fresh clone. Missing API keys degrade gracefully, never crash.
- The DB holds all 237 companies, with ≥4 of the 6 connectors producing real (non-fixture) signals for at least 25 tickers combined. Every signal has a working citation link.
- One newsletter issue is generated from real signals and passes the fact-check gate, with disclosures present.
- `ll warroom --ticker PCYO --replay`, plus one other ticker of your choice, completes. Two browser sessions see the same live board, and a redirect from one changes the output.
- The public landing, sample issue, pricing, methodology, and disclosures pages render, and the waitlist and pilot forms write to the DB.
- `./out/leads.csv` (13F-based) and ≥20 outreach drafts exist.
- `pytest` passes, `ruff` is clean, and `README.md`, `SOURCES.md`, `BLOCKERS.md` and `LEGAL_REVIEW.md` are complete.
- Finish with `LAUNCH_CHECKLIST.md`: the exact manual steps Andrew needs to take to go live this week (accounts to create, env vars, the deploy command, first 10 outreach targets, and the newsletter platform to paste into), each with an estimated time.

Begin with `PLAN.md`.
