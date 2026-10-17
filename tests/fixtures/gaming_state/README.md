Offline fixtures for the `gaming_state` connector (Indiana Gaming Commission monthly revenue reports). All real,
fetched 2026-10-17 during build verification with the Local Ledger User-Agent, then trimmed:

- `2026-01-Revenue.pdf`, `2026-02-Revenue.pdf`, `2025-01-Revenue.pdf`, `2025-02-Revenue.pdf`, `2026-09Revenue.pdf`,
  `2025-09-Revenue.pdf`: the real IGC monthly revenue report PDFs (from `www.in.gov/dA/<id>/YYYY-MM-Revenue.pdf` and
  `www.in.gov/igc/files/reports/2026/2026-09Revenue.pdf`) cut to two pages with pypdf: page 1 (wagering tax, win /
  free play / taxable AGR, table and EGD win by casino) and page 6 (EGD units, coin-in and win). Page content is
  unmodified; the other pages (YTD tax, table-game detail, sports wagering) were dropped for size.
- `monthly-revenue.html`, `archived-index.html`, `archived-2025.html`: minimal pages holding the real `<a>` tags
  (hrefs and link text verbatim) from `www.in.gov/igc/publications/monthly-revenue/`, the archive index and the 2025
  archive page, keeping only the January and February links, so the tests see February 2026 as the latest month.
