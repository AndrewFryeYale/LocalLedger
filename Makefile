PY ?= python3
VENV := venv
LL := $(VENV)/bin/ll
WEEK ?= $(shell date +%G-W%V)

.PHONY: quickstart demo-data setup seed ingest models signals newsletter demo warroom leads outreach pdfs serve test lint check stats costs citations

$(VENV)/bin/python:
	$(PY) -m venv $(VENV)

quickstart: $(VENV)/bin/python ## fresh clone -> running app in ~2 min: deps + bundled demo data + server (no ingest, no key needed)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -e .
	@test -f .env || cp .env.example .env
	$(LL) demo-load
	@echo "Open http://127.0.0.1:8000  (passcodes: ledger-pilot, ledger-admin)"
	$(VENV)/bin/uvicorn localledger.web.app:app --port 8000

demo-data: ## maintainer: refresh ./demo from the current database and results
	$(LL) demo-export

setup: $(VENV)/bin/python ## venv + deps + .env + analyst-coverage refresh (non-blocking)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -e ".[dev,coverage]"
	@test -f .env || cp .env.example .env
	-$(LL) coverage

seed: ## load 237 companies + archetypes, register sources
	$(LL) seed

ingest: ## run every connector (graceful when a source is down)
	$(LL) ingest

models: ## company models + valuation, then replay signals to record each one's impact (the so-what)
	$(LL) models

signals: models ## rank signals, write analyst notes, fact-check them
	$(LL) signals

newsletter: ## build this week's issue to ./out/newsletter (never sent)
	$(LL) newsletter --week $(WEEK)

demo: ## earnings war-room replay for PCYO
	$(LL) warroom --ticker PCYO --replay

warroom: demo

leads: ## 13F fund target list -> ./out/leads.csv
	$(LL) leads

outreach: ## personalized drafts for top-50 leads -> ./out/outreach (never sent)
	$(LL) outreach

pdfs: ## fund one-pager + sample issue PDFs
	$(LL) pdfs

serve: ## web app on http://127.0.0.1:8000
	$(VENV)/bin/uvicorn localledger.web.app:app --reload --port 8000

test:
	$(VENV)/bin/pytest

lint:
	$(VENV)/bin/ruff check src tests

check: lint test

stats:
	$(LL) stats

costs:
	$(LL) costs

citations: ## HTTP-check every signal citation URL
	$(LL) check-citations
