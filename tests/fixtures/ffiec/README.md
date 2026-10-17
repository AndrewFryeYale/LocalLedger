Offline fixtures for `tests/test_ffiec.py`. Everything here was fetched live on 2026-10-17 during build verification
with the Local Ledger User-Agent. It is real data, trimmed for size.

- `cdr_bulk_page.html`: real `GET https://cdr.ffiec.gov/public/PWS/DownloadBulkData.aspx`, untrimmed.
- `cdr_bulk_page_dates.html`: real response to the product postback ("Call Reports -- Single Period"), which lists the
  report dates (06/30/2026 = option 152, 03/31/2026 = 151, 06/30/2025 = 147). Untrimmed.
- `call_bulk_{06302026,03312026,06302025}.zip`: real "FFIEC CDR Call Bulk All Schedules {date}.zip" downloads (TSV),
  trimmed. Only Readme.txt, POR and schedules RC, RCCI, RCE, RCN and RCRI are kept, with their two header lines and the
  rows for five banks: Oak Valley Community Bank (IDRSSD 1864197, OVLY), North Shore Trust and Savings (775773, NSTS),
  First Bank Midwest (419675, RMBI), Newtek Bank, N.A. (502111, NEWT) and First Federal Savings Bank of Kentucky
  (741974, KFFB). Every kept line is byte-identical to the original file. The real zips are ~6 MB each.
- `fdic_institutions.json`: real `https://api.fdic.gov/banks/institutions?filters=ACTIVE:1&fields=...&limit=10000`
  response (4,219 rows), trimmed to 24 rows: the five banks above, KFFB's second bank, First Bank (NJ), City First Bank,
  First Guaranty Bank, and the holders that test the matcher (both COMMUNITY BCORPs, both UNION BANKSHARES INCs,
  FIRST BANK CORP, GREAT GUARANTY BANCSHARES INC and Ames National's six banks). `meta.total` and `totals.count` were
  set to 24 to match the trimmed rows. No values were changed.
