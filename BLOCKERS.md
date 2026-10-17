# Blockers log

Items that didn't work as specified, what was tried, and the next step. Each blocked piece is stubbed behind its interface so the rest of the system keeps running.

_(none yet)_

## Census Building Permits Survey: robots.txt blocks generic agents (RFC 9309 reading)

- **What:** `https://www2.census.gov/robots.txt` begins `User-agent: *`, then a blank line, then `User-agent: RavenCrawler` / `Disallow: /`. Under RFC 9309, blank lines don't end a group, so `*` and `RavenCrawler` share the `Disallow: /` rule and every generic agent (ours included) is disallowed site-wide. Python 3.14's `urllib.robotparser` follows this reading. Under the pre-RFC convention (blank lines end records), `*` would be allowed. The Census Bureau probably intends to allow everyone, but we don't override robots.txt on an assumption.
- **Tried:** api.census.gov (no county/place BPS dataset; only national/regional `eits/resconst`); HUD SOCDS building-permits database (returns HTTP 202 with an empty body, i.e. a bot challenge we won't bypass). During build verification, before the robots issue was spotted, two probe requests fetched `co2608c.txt` and the Arapahoe row of `co2508c.txt`; a few of those rows are used only as offline test fixtures.
- **Stubbed:** the connector is fully built and tested on fixtures. When robots.txt disallows it, it reads files from `data/inbox/census_bps/` (a browser download by a person, see the README there), stores them under `./raw/` and cites the canonical census.gov URL.
- **Next step (Andrew, ~10 min):** email the BPS team (erd.building.permits@census.gov), describing the monthly automated download of ~8 files with our User-Agent, and ask them to confirm or fix the robots.txt grouping. If they confirm in writing, add the entry to `config/robots_permissions.yaml` (template in that file) and the connector fetches automatically. Until then, drop the four monthly files into the inbox.
