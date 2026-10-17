import json
from datetime import date

import httpx
import pytest
import yaml
from sqlmodel import select

from localledger.connectors.ffiec import (
    CDR_BULK_URL,
    F_DATES,
    F_DOWNLOAD,
    F_PRODUCT,
    FfiecConnector,
    bank_metrics,
    build_bank_map,
    choose_periods,
    facsimile_url,
    holder_candidates,
    match_company,
    normalize_name,
    parse_extract,
    period_options,
    read_bulk_zip,
    render_extract,
    resolve_bank_map,
)
from localledger.http import read_document_text
from localledger.models import Document, Signal
from tests.conftest import FIXTURES, FakeHttp

F = FIXTURES / "ffiec"
TODAY = date(2026, 10, 17)
PERIODS = ("06302026", "03312026", "06302025")
DATE_VALUES = {"152": "06302026", "151": "03312026", "147": "06302025"}  # dropdown values in cdr_bulk_page_dates.html
INST = [r["data"] for r in json.loads((F / "fdic_institutions.json").read_text())["data"]]
TICKERS = ["OVLY", "NSTS", "RMBI", "NEWT", "KFFB"]
KFFB_OVERRIDE = {
    "cert": 29937,
    "rssd": 741974,
    "bank": "First Federal Savings Bank of Kentucky",
    "basis": "10-K names two banks; this is the larger",
}


class CdrFake(FakeHttp):
    """FakeHttp plus the CDR's ASP.NET form: POSTs are answered by the posted fields, like the real page."""

    def __init__(self, routes=None, download=None):
        base = {
            CDR_BULK_URL: F / "cdr_bulk_page.html",
            "https://api.fdic.gov/banks/institutions*": F / "fdic_institutions.json",
        }
        super().__init__({**base, **(routes or {})})
        self.download = download
        self.posts: list[dict] = []

    def post(self, url, **kwargs):
        data = kwargs.get("data") or {}
        self.posts.append(data)
        req = httpx.Request("POST", url)
        if F_DOWNLOAD in data:
            period = DATE_VALUES[data[F_DATES]]
            if self.download:
                return httpx.Response(
                    200, content=self.download(period), headers={"content-type": "text/html"}, request=req
                )
            disposition = f'attachment; filename="FFIEC CDR Call Bulk All Schedules {period}.zip"'
            headers = {"content-type": "application/octet-stream", "content-disposition": disposition}
            return httpx.Response(
                200, content=(F / f"call_bulk_{period}.zip").read_bytes(), headers=headers, request=req
            )
        if data.get("__EVENTTARGET") == F_PRODUCT and "__VIEWSTATE" in data:
            html = (F / "cdr_bulk_page_dates.html").read_bytes()
            return httpx.Response(200, content=html, headers={"content-type": "text/html; charset=utf-8"}, request=req)
        return httpx.Response(400, content=b"bad postback", request=req)


def _map_file(tmp_path, overrides=None):
    path = tmp_path / "bank_map.yaml"
    path.write_text(yaml.safe_dump({"overrides": overrides or {}, "auto": {}, "unmapped": {}}))
    return path


# --- mapping ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,norm",
    [
        ("BANK OF THE JAMES FINANCIAL GROUP INC", "BANK JAMES FINANCIAL GROUP"),
        ("BK JAMES FINL GROUP INC", "BANK JAMES FINANCIAL GROUP"),
        ("PEOPLES BCORP OF NC INC", "PEOPLES BANCORP NORTH CAROLINA"),
        ("PEOPLES BANCORP OF NORTH CAROLINA INC", "PEOPLES BANCORP NORTH CAROLINA"),
        ("FIRST UNITED CORP/MD/", "FIRST UNITED"),
        ("BROADWAY FINANCIAL CORP DE", "BROADWAY FINANCIAL"),
        ("CITIZENS&NORTHERN CORP", "CITIZENS & NORTHERN"),
        ("WEST BANCORPORATION INC", "WEST BANCORP"),
        ("WEST BCORP INC", "WEST BANCORP"),
        ("RHINEBECK BCORP MHC", "RHINEBECK BANCORP"),
    ],
)
def test_normalize_name(raw, norm):
    assert normalize_name(raw) == norm


