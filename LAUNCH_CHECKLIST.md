# Launch checklist: going live this week

These are the manual steps only Andrew can take. Nothing here has been done for you: no accounts created, nothing deployed, nothing sent. Time estimates assume everything in this repo already works locally (`make test` green).

Total: roughly 6–8 hours across the week.

## Day 1: legal and identity (≈ 2 h)

| # | Step | Time |
|---|---|---|
| 1 | Book a 30-minute consult with a securities lawyer and send them `LEGAL_REVIEW.md`, especially publisher's exclusion vs. adviser registration for the paid platform and the war room. Don't launch the paid tier before this. | 20 min to book + the call |
| 2 | Decide the holdings policy and fill in `config/holdings.yaml` with your actual positions in covered names. The publish block reads this file. | 15 min |
| 3 | Check any Yale SOM or employer policy on publishing research and personal trading. | 15 min |
| 4 | Register a domain (e.g. `localledger.co`, about $12/yr) and set up a sending address (Google Workspace, about $7/mo, or Fastmail). Update `CONTACT_EMAIL` in `.env` so the SEC User-Agent and the site use it. | 30 min |
| 5 | Email the Census BPS team (erd.building.permits@census.gov) about automated monthly downloads; see BLOCKERS.md. Until they reply, drop the four monthly files into `data/inbox/census_bps/`. | 10 min, then 3 min/month |

## Day 2: newsletter platform (≈ 1.5 h)

| # | Step | Time |
|---|---|---|
| 6 | Create a **Beehiiv** account (free up to 2,500 subscribers; Substack also works). Publication name: Local Ledger. Paste the disclosure text from `/disclosures` into the footer template. | 30 min |
| 7 | Run `make newsletter`, review `out/newsletter/<week>.md`, then paste `out/newsletter/<week>.html` into Beehiiv (Custom HTML block). Send a test to yourself and check every source link opens. | 30 min/week |
| 8 | Import the site's waitlist weekly: `/admin` → Export waitlist CSV → Beehiiv import. | 10 min/week |

## Day 3: deploy the site (≈ 1.5 h)

| # | Step | Time |
|---|---|---|
| 9 | Push this repo to a **private** GitHub repo (`git remote add origin ...; git push -u origin main`). | 10 min |
| 10 | Create a **Render** account (render.com). New → Blueprint → select the repo (uses `render.yaml`: Docker web service, starter plan about $7/mo, plus a 5 GB disk). | 20 min |
| 11 | In the Render dashboard set the env vars: `PORTKEY_API_KEY`, `PILOT_PASSCODE`, `ADMIN_PASSCODE` (long random strings), `CONTACT_EMAIL`. `SECRET_KEY` is generated. Keep `LLM_BUDGET_USD=10` until you've watched a few weekly runs. | 10 min |
| 12 | Deploy: Render deploys on push, or click "Manual Deploy" (CLI alternative: `render deploys create <service-id>`). Then open a shell on the service and run `ll ingest && ll signals && ll newsletter && ll warroom --ticker PCYO --replay` to populate the disk. | 30 min |
| 13 | Point the domain at Render (custom domain + CNAME), check HTTPS, and test `/`, `/sample`, `/pricing`, both forms, `/login`, and the war room in two browsers. | 20 min |

## Day 4–5: outreach (≈ 2 h)

| # | Step | Time |
|---|---|---|
| 14 | Run `make leads && make outreach` to refresh the 13F list and drafts. Read every draft; drafts are never sent automatically. | 30 min |
| 15 | Find a named contact for each of the first 10 targets: the 13F signatory (filing index link in `out/leads.csv`), the firm website, LinkedIn. | 45 min |
| 16 | Send the first 10 from your own mailbox, one by one. Then toggle "contacted" on `/admin`. | 30 min |
| 17 | Attach or link `out/pdf/fund_one_pager.pdf` and the sample issue when someone replies. | — |

### First 10 outreach targets (from `out/leads.csv`, 13F period June 30, 2026)

Ranked by meaningful positions (≥ $250K and ≥ 0.25% of their 13F) in universe names, then concentration. Drafts are in `out/outreach/`.

| Rank | Fund | City | Universe names held (top) | Draft |
|---|---|---|---|---|
| 1 | Fourthstone LLC | St. Louis, MO | AVBH MRBK FNWB ISTR PROV (16 names) | `001-fourthstone-llc.md` |
| 2 | Oppenheimer & Close, LLC | New York, NY | BRT FRPH LSBK CPBI TRC (16) | `002-oppenheimer-close-llc.md` |
| 3 | Banc Funds Co LLC | Chicago, IL | AVBH FCBM FRST MNSB MYFW (14) | `003-banc-funds-co-llc.md` |
| 4 | Pacific Ridge Capital Partners, LLC | Lake Oswego, OR | ALCO PKBK FSBW CBK USCB (10) | `004-pacific-ridge-capital-partners-llc.md` |
| 5 | Maltese Capital Management LLC | New York, NY | FRST PCB ISTR MGYR OPBK (10) | `005-maltese-capital-management-llc.md` |
| 6 | Mendon Capital Advisors Corp | Key Largo, FL | FRST USCB FCBM COSO MYFW (9) | `006-mendon-capital-advisors-corp.md` |
| 7 | Peapod Lane Capital LLC | Bedford Hills, NY | ALCO LMNR STHO PCYO HYNE (9) | `007-peapod-lane-capital-llc.md` |
| 8 | Daytona Street Capital LLC | St. Louis, MO | CFBK FSBW CBK USCB MVBF (9) | `008-daytona-street-capital-llc.md` |
| 9 | Stilwell Value LLC | New York, NY | SFBC LSBK CPBI CLST FSBW (8) | `009-stilwell-value-llc.md` |
| 10 | Raffles Associates LP | New York, NY | CBK PROV COSO ESOA MAYS (8) | `010-raffles-associates-lp.md` |

## Weekly operating rhythm (≈ 1 h/week)

1. Monday: `make ingest signals newsletter` (≈ 20 min of machine time; LLM spend shows at the end of each command, and `ll costs` shows the total).
2. Review the issue; if a note mentions a name you hold, use `ll publish --note-id N --disclose` (the blackout reminder prints).
3. Paste into Beehiiv and schedule.
4. On any covered company's earnings day: open a war room (`/warroom`, or `ll warroom --ticker X --replay`), invite the pilot fund, and approve the note.

## Env vars reference

`PORTKEY_API_KEY` (required for live LLM), `LLM_BUDGET_USD` (default 10), `LLM_MODEL_HEAVY` / `LLM_MODEL_FAST` (default gpt-5.6-luna), `LLM_PRICE_IN_PER_MTOK` / `LLM_PRICE_OUT_PER_MTOK`, `CONTACT_EMAIL`, `BRAND_NAME`, `PILOT_PASSCODE`, `ADMIN_PASSCODE`, `SECRET_KEY`, `DATABASE_URL` (optional Postgres), `BLACKOUT_DAYS`, `MAX_NOTES_PER_RUN`, `SIGNAL_NOTE_THRESHOLD`. See `.env.example`.
