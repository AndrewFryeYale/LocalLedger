Offline fixtures for the `og_state` connector (New Mexico OCD C-115 production via NM Tech GO-TECH). All real, fetched
2026-10-17 during build verification with the Local Ledger User-Agent, then trimmed:

- `San Juan2026.zip`, `San Juan2025.zip`, `Rio Arriba2026.zip`, `Rio Arriba2025.zip`: re-zipped subsets of the real
  GO-TECH county-year files (`https://octane.nmt.edu/gotech/Petroleum_Data/county/{County}{yyyy}.zip`). Each keeps the
  real header and real, unmodified CSV lines: the 8 (San Juan) / 4 (Rio Arriba) HILCORP ENERGY COMPANY rows with the
  most August 2026 gas, plus two rows from other operators (to test operator filtering), and the 2025 rows for the
  same API numbers. Totals in the tests are therefore subset totals, not Hilcorp's full county production.
- `county.aspx.html`: the real GO-TECH "Search by County" page (San Juan result), with scripts, styles, ASP.NET
  hidden fields and pre-2024 download links removed.
- `ocd_turnstile.html`: the real Cloudflare Turnstile "Human Verification Required" page that
  `wwwapps.emnrd.nm.gov/ocd/ocdpermitting/Reporting/Production/C115BalancingSummary.aspx` returned (why the OCD's
  own report pages are not used). Used to test that a bot challenge makes the source unavailable.
- `sjt_ex99_1.htm`: SJT's 8-K Ex. 99.1 filed 2026-09-18 (September 2026: no distribution), byte-for-byte as stored
  by the EDGAR connector.