def test_largest_bank_in_a_multibank_holding_company():
    ames = next(c for c in holder_candidates(INST) if c["name"] == "AMES NATIONAL CORP")
    assert ames["n_banks"] == 6
    assert ames["top"]["NAME"] == "First National Bank, Ames, Iowa"
    m = match_company(["AMES NATIONAL CORP", "Ames National"], "IA", holder_candidates(INST))
    assert (m["cert"], m["banks_in_holding"], m["confidence"]) == (ames["top"]["CERT"], 6, "high")


def test_state_breaks_ties_and_ambiguity_is_never_guessed():
    cands = holder_candidates(INST)
    vt = match_company(["COMMUNITY BANCORP /VT", "Community Bancorp"], "VT", cands)
    assert vt["state"] == "VT" and vt["match"] == "name_exact+state" and vt["confidence"] == "high"
    assert "ambiguous" in match_company(["Community Bancorp"], None, cands)["reason"]
    assert (
        "ambiguous" in match_company(["First Bank"], None, cands)["reason"]
    )  # FRBA: First Bank (NJ) vs FIRST BANK CORP


def test_low_confidence_fuzzy_match_is_rejected():
    # FGBI's top-tier holder is now SMITH&HOOD HOLDING CO; the closest name is a different Louisiana bank.
    result = match_company(["First Guaranty Bancshares, Inc."], "LA", holder_candidates(INST))
    assert "cert" not in result
    assert "GREAT GUARANTY BANCSHARES INC" in result["reason"] and "no confident match" in result["reason"]


def test_overrides_win_and_existing_matches_are_kept():
    companies = {
        "OVLY": {"name": "Oak Valley Bancorp", "sec_name": "Oak Valley Bancorp", "state": "CA"},
        "KFFB": {"name": "Kentucky First Federal Bancorp", "sec_name": None, "state": "KY"},
        "NSTS": {"name": "NSTS Bancorp", "sec_name": None, "state": None},
        "FGBI": {"name": "First Guaranty Bancshares", "sec_name": "First Guaranty Bancshares, Inc.", "state": "LA"},
        "RMBI": {"name": "Richmond Mutual Bancorporation", "sec_name": None, "state": "IN"},
    }
    existing = {
        "overrides": {"KFFB": KFFB_OVERRIDE, "OVLY": {"cert": 1, "rssd": 2, "bank": "Hand-picked Bank"}},
        "auto": {
            "KFFB": {"cert": 31197, "rssd": 831875, "bank": "stale auto entry"},
            "NSTS": {"cert": 27683, "rssd": 775773, "bank": "kept as is"},
            "RMBI": {"cert": 999999, "rssd": 1, "bank": "no longer active"},
        },
    }
    data = build_bank_map(companies, INST, existing, TODAY)
    assert "OVLY" not in data["auto"] and "KFFB" not in data["auto"]
    assert data["auto"]["NSTS"]["bank"] == "kept as is"
    assert data["auto"]["RMBI"]["cert"] == 28533  # inactive cert -> re-matched
    assert "FGBI" in data["unmapped"] and data["unmapped"]["FGBI"]["reason"]
    resolved = resolve_bank_map(data)
    assert resolved["OVLY"]["bank"] == "Hand-picked Bank" and resolved["OVLY"]["source"] == "override"
    assert resolved["KFFB"]["cert"] == 29937
    both = resolve_bank_map({"auto": {"X": {"cert": 1}}, "overrides": {"X": {"cert": 2}}})
    assert both["X"]["cert"] == 2


# --- parsing and math -------------------------------------------------------------------------------------------------
def test_periods_and_dropdown():
    options = period_options((F / "cdr_bulk_page_dates.html").read_bytes())
    assert options["06302026"] == "152" and options["03312026"] == "151"
    assert choose_periods(list(options), TODAY) == PERIODS
    with_q3 = ["09302026", *options]
    assert choose_periods(with_q3, date(2026, 11, 1))[0] == "06302026"  # Q3 not complete yet
    assert choose_periods(with_q3, date(2026, 11, 20)) == ("09302026", "06302026", "09302025")


