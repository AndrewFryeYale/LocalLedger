# Backtest (as of 2026-10-18)

## 1. Revenue forecasts, point in time
606 company-quarters across 208 companies; each forecast made on the first day of the quarter from filings public at the time.

| metric | value |
|---|---|
| median abs. error, our model | 8.3% |
| median abs. error, same quarter last year | 11.5% |
| median abs. error, last reported quarter | 9.3% |
| model at least as good as both naive forecasts | 42% |
| actual inside our P10-P90 range (target 80%) | 85% |
| Brier score, P(revenue up y/y) (lower is better) | 0.178 vs 0.202 for always forecasting the base rate |

## 2. Banks: call report vs the 10-Q
253 bank-quarters, 74 banks (Q ending Dec 2025, Q ending Jun 2026, Q ending Mar 2026, Q ending Sep 2025).

| metric | value |
|---|---|
| median days the call report lands before the 10-Q (due date / +45-day posting) | 13 / -2 |
| call report is the first public revenue number (due date / +45 days) | 13% / 2% |
| ...in those cases, median days ahead of the company (due date / +45 days) | 12 / 34 |
| ...and the nowcast's median error | 2.2% |
| median abs. error, statistical model | 4.2% |
| median abs. error, call-report nowcast | 2.1% |
| nowcast beats the model | 75% |
| q/q direction right | 77% |

## 3. Events flagged in the last 12 months (abnormal return = stock minus IWM, from the close before)

| event | n | tickers | median 5d | median 20d | median 60d | share down at 20d |
|---|---|---|---|---|---|---|
| Auditor change | 34 | 27 | 0.8% | 0.0% | -1.5% | 50% |
| Local agenda item | 34 | 7 | -0.3% | -3.2% | -3.2% | 66% |
| Listing deficiency / delisting notice | 33 | 22 | -0.6% | -9.7% | -12.7% | 79% |
| Late 10-Q notice | 21 | 16 | -0.0% | -9.5% | -2.6% | 75% |
| Late 10-K notice | 14 | 14 | -6.2% | -11.9% | -18.5% | 85% |
| Non-reliance on prior financials | 12 | 10 | -2.1% | -0.2% | -11.7% | 55% |
| Debt acceleration trigger | 7 | 7 | -9.3% | 4.3% | -0.4% | 33% |
| Bankruptcy or receivership | 5 | 1 | -1.6% | 7.7% | -0.1% | 0% |
| Material impairment | 3 | 2 | -2.3% | 2.2% | 7.2% | 33% |
| All EDGAR risk events | 129 | 60 | -0.9% | -8.1% | -6.0% | 64% |

Agenda items: 34 matches for 7 companies; 23 were later named in an SEC filing, a median 50 days after the meeting (max 105).

## 4. Reading load
5,165 documents in 12 months (10-Q 673, 8-K 2,601, 10-K 225, agenda 1,226, call_report 440) would take about 1,056 hours to read at the stated minutes per document; the system surfaced 163 events from them (3.2%).
