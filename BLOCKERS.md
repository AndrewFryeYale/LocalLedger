# Blockers log

Items that didn't work as specified, what was tried, and the next step. Each blocked piece is stubbed behind its interface so the rest of the system keeps running.

_(none yet)_

## Census Building Permits Survey: robots.txt blocks generic agents (RFC 9309 reading)

- **What:** `https://www2.census.gov/robots.txt` begins `User-agent: *`, then a blank line, then `User-agent: RavenCrawler` / `Disallow: /`. Under RFC 9309, blank lines don't end a group, so `*` and `RavenCrawler` share the `Disallow: /` rule and every generic agent (ours included) is disallowed site-wide. Python 3.14's `urllib.robotparser` follows this reading. Under the pre-RFC convention (blank lines end records), `*` would be allowed. The Census Bureau probably intends to allow everyone, but we don't override robots.txt on an assumption.
- **Tried:** api.census.gov (no county/place BPS dataset; only national/regional `eits/resconst`); HUD SOCDS building-permits database (returns HTTP 202 with an empty body, i.e. a bot challenge we won't bypass). During build verification, before the robots issue was spotted, two probe requests fetched `co2608c.txt` and the Arapahoe row of `co2508c.txt`; a few of those rows are used only as offline test fixtures.
- **Stubbed:** the connector is fully built and tested on fixtures. When robots.txt disallows it, it reads files from `data/inbox/census_bps/` (a browser download by a person, see the README there), stores them under `./raw/` and cites the canonical census.gov URL.
- **Next step (Andrew, ~10 min):** email the BPS team (erd.building.permits@census.gov), describing the monthly automated download of ~8 files with our User-Agent, and ask them to confirm or fix the robots.txt grouping. If they confirm in writing, add the entry to `config/robots_permissions.yaml` (template in that file) and the connector fetches automatically. Until then, drop the four monthly files into the inbox.

## NM OCD production reports: Cloudflare Turnstile challenge
- **What:** every `wwwapps.emnrd.nm.gov/ocd/ocdpermitting/Reporting/Production/*.aspx` page linked from the OCD Statistics page (C-115 operator summary/detail, county production) returns "Human Verification Required" (Cloudflare Turnstile). We don't bypass challenges. OCD's bulk data is anonymous FTP only (multi-GB XML table extracts).
- **Workaround (live):** `og_state` uses NM Tech PRRC GO-TECH county-year zips built from the same OCD C-115 data (public, no challenge).
- **Next step:** ask OCD (EMNRD) whether automated access to the C-115 reports is allowed or an HTTPS bulk file exists, so we can cite the state directly.

## Colorado Division of Gaming: CloudFront block
- **What:** `https://sbg.colorado.gov/gaming-revenue`, and even `/robots.txt`, return a CloudFront 403 "Request blocked" to our declared UA. Not worked around. Affects CNTY (Central City, Cripple Creek) and FLL (Bronco Billy's/Chamonix).
- **Stubbed:** `gaming_state` runs on Indiana (FLL Rising Star); the state is a config choice in `config/gaming_map.yaml`.
- **Next step:** email the Division of Gaming for an allowed access path or a direct report-file URL, and record the written basis in `config/robots_permissions.yaml`.

## Illinois Gaming Board: no per-month file to cite
- **What:** IGB property-level casino revenue (incl. FLL American Place, row "FHR-Illinois LLC") is published only through the form app `https://igbapps.illinois.gov/CasinoReports_AEM.aspx` (PDF/CSV export via POST). robots allows it and there's no challenge, but there's no stable per-month URL for `citation_url`.
- **Next step:** decide whether citing the form page plus the stored CSV export is acceptable. If so, add IL as a second gaming state (most of FLL's Midwest revenue).

## Missouri Gaming Commission: not built
- **What:** the bare host `mgc.dps.mo.gov` fails TLS hostname verification; `https://www.mgc.dps.mo.gov/` answers 200 but robots.txt redirects to an error page. Not evaluated further (one gaming state per build). It's the candidate next state for CNTY.

## Shakopee, MN (CPHC): agenda host blocked by robots.txt
- **What:** Shakopee's agendas are only on `shakopeemn.granicus.com`, whose robots.txt says `User-agent: *` / `Disallow: /`. Tried Legistar clients `shakopee`, `scottcounty` and `scottcountymn` (none exist); shakopeemn.gov hosts no agenda PDFs itself.
- **Stubbed:** the town stays in `config/agenda_sources.yaml` with `status: blocked`; the CPHC watchlist is ready.
- **Next step (Andrew, ~10 min):** ask the Shakopee City Clerk for written permission (then record it in `config/robots_permissions.yaml`) or for email delivery of agendas. This is the main local source for CPHC (Canterbury Commons TIF).

## NYC Council Legistar (CLPR, MAYS, NYC, SEG, DCGO): token required
- **What:** the `nyc` Legistar client returns HTTP 403 "Token is required".
- **Next step:** request a free API token from NYC Council; supporting it needs a small token option in `connectors/legistar.py`.

## Stale or missing municipal sources
- `sfgov` (INTG): `/events` returns HTTP 400 and `/matters` stops in 2020; find SF's current agenda source.
- `lacounty` (TRC): Legistar Board events end 2026-06-22; Board of Supervisors agendas now come through `pdf_agenda` (`la-county-bos`).
- Not on Legistar: Kern (TRC; Granicus, robots-blocked, but kernplanning.com allows all agents and is a lead), Loudoun/Fairfax (CHCI), Collier (ALCO), Rio Rancho/Sandoval (AXR), Ventura/Santa Paula (LMNR), Aurora (PCYO), Warren/Trumbull (AWX), Sussex County DE (ARTNA), Chesterfield VA (STHO). **Next step:** find each platform and add one line to `config/agenda_sources.yaml`.

## Build-time compliance notes (disclosed for the record)
- During build verification a few requests went out before the target host's robots.txt had been read: two GETs to `*.legistar1.com` agenda PDFs (Maui, San Bernardino), one GET to `shakopeemn.granicus.com/ViewPublisher.php`, and the two Census probe requests noted above. Nothing from the legistar1/Granicus requests was stored or used, and no connector contacts those hosts; the production HTTP layer checks robots.txt before every request.