def test_bulk_zip_to_extract_roundtrip():
    parsed = read_bulk_zip((F / "call_bulk_06302026.zip").read_bytes(), {"1864197"})
    assert parsed["period"] == "06302026" and set(parsed["rows"]) == {"1864197"}
    por, row = parsed["por"]["1864197"], parsed["rows"]["1864197"]
    assert (por["cert"], por["name"], por["filing_type"]) == ("33457", "OAK VALLEY COMMUNITY BANK", "041")
    assert row["RCONB529"] == "1152575" and row["RCOA3792"] == "241972" and row["RCFDB529"] == ""
    text = render_extract(por, ["06302026"], {"06302026": row}, {})
    back = parse_extract(text)
    assert back["bank"] == {"name": "OAK VALLEY COMMUNITY BANK", "rssd": "1864197", "cert": "33457"}
    assert back["items"]["net_loans"]["06302026"] == 1152575 and back["codes"]["net_loans"] == "RCONB529"
    assert "RCONB529\tLoans and leases, net of unearned income and allowance\t1152575" in back["sections"]["loans"]
    assert all(block in text for block in back["sections"].values())


def _metrics(rssd):
    bulk = {p: read_bulk_zip((F / f"call_bulk_{p}.zip").read_bytes(), {rssd}) for p in PERIODS}
    rows = {p: bulk[p]["rows"][rssd] for p in PERIODS}
    items = parse_extract(render_extract(bulk[PERIODS[0]]["por"][rssd], list(PERIODS), rows, {}))["items"]
    return [bank_metrics(items, p) for p in PERIODS]


def test_metric_math():
    cur, pri, _ = _metrics("1864197")  # Oak Valley: files total risk-based capital
    assert cur["cre"] == 0 + 57605 + 97668 + 543416 + 7866  # construction + multifamily + non-owner-occ + unsecured
    assert cur["cre_basis"] == "total risk-based capital" and cur["capital"] == 241972
    assert cur["cre_ratio"] == pytest.approx(706555 / 241972)
    assert cur["nib_share"] == pytest.approx(568341 / 1764388)
    assert cur["noncurrent"] == 50 and pri["noncurrent"] == 195 + 4574
    assert cur["noncurrent_ratio"] == pytest.approx(50 / 1163747)
    assert cur["npa_assets"] == pytest.approx((50 + 2581) / 2001344)
    cblr = _metrics("775773")[0]  # North Shore Trust: CBLR bank, no total risk-based capital reported
    assert cblr["cre_basis"] == "Tier 1 capital + ACL" and cblr["capital"] == 69402


