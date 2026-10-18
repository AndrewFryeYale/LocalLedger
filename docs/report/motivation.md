# Motivation and what is new (report sections 2 and 3)

Draft for `report.html`. Every number comes from this repo's data (`ll stats`, `ll backtest`, `out/leads.csv`) or from a cited outside source. Outside prices are third-party estimates where the vendor publishes none, and they are labeled that way.

## 2. Motivation

### The problem

Wall Street covers few small public companies. In our universe of 237 US-listed names (234 of them under $500M in market value), **100 have zero consensus analyst estimates** (Yahoo Finance check, `data/coverage_check.py`). Yet many of these stocks move on **local** news before it reaches an SEC filing:

- a **town planning board** approving a land developer's subdivision;
- a **community bank's quarterly call report** to the FFIEC;
- a **state oil and gas production report** for a royalty trust;
- a **state gaming commission's** monthly casino revenue.

Those records are public but scattered across thousands of websites in a dozen formats. Reading everything that touched our 237 names in the last 12 months would mean **5,165 documents and about 1,056 hours**: 8-Ks, 10-Qs, 10-Ks, 1,226 council agendas and 440 call reports, at 5 to 60 minutes each (`ll backtest`, reading-load study). That is roughly half an analyst-year, for a universe that a small fund can't afford to staff.

### The person behind it

The user is **the analyst or portfolio manager at a small value fund that owns microcaps**: often a team of two to five people, holding names no sell-side analyst follows.

Public 13F filings show who these people are. **Oppenheimer & Close, LLC** (New York) is one example; they are #2 on our 13F lead list. As of June 30, 2026 they reported **16 positions in our universe, worth $32.4M**, about 20% of their $158.5M 13F portfolio. **12 of those 16 names have zero analyst coverage**: 12 community banks, 2 land developers, an apartment owner and a commercial property owner. Keeping up with just those 16 names over the last 12 months meant reading 131 8-Ks, 48 10-Qs, 16 10-Ks and 48 call reports, about **59 hours of reading**. That is before any town agenda.

What that reading would have caught: **FRP Holdings (FRPH)**, one of their zero-coverage holdings, filed a late-10-K notice on March 31, 2026 ([SEC Form 12b-25](https://www.sec.gov/Archives/edgar/data/844059/000084405926000026/form12b-25xntform10xkfy2025.htm)). Over the next 20 trading days FRPH fell **17.2% relative to the Russell 2000 (IWM)**. Across all 14 late-10-K notices in our universe the median was −11.9%, and 85% fell. Local Ledger flags that filing the day it appears.

We have not spoken with Oppenheimer & Close. Their 13F is used only as public evidence that this kind of holder exists and what their workload looks like. The real-user test (Impact section) replaces this example with a named tester's own before-and-after time.

## 3. What is new here

### Existing products, compared honestly

| Product | What it reads | Who pays, and how much | Where it is stronger | What it misses for this user |
|---|---|---|---|---|
| **Bloomberg Terminal** | Market data, news, SEC filings, consensus estimates | The reader; about $2,360/month per seat ([CostBench](https://www.costbench.com/compare/koyfin-vs-bloomberg-terminal/)) | Real-time prices, breadth, messaging | Town agendas, call-report line items and state agency data aren't linked to the stocks they move |
| **AlphaSense** | Search across filings, transcripts, broker research, expert calls | The reader; about $10K–$20K per seat per year, no public list price ([Sacra](https://sacra.com/c/alphasense/); Vendr median $17.5K via [AllMind](https://allmind.ai/research/alphasense-pricing-2026)) | Largest document corpus; expert-call library | It's a search tool, so you have to know what to ask. Its corpus is national-level documents, not municipal or state records. Priced for large teams |
| **Fintool** | AI answers from SEC filings and earnings calls | The reader; sales-led, no public price ([fintool.com/pricing](https://fintool.com/pricing)) | Fast Q&A over filings | Same SEC-only corpus. It doesn't watch what happens before a filing |
| **Koyfin** | Charts, fundamentals, estimates | The reader; $0–$79/month on annual plans ([Koyfin pricing guide](https://www.koyfin.com/pricing-llm-info/)) | Cheap, polished data terminal | No primary-source local records; estimates are thin where coverage is zero |
| **Issuer-paid research** (e.g. Noble Capital Markets / Channelchek) | A covered company's filings, plus management access | **The company being covered**; IR Magazine gives $45,000 a year as an example cost ([IR Magazine](https://irmagazine.com/sell-side/filling-gap-what-know-about-paying-equity-research); [Channelchek on company-sponsored research](https://dashboard.channelchek.com/?p=48586)) | Full models and price targets; analyst access | It covers only companies that pay, and the payer is the subject. Local Ledger is never paid by issuers (see Disclosures) |
| **Local-government monitoring** (Curate, Quorum Local; Shovels + ReZone for permits and meeting decisions) | Agendas, minutes and permits from thousands of governments | Sales, public-affairs and real-estate teams ([Curate](https://platform.softwareone.com/product/curate/PCP-9784-4606); [Quorum Local](https://platform.softwareone.com/product/quorum-local/PCP-6416-3163); [Shovels](https://commercialobserver.com/?p=523692)) | Far wider coverage: Curate lists 12,000+ entities; we cover 12 local governments | They don't map records to public-company tickers or tie them to company financials. No fact-checked investment note |

### What is new

1. **The join.** Local Ledger links a municipal agenda, a call report or a state production file to the ticker it affects. It then updates that company's revenue model and writes a cited note. No product above does all three steps: the investor tools don't read local records, and the local-government tools don't know the stock.
2. **Measured lead time, not a claim.** In a point-in-time backtest over the last 12 months:
   - town agenda items appeared a **median 50 days before** the first SEC filing that mentioned the project (23 of 34 items, up to 105 days);
   - risk filings flagged on the day they were filed were followed by a **median −8.1%** 20-day return relative to IWM (129 events across 60 names).
3. **Trust built into the pipeline.** Every claim must quote its source verbatim, and every number must appear in that quote; anything else is dropped before a person sees it. A red-team test shows planted instructions in a council agenda never reaching a published note.
4. **A multiplayer war room for earnings.** Eight agents draft a post-earnings note while a fund's analysts watch, redirect the bear case or take over a section. A person approves the result.
5. **Priced for the small fund.** The LLM cost of serving one fund is **about $0.09 per month** (measured token counts; `ll tokenomics`). That makes a $99/month seat or a free newsletter workable, where incumbents charge $10K+ per seat.

### Honest limits of the comparison

- Incumbents cover far more ground. We read 7 source types and 12 local governments, against Curate's 12,000+ entities and AlphaSense's broker and expert library.
- Competitor prices are third-party estimates or published list prices as of October 2026. Negotiated prices differ.
- Lead times and returns come from a 12-month backtest over one universe and are not a forecast. The agenda result rests on 34 items from 7 companies.
