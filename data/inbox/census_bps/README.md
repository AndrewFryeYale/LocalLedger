# Census BPS manual inbox

`www2.census.gov`'s robots.txt currently disallows generic automated agents under an RFC 9309 reading (see BLOCKERS.md),
so the connector does not fetch these files itself. Until the Census Bureau confirms automated access:

1. Open https://www2.census.gov/econ/bps/County/ in a browser.
2. Save the latest month's `coYYMMc.txt` and `coYYMMy.txt` (for example `co2608c.txt` and `co2608y.txt`) and the same
   two files for one year earlier (`co2508c.txt`, `co2508y.txt`) into this folder.
3. Optional, for place-level markets (Rio Rancho, NM): from `Place/West Region/`, save `weYYMMc.txt` / `weYYMMy.txt`
   for both years.
4. Run `ll ingest --source census_bps`.

Each file is stored under ./raw with its sha256, and signals cite the canonical census.gov URL for that file.
