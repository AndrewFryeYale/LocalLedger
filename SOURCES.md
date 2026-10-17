# Source register

Every source is public. We use no logins, no paywalls and no expert networks. Each fetch declares the User-Agent `Local Ledger research aceamf25@gmail.com` and honors robots.txt (RFC 9309: 4xx on robots.txt = allow, 5xx/unreachable = disallow). Raw responses are stored under `./raw/<source>/` with their sha256.

| id | Source | Access method (verified at build) | Cadence | Terms / notes |
|---|---|---|---|---|
| `edgar` | SEC EDGAR | `www.sec.gov/files/company_tickers.json`; `data.sec.gov/submissions/CIK##########.json`; filing `-index.htm` pages for EX-99.1; primary docs under `/Archives/edgar/data/`; XBRL frames `data.sec.gov/api/xbrl/frames/us-gaap/{tag}/USD/CY{yyyy}Q{q}.json`. Verified 2026-10-17. | daily | SEC fair access: declared UA with email, <=10 req/s (we use ~7.5/s). robots.txt allows `/Archives/edgar/data` and `/files/`. data.sec.gov has no robots.txt (4xx = allow). |
