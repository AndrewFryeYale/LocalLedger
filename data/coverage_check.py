"""
Fill in analyst coverage (and refresh market caps) for every name in
Microcap_Local_Gov_Universe.xlsx using Yahoo Finance.

Run on your own computer (needs internet):
    pip install yfinance openpyxl
    python coverage_check.py Microcap_Local_Gov_Universe.xlsx

Writes Microcap_Local_Gov_Universe_checked.xlsx next to the input, plus
coverage_results.csv. Priority scores recalculate when you open the file in Excel.

Caveat: Yahoo's analyst count misses some single-broker and sponsored coverage.
Treat "0 (checked)" as "no consensus coverage on Yahoo" and confirm on
Bloomberg (ANR) or CapIQ before calling a name uncovered in print.
"""
import csv
import sys
import time
from pathlib import Path

import yfinance as yf
from openpyxl import load_workbook

SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "Microcap_Local_Gov_Universe.xlsx")
OUT = SRC.with_name(SRC.stem + "_checked.xlsx")
HEADER_ROW = 4          # header row on the Universe tab
COL_TICKER, COL_MCAP, COL_COV = 1, 3, 8
KEEP_MANUAL = {"0 (checked)", "1 (checked)", "2+ (checked)"}  # don't overwrite hand-verified rows


def bucket(n):
    if n is None or n == 0:
        return "0 (checked)"
    return "1 (checked)" if n == 1 else "2+ (checked)"


wb = load_workbook(SRC)            # keeps formulas intact
ws = wb["Universe"]
results = []
row = HEADER_ROW + 1
while ws.cell(row=row, column=COL_TICKER).value:
    t = str(ws.cell(row=row, column=COL_TICKER).value).strip()
    current = ws.cell(row=row, column=COL_COV).value
    n, mcap, err = None, None, ""
    for attempt in range(3):
        try:
            info = yf.Ticker(t).info or {}
            n = info.get("numberOfAnalystOpinions")
            mcap = info.get("marketCap")
            break
        except Exception as e:  # rate limits / transient errors
            err = str(e)[:80]
            time.sleep(3 * (attempt + 1))
    if current not in KEEP_MANUAL and not err:
        ws.cell(row=row, column=COL_COV).value = bucket(n)
    if mcap:
        ws.cell(row=row, column=COL_MCAP).value = round(mcap / 1e6, 2)
    results.append({"ticker": t, "analysts_yahoo": n, "market_cap_musd": round(mcap / 1e6, 2) if mcap else "",
                    "coverage_cell": ws.cell(row=row, column=COL_COV).value, "error": err})
    print(f"{t:6} analysts={n!s:5} mcap={results[-1]['market_cap_musd']}")
    time.sleep(0.6)
    row += 1

wb.calculation.fullCalcOnLoad = True
wb.save(OUT)
with open(SRC.with_name("coverage_results.csv"), "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
    w.writeheader()
    w.writerows(results)
zero = sum(1 for r in results if r["coverage_cell"] == "0 (checked)")
print(f"\nDone: {len(results)} names, {zero} with zero Yahoo coverage -> {OUT}")