# --- connector --------------------------------------------------------------------------------------------------------
def test_ffiec_run_on_fixtures(seeded, tmp_path):
    path = _map_file(tmp_path, {"KFFB": KFFB_OVERRIDE})
    http = CdrFake()
    conn = FfiecConnector(seeded, http=http, bank_map_path=path, today=TODAY, origin="fixture")
    result = conn.run(tickers=TICKERS)
    assert result.status == "available", result.errors
    assert result.signals == 20 and result.tickers == set(TICKERS)
    assert len([p for p in http.posts if F_DOWNLOAD in p]) == 3

    saved = yaml.safe_load(path.read_text())
    assert set(saved["auto"]) == {"OVLY", "NSTS", "RMBI", "NEWT"} and set(saved["overrides"]) == {"KFFB"}
    assert saved["auto"]["OVLY"]["cert"] == 33457 and saved["auto"]["OVLY"]["confidence"] == "high"
    assert path.read_text().startswith("# Ticker -> FDIC-insured bank")

    sigs = {(s.ticker, s.metric): s for s in seeded.exec(select(Signal).where(Signal.source_id == "ffiec")).all()}
    loans = sigs[("OVLY", "net_loans_qoq")]
    assert loans.value == 1_152_575_000 and loans.prior_value == 1_132_713_000 and loans.period == "2026-06-30"
    assert loans.summary == (
        "Oak Valley Bancorp (Oak Valley Community Bank): net loans & leases $1.15B at 6/30/26, +1.8% q/q, +5.1% y/y."
    )
    assert loans.citation_url == facsimile_url(1864197, "06302026")
    assert loans.citation_url.startswith("https://cdr.ffiec.gov/public/ViewFacsimileDirect.aspx?ds=call&idType=ID_RSSD")

    deposits = sigs[("OVLY", "deposit_mix")]
    assert "noninterest-bearing deposits 32.2% of total deposits at 6/30/26 (-0.6 pts q/q)" in deposits.summary
    cre = sigs[("RMBI", "cre_concentration")]
    assert "CRE loans 304% of total risk-based capital" in cre.summary and "crossed above the 300%" in cre.summary
    assert cre.significance >= 0.8
    assert "Tier 1 capital + ACL" in sigs[("NSTS", "cre_concentration")].summary
    npl = sigs[("NSTS", "noncurrent_loans")]
    assert "noncurrent loans 1.87% of loans at 6/30/26 vs 0.22% at 3/31/26" in npl.summary
    assert npl.significance >= 0.75
    assert "total loans incl. held for sale +21.5% q/q" in sigs[("NEWT", "net_loans_qoq")].summary
    assert sigs[("KFFB", "net_loans_qoq")].summary.startswith(
        "Kentucky First Federal Bancorp (First Federal Savings Bank of Kentucky):"
    )

    for s in sigs.values():
        detail = json.loads(s.detail_json)
        doc = seeded.get(Document, s.document_id)
        assert doc.origin == "fixture" and doc.ticker == s.ticker and doc.content_type == "text/plain"
        assert detail["excerpt"] in read_document_text(doc)  # verbatim, so the fact-checker can find it
        assert detail["excerpt"].startswith("[") and "in thousands of USD" in detail["excerpt"]
        assert detail["prior_document_id"] == doc.id and set(detail["source_documents"]) == set(PERIODS)
        zips = [seeded.get(Document, i) for i in detail["source_documents"].values()]
        assert all(z.content_type == "application/zip" and z.sha256 in read_document_text(doc) for z in zips)
    assert sum(1 for s in sigs.values() if s.metric == "net_loans_qoq") == 5


def test_rerun_reuses_closed_quarters_and_mapping(seeded, tmp_path):
    path = _map_file(tmp_path, {"KFFB": KFFB_OVERRIDE})
    FfiecConnector(seeded, http=CdrFake(), bank_map_path=path, today=TODAY, origin="fixture").run(tickers=TICKERS)
    http = CdrFake()
    result = FfiecConnector(seeded, http=http, bank_map_path=path, today=TODAY, origin="fixture").run(tickers=TICKERS)
    assert result.signals == 20, result.errors
    assert [DATE_VALUES[p[F_DATES]] for p in http.posts if F_DOWNLOAD in p] == ["06302026"]  # latest only
    assert not any("api.fdic.gov" in c for c in http.calls)  # everything already mapped
    assert len(seeded.exec(select(Signal).where(Signal.source_id == "ffiec")).all()) == 20


def test_bot_challenge_is_unavailable(seeded, tmp_path):
    http = CdrFake({CDR_BULK_URL: (403, b"Access denied")})
    result = FfiecConnector(seeded, http=http, bank_map_path=_map_file(tmp_path), today=TODAY).run(tickers=["OVLY"])
    assert result.status == "unavailable"
    assert "HTTP 403" in result.errors[0] and "not bypassed" in result.errors[0]


def test_fdic_down_with_nothing_mapped_is_unavailable(seeded, tmp_path):
    conn = FfiecConnector(seeded, http=FakeHttp({}), bank_map_path=_map_file(tmp_path), today=TODAY)
    result = conn.run(tickers=["OVLY"])
    assert result.status == "unavailable" and "FDIC BankFind" in result.errors[0]


def test_non_zip_download_fails_the_target_not_the_run(seeded, tmp_path):
    http = CdrFake(download=lambda period: b"<html>Your session has expired</html>")
    result = FfiecConnector(seeded, http=http, bank_map_path=_map_file(tmp_path), today=TODAY).run(tickers=["OVLY"])
    assert result.signals == 0 and result.status == "unavailable"
    assert any("instead of a zip" in e for e in result.errors)
