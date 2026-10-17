# Questions for a securities lawyer

These are open questions to put to qualified securities counsel before launch. They are deliberately not answered here: nothing in this repo is a legal conclusion.

Context to share with counsel: Local Ledger publishes impersonal research on US-listed microcaps built only from public records (SEC filings, municipal agendas, government statistics, state regulatory data). Products: a free weekly newsletter, a $99/month analyst subscription to a web platform (signal feed, company pages), and paid fund pilots/licenses that include a multiplayer "earnings war room" where subscribers can steer AI agents. The author is an MBA student and former buy-side analyst who may hold positions in covered securities.

## 1. Investment adviser status
1. Does the newsletter, as structured, fit the "publisher's exclusion" from the definition of investment adviser (Advisers Act §202(a)(11)(D), *Lowe v. SEC*)? What facts matter most: bona fide publication, general and regular circulation, impersonal content?
2. Does the **paid platform** change the analysis? It has a ranked feed, company pages, and alerts by ticker or archetype. Is a subscriber-configurable watchlist or alert "personalized"?
3. Does the **fund pilot / earnings war room** change the analysis? A fund's analysts can redirect agents ("rerun the bear case assuming lower volumes") and approve a note. Is that interactive, client-specific output, and does it move the service outside the exclusion?
4. Are custom watchlists, custom towns/datasets, or bespoke research requests for a fund acceptable, or are they advisory services?
5. Is state investment-adviser registration (or notice filing) triggered anywhere, given subscribers in multiple states and the author's state of residence? Are there state-level publisher exemptions to rely on?
6. Does using the word "signal", ranking names by "significance", or ranking by "priority" read as a recommendation for these purposes?

## 2. Disclosure language
7. Review the disclosure footer (`src/localledger/compliance.py` → `disclosures()`; shown on every page, issue and PDF). Is it adequate and accurate, and what's missing (e.g. past-performance language, no-guarantee language, conflicts, AI-generation disclosure)?
8. How specific must holdings disclosure be (ticker, direction, size range, date acquired), and where must it appear (each note, each issue, the site)?
9. Should research that AI agents generate and a human reviews carry a specific disclosure about the process and its error rate?
10. Are the "Data may be wrong" and source-citation practices sufficient to manage liability for errors in public records or in our mapping of records to companies?

## 3. Personal trading, scalping and market abuse
11. Given *SEC v. Park* and *SEC v. Blavin*-type scalping theories, what personal-trading policy is required: pre-clearance, blackout windows (the repo defaults to ±3 days, `BLACKOUT_DAYS`), minimum holding periods, a ban on trading against published research?
12. Is the technical block on publishing notes about held tickers unless a disclosure flag is set (`compliance.check_publish`) adequate as a control, or should held names be excluded outright?
13. Do fund-pilot customers receiving research before newsletter readers raise front-running or selective-dissemination concerns? Should there be a uniform release time?
14. Any Regulation FD-adjacent or MNPI risk from municipal records that are public but obscure (e.g. a planning-board packet posted before a company press release)? Is "public record" sufficient, or does accessibility matter?

## 4. Data sources and terms
15. Are there terms-of-use, copyright or database-right concerns with redistributing excerpts and quotes from municipal agendas, state datasets and SEC filings in a paid product?
16. Where robots.txt is ambiguous (e.g. www2.census.gov, logged in BLOCKERS.md), is written permission from the agency sufficient and advisable? What should we keep on file?
17. Is citing and linking to state regulator reports (gaming, oil and gas) in a commercial product consistent with those sites' terms?

## 5. Marketing and outreach
18. Cold outreach to funds identified from 13F filings: any CAN-SPAM, state or anti-solicitation issues? Required opt-out language for one-to-one emails?
19. Can we say "independent", "no issuer compensation", "every claim cited" and "fact-checked" in marketing, and what substantiation should we keep?
20. Testimonials or pilot-customer logos: what restrictions apply (Advisers Act marketing rule, if it applies at all)?

## 6. Entity, insurance and contracts
21. Recommended entity structure and whether to carry media liability or E&O insurance before launch.
22. What should the subscriber terms of service and the fund-pilot agreement say about no advice, no reliance, limitation of liability, confidentiality of pilot feedback, and data usage?
23. Any school or employer policy (Yale SOM or future employers) to clear before publishing research and trading personally?
